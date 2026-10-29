---
name: cross-review
description: Review a pull request in Kenny's (kennylhilljr) GitHub repos and post feedback as the right bot. Use when asked to review a PR, reply to review comments, post any comment on a PR or issue, or open a PR of your own. Codex reviews what Claude wrote and Claude reviews what Codex wrote; feedback is posted as claude-pr-gh[bot] or codex-pr-gh[bot], never under Kenny's account.
---

# Cross-review

Kenny's repos use two GitHub Apps for agent feedback: `claude-pr-gh` and `codex-pr-gh`. Anything an agent posts on a PR goes through `prbot`, which signs in as the right App. Posting with plain `gh` or a GitHub MCP tool would put the words under Kenny's name, and the guard hook will refuse it.

Know which agent you are. If you are Claude, your bot is `claude`. If you are Codex, your bot is `codex`.

## Review a PR

1. Check who should review it. This reads the PR and posts nothing:

   ```bash
   prbot review <number> -R <owner/repo> --route-only
   ```

   It prints who wrote the PR and who reviews it: the other agent, unless a `review:claude` or `review:codex` label or the user says otherwise.

2. If it exits with code 3, it could not tell who wrote the PR. Ask the user which agent should review it. Do not guess, and do not default to yourself.

3. Run the review. prbot runs the chosen agent read-only and posts the result as that agent's bot:

   ```bash
   prbot review <number> -R <owner/repo>
   ```

   Add `--reviewer claude`, `--reviewer codex` or `--reviewer both` only when the user asked for a specific reviewer or answered step 2. Use `--dry-run` to show the user the review before posting it.

4. Report the link prbot prints. If the reviewer fails, for example on a usage limit, say so. Do not substitute a self-review unless the user asks for one.

Run from a checkout of the repository when one exists, so the reviewer can read files beyond the diff.

## Post anything else on a PR

Replies to review threads, follow-up comments and status notes go through your own bot. Put prbot's options before `--` and the `gh` command after it:

```bash
prbot gh <your-bot> -R <owner/repo> -- pr comment <number> --body-file reply.md
```

```bash
prbot gh <your-bot> -R <owner/repo> -- pr review <number> --comment --body-file review.md
```

Reading is fine with plain `gh`: `gh pr view`, `gh pr diff`, `gh api` GET calls.

Exceptions that stay on Kenny's account:

- Comments that start with `@codex` (they trigger Codex Cloud, which may ignore other bots).
- Approvals, merges and anything Kenny asks to post as himself.

## Open a PR of your own

So the right agent reviews it, and so GitHub credits the work to you:

- Commit and push through your bot. prbot sets the bot as author and committer, so GitHub shows `claude-pr-gh[bot]` or `codex-pr-gh[bot]` with its logo, and pushes with the bot's token:

  ```bash
  prbot git <your-bot> -- commit -m "..."
  ```

  ```bash
  prbot git <your-bot> -- push origin <branch>
  ```

  Never commit with Kenny's git identity, and not as `Claude <noreply@anthropic.com>` either: that email belongs to Anthropic's own `claude` account, not the bot.

- Add the label `agent:claude` or `agent:codex` to the PR.

New commits on open, non-draft PRs are picked up by `prbot watch` every 5 minutes, so you do not need to request a review for them. Run `prbot review` yourself only when the user wants one now.

## Rules

- Never post PR feedback with plain `gh`, `gh api` writes, or GitHub MCP tools.
- Never commit or push in Kenny's repos with plain `git`; use `prbot git <your-bot> -- commit` and `-- push`.
- Never print, log or save a token. prbot handles tokens.
- Never approve or merge on Kenny's behalf unless he asks in this conversation.
- The rules and their reasons are in `DESIGN.md` in the `pr-review-bots` repo.
