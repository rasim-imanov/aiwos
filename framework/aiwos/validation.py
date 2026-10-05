"""Layered validation. Deterministic checks are executed here; the model only interprets results.

Layers: structural (built-in) -> technical (commands) -> requirement/quality (review) -> goal.
"""

import os
import subprocess
import time

from . import gitops
from .model import View
from .ops import Ctx, emit, scope_violations
from .util import AiwosError, iso, rand_id


def _tail(text, n):
    text = text or ""
    return text if len(text) <= n else "…" + text[-n:]


def pending_commands(store, wid, only=None):
    """Validation commands of `wid` that were neither written nor approved on this machine."""
    wp = store.find_work(wid)
    commands = [c["run"] for c in wp.get("validation", []) if c.get("run") and (not only or c["name"] in only)]
    return store.unapproved_commands(commands)


def run_checks(ctx, wid, only=None, approved=()):
    """`approved` = the exact command strings a person confirmed (the CLI requires an interactive terminal for
    that). Approval is bound to those strings, so a definition that changes in between is still refused."""
    st = ctx.store
    v = View(st)
    if wid not in v.work:
        raise AiwosError("unknown work package %s" % wid)
    wp, s = v.work[wid], v.state[wid]
    pending = [c for c in pending_commands(st, wid, only) if c not in set(approved or ())]
    if pending:
        raise AiwosError(
            "%s has validation commands that were not written or approved on this machine (e.g. received via "
            "`aiwos sync`). A person must review and approve them in their own terminal: "
            "`aiwos validate %s --approve`. Commands:\n  %s" % (wid, wid, "\n  ".join(pending)))
    if approved:
        st.approve_commands(list(approved), ctx.actor)
    cwd = st.root
    if s.get("worktree"):
        wt = os.path.join(st.main_root, *s["worktree"].split("/"))
        if os.path.isdir(wt):
            cwd = wt
    vid = rand_id("V")
    emit(ctx, "VALIDATION_STARTED", ["goal:" + wp["goal"], "work:" + wid], {"work_id": wid, "validation_id": vid}, "DEBUG")
    results = []

    # structural: scope discipline + declared outputs exist
    if s.get("branch"):
        files = gitops.changed_files(st, s)
        bad = scope_violations(st, wp, files)
        results.append({"name": "scope", "layer": "structural", "passed": not bad,
                        "detail": "out-of-scope changes: %s" % bad if bad else "%d changed files within resources" % len(files)})
    for out in wp.get("outputs", []):
        if isinstance(out, str) and "/" in out and not any(ch in out for ch in "*?"):
            exists = os.path.exists(os.path.join(cwd, *out.split("/")))
            results.append({"name": "output:" + out, "layer": "structural", "passed": exists,
                            "detail": "exists" if exists else "declared output missing"})

    # technical: declared commands, run for real
    timeout = int(st.config["validation"]["timeout_seconds"])
    tail = int(st.config["validation"]["output_tail_chars"])
    for chk in wp.get("validation", []):
        if not chk.get("run") or (only and chk["name"] not in only):
            continue
        t0 = time.time()
        try:
            r = subprocess.run(chk["run"], shell=True, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            passed, code, output = r.returncode == 0, r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            passed, code, output = False, None, "timed out after %ss" % timeout
        results.append({"name": chk["name"], "layer": chk.get("layer", "technical"), "passed": passed,
                        "exit_code": code, "seconds": round(time.time() - t0, 2), "command": chk["run"],
                        "output_tail": _tail(output, tail)})

    manual = [c["name"] for c in wp.get("validation", []) if c.get("layer") == "manual"]
    passed = bool(results) and all(r["passed"] for r in results)
    incomplete = not results
    rec = {"id": vid, "work_id": wid, "goal": wp["goal"], "ts": iso(), "by": ctx.sender,
           "commit": gitops.out(["rev-parse", "--short", s["branch"]], st.main_root, check=False) if s.get("branch") else None,
           "passed": passed and not incomplete, "incomplete": incomplete, "results": results,
           "manual_pending": manual}
    st.save_validation(rec)
    etype = "VALIDATION_PASSED" if rec["passed"] else "VALIDATION_FAILED"
    emit(ctx, etype, ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "validation_id": vid, "layer": "technical",
          "failed": [r["name"] for r in results if not r["passed"]],
          "incomplete": incomplete}, "INFO" if rec["passed"] else "WARNING",
         refs=["state/validation/%s.json" % vid])
    return rec


def record_review(ctx, wid, verdict, reviewer, notes=None, findings=None):
    """Record an independent review (requirement/quality layer). `reviewer` names the independent agent/human."""
    st = ctx.store
    v = View(st)
    if wid not in v.work:
        raise AiwosError("unknown work package %s" % wid)
    verdict = verdict.lower()
    if verdict not in ("pass", "fail"):
        raise AiwosError("verdict must be pass or fail")
    if not reviewer:
        raise AiwosError("--reviewer is required (the independent reviewer's identity)")
    wp = v.work[wid]
    vid = rand_id("V")
    rec = {"id": vid, "work_id": wid, "goal": wp["goal"], "ts": iso(), "by": ctx.sender, "layer": "review",
           "reviewer": reviewer, "passed": verdict == "pass", "notes": notes, "findings": findings or []}
    st.save_validation(rec)
    emit(ctx, "VALIDATION_PASSED" if rec["passed"] else "VALIDATION_FAILED", ["goal:" + wp["goal"], "work:" + wid],
         {"work_id": wid, "validation_id": vid, "layer": "review", "reviewer": reviewer},
         "INFO" if rec["passed"] else "WARNING",
         refs=[notes] if notes and os.path.exists(os.path.join(st.root, notes)) else None)
    return rec


def contradictions(store, wid):
    """Flag validation evidence that disagrees (e.g. tests pass but review fails)."""
    v = View(store)
    s = v.state[wid]
    out = []
    if s["validation"] and s["review"] and s["validation"]["passed"] != s["review"]["passed"]:
        out.append("technical validation %s but review %s" % (
            "passed" if s["validation"]["passed"] else "failed", "passed" if s["review"]["passed"] else "failed"))
    return out


__all__ = ["run_checks", "record_review", "contradictions", "Ctx"]
