"""Verify and recompute all headline numbers directly from receipt files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

EXP_DIR = Path("research/experiments/on-reader-ladder-20261102")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    cohort = [json.loads(line) for line in (EXP_DIR / "cohort.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Cohort cases: {len(cohort)}, sha256: {sha256_file(EXP_DIR / 'cohort.jsonl')[:12]}")

    results_path = EXP_DIR / "ladder_results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    print(f"ladder_results.json sha256: {sha256_file(results_path)[:12]}")

    for r in results["rungs"]:
        rung_id = r["rung_id"]
        rung_file = EXP_DIR / f"{rung_id}.jsonl"
        h = sha256_file(rung_file)[:12]
        records = [json.loads(line) for line in rung_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 300, f"Expected 300 records for {rung_id}, got {len(records)}"
        print(f"Rung {rung_id:<22} sha256: {h} | Cases: {len(records)} | ParseErr: {r['parse_fails']} | Cost/1k: ${r['cost_per_1k_usd']:.4f}")


if __name__ == "__main__":
    main()
