"""Tamper-evident event log: every entry commits to the hash of the previous one."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class TamperEvidenceLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])["hash"] if lines else "0" * 64

    def append(self, event: dict) -> str:
        body = json.dumps({"prev": self._head, "event": event}, sort_keys=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"prev": self._head, "event": event, "hash": digest}, sort_keys=True) + "\n")
        self._head = digest
        return digest

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
