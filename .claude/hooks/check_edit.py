#!/usr/bin/env python3
"""Project Claude Code hook for edits in this repository.

Claude Code sends the tool call as JSON on stdin. Exit 2 blocks a PreToolUse
call, and on PostToolUse it sends stderr back to the agent.

- PreToolUse: refuse to write key files (*.pem, *.key, .env*).
- PostToolUse: run the unit tests after an edit to prbot, integrations/ or
  tests/, and flag em and en dashes after an edit to a *.md file or skills/.

Standard library only. Nothing here posts, pushes or calls GitHub.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = {"Write", "Edit", "MultiEdit"}
TEST_TIMEOUT = 50  # shorter than the 60 second timeout in .claude/settings.json
OUTPUT_LIMIT = 4000
DASHES = {"—": "em dash", "–": "en dash"}
# Set in the test run's environment so a hook started from inside it does nothing.
ACTIVE_ENV = "PRBOT_EDIT_HOOK_ACTIVE"
TEST_COMMAND = ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."]


def is_key_file(path):
    name = os.path.basename(path)
    lower = name.lower()
    return lower.endswith(".pem") or lower.endswith(".key") or name.startswith(".env")


def resolve(path, cwd):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    return Path(os.path.normpath(p))


def repo_relative(path):
    """Return the path relative to the repository root as a POSIX string, or None."""
    for root in {REPO_ROOT, REPO_ROOT.resolve()}:
        for candidate in (path, path.resolve() if path.exists() else path):
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                continue
    return None


def needs_tests(rel):
    if rel is None:
        return False
    return rel == "prbot" or rel.startswith("integrations/") or rel.startswith("tests/")


def needs_writing_check(rel):
    if rel is None:
        return False
    return rel.lower().endswith(".md") or rel.startswith("skills/")


def run_tests(env=None):
    """Run the suite in fresh temp folders. Return an error message, or None on success."""
    tmpdir = tempfile.mkdtemp(prefix="prbot-hook-tmp-")
    configdir = tempfile.mkdtemp(prefix="prbot-hook-config-")
    child = dict(os.environ if env is None else env)
    child["TMPDIR"] = tmpdir
    child["PRBOT_CONFIG_DIR"] = configdir
    child[ACTIVE_ENV] = "1"
    try:
        try:
            proc = subprocess.run(
                TEST_COMMAND, cwd=str(REPO_ROOT), env=child,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=TEST_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"Unit tests timed out after {TEST_TIMEOUT} seconds: {' '.join(TEST_COMMAND)}"
        except OSError as exc:
            return f"Could not start the unit tests ({' '.join(TEST_COMMAND)}): {exc}"
        if proc.returncode != 0:
            return "Unit tests failed after this edit:\n" + (proc.stdout or "")[-OUTPUT_LIMIT:]
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(configdir, ignore_errors=True)


def dash_lines(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        kinds = [name for char, name in DASHES.items() if char in line]
        if kinds:
            found.append((number, kinds))
    return found


def main(stdin=None):
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    if payload.get("tool_name") not in TOOLS:
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return 0

    if event == "PreToolUse":
        if is_key_file(file_path):
            print(
                f"Blocked: {file_path} looks like a key or secrets file (.pem, .key or .env). "
                "App keys live in macOS Keychain under service prbot, never in files in this "
                "repository. See AGENTS.md, Safety.",
                file=sys.stderr,
            )
            return 2
        return 0

    if event != "PostToolUse":
        return 0

    path = resolve(file_path, payload.get("cwd"))
    rel = repo_relative(path)
    problems = []
    if needs_writing_check(rel):
        hits = dash_lines(path)
        if hits:
            lines = ", ".join(f"{n} ({' and '.join(k)})" for n, k in hits)
            problems.append(
                f"Writing check: {rel} contains em or en dashes on line(s) {lines}. "
                "Use plain punctuation instead."
            )
    if needs_tests(rel) and not os.environ.get(ACTIVE_ENV):
        error = run_tests()
        if error:
            problems.append(error)
    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
