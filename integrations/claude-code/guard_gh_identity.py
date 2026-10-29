#!/usr/bin/env python3
"""Claude Code PreToolUse hook (matcher: Bash).

Denies gh commands that post PR feedback on the user's own account and tells
Claude the prbot command that posts the same thing as the Claude review bot.
Everything else passes through untouched.
"""
import importlib.util, json, re, shlex, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

PRBOT = Path(__file__).resolve().parents[2] / "prbot"
BOT = "claude"


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


def decide(payload):
    tool = payload.get("tool_name", "")
    if is_mcp_feedback_write(tool):
        return deny(f"{tool} would post PR feedback with the user's own GitHub token. Post it as the Claude "
                    f"review bot instead: {shlex.quote(str(PRBOT))} gh {BOT} -- pr review|pr comment|api ...")
    if tool != "Bash":
        return None
    prbot = load_prbot()
    command = (payload.get("tool_input") or {}).get("command", "")
    offending = prbot.unrouted_writes(command, prbot.load_config().get("codex_triggers_as_bot", False))
    if not offending:
        return None
    fixes = "\n".join(f"  {shlex.quote(str(PRBOT))} gh {BOT} -- {shlex.join(a)}" for a in offending)
    return deny("PR feedback must be posted by the Claude review bot, not the user's account. "
                f"Run it through prbot instead:\n{fixes}")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    out = decide(payload)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
