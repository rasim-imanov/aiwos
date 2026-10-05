"""Goal, work graph, claims, sessions, communication, validation and context (spec §76)."""

import json
import os
import sys
import unittest

from helpers import ProjectCase, ops

from aiwos import acp
from aiwos.acp_server import handle
from aiwos.context import projection, trace
from aiwos.model import View
from aiwos.util import AiwosError, patterns_may_overlap, path_matches
from aiwos.validation import contradictions, record_review, run_checks


class NoGit(ProjectCase):
    """Most coordination logic does not need git; doc/research work packages never branch."""
    use_git = False


# ====================================================================== goals
class GoalTests(NoGit):
    def test_rough_goal_is_draft_until_confirmed(self):
        g = ops.goal_create(self.human(), {"title": "Build something"})
        self.assertEqual(g["status"], "DRAFT")
        with self.assertRaises(AiwosError):  # no success criteria -> cannot confirm
            ops.goal_confirm(self.human(), g["id"])

    def test_unknown_fields_rejected_keeps_goal_compact(self):
        with self.assertRaises(AiwosError):
            ops.goal_create(self.human(), {"title": "x", "favourite_colour": "blue"})

    def test_ambiguous_goal_records_assumptions_and_unknowns(self):
        g = ops.goal_create(self.human(), {"title": "Auth", "assumptions": ["ASSUMPTION: email login"],
                                           "unknowns": ["SSO needed?"], "success_criteria": ["users can log in"]})
        self.assertEqual(g["assumptions"], ["ASSUMPTION: email login"])
        self.assertEqual(g["unknowns"], ["SSO needed?"])

    def test_work_cannot_be_claimed_before_confirmation(self):
        g = ops.goal_create(self.human(), {"title": "x", "success_criteria": ["y"]})
        ops.work_add(self.human(), g["id"], [{"title": "a", "purpose": "p", "success_criteria": ["c"], "kind": "doc"}])
        with self.assertRaisesRegex(AiwosError, "confirm"):
            ops.work_claim(self.session("A"), "WP-001")

    def test_changed_goal_requires_reconfirmation(self):
        gid = self.confirmed_goal()
        g = ops.goal_update(self.human(), gid, {"success_criteria": ["different now"]})
        self.assertEqual(g["status"], "PROPOSED")
        g = ops.goal_update(self.human(), gid, {"notes": "typo fix"})
        self.assertEqual(g["status"], "PROPOSED")  # stays proposed until confirmed again
        g = ops.goal_confirm(self.human(), gid, "alice")
        self.assertEqual(g["status"], "CONFIRMED")
        self.assertEqual(len(g["approvals"]), 2)

    def test_goal_completion_needs_finished_work_and_evidence(self):
        gid = self.confirmed_goal(criteria=["a", "b"])
        ops.work_add(self.human(), gid, [{"title": "w", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "risk": "low",
                                          "validation": [{"name": "ok", "run": "exit 0"}]}])
        with self.assertRaisesRegex(AiwosError, "unfinished"):
            ops.goal_complete(self.human(), gid, {"0": "x", "1": "y"})
        a = self.session("A")
        ops.work_claim(a, "WP-001")
        ops.work_start(a, "WP-001")
        ops.work_submit(a, "WP-001")
        run_checks(a, "WP-001")
        ops.work_complete(a, "WP-001")
        with self.assertRaisesRegex(AiwosError, "no evidence"):
            ops.goal_complete(self.human(), gid, {"0": "V-1"})
        self.assertEqual(ops.goal_complete(self.human(), gid, {"0": "V-1", "1": "doc"})["status"], "COMPLETE")


