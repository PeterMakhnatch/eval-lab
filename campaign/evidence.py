from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campaign.plan import RERUN_TAG
from campaign.system import Walk, WalkError

MAX_WALK_DIRS = 2_000_000
MAX_TRIALS = 1_000_000
MAX_JSON_BYTES = 64_000_000
TOKEN_FIELDS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")


@dataclass(frozen=True)
class Cost:
    recorded_usd: float
    trials: int
    seats: int
    seats_without_cost: int
    unreadable_results: int
    unknown_trials: int
    problems: tuple[str, ...]

    def charged(self, unknown_seat_usd: float) -> float:
        assert unknown_seat_usd > 0
        return round(self.recorded_usd + self.seats_without_cost * unknown_seat_usd, 6)

    def record(self) -> dict[str, Any]:
        record = asdict(self)
        if self.recorded_usd == 0 and self.unknown_trials:
            record["recorded_usd"] = None
        return record


def read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def reward(trial: Path) -> float | None:
    data = read_json(trial / "verifier" / "reward.json")
    return finite(data.get("reward")) if isinstance(data, dict) else None


def is_trial(path: Path) -> bool:
    return "__" in path.name and path.is_dir()


def trial_dirs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    found = sorted(path for path in folder.iterdir() if is_trial(path))
    assert len(found) <= MAX_TRIALS
    return found


def instance(trial: Path) -> str:
    return trial.name.split("__", 1)[0]


def rerun_folders(root: Path, arm: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"{arm}{RERUN_TAG}*") if path.is_dir())


def task_instances(tasks: Path) -> list[str]:
    if (tasks / "task.toml").is_file():
        return [tasks.name]
    if not tasks.is_dir():
        return []
    found = sorted(path.name for path in tasks.iterdir() if (path / "task.toml").is_file())
    assert len(found) <= MAX_TRIALS
    return found


def failure_kind(trial: Path, detected: frozenset[str]) -> str | None:
    if trial.name in detected:
        return "detected"
    if reward(trial) is not None:
        return None
    result_path = trial / "result.json"
    if not result_path.is_file():
        return "no-result"
    result = read_json(result_path)
    if not isinstance(result, dict):
        return "unreadable-result"
    if (trial / "verifier" / "reward.json").is_file():
        return "invalid-reward"
    info = result.get("exception_info") or {}
    if not isinstance(info, dict):
        return "no-reward"
    if "429" in str(info.get("exception_message", "")):
        return "pull-limit-429"
    kind = info.get("exception_type")
    return kind if isinstance(kind, str) and kind else "no-reward"


def folder_counts(folder: Path, detected: frozenset[str]) -> dict[str, Any]:
    trials = trial_dirs(folder)
    kinds: Counter[str] = Counter()
    measured: Counter[str] = Counter()
    for trial in trials:
        kind = failure_kind(trial, detected)
        if kind is None:
            measured[instance(trial)] += 1
        else:
            kinds[kind] += 1
    with_reward = sum(measured.values())
    assert with_reward + sum(kinds.values()) == len(trials)
    return {
        "started": len(trials),
        "with_reward": with_reward,
        "unmeasured": dict(sorted(kinds.items())),
        "measured_by_instance": dict(sorted(measured.items())),
    }


def _number(raw: Any) -> float | None:
    try:
        return finite(raw)
    except OverflowError:
        return None


def _raw_agent_cost(result: Any) -> Any:
    agent = result.get("agent_result") if isinstance(result, dict) else None
    return agent.get("cost_usd") if isinstance(agent, dict) else None


def _agent_cost(result: Any) -> float | None:
    cost = _number(_raw_agent_cost(result))
    return cost if cost is not None and cost >= 0 else None


def _count_tokens(counters: Any) -> float:
    if not isinstance(counters, dict):
        return 0.0
    total = 0.0
    for field in TOKEN_FIELDS:
        count = _number(counters.get(field))
        if count is not None and count > 0:
            total += count
    return total


def _token_count(result: Any) -> float:
    agent = result.get("agent_result") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return 0.0
    direct = _count_tokens(agent)
    if direct:
        return direct
    usage = agent.get("model_usage")
    if not isinstance(usage, dict):
        return 0.0
    assert len(usage) <= MAX_TRIALS
    return sum((_count_tokens(counters) for counters in usage.values()), 0.0)


def _positive_tokens(result: Any) -> bool:
    return _token_count(result) > 0


def _has_model_info(result: Any) -> bool:
    info = result.get("agent_info") if isinstance(result, dict) else None
    return isinstance(info, dict) and bool(info.get("model_info"))


