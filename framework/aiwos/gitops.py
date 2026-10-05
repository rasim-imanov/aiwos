"""Thin Git integration: branches, worktrees, change sets, merge checks."""

import os
import subprocess

from .util import AiwosError


def git(args, cwd, check=True, input_text=None, env=None):
    try:
        r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                           input=input_text, env=env, timeout=120, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise AiwosError("git is not installed")
    if check and r.returncode != 0:
        raise AiwosError("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def out(args, cwd, check=True):
    return git(args, cwd, check=check).stdout.strip()


def has_commits(cwd):
    return git(["rev-parse", "--verify", "HEAD"], cwd, check=False).returncode == 0


def current_branch(cwd):
    return out(["rev-parse", "--abbrev-ref", "HEAD"], cwd, check=False) or None


def base_branch(store):
    b = store.config["git"].get("base_branch")
    if b:
        return b
    return current_branch(store.main_root) or "main"


def branch_exists(cwd, name):
    return git(["rev-parse", "--verify", "--quiet", "refs/heads/" + name], cwd, check=False).returncode == 0


def start_work_branch(store, wid, title, use_worktree):
    """Create (or reuse, when recovering) a branch and optional worktree for a work package."""
    from .util import slug
    if not has_commits(store.main_root):
        raise AiwosError("repository has no commits yet; commit once before starting branch-based work")
    base = base_branch(store)
    branch = "%s%s-%s" % (store.config["git"]["branch_prefix"], wid.lower(), slug(title, 30))
    base_sha = out(["rev-parse", base], store.main_root)
    reused = branch_exists(store.main_root, branch)
    if not reused:
        git(["branch", branch, base_sha], store.main_root)
    worktree = None
    if use_worktree:
        rel = "%s/%s" % (store.config["git"]["worktree_dir"].rstrip("/"), wid)
        worktree = os.path.join(store.main_root, *rel.split("/"))
        if not os.path.isdir(worktree):
            git(["worktree", "add", worktree, branch], store.main_root)
        worktree = rel
    return {"branch": branch, "base": base, "base_sha": base_sha, "worktree": worktree, "reused": reused}


def changed_files(store, st):
    """Files changed on the work branch relative to its base, plus uncommitted edits in its worktree."""
    files = set()
    branch, base = st.get("branch"), st.get("base")
    if branch and base:
        mb = git(["merge-base", base, branch], store.main_root, check=False).stdout.strip()
        if mb:
            files.update(x for x in out(["diff", "--name-only", mb, branch], store.main_root).splitlines() if x)
    wt = st.get("worktree")
    wt_abs = os.path.join(store.main_root, *wt.split("/")) if wt else None
    if wt_abs and os.path.isdir(wt_abs):
        for line in out(["status", "--porcelain", "-uall"], wt_abs).splitlines():
            if len(line) > 3:
                path = line[3:].split(" -> ")[-1].strip('"')
                files.add(path)
    return sorted(files)


def commits(store, st):
    branch, base = st.get("branch"), st.get("base")
    if not (branch and base):
        return []
    r = git(["log", "--format=%h %s", "%s..%s" % (base, branch)], store.main_root, check=False)
    return [l for l in r.stdout.splitlines() if l][:50]


def is_merged(store, branch, base):
    if not branch_exists(store.main_root, branch):
        return False
    return git(["merge-base", "--is-ancestor", branch, base], store.main_root, check=False).returncode == 0


def remove_worktree(store, rel):
    path = os.path.join(store.main_root, *rel.split("/"))
    if not os.path.isdir(path):
        return False
    if out(["status", "--porcelain"], path):
        return False  # never discard uncommitted work
    git(["worktree", "remove", path], store.main_root, check=False)
    return True
