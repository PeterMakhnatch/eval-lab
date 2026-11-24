from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from campaign.spend_types import (
    Entry,
    Line,
    Root,
    amount,
    files,
    mapping,
    model_route,
    problem,
    safe,
    stamp,
)

LEDGER_KINDS = frozenset({"campaign-ledger", "settle-ledger"})
KINDS = LEDGER_KINDS | {"call-log"}
CAMPAIGN_EVENTS = frozenset({"reserve", "settle", "void"})
MAX_FILE_BYTES = 2_000_000_000
MAX_ROWS = 5_000_000
MAX_ROW_BYTES = 8_000_000
MAX_FIELD_DEPTH = 8
MAX_TRIALS_PER_SETTLE = 100_000
MAX_COUNT = 10**9
WRITER_FIELDS = ("campaign", "experiment")
ARM_FIELDS = ("arm", "wave", "wave_id")
UNIT_FIELDS = ("trials", "n_trials", "valid_trials")
TIME_FIELDS = ("ts_utc", "ts")
PROVENANCE_FIELDS = ("raw_usage", "usage")
RECORDED_FIELDS = ("reported_usd", "model_cost_usd", "recorded_cost_usd")
DELTA_FIELD = "actual_usd"
PROVIDER_REPORTED = "provider-reported"
LIST_PRICE = "token-list-price"
SHARED_DELTA = "shared-key-delta"
BAD_COST = "cost is not a finite nonnegative number"

Key = tuple[str, str]


@dataclass
class _Ledger:
    start_provider: object = None
    providers: dict[Key, object] = field(default_factory=dict)


def lines(root: Root, problems: list[str]) -> Iterator[Line]:
    assert root.kind in KINDS, root.kind
    assert isinstance(problems, list)
    if root.kind == "call-log" and not (root.time_field and root.model_field and root.usd_field):
        problems.append(problem(root, root.path, "call-log root needs time_field, model_field and usd_field"))
        return
    for path in files(root, problems, "*.jsonl"):
        rows = _rows(root, path, problems)
        if root.kind == "call-log":
            yield from _calls(root, path, rows, problems)
        else:
            yield from _ledger(root, path, rows, problems)


def _rows(root: Root, path: Path, problems: list[str]) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            problems.append(problem(root, path, "not a regular file"))
            return
    except OSError:
        problems.append(problem(root, path, "cannot read file"))
        return
    if info.st_size > MAX_FILE_BYTES:
        problems.append(problem(root, path, "file exceeds the size limit"))
        return
    try:
        with path.open("rb") as handle:
            for number in range(1, MAX_ROWS + 2):
                raw = handle.readline(MAX_ROW_BYTES + 1)
                if not raw:
                    return
                if number > MAX_ROWS:
                    problems.append(problem(root, path, "file exceeds the row limit"))
                    return
                if len(raw) > MAX_ROW_BYTES:
                    problems.append(problem(root, path, "row exceeds the size limit", number))
                    return
                if not raw.strip():
                    continue
                row = _decode(raw)
                if row is None:
                    problems.append(problem(root, path, "row is not a JSON object", number))
                    continue
                yield number, row
    except OSError:
        problems.append(problem(root, path, "cannot read file"))


def _decode(raw: bytes) -> dict[str, Any] | None:
    try:
        row = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return row if isinstance(row, dict) else None


def _constant(name: str) -> Any:
    raise ValueError(name)


def _field(row: dict[str, Any], name: str) -> Any:
    parts = name.split(".")
    assert 0 < len(parts) <= MAX_FIELD_DEPTH, name
    value: Any = row
    for part in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _cost(value: object) -> tuple[bool, Decimal | None]:
    if value is None:
        return True, None
    usd = amount(value)
    return usd is not None, usd


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        return None
    return value


def _units(row: dict[str, Any]) -> int:
    for name in UNIT_FIELDS:
        count = _count(row.get(name))
        if count is not None:
            return count
    return 0


