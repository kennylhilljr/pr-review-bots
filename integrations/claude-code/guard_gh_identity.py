#!/usr/bin/env python3
"""Claude Code PreToolUse hook (matcher: Bash).

Denies gh commands that post PR feedback on the user's own account and tells
Claude the prbot command that posts the same thing as the Claude review bot.
Everything else passes through untouched.
"""
import importlib.util, json, os, re, shlex, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

PRBOT = Path(__file__).resolve().parents[2] / "prbot"
BOT = "claude"
TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:[^\s'\"]*/)?(gh|prbot|git)(?![A-Za-z0-9_])")
# Each word between git and commit/push is an option (starts with -) or a plain word (cannot start
# with -). The two alternatives never match the same text, so a failed search stays linear.
GIT_WRITE_FALLBACK = re.compile(
    r"(?<![A-Za-z0-9_])(?:[^\s'\"]*/)?git\s+(?:-\S+\s+|[A-Za-z0-9=./][A-Za-z0-9=./-]*\s+)*(commit|push)\b")
# While the guard is broken, a command passes only if every simple command in it starts with one of these
# programs, none of which can start another program (rg --pre and sort --compress-program can, and printf -v
# can set PATH, so they are out). Bash can spell gh without the letters in a row (g\h, 'g'h, ${x}h, a glob, base64 piped to sh), so a
# token check alone cannot prove a command unrelated.
SAFE_WHEN_BROKEN = frozenset("""
    ls pwd cd cat head tail wc echo grep egrep fgrep which stat file diff cmp uniq cut tr
    true false date whoami id uname basename dirname realpath du df tree
""".split())
# Command and process substitution, subshells, $'..' and ${..} can run or build any program, even inside an
# argument, and {NAME}>file assigns a file descriptor number to a variable such as PATH, so these characters
# are never allowed while the guard is broken.
RUNS_CODE = re.compile(r"[$`(){}]")
SEPARATOR = re.compile(r"\|\||&&|[;|\n]|(?<![<>])&(?!>)")


def load_prbot():
    loader = SourceFileLoader("prbot", str(PRBOT))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("prbot", loader))
    loader.exec_module(mod)
    return mod


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def is_mcp_feedback_write(tool):
    """GitHub MCP tools that post reviews or comments with the user's token (github server, Arcade, plugins)."""
    if not tool.startswith("mcp__") or "github" not in tool.lower():
        return False
    action = tool.rsplit("__", 1)[-1].lower().removeprefix("github_")
    return not re.match(r"(list|get|search|count|check)", action) and re.search(r"review|comment", action) is not None


def routing_fix(offending):
    fixes = "\n".join(f"  {shlex.quote(str(PRBOT))} gh {BOT} -- {shlex.join(a)}" for a in offending)
    return ("PR feedback must be posted by the Claude review bot, not the user's account. "
            f"Run it through prbot instead:\n{fixes}")


def bash_command(payload):
    ti = payload.get("tool_input")
    command = ti.get("command") if isinstance(ti, dict) else ""
    return command if isinstance(command, str) else ""


def decide(payload):
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return None
    if is_mcp_feedback_write(tool):
        return deny(f"{tool} would post PR feedback with the user's own GitHub token. Post it as the Claude "
                    f"review bot instead: {shlex.quote(str(PRBOT))} gh {BOT} -- pr review|pr comment|api ...")
    if tool != "Bash":
        return None
    try:
        command = bash_command(payload)
        prbot = load_prbot()
        cfg = prbot.load_config()
        owners = {cfg[b]["owner"] for b in prbot.AGENTS if b in cfg and cfg[b].get("owner")}
        cwd = payload.get("cwd") or os.getcwd()
        pushes = []
        for args, cdir in prbot.git_writes(command):
            repo = prbot.repo_from_git(os.path.join(cwd, cdir) if cdir else cwd)
            if repo and repo.split("/")[0] in owners:  # only repos the bots are installed on
                pushes.append(args)
        if pushes:
            fixes = "\n".join(f"  {shlex.quote(str(PRBOT))} git {BOT} -- {shlex.join(a)}" for a in pushes)
            return deny("Commits and pushes in Kenny's repos must come from the Claude bot (claude-pr-gh[bot]), "
                        f"not his account. Run them through prbot, which sets the bot identity and token:\n{fixes}")
        offending = prbot.unrouted_writes(command, cfg.get("codex_triggers_as_bot", False))
        if offending:
            return deny(routing_fix(offending))
        return None
    except Exception as e:
        return fail_closed(payload, e)


def provably_unrelated(command):
    """True only when every simple command starts with a SAFE_WHEN_BROKEN program and nothing can run code."""
    if RUNS_CODE.search(command):
        return False
    for segment in SEPARATOR.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words:
            continue
        # VAR=value before the program could change PATH, so the named program is not the one that runs
        if words[0] not in SAFE_WHEN_BROKEN:
            return False
    return True


def fail_closed(payload, e):
    """The guard could not decide. Deny gh, prbot and git calls and anything not provably unrelated; name
    the error only."""
    try:
        command = bash_command(payload)
    except Exception:
        command = ""
    tag = f"prbot guard failed ({type(e).__name__}: {e})"
    if type(e).__name__ == "ConfigError":
        suffix = ". Fix or remove ~/.config/prbot/config.json."
    else:
        suffix = ". Fix the guard, then re-run."
    try:
        offending = load_prbot().unrouted_writes(command, False)
    except Exception:
        offending = []
    if offending:
        return deny(f"{routing_fix(offending)}\n(guard note: {tag}{suffix})")
    if GIT_WRITE_FALLBACK.search(command):
        return deny(f"{tag}; refusing git commit/push candidate{suffix}")
    if TOKEN.search(command):
        return deny(f"{tag}; refusing gh/prbot/git call{suffix}")
    if not provably_unrelated(command):
        return deny(f"{tag}; refusing a command the broken guard cannot prove makes no gh/prbot/git call "
                    f"(only simple commands such as ls, cat or grep pass){suffix}")
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    out = decide(payload)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
