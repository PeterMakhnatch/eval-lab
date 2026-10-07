#!/usr/bin/env python3
"""HAR-191's explicitly authorized, batch-fenced direct Modal verifier replay.

Run from the repository with ``uv run --with modal==1.6.1 python``. Without
--execute this only displays a plan. This is not a Harbor trial or model run.
Cold image pulls are the accepted residual risk in the 2026-10-07 disposition;
provider-observed costs and conservative Sandbox exposure remain distinct.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import results
import runtime
from budget import (
    CAP_USD,
    CPU_LIMIT,
    MAX_BATCH_TASKS,
    MAX_OPEN_TASKS,
    MEMORY_LIMIT_MIB,
    SANDBOX_SECONDS,
    BudgetError,
    BudgetLedger,
    billing_snapshot,
    per_sandbox_worst_case,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
AUTHORITY = "HAR-191 Research-Harbor delegate disposition 2026-10-07T04:06Z; direct Modal batch-enforced $2 exception"
SDK_VERSION = "1.6.1"
RATE_KEYS = ("cpu_hour_cost_sandbox", "mem_gib_hour_cost_sandbox", "egress_gib_cost")


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _load(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _task_id(value: str) -> str:
    return f"format-code-task-{value}" if len(value) == 6 and value.isdecimal() else value


def _source_snapshot(directory: Path) -> dict[str, str]:
    hashes = {}
    for name in ("extract.py", "results.py", "runtime.py", "budget.py", "sweep.py"):
        content = (HERE / name).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        destination = directory / "source" / digest / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError("Retained source content conflicts with its digest")
        if not destination.exists():
            destination.write_bytes(content)
        hashes[name] = digest
    return hashes


def _operational(task: dict, status: str, reason: str) -> dict:
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "run_digest": task["run_digest"],
        "image": task["image"],
        "test_patch_sha256": task["test_patch_sha256"],
        "base": None,
        "execution_status": status,
        "execution_reason": reason,
        "method": "direct-modal-verifier-replay",
        "arms": {},
        "extraction": {"status": "input-error", "rationale": reason, "fix": None},
    }


def _unknown_allocations(count: int, reason: str) -> list[dict]:
    # An unexpected runtime exception cannot prove that no Sandbox was created.
    return [
        {
            "sandbox_id": None,
            "creation_attempted": True,
            "allocation_state": "unknown",
            "terminal_confirmed": False,
            "lifetime_upper_seconds": SANDBOX_SECONDS,
            "creation_started_at": None,
            "terminated_at": None,
            "error": reason,
        }
        for _ in range(count)
    ]


class Sweep:
    def __init__(self, args: argparse.Namespace, manifest: dict, manifest_sha: str):
        self.args = args
        self.manifest = manifest
        self.directory = args.evidence.resolve()
        self.receipts_dir = self.directory / "receipts"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.directory / "run.json"
        self.tasks = {task["task_id"]: task for task in manifest["tasks"]}
        self.observations = {}
        for path in sorted(self.receipts_dir.glob("*.json")):
            receipt = _load(path)
            task_id = receipt.get("task_id")
            if task_id not in self.tasks or task_id in self.observations:
                raise ValueError(f"Unexpected or duplicate retained receipt: {path}")
            self.observations[task_id] = receipt
        if self.metadata_path.exists():
            self.metadata = _load(self.metadata_path)
            if (
                self.metadata.get("manifest_sha256") != manifest_sha
                or self.metadata.get("authority") != AUTHORITY
            ):
                raise ValueError("Existing study identity differs; do not reset its budget")
            if self.metadata.get("app_ids") and not (self.directory / "budget.json").is_file():
                raise ValueError(
                    "Existing App custody has no budget journal; do not reset exposure"
                )
        else:
            if (self.directory / "budget.json").exists():
                raise ValueError(
                    "Existing budget journal has no study custody; do not create a new run"
                )
            self.metadata = {
                "schema_version": 1,
                "run_id": uuid.uuid4().hex[:12],
                "manifest_path": str(args.manifest.resolve()),
                "manifest_sha256": manifest_sha,
                "authority": AUTHORITY,
                "started_at": _utc(),
                "app_ids": [],
                "invocations": [],
                "environment": "main",
                "locked_order": "ascending frozen image_mib (unknown last), then task_id",
                "open_selection": "first eligible completed locked failures in that order; not a population-random sample",
            }
        self.source_hashes = _source_snapshot(self.directory)
        self.metadata["invocations"].append(
            {
                "started_at": _utc(),
                "source_sha256": self.source_hashes,
                "task_filter": args.task,
                "max_open": args.max_open,
            }
        )
        _write(self.metadata_path, self.metadata)
        self.started_at = datetime.fromisoformat(self.metadata["started_at"])
        self.cancelled = False
        self.workspace: Any = None
        self.app: Any = None
        self.ledger: BudgetLedger
        self.rates: dict[str, str] = {}

    def ordered(self, task_ids: set[str] | None = None) -> list[dict]:
        rows = self.tasks.values() if task_ids is None else (self.tasks[key] for key in task_ids)
        return sorted(
            rows,
            key=lambda task: (
                task["image_mib"] if task.get("image_mib") is not None else float("inf"),
                task["task_id"],
            ),
        )

    async def snapshot(self) -> dict:
        observed = await billing_snapshot(
            self.workspace, list(self.metadata["app_ids"]), self.started_at
        )
        self.ledger.observe(observed)
        return observed

    def publish(self, state: str) -> dict:
        observations = [
            (self.receipts_dir / f"{task_id}.json", receipt)
            for task_id, receipt in sorted(self.observations.items())
        ]
        summary = results.write_sweep(
            self.manifest,
            observations,
            self.args.output.resolve(),
            self.directory / "coverage.json",
        )
        summary.update(
            {
                "state": state,
                "authority": AUTHORITY,
                "manifest_sha256": self.metadata["manifest_sha256"],
                "actual_provider_usd": str(self.ledger.actual_usd),
                "conservative_exposure_usd": str(self.ledger.exposure_usd),
                "cap_usd": str(CAP_USD),
                "budget_journal": str(self.directory / "budget.json"),
                "admitted_tasks": len(
                    {
                        task
                        for batch in self.ledger.batches
                        if batch["kind"] == "locked"
                        for task in batch["task_ids"]
                    }
                ),
                "sandbox_attempted_tasks": sum(
                    any(report.get("creation_attempted") for report in receipt.get("sandboxes", []))
                    for receipt in self.observations.values()
                ),
                "open_tasks": sorted(self.ledger.open_task_ids),
                "app_ids": list(self.metadata["app_ids"]),
                "pending_cleanup": self.ledger.pending,
                "billing_limit": "Observed reports may lag; retained runtime bounds are not claimed as settled invoices. Cold imports remain the explicitly accepted residual risk.",
            }
        )
        _write(self.directory / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True), flush=True)
        return summary

    def retain(self, receipt: dict) -> None:
        task_id = receipt["task_id"]
        _write(self.receipts_dir / f"{task_id}.json", receipt)
        self.observations[task_id] = receipt

    async def batch(self, candidates: list[dict], kind: str, maximum: int) -> int:
        import modal

        self.app = modal.App(
            name=f"har191-oracle-{self.metadata['run_id']}-{len(self.metadata['app_ids']) + 1:03d}",
            tags={"linear_card": "HAR-191", "run_id": self.metadata["run_id"], "arm_kind": kind},
        )
        # Registration has no compute. Each batch gets disjoint billing identity;
        # no image build or Sandbox starts until its reservation is durable.
        async with self.app.run(environment_name="main"):
            app_id = self.app.app_id
            if not isinstance(app_id, str) or not app_id or app_id in self.metadata["app_ids"]:
                raise RuntimeError("Modal did not return a fresh App identity")
            self.metadata["app_ids"].append(app_id)
            _write(self.metadata_path, self.metadata)
            return await self._batch_in_app(candidates, kind, maximum)

    async def _batch_in_app(self, candidates: list[dict], kind: str, maximum: int) -> int:
        snapshot = await self.snapshot()
        exposure = max(self.ledger.exposure_usd, Decimal(snapshot["actual_usd"]))
        slots = 2 if kind == "locked" else 1
        task_reservation = per_sandbox_worst_case(self.rates) * slots
        affordable = max(0, int((CAP_USD - exposure) / task_reservation))
        count = min(len(candidates), maximum, MAX_BATCH_TASKS, affordable)
        if count == 0:
            _write(
                self.directory / "admission-stop.json",
                {
                    "at": _utc(),
                    "kind": kind,
                    "actual_snapshot": snapshot,
                    "exposure_usd": str(exposure),
                    "per_task_reservation_usd": str(task_reservation),
                    "remaining_task_ids": [task["task_id"] for task in candidates],
                },
            )
            return 0
        chosen = candidates[:count]
        admitted = self.ledger.reserve(
            [task["task_id"] for task in chosen],
            count * slots,
            snapshot,
            kind=kind,
            app_id=self.app.app_id,
        )
        batch_dir = self.directory / "batches" / admitted["batch_id"]
        _write(
            batch_dir / "admission.json",
            {**admitted, "app_id": self.app.app_id, "source_sha256": self.source_hashes},
        )
        semaphore = asyncio.Semaphore(self.args.concurrency)
        started: set[str] = set()

        async def one(task: dict) -> dict:
            async with semaphore:
                task_id = task["task_id"]
                started.add(task_id)
                if kind == "locked":
                    return await runtime.run_pair(
                        task, self.app, self.directory / "tasks" / task_id, HERE / "extract.py"
                    )
                return await runtime.run_open(
                    task,
                    self.observations[task_id],
                    self.app,
                    self.directory / "tasks" / task_id / "open",
                )

        jobs = [asyncio.create_task(one(task)) for task in chosen]
        try:
            returned = await asyncio.gather(*jobs, return_exceptions=True)
        except asyncio.CancelledError:
            self.cancelled = True
            for job in jobs:
                job.cancel()
            returned = await asyncio.shield(asyncio.gather(*jobs, return_exceptions=True))
        reports = []
        for task, result in zip(chosen, returned, strict=True):
            task_id = task["task_id"]
            if isinstance(result, BaseException):
                reason = (
                    f"runtime did not return allocation custody: {type(result).__name__}: {result}"
                )
                if task_id in started:
                    reports.extend(_unknown_allocations(slots, reason))
                else:
                    reports.extend(
                        {
                            "sandbox_id": None,
                            "creation_attempted": False,
                            "terminal_confirmed": True,
                            "lifetime_upper_seconds": 0,
                            "error": "cancelled before entering the runtime",
                        }
                        for _ in range(slots)
                    )
                if kind == "locked":
                    receipt = _operational(task, "infrastructure-error", reason)
                else:
                    receipt = dict(self.observations[task_id])
                    receipt["arms"] = {
                        **receipt["arms"],
                        "open_oracle": {"status": "infrastructure-error", "reason": reason},
                    }
            elif kind == "locked":
                receipt = result
                reports.extend(result["sandboxes"])
                self.cancelled = self.cancelled or bool(result.get("cancelled"))
            else:
                receipt = dict(self.observations[task_id])
                receipt["arms"] = {**receipt["arms"], "open_oracle": result["arm"]}
                receipt["open_sandbox"] = result["sandbox"]
                reports.append(result["sandbox"])
                self.cancelled = self.cancelled or bool(result["arm"].get("cancelled"))
            receipt["last_batch_id"] = admitted["batch_id"]
            receipt["last_app_id"] = self.app.app_id
            receipt["source_sha256"] = self.source_hashes
            self.retain(receipt)
        # Keep allocation evidence even if the provider's billing API is unavailable.
        _write(batch_dir / "allocation-results.json", {"sandboxes": reports, "at": _utc()})
        after = await self.snapshot()
        settled = self.ledger.finish(admitted["batch_id"], after, reports)
        _write(batch_dir / "finish.json", settled)
        self.publish("pending-cleanup" if self.ledger.pending else "running")
        return count

    async def confirm_open(self) -> None:
        while not self.cancelled and not self.ledger.pending:
            remaining = self.args.max_open - len(self.ledger.open_task_ids)
            if remaining <= 0:
                return
            candidates = [
                task
                for task in self.ordered()
                if task["task_id"] in self.observations
                and task["task_id"] not in self.ledger.open_task_ids
                and results.classify_receipt(self.observations[task["task_id"]])["label"]
                == "oracle:fail"
            ]
            if not candidates or not await self.batch(candidates, "open", remaining):
                return

    async def execute(self) -> int:
        import modal

        if importlib.metadata.version("modal") != SDK_VERSION:
            raise ValueError(f"Use the approved Modal SDK {SDK_VERSION}")
        self.workspace = modal.Workspace.from_context()
        live_rates = await self.workspace.billing.rates.aio()
        self.rates = {key: str(live_rates[key]) for key in RATE_KEYS}
        _write(self.directory / "rates.json", {"observed_at": _utc(), "rates": self.rates})
        self.ledger = BudgetLedger(
            self.directory / "budget.json", self.rates, self.metadata["manifest_sha256"], AUTHORITY
        )
        if self.ledger.pending:
            self.publish("pending-cleanup")
            raise BudgetError("Existing batch has unresolved allocation/cleanup; no new launches")
        selected = set(self.args.task) if self.args.task else set(self.tasks)
        admitted_ids = {
            task
            for batch in self.ledger.batches
            if batch["kind"] == "locked"
            for task in batch["task_ids"]
        }
        missing = admitted_ids - set(self.observations)
        if missing:
            raise ValueError(
                f"Admitted tasks lack retained receipts; do not relaunch: {sorted(missing)}"
            )
        pending = self.ordered(selected - admitted_ids)
        state = "complete"
        while pending and not self.cancelled and not self.ledger.pending:
            count = await self.batch(pending, "locked", self.args.batch_size)
            if count == 0:
                state = "budget-stopped"
                break
            pending = pending[count:]
            await self.confirm_open()
        if not self.cancelled and not self.ledger.pending:
            await self.confirm_open()
        if self.cancelled:
            state = "cancelled"
        elif self.ledger.pending:
            state = "pending-cleanup"
        elif self.args.task and state == "complete":
            state = "selected-slice-complete"
        if state == "budget-stopped":
            for task in pending:
                self.retain(
                    _operational(
                        task,
                        "budget-stopped",
                        "No batch fits actual-plus-retained-exposure and both fresh Sandbox reservations within $2",
                    )
                )
        if self.metadata["app_ids"]:
            _write(self.directory / "final-billing.json", await self.snapshot())
        self.publish(state)
        return 130 if self.cancelled else 2 if self.ledger.pending else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "research/experiments/python-task-ledger/oracle_sweep.csv",
    )
    parser.add_argument("--task", action="append", type=_task_id, default=[])
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-open", type=int, default=MAX_OPEN_TASKS)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH_TASKS or not 1 <= args.concurrency <= MAX_BATCH_TASKS:
        parser.error("batch size and concurrency must be in1..100")
    if not 0 <= args.max_open <= MAX_OPEN_TASKS:
        parser.error("open sample must be in0..20")
    manifest = _load(args.manifest)
    tasks = manifest.get("tasks")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or not isinstance(tasks, list)
        or not tasks
    ):
        parser.error("manifest requires schema_version1 and a nonempty task list")
    for task in tasks:
        if not isinstance(task, dict):
            parser.error("manifest tasks must be objects")
        problem = results._identity_error(task, require_base=False)
        if problem:
            parser.error(f"manifest: {problem}")
    ids = [task["task_id"] for task in tasks]
    if (
        len(ids) != len(set(ids))
        or len(args.task) != len(set(args.task))
        or not set(args.task).issubset(ids)
    ):
        parser.error("duplicate or unknown task selection")
    manifest_sha = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    protected = {
        args.manifest.resolve(),
        *(
            args.evidence.resolve() / name
            for name in ("budget.json", "run.json", "coverage.json", "summary.json")
        ),
    }
    if args.output.resolve() in protected:
        parser.error("scientific CSV cannot overwrite study inputs or state")
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "plan-only",
                    "allocations": 0,
                    "requested_tasks": len(tasks),
                    "selected_tasks": len(args.task) or len(tasks),
                    "manifest_sha256": manifest_sha,
                    "authority": AUTHORITY,
                    "limits": {
                        "physical_cpu_limit": CPU_LIMIT,
                        "equivalent_vcpu": 2,
                        "memory_limit_mib": MEMORY_LIMIT_MIB,
                        "sandbox_seconds": SANDBOX_SECONDS,
                        "sandboxes_per_locked_task": 2,
                        "max_batch_tasks": args.batch_size,
                        "max_open_tasks": args.max_open,
                        "cap_usd": str(CAP_USD),
                    },
                },
                sort_keys=True,
            )
        )
        return 0
    args.evidence.mkdir(parents=True, exist_ok=True)
    with (args.evidence / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study = Sweep(args, manifest, manifest_sha)
        try:
            return asyncio.run(study.execute())
        except (Exception, KeyboardInterrupt) as exc:
            _write(
                args.evidence / "controller-error.json",
                {
                    "at": _utc(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "action": "Inspect retained reservations and actual provider IDs; do not reset or blindly relaunch",
                },
            )
            print(f"Sweep stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