# ====================================================================== work graph
class GraphTests(NoGit):
    def plan(self):
        gid = self.confirmed_goal()
        new, warns = ops.work_add(self.human(), gid, [
            {"key": "model", "title": "Payment model", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
             "resources": ["backend/payment/model/**"]},
            {"key": "prov", "title": "Provider", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
             "depends_on": ["model"], "resources": ["backend/payment/provider/**"]},
            {"key": "ui", "title": "UI", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
             "resources": ["frontend/subscriptions/**"]},
            {"key": "tests", "title": "Integration tests", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
             "depends_on": ["prov", "ui"], "resources": ["tests/integration/**"]},
        ])
        return gid, new, warns

    def test_sequential_and_parallel_readiness(self):
        gid, new, warns = self.plan()
        self.assertEqual(warns, [])
        v = View(self.store)
        self.assertEqual(v.status("WP-001"), "READY")
        self.assertEqual(v.status("WP-002"), "WAITING")
        self.assertEqual(v.status("WP-003"), "READY")    # parallel with the backend chain
        self.assertEqual(v.status("WP-004"), "WAITING")
        nxt = [x["work_id"] for x in ops.next_work(self.session("A"))]
        self.assertEqual(nxt[0], "WP-001")               # unblocks the longest chain first
        self.assertIn("WP-003", nxt)

    def test_cycles_and_unknown_deps_rejected(self):
        gid, _, _ = self.plan()
        with self.assertRaisesRegex(AiwosError, "cycle"):
            ops.work_update(self.human(), "WP-001", {"depends_on": ["WP-004"]})
        with self.assertRaisesRegex(AiwosError, "unknown"):
            ops.work_add(self.human(), gid, [{"title": "x", "purpose": "p", "success_criteria": ["c"], "depends_on": ["WP-999"]}])

    def test_blocked_and_completed_dependencies(self):
        self.plan()
        a = self.session("A")
        ops.work_claim(a, "WP-001")
        with self.assertRaisesRegex(AiwosError, "waiting"):
            ops.work_claim(a, "WP-002")
        ops.work_block(a, "WP-001", "need API key", needs_input=True)
        self.assertEqual(View(self.store).status("WP-001"), "NEEDS_INPUT")
        ops.work_unblock(a, "WP-001")
        ops.work_update(self.human(), "WP-001", {"validation": [{"name": "ok", "run": "exit 0"}], "risk": "low"})
        ops.work_submit(a, "WP-001")
        run_checks(a, "WP-001")
        res = ops.work_complete(a, "WP-001")
        self.assertEqual(res["now_ready"], ["WP-002"])
        self.assertEqual(View(self.store).status("WP-002"), "READY")

    def test_planning_detects_parallel_overlap(self):
        gid = self.confirmed_goal()
        _, warns = ops.work_add(self.human(), gid, [
            {"title": "a", "purpose": "p", "success_criteria": ["c"], "resources": ["backend/payment/**"]},
            {"title": "b", "purpose": "p", "success_criteria": ["c"], "resources": ["backend/payment/PaymentService.java"]},
        ])
        self.assertEqual(len(warns), 1)
        self.assertTrue(any(e["type"] == "CONFLICT_DETECTED" for e in self.store.events()))


