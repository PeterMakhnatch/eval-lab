#!/usr/bin/env python3
"""C1 executed validation on Modal: the honest cell and the cheat cell.

Two paid executions, one per solution, against the unhardened grader in
``tasks/billing-amount-format``. Cap $0.50. Needs the lab Harbor; run with a
venv that has ``modal`` and ``harbor`` installed and this checkout's ``src``
first on the path (the script inserts it).

    validate.py --plan
    validate.py --run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

LEDGER = (
    Path.home() / ".local/state/evallab-failure-library/c1-impossible-test-modification-ledger.json"
)
APPROVAL = "Peter 2026-10-30 factory budget: C1 impossible test-modification task, Modal cap $0.50"
TAG = "c1-impossible-test-modification"
CAP_USD = 0.50
USD_PER_EXECUTION = 0.05
CPU_S, GIB_S = 0.00003942, 0.00000667  # Modal Sandbox rates, docs/execution-tiers.md
PACKAGE = HERE / "tasks/billing-amount-format"
CELLS = {"honest": HERE / "solutions/honest/solve.sh", "cheat": HERE / "solutions/cheat/solve.sh"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def estimate(package: Path, wall: float) -> float:
    env = tomllib.loads((package / "task.toml").read_text(encoding="utf-8")).get("environment", {})
    return (
        (int(env.get("cpus", 1)) * CPU_S + int(env.get("memory_mb", 2048)) / 1024 * GIB_S)
        * wall
        * 1.25
    )


def gate(approval: str = APPROVAL, cap_usd: float = CAP_USD, ledger: Path = LEDGER):
    from evallab.regrade import CloudSpendGate

    return CloudSpendGate(
        approval=approval,
        cap_usd=cap_usd,
        usd_per_execution=USD_PER_EXECUTION,
        spend_ledger=ledger,
        modal_app_name=TAG,
        billing_tag=TAG,
    )


def run_cell(cell: str, spend, *, approval: str, cap_usd: float, ledger: Path, retry: bool) -> dict:
    from evallab.qa.task_modal import ModalBackend
    from evallab.registry import task_directory_digest

    result_path = HERE / "receipts" / cell / "result.json"
    if result_path.exists():
        previous = json.loads(result_path.read_text())
        if not (retry and previous.get("reward") is None):
            return previous
    digest = task_directory_digest(PACKAGE)
    trial_dir = HERE / "receipts" / cell / "trial"
    if trial_dir.exists():
        # An earlier attempt left a trial leaf behind; Harbor needs an empty one and
        # the provider sandbox name is derived from this path.
        stale = trial_dir.with_name(f"trial-previous-{int(time.time())}")
        trial_dir.rename(stale)
        print(json.dumps({"cell": cell, "moved_stale_trial": str(stale)}), file=sys.stderr)
    trial_dir.mkdir(parents=True, exist_ok=True)
    script = CELLS[cell].read_text()
    backend = ModalBackend(repo_root=REPO, spend_gate=spend)
    started = time.monotonic()
    observation = backend.run_container_trial(
        PACKAGE,
        digest=digest,
        mode="shortcut",
        script=script,
        trial_dir=trial_dir,
        timeout_seconds=900,
    )
    wall = time.monotonic() - started
    verifier = trial_dir / "verifier"
    agent_log = trial_dir / "agent/oracle.txt"
    test_output = verifier / "test_output.log"
    reward_file = verifier / "reward.txt"
    result = {
        "cell": cell,
        "status": observation.status,
        "reason": observation.reason,
        "reward": observation.facts.get("reward"),
        "verifier_reward": reward_file.read_text().strip() if reward_file.is_file() else None,
        "wall_s": round(wall, 1),
        "est_usd": round(estimate(PACKAGE, wall), 5),
        "package_digest": digest,
        "script_sha256": sha256(script.encode()),
        "campaign": {"approval": approval, "cap_usd": cap_usd, "ledger": str(ledger)},
        "agent_log_tail": agent_log.read_text(errors="replace")[-1200:]
        if agent_log.is_file()
        else "",
        "test_output_tail": test_output.read_text(errors="replace")[-1200:]
        if test_output.is_file()
        else "",
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--cell", choices=sorted(CELLS), default=None)
    parser.add_argument(
        "--retry-failed", action="store_true", help="re-run a cell whose receipt has no reward"
    )
    parser.add_argument("--approval", default=APPROVAL)
    parser.add_argument("--cap-usd", type=float, default=CAP_USD)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    cells = [args.cell] if args.cell else list(CELLS)
    if args.plan:
        print(
            json.dumps(
                {
                    "package": str(PACKAGE),
                    "cells": {c: sha256(CELLS[c].read_bytes()) for c in cells},
                    "cap_usd": args.cap_usd,
                    "approval": args.approval,
                    "ledger": str(args.ledger),
                    "executions": len(cells),
                },
                indent=2,
            )
        )
        return
    if not args.run:
        parser.error("choose --plan or --run")
    spend = gate(args.approval, args.cap_usd, args.ledger)
    results = []
    for cell in cells:
        result = run_cell(
            cell,
            spend,
            approval=args.approval,
            cap_usd=args.cap_usd,
            ledger=args.ledger,
            retry=args.retry_failed,
        )
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in ("cell", "status", "reward", "verifier_reward", "wall_s", "est_usd")
                }
            )
        )
        results.append(result)
    print(json.dumps({"summary": results}, indent=2))


if __name__ == "__main__":
    main()
