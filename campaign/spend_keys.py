from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Protocol

from campaign.spend_types import Root, amount

TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 65_536
COUNTERS = ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining")


class KeyProbeSystem(Protocol):
    def key_env_value(self, name: str) -> str | None: ...

    def openrouter_key(self, key: str, timeout: float, size: int) -> bytes | None: ...


def lane_keys(roots: list[Root], problems: list[str]) -> dict[str, str]:
    keys: dict[str, set[str]] = {}
    for root in roots:
        if root.key_env is not None:
            keys.setdefault(root.owner, set()).add(root.key_env)
    lanes = {}
    for lane, names in sorted(keys.items()):
        if len(names) != 1:
            problems.append(f"{lane}: roots name different key_env variables for this lane")
        else:
            lanes[lane] = next(iter(names))
    return lanes


def _counters(body: bytes) -> dict[str, Decimal | None] | None:
    if not 0 < len(body) <= MAX_RESPONSE_BYTES:
        return None
    try:
        obj = json.loads(body, parse_float=Decimal, parse_int=Decimal)
    except (ValueError, RecursionError, InvalidOperation):
        return None
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return None
    counters = {}
    for name in COUNTERS:
        if name not in data:
            return None
        value = data[name]
        if value is None and name in ("limit", "limit_remaining"):
            counters[name] = None
            continue
        negative = name == "limit_remaining" and isinstance(value, Decimal) and value.is_signed()
        number = amount(value.copy_abs() if negative else value)
        if number is None:
            return None
        counters[name] = number.copy_negate() if negative and number else number
    return counters


def _read(system: KeyProbeSystem, name: str) -> tuple[dict[str, Decimal | None] | None, str | None]:
    value = system.key_env_value(name)
    if not value:
        return None, "variable is missing or empty"
    if not (len(value) <= 4096 and value.isascii() and value.isprintable() and " " not in value):
        return None, "variable is not a valid header value"
    body = system.openrouter_key(value, TIMEOUT_SECONDS, MAX_RESPONSE_BYTES + 1)
    if body is None:
        return None, "OpenRouter key request failed"
    data = _counters(body)
    if data is None:
        return None, "invalid OpenRouter key counters (response not shown)"
    return data, None


def rows(system: KeyProbeSystem, lanes: dict[str, str], recorded: dict[str, Decimal],
         problems: list[str]) -> list[dict[str, str | None]]:
    probes = {name: _read(system, name) for name in sorted(set(lanes.values()))}
    result = []
    for lane, name in lanes.items():
        data, problem = probes[name]
        if problem is not None:
            problems.append(f"{lane}: {name}: {problem}")
        counters: dict[str, str | None] = dict.fromkeys(COUNTERS)
        if data is not None:
            counters.update({field: format(value, "f") for field, value in data.items() if value is not None})
        usage = data["usage"] if data is not None else None
        result.append({
            "lane": lane, "key_env": name, "problem": problem,
            **counters,
            "recorded_usd": format(recorded[lane], "f"),
            "remainder_usd": format(usage - recorded[lane], "f") if usage is not None else None,
        })
    return result
