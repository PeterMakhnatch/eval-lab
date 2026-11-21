"""Exact measures over the world event logs of finished trials: who acted, who talked, who received what.

Each directory is one arm. The table has one row for each arm, with means over its trials.
No model reads any text: every number comes from the request log of the world.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from muster import events, load, render
from muster.commands import emit
from muster.model import InputError, SeatTrial

# (column, key in events.summarize, what the mean is taken over)
COLUMNS = (
    ("actions", "actions"),
    ("posts", "posts"),
    ("fetches", "fetches"),
    ("probes", "probes"),
    ("talk share", "communication_share"),
    ("unread acts", "actions_with_unread"),
    ("never fetched", "never_fetched"),
    ("duplicates", "duplicate_actions"),
    ("informed", "informed_duplicates"),
    ("same 1st act", "identical_first_action"),
    ("idle seats", "never_acted"),
    ("late", "late"),
)


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add(
        "directories",
        type=Path,
        nargs="+",
        metavar="DIR",
        help="one directory for each arm (NAME=DIR names it)",
    )
    add("--trials", action="store_true", help="also print one row for each trial")
    add("--json", action="store_true", help="print the report as JSON")


def _arm(spec: Path) -> tuple[str, Path]:
    text = str(spec)
    if "=" in text and not Path(text).exists():
        name, _, path = text.partition("=")
        return name, Path(path)
    return Path(text).name, Path(text)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, list):
        return float(len(value))
    return float(value) if isinstance(value, int | float) else None


def measure(trial: SeatTrial) -> dict[str, Any] | None:
    """The measures of one trial, from the graded log when it exists, else from the collected log."""
    logs = load.event_logs(trial.path)
    path = logs["graded"] or logs["collected"]
    if path is None:
        return None
    log, error = load.read_events(path)
    if error or log is None:
        return None
    return events.summarize(log, seats=[run.seat for run in trial.seats])


def report(directories: list[Path]) -> dict[str, Any]:
    arms = []
    for name, directory in map(_arm, directories):
        trials = [item for item in load.walk(directory) if isinstance(item, SeatTrial)]
        if not trials:
            raise InputError(f"no seat trial under {directory}")
        rows = []
        for trial in trials:
            measures = measure(trial)
            if measures is not None:
                rows.append({"trial": trial.trial_name, "task": trial.task, **measures})
        means = {}
        for _, key in COLUMNS:
            values = [number for row in rows if (number := _number(row[key])) is not None]
            means[key] = sum(values) / len(values) if values else None
        arms.append(
            {
                "arm": name,
                "directory": str(directory),
                "trials": len(trials),
                "with_log": len(rows),
                "means": means,
                "rows": rows,
            }
        )
    return {"arms": arms}


def build(args: argparse.Namespace) -> dict[str, Any]:
    result = report(args.directories)
    result["show_trials"] = bool(args.trials)
    return result


def _cell(key: str, value: float | None) -> str:
    if value is None:
        return "-"
    if key in ("communication_share", "identical_first_action"):
        return f"{100 * value:.0f}%"
    return f"{value:.1f}"


def text(result: dict[str, Any]) -> str:
    headers = ["arm", "trials", *(column for column, _ in COLUMNS)]
    table = [
        [
            arm["arm"],
            f"{arm['with_log']}/{arm['trials']}",
            *(_cell(key, arm["means"][key]) for _, key in COLUMNS),
        ]
        for arm in result["arms"]
    ]
    lines = [render.table(headers, table, right=range(1, len(headers))), ""]
    lines += [
        "Means over the trials of an arm. One request to the world is one event.",
        "actions       accepted requests that are not messages",
        "posts         messages sent; fetches: reads of the message list",
        "probes        requests for a route that the world does not have",
        "talk share    posts and fetches as a share of posts, fetches and actions",
        "unread acts   actions taken while a message of another seat was posted and not yet received",
        "never fetched pairs of (message, other seat) where that seat never received the message",
        "duplicates    actions that repeat the act of another seat; informed: the actor had received",
        "              a message that the first actor posted after its act",
        "same 1st act  share of trials where 2 or more seats start with the same act",
        "idle seats    seats with no action; late: requests after the world closed",
    ]
    if result.get("show_trials"):
        for arm in result["arms"]:
            rows = [
                [row["trial"], *(_cell(key, _number(row[key])) for _, key in COLUMNS)] for row in arm["rows"]
            ]
            lines += [
                "",
                arm["arm"],
                render.table(["trial", *(column for column, _ in COLUMNS)], rows, right=range(1, 13)),
            ]
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    emit(build(args), args.json, text)
    return 0
