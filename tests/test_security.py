"""Regression tests for the v0.1 security review: shared state is untrusted input."""

import json
import os
import sys
import unittest

from helpers import ProjectCase, git, ops

from aiwos.installer import LEGACY_PERMISSIONS, PERMISSIONS, Installer
from aiwos.store import Store
from aiwos.sync import safe_rel, sync
from aiwos.util import AiwosError
from aiwos.validation import run_checks


class ValidationCommandTrust(ProjectCase):
    use_git = False

    def test_commands_from_shared_state_need_local_approval(self):
        gid = self.confirmed_goal()
        marker = os.path.join(self.tmp, "pwned")
        a = self.session("A")
        ops.work_add(self.human(), gid, [{"title": "w", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
                                          "risk": "low", "validation": [{"name": "ok", "run": "exit 0"}]}])
        # A teammate's (or attacker's) definition arrives through sync: written straight to state.
        wp = self.store.find_work("WP-001")
        wp["validation"] = [{"name": "evil", "run": '"%s" -c "open(%r, \'w\').close()"' % (sys.executable, marker)}]
        self.store.save_work(wp)
        ops.work_claim(a, "WP-001")
        ops.work_start(a, "WP-001")
        ops.work_submit(a, "WP-001")
        with self.assertRaisesRegex(AiwosError, "not written or approved on this machine"):
            run_checks(a, "WP-001")
        self.assertFalse(os.path.exists(marker))
        r = self.cli("validate", "WP-001", session="S-A")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--approve", r.stderr)
        self.assertFalse(os.path.exists(marker))
        # An agent (no interactive terminal) cannot approve, even with --approve.
        r = self.cli("validate", "WP-001", "--approve", session="S-A", stdin="yes\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn("interactive terminal", r.stderr)
        self.assertFalse(os.path.exists(marker))
        # An unrelated local edit must not approve the inherited, foreign command.
        ops.work_update(a, "WP-001", {"notes": "harmless edit"})
        ops.work_submit(a, "WP-001")
        with self.assertRaisesRegex(AiwosError, "not written or approved"):
            run_checks(a, "WP-001")
        self.assertFalse(os.path.exists(marker))
        # Approval is bound to the exact commands a person saw.
        with self.assertRaisesRegex(AiwosError, "not written or approved"):
            run_checks(a, "WP-001", approved=["some other command"])
        evil = self.store.find_work("WP-001")["validation"][0]["run"]
        run_checks(a, "WP-001", approved=[evil])       # what the CLI does after a person typed 'yes'
        self.assertTrue(os.path.exists(marker))

    def test_locally_authored_commands_run_without_extra_step(self):
        gid = self.confirmed_goal()
        a = self.session("A")
        ops.work_add(self.human(), gid, [{"title": "w", "purpose": "p", "success_criteria": ["c"], "kind": "doc",
                                          "risk": "low", "validation": [{"name": "ok", "run": "exit 0"}]}])
        ops.work_update(self.human(), "WP-001", {"validation": [{"name": "ok2", "run": "exit 0 "}]})
        ops.work_claim(a, "WP-001")
        ops.work_start(a, "WP-001")
        ops.work_submit(a, "WP-001")
        self.assertTrue(run_checks(a, "WP-001")["passed"])


class SyncPathSafety(ProjectCase):
    def test_safe_rel(self):
        for ok in ("goals/GOAL-001/goal.json", "events/s-abc.jsonl", "handoffs/H-WP-001-x.md"):
            self.assertTrue(safe_rel(ok), ok)
        for bad in ("../x.json", "goals/../../x.json", r"goals/..\..\x.json", "goals/C:x.json", "evil.json",
                    ".local/approved-commands.json", "goals/.hidden", "goals//x", "/etc/passwd",
                    "goals/" + "a" * 200, "bin/x"):
            self.assertFalse(safe_rel(bad), bad)

    def test_malicious_remote_tree_is_ignored(self):
        remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", remote)
        blob = git(remote, "hash-object", "-w", "--stdin", input_text='{"pwned": true}\n')
        good = git(remote, "hash-object", "-w", "--stdin", input_text='{"id": "S-x", "last_seen": "2026-01-01T00:00:00Z"}\n')
        # NUL-terminated: Windows text-mode pipes would otherwise add "\r" to every name
        inner = git(remote, "mktree", "-z", input_text="100644 blob %s\t..\\..\\..\\pwned.json\x00100644 blob %s\tS-x.json\x00" % (blob, good))
        top = git(remote, "mktree", "-z", input_text="100644 blob %s\tevil.json\x00040000 tree %s\tsessions\x00" % (blob, inner))
        commit = git(remote, "commit-tree", top, "-m", "malicious state")
        git(remote, "update-ref", "refs/heads/aiwos-state", commit)
        git(self.root, "remote", "add", "origin", remote)
        rep = sync(self.store, push=False, remote="origin")
        self.assertIn("evil.json", rep["rejected_paths"])
        self.assertTrue(any("pwned" in p for p in rep["rejected_paths"]))
        self.assertEqual(rep["merged_files"], ["sessions/S-x.json"])   # the legitimate file still merges
        for root, dirs, files in os.walk(self.tmp):
            self.assertNotIn("pwned.json", files)
            self.assertNotIn("evil.json", files)


class PermissionAllowlist(ProjectCase):
    use_git = False

    def test_allowlist_is_read_only_and_legacy_grants_removed(self):
        sp = os.path.join(self.root, ".claude", "settings.json")
        doc = json.load(open(sp))
        allow = doc["permissions"]["allow"]
        for risky in ("validate", "init", "uninstall", "sync", "acp", "work add", "work update", "hook"):
            self.assertFalse(any(risky in p for p in allow), risky)
        self.assertIn("Bash(aiwos status)", allow)
        doc["permissions"]["allow"] = allow + LEGACY_PERMISSIONS + ["Bash(npm test)"]
        with open(sp, "w") as f:
            json.dump(doc, f)
        Installer(self.root).install()                    # upgrade removes the old broad grants
        allow = json.load(open(sp))["permissions"]["allow"]
        self.assertFalse(set(LEGACY_PERMISSIONS) & set(allow))
        self.assertIn("Bash(npm test)", allow)
        self.assertEqual(sorted(set(allow) - {"Bash(npm test)"}), sorted(PERMISSIONS))


if __name__ == "__main__":
    unittest.main()
