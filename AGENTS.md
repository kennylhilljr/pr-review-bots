# Agent guide

This repository holds `prbot`, the CLI behind Kenny's two GitHub review bots (`claude-pr-gh` and `codex-pr-gh`), plus the hooks and launchd job that make agents use them. Start with `README.md`; `DESIGN.md` explains why it works this way.

## Layout

- `prbot`: the whole CLI in one Python file, standard library only. Sections: gh-call classification, config and Keychain storage, GitHub API and App login, repo resolution, review routing, `watch`, then the commands.
- `bots/`: App name, description and logo path for each bot. Read by `prbot register`.
- `logos/`: bot logos, SVG sources and 512 px PNGs.
- `integrations/claude-code/guard_gh_identity.py`: PreToolUse hook that stops Claude Code from posting PR feedback as Kenny.
- `integrations/codex/`: the `gh` shim and the `codex-as-bot` launcher for Codex CLI.
- `integrations/launchd/install-watch.sh`: installs the job that runs `prbot watch` every 5 minutes.
- `skills/cross-review/`: the skill Claude Code and Codex use to review PRs and post through the bots. It is linked into `~/.claude/skills` and `~/.codex/skills`.
- `tests/test_prbot.py`: unit tests. Fixtures copy the shape of real PRs from `openclaw-ecc-orchestrator`.
- `docs/assets/`: the README header.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests make no network calls. To try a change against real repos without posting anything, use the read-only modes:

```bash
./prbot review 20 -R kennylhilljr/openclaw-ecc-orchestrator --route-only
```

```bash
./prbot watch --dry-run
```

## Conventions

- Keep `prbot` to the Python standard library plus the `gh` and `openssl` binaries. No pip packages.
- Add a regression test for every bug you fix, built from the real input that broke it.
- Update `README.md` and `DESIGN.md` in the same change when behavior or a command changes.
- Commit and push through your bot, not as Kenny: `./prbot git claude -- commit -m "..."` and `./prbot git claude -- push origin main` (Codex uses `codex`). This makes GitHub show `claude-pr-gh[bot]` or `codex-pr-gh[bot]` as author and pusher.
- Follow the writing rules in Kenny's global instructions for docs and commit messages: plain words, sentence-case headings, no em dashes.

## Safety

- Never print, log or commit a token or a private key. App keys live in macOS Keychain under service `prbot`; `*.pem` is ignored.
- Never post to GitHub while testing. Use `--route-only`, `--dry-run`, or the unit tests. Real posts happen through `prbot review`, `prbot gh` and the launchd job only.
- Changing App permissions, installing the launchd job, or anything that posts as a bot needs Kenny's go-ahead in the conversation.
