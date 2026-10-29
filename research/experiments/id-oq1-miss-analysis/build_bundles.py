"""ID-OQ-1: build per-task miss-analysis bundles from retained OQ-6 v4 Sonnet 5.5 "B" DEV cells.

Read-only over retained cells and DEV-only scorer keys (DEV keys are authorized for tuning; no TEST or
cross material is touched). Writes one bundle JSON per keyed DEV task plus a manifest. No model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

RESULTS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-27")
CALLS = RESULTS / "oq6-tq-gate-rerun-v4/calls/anthropic--claude-sonnet-5-5/B/dev"
KEYS = RESULTS / "oq6-tq-gate-rerun/scorer-only-keys"
CONFIDENCE_FLOOR_NEW = 0.5


def task_parity(task_id: str) -> str:
    return "even" if int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 2 == 0 else "odd"


def finding_rows(cell: dict, prediction: dict) -> list[dict]:
    answer = cell["audit"].get("answer") or {}
    findings = answer.get("findings") or []
    payloads = {p["rank"]: p for p in prediction.get("parsed_payloads") or []}
    paired = cell["paired"]
    eligible_new = set(paired["memberships"]["new_verified_0_5"])
    eligible_old = set(paired["memberships"]["old_0_8"])
    judgments = {j["id"]: j for j in cell["scorer"].get("judgments") or []}
    rows = []
    for index, finding in enumerate(findings, start=1):
        cid = f"C{index}"
        payload = payloads.get(index) or payloads.get(index - 1) or {}
        checks = payload.get("checks") or []
        judgment = judgments.get(cid)
        rows.append(
            {
                "id": cid,
                "confidence": finding["confidence"],
                "direction": finding.get("direction"),
                "mechanism": finding.get("mechanism"),
                "citations": finding.get("citations"),
                "citation_verified": bool(checks) and all(c.get("verified") for c in checks),
                "failed_citation_details": [c.get("detail") for c in checks if not c.get("verified")],
                "eligible_new_gate": cid in eligible_new,
                "eligible_old_gate": cid in eligible_old,
                "judge": None
                if judgment is None
                else {
                    "verdict": judgment["verdict"],
                    "matched_key_ids": judgment["matched_key_ids"],
                    "reason": judgment["reason"],
                },
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for cell_path in sorted(CALLS.glob("D*/cell.json")):
        cell = json.loads(cell_path.read_text())
        if cell["key_count"] == 0:
            continue
        case = cell["case_id"]
        prediction = json.loads((cell_path.parent / "prediction.json").read_text())
        matched = set(cell["paired"]["gates"]["new_verified_0_5"]["matched_key_ids"])
        keys = json.loads((KEYS / f"{case}.json").read_text())
        key_rows = [
            {
                "key_id": k["key_id"],
                "category": k.get("category"),
                "summary": k.get("summary"),
                "root_causes": k.get("root_causes"),
                "matched_by_gate": k["key_id"] in matched,
            }
            for k in keys
        ]
        bundle = {
            "case_id": case,
            "task_parity": task_parity(cell["task_id"]),
            "prompt_path": str(cell_path.parent / "audit/prompt.txt"),
            "response_path": str(cell_path.parent / "audit/response.txt"),
            "keys": key_rows,
            "findings": finding_rows(cell, prediction),
        }
        (args.output / f"{case}.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        manifest.append(
            {
                "case_id": case,
                "task_parity": bundle["task_parity"],
                "keys": len(key_rows),
                "matched": sum(k["matched_by_gate"] for k in key_rows),
                "findings": len(bundle["findings"]),
            }
        )
    (args.output / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(
        json.dumps(
            {
                "cases": len(manifest),
                "keys": sum(m["keys"] for m in manifest),
                "matched": sum(m["matched"] for m in manifest),
                "findings": sum(m["findings"] for m in manifest),
                "even": sum(m["task_parity"] == "even" for m in manifest),
            }
        )
    )


if __name__ == "__main__":
    main()
