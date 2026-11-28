from __future__ import annotations

import argparse
import copy
import os
import stat
from pathlib import Path
from typing import Any

from muster import load, regrade, render
from muster.commands import emit
from muster.model import InputError, SeatTrial

MAX_CHAIN_BYTES = regrade.MAX_CHAIN_BYTES
_READ_CHUNK = 65_536

DESCRIPTION = """Recompute the rewards of finished trials from their event logs, and compare them with the record.

Exit status: 0 every replayed reward equals the record, 1 at least one differs or a hash-chained (kit v2)
event log is broken, 3 unusable input or a trial that cannot be compared (no task, no scorer, no usable
log, no recorded reward, or a replay error).
This command imports each task's tests/verify.py. Use it on tasks that you trust."""


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("directory", type=Path, help="a trial, a job, or any directory that holds trials")
    add("--task-root", type=Path, metavar="DIR", help="look for each task directory here, by its name")
    add(
        "--hmac-key-env",
        metavar="NAME",
        help="also check each kit v2 log line's hmac with the key in this environment variable",
    )
    add("--json", action="store_true", help="print the report as JSON")


def _task_dir(trial: SeatTrial, root: Path | None) -> Path | None:
    recorded = Path(trial.task_path) if trial.task_path else None
    if root is not None and recorded is not None and os.path.isdir(root / recorded.name):
        return root / recorded.name
    if root is not None and os.path.isdir(root / trial.task):
        return root / trial.task
    return recorded if recorded is not None and os.path.isdir(recorded) else None


def _event_logs(trial_path: Path) -> dict[str, Path | None]:
    logs = load.event_logs(trial_path)
    graded = Path(trial_path) / "verifier" / "events.jsonl"
    if logs["graded"] is None and os.path.lexists(graded):
        logs = {**logs, "graded": graded}
    return logs


def _read_bounded_utf8(path: Path, max_bytes: int = MAX_CHAIN_BYTES) -> tuple[str | None, str | None]:
    assert max_bytes >= 1
    try:
        info = os.lstat(path)
    except OSError as error:
        return None, f"cannot read {path.name}: {error.strerror or error}"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{path.name} is not a regular file"
    if info.st_size > max_bytes:
        return None, f"{path.name} exceeds {max_bytes} bytes"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return None, f"cannot read {path.name}: {error.strerror or error}"
    chunks: list[bytes] = []
    total = 0
    max_steps = max_bytes // _READ_CHUNK + 3
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, f"{path.name} is not a regular file"
        for _ in range(max_steps):
            want = max(1, min(_READ_CHUNK, max_bytes + 1 - total))
            piece = os.read(descriptor, want)
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > max_bytes:
                return None, f"{path.name} exceeds {max_bytes} bytes"
    except OSError as error:
        return None, f"cannot read {path.name}: {error.strerror or error}"
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{path.name} is not UTF-8 text"


def _check_one_chain(
    path: Path, hmac_key: str | None, cap: int, *, allow_unhashed: bool = False
) -> tuple[bool | None, str | None]:
    content, read_error = _read_bounded_utf8(path, cap)
    if read_error is not None or content is None:
        return False, read_error
    if allow_unhashed and hmac_key is not None:
        base_status, base_problem, _ = regrade.check_event_chain(content, None)
        if base_status is None:
            return None, None
        if base_status is False:
            return False, base_problem
    status, problem, _ = regrade.check_event_chain(content, hmac_key)
    return status, problem


def _chain(
    logs: dict[str, Path | None], hmac_key: str | None, max_bytes: int | None = None
) -> dict[str, Any]:
    graded, collected = logs["graded"], logs["collected"]
    primary = graded or collected
    if primary is None:
        return {}
    cap = MAX_CHAIN_BYTES if max_bytes is None else max_bytes
    status, problem = _check_one_chain(primary, hmac_key, cap)
    if graded is not None and collected is not None and status is not False:
        c_status, c_problem = _check_one_chain(collected, hmac_key, cap, allow_unhashed=True)
        if c_status is False:
            status, problem = False, f"collected: {c_problem}"
        elif status is None and c_status is True:
            status = True
    if status is None:
        return {}
    shown: dict[str, Any] = {"chain_ok": status, "chain_hmac_checked": hmac_key is not None}
    return shown | ({"chain_problem": problem} if problem else {})


