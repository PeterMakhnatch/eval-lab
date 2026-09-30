#!/usr/bin/env python3
"""Submit, approve and dispatch HAR-113 Daytona specs under one spend cap.

Every job is named ``<prefix>*`` (``har113-`` by default; ``--prefix har115-``
for HAR-115) and lands in this checkout's ``runs/``. Spend is measured, not
estimated: ``qualify-collect`` over every ``<prefix>*``
job into a scratch table, summing ``est_cost_usd`` (the Daytona rate card).
A batch is dispatched only while the measured spend plus the batch's
upper-tail cost stays under the cap.

    runner.py SPEC.json [SPEC.json ...] [--cap USD] [--parallel N] [--prefix P]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import pyarrow.parquet as pq
from common import ROOT

RUNS = ROOT / "runs"
SCRATCH = Path("/private/tmp/har113/spend.parquet")
PREFIX = "har113-"
CAP_USD = 2.50
#: Upper-tail cost of one code trial (HAR-108: mean $0.0057, p90 about $0.012).
TRIAL_COST_USD = 0.012


def sh(*args: str) -> str:
    done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return done.stdout + done.stderr


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def spend() -> tuple[float, int]:
    """Measured Daytona spend and trial count over every ``PREFIX*`` job."""
    jobs = sorted(str(p) for p in RUNS.glob(f"{PREFIX}*") if p.is_dir())
    if not jobs:
        return 0.0, 0
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
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
    out = subprocess.run(
        ["uv", "run", "evallab", "status", "--json"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    return [
        item["experiment_id"]
        for item in json.loads(out)["Now"]["items"]
        if item["label"].startswith(PREFIX) and (item["detail"] or "").startswith("approved")
    ]


def running() -> int:
    return len(list((ROOT / "queue/running").glob("*.json")))


def resolve_orphans() -> None:
    """Fail running specs whose Harbor process is gone (killed executor)."""
    from evallab.queue import DirectoryQueue, PolicyDecision

    live = sh("pgrep", "-fl", "harbor run")
    queue = DirectoryQueue(ROOT / "queue", create=False)
    for path, spec in queue.list_specs("running"):
        if f"--job-name {spec.name} " in live:
            continue
        failed = queue.transition(
            path,
            "failed",
            actor=f"{PREFIX}runner",
            event="running_reconcile_failed",
            reason_code="executor_killed_operator_resolved",
            policy_rule=spec.policy_rule,
        )
        queue.write_reason(
            queue.load(failed),
            PolicyDecision(
                admitted=False,
                reason_code="executor_killed_operator_resolved",
                message="owning executor was killed and no Harbor process remains; "
                "trial evidence kept in runs/",
            ),
        )
        log(f"resolved orphan {spec.name}")


def tick(ids: list[str], parallel: int) -> None:
    args = ["uv", "run", "evallab", "tick", "--parallel", str(parallel)]
    args += ["--max-specs", str(max(len(ids), 1))]
    for spec_id in ids:
        args += ["--spec-id", spec_id]
    sh(*args)


def submit(spec: Path) -> bool:
    out = sh("uv", "run", "evallab", "submit", str(spec))
    match = re.search(r"approve ([0-9A-Z]{26})", out)
    if not match:
        log(f"submit failed {spec.name}: {out.strip()[-300:]}")
        return False
    sh("uv", "run", "evallab", "approve", match.group(1), "--actor", "peter")
    return True


def settle(parallel: int) -> None:
    """Dispatch approved ``PREFIX*`` specs and wait until none is running."""
    while True:
        while running():
            resolve_orphans()
            if not running():
                break
            log(f"{running()} running spec(s); waiting")
            time.sleep(30)
        left = approved_ids()
        if not left:
            return
        log(f"dispatching {len(left)} approved")
        tick(left, parallel)


def run(specs: list[Path], *, cap: float = CAP_USD, parallel: int = 8, wave: int = 40) -> None:
    """Run specs whose job is not in ``runs/`` yet, in waves under the cap."""
    todo = [s for s in specs if not (RUNS / json.loads(s.read_text())["name"]).is_dir()]
    settle(parallel)
    while todo:
        used, trials = spend()
        room = int((cap - used) / TRIAL_COST_USD)
        log(f"spend ${used:.4f} over {trials} trials; {len(todo)} specs left; room {room}")
        if room < 1:
            log("stop: cap reached")
            return
        batch, todo = todo[: min(wave, room)], todo[min(wave, room) :]
        log(f"wave of {sum(submit(s) for s in batch)} submitted")
        settle(parallel)
    used, trials = spend()
    log(f"done: spend ${used:.4f} over {trials} trials")


def main() -> None:
    global PREFIX, SCRATCH
    parser = argparse.ArgumentParser()
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--cap", type=float, default=CAP_USD)
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--wave", type=int, default=40)
    parser.add_argument("--prefix", default=PREFIX, help="job-name prefix spend is summed over")
    args = parser.parse_args()
    PREFIX = args.prefix
    SCRATCH = Path(f"/private/tmp/{PREFIX.rstrip('-')}/spend.parquet")
    run(args.specs, cap=args.cap, parallel=args.parallel, wave=args.wave)


if __name__ == "__main__":
    main()
