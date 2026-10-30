#!/usr/bin/env python3
"""H1 executed validation of ``grader-hardening@1`` on Modal (paid; cap set by --max-usd, default $6.00).

Per sampled DEV MiMo package: one calibration run of the reference solution (upstream grader plus a JUnit request) to
read the reference test count; then, on the original package (L0) and on the hardened variant (H), the correct
solution, an empty submission and each attack family of ``attacks.py``. Cells are consumed once; a rerun skips finished
cells. Needs the lab Harbor: run with ``.venv/bin/python`` after ``uv sync --extra laminar``.

    run_matrix.py --plan                      # selection only, no cloud
    run_matrix.py --smoke                     # first selected task, a few cells
    run_matrix.py --run --max-usd 6.00        # everything
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
import attacks  # noqa: E402

from evallab.grader_hardening import (  # noqa: E402
    build_calibration_test_sh,
    derive_grader_hardening,
    junit_counts,
    report_gate_blocker,
)
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402
from evallab.registry import task_directory_digest  # noqa: E402

BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-30/h1-grader-hardening")
SCRATCH = Path("/tmp/h1-grader-hardening")
LEDGER = Path.home() / ".local/state/evallab-failure-library/h1-modal-ledger-v2.json"
SPEND_LOG = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"
APPROVAL = (
    "Peter 2026-10-30 factory budget: H1 grader-hardening executed validation, Modal cap $6.00"
)
TAG = "h1-grader-hardening"
DATASET = "mimo-defect-map-v1"
SAMPLE = 12
BACKFILL = 2
CPU_S, GIB_S = 0.00003942, 0.00000667  # Modal Sandbox rates in docs/execution-tiers.md


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def select() -> tuple[list[dict], list[dict]]:
    """Deterministic DEV sample: sha256(task_id) order, strict assignment check, reference present, pytest runner."""
    registry = CorpusRegistry(repo_root=REPO)
    references = {
        r["task_id"]: r
        for r in csv.DictReader(
            (REPO / "research/experiments/mimo-v6-full-fleet/references.csv").open()
        )
    }
    rows = [
        json.loads(line) for line in (BRIDGE / "SK-2/receipts/tasks.jsonl").read_text().splitlines()
    ]
    chosen, skipped = [], []
    for row in sorted(rows, key=lambda r: sha256(r["task_id"].encode())):
        task = row["task_id"]
        why = None
        package = Path(row["original_task"])
        if registry.assigned_split(
            DATASET, task_id=task
        ) != "dev" or registry.assigned_test_anywhere(task_id=task):
            why = "not strictly DEV"
        elif task not in references:
            why = "no vendored reference patch"
        elif (
            not (package / "tests/test.sh").is_file()
            or "EOF_RESET_TEST_FILES" not in (package / "tests/test.sh").read_text()
        ):
            why = "not the single-container reset-list shape"
        elif (blocker := report_gate_blocker(package)) is not None:
            why = f"no report gate: {blocker}"
        if why:
            skipped.append({"task_id": task, "reason": why})
            continue
        patch_path = REPO / references[task]["patch_path"]
        patch = patch_path.read_bytes()
        if sha256(patch) != references[task]["patch_sha256"]:
            skipped.append({"task_id": task, "reason": "reference patch sha256 mismatch"})
            continue
        chosen.append(
            {
                "task_id": task,
                "package": str(package),
                "patch": patch.decode(),
                "patch_sha256": sha256(patch),
                "order_sha256": sha256(task.encode()),
            }
        )
    return chosen[: SAMPLE + BACKFILL], skipped


class Meter:
    """Cost estimate from measured sandbox seconds, with the shared 25% margin; the cap is enforced on it."""

    def __init__(self, cap: float):
        self.cap, self.spent, self.cells, self.lock = cap, 0.0, 0, threading.Lock()

    def estimate(self, package: Path, wall: float) -> float:
        env = tomllib.loads((package / "task.toml").read_text()).get("environment", {})
        return (
            (int(env.get("cpus", 2)) * CPU_S + int(env.get("memory_mb", 8192)) / 1024 * GIB_S)
            * wall
            * 1.25
        )

    def admit(self, projected: float) -> None:
        with self.lock:
            if self.spent + projected > self.cap:
                raise RuntimeError(f"cap ${self.cap:.2f} would be passed (spent ${self.spent:.3f})")

    def add(self, cost: float) -> None:
        with self.lock:
            self.spent += cost
            self.cells += 1


def gate(cap: float, per_execution: float, ledger: str = "h1-modal-ledger-v2.json"):
    from evallab.regrade import CloudSpendGate

    return CloudSpendGate(
        approval=APPROVAL,
        cap_usd=cap,
        usd_per_execution=per_execution,
        spend_ledger=LEDGER.with_name(ledger),
        modal_app_name=TAG,
        billing_tag=TAG,
    )


def run_cell(cell_dir: Path, package: Path, script_text: str, spend, meter: Meter) -> dict:
    """One Modal execution; the result file is the consumption marker."""
    result_path = cell_dir / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    from evallab.qa.task_modal import ModalBackend

    meter.admit(meter.estimate(package, 240))
    cell_dir.mkdir(parents=True, exist_ok=True)
    backend = ModalBackend(repo_root=REPO, spend_gate=spend)
    started = time.monotonic()
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="shortcut",
        script=script_text,
        trial_dir=cell_dir / "trial",
        timeout_seconds=1800,
    )
    wall = time.monotonic() - started
    trial = cell_dir / "trial"
    verifier = trial / "verifier"
    agent_log = (
        (trial / "agent/oracle.txt").read_text(errors="replace")
        if (trial / "agent/oracle.txt").exists()
        else ""
    )
    test_output = (
        (verifier / "test_output.log").read_text(errors="replace")
        if (verifier / "test_output.log").exists()
        else ""
    )
    junit = sorted(verifier.glob("junit-*.xml"))
    counts = None
    if junit:
        try:
            counts = junit_counts(junit[-1].read_text(errors="replace"))
        except Exception:  # forged or truncated report: recorded as absent counts
            counts = None
    removed = (
        (verifier / "l1-removed.txt").read_text(errors="replace").split()
        if (verifier / "l1-removed.txt").exists()
        else None
    )
    reward = observation.facts.get("reward")
    cost = meter.estimate(package, wall)
    meter.add(cost)
    result = {
        "status": observation.status,
        "reason": observation.reason,
        "reward": reward,
        "wall_s": round(wall, 1),
        "est_usd": round(cost, 5),
        "attack_applied": "H1_APPLIED" in agent_log,
        "junit_counts": counts,
        "l1_removed": removed,
        "gate": next(
            (
                ln.split("gate ")[-1].strip(" )")
                for ln in (
                    (verifier / "test-stdout.txt").read_text(errors="replace").splitlines()
                    if (verifier / "test-stdout.txt").exists()
                    else []
                )
                if "(gate " in ln
            ),
            None,
        ),
        "test_output_tail": test_output[-600:],
        "evidence_sha256": sha256((agent_log + test_output).encode()),
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def calibrate(task: dict, spend, meter: Meter) -> dict:
    package = Path(task["package"])
    scratch = SCRATCH / "calibration" / task["task_id"]
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(package, scratch)
    for path in (scratch, *scratch.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)
    test_sh = scratch / "tests/test.sh"
    test_sh.write_text(build_calibration_test_sh((package / "tests/test.sh").read_text()))
    workdir = tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]
    script = attacks.script(
        "correct", workdir=workdir, reference_patch=task["patch"], expected_tests=None, e2_file=None
    )
    result = run_cell(
        OUT / "cells" / task["task_id"] / "calibration", scratch, script, spend, meter
    )
    out = {"result": result, "ok": False}
    counts = result.get("junit_counts")
    if (
        result["reward"] == 1.0
        and counts
        and counts[1] == 0
        and counts[2] == 0
        and counts[0] > counts[3]
    ):
        out.update(ok=True, tests=counts[0], skipped=counts[3])
    return out


def hardened_variant(task: dict, calibration: dict) -> Path:
    """Derive the hardened variant into a scratch store (re-derivable, identical digest on a rerun)."""
    root, store = SCRATCH / "root", SCRATCH / "store"
    slug = "mimo-v2.6-rl__" + task["task_id"]
    for stale in (store / slug, root / "library/task-variants" / slug):
        shutil.rmtree(stale, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    record = derive_grader_hardening(
        task["package"],
        expected_tests=calibration["tests"],
        expected_skipped=calibration["skipped"],
        repo_root=root,
        variants_root=store,
    )
    return (
        store
        / record.task_name.strip("/").replace("/", "__")
        / record.variant_digest.split(":")[1][:12]
    )


def l1_only_variant(task: dict) -> Path:
    """The hardened grader without the report gate (plain copy; attribution only, never shipped)."""
    from evallab.grader_hardening import build_test_sh

    scratch = SCRATCH / "l1-only" / task["task_id"]
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(task["package"], scratch)
    for path in (scratch, *scratch.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)
    parent_text = (Path(task["package"]) / "tests/test.sh").read_text()
    (scratch / "tests/test.sh").write_text(build_test_sh(parent_text, with_report_gate=False))
    return scratch


def task_cells(
    task: dict, calibration: dict, spend, meter: Meter, arms: list[str], pool: ThreadPoolExecutor
) -> list:
    original = Path(task["package"])
    variant = hardened_variant(task, calibration)
    l1_only = l1_only_variant(task)
    workdir = tomllib.loads((original / "task.toml").read_text())["environment"]["workdir"]
    e2 = attacks.e2_source_file(task["patch"])
    jobs = []
    for level, package in (("H", variant), ("L1", l1_only), ("L0", original)):
        for arm in arms:
            if arm == "E2" and e2 is None:
                continue
            script = attacks.script(
                arm,
                workdir=workdir,
                reference_patch=task["patch"],
                expected_tests=calibration["tests"],
                e2_file=e2,
            )
            jobs.append(
                (
                    task["task_id"],
                    level,
                    arm,
                    pool.submit(
                        run_cell,
                        OUT / "cells" / task["task_id"] / f"{level}-{arm}",
                        package,
                        script,
                        spend,
                        meter,
                    ),
                )
            )
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-usd", type=float, default=6.0)
    parser.add_argument("--per-execution", type=float, default=0.005)
    parser.add_argument(
        "--gate-cap",
        type=float,
        default=None,
        help="cap of the persisted gate ledger (default --max-usd)",
    )
    parser.add_argument("--ledger", default="h1-modal-ledger-v2.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    chosen, skipped = select()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selection.json").write_text(
        json.dumps(
            {
                "chosen": [{k: v for k, v in t.items() if k != "patch"} for t in chosen],
                "skipped_before_sample": skipped,
                "sample": SAMPLE,
                "backfill": BACKFILL,
            },
            indent=2,
        )
    )
    print(json.dumps({"chosen": [t["task_id"] for t in chosen], "skipped": skipped}, indent=1))
    if args.plan or not (args.smoke or args.run):
        return
    meter = Meter(args.max_usd)
    spend = gate(args.gate_cap or args.max_usd, args.per_execution, args.ledger)
    arms = ["correct", "empty", "A"] if args.smoke else list(attacks.ARMS)
    tasks = chosen[:1] if args.smoke else chosen
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        calibrations = list(pool.map(lambda t: calibrate(t, spend, meter), tasks))
        usable = [(t, c) for t, c in zip(tasks, calibrations, strict=True) if c["ok"]][
            : (1 if args.smoke else SAMPLE)
        ]
        for t, c in zip(tasks, calibrations, strict=True):
            print(
                "calibration",
                t["task_id"],
                c["ok"],
                c.get("tests"),
                c.get("skipped"),
                c["result"]["reason"],
                flush=True,
            )
        jobs = []
        for t, c in usable:
            jobs += task_cells(t, c, spend, meter, arms, pool)
        rows = []
        for task_id, level, arm, future in jobs:
            try:
                rows.append({"task": task_id, "level": level, "arm": arm, **future.result()})
            except Exception as exc:
                rows.append(
                    {
                        "task": task_id,
                        "level": level,
                        "arm": arm,
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
    (OUT / ("smoke-matrix.json" if args.smoke else "matrix.json")).write_text(
        json.dumps(
            {
                "calibrations": {
                    t["task_id"]: {k: v for k, v in c.items() if k != "result"}
                    | {"reward": c["result"]["reward"]}
                    for t, c in zip(tasks, calibrations, strict=True)
                },
                "cells": rows,
                "estimated_usd": round(meter.spent, 4),
                "executions": meter.cells,
            },
            indent=1,
        )
    )
    print(json.dumps({"estimated_usd": round(meter.spent, 4), "executions": meter.cells}))


if __name__ == "__main__":
    main()
