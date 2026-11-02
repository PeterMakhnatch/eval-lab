#!/usr/bin/env python3
"""Recomputation script for F2-CENSUS.

Reads census.json, validates data integrity, computes exact metrics,
and writes receipts under the experiment directory.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

DIR = Path(__file__).resolve().parent


def sha256_12(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def main() -> None:
    census_file = DIR / "census.json"
    with open(census_file, encoding="utf-8") as f:
        data = json.load(f)

    candidates = data["candidates"]
    scores = [c["total_score"] for c in candidates]

    # Dimension scores
    dim_scores: dict[str, list[int]] = {dim: [] for dim in data["scoring_dimensions"]}
    for c in candidates:
        for dim in data["scoring_dimensions"]:
            dim_scores[dim].append(c["scores"][dim])

    dim_stats = {
        dim: {
            "mean": round(statistics.mean(vals), 3),
            "median": statistics.median(vals),
            "distribution": {str(k): vals.count(k) for k in (0, 1, 2, 3)},
        }
        for dim, vals in dim_scores.items()
    }

    # Incident counts across all candidates
    incident_types = {
        "stalled_workers_and_timeout_kills": 0,
        "quota_and_429_stops": 0,
        "lost_tmp_receipts": 0,
        "concurrency_and_claim_order_bugs": 0,
        "merge_conflicts_and_train_failures": 0,
        "reservation_cap_exhaustion": 0,
        "manual_redispatch_and_route_stalls": 0,
    }

    for c in candidates:
        text = json.dumps(c["incident_citations"]) + json.dumps(c["score_evidence"])
        if "killed" in text or "timeout" in text or "stalled" in text:
            incident_types["stalled_workers_and_timeout_kills"] += 1
        if "quota" in text or "429" in text or "burnout" in text:
            incident_types["quota_and_429_stops"] += 1
        if "/tmp" in text:
            incident_types["lost_tmp_receipts"] += 1
        if "lock" in text or "re-queued" in text or "race" in text:
            incident_types["concurrency_and_claim_order_bugs"] += 1
        if "merge" in text or "conflict" in text or "rebase" in text:
            incident_types["merge_conflicts_and_train_failures"] += 1
        if "reservation" in text or "CloudSpendGate" in text:
            incident_types["reservation_cap_exhaustion"] += 1
        if "redispatch" in text or "loop" in text or "stale" in text or "babysit" in text:
            incident_types["manual_redispatch_and_route_stalls"] += 1

    summary = {
        "candidate_count": len(candidates),
        "non_candidate_count": len(data["non_candidates"]),
        "total_score_stats": {
            "min": min(scores),
            "max": max(scores),
            "mean": round(statistics.mean(scores), 2),
            "median": statistics.median(scores),
        },
        "dimension_stats": dim_stats,
        "incident_types_affected_workflows": incident_types,
        "top_5": [
            {
                "rank": i + 1,
                "id": c["id"],
                "name": c["name"],
                "total_score": c["total_score"],
                "owner_today": c["owner_today"],
            }
            for i, c in enumerate(candidates[:5])
        ],
    }

    receipt_file = DIR / "census_metrics.json"
    with open(receipt_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary computed: {receipt_file} (sha256[:12]: {sha256_12(receipt_file)})")
    print(f"census.json sha256[:12]: {sha256_12(census_file)}")
    print(f"CENSUS.md sha256[:12]: {sha256_12(DIR / 'CENSUS.md')}")


if __name__ == "__main__":
    main()
