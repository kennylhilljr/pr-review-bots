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
   |  PR or issue feedback?  yes -> run: prbot gh <bot> -- pr review ...
   |  (the hook also refuses any gh/git command off its safe list)
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
  It checks every Bash command against a safe list (below). Feedback writes
  and commits or pushes in the bots' repositories are denied with the exact
  `prbot gh` or `prbot git` command to run instead.
- Codex CLI: a `gh` shim placed first on `PATH` when Codex starts
  (`integrations/codex/bin/gh`, launched by `integrations/codex/codex-as-bot`).
  Feedback writes go through `prbot gh codex`; every other call goes straight
  to the real `gh`.

The shim sees gh's own argv, so it only has to tell feedback from everything
else. `prbot classify` counts these as feedback:

- `gh pr review`, `gh pr comment` and `gh issue comment`.
- `gh pr close` and `gh issue close` with `-c`/`--comment`, which post the
  comment before closing.
- `gh api` writes to review, comment and reply endpoints: `POST`, `PATCH`,
  `PUT` or `DELETE`, or fields with no method, in any of gh's flag forms
  (`-f body=x`, `-fbody=x`, `--field=body=x`, `-XPOST`), with or without a
  `?query` or `#fragment` after the path.
- `gh api graphql` with a review or comment mutation, inline or in a payload
  file. The classifier reads the files named by `query=@file` (in any
  `-f`, `-F`, `--field` or `--raw-field` form) or `--input file`, relative to
  the current directory, and checks JSON bodies string by string. It opens
  them without blocking and never reads stdin. A payload it can't read
  (missing, not a regular file, stdin, too large or not UTF-8) counts as a
  write, because its contents are unknown.

It finds the subcommand after `-R`, `--repo` and `--hostname`, whether they
come before the command group or between the group and the subcommand.

The hook sees a whole shell command, and a deny list over shell text kept
missing forms: four review rounds each found new ways to hide a write (a `#`
comment holding an apostrophe, `gh $(true) pr comment`, `env -C`, a `#`
fragment on an API path). So the hook works the other way round and only lets
through what it can read and knows is safe:

1. `shell_commands` in `prbot` is one hand-written lexer for the shell
   subset agents use. It tracks single and double quotes, backslashes, `#`
   comments, line continuations, `$(...)`, backticks, `<(...)` and `>(...)`,
   `${...}`, `$'...'`, here-documents and redirections in one pass, and
   returns every simple command, the ones inside substitutions included. A
   word built at run time is marked as such. Anything it doesn't fully
   understand (unbalanced quotes, `case`, functions, arrays, arithmetic
   commands, a here-document with a substitution in it) is unparseable.
2. Some shell forms can start gh without the text ever naming it, so they are
   refused in every command: a program name only known at run time (`$x`,
   `${x}h`, `$'\x67h'`, a glob or brace pattern), `eval` and `sh -c`/`bash -c`/
   `zsh -c` of text only known at run time, a shell reading commands from
   stdin (`base64 -d | sh`, `bash -s`, any `/dev/` or `/proc/` script path,
   `bash <(...)`), any of about twenty shells (csh and tcsh included), an
   interpreter such as `python3` reading its program from a pipe or running
   `-c` code only known at run time, a program that runs its arguments as a
   command (`find -exec`, `arch`, `ssh`, `watch`, `script`, `awk` and the like)
   given run-time or glob text, `xargs` whose command comes from stdin,
   `xargs -I`, option clusters such as `env -iS`, `env -S`, `env -C` and
   `sudo -D`. Text stored for bash to expand again is refused too: a value or
   `read`/`printf`/`declare` input holding `$(` or a backtick, a `$'...'` value
   with `\x`, octal or `\u` escapes, `PS4` and the other prompt and startup
   variables, `$[ ]`, `$(( ))` and `(( ))` with names, `${a[name]}`, `let`,
   `declare -i`, `[[ ... -eq ... ]]`, an array subscript holding `$(` or a
   backtick, and `${x@P}`, since arithmetic on a variable holding `a[$(cmd)]`
   runs cmd. A backslash inside `${...}` is unparseable. Literal `sh -c` and
   `eval` text is read like the outer command, with the outer command's rules.