def _safe_read_events(path: Path | None) -> tuple[list[dict] | None, str | None]:
    if path is None:
        return None, None
    try:
        info = os.lstat(path)
    except OSError as error:
        return None, f"cannot read {path.name}: {error.strerror or error}"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{path.name} is not a regular file"
    if info.st_size > MAX_CHAIN_BYTES:
        return None, f"{path.name} exceeds {MAX_CHAIN_BYTES} bytes"
    return load.read_events(path)


def report(
    directory: Path, task_root: Path | None = None, hmac_key: str | None = None
) -> dict[str, Any]:
    items = list(load.walk(Path(directory)))
    trials = [item for item in items if isinstance(item, SeatTrial)]
    if not trials:
        raise InputError(f"no seat trial under {directory}")
    scorers: dict[Path, tuple] = {}
    rows: list[dict[str, Any]] = []
    for trial in trials:
        row: dict[str, Any] = {"trial": trial.trial_name, "task": trial.task, "roster": trial.roster_key()}
        logs = _event_logs(trial.path)
        row.update(_chain(logs, hmac_key))
        task = _task_dir(trial, task_root)
        if task is None:
            rows.append({**row, "status": "no-task", "differences": {}, "late_actions": []})
            continue
        if task not in scorers:
            scorers[task] = regrade.load_score(task)
        score, instance, reason = scorers[task]
        if score is None or not isinstance(instance, dict):
            rows.append(
                {**row, "status": "no-score", "reason": reason or "invalid task instance", "differences": {}, "late_actions": []}
            )
            continue
        events: dict[str, list[dict] | None] = {}
        for name, path in logs.items():
            events[name], error = _safe_read_events(path)
            if error:
                row[f"{name}_log_problem"] = error
        outcome = regrade.regrade_trial(
            trial.rewards, events["graded"], events["collected"], score, copy.deepcopy(instance), hmac_key=hmac_key
        )
        log_prob = row.get("graded_log_problem") or row.get("collected_log_problem")
        if log_prob and outcome["status"] != "no-log":
            outcome = {**outcome, "status": "corrupt-log", "reason": log_prob}
        rows.append({**row, **outcome})
    assert len(rows) == len(trials)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    chained = [row for row in rows if "chain_ok" in row]
    return {
        "directory": str(directory),
        "trials": len(rows),
        "counts": dict(sorted(counts.items())),
        "trials_with_late_actions": sum(1 for row in rows if row["late_actions"]),
        "chain_checked": len(chained),
        "chain_failures": sum(1 for row in chained if not row["chain_ok"]),
        "rows": rows,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    key = None
    if args.hmac_key_env:
        key = os.environ.get(args.hmac_key_env)
        if not key:
            raise InputError(f"--hmac-key-env {args.hmac_key_env}: that variable is not set or is empty")
    return report(args.directory, args.task_root, key)


def text(result: dict[str, Any]) -> str:
    counts = ", ".join(f"{count} {status}" for status, count in result["counts"].items())
    lines = [f"{result['trials']} trials replayed from their event logs: {counts}", ""]
    shown = [
        row
        for row in result["rows"]
        if row["status"] != "match" or row["late_actions"] or row.get("chain_ok") is False
    ]
    if shown:
        table = []
        for row in shown:
            differ = "; ".join(
                f"{key}: recorded {render.number(pair[0])}, replayed {render.number(pair[1])}"
                for key, pair in row["differences"].items()
            )
            late = ", ".join(f"{item['seat']} {item['kind']}" for item in row["late_actions"])
            detail = differ or row.get("reason", "") or row.get("chain_problem", "")
            table.append([row["trial"], row["status"], detail, late])
        lines += [render.table(["trial", "status", "difference", "late actions"], table), ""]
    if result["counts"].get("mismatch"):
        lines.append("A mismatch means: the recorded reward is not what the event log gives.")
    if result["trials_with_late_actions"]:
        lines.append(
            f"{result['trials_with_late_actions']} trials have seat actions after play ended that the verifier counted."
        )
    if result["chain_checked"]:
        lines.append(
            f"hash chain: {result['chain_checked']} logs checked, {result['chain_failures']} broken."
        )
    if not shown:
        lines.append("Every recorded reward equals the reward that its event log gives.")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    result = build(args)
    emit(result, args.json, text)
    status = exit_status(result["counts"])
    return 1 if status == 0 and result["chain_failures"] else status


def exit_status(counts: dict[str, int]) -> int:
    assert counts and all(count > 0 for count in counts.values())
    if counts.get("mismatch") or counts.get("corrupt-log"):
        return 1
    return 0 if set(counts) == {"match"} else 3
