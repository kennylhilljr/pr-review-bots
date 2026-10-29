![pr-review-bots: claude-pr-gh and codex-pr-gh reviewing a pull request](docs/assets/readme-header.webp)

<p align="center">
  <img alt="Runtime dependencies: Python standard library" src="https://img.shields.io/badge/runtime%20dependencies-standard%20library-0F172A?style=flat-square">
  <img alt="Tests: unittest" src="https://img.shields.io/badge/tests-unittest-22C55E?style=flat-square">
  <img alt="Bots: claude-pr-gh and codex-pr-gh" src="https://img.shields.io/badge/bots-claude--pr--gh%20%C2%B7%20codex--pr--gh-5F72F6?style=flat-square">
</p>

# pr-review-bots

Two GitHub Apps that post PR feedback when Claude or Codex reviews code for
kennylhilljr, so those comments show up as `claude-pr-gh[bot]` or
`codex-pr-gh[bot]` instead of as Kenny. See [DESIGN.md](DESIGN.md) for
the reasoning.

| Bot | Logo |
|---|---|
| `claude-pr-gh` | ![claude](logos/claude-reviewer.png) |
| `codex-pr-gh` | ![codex](logos/codex-reviewer.png) |

## Layout

```
prbot                                  CLI: register Apps, mint tokens, run gh as a bot
bots/claude.json, bots/codex.json      App name, description, logo path
logos/*.svg, logos/*.png               logo sources and 512 px uploads
integrations/claude-code/guard_gh_identity.py   PreToolUse hook for Claude Code
integrations/codex/codex-as-bot        starts Codex with the gh shim on PATH
integrations/codex/bin/gh              gh shim that routes feedback writes to prbot
tests/test_prbot.py                    python3 -m unittest discover -s tests
```

Requirements: Python 3.9 or later, `openssl`, `gh`. No pip packages.

## One-time setup

Run these in your own terminal from this folder. `register` opens a local page
that sends the App manifest to GitHub; you confirm the App name on GitHub's
page and get sent back.

1. Create the two Apps. If GitHub says a name is taken, edit `name` in
   `bots/<bot>.json` and run the command again.

   ```bash
   ./prbot register claude
   ```

   ```bash
   ./prbot register codex
   ```

2. Install each App on the repos where agents review. Choose "Only select
   repositories" or "All repositories". The install page links are printed by:

   ```bash
   ./prbot install-url claude
   ```

   ```bash
   ./prbot install-url codex
   ```

3. Upload the logos. GitHub has no API for this. Open
   `https://github.com/settings/apps/<slug>`, scroll to Display information,
   and upload `logos/claude-reviewer.png` or `logos/codex-reviewer.png`.

4. Check that each App sees its installations:

   ```bash
   ./prbot whoami claude
   ```

5. Put `prbot` on your PATH, for example:

   ```bash
   ln -s "$PWD/prbot" ~/.local/bin/prbot
   ```

## Posting as a bot

Any `gh` command works; prbot swaps in a one-hour token scoped to that repo.

```bash
prbot gh claude -- pr review 15 -R kennylhilljr/openclaw-ecc-orchestrator --comment -b "Two P2 issues in plan_lint.py"
```

```bash
prbot gh codex -- pr comment 15 -R kennylhilljr/openclaw-ecc-orchestrator --body-file review.md
```

Without `-R`, prbot reads the `origin` remote of the current directory.

## Reviewing a PR

`prbot review` picks the reviewer (Codex for Claude's PRs, Claude for
Codex's), runs it read-only, and posts the result as that agent's bot. See
"Who reviews which PR" in [DESIGN.md](DESIGN.md) for the rules.

```bash
prbot review 18 -R kennylhilljr/openclaw-ecc-orchestrator
```

Check the routing without running a model or posting:

```bash
prbot review 18 -R kennylhilljr/openclaw-ecc-orchestrator --route-only
```

Other options: `--dry-run` runs the review and prints it instead of posting;
`--reviewer claude|codex|both` overrides the routing. A `review:claude` or
`review:codex` label on the PR does the same. When prbot can't tell who wrote a
PR it exits with code 3 and posts nothing; rerun with `--reviewer`.

Run it from a checkout of the repo when you can, so the reviewer can read files
beyond the diff.

## Claude Code: enforce it

Add this to the `hooks` section of `~/.claude/settings.json`, with the absolute
path to this folder:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "f=\"$HOME/Documents/GitHub/pr-review-bots/integrations/claude-code/guard_gh_identity.py\"; [ ! -f \"$f\" ] || python3 \"$f\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

After that, any `gh pr review`, `gh pr comment`, `gh issue comment`, or
`gh api` write to comments or reviews that Claude tries on Kenny's login is
denied, and the denial tells Claude the `prbot gh claude -- ...` command to run
instead. GitHub MCP tools that create reviews or comments (the `github` server,
Arcade's `Github_*` tools) are denied the same way. Reads are untouched. The
existence check keeps the hook from blocking every tool call if the folder
moves; it does nothing in that case, so keep the path current. Add one line to `~/.claude/CLAUDE.md` so Claude
starts with the right command rather than learning it from the denial:

```
When posting PR feedback, use `prbot gh claude -- <gh args>`; never post reviews or comments with plain `gh`.
```

## Codex CLI: enforce it

Start Codex through the launcher:

```bash
integrations/codex/codex-as-bot
```

It puts `integrations/codex/bin/gh` first on PATH, so feedback writes go
through `prbot gh codex` and everything else reaches the real `gh`. Add the
same instruction to `~/.codex/AGENTS.md` with `codex` as the bot name.

Codex runs its shell commands in its own sandbox. If that sandbox blocks the
macOS Keychain or network access to api.github.com, prbot fails and nothing is
posted. Allow both for prbot, or set `PRBOT_KEY_STORE=file` and use
`prbot import-key` to keep the key in `~/.config/prbot/codex.pem`.

## @codex triggers

Comments that start with `@codex` stay on Kenny's account, because
`chatgpt-codex-connector` may not respond to other bots. To test whether it
does:

```bash
prbot gh claude -- pr comment 15 -R kennylhilljr/openclaw-ecc-orchestrator -b "@codex review"
```

If Codex reacts, add `"codex_triggers_as_bot": true` to
`~/.config/prbot/config.json` and the guards will route those through the bot
as well.

## Key rotation

Generate a new private key on the App settings page, then:

```bash
./prbot import-key claude ~/Downloads/new-key.pem
```

Delete the old key on GitHub afterwards and remove the downloaded file.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the classifier, shell-command parsing, JWT signing (verified
against a throwaway RSA key), the Claude hook, and the Codex shim. They make no
network calls. The `register`, `whoami` and token-minting calls against GitHub
have not been run yet, because the Apps do not exist until step 1.
