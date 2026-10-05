"""Coordinator operations. The CLI and the ACP endpoint are both thin layers over these."""

import os
import re

from . import acp, gitops
from .model import ACTIVE, TERMINAL, View, modes_conflict
from .util import (AiwosError, iso, norm_pattern, path_matches, patterns_may_overlap,
                   rand_id, slug, write_text_atomic)

GOAL_FIELDS = {"title", "intent", "outcome", "success_criteria", "scope", "non_goals", "constraints",
               "assumptions", "unknowns", "risks", "research", "decisions", "owners", "validation",
               "status", "notes"}
GOAL_STATUSES = ["DRAFT", "PROPOSED", "CONFIRMED", "ACTIVE", "COMPLETE", "ABANDONED"]
WORK_FIELDS = {"id", "key", "title", "purpose", "outcome", "depends_on", "inputs", "outputs", "resources",
               "reads", "contracts", "success_criteria", "validation", "risk", "complexity", "expertise",
               "kind", "notes", "objective"}
RISKS = ["low", "medium", "high"]


class Ctx:
    """Who is acting. session may be None for a human using the CLI directly."""

    def __init__(self, store, session=None, actor=None, actor_type=None):
        self.store = store
        self.session = session if session is not None else store.current_session_id()
        self.actor = actor or store.actor()
        self.actor_type = actor_type or ("AI" if self.session else "HUMAN")

    @property
    def sender(self):
        s = {"actor": self.actor, "actor_type": self.actor_type}
        if self.session:
            s["session"] = self.session
        return s

    @property
    def writer(self):
        return self.session or "H-" + slug(self.actor, 30)

    def need_session(self):
        if not self.session:
            raise AiwosError("no active session (set AIWOS_SESSION or run `aiwos session start`)")
        return self.session


def emit(ctx, etype, scope=(), data=None, priority="INFO", refs=None):
    ev = acp.make_event(etype, ctx.sender, scope, data, priority, refs)
    ctx.store.append_event(ev, ctx.writer)
    return ev


def send(ctx, ctype, to_session, scope=(), data=None, priority="IMPORTANT", refs=None):
    msg = acp.make_command(ctype, ctx.sender, "session:" + to_session, scope, data, priority, refs)
    errs = acp.validate(msg)
    if errs:
        raise AiwosError("; ".join(errs))
    ctx.store.append_event(msg, ctx.writer)
    return msg


# ====================================================================== goals

def _clean_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def goal_create(ctx, doc):
    unknown = set(doc) - GOAL_FIELDS
    if unknown:
        raise AiwosError("unknown goal fields: %s (allowed: %s)" % (sorted(unknown), sorted(GOAL_FIELDS)))
    if not doc.get("title"):
        raise AiwosError("goal needs a title")
    st = ctx.store
    with st.lock():
        gid = st.next_goal_id()
        goal = {"id": gid, "uid": rand_id("G", 12), "title": doc["title"],
                "intent": doc.get("intent", ""), "outcome": doc.get("outcome", ""),
                "status": doc.get("status", "DRAFT")}
        for k in ("success_criteria", "scope", "non_goals", "constraints", "assumptions", "unknowns",
                  "risks", "research", "decisions", "validation"):
            if k in doc:
                goal[k] = _clean_list(doc[k])
        goal["owners"] = _clean_list(doc.get("owners")) or [ctx.actor]
        if doc.get("notes"):
            goal["notes"] = doc["notes"]
        goal.update(created_by=ctx.actor, created_at=iso(), approvals=[])
        if goal["status"] not in GOAL_STATUSES:
            raise AiwosError("status must be one of %s" % GOAL_STATUSES)
        st.save_goal(goal)
    emit(ctx, "GOAL_CREATED", ["goal:" + gid], {"goal_id": gid, "title": goal["title"]}, "IMPORTANT")
    return goal


def goal_update(ctx, gid, patch):
    unknown = set(patch) - GOAL_FIELDS
    if unknown:
        raise AiwosError("unknown goal fields: %s" % sorted(unknown))
    st = ctx.store
    with st.lock():
        goal = st.load_goal(gid)
        material = {"outcome", "success_criteria", "scope", "non_goals", "constraints"}
        changed = [k for k, v in patch.items() if goal.get(k) != v]
        goal.update(patch)
        # Changing what success means invalidates a previous confirmation.
        if goal.get("status") in ("CONFIRMED", "ACTIVE") and material & set(changed) and "status" not in patch:
            goal["status"] = "PROPOSED"
        st.save_goal(goal)
    pr = "IMPORTANT" if material & set(changed) else "INFO"
    emit(ctx, "GOAL_UPDATED", ["goal:" + gid], {"goal_id": gid, "fields": changed, "status": goal["status"]}, pr)
    return goal


