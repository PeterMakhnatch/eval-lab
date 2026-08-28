"""State certificate models, invariant verification, and hash checking."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def compute_digest(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StateCertificate:
    initial_digest: str
    final_digest: str
    step_count: int
    mutations: list[str]
    invariants_passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DatabaseState:
    def __init__(self, records: dict[str, Any] | None = None):
        self.records: dict[str, Any] = records.copy() if records else {}
        self.history: list[str] = []

    def get(self, key: str) -> Any:
        return self.records.get(key)

    def set(self, key: str, value: Any) -> None:
        self.records[key] = value
        self.history.append(f"set:{key}")

    def delete(self, key: str) -> bool:
        if key in self.records:
            del self.records[key]
            self.history.append(f"delete:{key}")
            return True
        return False

    def digest(self) -> str:
        return compute_digest(self.records)

    def clone(self) -> DatabaseState:
        new_db = DatabaseState(self.records)
        new_db.history = list(self.history)
        return new_db
