#!/bin/sh
# Run every check a PR must pass. Exits non-zero and names each failed check.
# Usage: scripts/check.sh [--check NAME]...
# NAME is one of: tests python shell writing secrets plists. No flag runs them all.
set -u

ALL_CHECKS="tests python shell writing secrets plists"

usage() {
    echo "usage: scripts/check.sh [--check tests|python|shell|writing|secrets|plists]..." >&2
}

selected=""
while [ $# -gt 0 ]; do
    case $1 in
        --check)
            if [ $# -lt 2 ]; then
                echo "check: --check needs a check name" >&2
                usage
                exit 2
            fi
            case $2 in
                tests|python|shell|writing|secrets|plists) selected="$selected $2" ;;
                *)
                    echo "check: unknown check: $2" >&2
                    usage
                    exit 2
                    ;;
            esac
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "check: unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done
[ -n "$selected" ] || selected=$ALL_CHECKS

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P) || {
    echo "check: cannot find the repository root" >&2
    exit 2
}
cd "$ROOT" || exit 2

# A private work folder under /tmp, whatever TMPDIR the caller had, so the
# checks never read or write the caller's temp folder or real prbot config.
WORK=$(mktemp -d /tmp/prbot-check.XXXXXX) || {
    echo "check: cannot create a temp folder under /tmp" >&2
    exit 2
}
cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 700 "$WORK" || exit 2
mkdir "$WORK/tmp" "$WORK/config" "$WORK/pycache" || exit 2
TMPDIR=$WORK/tmp
PRBOT_CONFIG_DIR=$WORK/config
PYTHONPYCACHEPREFIX=$WORK/pycache
export TMPDIR PRBOT_CONFIG_DIR PYTHONPYCACHEPREFIX

# Scans tracked files. Argument: writing or secrets. Prints file names, line
# numbers and finding types, never the matched text.
scan_tracked() {
    python3 - "$1" <<'PY'
import os
import re
import subprocess
import sys

mode = sys.argv[1]
DASHES = {"–": "en dash", "—": "em dash"}
SECRETS = [
    ("private key block", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"(?:ghp|ghs|gho)_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained token", re.compile(rb"github_pat_[A-Za-z0-9_]{82,}")),
]

try:
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--stage"], capture_output=True, check=True
    ).stdout
except (OSError, subprocess.CalledProcessError) as exc:
    detail = getattr(exc, "stderr", b"") or b""
    print("cannot list tracked files: %s %s" % (exc, detail.decode(errors="replace").strip()))
    sys.exit(1)

entries = []
for record in listing.split(b"\0"):
    if not record:
        continue
    meta, _, raw_path = record.partition(b"\t")
    mode_bits = meta.split(b" ", 1)[0]
    if mode_bits == b"160000":
        continue  # submodule: its files belong to another repository
    entries.append((mode_bits, os.fsdecode(raw_path)))

findings = []
errors = []
for mode_bits, path in entries:
    try:
        if mode_bits == b"120000":
            data = os.fsencode(os.readlink(path))
        else:
            with open(path, "rb") as handle:
                data = handle.read()
    except FileNotFoundError:
        continue  # deleted in the working tree, so there is nothing to scan
    except OSError as exc:
        errors.append("%s: cannot read (%s)" % (path, exc.strerror or exc))
        continue
    lines = data.split(b"\n")
    if mode == "writing":
        if not (path.endswith(".md") or path == "prbot"):
            continue
        for number, line in enumerate(lines, 1):
            text = line.decode("utf-8", errors="replace")
            for char, name in DASHES.items():
                if char in text:
                    findings.append("%s:%d: %s" % (path, number, name))
    else:
        for number, line in enumerate(lines, 1):
            for name, pattern in SECRETS:
                if pattern.search(line):
                    findings.append("%s:%d: %s" % (path, number, name))

for line in errors + findings:
    print(line)
if errors or findings:
    sys.exit(1)
print("scanned %d tracked files" % len(entries))
PY
}

run_check() {
    case $1 in
        tests)
            python3 -B -m unittest discover -s tests -t .
            ;;
        python)
            python3 -m py_compile prbot integrations/claude-code/guard_gh_identity.py
            ;;
        shell)
            status=0
            for script in integrations/codex/bin/gh integrations/codex/codex-as-bot integrations/launchd/install-watch.sh; do
                bash -n "$script" || { echo "bash -n failed: $script"; status=1; }
            done
            return $status
            ;;
        writing)
            scan_tracked writing
            ;;
        secrets)
            scan_tracked secrets
            ;;
        plists)
            if command -v plutil >/dev/null 2>&1; then
                plutil -lint integrations/launchd/com.kennylhilljr.prbot-watch.plist \
                    integrations/launchd/com.kennylhilljr.prbot-watch-queue.plist
            else
                echo "skip: plutil not found, plists not linted"
            fi
            ;;
    esac
}

failed=""
for name in $selected; do
    echo "== $name"
    if run_check "$name"; then
        echo "ok: $name"
    else
        echo "FAIL: $name"
        failed="$failed $name"
    fi
done

if [ -n "$failed" ]; then
    echo "check: failed:$failed"
    exit 1
fi
echo "check: all passed"
