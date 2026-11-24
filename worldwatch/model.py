from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
HEX_CHARS = frozenset("0123456789abcdef")
UNHASHED_KEYS = frozenset({"entry_hash", "hmac"})
SEAT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_REPORT_JSON_NODES = 2_000_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SMALL_JSON_BYTES = 16 * 1024 * 1024
MAX_LINES_PER_FILE = 500_000
MAX_EVENTS_PER_FILE = 250_000
MAX_TRIALS = 10_000
MAX_SEATS = 512
MAX_ALERTS = 50_000
DEFAULT_STALL_SECONDS = 300.0
DEFAULT_MAX_REJECT_RATIO = 0.5

CRITICAL_TRIPWIRES = frozenset({
    "broken-hash-chain",
    "all-seats-silent",
    "cost-spike",
    "reject-storm",
})
WARN_TRIPWIRES = frozenset({
    "silent-seat",
    "claim-churn",
    "dup-unseen",
    "post-close-action",
    "zero-world-activity",
})
ALL_TRIPWIRES = CRITICAL_TRIPWIRES | WARN_TRIPWIRES

DETECTOR_NAMES = (
    "dup_unseen",
    "dup_seen",
    "claim_churn",
    "misreport_world_mismatch",
    "early_stop_with_budget",
    "post_settlement_waste",
    "unauthorized_release",
    "creditor_override",
    "silent_seat",
    "tool_friction_storm",
    "message_neglect_decay",
)

NO_MODEL_AGENTS = frozenset({
    "oracle",
    "nop",
    "script",
    "scripted",
    "scriptagent",
    "scripted-team-agent",
})


class InputError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    timestamp: str
    code: str
    severity: str
    trial: str | None
    seat: str | None
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        assert self.code in ALL_TRIPWIRES
        assert self.severity in ("critical", "warn")
        return {
            "alert_id": self.alert_id,
            "timestamp": self.timestamp,
            "code": self.code,
            "severity": self.severity,
            "trial": self.trial,
            "seat": self.seat,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class SeatLive:
    seat: str
    state: str
    model: str | None
    agent_steps: int
    world_events: int
    last_activity_at: str | None
    silent_s: float | None
    exit_code: int | None
    stalled: bool

    def as_dict(self) -> dict[str, Any]:
        assert self.agent_steps >= 0 and self.world_events >= 0
        return {
            "seat": self.seat,
            "state": self.state,
            "model": self.model,
            "agent_steps": self.agent_steps,
            "world_events": self.world_events,
            "last_activity_at": self.last_activity_at,
            "silent_s": self.silent_s,
            "exit_code": self.exit_code,
            "stalled": self.stalled,
        }


@dataclass(frozen=True, slots=True)
class TrialLive:
    trial: str
    path: Path
    ecosystem: str
    state: str
    result_state: str
    reward: float | None
    reward_valid: bool
    cost_usd: float | None
    events_count: int
    rejected_count: int
    reject_ratio: float
    partial_bytes: int
    resets: int
    chain_ok: bool | None
    chain_error: str | None
    seats: tuple[SeatLive, ...]

    def as_dict(self) -> dict[str, Any]:
        assert self.ecosystem in ("olam", "bespoke")
        assert 0.0 <= self.reject_ratio <= 1.0
        return {
            "trial": self.trial,
            "path": str(self.path),
            "ecosystem": self.ecosystem,
            "state": self.state,
            "result_state": self.result_state,
            "reward": self.reward,
            "reward_valid": self.reward_valid,
            "cost_usd": self.cost_usd,
            "events_count": self.events_count,
            "rejected_count": self.rejected_count,
            "reject_ratio": round(self.reject_ratio, 6),
            "partial_bytes": self.partial_bytes,
            "resets": self.resets,
            "chain_ok": self.chain_ok,
            "chain_error": self.chain_error,
            "seats": [seat.as_dict() for seat in self.seats],
        }


@dataclass(frozen=True, slots=True)
class DetectorHit:
    name: str
    fired: bool
    count: int
    seats: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        assert self.name in DETECTOR_NAMES
        assert self.count >= 0
        assert (self.count > 0) == self.fired or (self.fired and self.count == 0) is False
        return {
            "fired": self.fired,
            "count": self.count,
            "seats": list(self.seats),
            "evidence": self.evidence,
        }


def is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in HEX_CHARS for ch in value)


def is_no_model_name(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.strip().lower()
    tail = lowered.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return (
        lowered in NO_MODEL_AGENTS
        or tail in NO_MODEL_AGENTS
        or tail.endswith("scriptagent")
        or tail.startswith("scripted-")
    )


def sanitize_surrogates(text: str) -> str:
    assert isinstance(text, str)
    return _SURROGATE_RE.sub("\ufffd", text)


def finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        num = float(value)
    except OverflowError:
        return None
    return num if math.isfinite(num) else None


def nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def as_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def parse_iso_utc(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = finite_float(value)
        if num is None or num < 0 or num > 32_503_680_000:
            return None
        try:
            return datetime.fromtimestamp(num, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stamp_utc(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    utc_val = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return utc_val.isoformat().replace("+00:00", "Z")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token!r}")


def validate_json_tree(
    root: Any,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> None:
    assert max_depth >= 1 and max_nodes >= 1
    stack: list[tuple[Any, int]] = [(root, 0)]
    visited = 0
    while stack:
        visited += 1
        if visited > max_nodes:
            raise ValueError(f"JSON exceeds {max_nodes} nodes")
        node, depth = stack.pop()
        if depth > max_depth:
            raise ValueError(f"JSON exceeds max depth {max_depth}")
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("non-finite float in JSON")
        elif isinstance(node, Mapping):
            if len(node) + visited > max_nodes + 1:
                raise ValueError(f"JSON exceeds {max_nodes} nodes")
            for key, val in node.items():
                if not isinstance(key, str):
                    raise ValueError("non-string JSON key")
                stack.append((val, depth + 1))
        elif isinstance(node, (list, tuple)):
            if len(node) + visited > max_nodes + 1:
                raise ValueError(f"JSON exceeds {max_nodes} nodes")
            for item in node:
                stack.append((item, depth + 1))


def parse_strict_json(
    text: str,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> Any:
    assert isinstance(text, str)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeded parser limit") from exc
    validate_json_tree(parsed, max_depth=max_depth, max_nodes=max_nodes)
    return parsed


def canonical_json(
    value: Any,
    *,
    ensure_ascii: bool = False,
    max_nodes: int = MAX_JSON_NODES,
) -> str:
    validate_json_tree(value, max_nodes=max_nodes)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ensure_ascii,
        allow_nan=False,
    )


def compute_entry_hash(event: Mapping[str, Any], *, ensure_ascii: bool = False) -> str:
    body = {key: val for key, val in event.items() if key not in UNHASHED_KEYS}
    try:
        encoded = canonical_json(body, ensure_ascii=ensure_ascii).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"UTF-8 encode failed: {exc}") from exc
    digest = hashlib.sha256(encoded).hexdigest()
    assert len(digest) == 64
    return digest


def compute_entry_hmac(digest: str, key: str) -> str:
    assert is_sha256_hex(digest) and bool(key)
    mac = hmac.new(key.encode("utf-8"), digest.encode("ascii"), "sha256").hexdigest()
    assert len(mac) == 64
    return mac


def make_alert_id(code: str, trial: str | None, seat: str | None, discriminator: str = "") -> str:
    assert code in ALL_TRIPWIRES
    raw = sanitize_surrogates(f"{code}\0{trial or '-'}\0{seat or '-'}\0{discriminator}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
