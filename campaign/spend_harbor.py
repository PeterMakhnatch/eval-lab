from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from campaign import evidence
from campaign.spend_types import (
    ROUTES,
    SOURCES,
    Entry,
    MissingUse,
    Root,
    Trial,
    amount,
    files,
    mapping,
    model_route,
    problem,
    safe,
    stamp,
)

MAX_RESULT_BYTES = 32_000_000
MAX_TRAJECTORY_BYTES = 80_000_000
MAX_CONTEXTS = 256
DEFAULT_SOURCE = "token-list-price"
OPENROUTER = "openrouter"
OPENROUTER_KEY = "OPENROUTER_API_KEY"
TRIAL_PARTS = frozenset({"agent", "seats", "steps"})
TRAJECTORY_TOKENS = ("total_prompt_tokens", "total_cached_tokens", "total_completion_tokens")
MAX_TOKENS = 10**15
Tokens = tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Context:
    result: dict[str, Any]
    model_info: dict[str, Any]
    agent: dict[str, Any]
    fallback_agent: dict[str, Any]
    execution: dict[str, Any]
    trajectory: Path | None


@dataclass(frozen=True)
class Located:
    root: Root
    path: Path
    problems: list[str]

    def flag(self, reason: str) -> None:
        self.problems.append(problem(self.root, self.path, reason))


def trials(root: Root, problems: list[str]) -> Iterator[Trial]:
    assert root.kind == "harbor-trials", root.kind
    assert (root.cost_source or DEFAULT_SOURCE) in SOURCES
    for path in files(root, problems, "result.json", tree_only=True):
        if _skipped(root, path):
            continue
        at = Located(root, path, problems)
        record = _load(at, MAX_RESULT_BYTES)
        if record is None:
            continue
        found = _trial(at, record)
        if found is not None:
            assert found.entries, found.identity
            yield found


def _skipped(root: Root, path: Path) -> bool:
    parts = path.relative_to(root.path).parts[:-1]
    return any(part in TRIAL_PARTS for part in parts)


def _load(at: Located, limit: int, *, optional: bool = False) -> dict[str, Any] | None:
    assert limit > 0
    try:
        info = at.path.stat()
        if not stat.S_ISREG(info.st_mode):
            at.flag("not a regular file")
            return None
        if info.st_size > limit:
            at.flag("file larger than the read limit")
            return None
        with at.path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            at.flag("file larger than the read limit")
            return None
        data = json.loads(raw, parse_float=Decimal)
    except FileNotFoundError:
        if not optional:
            at.flag("file disappeared before it could be read")
        return None
    except OSError:
        at.flag("unreadable file")
        return None
    except (ValueError, RecursionError):
        at.flag("invalid JSON")
        return None
    if not isinstance(data, dict):
        at.flag("JSON is not an object")
        return None
    return data


def _trial(at: Located, record: dict[str, Any]) -> Trial | None:
    if "trial_name" not in record:
        if isinstance(record.get("stats"), dict):
            return None
        at.flag("result.json is neither a Harbor trial nor a Harbor job")
        return None
    if mapping(mapping(record.get("config")).get("source_trial")):
        return None
    name = _text(record.get("id")) or _text(record.get("trial_name"))
    if name is None:
        at.flag("trial without id or trial_name")
        return None
    started = _time(at, record.get("started_at"), "malformed started_at")
    finished = _time(at, record.get("finished_at"), "malformed finished_at")
    contexts = _contexts(at, record)
    if contexts is None:
        return None
    moment = finished or started
    if moment is None:
        if any(context.result.get("cost_usd") is not None for context in contexts):
            at.flag("trial with cost but without started_at or finished_at")
        return None
    built = [_entry(at, context, moment) for context in contexts]
    entries = tuple(entry for entry, _ in built)
    missing = _missing_use(at, record, contexts, built, moment)
    return Trial((name, started or moment), moment, entries, at.path.parent, missing)


