# PR review bots: design

## Problem

On kennylhilljr/openclaw-ecc-orchestrator#15, every review instruction and every
piece of feedback that an agent wrote was posted as `kennylhilljr`. A reader of
the PR cannot tell which comments Kenny typed and which ones Claude or Codex
wrote for him. The only bot in the thread is `chatgpt-codex-connector[bot]`,
which belongs to OpenAI and only covers Codex Cloud's own reviews.

Goal: when Claude or Codex posts PR feedback on Kenny's behalf, GitHub shows a
bot that Kenny owns, with its own name and logo, and Kenny's personal token is
never used for those writes.

## Identities

Two GitHub Apps, owned by the `kennylhilljr` account and installed on the repos
where agents review code.

| | Claude bot | Codex bot |
|---|---|---|
| App name (default) | `claude-pr-gh` | `codex-pr-gh` |
| Shows on GitHub as | `claude-pr-gh[bot]` | `codex-pr-gh[bot]` |
| Logo | `logos/claude-reviewer.png` | `logos/codex-reviewer.png` |
| Used by | Claude Code sessions, Claude agents in the orchestrator | Codex CLI sessions, Codex agents in the orchestrator |

GitHub Apps were chosen over extra user accounts. GitHub's terms limit free
machine accounts, an App gets the `[bot]` badge so nobody mistakes it for a
person, and App tokens last one hour and can be narrowed to a single repository
per request.

App names are global on GitHub, so the defaults may already be taken. The names
live in `bots/*.json` and can be changed before registering.

## Permissions

Each App asks for the least it needs to review:

- Pull requests: read and write (reviews, review comments, PR comments)
- Issues: read and write (PR conversation comments use the issues API)
- Contents: read (diffs and file contents)
- Metadata: read (required by GitHub)

No webhooks and no events. The Apps never receive traffic; they only hold an
identity that local tools borrow.

## How a post becomes a bot post

```
agent (Claude Code / Codex CLI)
   |  gh pr review 15 --comment -b "..."
   v
guard  (Claude Code PreToolUse hook, or the gh shim for Codex)
   |  write to a PR?  yes -> run: prbot gh <bot> -- pr review ...
   v
prbot
   1. read App id and private key (macOS Keychain)
   2. sign a 9-minute JWT with openssl (RS256)
   3. look up the installation for owner/repo
   4. mint an installation token limited to that one repo
   5. exec gh with GH_TOKEN=<installation token>
   v
GitHub shows the comment as claude-pr-gh[bot]
```

The token lives only in the environment of the one `gh` process. It is never
written to disk or printed.

## Enforcement

Instructions in `CLAUDE.md` or `AGENTS.md` are easy for an agent to skip, so
each agent also gets a mechanical check.

- Claude Code: a PreToolUse hook on Bash (`integrations/claude-code/guard_gh_identity.py`).
  It denies any `gh` command that writes to a PR or issue unless it already
  goes through `prbot gh`, and the denial message gives Claude the exact
  command to run instead.
- Codex CLI: a `gh` shim placed first on `PATH` when Codex starts
  (`integrations/codex/bin/gh`, launched by `integrations/codex/codex-as-bot`).
  Write commands go through `prbot gh codex`; reads go straight to the real
  `gh`.

Both use the same classifier in `prbot`, so the rules cannot drift apart. Reads
such as `gh pr view`, `gh pr diff` and `gh api` GET calls stay on Kenny's login.

## Who reviews which PR

The default is cross-review: Codex reviews what Claude wrote, and Claude
reviews what Codex wrote. `prbot review <pr>` applies it.

Who wrote the PR, in order:

1. An `agent:claude` or `agent:codex` label.
2. Commit authors and `Co-Authored-By` trailers. Claude commits as
   `claude <noreply@anthropic.com>`, Codex as `codex <codex@openai.com>`. A
   human co-author does not change the answer.
3. Neither, or both: unclear.

Who reviews, in order:

1. `--reviewer claude|codex|both` on the command line.
2. A `review:claude` and/or `review:codex` label on the PR.
3. The other agent.

When the author is unclear and nothing names a reviewer, prbot posts nothing
and exits with code 3, telling the agent to ask Kenny. It never guesses, and it
never falls back to self-review when the other agent is unavailable (for
example, a usage limit); that run fails and says why.

The reviewing agent runs read-only: Claude with only Read, Grep and Glob and
no MCP servers, Codex with `-s read-only`. Each gets the diff on stdin and, if
prbot runs inside a checkout of the repo, can read files for context. The
reviewer returns text; prbot posts it as a PR review comment through that
agent's bot, with a line saying why that agent was chosen and which commit it
reviewed.

## Reviews as PRs come in

`prbot watch` runs every 5 minutes from a launchd job on Kenny's Mac rather
than in GitHub Actions. Running locally reuses the Claude and Codex logins
Kenny already pays for; Actions would need API keys for both and bill
separately. The cost is that reviews only happen while the Mac is awake.

Each pass looks at open, non-draft PRs on every repository the Apps are
installed on, and reviews each new head commit once with the agent the routing
rules pick. Whether a commit was already reviewed comes from the
"Reviewed commit" footer in the bots' own reviews, so a lost state file never
causes duplicate reviews. The state file only remembers PRs it skipped as
unclear (so they are logged once) and reviewers that failed (retried after an
hour). A pass runs at most 3 reviews, so a backlog of open PRs cannot start a
burst of model runs. `--baseline` marks the commits open at install time as
seen, for starting from now instead of reviewing old PRs.

## What stays on Kenny's account

- Merges, approvals that satisfy branch protection, and anything Kenny types
  himself.
- `@codex` trigger comments for Codex Cloud, until tested. The
  `chatgpt-codex-connector` may ignore comments written by other bots. Test
  once with `prbot gh claude -- pr comment <n> -b "@codex review"`. If the
  connector responds, set `"codex_triggers_as_bot": true` in
  `~/.config/prbot/config.json` and those triggers move to the bot too. Until
  then the guard lets a comment whose body starts with `@codex` through on
  Kenny's account.

## Key storage

| Item | Where | Secret |
|---|---|---|
| App id, slug, client id, owner | `~/.config/prbot/config.json` | no |
| App private key (PEM) | macOS Keychain, service `prbot`, account `<bot>`, base64 | yes |
| Installation token | environment of one `gh` process | yes, expires in 1 hour |

On hosts without the `security` command the key falls back to
`~/.config/prbot/<bot>.pem` with mode 600. To rotate a key, generate a new one
in the App settings, run `prbot import-key <bot> new.pem`, then delete the old
key on GitHub.

## Logos

Each logo is a small robot with a `</>` panel on its chest, and the two have
different builds so they are easy to tell apart at avatar size. The Claude bot
is round (pill head, oval body, single antenna) in clay. The Codex bot is boxy
(square head, visor eyes, twin antennae, tread feet) in blue. Both PNGs have a
transparent background, so they sit on GitHub's light and dark themes alike;
the Codex gradient (`#8A9AF8` to `#3A33F5`) was sampled from the official
`codex` account's GitHub avatar. Neither copies the Anthropic or OpenAI marks. Sources are SVG; the PNG renders are 512 px, which
fits GitHub's App logo upload (PNG, JPG or GIF, under 1 MB).

## Out of scope for this version

- A GitHub Actions path (claude-code-action or a Codex action running in CI
  with an App token from `actions/create-github-app-token`). The same Apps work
  there; it needs the private key stored as a repo or org secret.
- Rebranding `chatgpt-codex-connector[bot]`. That App belongs to OpenAI.
