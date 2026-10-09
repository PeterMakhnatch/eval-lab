"""Remove measured pristine-state rule credit without disabling the checks.

Only positively weighted rule atoms which award credit on the pristine
post-G2 state may be zero-weighted. They still execute and report failures;
they simply no longer increase reward for doing nothing. Decisions must carry
measured item scores, rather than guesses based on function names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "general-nop-zero-weight@1"
META_REL = "tests/verifier/verifier_meta.json"


def build_meta(meta: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    """Zero only observed passing rule atoms; refuse ambiguous/empty decisions."""
    result = json.loads(json.dumps(meta))
    by_id = {it["id"]: it for it in result["items"]}
    if len(by_id) != len(result["items"]):
        raise VariantInvalid("duplicate item ids")
    if not scores:
        raise VariantInvalid("no measured passing rules")
    for item_id, score in scores.items():
        item = by_id.get(item_id)
        if (item is None or item.get("method") != "rule" or item.get("gate")
                or float(item.get("weight", 1)) <= 0 or not 0 < score <= 1):
            raise VariantInvalid(f"{item_id}: not a positively weighted measured positive rule atom")
        item["weight"] = 0
    return result


def rule_floor(meta: dict[str, Any], scores: dict[str, float]) -> float:
    """Sidecar weighted reward with all text-judge scores fixed to zero."""
    numerator = denominator = 0.0
    for item in meta["items"]:
        if item.get("gate"):
            if scores.get(item["id"], 0) < 0.999:
                return 0.0
            continue
        method = item.get("method")
        if method == "agent" or (method == "llm" and item.get("judge") == "vision"):
            continue
        weight = float(item.get("weight", 1))
        denominator += weight
        if method == "rule":
            numerator += weight * scores.get(item["id"], 0)
    return round(numerator / denominator, 4) if denominator else 0.0


def derive_general_nop_gate(
    parent_dir: Path | str,
    *,
    scores: dict[str, float],
    evidence: str,
    repo_root: Path | str,
    parent_source: dict[str, Any],
    created_by: str = "judge-variants-night",
) -> VariantRecord:
    """Derive a task with an item-bound, measured zero-credit decision."""
    parent = Path(parent_dir)
    meta = json.loads((parent / META_REL).read_text(encoding="utf-8"))
    updated = build_meta(meta, scores)
    return derive_task(
        parent,
        changes={META_REL: (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode()},
        transform=TRANSFORM_ID,
        rationale="Zero reward weight for rules measured passing on pristine state; keep the checks and failure reports.",
        created_by=created_by,
        inputs={"measured_scores": scores, "evidence": evidence},
        repo_root=repo_root,
        parent_source=parent_source,
    )
