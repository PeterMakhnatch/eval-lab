"""Run exact-package oracle/nop controls through first-party native Harbor on Modal.

Default is a provider-free plan. Execution requires --execute and recorded approval.
One durable lane ledger reserves image preparation plus both native creation
attempts for every agent/verifier environment before any provider execution. These
are conservative reservations, not measured Modal billing. Failed admissions are
never refunded or silently retried; the 2026-10-17 mission's $4 cap is immutable.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.execution_contracts import RunRequest, validate_request
from evallab.queue import Executor
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate, lab_harbor_problem

RELEASE = Path("research/experiments/mimo-v6-full-fleet")
MISSION_CAP_USD = 4.0
UNITS_PER_ENVIRONMENT = 3  # image preparation plus two native sandbox-create attempts
SANDBOX_TTL_SECONDS = 900
MIN_USD_PER_UNIT = 0.05  # exceeds a full 900-s, 2-CPU/8-GiB sandbox reservation


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def pending_controls(
    args: argparse.Namespace, admissions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    with args.manifest.open(newline="", encoding="utf-8") as handle:
        manifest = {row["task_id"]: row for row in csv.DictReader(handle)}
    task_ids = list(dict.fromkeys(args.task_id)) if args.task_id else sorted(manifest)
    prior = {
        (item["task_id"], item["final_digest"], item["agent"]) for item in admissions
        if item["state"] != "preflight_refused"
    }
    pending = []
    for task_id in task_ids:
        row = manifest[task_id]
        if row["status"] != "built":
            raise ValueError(f"{task_id}: package was not built")
        package = args.package_root / row["package_path"]
        if task_directory_digest(package) != row["final_digest"]:
            raise ValueError(f"{task_id}: actual package does not match the fleet manifest")
        raw = tomllib.loads((package / "task.toml").read_text())
        verifier = raw.get("verifier") or {}
        separate = verifier.get("environment_mode") == "separate" or (
            verifier.get("environment_mode") is None and verifier.get("environment") is not None
        )
        environments = [raw["environment"]]
        if separate:
            environments.append(verifier.get("environment") or raw["environment"])
        for environment in environments:
            if environment.get("gpus") or environment.get("gpu"):
                raise ValueError(f"{task_id}: GPU allocation is not priced by this lane")
            if environment.get("cpus", 2) > 2 or environment.get("memory_mb", 8192) > 8192:
                raise ValueError(f"{task_id}: resources exceed the 2-CPU/8-GiB reservation basis")
        for agent in ("oracle", "nop"):
            if (task_id, row["final_digest"], agent) not in prior:
                pending.append({
                    "task_id": task_id, "final_digest": row["final_digest"], "agent": agent,
                    "package": str(package),
                    "reservation_units": len(environments) * UNITS_PER_ENVIRONMENT,
                })
    return pending, len(task_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--max-usd", type=float, default=MISSION_CAP_USD)
    parser.add_argument("--usd-per-unit", type=float, default=0.05)
    parser.add_argument("--approve", default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.repo_root = args.repo_root.resolve()
    args.package_root = args.package_root.resolve()
    args.manifest = args.manifest or args.repo_root / RELEASE / "manifest.csv"
    if not math.isfinite(args.max_usd) or not 0 < args.max_usd <= MISSION_CAP_USD:
        raise ValueError("the current mission permits a positive cap no greater than $4")
    if not math.isfinite(args.usd_per_unit) or args.usd_per_unit < MIN_USD_PER_UNIT:
        raise ValueError("this mission reserves at least $0.05 per allocation/preparation unit")
    if not 1 <= args.workers <= 16:
        raise ValueError("Modal concurrency must be in [1, 16]")
    ledger_path = args.repo_root / RELEASE / "native-admissions.json"
    lock_name = hashlib.sha256(str(args.repo_root).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"mimo-v6-native-{lock_name}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text())
            if ledger["cap_usd"] != args.max_usd or ledger["usd_per_unit"] != args.usd_per_unit:
                raise ValueError("existing lane cap and reservation unit cannot be changed")
        else:
            ledger = {
                "schema": "mimo-v6-native-admissions/1", "cap_usd": args.max_usd,
                "usd_per_unit": args.usd_per_unit, "units_reserved": 0, "admissions": [],
                "basis": "reservations, not measured Modal billing; no failure refunds",
            }
        pending, task_count = pending_controls(args, ledger["admissions"])
        units = sum(item["reservation_units"] for item in pending)
        total_usd = round((ledger["units_reserved"] + units) * args.usd_per_unit, 6)
        manifest_sha = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
        plan = {
            "manifest_sha256": manifest_sha,
            "selected_tasks": task_count,
            "pending_executions": len(pending), "pending_reservation_units": units,
            "already_reserved_usd": round(ledger["units_reserved"] * args.usd_per_unit, 6),
            "proposed_total_reserved_usd": total_usd, "cap_usd": args.max_usd,
            "fits_cap": total_usd <= args.max_usd + 1e-9,
            "sandbox_ttl_seconds": SANDBOX_TTL_SECONDS,
            "unit_basis": "preparation plus two native creation attempts per environment",
            "billing": "not measured by this plan or reservation ledger",
        }
        print(json.dumps({"plan": plan}, sort_keys=True), flush=True)
        if not args.execute:
            return 0
        if not plan["fits_cap"]:
            raise ValueError("full proposed admission exceeds the lane's remaining reservation cap")
        if not args.approve.strip():
            raise ValueError("recorded approval is required for real Modal execution")
        problem = lab_harbor_problem()
        if problem:
            raise ValueError(problem)
        gate = CloudSpendGate(
            approval=args.approve, cap_usd=args.max_usd,
            usd_per_execution=args.usd_per_unit, executions=ledger["units_reserved"],
        )
        requests: dict[str, RunRequest] = {}
        for item in pending:
            item.update(
                state="reserved", manifest_sha256=manifest_sha, approval=args.approve,
                reserved_at=datetime.now(UTC).isoformat(),
                job=f"m6f-{item['task_id'].rsplit('-', 1)[1]}-{item['agent']}-{uuid.uuid4().hex[:8]}",
            )
            request = RunRequest(
                task=Path(item["package"]), agent=item["agent"], name=item["job"],
                jobs_dir=args.jobs_dir, environment="modal", allow_billable=True,
                concurrency=1, attempts=1, timeout_seconds=600,
                modal_app_name="mimo-clean-census-fleet-20261017",
                modal_resource_policy="limit", modal_sandbox_timeout_seconds=SANDBOX_TTL_SECONDS,
            )
            validate_request(request, repo_root=args.repo_root)
            requests[item["job"]] = request
        for item in pending:
            for _ in range(item["reservation_units"]):
                gate.charge()
            ledger["admissions"].append(item)
        ledger["units_reserved"] = gate.executions
        save(ledger_path, ledger)  # durable before any provider starts, including preparation
        args.jobs_dir.mkdir(parents=True, exist_ok=True)

        def execute(item: dict[str, Any]) -> dict[str, Any]:
            started = time.monotonic()
            request = requests[item["job"]]
            try:
                Executor.from_repo(args.repo_root).execute_direct(request, ingest=False)
                outcome = {"state": "executed", "process_rc": 0}
            except Exception as exc:
                outcome = {"state": "failed", "process_rc": 1, "error": f"{type(exc).__name__}: {exc}"}
            return {**outcome, "wall_seconds": round(time.monotonic() - started, 3),
                    "finished_at": datetime.now(UTC).isoformat()}

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(execute, item): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                item.update(future.result())
                save(ledger_path, ledger)
                print(json.dumps({"admission": item}, sort_keys=True), flush=True)
        return int(any(item["state"] == "failed" for item in pending))


if __name__ == "__main__":
    raise SystemExit(main())
