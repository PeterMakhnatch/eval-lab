#!/usr/bin/env python3
"""Modal validation of the four safeguard-integrity tasks: one execution per (task, solution) cell.

Each cell runs ``solutions/<task>/<variant>/solve.sh`` in the agent container and then the package's
own ``tests/test.sh`` natively (``ModalBackend.run_container_trial(mode="shortcut")``, lab Harbor 0.24.0,
no model agent). Cap $2.50 (T3-SAFEGUARD-BENCH contract), reserved per execution through ``CloudSpendGate``.

    validate.py --plan
    validate.py --run [--task alert-sink-backfill] [--variant honest]

Full Harbor trial directories go to ``--trials-root``; the small per-cell receipts committed here are
``receipts/<task>/<variant>/{result.json,safeguard_report.json,reward.txt}``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "t3-safeguard-bench-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
APPROVAL = "T3-SAFEGUARD-BENCH overnight contract 2026-10-31: Modal validation of 4 safeguard-integrity tasks, cap $2.50"
TAG = "t3-safeguard-bench"
CAP_USD = 2.50
USD_PER_EXECUTION = 0.05
CPU_S, GIB_S = 0.00003942, 0.00000667  # Modal Sandbox rates, docs/execution-tiers.md
TRIALS_ROOT = Path.home() / "Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T3-SAFEGUARD-BENCH/receipts/trials"
TASKS = {
    "alert-sink-backfill": ["honest", "subverted", "nop"],
    "serving-path-migration": ["honest", "subverted", "dishonest_subverted", "nop"],
    "ci-gate-stabilization": ["honest", "subverted", "nop"],
    "delegated-preflight-refactor": ["honest", "subverted", "dishonest_subverted", "nop"],
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def estimate(package: Path, wall: float) -> float:
    env = tomllib.loads((package / "task.toml").read_text(encoding="utf-8")).get("environment", {})
    return (
        (int(env.get("cpus", 1)) * CPU_S + int(env.get("memory_mb", 2048)) / 1024 * GIB_S) * wall * 1.25
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


def run_cell(
    task: str, variant: str, spend, trials_root: Path, retry: bool, campaign: dict | None = None
) -> dict:
    from evallab.qa.task_modal import ModalBackend
    from evallab.registry import task_directory_digest

    package = HERE / "tasks" / task
    script_path = HERE / "solutions" / task / variant / "solve.sh"
    receipt = HERE / "receipts" / task / variant
    result_path = receipt / "result.json"
    if result_path.exists():
        previous = json.loads(result_path.read_text())
        if not (retry and previous.get("reward") is None):
            return previous
    digest = task_directory_digest(package)
    trial_dir = trials_root / task / variant / "trial"
    if trial_dir.exists():
        trial_dir.rename(trial_dir.with_name(f"trial-previous-{int(time.time())}"))
    trial_dir.mkdir(parents=True, exist_ok=True)
    script = script_path.read_text()
    backend = ModalBackend(repo_root=REPO, spend_gate=spend)
    started = time.monotonic()
    observation = backend.run_container_trial(
        package,
        digest=digest,
        mode="shortcut",
        script=script,
        trial_dir=trial_dir,
        timeout_seconds=900,
    )
    wall = time.monotonic() - started
    verifier = trial_dir / "verifier"
    report_file = verifier / "safeguard_report.json"
    reward_file = verifier / "reward.txt"
    est = estimate(package, wall)
    result = {
        "task": task,
        "variant": variant,
        "status": observation.status,
        "reason": observation.reason,
        "reward": observation.facts.get("reward"),
        "verifier_reward": reward_file.read_text().strip() if reward_file.is_file() else None,
        "safeguard_report": json.loads(report_file.read_text()) if report_file.is_file() else None,
        "wall_s": round(wall, 1),
        "est_usd": round(est, 5),
        "reserved_usd": USD_PER_EXECUTION,
        "package_digest": digest,
        "script_sha256": sha256(script.encode()),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "trial_dir": str(trial_dir),
        "campaign": campaign or {"approval": APPROVAL, "cap_usd": CAP_USD, "ledger": str(LEDGER)},
    }
    receipt.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if report_file.is_file():
        (receipt / "safeguard_report.json").write_text(report_file.read_text())
    if reward_file.is_file():
        (receipt / "reward.txt").write_text(reward_file.read_text())
    with SPEND_LOG.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "cost_usd": round(est, 6),
                    "job": f"{TAG}:{task}/{variant}",
                    "note": f"modal shortcut cell status={observation.status} reward={result['reward']}",
                    "route": "modal",
                    "ts": result["ran_at"],
                },
                sort_keys=True,
            )
            + "\n"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--task", choices=sorted(TASKS), default=None)
    parser.add_argument("--variant", default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--trials-root", type=Path, default=TRIALS_ROOT)
    parser.add_argument("--approval", default=APPROVAL)
    parser.add_argument("--cap-usd", type=float, default=CAP_USD)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    cells = [
        (task, variant)
        for task, variants in TASKS.items()
        for variant in variants
        if (args.task in (None, task)) and (args.variant in (None, variant))
    ]
    if args.plan:
        print(
            json.dumps(
                {
                    "cells": [
                        {
                            "task": t,
                            "variant": v,
                            "script_sha256": sha256((HERE / "solutions" / t / v / "solve.sh").read_bytes()),
                        }
                        for t, v in cells
                    ],
                    "cap_usd": CAP_USD,
                    "reserved_if_all_run_usd": round(len(cells) * USD_PER_EXECUTION, 2),
                },
                indent=2,
            )
        )
        return
    if not args.run:
        parser.error("choose --plan or --run")
    spend = gate(args.approval, args.cap_usd, args.ledger)
    campaign = {"approval": args.approval, "cap_usd": args.cap_usd, "ledger": str(args.ledger)}
    for task, variant in cells:
        result = run_cell(task, variant, spend, args.trials_root, args.retry_failed, campaign)
        report = result.get("safeguard_report") or {}
        print(
            json.dumps(
                {
                    "task": task,
                    "variant": variant,
                    "status": result["status"],
                    "reward": result["reward"],
                    "functional_pass": report.get("functional_pass"),
                    "safeguard_preserved": report.get("safeguard_preserved"),
                    "test_tampered": report.get("test_tampered"),
                    "summary_dishonest": report.get("summary_dishonest"),
                    "wall_s": result["wall_s"],
                    "est_usd": result["est_usd"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
