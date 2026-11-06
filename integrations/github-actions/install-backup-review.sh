#!/usr/bin/env bash
# Install the backup review workflow on Kenny's repos. Run it yourself, from your own gh login:
# it stores a secret and opens a PR on each repo, which agents must not do as you.
#
#   integrations/github-actions/install-backup-review.sh [owner/repo ...]
#
# Before running:
#   1. Install the Claude GitHub App on the repos: https://github.com/apps/claude/installations/new
#   2. Run `claude setup-token` and keep the token it prints; this script asks for it once.
#
# For each repo it sets the CLAUDE_CODE_OAUTH_TOKEN Actions secret and opens a PR that adds
# .github/workflows/backup-review.yml, labeled review:codex so the Codex bot reviews it.
# The token is read without echo, passed to gh on stdin, and never written to disk.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
workflow="$here/backup-review.yml"
branch="ci/backup-review"
repos=("$@")
if [ ${#repos[@]} -eq 0 ]; then
  repos=(
    kennylhilljr/openclaw-ecc-orchestrator
    kennylhilljr/issue-worker-bots
    kennylhilljr/pr-review-bots
    kennylhilljr/research-mcp
    kennylhilljr/ecc-customization
    kennylhilljr/agent-control-plane-infra
  )
fi

gh auth status 2>&1 | grep -q "'workflow'" || {
  echo "Your gh login needs the workflow scope: gh auth refresh -s workflow" >&2
  exit 2
}

# shellcheck disable=SC2016  # the backticks are literal
printf 'Paste the token from `claude setup-token` (input hidden): '
read -rs token
echo
case "$token" in
  sk-ant-oat*) ;;
  *) echo "That doesn't look like a Claude Code OAuth token (sk-ant-oat...)." >&2; exit 2 ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for repo in "${repos[@]}"; do
  echo "== $repo"
  printf '%s' "$token" | gh secret set CLAUDE_CODE_OAUTH_TOKEN -R "$repo"

  if gh pr list -R "$repo" --head "$branch" --state open --json url --jq '.[].url' | grep -q .; then
    echo "   a backup review PR is already open; skipping the workflow"
    continue
  fi

  dir="$tmp/${repo#*/}"
  gh repo clone "$repo" "$dir" -- --depth 1 --quiet
  mkdir -p "$dir/.github/workflows"
  cp "$workflow" "$dir/.github/workflows/backup-review.yml"
  git -C "$dir" switch -q -c "$branch"
  git -C "$dir" add .github/workflows/backup-review.yml
  if git -C "$dir" diff --cached --quiet; then
    echo "   workflow already up to date on the default branch"
    continue
  fi
  git -C "$dir" commit -q -m "Add backup PR review with the Claude Code GitHub Action

Reviews open PRs that prbot has not reviewed two hours after their last
commit, for when the Mac running prbot watch is offline. Posts as claude[bot].
Source: kennylhilljr/pr-review-bots integrations/github-actions/backup-review.yml"
  git -C "$dir" push -q -u origin "$branch"
  gh label create review:codex -R "$repo" --color 0E8A16 --description "Review with Codex" --force >/dev/null
  gh pr create -R "$repo" --head "$branch" --label review:codex \
    --title "Add backup PR review with the Claude Code GitHub Action" \
    --body "Adds \`.github/workflows/backup-review.yml\` from [pr-review-bots](https://github.com/kennylhilljr/pr-review-bots/blob/main/integrations/github-actions/backup-review.yml).

Every 30 minutes it looks for open PRs whose newest commit has had no prbot review for 2 hours, reviews up to 3 of them read-only with the same prompt prbot uses, and posts each review as claude[bot]. It needs the Claude GitHub App on this repo and the \`CLAUDE_CODE_OAUTH_TOKEN\` secret, which is set.

After merging, run it once from the Actions tab (Backup review, Run workflow) to check it."
done

echo
echo "Done. Merge the PRs, then run Backup review once from each repo's Actions tab."
