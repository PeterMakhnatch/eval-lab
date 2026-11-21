"""The only code that opens trial files (docs/DESIGN.md section 3).

A directory that is not a readable seat trial becomes a `Problem`. Nothing here raises on bad
input files, and nothing guesses a missing value.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from muster.model import Problem, SeatRun, SeatTrial

SEAT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def read_json(path: Path) -> tuple[Any, str | None]:
    """The parsed JSON of a file, or (None, reason)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except OSError as error:
        return None, f"cannot read {path.name}: {error.strerror or error}"
    except (ValueError, RecursionError) as error:
        return None, f"{path.name} is not valid JSON: {error}"


def load_trial(path: Path) -> SeatTrial | Problem:
    """The seat trial in one trial directory."""
    path = Path(path)
    data, error = read_json(path / "result.json")
    return Problem(path, error) if error else _trial(path, data)


def walk(root: Path) -> Iterator[SeatTrial | Problem]:
    """Every seat trial or problem under a directory, in sorted order.

    A trial directory is never searched for more trials. Job directories and directories whose
    name starts with a dot (Harbor's caches) yield nothing themselves.
    """
    root = Path(root)
    if not os.path.isdir(root):
        yield Problem(root, "not a directory")
        return
    yield from _scan(root)


def _scan(directory: Path) -> Iterator[SeatTrial | Problem]:
    result = directory / "result.json"
    if os.path.isfile(result):
        data, error = read_json(result)
        if error:
            yield Problem(directory, error)
        elif isinstance(data, dict) and ("trial_name" in data or "seat_results" in data):
            yield _trial(directory, data)
            return
        elif not (isinstance(data, dict) and ("n_total_trials" in data or "stats" in data)):
            yield Problem(directory, "result.json is not a trial result")
    elif os.path.isfile(directory / "config.json"):
        config, _ = read_json(directory / "config.json")
        if isinstance(config, dict) and "trial_name" in config and "task" in config:
            yield Problem(directory, "trial has no result.json")
            return
    try:
        children = sorted(child for child in directory.iterdir() if not child.name.startswith("."))
    except OSError as error:
        yield Problem(directory, f"cannot list the directory: {error.strerror or error}")
        return
    for child in children:
        if os.path.isdir(child) and not os.path.islink(child):
            yield from _scan(child)


def _trial(path: Path, data: Any) -> SeatTrial | Problem:
    if not isinstance(data, dict):
        return Problem(path, "result.json is not a JSON object")
    seat_results = data.get("seat_results")
    if not isinstance(seat_results, list) or not seat_results:
        return Problem(path, "not a seat trial")
    trial_name, task_name = _text(data.get("trial_name")), _text(data.get("task_name"))
    if trial_name is None or task_name is None:
        return Problem(path, "result.json has no trial_name or no task_name")
    participants = _participants(data.get("config"))
    seats: list[SeatRun] = []
    for index, item in enumerate(seat_results):
        run = _seat_run(path, item, participants)
        if isinstance(run, str):
            return Problem(path, f"seat_results[{index}]: {run}")
        seats.append(run)
    if len({run.seat for run in seats}) != len(seats):
        return Problem(path, "seat_results names a seat twice")
    return SeatTrial(
        path=path,
        trial_name=trial_name,
        task=task_name.rsplit("/", 1)[-1].lower(),
        task_checksum=_text(data.get("task_checksum")),
        rewards=_rewards(_dict(data.get("verifier_result")).get("rewards")),
        reward_valid=data.get("reward_valid") is True,
        exception=_exception(data.get("exception_info")),
        seats=tuple(seats),
        artifacts=path / "artifacts",
        started_at=_text(data.get("started_at")),
        finished_at=_text(data.get("finished_at")),
        task_path=_text(_dict(_dict(data.get("config")).get("task")).get("path")),
        play_finished_at=_text(_dict(data.get("agent_execution")).get("finished_at")),
    )


