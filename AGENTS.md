# Agent guide

This repository holds `prbot`, the CLI behind Kenny's two GitHub review bots (`claude-pr-gh` and `codex-pr-gh`), plus the hooks and launchd job that make agents use them. Start with `README.md`; `DESIGN.md` explains why it works this way.

## Layout

- `prbot`: the whole CLI in one Python file, standard library only. Sections: gh-call classification, config and Keychain storage, GitHub API and App login, repo resolution, review routing, `watch`, then the commands.
- `bots/`: App name, description and logo path for each bot. Read by `prbot register`.
- `logos/`: bot logos, SVG sources and 512 px PNGs.
- `integrations/claude-code/guard_gh_identity.py`: PreToolUse hook that stops Claude Code from posting PR feedback as Kenny.
- `integrations/codex/`: the `gh` shim and the `codex-as-bot` launcher for Codex CLI.
- `integrations/github-actions/`: `backup-review.yml`, a GitHub Actions workflow that reviews PRs prbot hasn't reviewed after 2 hours, posting as `claude[bot]`, and `install-backup-review.sh`, which Kenny runs to set the secret and open a PR adding it to each repo.
- `integrations/launchd/`: `install-watch.sh` installs the job that runs `prbot watch` every 5 minutes; the two plists and `README.md` there are the event queue job and hourly backstop that replace it.
- `skills/cross-review/`: the skill Claude Code and Codex use to review PRs and post through the bots. It is linked into `~/.claude/skills` and `~/.codex/skills`.
- `.claude/settings.json` and `.claude/hooks/check_edit.py`: project Claude Code hooks. They refuse writes to `*.pem`, `*.key` and `.env*` files, run the unit tests after an edit to `prbot`, `integrations/` or `tests/`, and flag em and en dashes after an edit to a `*.md` file or `skills/`. Tested in `tests/test_claude_hooks.py`.
- `tests/test_prbot.py`: unit tests. Fixtures copy the shape of real PRs from `openclaw-ecc-orchestrator`.
- `scripts/check.sh`: the before-PR check; `tests/test_check.py` tests it and `tests/test_ci_docs.py` tests `docs/ci.md`.
- `docs/ci.md`: what the check script does and the CI workflow Kenny adds.
- `docs/assets/`: the README header.

## Tests

```bash
python3 -B -m unittest discover -s tests -t . -v
```

Before opening a PR, run every check with `scripts/check.sh`. It runs the tests in an isolated temp folder, compiles the Python files, runs `bash -n` on the shell scripts, rejects em and en dashes in tracked Markdown and `prbot`, and rejects private keys and literal GitHub tokens in tracked files. `docs/ci.md` lists the checks and the `--check` flags.

```bash
scripts/check.sh
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

## Review rules

Review bots check every PR against these rules, as well as Conventions and Safety above. Each rule names the code it covers. A change that breaks one fixes the code or updates the rule in the same PR.

**Identity**
- `is_feedback_write` decides whether a `gh` argv posts PR feedback. The Claude Code hook (`decide` in `guard_gh_identity.py`) reaches it through `guard_bash`, which reads the command with `shell_commands` and refuses any command naming `gh`, `git` or `prbot` that is off its safe list. The Codex shim (`integrations/codex/bin/gh`) reaches it through `prbot classify` and refuses to run `gh` when classify gives no trusted answer. A new kind of feedback write adds a case to `Classify` and a `Guard`, `Hook` or `CodexShim` test.
- When the hook's check raises, `fail_closed` refuses feedback writes found by `unrouted_writes` (which splits commands with the older `_commands` scanner), then any `gh`, `git` or `prbot` call and any command it cannot prove unrelated. Only tests call `git_writes`.
- `_Guard.git_call` refuses a `GIT_IDENTITY` command (commit, push, merge, rebase and others) run in a repo with a remote owned by an account in the prbot config, or when it cannot tell the directory or an earlier step in the same command clones, inits or changes the repo. The hook refuses GitHub MCP tools that `is_mcp_feedback_write` matches. A change to either adds a `Guard` or `Hook` test.
- A `pr review`, `pr comment`, `issue comment` or close `-c` whose last body or comment value starts with `@codex` is not a feedback write unless `codex_triggers_as_bot` is set. `gh_read` runs the real `gh` with the caller's own login. Only `cmd_gh`, `cmd_git` and the posting step of `cmd_review` run `gh` or `git` with a bot token; `installed_repos` asks for a `metadata: read` token per installation for prbot's own API calls.

**Tokens**
- `cmd_gh` and `cmd_review` pass the installation token to `gh` only as `GH_TOKEN`, and `cmd_git` to `git` only as `PRBOT_GIT_TOKEN`, read by the credential helper in `git_env`. None of them puts it in argv, a git config file, a file on disk, a log line or an OpenClaw event.
- All three remove `GITHUB_TOKEN` from that environment.
- `installation_token` asks for one repository. `cmd_git` asks for a token only for push, fetch and pull, and only for `contents: write`.
- `manifest` sets no active webhook and no events. Its permissions are `PERMISSIONS` unless the bot spec sets its own, plus `metadata: read`.

**Watch and the event queue**
- Config, watch state and rewritten event files are written with `write_json_atomic`.
- In `cmd_watch_queue`, a PR event is deleted when its PR is done, left in the queue when it is over the run's limit, and deferred when the repository lookup or the review failed or has to wait, or when its repo is missing from an incomplete bot inventory. It is rejected only when it is malformed or not an event file, names a repo missing from a complete bot inventory, or its deferral count reaches `MAX_ATTEMPTS`.
- Once it has the lock, `cmd_watch` holds `watch_lock` for the rest of the run: across `cmd_watch_queue` for a queue run, and across the full pass and `requeue_deferred` otherwise.

**Tests**
- Tests never post to GitHub, call the network, use Keychain, or read or write the real config, state or OpenClaw folders, except that one `OpenClaw` test imports the real OpenClaw runtime writer when it is installed. `tests/__init__.py` points `PRBOT_CONFIG_DIR`, `PRBOT_QUEUE_DIR` and `PRBOT_OPENCLAW_EVENTS` at a temp folder for the whole run; tests that need their own paths patch them and put fake `gh`, `claude` or `codex` on `PATH`.
