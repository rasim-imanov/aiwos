"""Small shared helpers: time, ids, atomic files, locking, path patterns."""

import contextlib
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import time
import uuid


class AiwosError(Exception):
    """User-facing error: printed without a traceback."""


# ---------------------------------------------------------------- time / ids

def now():
    return _dt.datetime.now(_dt.timezone.utc)


def iso(t=None):
    t = t or now()
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


_last_event_t = [None]


def event_ts():
    """Microsecond UTC timestamp, strictly increasing within this process (stable event order)."""
    t = now()
    if _last_event_t[0] is not None and t <= _last_event_t[0]:
        t = _last_event_t[0] + _dt.timedelta(microseconds=1)
    _last_event_t[0] = t
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s):
    if not s:
        return None
    s = s.rstrip("Z")
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
    return _dt.datetime.strptime(s, fmt).replace(tzinfo=_dt.timezone.utc)


def age_seconds(ts):
    t = parse_iso(ts)
    return (now() - t).total_seconds() if t else float("inf")


def rand_id(prefix, n=8):
    return "%s-%s" % (prefix, uuid.uuid4().hex[:n])


def event_id():
    # Time-ordered and globally unique enough for multi-machine union merges.
    return "EV-%s-%s" % (now().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].strip("-") or "item"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- files

def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def dumps(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_text_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp-%s" % (path, uuid.uuid4().hex[:6])
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(20):  # Windows: target may be briefly open elsewhere
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_json(path, obj):
    write_text_atomic(path, dumps(obj))


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)


def read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn final line from a crash is skipped, not fatal
    except FileNotFoundError:
        pass
    return out


@contextlib.contextmanager
def file_lock(path, timeout=10.0, stale_after=30.0):
    """Cross-process exclusive lock via O_EXCL; stale locks (crashed holder) are broken."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.remove(path)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise AiwosError("state is locked by another process (%s)" % path)
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


# ---------------------------------------------------------------- path patterns

_WILD = re.compile(r"[*?\[]")


def norm_pattern(p):
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    if p.endswith("/"):
        p += "**"
    return p


def literal_prefix(pattern):
    """Directory-aligned literal prefix of a glob ('src/pay/**/*.py' -> 'src/pay')."""
    pattern = norm_pattern(pattern)
    m = _WILD.search(pattern)
    if not m:
        return pattern
    head = pattern[: m.start()]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def path_matches(path, pattern):
    path = norm_pattern(path)
    pattern = norm_pattern(pattern)
    if pattern.startswith("work:") or path.startswith("work:"):
        return path == pattern
    if not _WILD.search(pattern):
        return path == pattern or path.startswith(pattern.rstrip("/") + "/")
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    # fnmatch's '*' already crosses '/', so '**' behaves as recursive.
    return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.replace("**/", ""))


def patterns_may_overlap(a, b):
    """Conservative overlap test: never misses a real overlap, may over-report."""
    a, b = norm_pattern(a), norm_pattern(b)
    if a.startswith("work:") or b.startswith("work:"):
        return a == b
    if a == b:
        return True
    if not _WILD.search(a) and not _WILD.search(b):
        return path_matches(a, b) or path_matches(b, a)
    if not _WILD.search(a):
        return path_matches(a, b) or _prefix_related(a, literal_prefix(b))
    if not _WILD.search(b):
        return path_matches(b, a) or _prefix_related(b, literal_prefix(a))
    return _prefix_related(literal_prefix(a), literal_prefix(b))


def _prefix_related(p, q):
    if p == "" or q == "":
        return True
    p, q = p.rstrip("/"), q.rstrip("/")
    return p == q or p.startswith(q + "/") or q.startswith(p + "/")
