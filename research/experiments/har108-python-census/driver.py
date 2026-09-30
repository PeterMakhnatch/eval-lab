#!/usr/bin/env python3
"""Run the HAR-108 census nops in waves under a Daytona spend cap.

Loop: settle this checkout's queue, measure spend so far (``qualify-collect``
into a scratch table over every ``har108-nop-*`` job), and, while the
remaining budget covers a wave at ``WAVE_COST_USD`` per nop, submit and
approve the next pool slice (pool.json order) and dispatch every approved
har108 spec. A wave whose setup-error rate exceeds ``STOP_SETUP_RATE`` stops
the loop for review (HAR-108 rule 3).

Usage (from the worktree root): driver.py [--cap USD] [--wave N] [--parallel N]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import SETUP_ERROR  # noqa: E402

RUNS = ROOT / "runs"
SCRATCH = Path("/private/tmp/har108-spend.parquet")
#: Upper-tail per-nop cost (code nops so far: mean $0.0079, p90 about $0.012).
WAVE_COST_USD = 0.012
STOP_SETUP_RATE = 0.5
#: Smaller waves (the cap's tail) are too noisy for the stop rule: the census
#: stopped on 2/3 at $2.96 although every 100-task wave ran at about 25%.
STOP_MIN_TRIALS = 20


def sh(*args: str) -> str:
    done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return done.stdout + done.stderr


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def spend() -> tuple[float, int]:
    jobs = sorted(str(p) for p in RUNS.glob("har108-nop-*") if p.is_dir())
    if not jobs:
        return 0.0, 0
    sh(
        "uv",
        "run",
        "evallab",
        "tasks",
        "qualify-collect",
        *jobs,
        "--backend-rate-card",
        "daytona",
        "--output",
        str(SCRATCH),
    )
    rows = pq.read_table(SCRATCH).to_pylist()
    return sum(r["est_cost_usd"] or 0 for r in rows), len(rows)


def approved_ids() -> list[str]:
    status = json.loads(
        subprocess.run(
            ["uv", "run", "evallab", "status", "--json"], cwd=ROOT, capture_output=True, text=True
        ).stdout
    )
    return [
        item["experiment_id"]
        for item in status["Now"]["items"]
        if item["label"].startswith("har108-nop") and (item["detail"] or "").startswith("approved")
    ]


def running() -> int:
    return len(list((ROOT / "queue/running").glob("*.json")))


def resolve_orphans() -> None:
    """Fail running specs whose Harbor process is gone (killed executor).

    Harbor only writes the job's ``finished_at`` from its own process; when the
    executor that owned it is killed after the verifier ran, the spec would
    sit in ``running/`` until its multi-hour timeout and block every tick.
    Trial evidence stays in ``runs/`` for qualify-collect.
    """
    from evallab.queue import DirectoryQueue, PolicyDecision

    live = sh("pgrep", "-fl", "harbor run")
    queue = DirectoryQueue(ROOT / "queue", create=False)
    for path, spec in queue.list_specs("running"):
        if f"--job-name {spec.name} " in live:
            continue
        failed = queue.transition(
            path,
            "failed",
            actor="har108-census-driver",
            event="running_reconcile_failed",
            reason_code="executor_killed_operator_resolved",
            policy_rule=spec.policy_rule,
        )
        queue.write_reason(
            queue.load(failed),
            PolicyDecision(
                admitted=False,
                reason_code="executor_killed_operator_resolved",
                message="owning executor was killed and no Harbor process remains; trial evidence kept in runs/",
            ),
        )
        log(f"resolved orphan {spec.name}")


def tick(ids: list[str], parallel: int) -> str:
    args = [
        "uv",
        "run",
        "evallab",
        "tick",
        "--parallel",
        str(parallel),
        "--max-specs",
        str(max(len(ids), 1)),
    ]
    for i in ids:
        args += ["--spec-id", i]
    return sh(*args)


def submit(entries: list[dict]) -> int:
    n = 0
    for entry in entries:
        out = sh("uv", "run", "evallab", "submit", str(HERE / entry["spec"]))
        match = re.search(r"approve ([0-9A-Z]{26})", out)
        if not match:
            log(f"submit failed {entry['task_id']}: {out.strip()[-200:]}")
            continue
        sh("uv", "run", "evallab", "approve", match.group(1), "--actor", "peter")
        n += 1
    return n


def setup_rate(names: list[str]) -> tuple[int, int]:
    hits = total = 0
    for name in names:
        for stdout in RUNS.glob(f"{name}/*/verifier/test-stdout.txt"):
            total += 1
            hits += bool(SETUP_ERROR.search(stdout.read_text(errors="replace")))
    return hits, total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cap", type=float, default=3.0)
    parser.add_argument("--wave", type=int, default=100)
    parser.add_argument("--parallel", type=int, default=8)
    args = parser.parse_args()
    pool = [e for e in json.loads((HERE / "pool.json").read_text())["pool"] if e.get("spec")]
    while True:
        # Settle: dispatch leftovers, wait out detached work.
        while running():
            resolve_orphans()
            if not running():
                break
            log(f"{running()} running spec(s); waiting")
            time.sleep(30)
            tick(approved_ids(), args.parallel)  # reconciles; dispatches only once none run
        left = approved_ids()
        if left:
            log(f"dispatching {len(left)} approved")
            tick(left, args.parallel)
            continue
        used, rows = spend()
        done = {p.name for p in RUNS.glob("har108-nop-*")}
        todo = [e for e in pool if Path(e["spec"]).stem not in done]
        room = int((args.cap - used) / WAVE_COST_USD)
        log(f"spend ${used:.4f} over {rows} nops; {len(todo)} pool tasks left; room {room}")
        if not todo or room < 1:
            log("stop: " + ("pool done" if not todo else "cap reached"))
            return
        wave = todo[: min(args.wave, room)]
        names = [Path(e["spec"]).stem for e in wave]
        log(f"wave of {submit(wave)} submitted")
        tick(approved_ids(), args.parallel)
        hits, total = setup_rate(names)
        log(f"wave setup-error rate {hits}/{total}")
        if total >= STOP_MIN_TRIALS and hits / total > STOP_SETUP_RATE:
            log("stop: setup-error rate above threshold; review before continuing")
            return


if __name__ == "__main__":
    main()