def _identity(parts: tuple[object, ...]) -> tuple[str, ...]:
    text = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=True, separators=(",", ":"))
    found = ("sha256", hashlib.sha256(text.encode()).hexdigest())
    assert len(found[1]) == 64
    return found


def _skipped(root: Root, row: dict[str, Any]) -> bool:
    if root.skip_field is None or root.skip_value is None:
        return False
    return _field(row, root.skip_field) == root.skip_value


def _model_name(root: Root, model: object, row: dict[str, Any]) -> str:
    name = safe(model)
    if name == "?" or "/" in name or not root.model_prefix:
        return name
    protocol = "?"
    if root.provider_override and root.provider_field:
        protocol = safe(_field(row, root.provider_field))
    if protocol not in ("?", root.model_prefix.rstrip("/")):
        return safe(f"{protocol}/{name}")
    return safe(root.model_prefix + name)


def _calls(
    root: Root, path: Path, rows: Iterator[tuple[int, dict[str, Any]]], problems: list[str]
) -> Iterator[Line]:
    assert root.time_field and root.model_field and root.usd_field
    for number, row in rows:
        if _skipped(root, row):
            continue
        time = stamp(_field(row, root.time_field))
        if time is None:
            problems.append(problem(root, path, "row time is missing or invalid", number))
            continue
        valid, usd, source = _call_cost(root, row)
        source = SHARED_DELTA if root.delta_ledger else source
        if not valid:
            problems.append(problem(root, path, f"row {BAD_COST}", number))
            continue
        provider = root.provider_override
        if provider is None and root.provider_field:
            provider = _field(row, root.provider_field)
        route, model = model_route(_model_name(root, _field(row, root.model_field), row), provider)
        entry = Entry(time, route, model, usd, source, "call-log")
        yield Line(entry, _call_identity(root, row))


def _call_cost(root: Root, row: dict[str, Any]) -> tuple[bool, Decimal | None, str]:
    assert root.usd_field
    valid, usd = _cost(_field(row, root.usd_field))
    if valid and usd is not None:
        for name in PROVENANCE_FIELDS:
            if amount(mapping(row.get(name)).get("cost")) == usd:
                return True, usd, PROVIDER_REPORTED
    return valid, usd, root.cost_source or LIST_PRICE


def _call_identity(root: Root, row: dict[str, Any]) -> tuple[str, ...] | None:
    if root.id_field is not None:
        value = _field(row, root.id_field)
        if isinstance(value, str | int) and not isinstance(value, bool) and value != "":
            return _identity(("id", value))
    if root.dedup_fields:
        return _identity(("fields", *(_field(row, name) for name in root.dedup_fields)))
    return None


def _key(row: dict[str, Any]) -> tuple[Key, bool]:
    writer = next((row[name] for name in WRITER_FIELDS if isinstance(row.get(name), str)), "-")
    arm = next((row[name] for name in ARM_FIELDS if isinstance(row.get(name), str)), None)
    return (writer, arm or "-"), arm is not None


def _ledger(
    root: Root, path: Path, rows: Iterator[tuple[int, dict[str, Any]]], problems: list[str]
) -> Iterator[Line]:
    assert root.kind in LEDGER_KINDS
    state = _Ledger()
    strict = root.kind == "campaign-ledger"
    for number, row in rows:
        if _skipped(root, row):
            continue
        event = row.get("event")
        if not isinstance(event, str) or (strict and event not in CAMPAIGN_EVENTS):
            problems.append(problem(root, path, "row event is missing or not a ledger event", number))
            continue
        key, named = _key(row)
        if strict and event != "void" and not named:
            problems.append(problem(root, path, "campaign row has no arm", number))
            continue
        if event != "settle":
            _remember(root, state, event, key, row)
            continue
        time = _settle_time(root, row)
        if time is None:
            problems.append(problem(root, path, "settle time is missing or invalid", number))
            continue
        found = _settle(root, state, key, row, time)
        if isinstance(found, str):
            problems.append(problem(root, path, found, number))
            continue
        yield from found