3. A command whose text names gh, git or prbot in any letter case (after quotes,
   backslashes and line continuations are removed, so `g\h`, `'g'h` and `GH`
   count, as macOS finds the same binary) runs only when
   every simple command in it is on the safe list:
   - `gh` reads: `pr view/diff/list/checks/status`, `issue view/list/status`,
     `repo view`, `run view/list/watch`, `workflow list/view`,
     `release list/view`, `label list`, `auth status`, `search`, and
     `gh api` GET calls with one endpoint, no `-f`, `-F`, `--field`,
     `--raw-field` or `--input` and no method but GET, all with literal
     arguments.
   - `gh pr create`, `gh issue create`, `gh pr merge` and `gh pr edit`, which
     are not feedback, `gh pr checkout`, `gh pr ready`, `gh repo clone`, `gh pr close` and `gh issue close` without a comment,
     and `@codex` trigger comments.
   - `git` with only `-C`, `--no-pager` and similar global options, running a
     read (`status`, `log`, `diff`, `show`, `rev-parse`, `grep` and the like),
     a read-only `config`, or local work that makes no commit (`add`,
     `checkout`, `switch`, `restore`, `reset`, `stash`, `worktree`, `branch`,
     `tag`, `remote`, `fetch`, `clone`). Options that run a program or write a
     named file (`--upload-pack`, `--exec`, `rebase -x`, `grep -O`,
     `clone -u`, `--output` and the like) are refused, and so is any prefix of
     them, since git accepts `--upl` for `--upload-pack`.
   - `git commit`, `push`, `merge`, `pull`, `rebase`, `cherry-pick`, `revert`,
     `am`, `commit-tree` and `notes` outside the bots' repositories. In a repo
     the bots' owner owns they are denied with the `prbot git` command. The
     repo comes from `cd` (followed logically, as bash does; after `cd -P` or `set -P` the
     directory counts as unknown), `-C`, every
     remote URL of the repo and any branch remote or `remote.pushDefault` given
     as a URL; a push, pull or fetch also counts every word that looks like a
     remote URL. The owner is the URL's first path segment whatever the host (ssh
     aliases, IP addresses, ports), compared without letter case. They are refused
     when the directory is only known at run time (`cd $D`, `cd -`, `popd`,
     `pushd -n`, a `cd` after `&&`, `||` or in a pipe, background job or
     substitution, where it may not run in this shell), does not exist yet, or an earlier `git clone`, `init`,
     `worktree`, `remote` or `gh repo clone` in the same command may have changed the repo.
   - `prbot`, the route the guard points to.
   - `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg` (not `--pre`), `echo`,
     `printf` (not `-v`), `jq`, `sort` (not `-o`), `uniq` (no output file),
     `mkdir`, `set` options, `cd` and other programs that neither start a
     program nor write a file the guard has to protect, and the test runners `python3 -m unittest`, `pytest`,
     `scripts/check.sh`, `npm test`, and `make test` or `make check` with no
     other arguments.

   On top of that, such a command may not hold `if`/`for`/`while` blocks,
   subshells or `{ }` groups, wrappers (`env`, `sudo`, `nohup`, `xargs`,
   `time`), `VAR=value` prefixes other than pager and locale settings, or output
   redirection to a file (`2>&1`, `>&2` and `/dev/null` are fine). A
   double-quoted `$(...)` is allowed only as the value of a message option
   (`git commit -m "$(cat <<'EOF' ...)"`, `gh pr create --body "$(...)"`); a
   gh read or a gh or git argument known only at run time is refused. A plain
   assignment such as `SHA=$(git rev-parse HEAD)` is fine, except to `PATH`,
   `IFS`, `HOME` and `GH_`, `GIT_`, `LD_`, `DYLD_` or `BASH_` names.
4. Commands that never name gh, git or prbot and pass step 2 are not checked
   further: `npm install`, `python3 x.py > out` and `for f in *.py` loops run as
   before.

