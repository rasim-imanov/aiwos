"""Share coordination state between machines through Git, without touching code branches.

State is committed with plumbing to refs/aiwos/state and pushed to the remote branch
`aiwos-state` (configurable). Merging is semantic, not textual, so it never produces
merge conflicts:
  *.jsonl  union of events by id            (append-only, one writer per file)
  *.json   newest `updated_at`/`last_seen`  (single writer, or newest-wins definitions)
  goal id collision (two machines created GOAL-007) -> both kept, CONFLICT reported
Concurrent pushes are serialized by Git itself: a rejected push re-fetches and retries.
"""

import json
import os
import subprocess

from .gitops import git
from .util import AiwosError, dumps, read_json, read_text, write_text_atomic

LOCAL_REF = "refs/aiwos/state"
REMOTE_TRACK = "refs/aiwos/remote-state"


def _state_files(state_dir):
    out = []
    for dirpath, dirnames, filenames in os.walk(state_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.startswith(".") or ".tmp-" in f:
                continue
            full = os.path.join(dirpath, f)
            out.append(os.path.relpath(full, state_dir).replace("\\", "/"))
    return sorted(out)


def _hash_local(store, rels):
    if not rels:
        return {}
    paths = "\n".join(os.path.join(store.state, *r.split("/")) for r in rels) + "\n"
    r = git(["hash-object", "-w", "--stdin-paths"], store.main_root, input_text=paths)
    return dict(zip(rels, r.stdout.split()))


def _rev(store, ref):
    r = git(["rev-parse", "--verify", "--quiet", ref], store.main_root, check=False)
    return r.stdout.strip() or None


def _blob(store, sha):
    r = subprocess.run(["git", "cat-file", "blob", sha], cwd=store.main_root, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise AiwosError("git cat-file failed for %s" % sha)
    return r.stdout.decode("utf-8", errors="replace")


def _remote_tree(store, sha):
    r = git(["ls-tree", "-r", "-z", sha], store.main_root)
    out = {}
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def _stamp(doc):
    if not isinstance(doc, dict):
        return ""
    return doc.get("updated_at") or doc.get("last_seen") or doc.get("ts") or ""


def merge_file(rel, local_text, remote_text, conflicts):
    """Return merged text for one state file."""
    if local_text is None:
        return remote_text
    if remote_text is None or local_text == remote_text:
        return local_text
    if rel.endswith(".jsonl"):
        seen, lines = set(), []
        for text in (local_text, remote_text):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    key = json.loads(line).get("id") or line
                    ts = json.loads(line).get("ts", "")
                except ValueError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                lines.append((ts, key, line))
        lines.sort()
        return "".join(l + "\n" for _, _, l in lines)
    if rel.endswith(".json"):
        try:
            a, b = json.loads(local_text), json.loads(remote_text)
        except ValueError:
            return local_text
        if rel.endswith("goal.json") and a.get("uid") and b.get("uid") and a["uid"] != b["uid"]:
            conflicts.append({"kind": "goal-id-collision", "path": rel, "local": a.get("title"), "remote": b.get("title"),
                              "remote_uid": b["uid"]})
            return None  # caller keeps local and parks the remote copy
        return remote_text if _stamp(b) > _stamp(a) else local_text
    return local_text  # markdown etc.: unique names per writer; keep local on divergence


def _commit_state(store, parents, message):
    files = _state_files(store.state)
    hashes = _hash_local(store, files)
    idx = os.path.join(store.state, ".local", "sync-index")
    os.makedirs(os.path.dirname(idx), exist_ok=True)
    if os.path.exists(idx):
        os.remove(idx)
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    # NUL-terminated records: Windows text-mode pipes turn "\n" into "\r\n", which corrupts paths.
    info = "".join("100644 %s\t%s\0" % (hashes[f], f) for f in files)
    git(["update-index", "-z", "--add", "--index-info"], store.main_root, input_text=info, env=env)
    tree = git(["write-tree"], store.main_root, env=env).stdout.strip()
    if len(parents) == 1:
        ptree = git(["rev-parse", parents[0] + "^{tree}"], store.main_root).stdout.strip()
        if ptree == tree:
            return parents[0], False
    args = ["commit-tree", tree, "-m", message]
    for p in parents:
        args[2:2] = ["-p", p]
    env_c = dict(os.environ)
    env_c.setdefault("GIT_AUTHOR_NAME", store.actor())
    env_c.setdefault("GIT_AUTHOR_EMAIL", "aiwos@localhost")
    env_c.setdefault("GIT_COMMITTER_NAME", store.actor())
    env_c.setdefault("GIT_COMMITTER_EMAIL", "aiwos@localhost")
    sha = git(args, store.main_root, env=env_c).stdout.strip()
    return sha, True


def sync(store, push=True, remote=None, attempts=4):
    if not store.is_git:
        raise AiwosError("sync needs a Git repository")
    remote = remote or store.config["sync"].get("remote")
    branch = store.config["sync"]["branch"]
    report = {"remote": remote, "fetched": False, "merged_files": [], "committed": False, "pushed": False,
              "conflicts": []}
    for attempt in range(attempts):
        with store.lock():
            remote_sha = None
            if remote:
                r = git(["fetch", "--quiet", remote, "+refs/heads/%s:%s" % (branch, REMOTE_TRACK)], store.main_root, check=False)
                if r.returncode == 0:
                    remote_sha = _rev(store, REMOTE_TRACK)
                    report["fetched"] = True
                elif "couldn't find remote ref" not in (r.stderr or "") and "not found" not in (r.stderr or "").lower():
                    raise AiwosError("fetch from %s failed: %s" % (remote, r.stderr.strip()))
            local_sha = _rev(store, LOCAL_REF)
            if remote_sha and remote_sha != local_sha:
                rtree = _remote_tree(store, remote_sha)
                lhash = _hash_local(store, [p for p in rtree if os.path.exists(os.path.join(store.state, *p.split("/")))])
                for rel, blob in rtree.items():
                    if lhash.get(rel) == blob:
                        continue
                    lp = os.path.join(store.state, *rel.split("/"))
                    ltext = read_text(lp) if os.path.exists(lp) else None
                    rtext = _blob(store, blob)
                    merged = merge_file(rel, ltext, rtext, report["conflicts"])
                    if merged is None:
                        park = lp[:-5] + ".conflict-%s.json" % report["conflicts"][-1]["remote_uid"]
                        write_text_atomic(park, rtext)
                        continue
                    if merged != ltext:
                        write_text_atomic(lp, merged)
                        report["merged_files"].append(rel)
            parents = [p for p in (local_sha, remote_sha) if p]
            parents = list(dict.fromkeys(parents))
            if local_sha and remote_sha and local_sha != remote_sha:
                if git(["merge-base", "--is-ancestor", remote_sha, local_sha], store.main_root, check=False).returncode == 0:
                    parents = [local_sha]
                elif git(["merge-base", "--is-ancestor", local_sha, remote_sha], store.main_root, check=False).returncode == 0:
                    parents = [remote_sha]
            new, made = _commit_state(store, parents, "aiwos state sync by %s" % store.actor())
            git(["update-ref", LOCAL_REF, new], store.main_root)
            report["committed"] = report["committed"] or made
        if not (push and remote):
            return report
        if remote_sha == new:
            return report
        r = git(["push", "--quiet", remote, "%s:refs/heads/%s" % (new, branch)], store.main_root, check=False)
        if r.returncode == 0:
            report["pushed"] = True
            return report
        if "rejected" not in r.stderr and "fetch first" not in r.stderr and "non-fast-forward" not in r.stderr:
            raise AiwosError("push failed: %s" % r.stderr.strip())
    raise AiwosError("sync gave up after %d concurrent-update retries" % attempts)


def snapshot_path(store):
    return read_json(os.path.join(store.state, ".local", "last-sync.json"))


def record_sync(store, report):
    write_text_atomic(os.path.join(store.state, ".local", "last-sync.json"), dumps(report))
