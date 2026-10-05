"""Git integration, multi-machine sync, hooks, and the end-to-end acceptance scenarios (spec §93)."""

import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import GIT_ENV, ProjectCase, git, ops

from aiwos.model import View
from aiwos.store import Store
from aiwos.sync import sync
from aiwos.util import AiwosError
from aiwos.validation import record_review, run_checks

PY = '"%s"' % sys.executable


def plan_subscription(case):
    gid = case.confirmed_goal("Subscription platform", ["users can subscribe", "webhooks processed"])
    ops.work_add(case.human(), gid, [
        {"key": "api", "title": "Plans API contract", "purpose": "shared contract", "kind": "doc", "risk": "low",
         "resources": ["workspace/specs/**"], "success_criteria": ["spec exists"],
         "validation": [{"name": "spec-exists", "run": PY + " -c \"import os,sys; sys.exit(0 if os.path.exists('workspace/specs/plans.md') else 1)\""}]},
        {"key": "be", "title": "Payment backend", "purpose": "domain", "depends_on": ["api"], "risk": "low",
         "resources": ["workspace/backend/**"], "success_criteria": ["module"], "expertise": ["backend"],
         "validation": [{"name": "unit", "run": PY + " workspace/backend/test_pay.py"}]},
        {"key": "fe", "title": "Subscription UI", "purpose": "ui", "depends_on": ["api"], "risk": "medium",
         "resources": ["workspace/frontend/**"], "success_criteria": ["page"], "expertise": ["frontend"],
         "validation": [{"name": "exists", "run": PY + " -c \"import os,sys; sys.exit(0 if os.path.exists('workspace/frontend/page.html') else 1)\""}]},
    ])
    return gid