def goal_confirm(ctx, gid, by=None, note=None):
    st = ctx.store
    with st.lock():
        goal = st.load_goal(gid)
        if not goal.get("success_criteria"):
            raise AiwosError("cannot confirm %s: it has no success_criteria" % gid)
        goal["status"] = "CONFIRMED"
        goal.setdefault("approvals", []).append(
            {"by": by or ctx.actor, "recorded_by": ctx.sender, "ts": iso(), "note": note or ""})
        st.save_goal(goal)
    emit(ctx, "GOAL_CONFIRMED", ["goal:" + gid], {"goal_id": gid, "by": by or ctx.actor}, "IMPORTANT")
    return goal


def goal_complete(ctx, gid, evidence):
    """evidence: {criterion_index(str|int): "reference"} — every success criterion needs one."""
    st = ctx.store
    v = View(st)
    goal = st.load_goal(gid)
    open_wp = [w for w, wp in v.work.items() if wp["goal"] == gid and v.status(w) not in TERMINAL]
    if open_wp:
        raise AiwosError("goal %s still has unfinished work: %s" % (gid, ", ".join(open_wp)))
    crit = goal.get("success_criteria", [])
    ev = {str(k): val for k, val in (evidence or {}).items()}
    missing = [i for i in range(len(crit)) if not ev.get(str(i))]
    if missing:
        raise AiwosError("no evidence for success criteria %s: %s" % (missing, [crit[i] for i in missing]))
    with st.lock():
        goal = st.load_goal(gid)
        goal["status"] = "COMPLETE"
        goal["criteria_evidence"] = ev
        st.save_goal(goal)
    emit(ctx, "GOAL_COMPLETED", ["goal:" + gid], {"goal_id": gid}, "IMPORTANT")
    return goal


# ====================================================================== work packages

def _normalize_work(item, gid):
    unknown = set(item) - WORK_FIELDS
    if unknown:
        raise AiwosError("unknown work fields: %s (allowed: %s)" % (sorted(unknown), sorted(WORK_FIELDS)))
    for req in ("title", "purpose"):
        if not item.get(req):
            raise AiwosError("work package needs '%s': %r" % (req, item.get("title")))
    if not item.get("success_criteria"):
        raise AiwosError("work package %r needs success_criteria" % item["title"])
    risk = item.get("risk", "medium")
    if risk not in RISKS:
        raise AiwosError("risk must be one of %s" % RISKS)
    wp = {"id": item.get("id"), "goal": gid, "title": item["title"], "purpose": item["purpose"],
          "outcome": item.get("outcome", ""), "depends_on": _clean_list(item.get("depends_on")),
          "resources": [norm_pattern(r) for r in _clean_list(item.get("resources"))],
          "success_criteria": _clean_list(item["success_criteria"]),
          "validation": _clean_list(item.get("validation")), "risk": risk,
          "complexity": item.get("complexity", "medium"), "kind": item.get("kind", "code")}
    for k in ("inputs", "outputs", "reads", "contracts", "expertise"):
        if item.get(k):
            wp[k] = [norm_pattern(x) if k == "reads" else x for x in _clean_list(item[k])]
    for k in ("objective", "notes"):
        if item.get(k):
            wp[k] = item[k]
    for chk in wp["validation"]:
        if not isinstance(chk, dict) or not chk.get("name") or not (chk.get("run") or chk.get("layer") in ("review", "manual")):
            raise AiwosError("validation entries need {name, run} or {name, layer: review|manual}: %r" % chk)
    return wp


def _reachable(work, src):
    """All work ids that `src` transitively depends on."""
    seen, stack = set(), list(work[src].get("depends_on", []))
    while stack:
        d = stack.pop()
        if d in seen or d not in work:
            continue
        seen.add(d)
        stack.extend(work[d].get("depends_on", []))
    return seen


