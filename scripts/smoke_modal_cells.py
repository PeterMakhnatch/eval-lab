"""Modal smoke test for S1-MODAL-CELLS: runner resume and cache hit spend accounting.

Contract:
- Cap $0.15 total
- Approval: "Peter 2026-11-02 overnight S1-MODAL-CELLS: Modal cap $0.15"
- Run batch segmentation across runner instantiations.
- Validates cache hit reuse ($0) and incremental batch completion.
- Records reserved versus settled spend in persistent ledger.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.gold_certifier import exit0_pth_script, reference_script
from evallab.modal_cells import (
    CellSpec,
    ModalCellRunner,
    ReserveSettleSpendGate,
)
from evallab.production.corpus_queue import CorpusRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_task_dir() -> Path:
    candidates = [
        REPO_ROOT / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/format-code-task-003005",
        Path.home() / "Developer/eval-lab/derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/format-code-task-003005",
        REPO_ROOT.parent.parent / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/format-code-task-003005",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


TASK_DIR = _resolve_task_dir()
PATCH_FILE = (
    REPO_ROOT / "research/experiments/mimo-v6-full-fleet/references/format-code-task-003005.patch"
)
STATE_DIR = Path.home() / ".local/state/evallab-failure-library"
LEDGER_PATH = STATE_DIR / "s1-modal-cells-smoke-ledger.json"
SPEND_LOG = STATE_DIR / "spend.jsonl"

RESULTS_ROOT = Path.home() / "Developer/eval-lab-results/2026-11-02/S1-MODAL-CELLS"
CELLS_DIR = RESULTS_ROOT / "cells"
RECEIPTS_DIR = (
    Path.home()
    / "Developer/research-context/bridge/2026-11-02-overnight/infra/raw/S1-MODAL-CELLS/receipts"
)

APPROVAL = "Peter 2026-11-02 overnight S1-MODAL-CELLS: Modal cap $0.15"
CAP_USD = 0.15
MODAL_APP = "s1-modal-cells"
BILLING_TAG = "s1-modal-cells"


def build_cell_specs() -> list[CellSpec]:
    """Build the 6 trivial evaluation cells for format-code-task-003005."""
    patch_text = PATCH_FILE.read_text(encoding="utf-8")
    ref_script = reference_script("/testbed", patch_text)
    nop_script = "cd /testbed || exit 1\necho NOP_APPLIED\n"
    pth_script = exit0_pth_script("/testbed")

    # Mutants from last night's gold-certifier receipts
    prior_cells = (
        Path.home()
        / "Developer/eval-lab-results/2026-11-01/w3b-exhaustive-gold-certifier/test_cells/003005"
    )
    script_call_drop = (prior_cells / "evalbase-call_drop-L82/agent-script.sh").read_text(
        encoding="utf-8"
    )
    script_int_shift = (prior_cells / "evalbase-int_shift-L428/agent-script.sh").read_text(
        encoding="utf-8"
    )
    script_boundary = (prior_cells / "evalbase-boundary_flip-L178/agent-script.sh").read_text(
        encoding="utf-8"
    )

    return [
        CellSpec(
            package=TASK_DIR,
            variant="nop",
            submission=nop_script,
            task_id="003005",
            cell_id="nop",
        ),
        CellSpec(
            package=TASK_DIR,
            variant="reference",
            submission=ref_script,
            task_id="003005",
            cell_id="reference",
        ),
        CellSpec(
            package=TASK_DIR,
            variant="exit0_pth",
            submission=pth_script,
            task_id="003005",
            cell_id="exit0_pth",
        ),
        CellSpec(
            package=TASK_DIR,
            variant="evalbase-call_drop-L82",
            submission=script_call_drop,
            task_id="003005",
            cell_id="evalbase-call_drop-L82",
        ),
        CellSpec(
            package=TASK_DIR,
            variant="evalbase-int_shift-L428",
            submission=script_int_shift,
            task_id="003005",
            cell_id="evalbase-int_shift-L428",
        ),
        CellSpec(
            package=TASK_DIR,
            variant="evalbase-boundary_flip-L178",
            submission=script_boundary,
            task_id="003005",
            cell_id="evalbase-boundary_flip-L178",
        ),
    ]


def run_smoke() -> dict[str, Any]:
    print("=" * 70)
    print("S1-MODAL-CELLS REAL MODAL SMOKE TEST")
    print(f"Approval: {APPROVAL}")
    print(f"Cap: ${CAP_USD:.2f}")
    print(f"Package: {TASK_DIR.name}")
    print(f"Results dir: {CELLS_DIR}")
    print(f"Ledger: {LEDGER_PATH}")
    print("=" * 70)
    # Clean slate for smoke test: move existing ledger aside instead of deleting
    CELLS_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    registry = CorpusRegistry(REPO_ROOT)
    if not registry.has_assignment_authority():
        raise RuntimeError("CorpusRegistry lacks split assignment authority")
    if registry.assigned_test_anywhere(task_id="format-code-task-003005") or registry.sealed_identity(task_id="format-code-task-003005"):
        raise RuntimeError("Task format-code-task-003005 is marked TEST or held out; refusing")
    if LEDGER_PATH.exists():
        backup = LEDGER_PATH.with_name(f"{LEDGER_PATH.name}.backup-{int(time.time())}")
        LEDGER_PATH.rename(backup)
        print(f"Moved existing ledger aside to {backup}")
    lock_file = LEDGER_PATH.with_name(LEDGER_PATH.name + ".lock")
    if lock_file.exists():
        lock_file.unlink()
    # Clear previous cells in smoke dir if any
    for child in CELLS_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)

    all_cells = build_cell_specs()
    assert len(all_cells) == 6, f"Expected 6 cells, got {len(all_cells)}"

    # -----------------------------------------------------------------------
    # Step 1: Run 1 Part 1 - Run 2 cells to populate cache, then restart runner
    # -----------------------------------------------------------------------
    print("\n>>> STEP 1: Run 1 (Part 1) - Launch 2 cells, then restart runner to test resume")
    gate1 = ReserveSettleSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        default_reservation_usd=0.003,
        spend_ledger=LEDGER_PATH,
        spend_log=SPEND_LOG,
        modal_app_name=MODAL_APP,
        billing_tag=BILLING_TAG,
    )
    runner1 = ModalCellRunner(
        repo_root=REPO_ROOT,
        spend_gate=gate1,
        results_dir=CELLS_DIR,
        timeout_seconds=300,
    )

    t0 = time.monotonic()
    # Run only first 2 cells to simulate kill midway
    step1_cells = all_cells[:2]
    step1_results = runner1.run_batch(
        step1_cells,
        concurrency=2,
        on_status=lambda st: print(f"  [Step 1] {st.summary_line()}", flush=True),
    )
    step1_wall = time.monotonic() - t0
    step1_summary = gate1.summary()
    print(f"  Step 1 finished in {step1_wall:.1f}s")
    print(f"  Step 1 cells finished: {len(step1_results)}")
    print(f"  Step 1 settled: ${step1_summary['settled_usd']:.5f}")
    assert len(step1_results) == 2, f"Expected 2 cells, got {len(step1_results)}"
    assert all(not r.get("cache_hit") for r in step1_results), "Step 1 cells should be cache misses"

    # Simulate abrupt stop / process boundary
    del runner1
    del gate1

    # -----------------------------------------------------------------------
    # Step 2: Run 1 Part 2 - Resume batch with all 6 cells
    # -----------------------------------------------------------------------
    print("\n>>> STEP 2: Run 1 (Part 2) - Resume batch with all 6 cells, verifying cache hits")
    gate2 = ReserveSettleSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        default_reservation_usd=0.003,
        spend_ledger=LEDGER_PATH,
        spend_log=SPEND_LOG,
        modal_app_name=MODAL_APP,
        billing_tag=BILLING_TAG,
    )
    runner2 = ModalCellRunner(
        repo_root=REPO_ROOT,
        spend_gate=gate2,
        results_dir=CELLS_DIR,
        timeout_seconds=300,
    )

    plan2 = runner2.plan(all_cells)
    print(f"  Plan on resume: {plan2.summary()}")
    assert plan2.cached == 2, f"Expected 2 cached cells on resume, got {plan2.cached}"
    assert plan2.uncached == 4, f"Expected 4 uncached cells on resume, got {plan2.uncached}"

    t0 = time.monotonic()
    step2_results = runner2.run_batch(
        all_cells,
        concurrency=4,
        on_status=lambda st: print(f"  [Step 2] {st.summary_line()}", flush=True),
    )
    step2_wall = time.monotonic() - t0
    step2_summary = gate2.summary()
    print(f"  Step 2 finished in {step2_wall:.1f}s")
    print(f"  Step 2 total results: {len(step2_results)}")
    print(f"  Step 2 total settled: ${step2_summary['settled_usd']:.5f}")
    assert len(step2_results) == 6, f"Expected 6 cells in resumed run, got {len(step2_results)}"

    step2_cached = [r for r in step2_results if r.get("cache_hit") is True]
    step2_executed = [r for r in step2_results if not r.get("cache_hit")]
    print(f"  Resumed run cache hits: {len(step2_cached)} (cost: $0.00)")
    print(f"  Resumed run newly executed: {len(step2_executed)}")
    assert len(step2_cached) == 2, f"Expected 2 cache hits, got {len(step2_cached)}"
    assert len(step2_executed) == 4, f"Expected 4 new executions, got {len(step2_executed)}"

    del runner2
    del gate2

    # -----------------------------------------------------------------------
    # Step 3: Run 2 - Second run of all 6 cells (must be 6 cache hits at $0)
    # -----------------------------------------------------------------------
    print("\n>>> STEP 3: Run 2 - Complete second run of all 6 cells (target: 6 cache hits at $0)")
    gate3 = ReserveSettleSpendGate(
        approval=APPROVAL,
        cap_usd=CAP_USD,
        default_reservation_usd=0.003,
        spend_ledger=LEDGER_PATH,
        spend_log=SPEND_LOG,
        modal_app_name=MODAL_APP,
        billing_tag=BILLING_TAG,
    )
    runner3 = ModalCellRunner(
        repo_root=REPO_ROOT,
        spend_gate=gate3,
        results_dir=CELLS_DIR,
        timeout_seconds=300,
    )

    plan3 = runner3.plan(all_cells)
    print(f"  Plan for Run 2: {plan3.summary()}")
    assert plan3.cached == 6, f"Expected 6 cached cells in Run 2, got {plan3.cached}"
    assert plan3.uncached == 0, f"Expected 0 uncached cells in Run 2, got {plan3.uncached}"

    t0 = time.monotonic()
    spend_before = gate3.summary()["settled_usd"]
    step3_results = runner3.run_batch(
        all_cells,
        concurrency=4,
        on_status=lambda st: print(f"  [Step 3] {st.summary_line()}", flush=True),
    )
    step3_wall = time.monotonic() - t0
    step3_summary = gate3.summary()
    spend_after = step3_summary["settled_usd"]

    print(f"  Step 3 finished in {step3_wall:.3f}s")
    print(f"  Step 3 results: {len(step3_results)}")
    print(f"  Spend before: ${spend_before:.5f}, Spend after: ${spend_after:.5f}")
    assert len(step3_results) == 6
    assert all(r.get("cache_hit") is True for r in step3_results), "All 6 cells in Run 2 must be cache hits"
    assert spend_after == spend_before, f"Spend changed in Run 2: {spend_before} -> {spend_after}"

    # Read final raw ledger
    raw_ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))

    receipt = {
        "benchmark": "S1-MODAL-CELLS",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "approval": APPROVAL,
        "cap_usd": CAP_USD,
        "task_id": "format-code-task-003005",
        "step1_interrupted": {
            "cells_planned": 2,
            "cells_completed": len(step1_results),
            "wall_seconds": round(step1_wall, 2),
            "settled_usd": step1_summary["settled_usd"],
            "reserved_usd": step1_summary["reserved_usd"],
        },
        "step2_resumed": {
            "cells_total": 6,
            "cache_hits": len(step2_cached),
            "new_executions": len(step2_executed),
            "wall_seconds": round(step2_wall, 2),
            "settled_usd": step2_summary["settled_usd"],
            "reserved_usd": step2_summary["reserved_usd"],
        },
        "step3_second_run": {
            "cells_total": 6,
            "cache_hits": len(step3_results),
            "new_executions": 0,
            "wall_seconds": round(step3_wall, 3),
            "additional_spend_usd": round(spend_after - spend_before, 6),
            "settled_usd": step3_summary["settled_usd"],
            "reserved_usd": step3_summary["reserved_usd"],
        },
        "final_ledger_summary": step3_summary,
        "cells": [
            {
                "variant": r["variant"],
                "cell_key": r["cell_key"],
                "status": r["status"],
                "reward": r.get("reward"),
                "wall_s": r.get("wall_s"),
                "est_usd": r.get("est_usd"),
            }
            for r in step2_results
        ],
        "ledger_json": raw_ledger,
    }

    # Save receipts in both locations
    receipt_file_1 = RECEIPTS_DIR / "smoke-modal-cells-receipt.json"
    receipt_file_1.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved primary receipt -> {receipt_file_1}")

    copy_receipts_dir = RESULTS_ROOT / "receipts"
    copy_receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_file_2 = copy_receipts_dir / "smoke-modal-cells-receipt.json"
    receipt_file_2.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Saved secondary receipt -> {receipt_file_2}")

    print("\n" + "=" * 70)
    print("SMOKE TEST RESULTS SUMMARY")
    print("  Cells evaluated: 6")
    print(f"  Run 1 (killed midway): 2 cells run, settled ${step1_summary['settled_usd']:.5f}")
    print("  Run 1 (resumed): 2 cache hits ($0.00), 4 newly run cells")
    print("  Run 2: 6/6 cache hits ($0.00 spend across all cells)")
    print(f"  Final settled spend: ${step3_summary['settled_usd']:.5f} (Cap: ${CAP_USD:.2f})")
    print(f"  Effective spend: ${step3_summary['effective_spend_usd']:.5f}")
    print(f"  Remaining cap: ${step3_summary['remaining_usd']:.5f}")
    print("=" * 70)
    return receipt


if __name__ == "__main__":
    run_smoke()