# ====================================================================== claims
class ClaimTests(NoGit):
    def test_patterns(self):
        self.assertTrue(path_matches("backend/payment/x/Y.java", "backend/payment/**"))
        self.assertTrue(path_matches("src/a.py", "src/**/*.py"))
        self.assertFalse(path_matches("frontend/a.ts", "backend/**"))
        self.assertTrue(patterns_may_overlap("backend/payment/**", "backend/payment/PaymentService.java"))
        self.assertTrue(patterns_may_overlap("**/*.java", "backend/x.java"))
        self.assertFalse(patterns_may_overlap("backend/payment/**", "frontend/subscriptions/**"))

    def test_non_overlapping_and_overlapping(self):
        a, b = self.session("A"), self.session("B")
        made, conf = ops.claim_acquire(a, ["backend/payment/**"])
        self.assertTrue(made and not conf)
        made, conf = ops.claim_acquire(b, ["frontend/subscriptions/**"])
        self.assertTrue(made and not conf)
        made, conf = ops.claim_acquire(b, ["backend/payment/PaymentService.java"])
        self.assertIsNone(made)
        self.assertEqual(conf[0]["session"], "S-A")
        # READ next to WRITE is fine, EXCLUSIVE is not
        self.assertFalse(ops.claim_acquire(b, ["backend/payment/**"], "READ")[1])

    def test_exclusive_blocks_readers(self):
        a, b = self.session("A"), self.session("B")
        ops.claim_acquire(a, ["db/schema.sql"], "EXCLUSIVE")
        self.assertTrue(ops.claim_acquire(b, ["db/schema.sql"], "READ")[1])

    def test_expired_and_released_claims(self):
        a, b = self.session("A"), self.session("B")
        ops.claim_acquire(a, ["x/**"])
        self.assertTrue(ops.claim_acquire(b, ["x/y"])[1])
        self.expire("S-A")                                   # crashed: lease lapses
        self.assertFalse(ops.claim_acquire(b, ["x/y"])[1])
        c = self.session("C")
        ops.claim_release(b, everything=True)
        self.assertFalse(ops.claim_acquire(c, ["x/y"])[1])

    def test_check_write_enforcement(self):
        a, b = self.session("A"), self.session("B")
        ops.claim_acquire(a, ["backend/**"])
        d, why = ops.check_write(b, os.path.join(self.root, "backend", "x.py"))
        self.assertEqual(d, "deny")
        self.assertIn("S-A", why)
        self.assertEqual(ops.check_write(a, os.path.join(self.root, "backend", "x.py"))[0], "allow")
        self.assertEqual(ops.check_write(a, os.path.join(self.root, "frontend", "x.py"))[0], "deny")  # out of own scope
        self.assertEqual(ops.check_write(a, os.path.join(self.root, ".ai", "state", "goals", "x"))[0], "deny")
        self.assertEqual(ops.check_write(self.session("C"), os.path.join(self.root, "docs", "x.md"))[0], "allow")


# ====================================================================== sessions
class SessionTests(NoGit):
    def setUp(self):
        super().setUp()
        self.gid = self.confirmed_goal()
        ops.work_add(self.human(), self.gid, [
            {"title": "pay", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "resources": ["backend/**"]},
            {"title": "ui", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "resources": ["frontend/**"]},
            {"title": "hooks", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "resources": ["webhooks/**"]}])

    def test_concurrent_sessions_take_independent_work(self):
        sess = [self.session(n) for n in "ABC"]
        taken = []
        for s in sess:
            wid = ops.next_work(s)[0]["work_id"]
            ops.work_claim(s, wid)
            taken.append(wid)
        self.assertEqual(sorted(taken), ["WP-001", "WP-002", "WP-003"])
        self.assertEqual(ops.next_work(self.session("D")), [])
        with self.assertRaisesRegex(AiwosError, "owned by live session"):
            ops.work_claim(self.session("D"), taken[0])

    def test_crashed_session_is_recovered(self):
        a, b = self.session("A"), self.session("B")
        ops.work_claim(a, "WP-001")
        ops.work_start(a, "WP-001")
        self.expire("S-A")
        v = View(self.store)
        self.assertTrue(v.stale_owner("WP-001"))
        self.assertTrue(ops.next_work(b)[0]["recover"])
        rep = ops.recover(b)
        self.assertEqual(rep["expired_sessions"], ["S-A"])
        self.assertEqual(rep["released_work"], ["WP-001"])
        r = ops.work_claim(b, "WP-001")
        self.assertEqual(View(self.store).state["WP-001"]["owner"], "S-B")
        self.assertIsNone(r.get("takeover_from"))

    def test_takeover_without_recover_command(self):
        a, b = self.session("A"), self.session("B")
        ops.work_claim(a, "WP-001")
        self.expire("S-A")
        self.assertEqual(ops.work_claim(b, "WP-001")["takeover_from"], "S-A")
        # The dead session's late events are stale and ignored.
        ops.emit(a, "WORK_COMPLETED", ["work:WP-001"], {"work_id": "WP-001"})
        v = View(self.store)
        self.assertEqual(v.status("WP-001"), "CLAIMED")
        self.assertTrue(any("stale event" in w for w in v.warnings))

    def test_handoff_releases_with_reference(self):
        a, b = self.session("A"), self.session("B")
        ops.work_claim(a, "WP-002")
        ops.work_start(a, "WP-002")
        h = ops.handoff_create(a, "WP-002", completed="layout", remaining="states", next_action="add empty state")
        self.assertTrue(os.path.exists(h["path"]))
        with open(h["path"], encoding="utf-8") as f:
            self.assertIn("add empty state", f.read())
        self.assertIn("WP-002", [x["work_id"] for x in ops.next_work(b)])
        r = ops.work_claim(b, "WP-002")
        self.assertEqual(r["handoff"], h["handoff"])
        self.assertEqual(projection(self.store, "WP-002", "S-B")["state"]["handoff"], "state/handoffs/%s.md" % h["handoff"])

    def test_session_end_pauses_when_owning_work(self):
        a = self.session("A")
        ops.work_claim(a, "WP-001")
        self.assertEqual(ops.session_end(a)["status"], "paused")
        b = self.session("B")
        self.assertEqual(ops.session_end(b)["status"], "ended")