def _seat_shows_model_use(seat: Any) -> bool:
    if not isinstance(seat, dict):
        return False
    ran = bool(seat.get("agent_result")) or isinstance(seat.get("agent_execution"), dict)
    return _raw_agent_cost(seat) is not None or (ran and _has_model_info(seat)) or _positive_tokens(seat)


def _configured_model(agent: Any) -> bool:
    model = agent.get("model_name") if isinstance(agent, dict) else None
    return isinstance(model, str) and bool(model)


def _model_name(result: dict[str, Any], configured: Any, free_models: frozenset[str]) -> str | None:
    info = result.get("agent_info")
    model = info.get("model_info") if isinstance(info, dict) else None
    if isinstance(model, dict):
        provider = model.get("provider")
        for field in ("name", "model_name"):
            name = model.get(field)
            if isinstance(name, str) and name:
                if name in free_models:
                    return name
                if (isinstance(provider, str) and _configured_model(configured)
                        and configured["model_name"] == f"{provider}/{name}"):
                    return configured["model_name"]
                return name
    return configured["model_name"] if _configured_model(configured) else None


def _unpriced_model(result: dict[str, Any], tokens: float, configured: Any, free_models: frozenset[str]) -> str | None:
    usage = result["agent_result"].get("model_usage")
    if isinstance(usage, dict):
        assert len(usage) <= MAX_TRIALS
        named_tokens = 0.0
        for name, counters in usage.items():
            count = _count_tokens(counters)
            if not count:
                continue
            if name not in free_models:
                return name or _model_name(result, configured, free_models) or "an unnamed model"
            named_tokens += count
        if named_tokens >= tokens:
            return None
    name = _model_name(result, configured, free_models)
    return None if name in free_models else name or "an unnamed model"


def _zero_cost_problem(
    result: dict[str, Any], tokens: float, configured: Any, free_models: frozenset[str],
) -> str | None:
    assert tokens > 0
    name = _unpriced_model(result, tokens, configured, free_models)
    if name is None:
        return None
    count = int(tokens) if tokens.is_integer() else tokens
    return f"cost 0.0 with {count} tokens: no price for {name if name.isprintable() else repr(name)}?"


def _entries(collection: list[Any] | dict[str, Any]) -> Iterator[tuple[str | None, Any]]:
    assert len(collection) <= MAX_TRIALS
    if isinstance(collection, dict):
        yield from collection.items()
    else:
        yield from ((None, entry) for entry in collection)


def _seat_name(entry: Any) -> str | None:
    name = entry.get("seat") if isinstance(entry, dict) else None
    return name if isinstance(name, str) and name else None


def _absent_model_participants(participants: Any, seats: list[Any] | dict[str, Any]) -> int:
    if not isinstance(participants, list | dict) or not participants:
        return 0
    represented: set[str] = set()
    for key, seat in _entries(seats):
        if key:
            represented.add(key)
        name = _seat_name(seat)
        if name:
            represented.add(name)
    absent = sum(_configured_model(participant) and key not in represented
                 and _seat_name(participant) not in represented for key, participant in _entries(participants))
    assert 0 <= absent <= len(participants)
    return absent


def _participants_by_seat(participants: Any) -> dict[str, Any]:
    configured: dict[str, Any] = {}
    if isinstance(participants, list | dict):
        for key, entry in _entries(participants):
            name = _seat_name(entry) or key
            if name and _configured_model(entry):
                configured[name] = entry
    return configured


def _team_cost_unknown(result: dict[str, Any]) -> bool:
    if _raw_agent_cost(result) is not None or _has_model_info(result) or _positive_tokens(result):
        return True
    config = result.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    if _configured_model(agent):
        return True
    info = result.get("agent_info")
    if isinstance(info, dict) and info:
        return False
    return not (isinstance(agent, dict) and (agent.get("name") or agent.get("import_path")))


def _unknown(totals: Counter[str], seats: int) -> None:
    assert seats >= 0
    if seats:
        totals["unknown_trials"] += 1
        totals["seats_without_cost"] += seats


def _seat_costs(
    path: Path, seats: list[Any] | dict[str, Any], participants: Any, problems: list[str], free_models: frozenset[str],
) -> tuple[int, int, float]:
    known, unknown, subtotal = 0, 0, 0.0
    configured = None
    for key, seat in _entries(seats):
        cost = _agent_cost(seat)
        if cost == 0 and (tokens := _token_count(seat)):
            assert isinstance(seat, dict)
            if configured is None:
                configured = _participants_by_seat(participants)
            problem = _zero_cost_problem(seat, tokens, configured.get(_seat_name(seat) or key or ""), free_models)
            if problem is not None:
                problems.append(f"{path}: {problem}")
                unknown += 1
                continue
        if cost is not None:
            known += 1
            subtotal += cost
        elif _seat_shows_model_use(seat):
            unknown += 1
    return known, unknown, subtotal


