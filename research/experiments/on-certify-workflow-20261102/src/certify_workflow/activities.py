"""Fake deterministic activities for CertifyGold workflow.

All activities are deterministic stubs with injectable failures and costs:
no Modal calls, no model calls, no real money spent.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity

from certify_workflow.exceptions import InfraError
from certify_workflow.models import (
    CellResult,
    CellSpec,
    CorpusManifest,
    FakeCellConfig,
    MutantSummary,
    PackageSpec,
    SettleSpendRequest,
    SpendSummary,
)

OPERATORS = (
    "boundary_flip",
    "equality_flip",
    "guard_drop",
    "handler_drop",
    "call_drop",
    "keyword_drop",
    "bool_flip",
    "int_shift",
)


@activity.defn
async def generate_mutants(pkg: PackageSpec | dict[str, Any]) -> list[MutantSummary]:
    """Generate deterministic AST semantic mutants for a package."""
    if isinstance(pkg, dict):
        pkg = PackageSpec(**pkg)
    if pkg.inject_failures.get("generate_mutants"):
        raise InfraError(f"Injected AST parser crash for package {pkg.package_id}")

    count = max(1, pkg.mutant_count)
    mutants: list[MutantSummary] = []
    half = (count + 1) // 2

    for i in range(count):
        op = OPERATORS[i % len(OPERATORS)]
        line = 50 + (i * 12)
        diff = f"--- a/task.py\n+++ b/task.py\n@@ -{line},3 +{line},3 @@\n-{op} original\n+{op} mutant"
        m_hash = hashlib.sha256(f"{pkg.package_id}:{op}:{line}".encode()).hexdigest()[:12]
        is_train = i < half
        mutants.append(
            MutantSummary(
                operator=op,
                line=line,
                diff=diff,
                sha256=m_hash,
                is_train=is_train,
            )
        )
    return mutants


@activity.defn
async def synthesize_tests(
    pkg: PackageSpec | dict[str, Any],
    train_mutants: list[MutantSummary | dict[str, Any]],
) -> dict[str, Any]:
    """Synthesize behavioral unit tests using train mutants (fake LLM synthesizer)."""
    if isinstance(pkg, dict):
        pkg = PackageSpec(**pkg)
    muts = [MutantSummary(**m) if isinstance(m, dict) else m for m in train_mutants]
    if pkg.inject_failures.get("synthesize_tests"):
        raise InfraError(f"Injected synthesizer API disconnect for package {pkg.package_id}")

    test_code = (
        "# Contract-complete synthesized behavioral test\n"
        "import unittest\n\n"
        "class TestGoldContract(unittest.TestCase):\n"
    )
    for m in muts:
        test_code += f"    def test_kill_{m.operator}_L{m.line}(self):\n        pass\n"

    return {
        "status": "ok",
        "code": test_code,
        "train_mutant_count": len(muts),
        "synth_cost_usd": 0.0,
    }


@activity.defn
async def run_cell(
    spec: CellSpec | dict[str, Any],
    config: FakeCellConfig | dict[str, Any],
) -> CellResult:
    """Run an evaluation cell idempotently.

    Checks disk cache (<results_dir>/<cell_key>/result.json) first.
    If cached, returns instantly with cache_hit=True and cost_usd=$0.00.
    If uncached, executes deterministic stub, logs to ledger, saves cache, and returns result.
    """
    if isinstance(spec, dict):
        spec = CellSpec(**spec)
    if isinstance(config, dict):
        config = FakeCellConfig(**config)
    results_root = Path(config.results_dir) if config.results_dir else Path("/tmp/certify_cells")
    cell_dir = results_root / spec.cell_key
    result_file = cell_dir / "result.json"

    # 1. Idempotency Check: return cached result at zero cost
    if result_file.is_file():
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            return CellResult(
                cell_key=data["cell_key"],
                package_id=data["package_id"],
                cell_type=data["cell_type"],
                exit_code=data.get("exit_code", 0),
                reward=float(data.get("reward", 0.0)),
                cost_usd=0.0,  # Cache hit is free
                wall_seconds=0.0,
                cache_hit=True,
                error=data.get("error"),
                timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            )
        except Exception:
            pass  # Corrupted cache file: recompute

    # 2. Injectable Infrastructure Failure Check (with persistent attempt tracking across retries)
    if spec.cell_key in config.inject_infra_errors:
        target_attempts = config.inject_infra_errors[spec.cell_key]
        state_dir = results_root / ".infra_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        attempt_file = state_dir / f"{spec.cell_key}.attempts"

        current_attempt = 0
        if attempt_file.is_file():
            try:
                current_attempt = int(attempt_file.read_text(encoding="utf-8").strip())
            except ValueError:
                current_attempt = 0

        if current_attempt < target_attempts:
            attempt_file.write_text(str(current_attempt + 1), encoding="utf-8")
            raise InfraError(
                f"Injected transient Modal infra failure for {spec.cell_key} "
                f"(attempt {current_attempt + 1}/{target_attempts})"
            )

    # 3. Simulate latency if configured
    if config.latency_seconds > 0:
        import asyncio

        await asyncio.sleep(config.latency_seconds)

    # 4. Candidate Submission Failure (real evaluated result, not an infra error)
    if spec.cell_key in config.inject_submission_failures:
        res = CellResult(
            cell_key=spec.cell_key,
            package_id=spec.package_id,
            cell_type=spec.cell_type,
            exit_code=1,
            reward=0.0,
            cost_usd=config.base_cost_usd,
            wall_seconds=0.05,
            cache_hit=False,
            error="AssertionError: candidate test failed within container",
            timestamp=datetime.now(UTC).isoformat(),
        )
    else:
        # 5. Compute Deterministic Result by Cell Type
        reward = 0.0
        if spec.cell_type == "reference":
            reward = 1.0
        elif spec.cell_type in ("nop", "witness", "exit0_pth"):
            reward = 0.0
        elif spec.cell_type == "train_mutant":
            reward = 0.0  # Killed by synthesized contract test
        elif spec.cell_type == "eval_mutant":
            # Check if this eval mutant was killed
            reward = 0.0 if spec.cell_key in config.killed_eval_mutants else 1.0
        elif spec.cell_type == "eval_base_mutant":
            reward = 1.0  # Base grader accepted the mutant (proves weakness)
        else:
            reward = spec.expected_reward

        res = CellResult(
            cell_key=spec.cell_key,
            package_id=spec.package_id,
            cell_type=spec.cell_type,
            exit_code=0,
            reward=reward,
            cost_usd=config.base_cost_usd,
            wall_seconds=0.05,
            cache_hit=False,
            error=None,
            timestamp=datetime.now(UTC).isoformat(),
        )

    # 6. Atomic Cache Write
    cell_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = cell_dir / f"result.tmp.{os.getpid()}"
    tmp_file.write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
    tmp_file.replace(result_file)

    # 7. Record to execution ledger if configured
    if config.ledger_file:
        ledger_path = Path(config.ledger_file)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": "run_cell",
            "cell_key": spec.cell_key,
            "package_id": spec.package_id,
            "cell_type": spec.cell_type,
            "cost_usd": res.cost_usd,
            "cache_hit": False,
            "timestamp": res.timestamp,
        }
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    return res


@activity.defn
async def settle_spend(req: SettleSpendRequest | dict[str, Any]) -> SpendSummary:
    """Settle spend against the durable ledger."""
    if isinstance(req, dict):
        req = SettleSpendRequest(**req)
    if req.ledger_file:
        ledger_path = Path(req.ledger_file)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "event": "settle_spend",
            "package_id": req.package_id,
            "reserved_usd": req.reserved_usd,
            "actual_usd": req.actual_usd,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    return SpendSummary(
        package_id=req.package_id,
        reserved_usd=req.reserved_usd,
        actual_usd=req.actual_usd,
        cumulative_spend_usd=req.actual_usd,
    )


@activity.defn
async def write_manifest(manifest: CorpusManifest | dict[str, Any], output_dir: str) -> str:
    """Write corpus certification manifest to disk."""
    manifest_data = manifest if isinstance(manifest, dict) else manifest.to_dict()
    out = Path(output_dir) if output_dir else Path("/tmp/manifests")
    out.mkdir(parents=True, exist_ok=True)
    manifest_file = out / "gold-v1-manifest.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    return str(manifest_file)
