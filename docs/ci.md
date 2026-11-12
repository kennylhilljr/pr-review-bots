# CI for pr-review-bots

The repository's checks live in `scripts/check.sh`. Run it before you open a PR:

```bash
scripts/check.sh
```

GitHub should run the same script on every PR. The worker bots are not allowed to write `.github/workflows/`, so they can't add the workflow. Kenny adds it by saving the block below as `.github/workflows/ci.yml` and opening a PR with it.

## The workflow

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ['3.9', '3.13']
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Run scripts/check.sh
        run: scripts/check.sh
```

It runs on every pull request and on pushes to `main`, once on Python 3.9 (the oldest version `README.md` supports) and once on Python 3.13. The `ubuntu-latest` runner already has `gh`, `git` and `openssl`, which the tests use. The tests make no network calls, so the workflow needs no secrets and no App token. It only reads the repository, and the checkout doesn't keep a token on disk.

This is separate from `.github/workflows/backup-review.yml`. That workflow posts a review on PRs prbot has left alone; this one only runs the checks and posts nothing.

## What the script checks

| Check | What it does |
| --- | --- |
| `tests` | `python3 -B -m unittest discover -s tests -t .` |
| `python` | `python3 -m py_compile` on `prbot` and `integrations/claude-code/guard_gh_identity.py` |
| `shell` | `bash -n` on `integrations/codex/bin/gh`, `integrations/codex/codex-as-bot`, `integrations/launchd/install-watch.sh` and `integrations/github-actions/install-backup-review.sh`, one at a time |
| `writing` | No em dash (U+2014) or en dash (U+2013) in tracked `*.md` files or in `prbot` |
| `secrets` | No private key block and no literal GitHub token in any tracked file |
| `plists` | `plutil -lint` on the two plists in `integrations/launchd/`, or a Python `plistlib` parse when `plutil` is missing (as on Linux) |

With no arguments the script runs every check, keeps going after a failure, prints `FAIL: <check>` for each one that failed, and exits 1. Run one check, or a few, with `--check`:

```bash
scripts/check.sh --check writing --check secrets
```

An unknown or incomplete argument exits 2.

The `writing` and `secrets` checks read the working-tree copy of each file Git tracks (`git ls-files`), so untracked files are ignored and staged edits are checked. When the staged copy in the index differs from the working tree, or the file was deleted from the working tree, the staged copy is checked too, and its findings end with `(staged copy)`. If Git can't list the files or a tracked file can't be read, the check fails.

The `secrets` check looks for a `BEGIN ... PRIVATE KEY` line (plain, RSA, EC, OPENSSH, ENCRYPTED and other labels), for `ghp_` or `gho_` followed by 36 or more letters, digits and underscores, for `ghs_` followed by 36 or more letters, digits and underscores (the old 40-character installation tokens) or by an app ID, an underscore and a three-part JWT (the long stateless `ghs_APPID_JWT` ones), and for `github_pat_` followed by 82 or more letters, digits and underscores. It prints the file, line and kind of finding, never the matched text. Tests that need a fake token build it at runtime, for example `"ghp_" + "a" * 36`, so the source holds no literal token.

## Isolation

Each run makes a private folder under `/tmp`, whatever `TMPDIR` the caller has, and sets `TMPDIR`, `PRBOT_CONFIG_DIR` and `PYTHONPYCACHEPREFIX` to empty folders inside it. The checks never use the caller's temp folder or real prbot config, and bytecode never lands in the repository. The folder is deleted when the script exits or is stopped with Ctrl-C or a termination signal.

## Python 3.9

No Python 3.9 interpreter was available when this was written, so the suite has not run on 3.9 yet. The code parses with the 3.9 grammar, and `tests/test_check.py` passes on Python 3.10 and 3.13. The 3.9 job in the workflow above is the first real 3.9 run. If it fails, either fix the code or raise the minimum in `README.md` (badge and Requirements), in the matrix above and in `tests/test_ci_docs.py` together.
