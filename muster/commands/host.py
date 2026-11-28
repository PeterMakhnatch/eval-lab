from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

from muster.commands import emit, positive_float, positive_int
from muster.model import InputError
from muster.render import number, table

DESCRIPTION = """Put a command-line agent in every real seat of packaged tasks, with no Docker and no Harbor.

The packaged world runs in this process on a free port of 127.0.0.1; the packaged verifier grades the episode.
AGENT.json is {"about": "...", "argv": [...], "env": {...}, "pass_env": [...]}. argv may use the placeholders
{instruction} {home} {seat} {seatdir} {trial} {task} {world} {token}. The agent gets WORLD, SEAT and SEAT_TOKEN,
PATH HOME USER LANG TMPDIR and the pass_env names from this environment, and the env of the file.
Each seat works in a fresh private directory under --home-root; muster copies its files to <trial>/<seat>/home.
Packaged seat workspace files are placed in that directory. When there is one such file, WORKSPACE_FILE points to it.
The seat directories are not an OS security boundary; use only trusted agent commands.
Each trial is <out>/<task>__a<attempt>/ with host.json and the files that muster contrast, regrade and events read.
Exit status: 0 every trial has a reward, 1 a trial has none (its host.json gives the error), 3 unusable input."""


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("path", type=Path, help="a packaged task directory, or a directory of task directories")
    add("--agent", type=Path, required=True, metavar="AGENT.json", help="the agent command")
    add("--out", type=Path, required=True, metavar="DIR", help="the output directory")
    add("--attempts", type=positive_int, default=1, metavar="K", help="trials for each task (default: 1)")
    add("--concurrency", type=positive_int, default=1, metavar="C", help="trials at the same time (default: 1, at most 16)")
    add("--timeout-sec", type=positive_float, metavar="S", help="wall time of play (default: [agent] timeout_sec of the task)")
    add("--home-root", type=Path, metavar="DIR", help="parent of the seat homes (default: the system temporary directory)")
    add("--dry-run", action="store_true", help="print the trials and the agent command; start nothing")
    add("--json", action="store_true", help="print the report as JSON")


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    out = Path(os.path.realpath(args.out))
    home_root = Path(os.path.realpath(args.home_root or tempfile.gettempdir()))
    if not home_root.is_dir():
        raise InputError(f"--home-root {home_root} is not a directory")
    if _inside(home_root, out) or _inside(out, home_root):
        raise InputError("--home-root and --out must not contain each other: a seat could read other trials")
    return out, home_root


def _row(record: dict[str, Any], skipped: bool) -> dict[str, Any]:
    return {
        "trial": record["trial"], "condition": record.get("condition"), "reward": record.get("reward"),
        "exit_codes": record.get("exit_codes"), "timed_out": record.get("timed_out"),
        "wall_sec": record.get("wall_sec"), "error": record.get("error"), "skipped": skipped,
    }


def _line(row: dict[str, Any]) -> list[str]:
    reward = "-" if row["reward"] is None else number(row["reward"])
    codes = ",".join(f"{seat}={code}" for seat, code in (row["exit_codes"] or {}).items())
    late = ",".join(seat for seat, flag in (row["timed_out"] or {}).items() if flag) or "-"
    wall = "-" if row["wall_sec"] is None else number(row["wall_sec"])
    return [row["trial"], reward, codes, late, wall, "skipped" if row["skipped"] else (row["error"] or "")]


def text(report: dict[str, Any]) -> str:
    lines = [f"output: {report['out']}", f"agent: {report['agent']}", f"home root: {report['home_root']}"]
    if report["dry_run"]:
        for trial in report["planned"]:
            lines.append(f"{trial['trial']}  timeout {number(trial['timeout_sec'])} s")
            lines += [f"  {seat}: {' '.join(argv)}" for seat, argv in trial["argv"].items()]
        lines.append(f"dry run: {len(report['planned'])} trials planned. Nothing was started.")
        return "\n".join(lines) + "\n"
    for path in report["parked"]:
        lines.append(f"moved an incomplete trial to {path}")
    rows = [_line(row) for row in report["trials"]]
    lines += ["", table(["trial", "reward", "exit codes", "timed out", "wall s", "error"], rows, right=(1, 4))]
    lines.append(f"{report['with_reward']} of {len(report['trials'])} trials have a reward.")
    return "\n".join(lines) + "\n"


def _progress(as_json: bool):
    def report(record: dict[str, Any]) -> None:
        if not as_json:
            print("  ".join(str(item) for item in _line(_row(record, False))), flush=True)

    return report


def run(args: argparse.Namespace) -> int:
    from muster.lab import host

    if args.concurrency > host.MAX_CONCURRENCY:
        raise InputError(f"--concurrency is at most {host.MAX_CONCURRENCY}")
    agent = host.read_agent(args.agent)
    out, home_root = _paths(args)
    trials = host.plan(args.path, args.attempts, args.timeout_sec)
    report: dict[str, Any] = {"out": str(out), "agent": str(agent.path), "home_root": str(home_root),
                              "dry_run": args.dry_run}
    if args.dry_run:
        report["planned"] = [{"trial": trial.name, "task": str(trial.task), "timeout_sec": trial.timeout_sec,
                              "argv": host.dry_argv(trial, agent, out)} for trial in trials]
        emit(report, args.json, text)
        return 0
    out.mkdir(parents=True, exist_ok=True)
    pending, done, parked = host.sort_out(out, trials)
    records = host.run_job(pending, agent, out, home_root, args.concurrency, _progress(args.json))
    rows = [_row(record, True) for record in done] + [_row(record, False) for record in records]
    rows.sort(key=lambda row: row["trial"])
    assert len(rows) == len(trials)
    report |= {"parked": [str(path) for path in parked], "trials": rows,
               "with_reward": sum(row["reward"] is not None for row in rows)}
    emit(report, args.json, text)
    return 0 if report["with_reward"] == len(rows) else 1