def _missing_use(at: Located, record: dict[str, Any], contexts: list[Context],
                 built: list[tuple[Entry, Tokens]], moment: datetime) -> tuple[MissingUse, ...]:
    assert len(contexts) == len(built) and built
    seats = record.get("seat_results")
    if not seats:
        entry, traced = built[0]
        if any(found.usd is not None for found, _ in built) or not evidence._team_cost_unknown(record):
            return ()
        tokens = _result_tokens(mapping(record.get("agent_result"))) + traced
        return (MissingUse(moment, None, entry.route, _model(entry), _team_reason(record), tokens),)
    assert isinstance(seats, list) and len(seats) == len(built)
    found = [
        MissingUse(entry.time, _seat_label(seat.get("seat")), entry.route, _model(entry), _seat_reason(seat),
                   _result_tokens(context.result) + traced)
        for seat, context, (entry, traced) in zip(seats, contexts, built, strict=True)
        if entry.usd is None and evidence._seat_shows_model_use(seat)
    ]
    config = mapping(record.get("config"))
    participants = config.get("participants")
    for participant in participants if isinstance(participants, list) else []:
        if evidence._absent_model_participants([participant], seats):
            agent = mapping(participant)
            name = _text(agent.get("model_name"))
            context = Context({}, {}, agent, mapping(config.get("agent")), {}, None)
            route, label = model_route(_prefixed(at.root, name), _provider(at.root, context, name))
            found.append(MissingUse(moment, _seat_label(agent.get("seat")), route, None if label == "?" else label,
                                    "configured model without a seat result", ()))
    assert len(found) <= 2 * MAX_CONTEXTS
    return tuple(found)


def _model(entry: Entry) -> str | None:
    return None if entry.model == "?" else entry.model


def _seat_label(value: Any) -> str | None:
    return None if _text(value) is None else safe(value)


def _seat_reason(seat: dict[str, Any]) -> str:
    assert evidence._seat_shows_model_use(seat)
    if evidence._raw_agent_cost(seat) is not None:
        return "cost_usd is not a valid amount"
    if evidence._positive_tokens(seat):
        return "positive token count"
    return "model info on a seat that ran"


def _team_reason(record: dict[str, Any]) -> str:
    assert evidence._team_cost_unknown(record)
    if evidence._raw_agent_cost(record) is not None:
        return "cost_usd is not a valid amount"
    if evidence._has_model_info(record):
        return "model info"
    if evidence._positive_tokens(record):
        return "positive token count"
    if evidence._configured_model(mapping(record.get("config")).get("agent")):
        return "configured model"
    return "no agent information"


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < MAX_TOKENS else None


def _counted(prefix: str, counters: dict[str, Any], names: tuple[str, ...]) -> Tokens:
    found = ((name, _count(counters.get(name))) for name in names)
    return tuple((f"{prefix}.{name}", count) for name, count in found if count is not None)


def _result_tokens(result: dict[str, Any]) -> Tokens:
    found = _counted("result", result, evidence.TOKEN_FIELDS)
    usage = result.get("model_usage")
    if not isinstance(usage, dict) or not usage or len(usage) > MAX_CONTEXTS:
        return found
    sums: dict[str, int] = {}
    for counters in usage.values():
        for name, count in _counted("model_usage", mapping(counters), evidence.TOKEN_FIELDS):
            sums[name] = sums.get(name, 0) + count
    return found + tuple(sums.items())


def _contexts(at: Located, record: dict[str, Any]) -> list[Context] | None:
    config = mapping(record.get("config"))
    outer_info = mapping(mapping(record.get("agent_info")).get("model_info"))
    outer_agent = mapping(config.get("agent"))
    seats = record.get("seat_results")
    steps = record.get("step_results")
    for label, group in (("seat_results", seats), ("step_results", steps)):
        if group is not None and not isinstance(group, list):
            at.flag(f"malformed {label}")
            return None
        if group is not None and len(group) > MAX_CONTEXTS:
            at.flag(f"too many {label}")
            return None
    if seats:
        return _seat_contexts(at, seats, config, outer_info, outer_agent)
    if steps and record.get("agent_result") is None:
        return _step_contexts(at, steps, outer_info, outer_agent)
    result = mapping(record.get("agent_result"))
    execution = mapping(record.get("agent_execution"))
    trajectory = at.path.parent / "agent" / "trajectory.json"
    return [Context(result, outer_info, outer_agent, {}, execution, trajectory)]


