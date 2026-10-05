"""CLI surface: the commands skills rely on behave and return correct exit codes."""

import json
import os
import unittest

from helpers import ProjectCase


class CliTests(ProjectCase):
    def ok(self, *args, **kw):
        r = self.cli(*args, **kw)
        self.assertEqual(r.returncode, 0, "%s\n%s%s" % (args, r.stdout, r.stderr))
        return r.stdout

    def test_goal_plan_decision_route_flow(self):
        goal = {"title": "Docs site", "outcome": "site builds", "success_criteria": ["builds", "has search"],
                "assumptions": ["ASSUMPTION: static hosting"], "status": "PROPOSED"}
        self.assertIn("GOAL-001", self.ok("goal", "new", "--in", "-", stdin=json.dumps(goal)))
        self.assertIn("ASSUMPTION: static hosting", self.ok("goal", "show", "GOAL-001"))
        self.ok("goal", "confirm", "GOAL-001", "--by", "alice")
        plan = [{"key": "a", "title": "Content", "purpose": "p", "success_criteria": ["c"], "risk": "low", "complexity": "low",
                 "kind": "doc", "resources": ["workspace/content/**"]},
                {"title": "Search", "purpose": "p", "depends_on": ["a"], "success_criteria": ["c"], "risk": "high",
                 "resources": ["workspace/site/**"]}]
        out = self.ok("work", "add", "GOAL-001", "--in", "-", stdin=json.dumps(plan))
        self.assertIn("WP-002", out)
        self.assertIn("WP-002 WAITING", self.ok("work", "graph", "GOAL-001").replace("  ", " ").replace("  ", " "))
        self.assertIn("tier=light", self.ok("route", "WP-001"))
        self.assertIn("tier=deep", self.ok("route", "WP-002"))
        self.assertIn("tier=review", self.ok("route", "WP-002", "--kind", "review"))
        out = self.ok("decision", "new", "Use static site generator", "--goal", "GOAL-001")
        self.assertIn("D-001", out)
        self.ok("decision", "accept", "D-001")
        self.ok("decision", "new", "Switch generator", "--goal", "GOAL-001")
        self.ok("decision", "supersede", "D-001", "--by", "D-002")
        path = os.path.join(self.root, "knowledge", "decisions")
        d1 = [f for f in os.listdir(path) if f.startswith("D-001")][0]
        with open(os.path.join(path, d1), encoding="utf-8") as f:
            self.assertIn("**Status:** SUPERSEDED by D-002", f.read())
        types = [e["type"] for e in json.loads(self.ok("events", "--json"))]
        for t in ("GOAL_CREATED", "GOAL_CONFIRMED", "WORK_CREATED", "DECISION_PROPOSED", "DECISION_ACCEPTED", "DECISION_SUPERSEDED"):
            self.assertIn(t, types)

    def test_utf8_stdin(self):
        self.ok("goal", "new", "--in", "-", stdin=json.dumps({"title": "Café — naïve 日本"}, ensure_ascii=False))
        self.assertIn("Café — naïve 日本", self.ok("goal", "show", "GOAL-001"))

    def test_session_claim_status_and_exit_codes(self):
        self.ok("session", "start", "--id", "S-one")
        self.ok("session", "start", "--id", "S-two")
        self.ok("claim", "add", "workspace/a/**", session="S-one")
        r = self.cli("claim", "add", "workspace/a/x.py", session="S-two")
        self.assertEqual(r.returncode, 3)
        self.assertIn("CONFLICT", r.stdout)
        r = self.cli("claim", "check", "workspace/a/x.py", session="S-two")
        self.assertEqual(r.returncode, 3)
        status = self.ok("status")
        self.assertIn("S-one WRITE workspace/a/**", status)
        r = self.cli("work", "claim", "WP-404", session="S-one")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown work package", r.stderr)

    def test_acp_endpoint_and_views(self):
        msg = {"protocol": "acp/1", "kind": "command", "type": "CREATE_GOAL", "id": "C1",
               "data": {"goal": {"title": "acp goal", "success_criteria": ["x"]}}}
        resp = json.loads(self.ok("acp", stdin=json.dumps(msg)))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["reply_to"], "C1")
        r = self.cli("acp", stdin=json.dumps(dict(msg, protocol="acp/9")))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "INCOMPATIBLE_VERSION")
        self.assertIn("events", json.loads(self.ok("metrics")))
        self.ok("doctor")
        self.write("final2.md", "x")
        self.write("src/notes.py", "# TEMPORARY hack\n")
        audit = self.ok("audit")
        self.assertIn("root-clutter", audit)
        self.assertIn("unbounded-temporary", audit)
        self.write("workspace/ok.py", "# TEMPORARY: fast path; scope: demo; replace when: real API exists\n")
        self.assertNotIn("workspace/ok.py", self.ok("audit"))


if __name__ == "__main__":
    unittest.main()
