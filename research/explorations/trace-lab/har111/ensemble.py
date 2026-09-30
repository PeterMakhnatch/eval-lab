"""Median-of-N label over several checker output dirs (sound < suspect < broken); unanswered samples are skipped.

Usage: python3 ensemble.py OUT_DIR RUN_DIR [RUN_DIR ...]
Writes OUT_DIR/<task>.json with the median label, per-sample labels, and the union of not_inferable items.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = ["sound", "suspect", "broken"]


def combine(records: list[dict]) -> dict:
    labels = [r.get("label") for r in records]
    ranks = sorted(ORDER.index(lab) for lab in labels if lab in ORDER)
    label = ORDER[ranks[(len(ranks) - 1) // 2]] if ranks else None  # lower median on ties
    items = [i for r in records for i in r.get("unstated", [])]
    return {
        "task_id": records[0]["task_id"],
        "label": label,
        "sample_labels": labels,
        "unstated": items,
        "reasons": [r.get("reason") for r in records],
        "usage": [r.get("usage") for r in records],
    }


if __name__ == "__main__":
    out, runs = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(runs[0].glob("*.json")):
        recs = [json.loads((r / path.name).read_text()) for r in runs if (r / path.name).is_file()]
        (out / path.name).write_text(json.dumps(combine(recs), indent=2) + "\n")
