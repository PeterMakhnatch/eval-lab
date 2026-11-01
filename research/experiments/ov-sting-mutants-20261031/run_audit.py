#!/usr/bin/env python3
"""W2-STING-MUTANTS: reference-patch semantic mutants on 18 strictly-DEV MiMo packages (Modal, cap $1.75).

Selection (prereg): 12 of the 30 ``task_quality.repair_qualified`` tasks + 6 outside-30 tasks from
``mimo-v6-full-fleet/references.csv``, each strictly DEV (``CorpusRegistry``: dev split, no TEST
assignment anywhere), reference patch present with matching sha256, single-container reset-list
grader, >= 1 added line in a non-test ``.py`` file; sha256(task_id) order, no backfill.

Cells per task: 1 probe (reference control + post-patch source dump) + 3 mutants on the original
grader (+ 3 on the repaired grader for the 12 qualified tasks). Every cell is one Modal sandbox
through ``evallab.mutant_auditor.modal_cell_runner``; costs are measured sandbox seconds x Modal
rates x 1.25 and appended to ``~/.local/state/evallab-failure-library/spend.jsonl``.

Usage: ``uv run --no-sync --extra laminar python run_audit.py --plan | --run [--workers N]``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.mutant_auditor import (  # noqa: E402
    SandboxMeter,
    audit_task,
    modal_cell_runner,
    python_targets,
)
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

DATASET = "mimo-defect-map-v1"
RESULTS_HOME = Path.home() / "Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024"
ORIGINALS = RESULTS_HOME / "original-packages"
REPAIRED = RESULTS_HOME / "evidence/packages"
TASK_STORE = (
    Path.home()
    / "Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
REFERENCES = REPO / "research/experiments/mimo-v6-full-fleet/references.csv"
OUT = Path.home() / "Developer/eval-lab-results/2026-11-01/w2-sting-mutants"
STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "w2-sting-mutants-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
TAG = "w2-sting-mutants"
APPROVAL = "Peter 2026-10-31 overnight factory W2-STING-MUTANTS: Modal cap $1.75"
CAP_USD = 1.75
INSIDE, OUTSIDE = 12, 6


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repaired_snapshot(task_id: str) -> Path | None:
    root = REPAIRED / task_id
    if not root.is_dir():
        return None
    snapshots = sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file())
    return snapshots[-1] if snapshots else None


def select() -> tuple[list[dict], list[dict]]:
    registry = CorpusRegistry(repo_root=REPO)
    references = {r["task_id"]: r for r in csv.DictReader(REFERENCES.open())}
    inside_ids = sorted(p.name for p in ORIGINALS.iterdir() if p.is_dir())
    universes = {
        "inside": [(t, ORIGINALS / t) for t in inside_ids],
        "outside": [
            (t, TASK_STORE / t)
            for t in references
            if t not in inside_ids and (TASK_STORE / t / "task.toml").is_file()
        ],
    }
    chosen, skipped = [], []
    for universe, items in universes.items():
        quota = INSIDE if universe == "inside" else OUTSIDE
        taken = 0
        for task, package in sorted(items, key=lambda it: sha256(it[0].encode())):
            if taken >= quota:
                break
            why = None
            test_sh = package / "tests/test.sh"
            if registry.assigned_split(
                DATASET, task_id=task
            ) != "dev" or registry.assigned_test_anywhere(task_id=task):
                why = "not strictly DEV"
            elif task not in references:
                why = "no vendored reference patch"
            elif not test_sh.is_file() or "EOF_RESET_TEST_FILES" not in test_sh.read_text():
                why = "not the single-container reset-list shape"
            if why is None:
                patch = (REPO / references[task]["patch_path"]).read_bytes()
                if sha256(patch) != references[task]["patch_sha256"]:
                    why = "reference patch sha256 mismatch"
                elif not python_targets(patch.decode()):
                    why = "reference patch adds no non-test python line"
            if why:
                skipped.append({"task_id": task, "universe": universe, "reason": why})
                continue
            repaired = repaired_snapshot(task) if universe == "inside" else None
            if universe == "inside" and repaired is None:
                skipped.append(
                    {"task_id": task, "universe": universe, "reason": "no repaired snapshot"}
                )
                continue
            taken += 1
            chosen.append(
                {
                    "task_id": task,
                    "universe": universe,
                    "package": str(package),
                    "repaired_package": None if repaired is None else str(repaired),
                    "patch": patch.decode(),
                    "patch_sha256": sha256(patch),
                    "order_sha256": sha256(task.encode()),
                    "workdir": tomllib.loads((package / "task.toml").read_text())["environment"][
                        "workdir"
                    ],
                }
            )
    return chosen, skipped


def spend_logging_runner(inner, lock: threading.Lock):
    def run(cell_dir: Path, package: Path, script: str) -> dict:
        fresh = not (cell_dir / "result.json").exists()
        result = inner(cell_dir, package, script)
        if fresh:
            record = {
                "cost_usd": result["est_usd"],
                "job": f"{TAG}:{cell_dir.parent.name}/{cell_dir.name}",
                "note": (
                    f"W2-STING-MUTANTS cell {cell_dir.parent.name}/{cell_dir.name} "
                    f"status={result['status']} reward={result['reward']} wall_s={result['wall_s']} "
                    "(Modal rates x1.25 estimate)"
                ),
                "route": "modal",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with lock, SPEND_LOG.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return result

    return run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--workers", type=int, default=9, help="concurrent tasks (probes)")
    parser.add_argument("--cell-workers", type=int, default=10, help="concurrent mutant cells")
    parser.add_argument("--only", nargs="*", help="restrict to these task ids (smoke)")
    args = parser.parse_args()
    chosen, skipped = select()
    if args.only:
        chosen = [t for t in chosen if t["task_id"] in set(args.only)]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selection.json").write_text(
        json.dumps(
            {
                "chosen": [{k: v for k, v in t.items() if k != "patch"} for t in chosen],
                "skipped": skipped,
                "inside": INSIDE,
                "outside": OUTSIDE,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "chosen": [(t["universe"], t["task_id"]) for t in chosen],
                "skipped": skipped,
            },
            indent=1,
        )
    )
    if args.plan or not args.run:
        return
    from evallab.regrade import CloudSpendGate

    gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        usd_per_execution=0.005,
        spend_ledger=LEDGER,
        modal_app_name=TAG,
        billing_tag=TAG,
    )
    meter = SandboxMeter(CAP_USD)
    run_cell = spend_logging_runner(
        modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=meter), threading.Lock()
    )
    records = []
    with (
        ThreadPoolExecutor(max_workers=args.cell_workers) as cells,
        ThreadPoolExecutor(max_workers=args.workers) as tasks,
    ):

        def one(task: dict) -> dict:
            try:
                return audit_task(
                    task_id=task["task_id"],
                    package=Path(task["package"]),
                    reference_patch=task["patch"],
                    out_dir=OUT / "tasks" / task["task_id"],
                    run_cell=run_cell,
                    repaired_package=(
                        None if task["repaired_package"] is None else Path(task["repaired_package"])
                    ),
                    submit=cells.submit,
                )
            except Exception as exc:  # recorded, never silently dropped
                return {
                    "task_id": task["task_id"],
                    "outcome": f"error: {type(exc).__name__}: {exc}",
                }

        for task, record in zip(chosen, tasks.map(one, chosen), strict=True):
            record["universe"] = task["universe"]
            records.append(record)
            print(
                task["universe"],
                task["task_id"],
                record.get("outcome"),
                "weak_original=",
                record.get("weak_original"),
                "weak_repaired=",
                record.get("weak_repaired"),
                flush=True,
            )
    summary = {
        "approval": APPROVAL,
        "cap_usd": CAP_USD,
        "estimated_usd": round(meter.spent, 4),
        "sandbox_seconds": round(meter.seconds, 1),
        "executions": meter.cells,
        "tasks": records,
    }
    (OUT / "audit-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}))


if __name__ == "__main__":
    main()
