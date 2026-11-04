#!/usr/bin/env python3
"""Resume an interrupted `evallab certify-gold` output directory from its persisted `plan.json`.

Reuses any `cells/<name>/result.json` already on disk (via `modal_cell_runner`), optionally seeds
`evalbase-*` cells from an earlier run on the same base package, executes only the remaining cells on
Modal under the run's `CloudSpendGate`, writes `certification.json` and `summary.json`, and appends
spend records to `~/.local/state/evallab-ov1104/spend.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.gold_certifier import (  # noqa: E402
    CertificationCell,
    GoldMatrixSummary,
    _run_certification_cell,
    _write_certification_json,
    summarize_certification,
)
from evallab.modal_cells import SandboxMeter, modal_cell_runner  # noqa: E402
from evallab.registry import task_directory_digest  # noqa: E402
from evallab.regrade import CloudSpendGate  # noqa: E402

APPROVAL = "Peter 2026-11-03 23:35 ET: $30 overnight infra budget (ov1104), lane cap per PROGRAM.md"
SPEND_JSONL = Path.home() / ".local/state/evallab-ov1104/spend.jsonl"


def _log_spend(what: str, est_usd: float, receipt: str) -> None:
    SPEND_JSONL.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "lane": "grader",
        "what": what,
        "provider": "modal",
        "est_usd": round(float(est_usd or 0.0), 6),
        "actual_usd": None,
        "receipt": receipt,
    }
    with SPEND_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def main() -> int:
    os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="Interrupted certify-gold output directory")
    ap.add_argument("--max-usd", type=float, default=0.95)
    ap.add_argument("--reuse-evalbase-from", type=Path, help="Earlier run dir on same base package")
    args = ap.parse_args()

    out_dir = args.out.resolve()
    inputs = json.loads((out_dir / "inputs.json").read_text())
    task_id = inputs["task_id"]
    plan_raw = json.loads((out_dir / "plan.json").read_text())
    cells = [
        CertificationCell(
            name=c["name"],
            arm=c["arm"],
            package=Path(c["package"]),
            script=c["script"],
            marker=c["marker"],
            key=c.get("key", ""),
        )
        for c in plan_raw
    ]
    if args.reuse_evalbase_from:
        src_cells = args.reuse_evalbase_from.resolve() / "cells"
        for cell in cells:
            if cell.arm == "evalbase":
                dst = out_dir / "cells" / cell.name / "result.json"
                src = src_cells / cell.name / "result.json"
                if not dst.exists() and src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

    code = (out_dir / "test_gold_contract.py").read_text()
    upgraded = out_dir / "package"
    ledger = out_dir.with_name(f"{out_dir.name}.spend.json")
    gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=args.max_usd,
        usd_per_execution=0.005,
        spend_ledger=ledger,
        modal_app_name="ov1104-grader-certify",
        billing_tag="ov1104-grader-certify",
    )
    meter = SandboxMeter(args.max_usd)
    run_cell = modal_cell_runner(repo_root=REPO_ROOT, spend_gate=gate, meter=meter, timeout_seconds=240)

    outcomes: dict[str, dict] = {}
    try:
        for cell in cells:
            cell_dir = out_dir / "cells" / cell.name
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "agent-script.sh").write_text(cell.script)
            had_result = (cell_dir / "result.json").exists()
            res = _run_certification_cell(run_cell, cell_dir, cell.package, cell.script)
            outcomes[cell.name] = res
            if not had_result:
                _log_spend(
                    f"certify {out_dir.name}/{cell.name}",
                    res.get("est_usd", 0.0),
                    str(cell_dir / "result.json"),
                )
    finally:
        result = summarize_certification(
            task_id=task_id,
            upgraded=upgraded,
            test_lines=len(code.splitlines()),
            cells=cells,
            outcomes=outcomes,
        )
        result.package_digest = task_directory_digest(upgraded)
        _write_certification_json(out_dir / "certification.json", result.summary())
        _write_certification_json(out_dir / "summary.json", GoldMatrixSummary([result]).to_report_dict())

    output = {
        **result.summary(),
        "new_estimated_usd": round(meter.spent, 5),
        "new_executions": meter.cells,
        "receipt": str(out_dir / "certification.json"),
    }
    print(json.dumps(output, indent=2))
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
