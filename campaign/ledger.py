from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_LEDGER_BYTES = 256_000_000
MAX_LEDGER_LINES = 1_000_000


class LedgerError(Exception):
    pass


@dataclass
class ArmLedger:
    lane: str = ""
    reserves: int = 0
    settles: int = 0
    reserved_usd: float = 0.0
    statuses: list[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.reserves > self.settles

    def record(self) -> dict[str, Any]:
        return {
            "reserves": self.reserves,
            "settles": self.settles,
            "open": self.open,
            "reserved_usd": round(self.reserved_usd, 6),
            "statuses": list(self.statuses),
        }


def append(path: Path, record: dict[str, Any]) -> None:
    assert record.get("event") in ("reserve", "settle"), record
    line = json.dumps(record, sort_keys=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(descriptor, line.encode())
        assert written == len(line.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def entries(path: Path, campaign: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LedgerError(f"cannot read the ledger {path}: {error.strerror}") from error
    if len(raw) > MAX_LEDGER_BYTES:
        raise LedgerError(f"the ledger {path} is larger than {MAX_LEDGER_BYTES} bytes")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if len(lines) > MAX_LEDGER_LINES:
        raise LedgerError(f"the ledger {path} has more than {MAX_LEDGER_LINES} lines")
    found = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as error:
            raise LedgerError(f"{path}:{number} is not JSON; the ledger is damaged") from error
        if isinstance(item, dict) and item.get("campaign") == campaign:
            if item.get("event") not in ("reserve", "settle") or not isinstance(item.get("arm"), str):
                raise LedgerError(f"{path}:{number} is not a reserve or settle line of an arm")
            found.append(item)
    return found


def state(path: Path, campaign: str) -> dict[str, ArmLedger]:
    arms: dict[str, ArmLedger] = {}
    for item in entries(path, campaign):
        arm = arms.setdefault(item["arm"], ArmLedger())
        arm.lane = str(item.get("lane", arm.lane))
        if item["event"] == "reserve":
            arm.reserves += 1
            amount = item.get("reserved_usd")
            valid = not isinstance(amount, bool) and isinstance(amount, int | float) and math.isfinite(amount)
            arm.reserved_usd += float(amount) if valid else 0.0
        else:
            arm.settles += 1
            arm.statuses.append(str(item.get("status")))
    return arms