The denial lists each reason and gives the `prbot gh claude -- ...` or
`prbot git claude -- ...` command for any gh or git call in the command. The
safe list closes the shell-level gaps; the hook stops mistakes and is not a sandbox. A
program that builds the gh command itself (a script file, `python3 -c` with
string pieces, an executable copied under another name) is out of reach, as
before. `prbot classify` and the Codex shim keep the feedback classifier above,
because they see argv, not shell text, behind a gh safe list of their own
(`classify_gh`). Real gh expands aliases from its config.yml and runs
extensions under any name it does not own, so `gh alias set human-review 'pr
review'` followed by `gh human-review 19 --approve` would approve a PR as Kenny.
Parsing config.yml to follow aliases kept missing YAML forms (a quoted
`'aliases'` key, flow mappings, a redefined `co`), so classify never reads it.
It lets a call run as a read only when the first word after `-R`, `--repo` and
`--hostname` is a built-in gh command and, for a command group, the subcommand
is on that group's list. gh nests aliases only under groups, never under a
runnable command, and never lets one shadow a built-in name, so this covers
every alias whatever the config says. Built-in names such as `ext` for
`extension` count as the group they name; `co` is a config alias and is
refused. Everything else exits 4: unknown words (aliases, extensions, `gh gh`
after `--`), unlisted subcommands (`gh pr human-review`, `gh ext human-review`,
`gh extension exec`, `gh alias set` and `import`, `gh config set`, `gh auth
token`) and a flag before the command (`gh --help human-review`), since gh
hands the rest of the line to a shell alias unparsed.

What stays on Kenny's login: `gh pr create`, `gh issue create`, `gh pr merge`
and `gh pr edit`, and `@codex` trigger comments, including a close comment that
is a trigger (see "What stays on Kenny's account"). A `gh pr review` is
always feedback, whatever its body.

Both the Claude Code hook and the Codex gh shim fail closed for feedback writes
when the classifier cannot decide. The shim captures classify's stdout and exit
code and leaves its stderr visible. It routes through prbot only when the exit
code is 0 and stdout is write, and runs the real gh only when the exit code is 1
and stdout is read. Anything else, including a missing, non-executable or
crashing prbot, is refused with a non-zero exit. There is no bypass: the shim
classifies every call whatever the environment holds (`PRBOT_ACTIVE` included),
because prbot runs the real gh itself with the shim directory taken off PATH and
so never comes back through the shim. The hook's safe list
lives in prbot too. When prbot itself can't be loaded or decide()
raises, the hook falls back to a stricter check that needs no prbot code. It denies any command with a
gh, prbot or git token, and any command it cannot prove makes no such call.
Bash can spell gh without those letters in a row (`g\h`, `'g'h`, `${x}h`, a glob,
base64 piped to sh), so a command passes only when it has no `$`, backtick or
parentheses and every simple command in it starts with a program from a short
list that cannot start another program or write a file (ls, cat, grep, head,
wc and similar). It also has no output redirection except a descriptor copy or
close (`2>&1`, `>&2`, `2>&-`) or a write to `/dev/null`, because `echo > file`
could truncate the hook itself and leave the next write unguarded. `ls` still
works while the guard is broken; `sh`, `env`, `make`, scripts and `> file` do
not. The guard's denial reason names the error type and its message, never the
config contents.

Every other prbot command (review, whoami, gh, git and the rest) exits 2 with
one line on stderr when the config is bad, never a traceback. `load_config`
raises ConfigError for anything the guard can't trust: JSON the parser rejects
(deep nesting, and integers over 1000 digits on every Python version), invalid
UTF-8, a non-object, a dangling symlink for the file or for any directory above
it (a config directory that links to a missing folder may hide a real config), a `claude` or `codex` entry that is not an object or whose
`owner` is missing or not a non-empty string, and a `codex_triggers_as_bot`
that is not a boolean. Only a missing file in a real directory means `{}`. It lets an OSError from
reading the file (a directory, no permission, a symlink loop) through so the
guard can name it. `main` turns an OSError into the one line only when its
filename is the config path; any other OSError still raises. That line names
the config path and the error type, then a fixed reason ("the path is a
directory, not a file", "permission denied; check the file's mode and owner",
or "the file could not be read"), never the exception's own text. A queue run
(`watch --from-queue`) and the hourly pass check the config before the lock and
before anything moves: when the config is bad they print the same line on
stderr and exit 2, and a queue run logs it and leaves every file in the queue.
launchd retries at most once a minute until the config is fixed. If the config
breaks while a run is under way, the run prints the same line once and defers
the files it picked up, as for any other lookup or review failure, so they are
retried and never lost. The hook treats any exception from prbot, SystemExit
included, as a guard failure.

