"""Point prbot's state paths at a per-run temp folder before any test module imports prbot."""
import atexit, os, shutil, tempfile
from pathlib import Path

FORBIDDEN = tuple((Path.home() / r).resolve() for r in (".config", ".local/state", ".openclaw"))

base = tempfile.gettempdir()
# OpenClaw's TMPDIR can live under ~/.openclaw, so fall back to /tmp when the temp folder is a real state root
if any(Path(base).resolve().is_relative_to(root) for root in FORBIDDEN):
    base = "/tmp"
TMP = tempfile.mkdtemp(prefix="prbot-tests-", dir=base)

os.environ["PRBOT_CONFIG_DIR"] = f"{TMP}/config"
os.environ["PRBOT_QUEUE_DIR"] = f"{TMP}/queue"
os.environ["PRBOT_OPENCLAW_EVENTS"] = f"{TMP}/openclaw-events.jsonl"

atexit.register(shutil.rmtree, TMP, ignore_errors=True)
