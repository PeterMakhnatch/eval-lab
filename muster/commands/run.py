"""Run one roster of real agents on a task, or on every task of a directory, as one Harbor job.

ROSTER.json is {"participants": [{"seat": "p1", "name": "terminus-2", "model_name": "..."}, ...]},
or {"each": {...}} for the same settings in every seat. --out is an ordinary Harbor job directory.
The config and harbor's output are next to it, as <out>.job.json and <out>.harbor.log.
API keys come from the caller's environment. muster writes none of them to a file.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from muster import attribution, load, roster, runner
from muster.commands import add_run_options, emit
from muster.model import InputError, SeatTrial
from muster.render import number, table

SENSITIVE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
TEMPLATE = re.compile(r"\$\{[^}]+\}")
SHOWN = ("seat", "name", "import_path", "model_name")  # the participant settings that a report shows


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="a task directory, or a directory of task directories")
    parser.add_argument(
        "--roster", type=Path, required=True, metavar="ROSTER.json", help="who is in each seat"
    )
    add_run_options(parser)


def participants(data: Any, seats: tuple[str, ...]) -> list[dict[str, Any]]:
    """The Harbor `participants` list of a roster file, in seat order."""
    if not isinstance(data, dict) or ("participants" in data) == ("each" in data):
        raise InputError('the roster file needs "participants" or "each", and not both')
    each, listed = data.get("each"), data.get("participants")
    if isinstance(each, dict) and "seat" not in each:
        listed = [{"seat": seat, **each} for seat in seats]
    shaped = isinstance(listed, list) and all(
        isinstance(p, dict) and isinstance(p.get("seat"), str) for p in listed
    )
    if not shaped:
        raise InputError(
            '"participants" is a list of objects with "seat"; "each" is one object without "seat"'
        )
    if sorted(p["seat"] for p in listed) != sorted(seats):
        named = ", ".join(p["seat"] for p in listed)
        raise InputError(f"the roster names the seats {named}; the task has {', '.join(seats)}")
    for participant in listed:
        seat, env = participant["seat"], participant.get("env", {})
        if not participant.get("name") and not participant.get("import_path"):
            raise InputError(f"participant {seat} has no name and no import_path")
        for key, value in env.items() if isinstance(env, dict) else []:
            if SENSITIVE.search(key) and not TEMPLATE.fullmatch(str(value)):
                raise InputError(
                    f"participant {seat}: env {key} holds a value; write ${{{key}}} and export it"
                )
    return sorted(listed, key=lambda p: seats.index(p["seat"]))


def build(args: argparse.Namespace) -> dict[str, Any]:
    tasks = [roster.read_task(path) for path in roster.task_dirs(args.path)]
    for task in tasks:
        if set(task.seats) != set(tasks[0].seats):
            raise InputError(f"the tasks do not have the same seats: {tasks[0].path} and {task.path}")
    data, error = load.read_json(args.roster)
    if error:
        raise InputError(f"{args.roster}: {error}")
    listed = participants(data, tasks[0].seats)
    out = Path(os.path.abspath(args.out or Path("runs") / Path(os.path.abspath(args.path)).name))
    config = roster.job_config(
        name=out.name,
        jobs_dir=out.parent,
        tasks=[task.path for task in tasks],
        participants=listed,
        attempts=args.attempts,
        concurrency=args.concurrency,
        timeout_multiplier=args.timeout_multiplier,
    )
    report: dict[str, Any] = {
        "dry_run": args.dry_run,
        "job_dir": str(out),
        "planned_trials": len(tasks) * args.attempts,
        "agent_timeout_multiplier": args.timeout_multiplier,
        "participants": [{key: p.get(key) for key in SHOWN} for p in listed],
    }
    if args.dry_run:
        return {**report, "config": str(runner.write_config(config, out.parent, f"{out.name}."))}
    harbor = runner.find_harbor(args.harbor)
    timeout = roster.job_timeout_sec(tasks, args.attempts, args.concurrency, args.timeout_multiplier)
    outcome = runner.run_job(config, out.parent, harbor, timeout_sec=timeout, stem=f"{out.name}.")
    items = list(load.walk(out)) if os.path.isdir(out) else []
    trials = [item for item in items if isinstance(item, SeatTrial)]
    valid, rewards = attribution.reward_means(trials)  # plain means: intervals and tests belong to tidegauge
    return report | {
        "config": str(outcome.config),
        "log": str(outcome.log),
        "harbor_status": outcome.returncode,
        "harbor_timed_out": outcome.timed_out,
        "seconds": round(outcome.seconds, 1),
        "trials": len(trials),
        "valid_rewards": valid,
        "unreadable": len(items) - len(trials),
        "rewards": rewards,
        "exceptions": dict(sorted(Counter(t.exception for t in trials if t.exception).items())),
    }


def text(report: dict[str, Any]) -> str:
    who = ", ".join(
        f"{p['seat']}={p['name'] or p['import_path']}" + (f"/{p['model_name']}" if p["model_name"] else "")
        for p in report["participants"]
    )
    lines = [f"job directory: {report['job_dir']}", f"roster: {who}", f"config: {report['config']}"]
    planned = report["planned_trials"]
    if report["dry_run"]:
        return "\n".join([*lines, f"dry run: {planned} trials planned. Nothing was started."]) + "\n"
    how = f"ended with status {report['harbor_status']}"
    if report["harbor_timed_out"]:
        how = "did not end in time"
    lines.append(f"harbor {how} after {number(report['seconds'])} s; its output is in {report['log']}")
    counts = f"{report['valid_rewards']} with a valid reward, {report['unreadable']} unreadable"
    lines.append(f"trials: {report['trials']} of {planned}, {counts}")
    if report["exceptions"]:
        listed = ", ".join(f"{name} {count}" for name, count in report["exceptions"].items())
        lines.append(f"exceptions: {listed}")
    if report["rewards"]:
        rows = [[key, number(cell["mean"]), cell["n"]] for key, cell in report["rewards"].items()]
        lines += ["", table(["reward key", "mean", "n"], rows, right=(1, 2))]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    report = build(args)
    emit(report, args.json, text)
    if report["dry_run"]:
        return 0
    complete = report["trials"] == report["planned_trials"] and not report["harbor_timed_out"]
    return 0 if report["harbor_status"] == 0 and complete else 1