class GitFlowTests(ProjectCase):
    def wt(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def test_worktree_branch_scope_and_merge_gate(self):
        plan_subscription(self)
        a = self.session("A")
        ops.work_claim(a, "WP-001")
        info = ops.work_start(a, "WP-001")
        self.assertTrue(info["branch"].startswith("ai/wp-001"))
        wt = self.wt(info["worktree"])
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"), info["branch"])
        # out-of-scope change is caught at submit
        self.write("workspace/specs/plans.md", "# Plans API\n", base=wt)
        self.write("stray.txt", "oops", base=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "WP-001: spec")
        with self.assertRaisesRegex(AiwosError, "outside WP-001"):
            ops.work_submit(a, "WP-001")
        git(wt, "rm", "-q", "stray.txt")
        git(wt, "commit", "-qm", "WP-001: remove stray")
        r = ops.work_submit(a, "WP-001")
        self.assertEqual(r["changed"], ["workspace/specs/plans.md"])
        self.assertTrue(run_checks(a, "WP-001")["passed"])
        with self.assertRaisesRegex(AiwosError, "not merged"):
            ops.work_complete(a, "WP-001")
        git(self.root, "merge", "-q", "--no-ff", info["branch"], "-m", "merge WP-001")
        res = ops.work_complete(a, "WP-001")
        self.assertEqual(sorted(res["now_ready"]), ["WP-002", "WP-003"])
        self.assertTrue(res["worktree_removed"])

    def test_scenario_e_recovery_keeps_branch(self):
        plan_subscription(self)
        a, b = self.session("A"), self.session("B")
        ops.work_claim(a, "WP-001")
        info = ops.work_start(a, "WP-001")
        self.write("workspace/specs/plans.md", "# partial\n", base=self.wt(info["worktree"]))
        git(self.wt(info["worktree"]), "add", "-A")
        git(self.wt(info["worktree"]), "commit", "-qm", "WP-001: wip")
        self.expire("S-A")                       # session A disappears
        ops.recover(b)
        ops.work_claim(b, "WP-001")
        info2 = ops.work_start(b, "WP-001")      # resumes the same branch + worktree
        self.assertEqual(info2["branch"], info["branch"])
        self.assertTrue(info2["reused"])
        self.assertTrue(os.path.exists(os.path.join(self.wt(info2["worktree"]), "workspace", "specs", "plans.md")))


class HookTests(ProjectCase):
    def payload(self, sid, event, path=None, cwd=None):
        p = {"session_id": sid, "cwd": cwd or self.root, "hook_event_name": event}
        if path:
            p.update(tool_name="Write", tool_input={"file_path": path})
        return p

    def test_hooks_end_to_end(self):
        plan_subscription(self)
        env_file = os.path.join(self.tmp, "env")
        open(env_file, "w").close()
        out = self.cli("hook", "session-start", stdin=json.dumps(self.payload("aaaa1111-x", "SessionStart")),
                       extra_env={"CLAUDE_ENV_FILE": env_file})
        self.assertEqual(out.returncode, 0, out.stderr)
        ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("S-aaaa1111", ctx)
        self.assertIn("GOAL-001", ctx)
        with open(env_file) as f:
            env_text = f.read()
        self.assertIn('AIWOS_SESSION="S-aaaa1111"', env_text)
        self.assertNotIn(":/", env_text.split("PATH=")[1][:4])  # no 'C:' drive colon in PATH entry
        self.hook("session-start", self.payload("bbbb2222-x", "SessionStart"))
        a = ops.Ctx(self.store, "S-aaaa1111", "a", "AI")
        ops.claim_acquire(a, ["workspace/backend/**"])
        target = os.path.join(self.root, "workspace", "backend", "pay.py")
        # B is denied, with an actionable reason
        r = self.hook("pre-write", self.payload("bbbb2222-x", "PreToolUse", target))
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("S-aaaa1111", r["hookSpecificOutput"]["permissionDecisionReason"])
        # A is allowed (no output = normal permission flow)
        self.assertIsNone(self.hook("pre-write", self.payload("aaaa1111-x", "PreToolUse", target)))
        # protected state
        r = self.hook("pre-write", self.payload("aaaa1111-x", "PreToolUse", os.path.join(self.root, ".ai", "state", "x.json")))
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        # directed message surfaces on next prompt, once
        ops.send(ops.Ctx(self.store, "S-bbbb2222", "b", "AI"), "REQUEST_RELEASE", "S-aaaa1111", ["work:WP-002"], {"note": "need backend"})
        r = self.hook("prompt", self.payload("aaaa1111-x", "UserPromptSubmit"))
        self.assertIn("REQUEST_RELEASE", r["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("prompt", self.payload("aaaa1111-x", "UserPromptSubmit")))
        # session end releases claims
        self.hook("session-end", self.payload("aaaa1111-x", "SessionEnd"))
        self.assertIsNone(self.hook("pre-write", self.payload("bbbb2222-x", "PreToolUse", target)))

    def test_hook_fails_open_outside_project_and_on_garbage(self):
        r = self.cli("hook", "pre-write", stdin="not json")
        self.assertEqual(r.returncode, 0)
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        r = self.cli("hook", "pre-write", stdin=json.dumps(self.payload("x", "PreToolUse", "/tmp/a", cwd=outside)), cwd=outside)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_contract_edit_notifies_via_post_hook(self):
        gid = plan_subscription(self)
        self.write("workspace/specs/plans.md", "v1")
        self.assertEqual(self.cli("contract", "add", "plans-api", "workspace/specs/plans.md", "--goal", gid,
                                  "--consumers", "WP-002,WP-003").returncode, 0)
        self.write("workspace/specs/plans.md", "v2")
        r = self.hook("post-write", self.payload("aaaa1111-x", "PostToolUse", os.path.join(self.root, "workspace", "specs", "plans.md")))
        self.assertIn("plans-api", r["hookSpecificOutput"]["additionalContext"])
        evs = [e for e in self.store.events() if e["type"] == "CONTRACT_CHANGED"]
        self.assertEqual(evs[-1]["priority"], "BLOCKING")
        self.assertIn("work:WP-003", evs[-1]["scope"])


class TwoMachineTests(ProjectCase):
    """Scenario C/D/F: Alice and Bob on different clones, coordinating through `aiwos sync`."""

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        cfg = os.path.join(self.root, ".ai", "config.json")
        doc = json.load(open(cfg))
        doc["sync"] = {"remote": "origin"}
        with open(cfg, "w") as f:
            json.dump(doc, f)
        git(self.root, "remote", "add", "origin", self.remote)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "enable sync")
        git(self.root, "push", "-q", "origin", "main")
        self.bob_root = os.path.join(self.tmp, "bob")
        git(self.tmp, "clone", "-q", self.remote, self.bob_root)
        self.alice = self.store = Store(self.root)  # reload: config now has a sync remote
        self.bob = Store(self.bob_root)

    def ctx(self, store, sid):
        ops.session_start(store, sid=sid, actor=sid, actor_type="AI")
        return ops.Ctx(store, sid, sid, "AI")

    def test_shared_goal_independent_work_and_handoff(self):
        gid = plan_subscription(self)
        a = self.ctx(self.alice, "S-alice")
        ops.work_claim(a, "WP-001")
        sync(self.alice)
        # Bob discovers the shared goal and the claimed package
        sync(self.bob)
        bv = View(self.bob)
        self.assertIn(gid, bv.goals)
        self.assertEqual(bv.state["WP-001"]["owner"], "S-alice")
        b = self.ctx(self.bob, "S-bob")
        self.assertEqual(ops.next_work(b), [])             # nothing safe yet: WP-002/3 wait on WP-001
        with self.assertRaisesRegex(AiwosError, "owned by live session"):
            ops.work_claim(b, "WP-001")
        # Alice completes the contract package; Bob learns WP-002/003 are ready
        ops.work_start(a, "WP-001")
        wt = os.path.join(self.root, ".ai", "worktrees", "WP-001")
        self.write("workspace/specs/plans.md", "# plans\n", base=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "WP-001: spec")
        ops.work_submit(a, "WP-001")
        run_checks(a, "WP-001")
        git(self.root, "merge", "-q", "--no-ff", "ai/wp-001-plans-api-contract", "-m", "merge")
        git(self.root, "push", "-q", "origin", "main")
        ops.work_complete(a, "WP-001")
        ops.work_claim(a, "WP-003")
        sync(self.alice)
        sync(self.bob)
        nxt = [x["work_id"] for x in ops.next_work(b)]
        self.assertEqual(nxt, ["WP-002"])                  # WP-003 is Alice's
        ops.work_claim(b, "WP-002")
        sync(self.bob)
        sync(self.alice)
        av = View(self.alice)
        self.assertEqual(av.state["WP-002"]["owner"], "S-bob")
        # Alice cannot write Bob's resources, even on another machine
        d, why = ops.check_write(a, os.path.join(self.root, "workspace", "backend", "pay.py"))
        self.assertEqual(d, "deny")
        # Bob hands off; Alice picks it up
        ops.handoff_create(b, "WP-002", completed="model", remaining="tests", next_action="write test_pay.py")
        sync(self.bob)
        sync(self.alice)
        self.assertIn("WP-002", [x["work_id"] for x in ops.next_work(a)])
        r = ops.work_claim(a, "WP-002")
        self.assertTrue(os.path.exists(self.alice.p("handoffs", r["handoff"] + ".md")))

    def test_offline_conflicting_claims_detected_on_sync(self):
        a = self.ctx(self.alice, "S-alice")
        sync(self.alice)
        sync(self.bob)
        b = self.ctx(self.bob, "S-bob")
        ops.claim_acquire(a, ["workspace/shared/config.json"])
        ops.claim_acquire(b, ["workspace/shared/**"])            # both offline: no conflict visible yet
        sync(self.alice)
        sync(self.bob)
        sync(self.alice)
        for st in (self.alice, self.bob):
            contested = View(st).contested
            self.assertEqual(len(contested), 1)
            self.assertEqual(contested[0]["winner"]["session"], "S-alice")  # deterministic: earlier claim wins
        d, why = ops.check_write(b, os.path.join(self.bob_root, "workspace", "shared", "config.json"))
        self.assertEqual(d, "deny")

    def test_goal_id_collision_is_reported_not_lost(self):
        sync(self.alice)
        sync(self.bob)
        ops.goal_create(ops.Ctx(self.alice, None, "alice", "HUMAN"), {"title": "Alice goal"})
        ops.goal_create(ops.Ctx(self.bob, None, "bob", "HUMAN"), {"title": "Bob goal"})
        sync(self.alice)
        rep = sync(self.bob)
        self.assertEqual(rep["conflicts"][0]["kind"], "goal-id-collision")
        parked = [f for f in os.listdir(self.bob.p("goals", "GOAL-001")) if "conflict" in f]
        self.assertEqual(len(parked), 1)

    def test_concurrent_push_retries(self):
        sync(self.alice)
        sync(self.bob)
        ops.emit(ops.Ctx(self.alice, "S-x", "a", "AI"), "NOTIFICATION", ["goal:G"], {"n": 1})
        ops.emit(ops.Ctx(self.bob, "S-y", "b", "AI"), "NOTIFICATION", ["goal:G"], {"n": 2})
        sync(self.alice)
        rep = sync(self.bob)     # remote moved since Bob's last fetch: merge, then fast-forward push
        self.assertTrue(rep["pushed"])
        sync(self.alice)
        notes = [e["data"]["n"] for e in self.alice.events() if e["type"] == "NOTIFICATION"]
        self.assertEqual(sorted(notes), [1, 2])


class InstallerTests(ProjectCase):
    def test_install_is_idempotent_and_non_destructive(self):
        from aiwos.installer import Installer
        # pre-existing user config survives a re-install
        sp = os.path.join(self.root, ".claude", "settings.json")
        doc = json.load(open(sp))
        doc["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]})
        doc["model"] = "opus"
        with open(sp, "w") as f:
            json.dump(doc, f)
        with open(os.path.join(self.root, "CLAUDE.md"), "a") as f:
            f.write("\n## My rules\nkeep me\n")
        skill = os.path.join(self.root, ".claude", "skills", "aiwos-goal", "SKILL.md")
        with open(skill, "a") as f:
            f.write("\nlocal tweak\n")
        log = Installer(self.root).install()
        self.assertTrue(any("KEEP .claude/skills/aiwos-goal/SKILL.md" in l for l in log))
        doc = json.load(open(sp))
        self.assertEqual(doc["model"], "opus")
        cmds = [h["command"] for g in doc["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("my-guard.sh", cmds)
        self.assertEqual(sum("aiwos" in c for c in cmds), 1)
        text = open(os.path.join(self.root, "CLAUDE.md")).read()
        self.assertEqual(text.count("<!-- aiwos:begin -->"), 1)
        self.assertIn("keep me", text)
        # uninstall keeps user data and user config
        ops.goal_create(self.human(), {"title": "keep my goal"})
        Installer(self.root).uninstall()
        doc = json.load(open(sp))
        self.assertNotIn("aiwos", json.dumps(doc))
        self.assertIn("my-guard.sh", json.dumps(doc))
        self.assertIn("keep me", open(os.path.join(self.root, "CLAUDE.md")).read())
        self.assertTrue(os.path.exists(skill))          # modified file kept
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude", "skills", "aiwos-plan")))
        self.assertTrue(os.path.exists(self.store.p("goals", "GOAL-001", "goal.json")))

    def test_launcher_runs_from_shell(self):
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh")
        r = subprocess.run([sh, os.path.join(self.root, ".ai", "bin", "aiwos"), "status"], cwd=self.root,
                           capture_output=True, text=True, env=GIT_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no goals yet", r.stdout)


class MigrationTests(ProjectCase):
    use_git = False

    def messy_project(self):
        self.write(".claude/agents/reviewer.md", "---\nname: reviewer\ndescription: Reviews code changes for quality, security and style issues\nmodel: sonnet\n---\nAlways run the tests before approving.\n")
        self.write(".claude/agents/code-checker.md", "---\nname: code-checker\ndescription: Reviews code changes for quality security and style issues\n---\nNever run the tests before approving.\nUse the reviewer agent for second opinions.\n")
        self.write(".claude/skills/deploy/SKILL.md", "---\nname: deploy\ndescription: Deploy to staging\ndisable-model-invocation: true\n---\nRun scripts/deploy.sh\n")
        self.write(".claude/commands/deploy.md", "---\ndescription: old deploy command\n---\nDeploy now\n")
        self.write("docs/CLAUDE.md", "@missing-file.md\n- Always use tabs for indentation in python files\n")
        self.write("AGENTS.md", "- Never use tabs for indentation in python files\n")
        sp = os.path.join(self.root, ".claude", "settings.json")
        doc = json.load(open(sp))
        doc["hooks"]["PostToolUse"].append({"matcher": "Edit", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/format.sh"}]})
        with open(sp, "w") as f:
            json.dump(doc, f)

    def test_scan_models_messy_project(self):
        from aiwos import adopt
        self.messy_project()
        m = adopt.scan(self.root)
        names = {a["name"] for a in m["agents"]}
        self.assertTrue({"reviewer", "code-checker", "aiwos-reviewer"} <= names)
        kinds = {p["kind"] for p in m["problems"]}
        self.assertIn("broken-hook-script", kinds)
        self.assertIn("broken-import", kinds)
        self.assertIn("possible-instruction-conflict", kinds)
        dup_kinds = {d["kind"] for d in m["duplicates"]}
        self.assertIn("same-name", dup_kinds)            # skill deploy vs command deploy
        self.assertIn("similar-purpose", dup_kinds)      # reviewer vs code-checker
        self.assertIn({"from": "agent:code-checker", "to": "agent:reviewer"}, m["references"])

    def test_capability_map_gate(self):
        from aiwos import adopt
        self.messy_project()
        r = self.cli("adopt", "scan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("adopt", "verify").returncode, 1)   # everything UNKNOWN
        path = os.path.join(self.root, "workspace", "migration", "capability-map.json")
        cmap = json.load(open(path))
        for c in cmap["capabilities"]:
            c["status"] = "PRESERVED"
        cmap["capabilities"][0]["status"] = "PARTIAL"
        cmap["capabilities"][0]["new_implementation"] = ".claude/agents/reviewer.md"
        with open(path, "w") as f:
            json.dump(cmap, f)
        problems = adopt.verify(self.root)
        self.assertTrue(any("PARTIAL" in p for p in problems))       # partial migration is not complete
        cmap["capabilities"][0]["status"] = "REPLACED"
        cmap["capabilities"][0]["new_implementation"] = ".claude/agents/does-not-exist.md"
        cmap["capabilities"][0]["validation"] = "compared outputs on 3 PRs"
        with open(path, "w") as f:
            json.dump(cmap, f)
        self.assertTrue(any("does not exist" in p for p in adopt.verify(self.root)))  # failed migration detected
        cmap["capabilities"][0]["new_implementation"] = ".claude/agents/aiwos-reviewer.md"
        with open(path, "w") as f:
            json.dump(cmap, f)
        self.assertEqual(adopt.verify(self.root), [])
        # re-scan keeps the mapping work
        self.cli("adopt", "scan")
        self.assertEqual(json.load(open(path))["capabilities"][0]["status"], "REPLACED")


if __name__ == "__main__":
    unittest.main()