Agents editing this repository also get project hooks from
`.claude/settings.json`, which runs `.claude/hooks/check_edit.py` on
PreToolUse and PostToolUse for Write, Edit and MultiEdit. Before a write it
refuses `*.pem`, `*.key` and `.env*` files, because App keys live in Keychain.
After an edit to `prbot`, `integrations/` or `tests/` it runs the unit tests in
fresh temp folders and returns the failure output; a marker variable stops a
hook started inside that run from starting the suite again. After an edit to a
`*.md` file or anything in `skills/` it reports em and en dashes by line. These
hooks never post, push or call GitHub. The identity guard
(`guard_gh_identity.py`) is separate: it is installed globally from
`~/.claude/settings.json` and covers every repository.

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

Both agents get the same prompt. It works through the checks that the
`pr-review-toolkit` Claude Code plugin splits across separate agents: the
repo's own AGENTS.md and CLAUDE.md rules, behavior and edge cases, silent
failures, error messages, security, test quality, and comments that no longer
match the code. Findings must be at least 80% likely to be real. The checks
live in one prompt rather than in the plugin because Codex can't run Claude
Code plugins, and because one run keeps each review inside the timeout.

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
unclear (so they are logged once; their labels are still read on every run)
and reviewers that failed (retried after an hour). A pass runs at most 3 reviews, so a backlog of open PRs cannot start a
burst of model runs. `--baseline` marks the commits open at install time as
seen, for starting from now instead of reviewing old PRs.

Polling every 5 minutes means up to 5 minutes of delay and 288 full scans a
day. The replacement is event-driven: OpenClaw's GitHub event router drops one
JSON file per PR event into `~/.local/state/github-events/prbot/`, and a
launchd job with `QueueDirectories` runs `prbot watch --from-queue` on that
directory. A queue run makes the same per-PR decision as a full pass, through
the same function, for only the PRs named in the files. The full pass stays as
an hourly backstop for events that never arrive.

launchd keeps restarting a `QueueDirectories` job while the directory has
any entry in it, so a queue run leaves only the files of PRs held back by its
per-run limit. Each run reviews at least one PR, so those relaunches always
make progress and drain a burst about a minute apart. A PR's files are
deleted once nothing is left to do on it. When its review fails or has to
wait out the retry delay, the files move to a sibling `prbot-deferred/` with an `attempts`
count, and the hourly pass moves them back after it finishes; the pass has
already looked at those PRs, so the queue job's recheck finds the reviewed
commit and costs only GitHub reads. Entries that can never succeed (bad JSON,
other event kinds, repositories the bots don't cover, stray files such as
`.DS_Store`, and events deferred 24 times) move to `prbot-rejected/` and stay
there as evidence until someone clears it. The router publishes by renaming a
finished file into the queue, but a file that is not valid JSON still gets 30
seconds to settle before it is rejected, in case some other writer is
mid-write. The state file is written through a temp file and `os.replace`,
and a damaged one is moved aside with a timestamp rather than crashing every
run; single malformed entries are dropped with a log line each.

Because a queue run moves everything it can't use out of the directory, it
first checks that the directory really is an event queue: the default path,
or a directory whose parent holds the router's `.github-events-queue` marker
file. A symlinked or non-directory path is refused too, and so are `-deferred`
and `-rejected` destinations that are not real directories. Without that
check, `prbot watch --from-queue .` in a checkout would move `.git` and the
sources into a `-rejected` directory.

When the router sees GitHub activity it can't pin to one PR, it writes a
uniquely named sweep file naming a repository, or null for all of them. A
queue run handles the sweeps it listed at the start after the PR events, by
running the full pass's loop over those repositories. The loop reports each
kind of PR outcome separately, so a sweep stays in the queue whenever any PR
hit the limit; a review ran in that run, and each review either posts or
records a failure, so the next run starts further along.

The bots' installation lists decide which repositories are covered. An empty
or unreadable list for either bot makes the inventory incomplete. A
repository missing from an incomplete inventory may belong to the missing
bot, so its events are deferred, not rejected, and the full pass keeps all
state rather than pruning entries for repositories it never looked at. The two jobs can overlap, so every watch run
takes a non-blocking `flock` on `~/.config/prbot/watch.lock` and exits quietly
when another run holds it. The lock covers every check on the queue
directories and every move that follows, including the hourly pass moving
deferred files back, so no run acts on a check another run has made stale.

Each reviewer CLI runs in its own process group with a 900 second limit
(`PRBOT_REVIEW_TIMEOUT`). A hung Codex review once held the whole watch queue.
On timeout prbot kills the group, not only the CLI, because children left
holding the output pipe would keep prbot waiting. The timeout counts as a
failed review, so the commit is not marked reviewed and the pass moves on.

