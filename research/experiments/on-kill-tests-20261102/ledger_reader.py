#!/usr/bin/env python3
"""ledger_reader.py: Universal parser and verifier for kill-test execution ledgers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_ledger(ledger_path: Path | str) -> list[dict[str, Any]]:
    path = Path(ledger_path)
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def summarize_ledger(
    ledger_path: Path | str, expected_items: list[str] | None = None
) -> dict[str, Any]:
    rows = read_ledger(ledger_path)
    exec_counts: dict[str, int] = {}
    effect_counts: dict[str, int] = {}
    refusal_counts: dict[str, int] = {}

    for r in rows:
        iid = str(r.get("id") or r.get("item_id") or "UNKNOWN")
        kind = str(r.get("kind") or r.get("event") or "UNKNOWN")
        if kind in ("exec", "start"):
            exec_counts[iid] = exec_counts.get(iid, 0) + 1
        elif kind == "effect":
            effect_counts[iid] = effect_counts.get(iid, 0) + 1
        elif kind in ("quota-refusal", "refusal"):
            refusal_counts[iid] = refusal_counts.get(iid, 0) + 1

    items_to_check = expected_items or sorted(
        set(exec_counts) | set(effect_counts) | set(refusal_counts)
    )
    for iid in items_to_check:
        exec_counts.setdefault(iid, 0)
        effect_counts.setdefault(iid, 0)
        refusal_counts.setdefault(iid, 0)

    exact_one_effect = bool(items_to_check) and all(
        effect_counts.get(iid, 0) == 1 for iid in items_to_check
    )

    return {
        "rows_count": len(rows),
        "items": items_to_check,
        "exec_counts": exec_counts,
        "effect_counts": effect_counts,
        "refusal_counts": refusal_counts,
        "total_executions": sum(exec_counts.values()),
        "total_effects": sum(effect_counts.values()),
        "total_refusals": sum(refusal_counts.values()),
        "exact_one_effect": exact_one_effect,
    }


def analyze_quota_parking(
    ledger_path: Path | str, target_item: str, t_reset: float
) -> dict[str, Any]:
    rows = read_ledger(ledger_path)
    attempts_before_t = 0
    first_exec_after_t: float | None = None

    for r in rows:
        iid = str(r.get("id") or r.get("item_id") or "")
        if iid != target_item:
            continue
        kind = str(r.get("kind") or r.get("event") or "")
        t = float(r.get("t", 0.0))
        if kind in ("exec", "start"):
            if t < t_reset:
                attempts_before_t += 1
            else:
                if first_exec_after_t is None or t < first_exec_after_t:
                    first_exec_after_t = t

    tolerance = (first_exec_after_t - t_reset) if first_exec_after_t is not None else None
    return {
        "target_item": target_item,
        "t_reset": t_reset,
        "attempts_before_t": attempts_before_t,
        "first_exec_after_t": first_exec_after_t,
        "tolerance_sec": round(tolerance, 4) if tolerance is not None else None,
        "clean_parking": attempts_before_t == 0 and first_exec_after_t is not None,
    }
