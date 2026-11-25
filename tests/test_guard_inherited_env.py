"""Issue #37: the guard checks the CDPATH and pager variables the agent's shell inherits.

Run: python3 -B -m unittest tests.test_guard_inherited_env"""
import importlib.util, json, os, re, shlex, subprocess, sys, tempfile, unittest
from unittest import mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


if "tests" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("tests", Path(__file__).resolve().parent / "__init__.py")
    _boot = importlib.util.module_from_spec(_spec)
    sys.modules["tests"] = _boot
    _spec.loader.exec_module(_boot)

prbot = load("prbot", ROOT / "prbot")

INHERITED = ("CDPATH", "GH_PAGER", "PAGER", "GIT_PAGER")
UNKNOWN = "cannot tell which repository git push runs in"


class InheritedEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mine = self.repo("kennylhilljr")
        self.theirs = self.repo("someone-else")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in INHERITED:
            os.environ.pop(k, None)

    def repo(self, owner, d=None):
        d = d or tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", f"https://github.com/{owner}/demo.git"], check=True)
        return d

    def guard(self, command, cwd=None):
        return prbot.guard_bash(command, cwd or self.mine, {"kennylhilljr"})

    def assertUnknown(self, out):
        self.assertIsNotNone(out)
        self.assertEqual(out["git"], [])
        self.assertTrue(any(UNKNOWN in r for r in out["reasons"]), out["reasons"])

    def test_cdpath_makes_bare_relative_cd_unknown(self):
        top = self.tmp.name
        mine_name, theirs_name = os.path.basename(self.mine), os.path.basename(self.theirs)
        alt = tempfile.mkdtemp(dir=top)
        self.repo("kennylhilljr", os.path.join(alt, theirs_name))  # where bash lands with CDPATH=alt

        self.assertIsNone(self.guard(f"cd {theirs_name} && git push", top))
        with mock.patch.dict(os.environ, {"CDPATH": alt}):
            self.assertUnknown(self.guard(f"cd {theirs_name} && git push", top))
            self.assertUnknown(self.guard(f"pushd {theirs_name} && git push", top))
            self.assertIsNone(self.guard(f"cd ./{theirs_name} && git push", top))
            self.assertIsNone(self.guard(f"cd {shlex.quote(self.theirs)} && git push", top))
            self.assertUnknown(self.guard(f"cd {mine_name} && git push", top))
            self.assertNotEqual(self.guard(f"cd ./{mine_name} && git push", top)["git"], [])
        with mock.patch.dict(os.environ, {"CDPATH": ""}):
            self.assertNotEqual(self.guard(f"cd {mine_name} && git push", top)["git"], [])

        sub = os.path.join(self.mine, "sub")
        os.mkdir(sub)
        with mock.patch.dict(os.environ, {"CDPATH": alt}):
            self.assertIsNotNone(self.guard("cd sub && ../scripts/check.sh && git status"))
            self.assertIsNone(self.guard("cd ./sub && ../scripts/check.sh && git status"))
            self.assertIsNone(self.guard("cd .. && scripts/check.sh && git status", sub))

    def test_inherited_pager_must_be_cat_or_empty(self):
        for name in ("GH_PAGER", "PAGER", "GIT_PAGER"):
            with mock.patch.dict(os.environ, {name: "less"}):
                for command in ("git status", "gh pr view 1"):
                    with self.subTest(name=name, command=command):
                        out = self.guard(command)
                        self.assertIsNotNone(out)
                        self.assertTrue(any(f"{name} is set in the environment" in r for r in out["reasons"]))
                        self.assertFalse(any("less" in r for r in out["reasons"]))
        with mock.patch.dict(os.environ, {"GH_PAGER": "cat", "PAGER": "", "GIT_PAGER": "cat"}):
            self.assertIsNone(self.guard("git status"))
            self.assertIsNone(self.guard("gh pr view 1"))
        with mock.patch.dict(os.environ, {"PAGER": "less"}):
            self.assertIsNone(self.guard("ls -la"))

    def run_hook(self, env):
        r = subprocess.run([sys.executable, str(ROOT / "integrations/claude-code/guard_gh_identity.py")],
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"},
                                             "cwd": self.theirs}),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_hook_refuses_inherited_pager(self):
        env = dict(os.environ, PRBOT_CONFIG_DIR=tempfile.mkdtemp(dir=self.tmp.name))
        for k in INHERITED:
            env.pop(k, None)
        self.assertEqual(self.run_hook(env).strip(), "")
        env["PAGER"] = "less"
        out = json.loads(self.run_hook(env))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("PAGER is set in the environment", out["permissionDecisionReason"])

    def test_docs_describe_inherited_environment(self):
        for doc in ("README.md", "DESIGN.md"):
            with self.subTest(doc=doc):
                text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
                self.assertIn("`CDPATH`", text)
                self.assertIn("set to anything other than `cat` or empty", text)


if __name__ == "__main__":
    unittest.main()
