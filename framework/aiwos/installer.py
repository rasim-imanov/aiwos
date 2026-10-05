"""Install / upgrade / uninstall the framework into a project. Non-destructive by design:

* existing files are backed up to .ai/backups/<timestamp>/ before any change;
* settings.json is merged (only hooks whose command mentions `aiwos` are owned by us);
* CLAUDE.md gets a marked block (<!-- aiwos:begin --> … <!-- aiwos:end -->), never a rewrite;
* framework-owned files that the user modified are not overwritten on upgrade (reported instead).
"""

import os
import shutil
import subprocess
import sys

from . import __version__
from .util import AiwosError, dumps, iso, read_json, read_text, sha256_bytes, write_text_atomic

FRAMEWORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEGIN, END = "<!-- aiwos:begin -->", "<!-- aiwos:end -->"
HOOK_CMD = 'sh "$CLAUDE_PROJECT_DIR/.ai/bin/aiwos" hook %s'
WRITE_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
HOOKS = [
    ("SessionStart", None, "session-start", 20),
    ("UserPromptSubmit", None, "prompt", 10),
    ("PreToolUse", WRITE_TOOLS, "pre-write", 10),
    ("PostToolUse", WRITE_TOOLS, "post-write", 10),
    ("SessionEnd", None, "session-end", 10),
]
PERMISSIONS = ["Bash(.ai/bin/aiwos *)", "Bash(sh .ai/bin/aiwos *)"]
RUNTIME_PARTS = ["aiwos", "aiwos_main.py", "bin", "claude", "templates"]


def sha256_file(path):
    """Line-ending-insensitive hash: a CRLF checkout (core.autocrlf) is not a local modification."""
    with open(path, "rb") as f:
        return sha256_bytes(f.read().replace(b"\r\n", b"\n"))


def _iter_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, f)
            yield full, os.path.relpath(full, base).replace("\\", "/")