def plan_check(work):
    """Graph problems + potential parallel write conflicts. Returns (errors, warnings)."""
    errors, warnings = [], []
    for wid, wp in work.items():
        for d in wp.get("depends_on", []):
            if d not in work:
                errors.append("%s depends on unknown %s" % (wid, d))
        if wid in _reachable(work, wid):
            errors.append("dependency cycle through %s" % wid)
    ids = sorted(work)
    reach = {w: _reachable(work, w) for w in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in reach[b] or b in reach[a]:
                continue  # ordered by dependencies: cannot run concurrently
            for ra in work[a].get("resources", []):
                for rb in work[b].get("resources", []):
                    if patterns_may_overlap(ra, rb):
                        warnings.append({"work": [a, b], "resources": [ra, rb],
                                         "advice": "serialize (add depends_on), split resources, or agree a contract"})
    return errors, warnings


def work_add(ctx, gid, items):
    st = ctx.store
    st.load_goal(gid)
    if isinstance(items, dict):
        items = [items]
    with st.lock():
        existing = {w["id"]: w for w in st.list_work()}
        new, keymap, taken = [], {}, []
        for it in items:
            wp = _normalize_work(it, gid)
            if not wp["id"]:
                wp["id"] = st.next_work_id(taken)
            elif not re.match(r"^WP-\d+$", wp["id"]):
                raise AiwosError("work ids look like WP-001")
            elif wp["id"] in existing:
                raise AiwosError("%s already exists (use `work update`)" % wp["id"])
            taken.append(wp["id"])
            if it.get("key"):
                keymap[it["key"]] = wp["id"]
            new.append(wp)
        for wp in new:  # allow depends_on to reference batch-local keys
            wp["depends_on"] = [keymap.get(d, d) for d in wp["depends_on"]]
        merged = dict(existing)
        merged.update({w["id"]: w for w in new})
        errors, warnings = plan_check(merged)
        if errors:
            raise AiwosError("plan rejected: " + "; ".join(errors))
        for wp in new:
            wp["created_at"] = iso()
            st.save_work(wp)
            # authored here (this call went through the user's permission prompt): its checks may run
            st.approve_commands([c["run"] for c in wp["validation"] if c.get("run")], ctx.actor)
    for wp in new:
        emit(ctx, "WORK_CREATED", ["goal:" + gid, "work:" + wp["id"]], {"work_id": wp["id"], "title": wp["title"]})
    new_ids = {w["id"] for w in new}
    relevant = [w for w in warnings if new_ids & set(w["work"])]
    for w in relevant:
        emit(ctx, "CONFLICT_DETECTED", ["goal:" + gid] + ["work:" + x for x in w["work"]],
             dict(w, potential=True, stage="planning"), "WARNING")
    return new, relevant


def work_update(ctx, wid, patch):
    st = ctx.store
    with st.lock():
        wp = st.find_work(wid)
        merged = dict({k: v for k, v in wp.items() if k in WORK_FIELDS}, **patch)
        new = _normalize_work(merged, wp["goal"])
        new["id"] = wid
        new["created_at"] = wp.get("created_at")
        allw = {w["id"]: w for w in st.list_work()}
        allw[wid] = new
        errors, warnings = plan_check(allw)
        if errors:
            raise AiwosError("update rejected: " + "; ".join(errors))
        st.save_work(new)
        st.approve_commands([c["run"] for c in new["validation"] if c.get("run")], ctx.actor)
    emit(ctx, "WORK_UPDATED", ["goal:" + new["goal"], "work:" + wid], {"work_id": wid, "fields": sorted(patch)}, "IMPORTANT")
    return new, [w for w in warnings if wid in w["work"]]


# ====================================================================== claims

def find_conflicts(view, session, resource, mode):
    out = []
    for c in view.claims:
        if not c["alive"] or c["session"] == session:
            continue
        if modes_conflict(mode, c["mode"]) and patterns_may_overlap(resource, c["resource"]):
            out.append(c)
    return out


def claim_acquire(ctx, resources, mode="WRITE", work=None, view=None, _locked=False):
    """Atomically acquire claims on several resources, or none at all."""
    sid = ctx.need_session()
    st = ctx.store
    mode = mode.upper()
    if mode not in ("READ", "WRITE", "EXCLUSIVE"):
        raise AiwosError("mode must be READ, WRITE or EXCLUSIVE")

    def _do():
        v = view or View(st)
        conflicts = []
        for r in resources:
            for c in find_conflicts(v, sid, norm_pattern(r), mode):
                conflicts.append({"resource": norm_pattern(r), "held": c["resource"], "mode": c["mode"],
                                  "session": c["session"], "work": c.get("work")})
        if conflicts:
            return None, conflicts
        mine = st.load_claims(sid)
        made = []
        for r in resources:
            r = norm_pattern(r)
            if any(c["resource"] == r and c.get("status") == "active" and c["mode"] == mode for c in mine):
                continue
            c = {"id": rand_id("CL"), "resource": r, "mode": mode, "work": work, "acquired_at": iso(),
                 "status": "active"}
            mine.append(c)
            made.append(c)
        st.save_claims(sid, mine)
        return made, []

    if _locked:
        made, conflicts = _do()
    else:
        with st.lock():
            made, conflicts = _do()
    scope = ["session:" + sid] + (["work:" + work] if work else [])
    if conflicts:
        emit(ctx, "CONFLICT_DETECTED", scope + ["work:" + c["work"] for c in conflicts if c.get("work")],
             {"prevented": True, "requested": resources, "mode": mode, "conflicts": conflicts}, "WARNING")
        return None, conflicts
    if made:
        emit(ctx, "CLAIM_ACQUIRED", scope, {"claims": [{"id": c["id"], "resource": c["resource"], "mode": c["mode"]} for c in made]}, "DEBUG")
    return made, []


def claim_release(ctx, work=None, resource=None, claim_id=None, everything=False, session=None, reason=None):
    sid = session or ctx.need_session()
    st = ctx.store
    released = []
    with st.lock():
        mine = st.load_claims(sid)
        for c in mine:
            if c.get("status") != "active":
                continue
            if everything or (work and c.get("work") == work) or (resource and c["resource"] == norm_pattern(resource)) \
                    or (claim_id and c["id"] == claim_id):
                c["status"] = "released"
                c["released_at"] = iso()
                if reason:
                    c["release_reason"] = reason
                released.append(c["resource"])
        st.save_claims(sid, mine)
    if released:
        emit(ctx, "CLAIM_RELEASED", ["session:" + sid] + (["work:" + work] if work else []),
             {"session": sid, "resources": released, "reason": reason}, "DEBUG")
    return released


def strip_worktree_prefix(store, rel):
    wd = store.config["git"]["worktree_dir"].strip("/") + "/"
    if rel and rel.startswith(wd):
        parts = rel[len(wd):].split("/", 1)
        return parts[1] if len(parts) > 1 else ""
    return rel


def check_write(ctx, path, view=None):
    """Decide whether the current session may write `path`. Returns (decision, reason)."""
    st = ctx.store
    rel = st.rel(path)
    if rel is None:
        return "allow", "outside project"
    rel = strip_worktree_prefix(st, rel)
    for p in st.config["enforcement"]["protected"]:
        if path_matches(rel, p):
            return "deny", "%s is protected framework state; change it through the `aiwos` CLI" % rel
    v = view or View(st)
    sid = ctx.session
    for c in v.claims:
        if c["alive"] and c["session"] != sid and c["mode"] in ("WRITE", "EXCLUSIVE") \
                and not c["resource"].startswith("work:") and path_matches(rel, c["resource"]):
            return "deny", ("%s is claimed (%s) by session %s for %s. Coordinate: `aiwos send %s REQUEST_RELEASE "
                            "--work %s`, pick other work (`aiwos next`), or agree a contract."
                            % (rel, c["mode"], c["session"], c.get("work") or "ad-hoc work", c["session"], c.get("work") or "-"))
    for pair in v.contested:
        if pair["loser"]["session"] == sid and path_matches(rel, pair["loser"]["resource"]):
            return "deny", "your claim on %s lost a conflict to session %s (claimed earlier); release it" % (
                pair["loser"]["resource"], pair["winner"]["session"])
    if sid:
        mine = [c for c in v.claims if c["session"] == sid and c["mode"] in ("WRITE", "EXCLUSIVE")
                and not c["resource"].startswith("work:")]
        if mine and not any(path_matches(rel, c["resource"]) for c in mine):
            msg = "%s is outside this session's claimed scope (%s). Extend it with `aiwos claim add '%s'` if this is intended." % (
                rel, ", ".join(c["resource"] for c in mine), rel)
            mode = st.config["enforcement"]["out_of_scope_writes"]
            return ("deny" if mode == "deny" else "warn"), msg
    return "allow", "ok"


# ====================================================================== work lifecycle

def _goal_ready_for_execution(goal):
    return goal.get("status") in ("CONFIRMED", "ACTIVE")


def work_claim(ctx, wid, force=False):
    sid = ctx.need_session()
    st = ctx.store
    with st.lock():
        v = View(st)
        if wid not in v.work:
            raise AiwosError("unknown work package %s" % wid)
        wp = v.work[wid]
        goal = v.goals[wp["goal"]]
        if not _goal_ready_for_execution(goal):
            raise AiwosError("%s is %s; confirm it with the user first (`aiwos goal confirm %s`)" % (goal["id"], goal["status"], goal["id"]))
        status, owner = v.status(wid), v.state[wid]["owner"]
        takeover = None
        if owner == sid:
            return {"work_id": wid, "status": status, "already_owned": True}
        if owner and not v.stale_owner(wid):
            raise AiwosError("%s is owned by live session %s" % (wid, owner))
        if owner:
            takeover = owner
        elif status == "WAITING" and not force:
            raise AiwosError("%s is waiting on %s" % (wid, ", ".join(v.blocking_deps(wid))))
        elif status in TERMINAL:
            raise AiwosError("%s is %s" % (wid, status))
        res = [("work:" + wid, "EXCLUSIVE")] + [(r, "WRITE") for r in wp.get("resources", [])] + \
              [(r, "READ") for r in wp.get("reads", [])]
        acquired = []
        for mode in ("EXCLUSIVE", "WRITE", "READ"):
            rs = [r for r, m in res if m == mode]
            if not rs:
                continue
            made, conflicts = claim_acquire(ctx, rs, mode, wid, view=v, _locked=True)
            if conflicts:
                for c in acquired:
                    c["status"] = "released"
                mine = st.load_claims(sid)
                ids = {c["id"] for c in acquired}
                for c in mine:
                    if c["id"] in ids:
                        c["status"] = "released"
                st.save_claims(sid, mine)
                raise AiwosError("cannot claim %s, resource conflict: %s" % (wid, conflicts))
            acquired += made
        s = st.load_session(sid) or {}
        if s:
            s["work"] = wid
            s["goal"] = wp["goal"]
            st.save_session(s)
    data = {"work_id": wid, "session": sid}
    if takeover:
        data["takeover_from"] = takeover
    emit(ctx, "WORK_CLAIMED", ["goal:" + wp["goal"], "work:" + wid], data, "IMPORTANT" if takeover else "INFO")
    if goal["status"] == "CONFIRMED":
        with st.lock():
            g = st.load_goal(goal["id"])
            g["status"] = "ACTIVE"
            st.save_goal(g)
    return {"work_id": wid, "takeover_from": takeover, "claims": [c["resource"] for c in acquired],
            "branch": v.state[wid]["branch"], "handoff": v.state[wid]["handoff"]}


def _owned(ctx, wid, v=None):
    sid = ctx.need_session()
    v = v or View(ctx.store)
    if wid not in v.work:
        raise AiwosError("unknown work package %s" % wid)
    if v.state[wid]["owner"] != sid:
        raise AiwosError("%s is not owned by this session (owner: %s). Claim it first." % (wid, v.state[wid]["owner"]))
    return v


def work_start(ctx, wid, worktree=None):
    v = _owned(ctx, wid)
    st = ctx.store
    wp, s = v.work[wid], v.state[wid]
    info = {}
    # Anything that writes repository files gets its own branch; pure research/ops packages without resources do not.
    branch_based = st.is_git and wp.get("resources")
    if branch_based:
        use_wt = st.config["git"]["use_worktrees"] if worktree is None else worktree
        info = gitops.start_work_branch(st, wid, wp["title"], use_wt)
        sess = st.load_session(ctx.session)
        if sess:
            sess.update(branch=info["branch"], worktree=info["worktree"])
            st.save_session(sess)
    emit(ctx, "WORK_STARTED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "branch": info.get("branch"), "worktree": info.get("worktree"),
          "base": info.get("base"), "resumed": bool(s["branch"]) or info.get("reused", False)})
    return dict(info, work_id=wid)