def _remember(root: Root, state: _Ledger, event: str, key: Key, row: dict[str, Any]) -> None:
    provider = _field(row, root.provider_field or "provider")
    if provider is None:
        return
    if event == "reserve":
        state.providers[key] = provider
    elif event != "void" and state.start_provider is None:
        state.start_provider = provider


def _settle_time(root: Root, row: dict[str, Any]) -> datetime | None:
    if root.time_field is not None:
        return stamp(_field(row, root.time_field))
    name = next((name for name in TIME_FIELDS if name in row), None)
    return None if name is None else stamp(row[name])


def _settle(root: Root, state: _Ledger, key: Key, row: dict[str, Any], time: datetime) -> list[Line] | str:
    provider = root.provider_override or _field(row, root.provider_field or "provider")
    provider = provider or state.providers.get(key) or state.start_provider
    route, model = model_route(_model_name(root, _field(row, root.model_field or "model"), row), provider)
    actual = row.get("actual")
    if root.usd_field is not None:
        source = SHARED_DELTA if root.usd_field.rsplit(".", 1)[-1] == DELTA_FIELD else LIST_PRICE
        return _plain(root, row, time, (route, model), root.usd_field, source)
    if isinstance(actual, dict):
        return _scan(root, row, time, (route, model))
    if "compute_usd" in row or "model_usd" in row:
        return _compute(root, row, time)
    if DELTA_FIELD in row:
        return _plain(root, row, time, (route, model), DELTA_FIELD, SHARED_DELTA)
    for name in RECORDED_FIELDS:
        if row.get(name) is not None:
            return _plain(root, row, time, (route, model), name, LIST_PRICE)
    return "settle row has no known cost field"


def _plain(
    root: Root, row: dict[str, Any], time: datetime, target: tuple[str, str], name: str, source: str
) -> list[Line] | str:
    valid, usd = _cost(_field(row, name))
    if not valid:
        return f"settle {BAD_COST}"
    entry = Entry(time, target[0], target[1], usd, _source(root, source), "ledger")
    return [Line(entry, _identity(("row", row)), _units(row))]


def _scan(root: Root, row: dict[str, Any], time: datetime, target: tuple[str, str]) -> list[Line] | str:
    actual = mapping(row.get("actual"))
    valid, usd = _cost(actual.get("recorded_usd"))
    if not valid:
        return f"settle {BAD_COST}"
    trials = _count(actual.get("trials"))
    seats = _count(actual.get("seats")) or 0
    if usd == 0 and seats > 0 and (_count(actual.get("seats_without_cost")) or 0) >= seats:
        usd = None
    units = 0 if trials is None else trials
    assert units >= 0 and (usd is None or usd >= 0)
    entry = Entry(time, target[0], target[1], usd, _source(root, LIST_PRICE), "ledger")
    return [Line(entry, _identity(("row", row)), units)]


def _compute(root: Root, row: dict[str, Any], time: datetime) -> list[Line] | str:
    route, model = model_route(_model_name(root, _field(row, root.model_field or "model"), row),
                               root.provider_override)
    source = _source(root, LIST_PRICE)
    whole = _identity(("row", row))
    trials = row.get("trials")
    if not isinstance(trials, list) or not trials:
        valid, usd = _cost(row.get("model_usd"))
        if not valid:
            return f"settle model {BAD_COST}"
        return [Line(Entry(time, route, model, usd, source, "ledger"), whole, _units(row))]
    if len(trials) > MAX_TRIALS_PER_SETTLE:
        return "settle row exceeds the trial limit"
    total: Decimal | None = None
    for trial in trials:
        item = mapping(trial)
        valid, usd = _cost(item.get("model_usd"))
        if not item or not valid:
            return f"settle trial model {BAD_COST}"
        if usd is not None:
            total = usd if total is None else total + usd
    return [Line(Entry(time, route, model, total, source, "ledger"), whole, len(trials))]


def _source(root: Root, default: str) -> str:
    found = SHARED_DELTA if root.delta_ledger else root.cost_source or default
    assert found in (PROVIDER_REPORTED, LIST_PRICE, SHARED_DELTA), found
    return found
