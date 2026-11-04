# launchd jobs for prbot watch

Two jobs review PRs on Kenny's Mac:

| Label | Runs | When |
| --- | --- | --- |
| `com.kennylhilljr.prbot-watch-queue` | `prbot watch --from-queue ~/.local/state/github-events/prbot` | Whenever the OpenClaw GitHub event router drops a file in that directory, at most once a minute (`ThrottleInterval` 60) |
| `com.kennylhilljr.prbot-watch` | `prbot watch` | Every hour (`StartInterval` 3600), as a backstop for missed events |

Both write to `~/Library/Logs/prbot-watch.log`. They share a lock file next to the watch state, so when they overlap the second one logs that the lock is held and exits; queue files stay for its next run.

launchd restarts the queue job for as long as anything is in the queue directory, so a queue run leaves only the files of PRs held back by its per-run limit. Each run reviews at least one PR, so a burst drains with one relaunch about every minute. Three directories sit side by side in `~/.local/state/github-events/`:

| Directory | Holds |
| --- | --- |
| `prbot/` | New event files, and files of PRs waiting only on the per-run limit, left untouched. A PR's files are deleted once nothing is left to do on it. |
| `prbot-deferred/` | Event files for PRs whose review failed, timed out, or waits out the one-hour retry delay, and every event file when the lookup of the bots' repositories fails. Each move adds 1 to an `attempts` field in the file. The hourly pass moves them back into `prbot/` after it finishes. |
| `prbot-rejected/` | Anything that can never be handled: bad JSON (after 30 seconds, in case it was still being written), events that are not `pull_request`, repositories the bots don't cover, dotfiles such as `.DS_Store`, other file names, directories and symlinks, and events deferred 24 times. Kept as evidence; nothing reads or removes them, so clear this directory by hand now and then. |

The hourly pass finds the queue directory through `PRBOT_QUEUE_DIR`, which defaults to `~/.local/state/github-events/prbot`. If you point the queue job at another directory, set `PRBOT_QUEUE_DIR` to the same path in the backstop plist.

The two plists in this directory hold absolute paths for this Mac, because launchd does not expand `~`. `install-watch.sh` is the older installer: it generates the single 5-minute job for whatever machine it runs on, and it is also the rollback below.

## Install

Check the plists, create the queue directory (launchd needs it to exist before it can watch it), then swap the jobs:

```bash
cd ~/Documents/GitHub/pr-review-bots
plutil -lint integrations/launchd/com.kennylhilljr.prbot-watch-queue.plist integrations/launchd/com.kennylhilljr.prbot-watch.plist
mkdir -p ~/.local/state/github-events/prbot
launchctl bootout gui/$(id -u)/com.kennylhilljr.prbot-watch
cp integrations/launchd/com.kennylhilljr.prbot-watch-queue.plist integrations/launchd/com.kennylhilljr.prbot-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kennylhilljr.prbot-watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kennylhilljr.prbot-watch-queue.plist
```

Verify that both are loaded, then look at the state and last exit code each one reports, and at the log:

```bash
launchctl print gui/$(id -u)/com.kennylhilljr.prbot-watch-queue
launchctl print gui/$(id -u)/com.kennylhilljr.prbot-watch
```

A review that runs longer than 900 seconds is stopped and counted as failed. To change that, add `PRBOT_REVIEW_TIMEOUT` (seconds) to `EnvironmentVariables` in both plists before copying them.

## Roll back

Remove the queue job and put back the 5-minute job:

```bash
launchctl bootout gui/$(id -u)/com.kennylhilljr.prbot-watch-queue
rm ~/Library/LaunchAgents/com.kennylhilljr.prbot-watch-queue.plist
~/Documents/GitHub/pr-review-bots/integrations/launchd/install-watch.sh
```

Files left in the queue, deferred or rejected directories do no harm once the queue job is gone.