def _detect_python():
    for c in ("python3", "python", "py"):
        try:
            r = subprocess.run([c, "-c", "import sys; print(sys.version_info >= (3, 9))"], capture_output=True,
                               text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip() == "True":
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return None


class Installer:
    def __init__(self, target, dry_run=False):
        self.target = os.path.abspath(target)
        self.dry = dry_run
        self.stamp = iso().replace(":", "").replace(".", "")[:17]
        self.log = []
        self.manifest_path = os.path.join(self.target, ".ai", "manifest.json")
        self.manifest = read_json(self.manifest_path, {}) or {}
        self.files = dict(self.manifest.get("files", {}))

    # ---------------------------------------------------------------- helpers
    def _abs(self, rel):
        return os.path.join(self.target, *rel.split("/"))

    def _backup(self, rel):
        src = self._abs(rel)
        if not os.path.exists(src) or self.dry:
            return
        dst = os.path.join(self.target, ".ai", "backups", self.stamp, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    def _write(self, rel, text, owned=True, force=False):
        dst = self._abs(rel)
        if os.path.exists(dst):
            cur = sha256_file(dst)
            recorded = self.files.get(rel)
            if recorded is None and owned and not force:
                self.log.append("SKIP %s (exists and is not framework-owned)" % rel)
                return False
            if recorded and cur != recorded and not force:
                self.log.append("KEEP %s (locally modified; use --force to overwrite)" % rel)
                return False
            with open(dst, encoding="utf-8", errors="replace") as f:
                if f.read() == text:  # text mode already normalizes CRLF
                    return False
        self.log.append(("WRITE " if not os.path.exists(dst) else "UPDATE ") + rel)
        if not self.dry:
            write_text_atomic(dst, text)
            if owned:
                self.files[rel] = sha256_file(dst)
        return True

    # ---------------------------------------------------------------- steps
    def install(self, force=False):
        if not os.path.isdir(self.target):
            raise AiwosError("target %s does not exist" % self.target)
        self._runtime()
        self._config()
        self._claude_assets(force)
        self._settings()
        self._claude_md()
        if not self.dry:
            write_text_atomic(self.manifest_path, dumps({"version": __version__, "installed_at": iso(),
                                                         "source": FRAMEWORK_DIR, "files": self.files}))
        return self.log

    def _runtime(self):
        rt = os.path.join(self.target, ".ai", "runtime")
        if os.path.abspath(rt) == os.path.abspath(FRAMEWORK_DIR):
            self.log.append("runtime already in place (%s)" % __version__)
        else:
            self.log.append("SYNC .ai/runtime (v%s)" % __version__)
            if not self.dry:
                if os.path.isdir(rt):
                    shutil.rmtree(rt)
                for part in RUNTIME_PARTS:
                    src = os.path.join(FRAMEWORK_DIR, part)
                    if os.path.isdir(src):
                        shutil.copytree(src, os.path.join(rt, part), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    elif os.path.isfile(src):
                        os.makedirs(rt, exist_ok=True)
                        shutil.copy2(src, os.path.join(rt, part))
        for name in ("aiwos", "aiwos.cmd"):
            with open(os.path.join(FRAMEWORK_DIR, "bin", name), encoding="utf-8") as f:
                self._write(".ai/bin/" + name, f.read(), force=True)
        self._write(".ai/.gitattributes", "# keep LF everywhere: the sh launcher breaks with CRLF (core.autocrlf)\n"
                    "* text=auto eol=lf\n*.cmd text eol=crlf\n", force=True)
        self._write(".ai/.gitignore", "# AI Work OS: machine-local and shared-via-sync state is not committed\n"
                    "state/\nworktrees/\nbackups/\nlocal.json\n__pycache__/\n*.pyc\n", force=True)

    def _config(self):
        cfg = self._abs(".ai/config.json")
        if not os.path.exists(cfg):
            self.log.append("WRITE .ai/config.json")
            if not self.dry:
                write_text_atomic(cfg, dumps({"version": 1, "lease_ttl_seconds": 1800,
                                              "enforcement": {"out_of_scope_writes": "deny"},
                                              "sync": {"remote": None},
                                              "validation": {"review_required_for_risk": ["medium", "high"]}}))
        local = self._abs(".ai/local.json")
        if not os.path.exists(local) and not self.dry:
            write_text_atomic(local, dumps({"python": _detect_python() or "python3"}))

    def _claude_assets(self, force):
        base = os.path.join(FRAMEWORK_DIR, "claude")
        for full, rel in _iter_files(base):
            with open(full, encoding="utf-8") as f:
                self._write(".claude/" + rel, f.read(), force=force)

    def _settings(self):
        rel = ".claude/settings.json"
        path = self._abs(rel)
        doc = read_json(path, {}) if os.path.exists(path) else {}
        if doc is None:
            raise AiwosError("%s is not valid JSON; fix it before installing" % rel)
        before = dumps(doc)
        hooks = doc.setdefault("hooks", {})
        for event, matcher, name, timeout in HOOKS:
            groups = hooks.setdefault(event, [])
            cmd = HOOK_CMD % name
            present = any(cmd == h.get("command") for g in groups for h in g.get("hooks", []))
            # drop stale aiwos hooks for this event (renamed commands from older versions)
            for g in groups:
                g["hooks"] = [h for h in g.get("hooks", []) if "aiwos" not in str(h.get("command")) or h.get("command") == cmd]
            hooks[event] = groups = [g for g in groups if g.get("hooks")]
            if not present:
                g = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
                if matcher:
                    g = {"matcher": matcher, "hooks": g["hooks"]}
                groups.append(g)
        perms = doc.setdefault("permissions", {}).setdefault("allow", [])
        for p in PERMISSIONS:
            if p not in perms:
                perms.append(p)
        if dumps(doc) != before:
            self._backup(rel)
            self.log.append("MERGE %s (hooks + aiwos permissions)" % rel)
            if not self.dry:
                write_text_atomic(path, dumps(doc))

    def _claude_md(self):
        with open(os.path.join(FRAMEWORK_DIR, "templates", "CLAUDE.block.md"), encoding="utf-8") as f:
            block = "%s\n%s\n%s\n" % (BEGIN, f.read().strip(), END)
        path = self._abs("CLAUDE.md")
        text = read_text(path) if os.path.exists(path) else ""
        if BEGIN in text and END in text:
            new = text[:text.index(BEGIN)] + block.rstrip("\n") + text[text.index(END) + len(END):]
        else:
            new = (text.rstrip() + "\n\n" if text.strip() else "") + block
        if new != text:
            self._backup("CLAUDE.md")
            self.log.append(("UPDATE " if text else "WRITE ") + "CLAUDE.md (aiwos block)")
            if not self.dry:
                write_text_atomic(path, new)

    # ---------------------------------------------------------------- uninstall
    def uninstall(self):
        for rel, digest in sorted(self.files.items()):
            p = self._abs(rel)
            if os.path.exists(p) and sha256_file(p) == digest:
                self.log.append("REMOVE " + rel)
                if not self.dry:
                    os.remove(p)
                    d = os.path.dirname(p)
                    while d != self.target and os.path.isdir(d) and not os.listdir(d):
                        os.rmdir(d)
                        d = os.path.dirname(d)
            elif os.path.exists(p):
                self.log.append("KEEP %s (modified)" % rel)
        sp = self._abs(".claude/settings.json")
        doc = read_json(sp)
        if doc:
            self._backup(".claude/settings.json")
            for event in list((doc.get("hooks") or {}).keys()):
                groups = []
                for g in doc["hooks"][event]:
                    g["hooks"] = [h for h in g.get("hooks", []) if "aiwos" not in str(h.get("command"))]
                    if g["hooks"]:
                        groups.append(g)
                if groups:
                    doc["hooks"][event] = groups
                else:
                    del doc["hooks"][event]
            allow = (doc.get("permissions") or {}).get("allow")
            if allow:
                doc["permissions"]["allow"] = [p for p in allow if p not in PERMISSIONS]
            self.log.append("CLEAN .claude/settings.json")
            if not self.dry:
                write_text_atomic(sp, dumps(doc))
        cm = self._abs("CLAUDE.md")
        if os.path.exists(cm):
            text = read_text(cm)
            if BEGIN in text and END in text:
                self._backup("CLAUDE.md")
                new = (text[:text.index(BEGIN)].rstrip() + "\n" + text[text.index(END) + len(END):].lstrip("\n")).strip()
                self.log.append("CLEAN CLAUDE.md")
                if not self.dry:
                    if new:
                        write_text_atomic(cm, new + "\n")
                    else:
                        os.remove(cm)
        if not self.dry:
            rt = self._abs(".ai/runtime")
            if os.path.isdir(rt) and os.path.abspath(rt) != os.path.abspath(FRAMEWORK_DIR):
                shutil.rmtree(rt)
            for f in (self.manifest_path,):
                if os.path.exists(f):
                    os.remove(f)
        self.log.append("kept .ai/config.json, .ai/state/, knowledge/, workspace/ (your data)")
        return self.log


def install_user_skill(dry_run=False):
    """Optional: a personal /aiwos-init skill so any project can be bootstrapped from Claude Code."""
    home = os.path.expanduser("~")
    dst = os.path.join(home, ".claude", "skills", "aiwos-init", "SKILL.md")
    src = os.path.join(FRAMEWORK_DIR, "templates", "user-skill-aiwos-init.md")
    text = read_text(src).replace("{{FRAMEWORK_DIR}}", FRAMEWORK_DIR.replace("\\", "/")) \
        .replace("{{PYTHON}}", sys.executable.replace("\\", "/"))
    if not dry_run:
        write_text_atomic(dst, text)
    return dst