def _seat_contexts(
    at: Located,
    seats: list[Any],
    config: dict[str, Any],
    outer_info: dict[str, Any],
    outer_agent: dict[str, Any],
) -> list[Context] | None:
    participants = config.get("participants")
    if participants is None:
        participants = []
    if not isinstance(participants, list) or len(participants) > MAX_CONTEXTS:
        at.flag("malformed participants")
        return None
    by_seat = {_text(mapping(item).get("seat")): mapping(item) for item in participants}
    lone = len(seats) == 1
    found = []
    for seat in seats:
        if not isinstance(seat, dict):
            at.flag("malformed seat result")
            return None
        name = _text(seat.get("seat"))
        if name is None:
            at.flag("seat result without seat name")
        info = mapping(mapping(seat.get("agent_info")).get("model_info"))
        if not info and lone:
            info = outer_info
        agent = by_seat.get(name, {}) if name is not None else {}
        if not agent and lone:
            agent = outer_agent
        trajectory: Path | None = None
        if name is not None and _component(name):
            trajectory = at.path.parent / "seats" / name / "agent" / "trajectory.json"
        result = mapping(seat.get("agent_result"))
        execution = mapping(seat.get("agent_execution"))
        found.append(Context(result, info, agent, outer_agent, execution, trajectory))
    return found


def _step_contexts(
    at: Located, steps: list[Any], outer_info: dict[str, Any], outer_agent: dict[str, Any]
) -> list[Context] | None:
    found = []
    for step in steps:
        if not isinstance(step, dict):
            at.flag("malformed step result")
            return None
        execution = mapping(step.get("agent_execution"))
        result = mapping(step.get("agent_result"))
        found.append(Context(result, outer_info, outer_agent, {}, execution, None))
    return found


def _entry(at: Located, context: Context, moment: datetime) -> tuple[Entry, Tokens]:
    source = at.root.cost_source or DEFAULT_SOURCE
    raw = context.result.get("cost_usd")
    usd = amount(raw)
    if raw is not None and usd is None:
        at.flag("malformed cost_usd")
    channel = "result"
    model = (_text(context.model_info.get("name")) or _text(context.model_info.get("model_name"))
             or _text(context.agent.get("model_name")))
    traced: Tokens = ()
    if raw is None and context.trajectory is not None:
        usd, named, traced = _trajectory(at, context.trajectory)
        channel = "trajectory" if usd is not None else channel
        model = model or named
    executed = _time(at, context.execution.get("finished_at"), "malformed seat finished_at")
    route, label = model_route(_prefixed(at.root, model), _provider(at.root, context, model))
    assert route in ROUTES, route
    return Entry(executed or moment, route, label, usd, source, channel), traced


def _provider(root: Root, context: Context, model: str | None) -> str | None:
    if root.provider_override:
        return root.provider_override
    stated = _text(context.model_info.get("provider"))
    if stated is not None:
        return stated
    configured = _text(context.agent.get("model_name")) or ""
    if configured.startswith(OPENROUTER + "/") or (model or "").startswith(OPENROUTER + "/"):
        return OPENROUTER
    key = context.agent.get("model_api_key_name")
    key = key or context.fallback_agent.get("model_api_key_name")
    return OPENROUTER if key == OPENROUTER_KEY else None


def _trajectory(at: Located, path: Path) -> tuple[Decimal | None, str | None, Tokens]:
    found = Located(at.root, path, at.problems)
    data = _load(found, MAX_TRAJECTORY_BYTES, optional=True)
    if data is None:
        return None, None, ()
    named = _text(mapping(data.get("agent")).get("model_name"))
    metrics = mapping(data.get("final_metrics"))
    raw = metrics.get("total_cost_usd")
    usd = amount(raw)
    if raw is not None and usd is None:
        found.flag("malformed final_metrics.total_cost_usd")
    return usd, named, _counted("trajectory", metrics, TRAJECTORY_TOKENS)


def _prefixed(root: Root, model: str | None) -> str | None:
    if model is None or not root.model_prefix or model.startswith(root.model_prefix):
        return model
    return root.model_prefix + model


def _time(at: Located, value: Any, reason: str) -> datetime | None:
    if value is None:
        return None
    found = stamp(value)
    if found is None:
        at.flag(reason)
    return found


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _component(name: str) -> bool:
    return name not in (".", "..") and not any(mark in name for mark in ("/", "\\", "\x00"))
