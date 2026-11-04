"""Machine-local fleet state. Transactions serialize launches and supervisor changes."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Lane:
    name: str
    program: str
    route: str
    brief: str
    worktree: str | None = None
    approval: str | None = None
    pid: int = 0
    pgid: int = 0
    command: str = ""
    status: str = "starting"
    reason: str = ""
    resumes: int = 0
    max_resumes: int = 2
    next_launch: float = 0
    served: dict[str, int] = field(default_factory=dict)
    files: dict[str, dict] = field(default_factory=dict)
    verification: dict = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return Path(self.program) / self.name


def default_state() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "evallab-fleet"
    )


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "fleet.sqlite", timeout=10)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS lanes (name TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, lane TEXT, data TEXT)"
        )
        self.db.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def lanes(self) -> list[Lane]:
        return [
            Lane(**json.loads(row[0]))
            for row in self.db.execute("SELECT data FROM lanes ORDER BY name")
        ]

    def get(self, name: str) -> Lane:
        row = self.db.execute("SELECT data FROM lanes WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown lane: {name}")
        return Lane(**json.loads(row[0]))

    def save(self, lane: Lane) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO lanes VALUES (?, ?)", (lane.name, json.dumps(asdict(lane)))
        )

    def meta(self, key: str, default: object = None):
        row = self.db.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, data: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(data)))

    def event(self, at: float, lane: Lane, **data: object) -> None:
        self.db.execute(
            "INSERT INTO events(at, lane, data) VALUES (?, ?, ?)", (at, lane.name, json.dumps(data))
        )
