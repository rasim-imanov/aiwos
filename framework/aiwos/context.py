"""Context projection, traceability, status views, audits and metrics.

Everything here returns references (paths, ids) rather than copied content: the
receiving agent decides what to open (lazy context loading).
"""

import os
import re
from collections import Counter

from .acp import priority_rank
from .model import ACTIVE, View
from .ops import subscriptions


def _decision_index(store):
    """Decision records live in knowledge/decisions/D-NNN-*.md with a `Status:` line."""
    d = os.path.join(store.root, "knowledge", "decisions")
    out = {}
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            m = re.match(r"^(D-\d+)-.*\.md$", name)
            if not m:
                continue
            path = os.path.join(d, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(1500)
            status = re.search(r"(?im)^\**status\**:\s*\**\s*([A-Z_]+)", head)
            goals = re.findall(r"GOAL-\d+", head)
            works = re.findall(r"WP-\d+", head)
            title = re.search(r"(?m)^#\s+(.+)$", head)
            out[m.group(1)] = {"id": m.group(1), "path": "knowledge/decisions/" + name,
                               "status": status.group(1) if status else "UNKNOWN",
                               "title": title.group(1).strip() if title else name,
                               "goals": sorted(set(goals)), "work": sorted(set(works))}
    return out


def contracts(store, v=None):
    """Contracts are registered by CONTRACT_CREATED/CHANGED events: {name: {path, hash, ...}}."""
    v = v or View(store)
    out = {}
    for ev in v.events:
        if ev.get("type") in ("CONTRACT_CREATED", "CONTRACT_CHANGED"):
            d = ev.get("data") or {}
            c = out.setdefault(d.get("name"), {})
            c.update({k: d[k] for k in ("name", "path", "hash", "goal", "consumers", "provider") if k in d})
            c["version"] = c.get("version", 0) + 1
            c["updated"] = ev["ts"]
    return out


def projection(store, wid, session=None):
    """Minimal context a session needs to execute one work package."""
    v = View(store)
    wp = v.work[wid]
    goal = v.goals[wp["goal"]]
    st = v.state[wid]
    decisions = _decision_index(store)
    rel_dec = [d for d in decisions.values()
               if (wid in d["work"] or wp["goal"] in d["goals"] or d["id"] in goal.get("decisions", []))
               and d["status"] not in ("SUPERSEDED", "REJECTED")]
    cons = contracts(store, v)
    wp_contracts = [dict(cons[c], name=c) for c in wp.get("contracts", []) if c in cons]
    missing_contracts = [c for c in wp.get("contracts", []) if c not in cons]
    deps = []
    for d in wp.get("depends_on", []):
        ds = v.state.get(d, {})
        deps.append({"work_id": d, "title": v.work[d]["title"], "status": v.status(d),
                     "outputs": v.work[d].get("outputs", []), "handoff": ds.get("handoff")})
    neighbours = sorted({c["resource"] for c in v.claims if c["alive"] and c["session"] != session
                         and c["mode"] != "READ" and not c["resource"].startswith("work:")})
    unread = []
    if session:
        subs = subscriptions(v, session)
        cur = store.cursor(session).get("after", "")
        for ev in v.events:
            if cur and (ev.get("ts", ""), ev.get("id", "")) <= tuple(cur.split("|", 1)):
                continue
            if set(ev.get("scope", [])) & subs and priority_rank(ev.get("priority")) >= 2 \
                    and (ev.get("from") or {}).get("session") != session:
                unread.append({"id": ev["id"], "type": ev["type"], "priority": ev.get("priority"),
                               "data": ev.get("data")})
    return {
        "goal": {k: goal.get(k) for k in ("id", "title", "outcome", "success_criteria", "constraints", "non_goals", "status")
                 if goal.get(k)},
        "work": {k: wp.get(k) for k in ("id", "title", "purpose", "outcome", "success_criteria", "inputs", "outputs",
                                        "resources", "reads", "validation", "risk", "complexity", "kind", "expertise", "notes")
                 if wp.get(k)},
        "state": {"status": v.status(wid), "owner": st["owner"], "branch": st["branch"], "worktree": st["worktree"],
                  "validation": st["validation"], "review": st["review"],
                  "handoff": ("state/handoffs/%s.md" % st["handoff"]) if st["handoff"] else None},
        "dependencies": deps,
        "decisions": [{"id": d["id"], "title": d["title"], "status": d["status"], "path": d["path"]} for d in rel_dec],
        "contracts": wp_contracts,
        "missing_contracts": missing_contracts,
        "claimed_by_others": neighbours,
        "unread_important_events": unread[-20:],
        "load_policy": "Open referenced files only when the current step needs them. Stay inside `resources`.",
    }


def trace(store, path):
    """Why does this artifact exist? Which work/goal/session produced it, and was it validated?"""
    v = View(store)
    rel = path.replace("\\", "/")
    hits = []
    for ev in v.events:
        d = ev.get("data") or {}
        touched = d.get("path") == rel or rel in (d.get("changed") or []) or rel in (ev.get("refs") or [])
        if touched:
            hits.append({"event": ev["id"], "type": ev["type"], "ts": ev["ts"], "work_id": d.get("work_id"),
                         "session": (ev.get("from") or {}).get("session"), "kind": d.get("kind")})
    work_ids = sorted({h["work_id"] for h in hits if h["work_id"]})
    from .util import path_matches
    for wid, wp in v.work.items():
        if wid not in work_ids and any(path_matches(rel, r) for r in wp.get("resources", [])):
            work_ids.append(wid)
    return {"path": rel, "events": hits,
            "work": [{"work_id": w, "goal": v.work[w]["goal"], "title": v.work[w]["title"], "status": v.status(w),
                      "validation": v.state[w]["validation"]} for w in work_ids if w in v.work]}


# ------------------------------------------------------------------ views

def status(store, session=None, goal=None):
    v = View(store)
    goals = [g for g in v.goals.values() if not goal or g["id"] == goal]
    lines = []
    for g in goals:
        if g.get("status") in ("COMPLETE", "ABANDONED") and not goal:
            lines.append("%s [%s] %s" % (g["id"], g["status"], g["title"]))
            continue
        lines.append("%s [%s] %s" % (g["id"], g["status"], g["title"]))
        for wid, wp in v.work.items():
            if wp["goal"] != g["id"]:
                continue
            s = v.state[wid]
            flag = []
            if s["owner"]:
                flag.append("owner=" + s["owner"] + (" (STALE)" if v.stale_owner(wid) else ""))
            if v.status(wid) == "WAITING":
                flag.append("waits:" + ",".join(v.blocking_deps(wid)))
            if s["reason"] and v.status(wid) in ("BLOCKED", "NEEDS_INPUT", "FAILED"):
                flag.append("reason: " + str(s["reason"])[:60])
            lines.append("  %-7s %-17s %s %s" % (wid, v.status(wid), wp["title"][:50], " ".join(flag)))
    live = [s for s in v.sessions.values() if s["id"] in v.alive]
    stale = [s for s in v.sessions.values() if s.get("status") in ("active", "paused") and s["id"] not in v.alive]
    lines.append("sessions: %d live%s" % (len(live), (", %d stale (run `aiwos recover`)" % len(stale)) if stale else ""))
    for s in live:
        lines.append("  %s %s %s%s" % (s["id"], s.get("actor"), s.get("work") or "-",
                                         " <- you" if s["id"] == session else ""))
    claims = [c for c in v.claims if c["alive"] and not c["resource"].startswith("work:")]
    if claims:
        lines.append("claims: " + "; ".join("%s %s %s" % (c["session"], c["mode"], c["resource"]) for c in claims[:12]))
    for pair in v.contested:
        lines.append("CONFLICT: %s (%s) vs %s (%s) — later claim must yield" % (
            pair["winner"]["resource"], pair["winner"]["session"], pair["loser"]["resource"], pair["loser"]["session"]))
    if not goals:
        lines.insert(0, "no goals yet — start with /aiwos-goal")
    return "\n".join(lines)


def session_brief(store, sid):
    """Tiny summary injected at session start (keep < ~800 chars)."""
    v = View(store)
    active_goals = [g for g in v.goals.values() if g.get("status") not in ("COMPLETE", "ABANDONED")]
    if not active_goals:
        return ("AI Work OS: session %s. No active goals. For multi-step work, use /aiwos-goal to "
                "turn the request into a confirmed goal first." % sid)
    parts = ["AI Work OS: session %s." % sid]
    for g in active_goals[:3]:
        counts = Counter(v.status(w) for w, wp in v.work.items() if wp["goal"] == g["id"])
        parts.append("%s [%s] %s — %s" % (g["id"], g["status"], g["title"][:60],
                                           ", ".join("%s:%d" % kv for kv in sorted(counts.items())) or "no work yet"))
    mine = v.owned_by(sid)
    if mine:
        parts.append("You own: %s. Run `aiwos context %s` before continuing." % (", ".join(mine), mine[0]))
    stale = [w for w in v.work if v.stale_owner(w)]
    if stale:
        parts.append("Stale (recoverable) work: %s." % ", ".join(stale))
    if v.contested:
        parts.append("Claim conflicts exist — run `aiwos status`.")
    parts.append("Commands: `aiwos status`, `aiwos next`, `aiwos inbox`. Skills: /aiwos-work, /aiwos-validate, /aiwos-handoff.")
    return " ".join(parts)


def doctor(store):
    """Consistency checks over durable state."""
    from .ops import plan_check
    v = View(store)
    problems = []
    errs, warns = plan_check(v.work)
    problems += ["graph: " + e for e in errs]
    problems += ["potential parallel conflict %s on %s" % (w["work"], w["resources"]) for w in warns]
    for wid, s in v.state.items():
        if s["owner"] and s["owner"] not in v.sessions:
            problems.append("%s owned by unknown session %s" % (wid, s["owner"]))
        if v.stale_owner(wid):
            problems.append("%s owner %s lease expired (run `aiwos recover`)" % (wid, s["owner"]))
        if s["worktree"] and v.status(wid) in ACTIVE and not os.path.isdir(os.path.join(store.main_root, *s["worktree"].split("/"))):
            problems.append("%s worktree %s is missing" % (wid, s["worktree"]))
    for c in v.claims:
        if c.get("work") and c["work"] not in v.work and not c["resource"].startswith("work:"):
            problems.append("claim %s references unknown work %s" % (c["id"], c["work"]))
    for name, c in contracts(store, v).items():
        p = os.path.join(store.root, *str(c.get("path", "")).split("/"))
        if not os.path.exists(p):
            problems.append("contract %s points to missing %s" % (name, c.get("path")))
    for d in _decision_index(store).values():
        if d["status"] == "UNKNOWN":
            problems.append("decision %s has no Status line" % d["path"])
    problems += v.warnings[:20]
    for pair in v.contested:
        problems.append("contested claim: %s by %s overlaps %s by %s" % (
            pair["loser"]["resource"], pair["loser"]["session"], pair["winner"]["resource"], pair["winner"]["session"]))
    return problems


SKIP_DIRS = {".git", ".ai", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}


def cleanup_audit(store):
    """Read-only workspace audit. Never deletes; returns findings for a human/agent to act on."""
    findings = []
    root = store.root
    allowed_root = set(store.config.get("cleanup", {}).get("allowed_root_entries", [])) | {
        ".ai", ".claude", ".git", ".gitignore", ".gitattributes", "CLAUDE.md", "README.md", "workspace", "knowledge",
        "LICENSE", ".github", ".mcp.json", "AGENTS.md", "CLAUDE.local.md", "docs"}
    for name in sorted(os.listdir(root)):
        if name not in allowed_root:
            findings.append({"kind": "root-clutter", "path": name,
                             "advice": "move into workspace/<domain>/ or knowledge/, or allow-list it in .ai/config.json cleanup.allowed_root_entries"})
    # "final2", "spec-copy", "notes_old", "plan (1)", "design-v3" — a version/copy suffix on a file name.
    suffix_re = re.compile(r"(?i)(?:[-_ .]?(?:final|copy|new|old|backup|bak|real|v\d+|\(\d+\))\d*)+$")
    # The marker must sit in a comment; prose that merely mentions the word is ignored.
    marker_re = re.compile(r"(?m)(?:^|\s)(?:#|//|/\*|<!--|--|\*)\s*TEMPORARY\b(?P<rest>[^\n]{0,240})")
    groups = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if not dirnames and not filenames and rel_dir != ".":
            findings.append({"kind": "empty-dir", "path": rel_dir})
        for f in filenames:
            rel = (rel_dir + "/" + f) if rel_dir != "." else f
            base, ext = os.path.splitext(f)
            stem = suffix_re.sub("", base)
            if stem and stem != base and not f.startswith("."):
                findings.append({"kind": "suspect-duplicate-name", "path": rel})
            groups.setdefault((rel_dir, stem.lower(), ext.lower()), []).append(f)
            if not f.endswith((".py", ".ts", ".js", ".java", ".go", ".rs", ".tsx", ".jsx", ".css", ".html", ".sh", ".sql",
                               ".kt", ".swift", ".rb", ".php", ".cs", ".vue", ".svelte")):
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    text = fh.read(200000)
            except OSError:
                continue
            for m in marker_re.finditer(text):
                rest = m.group("rest").lower()
                if not ("replace when" in rest or "until" in rest or "remove when" in rest):
                    findings.append({"kind": "unbounded-temporary", "path": rel,
                                     "advice": "TEMPORARY markers need reason + scope + replacement condition"})
                    break
    for (d, stem, ext), names in groups.items():
        if len(names) > 1 and stem:
            findings.append({"kind": "possible-duplicates", "path": "%s/%s*%s" % (d, stem, ext), "files": sorted(names)})
    return findings


def metrics(store):
    v = View(store)
    types = Counter(e.get("type") for e in v.events)
    prevented = sum(1 for e in v.events if e.get("type") == "CONFLICT_DETECTED" and (e.get("data") or {}).get("prevented"))
    hook_denials = sum(1 for e in v.events if e.get("type") == "CONFLICT_DETECTED" and (e.get("data") or {}).get("hook"))
    statuses = Counter(v.status(w) for w in v.work)
    total = len(v.work) or 1
    return {
        "events": len(v.events), "by_type": dict(types.most_common()),
        "work": dict(statuses), "completion_rate": round(statuses.get("COMPLETE", 0) / total, 3),
        "validation_failures": types.get("VALIDATION_FAILED", 0),
        "conflicts_detected": types.get("CONFLICT_DETECTED", 0), "conflicts_prevented": prevented,
        "write_denials_by_hook": hook_denials, "handoffs": types.get("HANDOFF_CREATED", 0),
        "sessions": len(v.sessions), "recoveries": types.get("SESSION_FAILED", 0),
    }
