"""Execute pinned repair-validation matrices through native Eval Lab controls.

The first two runs must be reference and nop, in that order; remaining runs
are admitted known-wrong submissions unless explicitly named as prompt-valid
alternates. Positive controls each use three native verifier invocations.
Controls do not certify model capability. Reservations are not invoices.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.queue import Executor
from evallab.registry import compute_task_digests
from evallab.regrade import CloudSpendGate
from evallab.runner import (
    load_job,
    load_matrix,
    matrix_run_outcome,
    staged_matrix_request,
)

APP = "mimo-clean-census-envcheck-20261017"
APPROVAL = "FOCUS.md Overnight plan 2026-10-17: EnvCheck $4 Modal lane cap"


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def load_gate(path: Path, cap_usd: float, reserve_usd: float) -> CloudSpendGate:
    gate = CloudSpendGate(APPROVAL, cap_usd, reserve_usd)
    if path.exists():
        prior = json.loads(path.read_text())
        for key in ("approval", "cap_usd", "usd_per_execution"):
            if prior.get(key) != gate.to_json()[key]:
                raise ValueError(f"existing spend gate differs at {key}")
        executions = prior.get("executions")
        if type(executions) is not int or executions < 0:
            raise ValueError("invalid persisted reservation count")
        gate.executions = executions
    return gate


def execute_matrix(
    matrix_path: Path,
    *,
    root: Path,
    output: Path,
    gate: CloudSpendGate,
    gate_path: Path,
    sandbox_timeout: int,
    execute: bool,
    valid_alternates: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    matrix = load_matrix(matrix_path)
    if matrix.environment != "modal" or matrix.concurrency != 1:
        raise ValueError("repair matrices must declare Modal and concurrency=1")
    if len(matrix.runs) < 3:
        raise ValueError("qualification requires reference, nop and an admitted wrong submission")
    if [run.expect_reward for run in matrix.runs[:2]] != [1.0, 0.0]:
        raise ValueError("first matrix runs must be reference=1 and nop=0")
    if [run.agent for run in matrix.runs[:2]] != ["oracle", "nop"]:
        raise ValueError("first matrix agents must be oracle and nop")
    if any(run.agent not in {"oracle", "nop"} or run.attempts != 1 for run in matrix.runs):
        raise ValueError("only single-submission model-free controls are admitted")
    tail_names = {run.name for run in matrix.runs[2:]}
    if not valid_alternates <= tail_names:
        raise ValueError("prompt-valid alternate names must identify trailing matrix runs")
    if not tail_names - valid_alternates:
        raise ValueError("qualification requires at least one known-wrong submission")
    for run in matrix.runs[2:]:
        if run.expect_reward != float(run.name in valid_alternates):
            raise ValueError("trailing expectations must match explicit alternate/wrong roles")
        if run.name in valid_alternates and (run.agent != "oracle" or run.solution is None):
            raise ValueError("a prompt-valid alternate needs its own bound solution override")
    package = (root / matrix.task).resolve()
    digests = compute_task_digests(package)
    if digests.package != matrix.task_package_digest or digests.verifier != matrix.verifier_digest:
        raise ValueError("matrix package or verifier digest differs from current bytes")
    matrix_digest = sha256(matrix_path)
    result: dict[str, Any] = {
        "task": matrix.task_id,
        "matrix": str(matrix_path.resolve()),
        "matrix_sha256": matrix_digest,
        "package_digest": digests.package,
        "verifier_digest": digests.verifier,
        "modal_app_name": APP,
        "sandbox_timeout_seconds": sandbox_timeout,
        "valid_alternate_runs": sorted(valid_alternates),
        "checks": [],
        "qualified": False,
    }
    for index, run in enumerate(matrix.runs):
        role = (
            "reference" if index == 0 else "nop" if index == 1 else
            "prompt-valid-alternate" if run.name in valid_alternates else "known-wrong"
        )
        repeated = role != "known-wrong"
        receipt_path = output / matrix.task_id / f"{run.name}.json"
        if receipt_path.exists():
            raise ValueError(
                f"immutable control evidence already exists at {receipt_path}; "
                "use a fresh matrix job name and output path, not an implicit replay cache"
            )
        check: dict[str, Any] = {
            "run": run.name,
            "role": role,
            "expected_reward": run.expect_reward,
            "matrix_sha256": matrix_digest,
            "status": "planned",
            "repeats": [],
        }
        with staged_matrix_request(matrix, run, repo_root=root) as (request, provenance):
            request = dataclasses.replace(
                request,
                allow_billable=True,
                modal_app_name=APP,
                modal_resource_policy="limit",
                modal_sandbox_timeout_seconds=sandbox_timeout,
                verifier_repeat_n=3 if repeated else None,
            )
            check.update(provenance)
            if not execute:
                result["checks"].append(check)
                continue
            gate.charge()
            # Retain the reservation even on process/setup failure, before any
            # provider work can begin. The parent also reconciles actual billing.
            write_json(gate_path, gate.to_json())
            check["started_at"] = datetime.now(UTC).isoformat()
            try:
                job_dir = Executor.from_repo(root, create_queue=False).execute_direct(request)
                check["job_dir"] = str(job_dir)
                check.update(matrix_run_outcome(load_job(job_dir), run))
                if repeated:
                    stabilities = list(job_dir.glob("*/verifier/stability.json"))
                    if len(stabilities) != 1:
                        raise ValueError("expected exactly one native RepeatVerifier stability receipt")
                    stability = json.loads(stabilities[0].read_text())
                    repeats = stability.get("runs", [])
                    check["repeats"] = [entry.get("reward") for entry in repeats]
                    if len(repeats) != 3 or any(entry.get("error") for entry in repeats):
                        check.update(status="infra", error="incomplete or errored repeat-verifier observations")
                    elif check["repeats"] != [run.expect_reward] * 3:
                        check.update(status="mismatch", error="repeat rewards differ from the control expectation")
            except Exception as exc:
                check.update(status="infra", error=f"{type(exc).__name__}: {exc}")
            check["finished_at"] = datetime.now(UTC).isoformat()
            write_json(receipt_path, check)
            print(json.dumps({"task": matrix.task_id, **check}), flush=True)
            result["checks"].append(check)
            if repeated and check["status"] != "ok":
                # A broken reference/nop cannot establish that a wrong patch was killed.
                break
    result["qualified"] = (
        execute
        and len(result["checks"]) == len(matrix.runs)
        and all(check.get("status") == "ok" for check in result["checks"])
    )
    if execute:
        write_json(output / matrix.task_id / "qualification.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--max-usd", type=float, required=True)
    parser.add_argument("--reserve-usd", type=float, required=True)
    parser.add_argument("--sandbox-timeout-seconds", type=int, default=600)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--valid-alternate-run", action="append", default=[],
        help="Explicit trailing prompt-valid positive control; repeatable within one matrix",
    )
    args = parser.parse_args()
    if args.max_usd > 4.0:
        parser.error("the current EnvCheck approval is at most $4 Modal")
    if args.sandbox_timeout_seconds < 1:
        parser.error("sandbox timeout must be positive")
    root = args.root.resolve()
    output = args.out.resolve()
    if not output.is_relative_to(root / "runs"):
        parser.error("qualification output must live under this owned worktree's runs/")
    gate = load_gate(args.budget, args.max_usd, args.reserve_usd)
    os.environ["EVALLAB_STATE_JOURNAL"] = "off"
    os.environ["EVALLAB_DERIVED_ROOT"] = str(root / "derived" / "parquet")
    rows = [execute_matrix(path, root=root, output=output, gate=gate, gate_path=args.budget,
                           sandbox_timeout=args.sandbox_timeout_seconds, execute=args.execute,
                           valid_alternates=frozenset(args.valid_alternate_run))
            for path in args.matrix]
    print(json.dumps({"execute": args.execute, "tasks": rows, "reservations": gate.to_json()}, indent=2))
    return 0 if not args.execute or all(row["qualified"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