def work_block(ctx, wid, reason, needs_input=False):
    v = _owned(ctx, wid)
    wp = v.work[wid]
    emit(ctx, "WORK_BLOCKED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "reason": reason, "needs_input": needs_input}, "BLOCKING")


def work_unblock(ctx, wid):
    v = _owned(ctx, wid)
    emit(ctx, "WORK_UNBLOCKED", ["goal:" + v.work[wid]["goal"], "work:" + wid], {"work_id": wid})


def scope_violations(store, wp, files):
    allowed = wp.get("resources", [])
    return [f for f in files if not any(path_matches(f, r) for r in allowed)]


def work_submit(ctx, wid, allow_scope=False):
    v = _owned(ctx, wid)
    st, wp, s = ctx.store, v.work[wid], v.state[wid]
    files, commits, violations = [], [], []
    if s["branch"]:
        files = gitops.changed_files(st, s)
        commits = gitops.commits(st, s)
        violations = scope_violations(st, wp, files)
        wt = s.get("worktree")
        if wt and os.path.isdir(os.path.join(st.main_root, *wt.split("/"))) and \
                gitops.out(["status", "--porcelain"], os.path.join(st.main_root, *wt.split("/"))):
            raise AiwosError("worktree %s has uncommitted changes; commit them before submitting" % wt)
        if violations and not allow_scope:
            emit(ctx, "CONFLICT_DETECTED", ["goal:" + wp["goal"], "work:" + wid],
                 {"work_id": wid, "scope_violation": violations}, "WARNING")
            raise AiwosError("changes outside %s's resources: %s. Revert them, add them to the work package "
                             "resources (`work update`), or pass --allow-scope with a reason." % (wid, violations))
    emit(ctx, "WORK_SUBMITTED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "commits": commits, "changed": files[:200], "scope_violation": violations})
    return {"work_id": wid, "changed": files, "commits": commits, "scope_violation": violations}


def completion_gate(store, v, wid):
    """List of reasons the work package cannot be completed (empty = may complete)."""
    wp, s = v.work[wid], v.state[wid]
    problems = []
    if s["status"] in ("PLANNED", "CLAIMED", "IN_PROGRESS", "BLOCKED", "NEEDS_INPUT"):
        problems.append("work has not been submitted (status %s)" % s["status"])
    if not s["validation"] or not s["validation"]["passed"]:
        problems.append("no passing validation run since last submission (`aiwos validate %s`)" % wid)
    needs_review = wp.get("risk") in store.config["validation"]["review_required_for_risk"] or \
        any(c.get("layer") == "review" for c in wp.get("validation", []))
    r = s["review"]
    if needs_review:
        if not r:
            problems.append("risk=%s requires an independent review (`aiwos review %s --verdict ...`)" % (wp.get("risk"), wid))
        elif not r["passed"]:
            problems.append("independent review failed")
        elif not r.get("reviewer") or r.get("reviewer") == s["owner"]:
            problems.append("review must come from a reviewer other than the builder session")
    elif r and not r["passed"]:
        problems.append("review failed")
    if s["branch"] and store.config["git"]["require_merge"]:
        if not gitops.is_merged(store, s["branch"], s["base"] or gitops.base_branch(store)):
            problems.append("branch %s is not merged into %s yet" % (s["branch"], s["base"]))
    return problems


def _clear_session_work(store, sid, wid):
    s = store.load_session(sid) if sid else None
    if s and s.get("work") == wid:
        s["work"] = None
        store.save_session(s)


def work_complete(ctx, wid, keep_worktree=False):
    st = ctx.store
    v = _owned(ctx, wid)
    problems = completion_gate(st, v, wid)
    if problems:
        raise AiwosError("cannot complete %s:\n  - %s" % (wid, "\n  - ".join(problems)))
    wp, s = v.work[wid], v.state[wid]
    emit(ctx, "WORK_COMPLETED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "validation": s["validation"]["id"], "review": (s["review"] or {}).get("id"),
          "branch": s["branch"]}, "IMPORTANT")
    claim_release(ctx, work=wid, reason="completed")
    _clear_session_work(st, ctx.session, wid)
    removed = False
    if s["worktree"] and not keep_worktree:
        removed = gitops.remove_worktree(st, s["worktree"])
    # Event-driven readiness: tell dependents they may start.
    v2 = View(st)
    ready = [w for w, x in v2.work.items() if wid in x.get("depends_on", []) and v2.status(w) == "READY"]
    for w in ready:
        emit(ctx, "NOTIFICATION", ["goal:" + wp["goal"], "work:" + w],
             {"work_id": w, "message": "dependencies complete; %s is READY" % w, "cause": wid}, "IMPORTANT")
    return {"work_id": wid, "now_ready": ready, "worktree_removed": removed}


def work_fail(ctx, wid, reason):
    v = _owned(ctx, wid)
    emit(ctx, "WORK_FAILED", ["goal:" + v.work[wid]["goal"], "work:" + wid], {"work_id": wid, "reason": reason}, "BLOCKING")
    claim_release(ctx, work=wid, reason="failed")
    _clear_session_work(ctx.store, ctx.session, wid)


def work_release(ctx, wid, reason=None):
    v = _owned(ctx, wid)
    emit(ctx, "WORK_RELEASED", ["goal:" + v.work[wid]["goal"], "work:" + wid], {"work_id": wid, "reason": reason}, "IMPORTANT")
    claim_release(ctx, work=wid, reason=reason or "released")
    _clear_session_work(ctx.store, ctx.session, wid)


def next_work(ctx, expertise=None, limit=5):
    """Pull-based scheduling: ready, unowned, no claim conflicts, goal confirmed."""
    v = View(ctx.store)
    out = []
    for wid, wp in v.work.items():
        goal = v.goals.get(wp["goal"], {})
        stale = v.stale_owner(wid)
        if not _goal_ready_for_execution(goal):
            continue
        if not stale and (v.status(wid) != "READY" or v.state[wid]["owner"]):
            continue
        if expertise and expertise not in wp.get("expertise", []):
            continue
        conflicts = []
        for r in wp.get("resources", []):
            conflicts += [c["session"] for c in find_conflicts(v, ctx.session, r, "WRITE")
                          if c.get("work") != wid]
        if conflicts:
            continue
        out.append({"work_id": wid, "title": wp["title"], "goal": wp["goal"], "risk": wp.get("risk"),
                    "expertise": wp.get("expertise", []), "recover": stale,
                    "unblocks": v.critical_path_len(wid) - 1})
    out.sort(key=lambda x: (not x["recover"], -x["unblocks"], RISKS.index(x["risk"] or "medium") * -1, x["work_id"]))
    return out[:limit]


# ====================================================================== handoff

def handoff_create(ctx, wid, completed="", remaining="", problems="", next_action="", decisions=(), release=True):
    st = ctx.store
    v = _owned(ctx, wid)
    wp, s = v.work[wid], v.state[wid]
    changed = gitops.changed_files(st, s) if s["branch"] else []
    hid = "H-%s-%s" % (wid, rand_id("x", 6)[2:])
    val = s["validation"]
    lines = [
        "# Handoff %s — %s %s" % (hid, wid, wp["title"]), "",
        "| | |", "|---|---|",
        "| Goal | %s |" % wp["goal"], "| From session | %s (%s) |" % (ctx.session, ctx.actor),
        "| Created | %s |" % iso(), "| Branch | %s |" % (s["branch"] or "—"),
        "| Worktree | %s |" % (s["worktree"] or "—"),
        "| Validation | %s |" % (("%s %s" % (val["id"], "PASSED" if val["passed"] else "FAILED")) if val else "not run"),
        "", "## Completed", completed or "—", "", "## Remaining", remaining or "—", "",
        "## Known problems", problems or "—", "", "## Recommended next action", next_action or "—", "",
        "## Decisions", "\n".join("- " + d for d in decisions) or "—", "",
        "## Changed artifacts", "\n".join("- `%s`" % f for f in changed[:100]) or "—", "",
    ]
    path = st.p("handoffs", hid + ".md")
    write_text_atomic(path, "\n".join(lines))
    emit(ctx, "HANDOFF_CREATED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "handoff": hid, "path": "state/handoffs/%s.md" % hid}, "IMPORTANT",
         refs=["state/handoffs/%s.md" % hid])
    if release:
        claim_release(ctx, work=wid, reason="handoff " + hid)
        _clear_session_work(st, ctx.session, wid)
    return {"handoff": hid, "path": path}


# ====================================================================== sessions & recovery

def session_start(store, claude_session=None, label=None, actor=None, actor_type="AI", sid=None):
    sid = sid or ("S-" + (claude_session or rand_id("x", 8)[2:])[:8])
    s = store.load_session(sid)
    t = iso()
    fresh = not s or s.get("status") not in ("active", "paused")
    if fresh:
        s = {"id": sid, "actor": actor or store.actor(), "actor_type": actor_type, "status": "active",
             "claude_session": claude_session, "label": label, "goal": None, "work": None,
             "branch": None, "worktree": None, "started_at": t, "last_seen": t, "cwd": store.root}
    s.update(last_seen=t, status="active")
    store.save_session(s)
    if fresh:
        ctx = Ctx(store, sid, s["actor"], actor_type)
        emit(ctx, "SESSION_STARTED", ["session:" + sid], {"session": sid, "label": label}, "DEBUG")
    return s


def heartbeat(store, sid, min_interval=60):
    s = store.load_session(sid)
    if not s:
        return None
    from .util import age_seconds
    if s.get("status") == "active" and age_seconds(s.get("last_seen")) < min_interval:
        return s
    if s.get("status") not in ("active", "paused"):
        return s  # expired sessions must re-register explicitly (their claims were recovered)
    s["last_seen"] = iso()
    store.save_session(s)
    return s


def session_end(ctx, status="completed"):
    st = ctx.store
    sid = ctx.need_session()
    s = st.load_session(sid)
    if not s:
        return None
    v = View(st)
    owned = v.owned_by(sid)
    s["status"] = "ended" if not owned else "paused"
    s["last_seen"] = iso()
    st.save_session(s)
    if not owned:
        claim_release(ctx, everything=True, reason="session ended")
    emit(ctx, "SESSION_COMPLETED" if not owned else "SESSION_PAUSED", ["session:" + sid],
         {"session": sid, "owned_work": owned}, "DEBUG" if not owned else "INFO")
    return s


def recover(ctx):
    """Expire dead sessions, free their claims, and return their work to the pool (history kept)."""
    st = ctx.store
    report = {"expired_sessions": [], "released_work": [], "released_claims": 0}
    with st.lock():
        v = View(st)
        for sid, s in v.sessions.items():
            if s.get("status") in ("active", "paused") and sid not in v.alive:
                s["status"] = "expired"
                st.save_session(s)
                report["expired_sessions"].append(sid)
    sys_ctx = Ctx(st, ctx.session, ctx.actor, "SYSTEM" if not ctx.session else ctx.actor_type)
    for sid in report["expired_sessions"]:
        report["released_claims"] += len(claim_release(sys_ctx, everything=True, session=sid, reason="lease expired"))
        for wid in v.owned_by(sid):
            emit(sys_ctx, "WORK_RELEASED", ["goal:" + v.work[wid]["goal"], "work:" + wid],
                 {"work_id": wid, "reason": "owner session %s lease expired" % sid, "system": True,
                  "previous_owner": sid, "branch": v.state[wid]["branch"]}, "WARNING")
            report["released_work"].append(wid)
        emit(sys_ctx, "SESSION_FAILED", ["session:" + sid], {"session": sid, "reason": "lease expired"}, "WARNING")
    return report


# ====================================================================== inbox

def subscriptions(v, sid):
    s = v.sessions.get(sid) or {}
    subs = {"session:" + sid}
    owned = v.owned_by(sid)
    for wid in owned:
        subs.add("work:" + wid)
        for d in v.work[wid].get("depends_on", []):
            subs.add("work:" + d)
    if s.get("goal"):
        subs.add("goal:" + s["goal"])
    return subs


def inbox(ctx, min_priority=None, mark_read=False, include_read=False):
    from .acp import priority_rank
    sid = ctx.need_session()
    st = ctx.store
    v = View(st)
    floor = priority_rank(min_priority or st.config["context"]["inbox_min_priority"])
    subs = subscriptions(v, sid)
    cur = st.cursor(sid)
    after = "" if include_read else cur.get("after", "")
    out = []
    for ev in v.events:
        if (ev.get("ts", ""), ev.get("id", "")) <= tuple(after.split("|", 1)) if after else False:
            continue
        if (ev.get("from") or {}).get("session") == sid:
            continue
        directed = ev.get("kind") == "command" and ev.get("to") == "session:" + sid
        relevant = directed or (set(ev.get("scope", [])) & subs and priority_rank(ev.get("priority")) >= floor)
        if relevant:
            out.append(ev)
    if mark_read and v.events:
        last = v.events[-1]
        st.save_cursor(sid, {"after": "%s|%s" % (last.get("ts", ""), last.get("id", ""))})
    return out
