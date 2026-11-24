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


def _agent_cost(result: Any) -> float | None:
    agent = result.get("agent_result") if isinstance(result, dict) else None
    raw = agent.get("cost_usd") if isinstance(agent, dict) else None
    try:
        cost = finite(raw)
    except OverflowError:
        return None
    return cost if cost is not None and cost >= 0 else None


def _unknown(totals: Counter[str], seats: int) -> None:
    assert seats >= 0
    if seats:
        totals["unknown_trials"] += 1
        totals["seats_without_cost"] += seats


def _result_cost(path: Path, totals: Counter[str], problems: list[str], expected_seats: int) -> tuple[float, bool]:
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
        if team is not None:
            return team, True
        _unknown(totals, configured or expected_seats)
        return 0.0, True
    assert len(seats) <= MAX_TRIALS
    totals["seats"] += len(seats)
    known, subtotal = 0, 0.0
    for seat in seats.values() if isinstance(seats, dict) else seats:
        cost = _agent_cost(seat)
        if cost is not None:
            known += 1
            subtotal += cost
    missing = max(len(seats), configured) - known
    _unknown(totals, missing)
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
            amount, is_trial_result = _result_cost(Path(dirpath) / "result.json", totals, problems, expected_seats)
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
