"""Tests for the project Claude Code hook in .claude/hooks/check_edit.py.

Hook subprocesses run only against temporary copies of the repository layout
that hold a copy of the hook and a tiny fixture suite, so these tests never
start the real suite and cannot recurse.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "check_edit.py"
SETTINGS = REPO / ".claude" / "settings.json"
HOOK_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/check_edit.py"'
EM, EN = "—", "–"

PASSING_SUITE = '''
import json, os, unittest

class Fixture(unittest.TestCase):
    def test_ok(self):
        record = os.environ.get("FIXTURE_RECORD")
        if record:
            config = os.environ["PRBOT_CONFIG_DIR"]
            with open(record, "w") as f:
                json.dump({"tmpdir": os.environ["TMPDIR"], "config": config,
                           "config_entries": os.listdir(config),
                           "active": os.environ.get("PRBOT_EDIT_HOOK_ACTIVE")}, f)
'''

FAILING_SUITE = '''
import sys, unittest

class Fixture(unittest.TestCase):
    def test_fails(self):
        # stderr, like unittest's own report, so the order in the combined output is fixed
        print("FIXTURE-START", file=sys.stderr, flush=True)
        print("x" * 9000, file=sys.stderr, flush=True)
        print("FIXTURE-END", file=sys.stderr, flush=True)
        self.fail("fixture failure marker")
'''


def load_hook(path=HOOK):
    spec = importlib.util.spec_from_file_location("check_edit_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureRepo:
    """A temp repo with the hook copied in and a fixture test suite."""

    def __init__(self, suite=PASSING_SUITE):
        self.root = Path(tempfile.mkdtemp(prefix="hook-fixture-")).resolve()
        (self.root / ".claude" / "hooks").mkdir(parents=True)
        shutil.copy(HOOK, self.root / ".claude" / "hooks" / "check_edit.py")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_fixture.py").write_text(suite)
        (self.root / "prbot").write_text("# fixture\n")
        for d in ("integrations/claude-code", "skills/cross-review", "docs", "logos", "prbotx", "testsuite", "integrationsx"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run(self, event, tool, file_path, cwd=None, env_extra=None):
        payload = {
            "session_id": "test",
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": {"file_path": str(file_path), "content": "x"},
            "cwd": str(cwd or self.root),
        }
        env = dict(os.environ)
        env.pop("PRBOT_EDIT_HOOK_ACTIVE", None)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(self.root / ".claude" / "hooks" / "check_edit.py")],
            input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
        )

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class HookBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.repo = FixtureRepo()
        self.addCleanup(self.repo.cleanup)
        self.record = self.repo.root / "record.json"

    def post(self, rel, tool="Edit", cwd=None, **kw):
        extra = {"FIXTURE_RECORD": str(self.record)}
        return self.repo.run("PostToolUse", tool, rel, cwd=cwd, env_extra=extra, **kw)

    # Rule 1: key files
    def test_blocks_key_files_for_every_tool(self):
        cases = [(tool, "bot.pem") for tool in ("Write", "Edit", "MultiEdit")]
        cases += [("Write", p) for p in ("/abs/dir/app.key", ".env", ".env.local", "config/.envrc", "KEY.PEM",
                                         str(self.repo.root / "secrets" / "private.key"))]
        for tool, path in cases:
            with self.subTest(tool=tool, path=path):
                proc = self.repo.run("PreToolUse", tool, path)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("Keychain", proc.stderr)
                self.assertIn(path, proc.stderr)

    def test_allows_files_that_only_look_like_keys(self):
        for path in ("docs/env.md", "keys.md", "logos/claude-reviewer.svg", "key.pem.md",
                     "environment.py", "prbot", "my.env", "keyfile", "docs/.envoy/README"):
            with self.subTest(path=path):
                proc = self.repo.run("PreToolUse", "Write", path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")

    def test_pre_tool_use_does_not_read_key_contents(self):
        key = self.repo.write("real.pem", "-----BEGIN " + "PRIVATE KEY-----\nSECRETMATERIAL\n")
        proc = self.repo.run("PreToolUse", "Write", key)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("SECRETMATERIAL", proc.stderr + proc.stdout)

    def test_ignores_other_tools_and_events(self):
        for event, tool in (("PreToolUse", "Bash"), ("PreToolUse", "Read"), ("PostToolUse", "Read"),
                            ("Notification", "Write"), ("UserPromptSubmit", "Edit")):
            with self.subTest(event=event, tool=tool):
                proc = self.repo.run(event, tool, "bot.pem")
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stderr, "")

    def test_bad_or_empty_payloads_are_ignored(self):
        hook = str(self.repo.root / ".claude" / "hooks" / "check_edit.py")
        for data in ("", "not json", "[]", json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write"})):
            with self.subTest(data=data):
                proc = subprocess.run([sys.executable, hook], input=data, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0)

    # Rule 2: run the tests
    def test_code_edits_run_tests_quietly_on_success(self):
        cases = [(tool, "prbot") for tool in ("Write", "Edit", "MultiEdit")]
        cases += [("Edit", "integrations/claude-code/guard_gh_identity.py"), ("Write", "tests/test_prbot.py")]
        for tool, rel in cases:
            with self.subTest(tool=tool, rel=rel):
                self.record.unlink(missing_ok=True)
                proc = self.post(rel, tool=tool)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr + proc.stdout, "")
                self.assertTrue(self.record.exists(), "fixture suite did not run")

    def test_absolute_and_relative_paths(self):
        proc = self.post(str(self.repo.root / "prbot"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.record.exists())
        self.record.unlink()
        proc = self.post("../prbot", cwd=self.repo.root / "docs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.record.exists())
        self.record.unlink()
        proc = self.post("guard.py", cwd=self.repo.root / "integrations" / "claude-code")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.record.exists())

    def test_unrelated_edits_launch_no_tests(self):
        for rel in ("logos/claude-reviewer.svg", "bots/claude.json", "prbotx/a.py", "testsuite/a.py",
                    "integrationsx/a.py", "docs/notes.txt", "prbot.py", "/elsewhere/tests/test_x.py"):
            with self.subTest(rel=rel):
                proc = self.post(rel)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(self.record.exists(), f"{rel} started the tests")

    def test_fresh_empty_config_and_tmpdir_cleaned_up(self):
        proc = self.post("prbot")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(self.record.read_text())
        self.assertEqual(seen["config_entries"], [])
        self.assertNotEqual(seen["tmpdir"], seen["config"])
        self.assertNotEqual(seen["config"], os.environ.get("PRBOT_CONFIG_DIR"))
        self.assertEqual(seen["active"], "1")
        self.assertFalse(os.path.exists(seen["tmpdir"]))
        self.assertFalse(os.path.exists(seen["config"]))
        # Each run gets new folders.
        self.record.unlink()
        self.post("prbot")
        again = json.loads(self.record.read_text())
        self.assertNotEqual(again["tmpdir"], seen["tmpdir"])
        self.assertNotEqual(again["config"], seen["config"])

    def test_failing_suite_exits_2_with_bounded_output(self):
        self.repo.write("tests/test_fixture.py", FAILING_SUITE)
        proc = self.post("tests/test_fixture.py", tool="Write")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Unit tests failed", proc.stderr)
        self.assertIn("fixture failure marker", proc.stderr)
        self.assertNotIn("FIXTURE-START", proc.stderr)
        self.assertLess(len(proc.stderr), 4200)
        self.assertEqual(proc.stdout, "")

    def test_hook_inside_a_test_run_does_not_recurse(self):
        proc = self.repo.run("PostToolUse", "Edit", "prbot",
                             env_extra={"PRBOT_EDIT_HOOK_ACTIVE": "1", "FIXTURE_RECORD": str(self.record)})
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.record.exists())

    # Rule 3: writing check
    def test_clean_docs_pass(self):
        for rel in ("README.md", "docs/env.md", "skills/cross-review/SKILL.md", "skills/cross-review/notes.txt"):
            with self.subTest(rel=rel):
                path = self.repo.write(rel, "Plain words - with a hyphen.\n")
                proc = self.post(str(path))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
        self.assertFalse(self.record.exists())

    def test_dashes_in_docs_report_line_numbers(self):
        for rel in ("README.md", "docs/guide.MD", "skills/cross-review/SKILL.md", "skills/cross-review/prompt.txt"):
            for char, name in ((EM, "em dash"), (EN, "en dash")):
                with self.subTest(rel=rel, name=name):
                    self.repo.write(rel, f"ok\nbad {char} here\nok\nalso {char}\n")
                    proc = self.post(rel, tool="MultiEdit")
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn(rel, proc.stderr)
                    self.assertIn("2 (" + name, proc.stderr)
                    self.assertIn("4 (" + name, proc.stderr)
                    self.assertNotIn(" 1 (", proc.stderr)

    def test_dash_outside_docs_is_not_checked(self):
        self.repo.write("logos/notes.txt", f"a {EM} b\n")
        self.repo.write("../outside-hook-test.md", f"a {EM} b\n")
        try:
            for rel in ("logos/notes.txt", str(self.repo.root.parent / "outside-hook-test.md")):
                with self.subTest(rel=rel):
                    proc = self.post(rel)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            (self.repo.root.parent / "outside-hook-test.md").unlink(missing_ok=True)

    def test_markdown_under_tests_runs_both_checks(self):
        self.repo.write("tests/test_fixture.py", FAILING_SUITE)
        self.repo.write("tests/README.md", f"line {EN}\n")
        proc = self.post("tests/README.md")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Writing check", proc.stderr)
        self.assertIn("1 (en dash)", proc.stderr)
        self.assertIn("Unit tests failed", proc.stderr)

    def test_deleted_doc_is_not_an_error(self):
        proc = self.post("docs/gone.md")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # In-process checks with mocks
    def test_import_does_not_run_the_hook(self):
        with mock.patch("sys.stdin", io.StringIO('{"hook_event_name": "PreToolUse"}')), \
                mock.patch("subprocess.run") as run:
            module = load_hook(self.repo.root / ".claude" / "hooks" / "check_edit.py")
        run.assert_not_called()
        self.assertTrue(callable(module.main))

    def test_timeout_is_reported(self):
        module = load_hook(self.repo.root / ".claude" / "hooks" / "check_edit.py")
        self.assertLess(module.TEST_TIMEOUT, 60)
        err = io.StringIO()
        payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                              "tool_input": {"file_path": "prbot"}, "cwd": str(self.repo.root)})
        with mock.patch.object(module.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(module.TEST_COMMAND, module.TEST_TIMEOUT)) as run, \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr", err):
            os.environ.pop("PRBOT_EDIT_HOOK_ACTIVE", None)
            code = module.main(io.StringIO(payload))
        self.assertEqual(code, 2)
        self.assertIn("timed out", err.getvalue())
        self.assertEqual(run.call_args.kwargs["timeout"], module.TEST_TIMEOUT)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.repo.root))

    def test_launch_failure_is_reported(self):
        module = load_hook(self.repo.root / ".claude" / "hooks" / "check_edit.py")
        err = io.StringIO()
        payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Write",
                              "tool_input": {"file_path": str(self.repo.root / "tests" / "x.py")}})
        with mock.patch.object(module.subprocess, "run", side_effect=FileNotFoundError("python3")), \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr", err):
            os.environ.pop("PRBOT_EDIT_HOOK_ACTIVE", None)
            code = module.main(io.StringIO(payload))
        self.assertEqual(code, 2)
        self.assertIn("Could not start the unit tests", err.getvalue())


class HookConfigurationTests(unittest.TestCase):
    def test_settings_register_only_check_edit(self):
        self.assertTrue(SETTINGS.is_file(), f"{SETTINGS} is missing")
        settings = json.loads(SETTINGS.read_text())
        hooks = settings["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse", "PostToolUse"})
        for event in ("PreToolUse", "PostToolUse"):
            with self.subTest(event=event):
                entries = hooks[event]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["matcher"], "Write|Edit|MultiEdit")
                self.assertEqual(entries[0]["hooks"], [
                    {"type": "command", "command": HOOK_COMMAND, "timeout": 60},
                ])

    def test_hook_script_exists(self):
        self.assertTrue(HOOK.is_file())

    def check_ignore(self, rel):
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO,
                              capture_output=True).returncode

    def test_gitignore_tracks_only_the_hook_files(self):
        if shutil.which("git") is None:
            self.fail("git is required for this test")
        for rel in (".claude/settings.json", ".claude/hooks/check_edit.py"):
            with self.subTest(rel=rel):
                self.assertEqual(self.check_ignore(rel), 1, f"{rel} should be tracked")
        for rel in (".claude/.cc-writes/x", ".claude/settings.local.json", ".claude/hooks/local.py"):
            with self.subTest(rel=rel):
                self.assertEqual(self.check_ignore(rel), 0, f"{rel} should stay ignored")

    def test_docs_name_the_hook_files(self):
        agents = (REPO / "AGENTS.md").read_text()
        readme = (REPO / "README.md").read_text()
        design = (REPO / "DESIGN.md").read_text()
        layout = readme.split("## Repository layout", 1)[1].split("\n## ", 1)[0]
        enforcement = design.split("## Enforcement", 1)[1].split("\n## ", 1)[0]
        for name, text in (("AGENTS.md", agents), ("README.md layout", layout), ("DESIGN.md Enforcement", enforcement)):
            with self.subTest(doc=name):
                self.assertIn(".claude/hooks/check_edit.py", text)
                self.assertIn(".claude/settings.json", text)
        self.assertIn("guard_gh_identity.py", enforcement)


if __name__ == "__main__":
    unittest.main()
