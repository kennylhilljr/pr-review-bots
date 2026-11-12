"""Checks that docs/ci.md holds a workflow Kenny can copy as is, and that the docs point to it."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_DOC = ROOT / "docs" / "ci.md"


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def workflow_yaml():
    blocks = re.findall(r"^```yaml\n(.*?)^```$", read("docs/ci.md"), re.S | re.M)
    if len(blocks) != 1:
        raise AssertionError("docs/ci.md needs exactly one yaml block, found %d" % len(blocks))
    return blocks[0]


class CiWorkflowDocTest(unittest.TestCase):
    def setUp(self):
        self.yaml = workflow_yaml()
        self.lines = [line.rstrip() for line in self.yaml.splitlines()]

    def assert_line(self, line):
        self.assertIn(line, self.lines, "missing line %r in the workflow" % line)

    def test_triggers_on_pull_requests_and_pushes_to_main(self):
        self.assert_line("on:")
        self.assert_line("  pull_request:")
        self.assert_line("  push:")
        self.assert_line("    branches: [main]")

    def test_permissions_are_read_only(self):
        self.assert_line("permissions:")
        self.assert_line("  contents: read")
        self.assertNotRegex(self.yaml, r":\s*write\b")

    def test_runs_on_ubuntu_with_python_matrix(self):
        self.assert_line("    runs-on: ubuntu-latest")
        self.assert_line("        python-version: ['3.9', '3.13']")

    def test_minimum_python_matches_readme(self):
        readme = read("README.md")
        self.assertIn("| Python 3.9 or newer |", readme)
        self.assertIn("badge/Python-3.9%2B", readme)
        self.assertIn("'3.9'", self.yaml)

    def test_checkout_and_setup_steps(self):
        self.assert_line("      - uses: actions/checkout@v4")
        self.assert_line("          persist-credentials: false")
        self.assert_line("      - uses: actions/setup-python@v5")
        self.assert_line("          python-version: ${{ matrix.python-version }}")
        self.assertLess(self.yaml.index("actions/checkout@v4"), self.yaml.index("actions/setup-python@v5"))

    def test_last_step_runs_the_check_script(self):
        steps = [line for line in self.lines if line.strip()]
        self.assertEqual(steps[-1].strip(), "run: scripts/check.sh")

    def test_needs_no_secrets_or_app_token(self):
        lowered = self.yaml.lower()
        for needle in ("secrets.", "github_token", "github.token", "app-id", "private-key",
                       "create-github-app-token", "token:"):
            self.assertNotIn(needle, lowered)


class CiDocLinksTest(unittest.TestCase):
    def test_before_pr_command_in_agents_and_readme(self):
        for rel in ("AGENTS.md", "README.md"):
            with self.subTest(file=rel):
                text = read(rel)
                self.assertIn("scripts/check.sh", text)
                self.assertRegex(text, r"(?i)before (?:opening|you open) a PR")

    def test_readme_and_design_link_to_ci_doc(self):
        for rel in ("README.md", "DESIGN.md"):
            with self.subTest(file=rel):
                self.assertIn("](docs/ci.md)", read(rel))

    def test_ci_doc_says_kenny_adds_the_workflow(self):
        text = read("docs/ci.md")
        self.assertIn(".github/workflows/ci.yml", text)
        self.assertIn("Kenny adds it", text)

    def test_check_script_exists_and_is_executable(self):
        script = ROOT / "scripts" / "check.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
