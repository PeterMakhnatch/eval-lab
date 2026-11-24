from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from tidegauge import render
from tidegauge.commands._common import add_stall
from tidegauge.load import read_running, walk
from tidegauge.model import Job, Problem, Running, Trial
from tidegauge.timeline import stamp

DESCRIPTION = """``tidegauge live``: a snapshot of a job that is still running.

The command reads the job once and writes nothing. A trial with a result.json is
finished (it has a valid reward) or failed (it has none). A trial directory with a
config.json and no result.json is running. A result.json that cannot be read yet
counts as unreadable: Harbor can be writing it. For each running trial the report
gives its age (from the time of its config.json) and, for each seat, the time since
the last step in its trajectory. A seat whose last step marks the task complete is
done; a seat with an exit-code.txt has exited; the other seats with a trajectory are
playing. A playing seat is flagged when its time without a step is above
--stall-seconds. Seat trajectories are on the host during the run only when the
environment writes them there; a seat without a trajectory shows no time.

Exit status: 0 = no seat above the threshold, 1 = at least 1 seat above it.
"""
HELP = "snapshot a running job: finished, running and failed trials, silent seats"
MAX_RUNNING = 4096


def moment(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job", metavar="JOB", help="the directory of the running Harbor job")
    add_stall(parser)
    parser.add_argument("--now", type=moment, help="measure ages at this time, for replays (default: the clock)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def _seconds(later: datetime, earlier: datetime | None) -> float | None:
    return None if earlier is None else round(max(0.0, (later - earlier).total_seconds()), 1)


def running_row(running: Running, now: datetime, stall_seconds: float) -> dict:
    seats = []
    for progress in running.seats:
        silent = _seconds(now, progress.last_step_at)
        seats.append({
            "seat": progress.seat,
            "state": progress.state,
            "agent_steps": progress.agent_steps,
            "last_step_at": stamp(progress.last_step_at),
            "silent_s": silent,
            "stalled": progress.state == "playing" and silent is not None and silent > stall_seconds,
        })
    return {
        "trial": running.path.name,
        "started_at": stamp(running.started_at),
        "age_s": _seconds(now, running.started_at),
        "seats": seats,
        "stalled": [seat["seat"] for seat in seats if seat["stalled"]],
    }


def _scan(job: Path) -> tuple[Counter, list[Path], int | None, bool]:
    counts: Counter = Counter()
    running: list[Path] = []
    planned, job_unreadable = None, False
    home = job.resolve()
    for item in walk(job):
        if isinstance(item, Trial):
            counts["finished" if item.valid and item.reward is not None else "failed"] += 1
            if not (item.valid and item.reward is not None):
                counts[f"failed:{item.not_started or item.exception or 'no valid reward'}"] += 1
        elif isinstance(item, Job):
            planned = item.n_total_trials if item.path.resolve() == home else planned
        elif isinstance(item, Problem) and "did not finish" in item.reason:
            running.append(item.path)
        elif isinstance(item, Problem) and item.path.parent.resolve() == home:
            job_unreadable = True
        else:
            counts["unreadable"] += 1
    return counts, running, planned, job_unreadable


def build(job: Path, now: datetime, stall_seconds: float) -> dict:
    if not job.is_dir():
        raise FileNotFoundError(f"not a directory: {job}")
    counts, running, planned, job_unreadable = _scan(job)
    rows = [running_row(read_running(path), now, stall_seconds) for path in sorted(running)[:MAX_RUNNING]]
    seen = counts["finished"] + counts["failed"] + counts["unreadable"] + len(running)
    return {
        "command": "live",
        "job": str(job),
        "now": stamp(now),
        "stall_seconds": stall_seconds,
        "planned": planned,
        "job_result_unreadable": job_unreadable,
        "finished": counts["finished"],
        "failed": counts["failed"],
        "failed_by_kind": {k.partition(":")[2]: v for k, v in sorted(counts.items()) if k.startswith("failed:")},
        "running": len(running),
        "unreadable": counts["unreadable"],
        "pending": planned - seen if planned is not None and planned >= seen else None,
        "stalled_seats": sum(len(row["stalled"]) for row in rows),
        "running_trials": rows,
    }


def text(report: dict) -> str:
    out = [
        f"job {report['job']} at {report['now']}; stall threshold {report['stall_seconds']:g} s",
        f"planned {report['planned'] if report['planned'] is not None else 'unknown'}, finished {report['finished']}, "
        f"failed {report['failed']}, running {report['running']}, unreadable {report['unreadable']}, "
        f"pending {report['pending'] if report['pending'] is not None else 'unknown'}",
    ]
    if report["failed_by_kind"]:
        out.append("failed: " + ", ".join(f"{k} ×{v}" for k, v in report["failed_by_kind"].items()))
    rows = []
    for row in report["running_trials"]:
        playing = [s for s in row["seats"] if s["state"] == "playing"]
        silent = [s["silent_s"] for s in playing if s["silent_s"] is not None]
        ended = sum(1 for s in row["seats"] if s["state"] in ("done", "exited"))
        flagged = ", ".join(f"{s['seat']} {s['silent_s']:.0f} s" for s in row["seats"] if s["stalled"])
        rows.append([
            render.shorten(row["trial"], 40),
            "-" if row["age_s"] is None else f"{row['age_s']:.0f}",
            len(playing),
            ended,
            len(row["seats"]) - len(playing) - ended,
            f"{max(silent):.0f}" if silent else "-",
            flagged or "-",
        ])
    if rows:
        headers = ["running trial", "age s", "playing", "done", "no data", "longest silence s", "stalled"]
        out += ["", render.table(headers, rows, right=(1, 2, 3, 4, 5))]
    out.append(
        f"{render.count(report['stalled_seats'], 'playing seat')} above {report['stall_seconds']:g} s without a step."
    )
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    report = build(Path(args.job), args.now or datetime.now(UTC), args.stall_seconds)
    print(render.to_json(report) if args.json else text(report))
    return 1 if report["stalled_seats"] else 0