## Backup review in GitHub Actions

Running locally means no reviews while the Mac is asleep. A GitHub Actions
workflow covers that gap, but only for PRs prbot has left alone for 2 hours,
so it doesn't post a second review of every PR. It finds earlier reviews by the
same "Reviewed commit" line prbot uses.

It posts as `claude[bot]` through the Claude GitHub App and authenticates with
a Claude Code OAuth token from Kenny's subscription. Posting as
`claude-pr-gh[bot]` would mean storing that App's private key in Actions
secrets, which moves it off the Mac. Kenny chose the other identity instead.
The cost is that a backup review may be Claude reviewing Claude's own PR.

The reviewer gets the same prompt as prbot's reviewers, with Read, Grep and
Glob only, and returns the review as structured output. A separate step posts
it, so the model never holds a token that can write. The workflow file lives in
this repo and is copied into each repo by `install-backup-review.sh`, which
Kenny runs. The worker bots can't write `.github/workflows/`.

## Test isolation

prbot reads its config, watch state, event queue and OpenClaw event log from
paths in Kenny's home folder. Tests must never touch those. `tests/__init__.py`
sets `PRBOT_CONFIG_DIR`, `PRBOT_QUEUE_DIR` and `PRBOT_OPENCLAW_EVENTS` to a
per-run temp folder before any test module imports prbot, and deletes that
folder when the run ends. It overwrites values already in the environment. If
the system temp folder sits under `~/.config`, `~/.local/state` or
`~/.openclaw` (OpenClaw can set `TMPDIR` that way), it uses `/tmp` instead.

The test command is:

```bash
python3 -B -m unittest discover -s tests -t . -v
```

With the repo root as the top level, `tests` is a package and its `__init__.py`
runs before the test modules load. Without `-t .`, unittest imports `test_prbot`
as a top-level module and does not run `tests/__init__.py` itself, so
`tests/test_prbot.py` runs it before it imports prbot (when no `tests` module is
loaded yet). Either way the state paths are set up before prbot loads.

## Checks before a PR

`scripts/check.sh` runs everything a PR must pass in one command: the unit
tests, `py_compile` on the Python files, `bash -n` on the shell scripts, a
writing check for em and en dashes, a secret check for private keys and
literal GitHub tokens, and `plutil -lint` on the plists (a Python `plistlib`
parse where `plutil` is missing).
It runs every check even after one fails, so a single run lists all problems.
Each check can also run alone with `--check`, which is how
`tests/test_check.py` plants one problem at a time in a throwaway Git repo.

The script makes its own folder under `/tmp` and points `TMPDIR`,
`PRBOT_CONFIG_DIR` and `PYTHONPYCACHEPREFIX` into it, so a run never depends on
the caller's temp folder or real config. This matters because OpenClaw can set
`TMPDIR` under `~/.openclaw`. The folder is removed on exit and on signals.

The writing and secret checks scan files Git tracks, not everything on disk, so
a local `*.pem` or draft doesn't fail the check. The secret check reports the
file, line and kind of finding and never prints the match.

GitHub should run the same script on every PR. The worker bots can't write
`.github/workflows/`, so [`docs/ci.md`](docs/ci.md) holds the workflow for
Kenny to add, with the full list of checks and token thresholds.

## What stays on Kenny's account

- Merges, approvals that satisfy branch protection, and anything Kenny types
  himself.
- `@codex` trigger comments for Codex Cloud, until tested. The
  `chatgpt-codex-connector` may ignore comments written by other bots. Test
  once with `prbot gh claude -- pr comment <n> -b "@codex review"`. If the
  connector responds, set `"codex_triggers_as_bot": true` in
  `~/.config/prbot/config.json` and those triggers move to the bot too. Until
  then the guard lets a trigger through on Kenny's account: a plain
  `gh pr comment`, `gh issue comment` or close `-c` whose last body or
  comment starts with `@codex`, alone or followed by whitespace. A comment with any
  option besides `-b`/`--body`, `-R`/`--repo` and `--hostname` (such as
  `--edit-last`, `--body-file` or `--web`) is not a trigger. A `gh pr review`
  (an approval or change request above all), a body such as `@codexfoo` or
  `@codex:`, and every `gh api` call never get this exemption.

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
