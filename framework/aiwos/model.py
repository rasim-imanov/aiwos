"""Derived view of the project: work-package status is folded from the event log.

Statuses: PLANNED -> (WAITING | READY) -> CLAIMED -> IN_PROGRESS -> READY_FOR_REVIEW
          -> VALIDATED | VALIDATION_FAILED -> COMPLETE ; plus BLOCKED, NEEDS_INPUT, FAILED.
A work package whose owner session lease expired is flagged `stale_owner`.
"""

from .util import patterns_may_overlap

ACTIVE = {"CLAIMED", "IN_PROGRESS", "BLOCKED", "NEEDS_INPUT", "READY_FOR_REVIEW", "VALIDATED", "VALIDATION_FAILED"}
TERMINAL = {"COMPLETE", "FAILED", "CANCELLED"}


def work_id(ev):
    return (ev.get("data") or {}).get("work_id")


class View:
    def __init__(self, store):
        self.store = store
        self.sessions = {s["id"]: s for s in store.list_sessions()}
        self.alive = {sid for sid, s in self.sessions.items() if store.session_alive(s)}
        self.goals = {}
        for gid in store.list_goal_ids():
            try:
                self.goals[gid] = store.load_goal(gid)
            except Exception:
                continue
        self.work = {w["id"]: w for w in store.list_work()}
        self.events = store.events()
        self.warnings = []
        self.state = {wid: {"status": "PLANNED", "owner": None, "validation": None, "review": None,
                            "handoff": None, "branch": None, "worktree": None, "base": None,
                            "reason": None, "commits": [], "artifacts": []} for wid in self.work}
        self._fold()
        self.claims = [c for c in store.all_claims() if c.get("status", "active") == "active"]
        for c in self.claims:
            c["alive"] = c.get("session") in self.alive
        self.contested = self._contested_claims()

    # -------------------------------------------------------------- fold
    def _fold(self):
        for ev in self.events:
            wid = work_id(ev)
            t = ev.get("type")
            if not wid or wid not in self.state:
                continue
            st = self.state[wid]
            d = ev.get("data") or {}
            sender = (ev.get("from") or {}).get("session")
            if t in ("VALIDATION_PASSED", "VALIDATION_FAILED"):
                rec = {"id": d.get("validation_id"), "passed": t == "VALIDATION_PASSED",
                       "layer": d.get("layer", "technical"), "ts": ev.get("ts"), "by": sender,
                       "reviewer": d.get("reviewer")}
                if rec["layer"] == "review":
                    st["review"] = rec
                else:
                    st["validation"] = rec
                if st["status"] in ACTIVE:
                    st["status"] = self._post_validation_status(st)
                continue
            if t == "ARTIFACT_CREATED" or t == "ARTIFACT_CHANGED":
                p = d.get("path")
                if p and p not in st["artifacts"]:
                    st["artifacts"].append(p)
                continue
            if t not in ("WORK_CLAIMED", "WORK_CREATED", "WORK_UPDATED") and t.startswith("WORK_") \
                    and st["owner"] and sender and sender != st["owner"] and not d.get("system"):
                self.warnings.append("stale event %s %s from %s ignored (owner is %s)" % (ev["id"], t, sender, st["owner"]))
                continue
            if t == "WORK_CLAIMED":
                st.update(owner=d.get("session") or sender, status="CLAIMED", reason=None)
            elif t == "WORK_STARTED":
                st.update(status="IN_PROGRESS", branch=d.get("branch") or st["branch"],
                          worktree=d.get("worktree") or st["worktree"], base=d.get("base") or st["base"])
            elif t == "WORK_BLOCKED":
                st.update(status="NEEDS_INPUT" if d.get("needs_input") else "BLOCKED", reason=d.get("reason"))
            elif t == "WORK_UNBLOCKED":
                st.update(status="IN_PROGRESS", reason=None)
            elif t == "WORK_SUBMITTED":
                st.update(status="READY_FOR_REVIEW", commits=d.get("commits") or st["commits"])
                st["validation"] = None   # a new submission must be validated again
                st["review"] = None
            elif t == "WORK_COMPLETED":
                st.update(status="COMPLETE", owner=None)
            elif t == "WORK_FAILED":
                st.update(status="FAILED", reason=d.get("reason"))
            elif t == "WORK_RELEASED":
                st.update(owner=None, status="PLANNED", reason=d.get("reason"))
            elif t == "HANDOFF_CREATED":
                st.update(owner=None, status="PLANNED", handoff=d.get("handoff"))

    @staticmethod
    def _post_validation_status(st):
        v, r = st["validation"], st["review"]
        if (v and not v["passed"]) or (r and not r["passed"]):
            return "VALIDATION_FAILED"
        if v and v["passed"]:
            return "VALIDATED"
        return st["status"]

    # -------------------------------------------------------------- derived
    def status(self, wid):
        st = self.state[wid]
        if st["status"] == "PLANNED":
            deps = self.work[wid].get("depends_on", [])
            return "READY" if all(self.state.get(d, {}).get("status") == "COMPLETE" for d in deps) else "WAITING"
        return st["status"]

    def stale_owner(self, wid):
        o = self.state[wid]["owner"]
        return bool(o) and o not in self.alive and self.state[wid]["status"] in ACTIVE

    def owned_by(self, sid):
        return [wid for wid, st in self.state.items() if st["owner"] == sid and st["status"] in ACTIVE]

    def blocking_deps(self, wid):
        return [d for d in self.work[wid].get("depends_on", []) if self.state.get(d, {}).get("status") != "COMPLETE"]

    def _contested_claims(self):
        """Overlapping live claims from different sessions (only possible after a sync merge)."""
        out = []
        live = [c for c in self.claims if c["alive"]]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                if a["session"] == b["session"] or not modes_conflict(a["mode"], b["mode"]):
                    continue
                if patterns_may_overlap(a["resource"], b["resource"]):
                    winner, loser = sorted([a, b], key=lambda c: (c.get("acquired_at", ""), c["session"]))
                    out.append({"winner": winner, "loser": loser})
        return out

    def critical_path_len(self, wid, _memo=None):
        memo = _memo if _memo is not None else {}
        if wid in memo:
            return memo[wid]
        dependents = [w for w, wp in self.work.items() if wid in wp.get("depends_on", [])]
        memo[wid] = 1 + max((self.critical_path_len(d, memo) for d in dependents), default=0)
        return memo[wid]


def modes_conflict(a, b):
    a, b = (a or "WRITE").upper(), (b or "WRITE").upper()
    if "EXCLUSIVE" in (a, b):
        return True
    return a == "WRITE" and b == "WRITE"
