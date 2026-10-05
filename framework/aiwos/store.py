"""Durable state access. Layout and ownership rules are documented in docs/architecture.md.

Project files (committed):   <root>/.ai/config.json, .ai/runtime/, .ai/bin/
Coordination state (shared): <main-worktree>/.ai/state/   (git-ignored; synced via `aiwos sync`)

Every mutable state file has a single writer (its session/actor) except goal and
work-package definitions, which carry `updated_at` and merge newest-wins.
"""

import copy
import os
import re
import subprocess

from .util import (AiwosError, age_seconds, append_jsonl, file_lock, iso, read_json,
                   read_jsonl, slug, write_json)

DEFAULT_CONFIG = {
    "version": 1,
    "lease_ttl_seconds": 1800,
    "enforcement": {
        # deny | warn: what to do when a session with claims edits outside them
        "out_of_scope_writes": "deny",
        "protected": [".ai/state/**", ".ai/runtime/**", ".ai/bin/**"],
    },
    "git": {
        "branch_prefix": "ai/",
        "use_worktrees": True,
        "worktree_dir": ".ai/worktrees",
        "base_branch": None,
        "require_merge": True,
    },
    "sync": {"remote": None, "branch": "aiwos-state"},
    "validation": {
        "review_required_for_risk": ["medium", "high"],
        "timeout_seconds": 1800,
        "output_tail_chars": 3000,
    },
    "context": {"inbox_min_priority": "IMPORTANT"},
    "routing": {
        "tiers": {
            "tool": {"use": "deterministic tool / script, no model"},
            "light": {"model": "haiku", "effort": "low"},
            "standard": {"model": "sonnet", "effort": "medium"},
            "deep": {"model": "opus", "effort": "high"},
            "review": {"model": "opus", "effort": "high"},
        },
        "rules": [
            {"if": {"kind": "review"}, "tier": "review"},
            {"if": {"risk": "high"}, "tier": "deep"},
            {"if": {"complexity": "high"}, "tier": "deep"},
            {"if": {"complexity": "low", "risk": "low"}, "tier": "light"},
        ],
        "default_tier": "standard",
    },
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_project_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, ".ai", "config.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def git_main_root(start):
    """Main worktree root without spawning git (hooks run on every edit)."""
    d = os.path.abspath(start)
    while True:
        dotgit = os.path.join(d, ".git")
        if os.path.isdir(dotgit):
            return d
        if os.path.isfile(dotgit):
            try:
                with open(dotgit, encoding="utf-8") as f:
                    m = re.match(r"gitdir:\s*(.+)", f.read().strip())
                gitdir = m.group(1).strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(d, gitdir))
                common = gitdir
                cfile = os.path.join(gitdir, "commondir")
                if os.path.isfile(cfile):
                    with open(cfile, encoding="utf-8") as f:
                        common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
                if os.path.basename(common) == ".git":
                    return os.path.dirname(common)
            except (OSError, AttributeError):
                pass
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


