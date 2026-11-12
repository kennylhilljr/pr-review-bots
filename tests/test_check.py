"""Tests for scripts/check.sh. Each test runs the script in a throwaway Git repo.

Fixture repos hold a copy of the script plus tiny generated files. They never
contain this file, so the fixture's own test run can't recurse into it.
Forbidden text (dashes, tokens, key markers) is built at runtime so this file
passes the scanner it tests.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check.sh"

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

PLIST = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>fixture</string>
    </dict>
    </plist>
    """)

PASSING_TEST = textwrap.dedent("""\
    import unittest

    class Fixture(unittest.TestCase):
        def test_passes(self):
            self.assertTrue(True)
    """)

# Records the environment the tests check runs under, then optionally sleeps.
ENV_REPORT_TEST = textwrap.dedent("""\
    import json, os, time, unittest

    class Env(unittest.TestCase):
        def test_report(self):
            config = os.environ["PRBOT_CONFIG_DIR"]
            report = {
                "TMPDIR": os.environ.get("TMPDIR"),
                "PRBOT_CONFIG_DIR": config,
                "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX"),
                "config_entries": os.listdir(config),
            }
            path = os.environ["CHECK_FIXTURE_REPORT"]
            with open(path + ".part", "w") as handle:
                json.dump(report, handle)
            os.rename(path + ".part", path)
            time.sleep(float(os.environ.get("CHECK_FIXTURE_SLEEP", "0")))
    """)


def token(prefix, length, alphabet="aB3"):
    """A fake token made at runtime: prefix plus `length` body characters."""
    return prefix + (alphabet * length)[:length]


class CheckScriptTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="check-test-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = self.base / "repo"
        self.make_fixture()

    def write(self, rel, content, add=True):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        if add:
            self.git("add", "--", rel)
        return path

    def git(self, *args):
        subprocess.run(
            ["git", "-c", "init.defaultBranch=main", *args],
            cwd=self.repo, check=True, capture_output=True,
        )

    def make_fixture(self):
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / "scripts" / "check.sh")
        self.git("init", "-q")
        self.write("README.md", "# Fixture\n\nPlain text - with a hyphen.\n")
        self.write("prbot", "#!/usr/bin/env python3\nprint('fixture')\n")
        self.write("integrations/claude-code/guard_gh_identity.py", "x = 1\n")
        for rel in ("integrations/codex/bin/gh", "integrations/codex/codex-as-bot",
                    "integrations/launchd/install-watch.sh"):
            self.write(rel, "#!/bin/bash\nif true; then echo ok; fi\n")
        for name in ("prbot-watch", "prbot-watch-queue"):
            self.write("integrations/launchd/com.kennylhilljr.%s.plist" % name, PLIST)
        self.write("tests/__init__.py", "")
        self.write("tests/test_fixture.py", PASSING_TEST)
        self.git("add", "scripts/check.sh")

    def env(self, **extra):
        env = dict(os.environ)
        env.pop("PYTHONPYCACHEPREFIX", None)
        env["GIT_CEILING_DIRECTORIES"] = str(self.base)
        env.update(extra)
        return env

    def run_check(self, *args, env=None, timeout=60):
        # Run from outside the repo to prove the script finds its own root.
        result = subprocess.run(
            [str(self.repo / "scripts" / "check.sh"), *args],
            cwd=str(self.base), env=env or self.env(),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr

    def assert_fails(self, check, needle=None):
        code, out = self.run_check("--check", check)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL: " + check, out)
        if needle:
            self.assertIn(needle, out)
        return out

    def assert_passes(self, *args):
        code, out = self.run_check(*args)
        self.assertEqual(code, 0, out)
        return out

    # Clean runs

    def test_clean_fixture_passes_every_check(self):
        out = self.assert_passes()
        for name in ("tests", "python", "shell", "writing", "secrets", "plists"):
            self.assertIn("ok: " + name, out)
        self.assertIn("check: all passed", out)

    # Writing check

    def test_em_dash_in_markdown_fails_writing(self):
        self.write("README.md", "# Fixture\n\nOne " + EM_DASH + " two\n")
        self.assert_fails("writing", "README.md:3: em dash")

    def test_en_dash_in_prbot_fails_writing(self):
        self.write("prbot", "#!/usr/bin/env python3\n# 1" + EN_DASH + "2\nprint(1)\n")
        self.assert_fails("writing", "prbot:2: en dash")

    def test_dash_outside_markdown_and_prbot_is_allowed(self):
        self.write("notes.txt", "a " + EM_DASH + " b\n")
        self.assert_passes("--check", "writing")

    def test_markdown_filename_with_spaces(self):
        self.write("docs/my notes.md", "a " + EM_DASH + " b\n")
        self.assert_fails("writing", "docs/my notes.md:1: em dash")

    def test_untracked_files_are_ignored(self):
        self.write("draft.md", "a " + EM_DASH + " b\n", add=False)
        self.write("secret.txt", token("ghp_", 40) + "\n", add=False)
        self.assert_passes("--check", "writing", "--check", "secrets")

    # Secrets check

    def test_each_token_family_fails_secrets_without_printing_it(self):
        cases = [
            (token("ghp_", 36), "GitHub token"),
            (token("ghs_", 36), "GitHub App installation token"),
            ("ghs_" + "12345_" + token("eyJ", 30) + "." + token("eyJ", 60) + "." + token("x-_", 43),
             "GitHub App installation token"),
            (token("gho_", 40), "GitHub token"),
            (token("ghp_", 36, "a_B3"), "GitHub token"),
            (token("github_pat_", 82, "aB3_"), "GitHub fine-grained token"),
        ]
        for value, kind in cases:
            with self.subTest(value=value[:12]):
                self.write("config.txt", "key = " + value + "\n")
                out = self.assert_fails("secrets", "config.txt:1: " + kind)
                self.assertNotIn(value, out)
                self.assertNotIn(value[-20:], out)

    def test_short_token_lookalikes_pass_secrets(self):
        self.write("config.txt", "\n".join([
            token("ghp_", 35), token("ghs_", 35), token("gho_", 35),
            token("github_pat_", 81, "aB3_"), "ghp_" + "-" * 40, "ghs_" + "1_a.b-" * 5,
        ]) + "\n")
        self.assert_passes("--check", "secrets")

    def test_private_key_markers_fail_secrets(self):
        for label in ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "):
            with self.subTest(label=label.strip() or "plain"):
                marker = "-" * 5 + "BEGIN " + label + "PRIVATE KEY" + "-" * 5
                self.write("key file.pem", marker + "\nMIIBfake\n")
                out = self.assert_fails("secrets", "key file.pem:1: private key block")
                self.assertNotIn("MIIBfake", out)
                self.assertNotIn(marker, out)

    def test_secret_in_binary_file_fails(self):
        self.write("blob.bin", b"\x00\xff" + token("ghp_", 36).encode() + b"\x00")
        self.assert_fails("secrets", "blob.bin:1: GitHub token")

    def test_staged_file_deleted_from_working_tree_is_still_scanned(self):
        path = self.write("config.txt", token("ghp_", 40) + "\n")
        path.unlink()
        self.assert_fails("secrets", "config.txt:1: GitHub token")

    def test_git_listing_error_fails_scans(self):
        shutil.rmtree(self.repo / ".git")
        self.assert_fails("secrets", "cannot list tracked files")
        self.assert_fails("writing", "cannot list tracked files")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_unreadable_tracked_file_fails_scan(self):
        path = self.write("locked.txt", "fine\n")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        self.assert_fails("secrets", "locked.txt: cannot read")

    # Tests, python and shell checks

    def test_failing_unit_test_fails_tests(self):
        self.write("tests/test_broken.py", PASSING_TEST.replace("assertTrue(True)", "assertTrue(False)"))
        self.assert_fails("tests")

    def test_python_syntax_error_fails_python(self):
        for rel in ("prbot", "integrations/claude-code/guard_gh_identity.py"):
            with self.subTest(file=rel):
                original = (self.repo / rel).read_text()
                self.write(rel, "def broken(:\n")
                self.assert_fails("python")
                self.write(rel, original)

    def test_shell_syntax_error_fails_shell_and_names_file(self):
        for rel in ("integrations/codex/bin/gh", "integrations/codex/codex-as-bot",
                    "integrations/launchd/install-watch.sh"):
            with self.subTest(file=rel):
                original = (self.repo / rel).read_text()
                self.write(rel, "#!/bin/bash\nif true; then echo\n")
                self.assert_fails("shell", "bash -n failed: " + rel)
                self.write(rel, original)

    # Plists check

    def stub_bin(self, plutil_script=None):
        """A bin folder with only the tools the plists check needs."""
        bindir = self.base / "bin"
        bindir.mkdir()
        for tool in ("dirname", "mktemp", "chmod", "mkdir", "rm"):
            found = shutil.which(tool)
            self.assertIsNotNone(found, tool)
            os.symlink(found, bindir / tool)
        if plutil_script is not None:
            stub = bindir / "plutil"
            stub.write_text(plutil_script)
            stub.chmod(0o755)
        return str(bindir)

    def test_plist_lint_failure_fails_plists(self):
        path = self.stub_bin("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/args\"\nexit 1\n")
        code, out = self.run_check("--check", "plists", env=self.env(PATH=path))
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL: plists", out)
        args = (self.base / "bin" / "args").read_text()
        self.assertIn("-lint", args)
        self.assertIn("prbot-watch.plist", args)
        self.assertIn("prbot-watch-queue.plist", args)

    def test_missing_plutil_skips_plists(self):
        path = self.stub_bin()
        code, out = self.run_check("--check", "plists", env=self.env(PATH=path))
        self.assertEqual(code, 0, out)
        self.assertIn("skip: plutil not found", out)

    # Arguments, full runs and isolation

    def test_bad_arguments_exit_2(self):
        for args in (["--check", "bogus"], ["--bogus"], ["--check"], ["tests"]):
            with self.subTest(args=args):
                code, out = self.run_check(*args)
                self.assertEqual(code, 2, out)
                self.assertIn("usage:", out)
                self.assertNotIn("== ", out)

    def test_full_run_continues_after_a_failure(self):
        self.write("README.md", "a " + EM_DASH + " b\n")
        self.write("prbot", "def broken(:\n")
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        for name in ("tests", "shell", "secrets", "plists"):
            self.assertIn("ok: " + name, out)
        self.assertIn("FAIL: python", out)
        self.assertIn("FAIL: writing", out)
        self.assertIn("check: failed: python writing", out)

    def inherited_env(self):
        inherited_tmp = self.base / "inherited-tmp"
        inherited_config = self.base / "inherited-config"
        inherited_tmp.mkdir()
        inherited_config.mkdir()
        (inherited_config / "config.json").write_text("{}")
        report = self.base / "report.json"
        self.write("tests/test_env.py", ENV_REPORT_TEST)
        env = self.env(TMPDIR=str(inherited_tmp), PRBOT_CONFIG_DIR=str(inherited_config),
                       PYTHONPYCACHEPREFIX=str(self.base / "inherited-pycache"),
                       CHECK_FIXTURE_REPORT=str(report))
        return env, report

    def assert_isolated(self, report):
        seen = json.loads(report.read_text())
        work = Path(seen["TMPDIR"]).parent
        self.assertEqual(str(work.parent), "/tmp")
        self.assertTrue(work.name.startswith("prbot-check."), seen)
        self.assertEqual(seen["PRBOT_CONFIG_DIR"], str(work / "config"))
        self.assertEqual(seen["PYTHONPYCACHEPREFIX"], str(work / "pycache"))
        self.assertEqual(seen["config_entries"], [])
        self.assertNotIn(str(self.base), json.dumps(seen))
        return work

    def test_inherited_temp_and_config_are_replaced_and_removed(self):
        env, report = self.inherited_env()
        code, out = self.run_check("--check", "tests", env=env)
        self.assertEqual(code, 0, out)
        work = self.assert_isolated(report)
        self.assertFalse(work.exists(), "work folder left behind: %s" % work)

    def test_work_folder_removed_on_terminate(self):
        env, report = self.inherited_env()
        env["CHECK_FIXTURE_SLEEP"] = "30"
        proc = subprocess.Popen(
            [str(self.repo / "scripts" / "check.sh"), "--check", "tests"],
            cwd=str(self.base), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 20
        while not report.exists():
            self.assertLess(time.monotonic(), deadline, "fixture test never started")
            time.sleep(0.05)
        work = self.assert_isolated(report)
        self.assertTrue(work.exists())
        os.killpg(proc.pid, signal.SIGTERM)
        self.assertNotEqual(proc.wait(timeout=20), 0)
        self.assertFalse(work.exists(), "work folder left behind: %s" % work)


if __name__ == "__main__":
    unittest.main()