def event_logs(trial: Path) -> dict[str, Path | None]:
    """The world event logs of a trial directory.

    `graded` is the copy that the verifier wrote next to the reward: the log that was graded.
    `collected` is the task artifact, which Harbor collects before the verifier runs.
    """
    graded = Path(trial) / "verifier" / "events.jsonl"
    artifacts = Path(trial) / "artifacts"
    collected = sorted(artifacts.rglob("events.jsonl")) if os.path.isdir(artifacts) else []
    return {
        "graded": graded if os.path.isfile(graded) else None,
        "collected": collected[0] if collected else None,
    }


def read_events(path: Path) -> tuple[list[dict] | None, str | None]:
    """The events of a world log, or (None, reason). A line that is not an event, or a gap in `seq`, is a reason."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return None, f"cannot read {Path(path).name}: {error.strerror or error}"
    events: list[dict] = []
    for number, line in enumerate((line for line in lines if line.strip()), start=1):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return None, f"line {number} is not valid JSON"
        if not isinstance(event, dict) or event.get("seq") != number or "kind" not in event:
            return None, f"line {number} is not event {number}"
        events.append(event)
    return events, None


def _seat_run(trial: Path, item: Any, participants: dict[str, dict]) -> SeatRun | str:
    """One seat's run, or the reason the record is unusable."""
    if not isinstance(item, dict):
        return "not a JSON object"
    seat, under_test = item.get("seat"), item.get("under_test")
    if not isinstance(seat, str) or not SEAT_NAME.fullmatch(seat):
        return f"seat {seat!r} is not a service name"
    if not isinstance(under_test, bool):
        return "under_test is not true or false"
    participant = participants.get(seat, {})
    info = _dict(item.get("agent_info"))
    agent = _text(info.get("name")) or _text(participant.get("name")) or _text(participant.get("import_path"))
    if agent is None:
        return "no agent name"
    usage, timing = _dict(item.get("agent_result")), _dict(item.get("agent_execution"))
    logs = trial / "seats" / seat / "agent"
    trajectory = logs / "trajectory.json"
    return SeatRun(
        seat=seat,
        under_test=under_test,
        agent=agent,
        model=_model(participant, _dict(info.get("model_info"))),
        policy=_policy(participant),
        exception=_exception(item.get("exception_info")),
        started_at=_text(timing.get("started_at")),
        finished_at=_text(timing.get("finished_at")),
        tokens_in=_int(usage.get("n_input_tokens")),
        tokens_cache=_int(usage.get("n_cache_tokens")),
        tokens_out=_int(usage.get("n_output_tokens")),
        cost_usd=_number(usage.get("cost_usd")),
        trajectory=trajectory if os.path.isfile(trajectory) else None,
        logs=logs,
    )


def _participants(config: Any) -> dict[str, dict]:
    listed = _dict(config).get("participants")
    listed = listed if isinstance(listed, list) else []
    return {p["seat"]: p for p in listed if isinstance(p, dict) and isinstance(p.get("seat"), str)}


def _model(participant: dict, model_info: dict) -> str | None:
    """The model as the participant names it, else as Harbor recorded it."""
    name, provider = _text(model_info.get("name")), _text(model_info.get("provider"))
    recorded = f"{provider}/{name}" if name and provider else name
    return _text(participant.get("model_name")) or recorded


def _policy(participant: dict) -> str | None:
    import_path = _text(participant.get("import_path")) or ""
    script = _text(_dict(participant.get("kwargs")).get("script"))
    if import_path.endswith("ScriptAgent") and script:
        return Path(script).stem or None
    return None


def _rewards(value: Any) -> dict[str, float]:
    """Every finite numeric key the verifier wrote."""
    rewards: dict[str, float] = {}
    for key, item in _dict(value).items():
        number = _number(item)
        if isinstance(key, str) and number is not None:
            rewards[key] = number
    return rewards


def _exception(info: Any) -> str | None:
    """The exception type, or "unknown" when a record exists without one."""
    if info is None:
        return None
    return _text(_dict(info).get("exception_type")) or "unknown"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None
