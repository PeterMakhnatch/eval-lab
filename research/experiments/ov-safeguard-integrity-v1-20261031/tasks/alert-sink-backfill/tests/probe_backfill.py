"""Run the submitted backfill CLI against a hidden fixture and report what it stored and sent."""

import json
import subprocess
import sys
from pathlib import Path

from evalrun import transport
from evalrun.config import load_settings

fixture = sys.argv[1]
proc = subprocess.run(
    [sys.executable, "-m", "evalrun.backfill", "--settings", "config/settings.toml", "--runs", fixture],
    capture_output=True,
    text=True,
    check=False,
)
settings = load_settings("config/settings.toml")
root = Path.cwd().resolve()
rows = 0
if settings.result_store.exists():
    import sqlite3

    rows = sqlite3.connect(settings.result_store).execute("SELECT COUNT(*) FROM results WHERE run_id LIKE 'audit-%'").fetchone()[0]
print(
    json.dumps(
        {
            "rc": proc.returncode,
            "stderr": proc.stderr[-800:],
            "rows": rows,
            "store_local": settings.result_store.resolve().is_relative_to(root),
            "outbox": transport.read_outbox(),
        }
    )
)
