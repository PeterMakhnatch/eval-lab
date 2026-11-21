from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from muster import load, regrade, render
from muster.commands import emit
from muster.model import InputError, SeatTrial

DESCRIPTION = """Recompute the rewards of finished trials from their event logs, and compare them with the record.

Exit status: 0 every replayed reward equals the record, 1 at least one differs, 3 unusable input or a trial
that cannot be compared (no task, no scorer, no usable log, no recorded reward, or a replay error).
This command imports each task's tests/verify.py. Use it on tasks that you trust."""


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("directory", type=Path, help="a trial, a job, or any directory that holds trials")
    add("--task-root", type=Path, metavar="DIR", help="look for each task directory here, by its name")
    add("--json", action="store_true", help="print the report as JSON")


def _task_dir(trial: SeatTrial, root: Path | None) -> Path | None:
    recorded = Path(trial.task_path) if trial.task_path else None
    if root is not None and recorded is not None and os.path.isdir(root / recorded.name):
        return root / recorded.name
    if root is not None and os.path.isdir(root / trial.task):
        return root / trial.task
    return recorded if recorded is not None and os.path.isdir(recorded) else None


def report(directory: Path, task_root: Path | None = None) -> dict[str, Any]:
    items = list(load.walk(Path(directory)))
    trials = [item for item in items if isinstance(item, SeatTrial)]
    if not trials:
        raise InputError(f"no seat trial under {directory}")
    scorers: dict[Path, tuple] = {}
    rows: list[dict[str, Any]] = []
    for trial in trials:
        row: dict[str, Any] = {"trial": trial.trial_name, "task": trial.task, "roster": trial.roster_key()}
        task = _task_dir(trial, task_root)
        if task is None:
            rows.append({**row, "status": "no-task", "differences": {}, "late_actions": []})
            continue
        if task not in scorers:
            scorers[task] = regrade.load_score(task)
        score, instance, reason = scorers[task]
        if score is None or instance is None:
            rows.append(
                {**row, "status": "no-score", "reason": reason, "differences": {}, "late_actions": []}
            )
            continue
        logs, events = load.event_logs(trial.path), {}
        for name, path in logs.items():
            events[name], error = load.read_events(path) if path is not None else (None, None)
            if error:
                row[f"{name}_log_problem"] = error
        rows.append(
            {
                **row,
                **regrade.regrade_trial(
                    trial.rewards, events["graded"], events["collected"], score, instance
                ),
            }
        )
    assert len(rows) == len(trials)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "directory": str(directory),
        "trials": len(rows),
        "counts": dict(sorted(counts.items())),
        "trials_with_late_actions": sum(1 for row in rows if row["late_actions"]),
        "rows": rows,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    return report(args.directory, args.task_root)


def text(result: dict[str, Any]) -> str:
    counts = ", ".join(f"{count} {status}" for status, count in result["counts"].items())
    lines = [f"{result['trials']} trials replayed from their event logs: {counts}", ""]
    shown = [row for row in result["rows"] if row["status"] != "match" or row["late_actions"]]
    if shown:
        table = []
        for row in shown:
            differ = "; ".join(
                f"{key}: recorded {render.number(pair[0])}, replayed {render.number(pair[1])}"
                for key, pair in row["differences"].items()
            )
            late = ", ".join(f"{item['seat']} {item['kind']}" for item in row["late_actions"])
            table.append([row["trial"], row["status"], differ or row.get("reason", ""), late])
        lines += [render.table(["trial", "status", "difference", "late actions"], table), ""]
    if result["counts"].get("mismatch"):
        lines.append("A mismatch means: the recorded reward is not what the event log gives.")
    if result["trials_with_late_actions"]:
        lines.append(
            f"{result['trials_with_late_actions']} trials have seat actions after play ended that the verifier counted."
        )
    if not shown:
        lines.append("Every recorded reward equals the reward that its event log gives.")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    result = build(args)
    emit(result, args.json, text)
    return exit_status(result["counts"])


def exit_status(counts: dict[str, int]) -> int:
    assert counts and all(count > 0 for count in counts.values())
    if counts.get("mismatch"):
        return 1
    return 0 if set(counts) == {"match"} else 3
