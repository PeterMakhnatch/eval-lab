from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

_SEAT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ABSOLUTE_PREFIX = re.compile(r"(?:[/\\]|[A-Za-z]:[/\\]|file:)", re.IGNORECASE)
_TOKEN_FIELDS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")
_MAX_DOC_BYTES = 64 * 1024 * 1024


class SeatEvidenceError(ValueError):
    pass


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def _parse_finite_float(token: str) -> float:
    val = float(token)
    if not math.isfinite(val):
        raise ValueError("non-finite JSON float")
    return val


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _read_document(path: Path) -> tuple[dict[str, Any], bytes] | None:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise SeatEvidenceError(f"{path.name} is not a regular file")
        if st.st_size > _MAX_DOC_BYTES:
            raise SeatEvidenceError(f"{path.name} exceeds size limit")
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise SeatEvidenceError(f"cannot read {path.name}") from None
    try:
        record = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except (ValueError, RecursionError):
        raise SeatEvidenceError(f"invalid JSON in {path.name}") from None
    if not isinstance(record, dict):
        raise SeatEvidenceError(f"{path.name} must contain a JSON object")
    return record, raw


def _version_one(record: dict[str, Any]) -> None:
    version = record.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise SeatEvidenceError("unsupported result schema version")


def _object(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _label(value: object) -> str | None:
    if (
        isinstance(value, str)
        and value.strip()
        and value.isprintable()
        and not _ABSOLUTE_PREFIX.match(value.lstrip())
    ):
        return value
    return None


def _required_label(record: dict[str, Any], key: str) -> str:
    value = _label(record.get(key))
    if value is None:
        raise SeatEvidenceError(f"missing or invalid {key}")
    return value


def _exception_type(record: dict[str, Any]) -> str | None:
    name = _object(record.get("exception_info")).get("exception_type")
    return name if isinstance(name, str) and name.isidentifier() else None


def _finished(record: dict[str, Any]) -> bool:
    value = record.get("finished_at")
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _artifact(root: Path, trial: Path, seat: str, filename: str) -> dict[str, str | None]:
    path = trial
    parts = ("seats", seat, "agent", filename)
    try:
        for part in parts[:-1]:
            path /= part
            if not stat.S_ISDIR(path.lstat().st_mode):
                return {"status": "unreadable", "path": None}
        path /= parts[-1]
        if stat.S_ISREG(path.lstat().st_mode) and os.access(path, os.R_OK):
            return {"status": "present", "path": path.relative_to(root).as_posix()}
    except FileNotFoundError:
        return {"status": "missing", "path": None}
    except OSError:
        pass
    return {"status": "unreadable", "path": None}


def _seat_index(rows: list[Any], issues: set[str], source: str) -> dict[str, dict[str, Any] | None]:
    index: dict[str, dict[str, Any] | None] = {}
    for item in rows:
        row = _object(item)
        name = row.get("seat")
        if not isinstance(name, str) or not _SEAT_NAME.fullmatch(name):
            issues.add(f"invalid_{source}_seat")
            continue
        if name in index:
            issues.add(f"duplicate_{source}")
            index[name] = None
        else:
            index[name] = row
    return index


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _seat_cost(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        cost = float(value)
    except OverflowError:
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _project_seat(
    root: Path, trial: Path, name: str, result: dict[str, Any], issues: set[str]
) -> dict[str, Any]:
    agent = _object(result.get("agent_info"))
    model = _object(agent.get("model_info"))
    usage = _object(result.get("agent_result"))
    under_test = result.get("under_test")
    if result and not isinstance(under_test, bool):
        issues.add("invalid_under_test")
    row = {
        "seat": name,
        "under_test": under_test if isinstance(under_test, bool) else None,
        "agent_name": _label(agent.get("name")),
        "model_name": _label(model.get("name")),
        "model_provider": _label(model.get("provider")),
        "exception_type": _exception_type(result),
        "cost_usd": _seat_cost(usage.get("cost_usd")),
        "trajectory": _artifact(root, trial, name, "trajectory.json"),
        "oracle": _artifact(root, trial, name, "oracle.txt"),
    }
    for key in _TOKEN_FIELDS:
        if _nonnegative_int(value := usage.get(key)):
            row[key] = value
    return row


def _rewards(record: dict[str, Any]) -> dict[str, int | float] | None:
    rewards = _object(record.get("verifier_result")).get("rewards")
    if rewards is None:
        return None
    if not isinstance(rewards, dict) or any(
        _ABSOLUTE_PREFIX.match(key.lstrip())
        or not key.isprintable()
        or type(value) not in (int, float)
        or (isinstance(value, float) and not math.isfinite(value))
        for key, value in rewards.items()
    ):
        raise SeatEvidenceError("invalid recorded rewards")
    return rewards


def _project_trial(root: Path, trial: Path, record: dict[str, Any], raw: bytes) -> dict[str, Any]:
    _version_one(record)
    config = _object(record.get("config"))
    _version_one(config)
    participants = config.get("participants")
    results = record.get("seat_results")
    if record.get("step_results") or not (participants or results):
        raise SeatEvidenceError("only seat trials are supported; non-seat or mixed job")
    if any(value is not None and not isinstance(value, list) for value in (participants, results)):
        raise SeatEvidenceError("invalid seat roster")

    issues: set[str] = set()
    declared = _seat_index(participants or [], issues, "participant")
    actual = _seat_index(results or [], issues, "seat")
    if not declared:
        issues.add("missing_participants")
    if declared.keys() - actual.keys():
        issues.add("missing_seat")
    if actual.keys() - declared.keys():
        issues.add("unknown_seat")

    seats = [
        _project_seat(root, trial, name, actual.get(name) or {}, issues)
        for name in sorted(declared)
    ]
    if seats and all(row["under_test"] is False for row in seats):
        issues.add("missing_under_test")
    if not _finished(record):
        issues.add("missing_trial_finish")

    reward_valid = record.get("reward_valid")
    if not isinstance(reward_valid, bool):
        raise SeatEvidenceError("missing or invalid reward_valid")
    rewards = _rewards(record)
    if reward_valid and not rewards:
        issues.add("missing_rewards")

    costs = [row["cost_usd"] for row in seats if row["cost_usd"] is not None]
    known_sum = sum(costs) if costs else None
    if known_sum is not None and not math.isfinite(known_sum):
        issues.add("invalid_cost_total")
        known_sum = None
    return {
        "trial_id": _required_label(record, "id"),
        "trial_name": _required_label(record, "trial_name"),
        "task_name": _required_label(record, "task_name"),
        "task_checksum": _required_label(record, "task_checksum"),
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "reward_valid": reward_valid,
        "rewards": rewards,
        "exception_type": _exception_type(record),
        "seats": seats,
        "cost_usd": {
            "known_sum": known_sum,
            "reported_seats": len(costs),
            "expected_seats": len(declared),
            "complete": not issues and len(costs) == len(declared),
        },
        "issues": sorted(issues),
    }


def _collect_lock(directory: Path, origins: dict[str, set[str]], issues: set[str]) -> None:
    try:
        lock = _read_document(directory / "lock.json")
    except SeatEvidenceError:
        issues.add("invalid_lock")
        return
    if lock is None:
        return
    harbor = _object(lock[0].get("harbor"))
    for source, target in (("version", "version"), ("git_commit_hash", "revision")):
        if value := _label(harbor.get(source)):
            origins.setdefault(target, set()).add(value)


def _directories(root: Path) -> list[Path]:
    try:
        return sorted(child for child in root.iterdir() if stat.S_ISDIR(child.lstat().st_mode))
    except OSError:
        raise SeatEvidenceError("cannot list job directory") from None


def read_seat_evidence(path: Path) -> dict[str, object]:
    try:
        root = path.resolve(strict=True)
        is_dir = root.is_dir()
    except (OSError, RuntimeError, ValueError):
        raise SeatEvidenceError("cannot resolve evidence directory") from None
    if not is_dir:
        raise SeatEvidenceError("PATH must be a directory")
    document = _read_document(root / "result.json")
    if document is None:
        raise SeatEvidenceError("missing result.json")
    record, raw = document
    _version_one(record)
    issues: set[str] = set()
    origins: dict[str, set[str]] = {}
    trials: list[dict[str, Any]] = []
    job_id = None

    if "stats" in record or "n_total_trials" in record:
        job_id = _required_label(record, "id")
        stats, expected = record.get("stats"), record.get("n_total_trials")
        if not isinstance(stats, dict) or not _nonnegative_int(expected):
            raise SeatEvidenceError("invalid job trial counts")
        if not _finished(record):
            issues.add("missing_job_finish")
        for child in _directories(root):
            child_document = _read_document(child / "result.json")
            if child_document is not None:
                trials.append(_project_trial(root, child, *child_document))
                _collect_lock(child, origins, issues)
        if not trials:
            raise SeatEvidenceError("job has no immediate trial results")
        completed = stats.get("n_completed_trials", expected)
        if not _nonnegative_int(completed) or completed != expected or len(trials) != expected:
            issues.add("job_trial_count_mismatch")
        for key in ("trial_id", "trial_name"):
            if len({trial[key] for trial in trials}) != len(trials):
                raise SeatEvidenceError(f"duplicate {key} in job")
    else:
        trials.append(_project_trial(root, root, record, raw))

    _collect_lock(root, origins, issues)
    harbor: dict[str, str] = {}
    for key, values in origins.items():
        if len(values) == 1:
            harbor[key] = next(iter(values))
        else:
            issues.add("conflicting_harbor_lock")
    for trial in trials:
        issues.update(trial["issues"])
    receipt: dict[str, object] = {
        "schema": "harbor-seat-evidence/v1",
        "job_id": job_id,
        "trials": sorted(trials, key=lambda row: (row["trial_name"], row["trial_id"])),
        "issues": sorted(issues),
    }
    if harbor:
        receipt["harbor"] = harbor
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.evidence.harbor_seats",
        description="Inventory an Olam Harbor seat run without opening artifacts.",
    )
    parser.add_argument("path", type=Path, metavar="PATH", help="raw Harbor job or trial directory")
    args = parser.parse_args(argv)
    try:
        receipt = read_seat_evidence(args.path)
    except SeatEvidenceError as exc:
        print(f"harbor seat evidence: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, allow_nan=False, sort_keys=True, indent=2))
    return 1 if receipt["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