class Store:
    def __init__(self, start=None, require=True):
        start = start or os.getcwd()
        self.root = find_project_root(start)
        if not self.root:
            if require:
                raise AiwosError("not inside an AI Work OS project (no .ai/config.json). Run `aiwos init`.")
            self.root = os.path.abspath(start)
        self.main_root = git_main_root(self.root) or self.root
        self.is_git = git_main_root(self.root) is not None
        cfg_root = self.root if os.path.isfile(os.path.join(self.root, ".ai", "config.json")) else self.main_root
        self.config = deep_merge(DEFAULT_CONFIG, read_json(os.path.join(cfg_root, ".ai", "config.json"), {}))
        override = os.environ.get("AIWOS_STATE_DIR")
        self.state = os.path.abspath(override) if override else os.path.join(self.main_root, ".ai", "state")
        self.local = read_json(os.path.join(self.main_root, ".ai", "local.json"), {}) or {}

    # ------------------------------------------------------------ paths
    def p(self, *parts):
        return os.path.join(self.state, *parts)

    def lock(self):
        return file_lock(self.p(".lock"))

    def rel(self, path):
        """Repo-relative POSIX path, or None if outside the project/worktree."""
        ap = os.path.abspath(path if os.path.isabs(path) else os.path.join(self.root, path))
        for base in (self.root, self.main_root):
            try:
                r = os.path.relpath(ap, base)
            except ValueError:  # different drive on Windows
                continue
            if not r.startswith(".."):
                return r.replace("\\", "/")
        return None

    # ------------------------------------------------------------ identity
    def actor(self):
        a = os.environ.get("AIWOS_ACTOR") or self.local.get("actor")
        if a:
            return a
        try:
            a = subprocess.run(["git", "config", "user.name"], cwd=self.root, capture_output=True,
                               text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            a = ""
        return a or os.environ.get("USERNAME") or os.environ.get("USER") or "human"

    def current_session_id(self):
        sid = os.environ.get("AIWOS_SESSION")
        if sid:
            return sid
        # Fallback inside Claude Code (CLAUDECODE=1) when the shell did not get AIWOS_SESSION:
        # the single live AI session registered for this exact root (ambiguous -> None, never a guess).
        if not os.environ.get("CLAUDECODE"):
            return None
        live = [s for s in self.list_sessions() if s.get("actor_type") == "AI" and s.get("cwd") == self.root
                and self.session_alive(s)]
        return live[0]["id"] if len(live) == 1 else None

    # ------------------------------------------------------------ goals
    def list_goal_ids(self):
        d = self.p("goals")
        if not os.path.isdir(d):
            return []
        return sorted(x for x in os.listdir(d) if re.match(r"^GOAL-\d+$", x))

    def load_goal(self, gid):
        g = read_json(self.p("goals", gid, "goal.json"))
        if g is None:
            raise AiwosError("unknown goal %s" % gid)
        return g

    def save_goal(self, goal):
        goal["updated_at"] = iso()
        write_json(self.p("goals", goal["id"], "goal.json"), goal)

    def next_goal_id(self):
        nums = [int(g.split("-")[1]) for g in self.list_goal_ids()]
        return "GOAL-%03d" % (max(nums, default=0) + 1)

    # ------------------------------------------------------------ work packages
    def list_work(self, gid=None):
        out = []
        for g in ([gid] if gid else self.list_goal_ids()):
            d = self.p("goals", g, "work")
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if re.match(r"^WP-\d+\.json$", name):
                    wp = read_json(os.path.join(d, name))
                    if wp:
                        out.append(wp)
        return out

    def find_work(self, wid):
        for g in self.list_goal_ids():
            wp = read_json(self.p("goals", g, "work", wid + ".json"))
            if wp:
                return wp
        raise AiwosError("unknown work package %s" % wid)

    def save_work(self, wp):
        wp["updated_at"] = iso()
        write_json(self.p("goals", wp["goal"], "work", wp["id"] + ".json"), wp)

    def next_work_id(self, taken=()):
        nums = [int(w["id"].split("-")[1]) for w in self.list_work()]
        nums += [int(t.split("-")[1]) for t in taken]
        return "WP-%03d" % (max(nums, default=0) + 1)

    # ------------------------------------------------------------ sessions
    def list_sessions(self):
        d = self.p("sessions")
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                s = read_json(os.path.join(d, name))
                if s:
                    out.append(s)
        return out

    def load_session(self, sid):
        return read_json(self.p("sessions", sid + ".json"))

    def save_session(self, s):
        write_json(self.p("sessions", s["id"] + ".json"), s)

    def session_alive(self, s):
        if not s or s.get("status") not in ("active", "paused"):
            return False
        return age_seconds(s.get("last_seen")) < float(self.config["lease_ttl_seconds"])

    # ------------------------------------------------------------ claims
    def load_claims(self, sid):
        return (read_json(self.p("claims", sid + ".json"), {}) or {}).get("claims", [])

    def save_claims(self, sid, claims):
        write_json(self.p("claims", sid + ".json"), {"session": sid, "updated_at": iso(), "claims": claims})

    def all_claims(self):
        d = self.p("claims")
        out = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".json"):
                    doc = read_json(os.path.join(d, name), {}) or {}
                    for c in doc.get("claims", []):
                        c = dict(c)
                        c.setdefault("session", doc.get("session"))
                        out.append(c)
        return out

    # ------------------------------------------------------------ events
    def append_event(self, ev, writer):
        append_jsonl(self.p("events", slug(writer, 60) + ".jsonl"), ev)

    def events(self):
        d = self.p("events")
        seen, out = set(), []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".jsonl"):
                    for ev in read_jsonl(os.path.join(d, name)):
                        if ev.get("id") in seen:
                            continue  # duplicate delivery (e.g. after a sync) is idempotent
                        seen.add(ev.get("id"))
                        out.append(ev)
        out.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")))
        return out

    # ------------------------------------------------------------ validation records
    def save_validation(self, v):
        write_json(self.p("validation", v["id"] + ".json"), v)

    def load_validation(self, vid):
        return read_json(self.p("validation", vid + ".json"))

    # ------------------------------------------------------------ local (unsynced) state
    def cursor(self, sid):
        return read_json(self.p(".local", "cursors", sid + ".json"), {}) or {}

    def save_cursor(self, sid, cur):
        write_json(self.p(".local", "cursors", sid + ".json"), cur)
