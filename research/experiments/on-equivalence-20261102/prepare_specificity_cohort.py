#!/usr/bin/env python3
"""Prepares the unified blind cohort of 118 items:
- 74 original items from A2B (22 survivors + 51 killed positive controls + 1 unexecuted)
- 44 known-equivalent controls (39 easy + 5 hard)

Deterministic shuffle with seed 20261102, assigning BLIND-001 .. BLIND-118.
Outputs blind_items_v2.json and opaque_mapping_v2.json.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

REPO_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-a2b-equiv-20261102")
EXP_DIR = REPO_ROOT / "research/experiments/on-equivalence-20261102"
CERT_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/FIX-A2B-SPECIFICITY/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/FIX-A2B-SPECIFICITY/receipts"

A2B_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A2B-EQUIV/receipts")


def main() -> None:
    CERT_RECEIPTS.mkdir(parents=True, exist_ok=True)
    DURABLE_RECEIPTS.mkdir(parents=True, exist_ok=True)

    with open(A2B_RECEIPTS / "opaque_mapping.json") as f:
        orig_mapping = {m["opaque_id"]: m for m in json.load(f)}

    with open(A2B_RECEIPTS / "blind_items.json") as f:
        orig_blind = json.load(f)

    with open(EXP_DIR / "known_equivalent_controls.json") as f:
        controls = json.load(f)

    raw_items: list[dict[str, Any]] = []

    # 1. Add 74 original items
    for b in orig_blind:
        m = orig_mapping[b["opaque_id"]]
        raw_items.append({
            "item_type": "original_item",
            "original_id": b["opaque_id"],
            "task_id": m["task_id"],
            "cell": m.get("cell"),
            "operator": m.get("operator"),
            "line": m.get("line"),
            "path": m.get("path"),
            "status": m.get("status"),
            "category": "mutant",
            "instruction": b["instruction"],
            "context_file": b["context_file"],
            "context_code": b["context_code"],
            "diff_text": b["diff_text"],
        })

    # 2. Add 44 controls
    for c in controls:
        raw_items.append({
            "item_type": "known_equivalent_control",
            "original_id": c["control_id"],
            "source_survivor": c["source_survivor"],
            "task_id": c["task_id"],
            "cell": None,
            "operator": c["operator"],
            "line": c["line"],
            "path": c["path"],
            "status": "known_equivalent",
            "category": c["category"],
            "description": c["description"],
            "instruction": c["instruction"],
            "context_file": c["path"],
            "context_code": c["context_code"],
            "diff_text": c["diff_text"],
        })

    assert len(raw_items) == 118, f"Expected 118 items, got {len(raw_items)}"
    survivors = sum(1 for x in raw_items if x["status"] == "survivor")
    killed = sum(1 for x in raw_items if x["status"] == "killed")
    unexec = sum(1 for x in raw_items if x["status"] == "unexecuted")
    equiv = sum(1 for x in raw_items if x["status"] == "known_equivalent")
    easy = sum(1 for x in raw_items if x["category"] == "easy")
    hard = sum(1 for x in raw_items if x["category"] == "hard")

    print("Cohort composition:")
    print(f"  Total: {len(raw_items)}")
    print(f"  Survivors: {survivors}")
    print(f"  Killed controls (positive): {killed}")
    print(f"  Unexecuted: {unexec}")
    print(f"  Known-equivalent controls (negative): {equiv} (Easy: {easy}, Hard: {hard})")

    # Deterministic shuffle
    rng = random.Random(20261102)
    # Sort first by stable key
    raw_items.sort(key=lambda x: f"{x['item_type']}::{x['original_id']}")
    rng.shuffle(raw_items)

    mapping: list[dict[str, Any]] = []
    blind_items: list[dict[str, Any]] = []

    for idx, item in enumerate(raw_items, start=1):
        opaque_id = f"BLIND-{idx:03d}"

        map_row = dict(item)
        del map_row["instruction"]
        del map_row["context_code"]
        del map_row["diff_text"]
        map_row["opaque_id"] = opaque_id
        mapping.append(map_row)

        blind_items.append({
            "opaque_id": opaque_id,
            "instruction": item["instruction"],
            "context_file": item["context_file"],
            "context_code": item["context_code"],
            "diff_text": item["diff_text"],
        })

    for target_dir in [CERT_RECEIPTS, DURABLE_RECEIPTS, EXP_DIR]:
        (target_dir / "opaque_mapping_v2.json").write_text(json.dumps(mapping, indent=2))
        (target_dir / "blind_items_v2.json").write_text(json.dumps(blind_items, indent=2))

    print(f"Saved opaque_mapping_v2.json and blind_items_v2.json to {EXP_DIR}, {CERT_RECEIPTS}, {DURABLE_RECEIPTS}.")


if __name__ == "__main__":
    main()
