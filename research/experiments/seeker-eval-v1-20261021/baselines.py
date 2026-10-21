#!/usr/bin/env python3
"""Seeker evaluation baseline table (pE-SELF-7).

Assembles every measured seeker arm on TB4 DEV into one table so a new model has
something to be compared against. Sources are the committed replay summaries; this
script measures nothing itself, it reconciles them.

Usage:
    uv run python research/experiments/seeker-eval-v1-20261021/baselines.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

SFT_SUMMARIES = {
    "pilot-768": REPO / "research/experiments/seeker-sft-pilot-20261021/replay-summary.json",
    "loss-target-768": REPO
    / "research/experiments/seeker-sft-loss-target-20261021/replay-summary.json",
}
PROMPTED_SUMMARY = REPO / "research/experiments/tb4-reward-seeker-v0-20261021/replay-summary.json"
COHORT = REPO / "research/datasets/seeker-sft-v1/tb4_dev_eval_cohort.json"
CLAIM_QUALITY = HERE / "claim-quality.json"
TRUNCATION = HERE / "truncation-analysis.json"


def _sft_rows() -> list[dict[str, Any]]:
    claim_quality = json.loads(CLAIM_QUALITY.read_text(encoding="utf-8"))["by_arm"]
    truncation = json.loads(TRUNCATION.read_text(encoding="utf-8"))["by_arm"]
    rows: list[dict[str, Any]] = []
    for source, path in SFT_SUMMARIES.items():
        summary = json.loads(path.read_text(encoding="utf-8"))
        for arm, bucket in sorted(summary["by_arm"].items()):
            key = f"{source}:{arm}"
            quality = claim_quality.get(key, {})
            trunc = truncation.get(key, {})
            rows.append(
                {
                    "family": "sft",
                    "arm": key,
                    "tasks": bucket["runs"],
                    "strict_valid": bucket["strict_valid"],
                    "claims": bucket["claimed_exploits"],
                    "plausible_claims": quality.get("plausible_claims", 0),
                    "upstream_measured": bucket["upstream_measured"],
                    "upstream_reward1": bucket["upstream_reward_1"],
                    "confirmed_1_0": bucket["confirmed_1_0"],
                    "unparsed_turn_rate": trunc.get("unparsed_turn_rate", 0.0),
                }
            )
    return rows


def _prompted_rows() -> list[dict[str, Any]]:
    if not PROMPTED_SUMMARY.is_file():
        return []
    summary = json.loads(PROMPTED_SUMMARY.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for driver, bucket in sorted((summary.get("per_driver") or {}).items()):
        rows.append(
            {
                "family": "prompted",
                "arm": driver,
                "tasks": bucket.get("valid_submissions"),
                "strict_valid": bucket.get("valid_submissions"),
                "claims": None,
                "plausible_claims": None,
                "upstream_measured": bucket.get("upstream_measured"),
                "upstream_reward1": bucket.get("upstream_reward1"),
                "confirmed_1_0": bucket.get("confirmed_exploits"),
                "unparsed_turn_rate": None,
            }
        )
    return rows


def main() -> int:
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    report = {
        "schema": "evallab.seeker_eval.baselines/v1",
        "cohort": {
            "key_id": cohort["key_id"],
            "split": cohort["split"],
            "dev_tasks": cohort["counts"]["total_dev_tasks"],
            "witness_seeded_dev_tasks": cohort["counts"]["witness_seeded_dev_tasks"],
            "strict_unseen_dev_tasks": cohort["counts"]["strict_unseen_dev_tasks"],
        },
        "rows": _sft_rows() + _prompted_rows(),
        "sources": {
            "sft_summaries": {k: v.as_posix() for k, v in SFT_SUMMARIES.items()},
            "prompted_summary": PROMPTED_SUMMARY.as_posix(),
            "cohort": COHORT.as_posix(),
            "claim_quality": CLAIM_QUALITY.as_posix(),
            "truncation": TRUNCATION.as_posix(),
        },
    }
    out = HERE / "baselines.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["rows"], indent=2, sort_keys=True))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