def _result_cost(
    path: Path, totals: Counter[str], problems: list[str], expected_seats: int, free_models: frozenset[str],
) -> tuple[float, bool]:
    result = read_json(path)
    if not isinstance(result, dict):
        if "__" in path.parent.name and path.exists():
            totals["unreadable_results"] += 1
            _unknown(totals, expected_seats)
        return 0.0, False
    if "trial_name" not in result:
        return 0.0, False
    totals["trials"] += 1
    team = _agent_cost(result)
    seats = result.get("seat_results")
    config = result.get("config")
    participants = config.get("participants") if isinstance(config, dict) else None
    configured = len(participants) if isinstance(participants, list | dict) else 0
    if not isinstance(seats, list | dict) or not seats:
        if team == 0 and (tokens := _token_count(result)):
            agent = config.get("agent") if isinstance(config, dict) else None
            problem = _zero_cost_problem(result, tokens, agent, free_models)
            if problem is not None:
                problems.append(f"{path}: {problem}")
                _unknown(totals, expected_seats)
                return 0.0, True
        if team is not None:
            return team, True
        _unknown(totals, expected_seats if _team_cost_unknown(result) else 0)
        return 0.0, True
    totals["seats"] += len(seats)
    known, unknown, subtotal = _seat_costs(path, seats, participants, problems, free_models)
    _unknown(totals, unknown + _absent_model_participants(participants, seats))
    missing = max(len(seats), configured) - known
    if team is None:
        return subtotal, True
    difference = subtotal - team
    if round(difference, 12) > 0.01 or (not missing and round(-difference, 12) > 0.01):
        problems.append(f"{path}: seat cost ${subtotal:.6f} and team cost ${team:.6f} disagree")
        return max(subtotal, team), True
    return max(subtotal, team) if missing else subtotal, True


def _walk_errors(root: Path) -> WalkError:
    def handle(error: OSError) -> None:
        vanished = isinstance(error, FileNotFoundError | NotADirectoryError)
        if vanished and error.filename is not None and Path(error.filename) != root:
            return
        raise error

    return handle


def _os_walk(root: Path, onerror: WalkError) -> Iterator[tuple[str, list[str], list[str]]]:
    return os.walk(root, onerror=onerror)


def distinct_roots(roots: tuple[Path, ...] | list[Path]) -> list[Path]:
    real = sorted({Path(os.path.realpath(root)) for root in roots})
    return [root for root in real if not any(other in root.parents for other in real)]


def scan_cost(
    roots: tuple[Path, ...] | list[Path], walk: Walk = _os_walk, *, expected_seats: int = 1,
    free_models: frozenset[str] = frozenset(),
) -> Cost:
    assert expected_seats >= 1
    totals: Counter[str] = Counter()
    problems: list[str] = []
    recorded = 0.0
    visited = 0
    for root in distinct_roots(roots):
        for dirpath, dirnames, filenames in walk(root, _walk_errors(root)):
            visited += 1
            assert visited <= MAX_WALK_DIRS, "too many folders under the cap roots"
            dirnames.sort()
            if "result.json" not in filenames:
                continue
            amount, is_trial_result = _result_cost(
                Path(dirpath) / "result.json", totals, problems, expected_seats, free_models,
            )
            recorded += amount
            if is_trial_result:
                dirnames[:] = []
    return Cost(
        recorded_usd=round(recorded, 6),
        trials=totals["trials"],
        seats=totals["seats"],
        seats_without_cost=totals["seats_without_cost"],
        unreadable_results=totals["unreadable_results"],
        unknown_trials=totals["unknown_trials"],
        problems=tuple(problems),
    )


def parse_utc(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def finished_trials(root: Path, since: datetime) -> list[tuple[datetime, str, bool]]:
    done: list[tuple[datetime, str, bool]] = []
    if not root.is_dir():
        return done
    for folder in sorted(root.iterdir()):
        for trial in trial_dirs(folder):
            result = read_json(trial / "result.json")
            finished = parse_utc(result.get("finished_at")) if isinstance(result, dict) else None
            if finished is not None and finished >= since:
                done.append((finished, str(trial), reward(trial) is not None))
            assert len(done) <= MAX_TRIALS
    done.sort()
    return done


def streak(root: Path, need: int, since: datetime) -> tuple[bool, int]:
    assert need >= 1
    done = finished_trials(root, since)
    last = [ok for _, _, ok in done[-need:]]
    return len(last) == need and not any(last), len(done)