# ====================================================================== communication (ACP)
class AcpTests(NoGit):
    def msg(self, **kw):
        m = {"protocol": "acp/1", "kind": "query", "type": "STATUS", "id": "Q-1", "data": {}}
        m.update(kw)
        return m

    def test_valid_command_and_query(self):
        a = self.session("A")
        r = handle(self.store, self.msg(kind="command", type="CREATE_GOAL", data={"goal": {"title": "via acp", "success_criteria": ["x"]}}), a)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["id"], "GOAL-001")
        r = handle(self.store, self.msg(), a)
        self.assertTrue(r["ok"])
        self.assertIn("GOAL-001", r["data"]["text"])

    def test_invalid_command(self):
        r = handle(self.store, self.msg(kind="command", type="DESTROY_EVERYTHING"), self.session("A"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "INVALID_MESSAGE")
        r = handle(self.store, self.msg(kind="command", type="REQUEST_RELEASE"), self.session("A"))
        self.assertFalse(r["ok"])  # directed command without audience
        r = handle(self.store, self.msg(kind="command", type="CLAIM_WORK", data={"work_id": "WP-404"}), self.session("A"))
        self.assertEqual(r["error"]["code"], "REJECTED")

    def test_incompatible_version(self):
        r = handle(self.store, self.msg(protocol="acp/2"), self.session("A"))
        self.assertEqual(r["error"]["code"], "INCOMPATIBLE_VERSION")
        self.assertTrue(handle(self.store, self.msg(protocol="acp/1.3", extra_future_field=1), self.session("A"))["ok"])

    def test_duplicate_event_is_idempotent(self):
        a = self.session("A")
        ev = self.msg(kind="event", type="NOTIFICATION", id="EV-fixed-1", scope=["goal:GOAL-001"], data={"m": 1})
        self.assertTrue(handle(self.store, ev, a)["ok"])
        r = handle(self.store, ev, a)
        self.assertTrue(r["data"]["duplicate"])
        self.assertEqual(sum(1 for e in self.store.events() if e["id"] == "EV-fixed-1"), 1)

    def test_sender_spoofing_rejected(self):
        r = handle(self.store, self.msg(**{"from": {"session": "S-B"}}), self.session("A"))
        self.assertEqual(r["error"]["code"], "SENDER_MISMATCH")

    def test_event_filtering_and_directed_commands(self):
        gid = self.confirmed_goal()
        ops.work_add(self.human(), gid, [
            {"title": "a", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "resources": ["a/**"]},
            {"title": "b", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "resources": ["b/**"]}])
        a, b = self.session("A"), self.session("B")
        ops.work_claim(a, "WP-001")
        ops.work_claim(b, "WP-002")
        ops.emit(b, "NOTIFICATION", ["work:WP-002"], {"m": "only for B's work"}, "BLOCKING")
        ops.emit(b, "NOTIFICATION", ["work:WP-001"], {"m": "debug noise"}, "DEBUG")
        ops.emit(b, "NOTIFICATION", ["work:WP-001"], {"m": "important for A"}, "IMPORTANT")
        ops.send(b, "REQUEST_REVIEW", "S-A", ["work:WP-002"], {"note": "please review"})
        inbox = ops.inbox(a)
        texts = [json.dumps(e["data"]) for e in inbox]
        self.assertTrue(any("important for A" in t for t in texts))
        self.assertTrue(any("please review" in t for t in texts))
        self.assertFalse(any("only for B" in t or "debug noise" in t for t in texts))
        ops.inbox(a, mark_read=True)
        self.assertEqual(ops.inbox(a), [])  # stale messages do not reappear

    def test_validate_function(self):
        self.assertTrue(acp.validate({"protocol": "acp/1", "kind": "event", "type": "NOPE"}))
        self.assertEqual(acp.validate({"protocol": "acp/1", "kind": "event", "type": "WORK_COMPLETED",
                                       "scope": ["work:WP-1"], "priority": "INFO"}), [])


# ====================================================================== validation
class ValidationTests(NoGit):
    def setUp(self):
        super().setUp()
        self.gid = self.confirmed_goal()
        self.a = self.session("A")

    def add(self, checks, risk="low"):
        new, _ = ops.work_add(self.human(), self.gid, [{"title": "w", "purpose": "p", "success_criteria": ["c"],
                                                        "kind": "doc", "risk": risk, "validation": checks}])
        wid = new[0]["id"]
        ops.work_claim(self.a, wid)
        ops.work_start(self.a, wid)
        ops.work_submit(self.a, wid)
        return wid

    def test_passing(self):
        wid = self.add([{"name": "ok", "run": "exit 0"}])
        self.assertTrue(run_checks(self.a, wid)["passed"])
        ops.work_complete(self.a, wid)
        self.assertEqual(View(self.store).status(wid), "COMPLETE")

    def test_failing_blocks_completion_then_repair(self):
        flag = os.path.join(self.root, "fixed.flag")
        wid = self.add([{"name": "needs-flag", "run": '"%s" -c "import os,sys; sys.exit(0 if os.path.exists(%r) else 1)"' % (sys.executable, flag)}])
        rec = run_checks(self.a, wid)
        self.assertFalse(rec["passed"])
        self.assertEqual(View(self.store).status(wid), "VALIDATION_FAILED")
        with self.assertRaisesRegex(AiwosError, "no passing validation"):
            ops.work_complete(self.a, wid)
        open(flag, "w").close()                       # repair
        ops.work_submit(self.a, wid)                  # resubmission resets validation state
        self.assertEqual(View(self.store).state[wid]["validation"], None)
        self.assertTrue(run_checks(self.a, wid)["passed"])
        ops.work_complete(self.a, wid)

    def test_incomplete_validation_is_not_a_pass(self):
        wid = self.add([])
        rec = run_checks(self.a, wid)
        self.assertTrue(rec["incomplete"])
        self.assertFalse(rec["passed"])

    def test_review_required_and_independent(self):
        wid = self.add([{"name": "ok", "run": "exit 0"}], risk="high")
        run_checks(self.a, wid)
        with self.assertRaisesRegex(AiwosError, "independent review"):
            ops.work_complete(self.a, wid)
        record_review(self.a, wid, "pass", "S-A")      # builder reviewing itself does not count
        with self.assertRaisesRegex(AiwosError, "other than the builder"):
            ops.work_complete(self.a, wid)
        record_review(self.a, wid, "pass", "aiwos-reviewer")
        ops.work_complete(self.a, wid)

    def test_contradictory_validation(self):
        wid = self.add([{"name": "ok", "run": "exit 0"}], risk="medium")
        run_checks(self.a, wid)
        record_review(self.a, wid, "fail", "aiwos-reviewer", notes="edge cases missing")
        self.assertTrue(contradictions(self.store, wid))
        self.assertEqual(View(self.store).status(wid), "VALIDATION_FAILED")
        with self.assertRaisesRegex(AiwosError, "review failed"):
            ops.work_complete(self.a, wid)


# ====================================================================== context
class ContextTests(NoGit):
    def test_projection_is_relevant_and_reference_based(self):
        gid = self.confirmed_goal()
        other = self.confirmed_goal("Unrelated goal")
        self.write("workspace/specs/api.md", "# API\nGET /plans\n")
        self.write("knowledge/decisions/D-001-stripe.md", "# D-001: Stripe\n\n**Status:** ACCEPTED\nGoal: %s\n" % gid)
        self.write("knowledge/decisions/D-002-other.md", "# D-002: Other\n\n**Status:** ACCEPTED\nGoal: %s\n" % other)
        self.write("knowledge/decisions/D-003-old.md", "# D-003: Old\n\n**Status:** SUPERSEDED by D-001\nGoal: %s\n" % gid)
        ops.work_add(self.human(), gid, [
            {"key": "api", "title": "API spec", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
             "resources": ["workspace/specs/**"], "outputs": ["workspace/specs/api.md"]},
            {"title": "UI", "purpose": "p", "success_criteria": ["c"], "kind": "doc", "depends_on": ["api"],
             "contracts": ["plans-api"], "resources": ["workspace/frontend/**"]}])
        ops.emit(self.human(), "CONTRACT_CREATED", ["goal:" + gid],
                 {"name": "plans-api", "path": "workspace/specs/api.md", "hash": "h", "consumers": ["WP-002"]})
        p = projection(self.store, "WP-002", None)
        self.assertEqual([d["id"] for d in p["decisions"]], ["D-001"])  # irrelevant + superseded excluded
        self.assertEqual(p["contracts"][0]["path"], "workspace/specs/api.md")  # reference, not content
        self.assertNotIn("GET /plans", json.dumps(p))
        self.assertEqual(p["dependencies"][0]["outputs"], ["workspace/specs/api.md"])
        self.assertLess(len(json.dumps(p)), 4000)

    def test_trace_answers_why_an_artifact_exists(self):
        gid = self.confirmed_goal()
        ops.work_add(self.human(), gid, [{"title": "spec", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
                                          "resources": ["workspace/specs/**"]}])
        ops.emit(self.human(), "ARTIFACT_CREATED", ["work:WP-001"], {"path": "workspace/specs/a.md", "work_id": "WP-001",
                                                                     "kind": "AUTHORITATIVE"})
        t = trace(self.store, "workspace/specs/a.md")
        self.assertEqual(t["work"][0]["work_id"], "WP-001")
        self.assertEqual(t["work"][0]["goal"], gid)

    def test_stale_context_contract_change_notifies_consumers(self):
        gid = self.confirmed_goal()
        ops.work_add(self.human(), gid, [{"title": "ui", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
                                          "resources": ["ui/**"], "contracts": ["api"]}])
        self.write("workspace/specs/api.md", "v1")
        r = self.cli("contract", "add", "api", "workspace/specs/api.md", "--goal", gid, "--consumers", "WP-001")
        self.assertEqual(r.returncode, 0, r.stderr)
        b = self.session("B")
        ops.work_claim(b, "WP-001")
        self.write("workspace/specs/api.md", "v2 breaking")
        r = self.cli("contract", "check")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(any(e["type"] == "CONTRACT_CHANGED" for e in ops.inbox(b)))


if __name__ == "__main__":
    unittest.main()
