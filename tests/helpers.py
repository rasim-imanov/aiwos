import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMEWORK = os.path.join(ROOT, "framework")
sys.path.insert(0, FRAMEWORK)

from aiwos import ops  # noqa: E402
from aiwos.installer import Installer  # noqa: E402
from aiwos.store import Store  # noqa: E402

# Every git process in the tests (including the framework's own) sees the same, machine-independent config.
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
                  GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf",
                  GIT_CONFIG_VALUE_0=os.environ.get("AIWOS_TEST_AUTOCRLF", "false"))
GIT_ENV = dict(os.environ)
GIT_ENV.pop("AIWOS_SESSION", None)
GIT_ENV.pop("CLAUDECODE", None)
os.environ.pop("AIWOS_SESSION", None)
os.environ.pop("CLAUDECODE", None)
os.environ["AIWOS_ACTOR"] = "tester"


def git(cwd, *args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, env=GIT_ENV)
    if check and r.returncode != 0:
        raise AssertionError("git %s: %s" % (args, r.stderr))
    return r.stdout.strip()


class ProjectCase(unittest.TestCase):
    use_git = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiwos-test-")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        if self.use_git:
            git(self.root, "init", "-q", "-b", "main")
            self.write("README.md", "# test\n")
            git(self.root, "add", ".")
            git(self.root, "commit", "-qm", "init")
        Installer(self.root).install()
        if self.use_git:
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "install aiwos")
        self.store = Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def write(self, rel, text, base=None):
        p = os.path.join(base or self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def session(self, name, human=False):
        ops.session_start(self.store, sid="S-" + name, actor="actor-" + name, actor_type="HUMAN" if human else "AI")
        return ops.Ctx(self.store, "S-" + name, "actor-" + name, "HUMAN" if human else "AI")

    def human(self):
        return ops.Ctx(self.store, None, "alice", "HUMAN")

    def confirmed_goal(self, title="Subscription platform", criteria=("works",)):
        g = ops.goal_create(self.human(), {"title": title, "outcome": "x", "success_criteria": list(criteria)})
        ops.goal_confirm(self.human(), g["id"], "alice")
        return g["id"]

    def cli(self, *args, session=None, stdin=None, cwd=None, extra_env=None):
        env = dict(GIT_ENV)
        env["AIWOS_ACTOR"] = "tester"
        if session:
            env["AIWOS_SESSION"] = session
        env.update(extra_env or {})
        r = subprocess.run([sys.executable, os.path.join(self.root, ".ai", "runtime", "aiwos_main.py")] + list(args),
                           cwd=cwd or self.root, capture_output=True, text=True, input=stdin, env=env,
                           encoding="utf-8")
        return r

    def hook(self, name, payload, cwd=None):
        r = self.cli("hook", name, stdin=json.dumps(payload), cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def expire(self, sid):
        s = self.store.load_session(sid)
        s["last_seen"] = "2000-01-01T00:00:00.000Z"
        self.store.save_session(s)
