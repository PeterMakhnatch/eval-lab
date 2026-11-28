from __future__ import annotations

import argparse
import json
import math
import re
import stat
import sys
from pathlib import Path
from typing import Any, cast

from .harbor_seats import SeatEvidenceError, read_seat_evidence

_SCHEMA = re.compile(r"ATIF-v1\.\d+")
_SOURCES = frozenset({"system", "user", "agent", "tool"})
_MAX_TRAJ_BYTES = 128 * 1024 * 1024
_USAGE = (
    ("n_input_tokens", "total_prompt_tokens"),
    ("n_cache_tokens", "total_cached_tokens"),
    ("n_output_tokens", "total_completion_tokens"),
    ("cost_usd", "total_cost_usd"),
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def _parse_finite_float(token: str) -> float:
    val = float(token)
    if not math.isfinite(val):
        raise ValueError("non-finite JSON float")
    return val


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_TRAJ_BYTES:
            return None
        record = json.loads(
            path.read_bytes(),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except (OSError, ValueError, RecursionError):
        return None
    return cast(dict[str, Any], record) if isinstance(record, dict) else None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        val = float(value)
    except OverflowError:
        return None
    return val if math.isfinite(val) else None


def _steps(doc: dict[str, Any], issues: list[str]) -> tuple[int, dict[str, int], int]:
    steps = doc.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        issues.append("invalid_steps")
        return 0, {}, 0
    by_source: dict[str, int] = {}
    tool_calls = 0
    previous = -1
    for raw in cast(list[dict[str, Any]], steps):
        step_id = raw.get("step_id")
        if type(step_id) is not int or step_id <= previous:
            issues.append("non_monotonic_step_id")
        else:
            previous = step_id
        source = raw.get("source")
        if isinstance(source, str) and source in _SOURCES:
            by_source[source] = by_source.get(source, 0) + 1
        else:
            issues.append("invalid_step_source")
        calls = raw.get("tool_calls")
        if isinstance(calls, list):
            tool_calls += len(cast(list[object], calls))
        elif calls is not None:
            issues.append("invalid_tool_calls")
    return len(steps), dict(sorted(by_source.items())), tool_calls


def _exact_int(value: object) -> int | None:
    if type(value) is int:
        return value
    return (
        int(value) if type(value) is float and math.isfinite(value) and value.is_integer() else None
    )


def _mismatches(seat: dict[str, Any], doc: dict[str, Any]) -> list[str]:
    metrics = doc.get("final_metrics")
    if not isinstance(metrics, dict):
        return []
    found: list[str] = []
    for result_key, atif_key in _USAGE:
        raw_exp = seat.get(result_key)
        raw_act = cast(dict[str, Any], metrics).get(atif_key)
        if raw_act is None:
            continue
        if result_key != "cost_usd":
            act_i = _exact_int(raw_act)
            if act_i is None or act_i < 0:
                found.append(f"usage_mismatch:{result_key}")
                continue
            if raw_exp is not None:
                exp_i = _exact_int(raw_exp)
                if exp_i is None or exp_i < 0 or exp_i != act_i:
                    found.append(f"usage_mismatch:{result_key}")
            continue
        actual = _number(raw_act)
        if actual is None or actual < 0.0:
            found.append(f"usage_mismatch:{result_key}")
            continue
        if raw_exp is not None:
            expected = _number(raw_exp)
            if (
                expected is None
                or expected < 0.0
                or not math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-9)
            ):
                found.append(f"usage_mismatch:{result_key}")
    return found


def _inspect_seat(root: Path, seat: dict[str, Any]) -> dict[str, object]:
    row: dict[str, object] = {"seat": seat["seat"], "under_test": seat["under_test"]}
    trajectory = cast(dict[str, Any], seat["trajectory"])
    status = trajectory["status"]
    if status == "missing":
        return {**row, "status": "absent", "issues": []}
    if status != "present":
        return {**row, "status": "invalid", "issues": ["unusable_trajectory_file"]}
    doc = _read_document(root / cast(str, trajectory["path"]))
    if doc is None:
        return {**row, "status": "invalid", "issues": ["invalid_json"], "usage_mismatches": []}

    issues: list[str] = []
    version = doc.get("schema_version")
    if not isinstance(version, str) or not _SCHEMA.fullmatch(version):
        issues.append("invalid_schema_version")
    if not _text(doc.get("session_id")):
        issues.append("missing_session_id")
    agent = doc.get("agent")
    agent_name = cast(dict[str, Any], agent).get("name") if isinstance(agent, dict) else None
    if not _text(agent_name):
        issues.append("invalid_agent")
    n_steps, by_source, n_tool_calls = _steps(doc, issues)
    raw_metrics = doc.get("final_metrics")
    if raw_metrics is not None and not isinstance(raw_metrics, dict):
        issues.append("invalid_final_metrics")
    mismatches = _mismatches(seat, doc)
    if issues:
        return {
            **row,
            "status": "invalid",
            "issues": sorted(set(issues)),
            "usage_mismatches": mismatches,
        }
    return {
        **row,
        "status": "valid",
        "schema_version": version,
        "agent_name": agent_name,
        "n_steps": n_steps,
        "steps_by_source": by_source,
        "n_tool_calls": n_tool_calls,
        "issues": [],
        "usage_mismatches": mismatches,
    }


def inspect_seat_trajectories(path: Path) -> dict[str, object]:
    receipt = read_seat_evidence(path)
    if receipt["issues"] != []:
        raise SeatEvidenceError("seat evidence has issues")
    try:
        root = path.resolve(strict=True)
    except OSError:
        raise SeatEvidenceError("cannot resolve path") from None
    trials: list[dict[str, object]] = []
    for trial in cast(list[dict[str, Any]], receipt["trials"]):
        seats = [_inspect_seat(root, seat) for seat in cast(list[dict[str, Any]], trial["seats"])]
        trials.append(
            {"trial_name": trial["trial_name"], "trial_id": trial["trial_id"], "seats": seats}
        )
    rows = [cast(dict[str, Any], s) for t in trials for s in cast(list[object], t["seats"])]

    def count(status: str) -> int:
        return sum(1 for r in rows if r["status"] == status)

    return {
        "schema": "harbor-seat-trajectories/v1",
        "job_id": receipt["job_id"],
        "n_trials": len(trials),
        "n_seats": len(rows),
        "n_valid": count("valid"),
        "n_absent": count("absent"),
        "n_invalid": count("invalid"),
        "n_usage_mismatches": sum(1 for r in rows if r.get("usage_mismatches")),
        "trials": trials,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.evidence.harbor_seat_trajectories",
        description="Validate ATIF trajectories of an Olam Harbor seat run.",
    )
    parser.add_argument("path", type=Path, metavar="PATH", help="raw Harbor job or trial directory")
    args = parser.parse_args(argv)
    try:
        report = inspect_seat_trajectories(args.path)
    except SeatEvidenceError as exc:
        print(f"harbor seat trajectories: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, sort_keys=True, indent=2))
    return 1 if report["n_invalid"] or report["n_usage_mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
