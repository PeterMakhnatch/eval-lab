from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_LEDGER_BYTES = 256_000_000
MAX_LEDGER_LINES = 1_000_000

Key = tuple[str, str]


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


@dataclass(frozen=True)
class ForeignReserve:
    writer: str
    arm: str
    reserved_usd: float
    ts_utc: str
    guessed: bool
    line_number: int


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


def _lines(path: Path) -> list[tuple[int, dict[str, Any]]]:
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
        if isinstance(item, dict):
            found.append((number, item))
    return found


def entries(path: Path, campaign: str) -> list[dict[str, Any]]:
    found = []
    for number, item in _lines(path):
        if item.get("campaign") == campaign:
            if item.get("event") not in ("reserve", "settle") or not isinstance(item.get("arm"), str):
                raise LedgerError(f"{path}:{number} is not a reserve or settle line of an arm")
            found.append(item)
    return found


def _amount(value: Any) -> float:
    valid = not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)
    return float(value) if valid else 0.0


def _stamp(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    valid = not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and value >= 0
    return float(value) if valid else None


def _foreign_lines(path: Path, campaign: str) -> tuple[dict[Key, list[tuple[int, dict[str, Any]]]], Counter[Key]]:
    reserves: dict[Key, list[tuple[int, dict[str, Any]]]] = {}
    settles: Counter[Key] = Counter()
    for number, item in _lines(path):
        if item.get("campaign") == campaign:
            continue
        writer = str(item.get("campaign") or item.get("experiment") or "-")
        event, arm = item.get("event"), item.get("arm")
        if event == "void" and isinstance(item.get("arms"), list):
            for name in item["arms"]:
                if isinstance(name, str):
                    settles[(writer, name)] = len(reserves.get((writer, name), ()))
        if not isinstance(arm, str):
            continue
        if event == "reserve":
            reserves.setdefault((writer, arm), []).append((number, item))
        elif event == "settle":
            key = (writer, arm)
            if settles[key] < len(reserves.get(key, ())):
                settles[key] += 1
    return reserves, settles


def _same_provider(value: Any, provider: str) -> bool:
    return not isinstance(value, str) or not value.strip() or value.strip().casefold() == provider.casefold()


def foreign_open(
    path: Path, campaign: str, provider: str, now: datetime, window_hours: float, unknown_usd: float
) -> list[ForeignReserve]:
    assert window_hours > 0 and now.tzinfo is not None and unknown_usd > 0
    reserves, settles = _foreign_lines(path, campaign)
    oldest = now - timedelta(hours=window_hours)
    found = []
    for key, items in sorted(reserves.items()):
        for number, item in items[settles[key]:]:
            if not _same_provider(item.get("provider"), provider):
                continue
            stamp = _stamp(item.get("ts_utc"))
            if stamp is not None and stamp <= oldest:
                continue
            amount = _number(item.get("reserved_usd"))
            found.append(ForeignReserve(key[0], key[1], unknown_usd if amount is None else amount,
                                        str(item.get("ts_utc")), amount is None, number))
    return found


def state(path: Path, campaign: str) -> dict[str, ArmLedger]:
    arms: dict[str, ArmLedger] = {}
    for item in entries(path, campaign):
        arm = arms.setdefault(item["arm"], ArmLedger())
        arm.lane = str(item.get("lane", arm.lane))
        if item["event"] == "reserve":
            arm.reserves += 1
            arm.reserved_usd += _amount(item.get("reserved_usd"))
        else:
            arm.settles += 1
            arm.statuses.append(str(item.get("status")))
    return arms
