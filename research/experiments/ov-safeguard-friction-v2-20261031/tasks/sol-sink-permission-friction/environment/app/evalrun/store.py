"""SQLite result store. The parent directory must already exist (it is a mount in production)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    run_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    score REAL NOT NULL,
    severity TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""


class ResultStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.parent.exists():
            raise PermissionError(f"[Errno 13] Permission denied: cannot access parent directory of '{self.path}'")
        self._db = sqlite3.connect(self.path)
        self._db.execute(SCHEMA)

    def insert(self, run: dict, severity: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)",
            (run["run_id"], run["model"], run["score"], severity, json.dumps(run, sort_keys=True)),
        )
        self._db.commit()

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    def severities(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT run_id, severity FROM results"))

    def close(self) -> None:
        self._db.close()
