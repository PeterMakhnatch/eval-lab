#!/usr/bin/env python3
"""ledger_reader.py: Parsing and analysis of kill-test execution ledgers for F11."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def read_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    rows = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def analyze_kt1_ledger(
    ledger_path: Path,
    expected_ids: list[str],
    t_kill: float,
) -> dict[str, Any]:
    rows = read_ledger(ledger_path)
    starts: list[dict[str, Any]] = [r for r in rows if r.get("kind") == "start"]
    exits: list[dict[str, Any]] = [r for r in rows if r.get("kind") == "exit"]
    effects: list[dict[str, Any]] = [r for r in rows if r.get("kind") == "effect"]
    unguarded: list[dict[str, Any]] = [r for r in rows if r.get("kind") == "unguarded-effect"]

    # Determine what was completed before t_kill
    completed_before_kill = set()
    for ex in exits:
        if ex["t"] <= t_kill and ex.get("rc", 0) == 0:
            completed_before_kill.add(ex["id"])

    # Determine what was in flight at t_kill
    in_flight_at_kill = set()
    for st in starts:
        if st["t"] <= t_kill:
            iid = st["id"]
            if iid not in completed_before_kill:
                # check if it exited before t_kill
                exited = any(ex["id"] == iid and ex["t"] <= t_kill for ex in exits)
                if not exited:
                    in_flight_at_kill.add(iid)

    # Re-executions of completed items after t_kill
    completed_re_executed = sum(
        1 for st in starts if st["t"] > t_kill and st["id"] in completed_before_kill
    )

    # In-flight items duplicated (started again after t_kill)
    in_flight_duplicated = sum(
        1 for st in starts if st["t"] > t_kill and st["id"] in in_flight_at_kill
    )

    # Calculate concurrency overlap seconds for processes of the same item
    # Build interval [start_t, end_t] for each (item_id, pid)
    proc_intervals: dict[str, list[tuple[int, float, float]]] = {}
    for st in starts:
        iid = st["id"]
        pid = st["pid"]
        t_start = st["t"]
        # Find corresponding exit
        matching_exits = [e for e in exits if e["id"] == iid and e["pid"] == pid and e["t"] >= t_start]
        t_end = matching_exits[0]["t"] if matching_exits else (t_start + st.get("seconds", 20.0))
        proc_intervals.setdefault(iid, []).append((pid, t_start, t_end))

    total_overlap_sec = 0.0
    overlaps_by_item: dict[str, float] = {}
    for iid, intervals in proc_intervals.items():
        item_overlap = 0.0
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                pid1, s1, e1 = intervals[i]
                pid2, s2, e2 = intervals[j]
                if pid1 != pid2:
                    overlap = max(0.0, min(e1, e2) - max(s1, s2))
                    item_overlap += overlap
        if item_overlap > 0:
            overlaps_by_item[iid] = round(item_overlap, 3)
            total_overlap_sec += item_overlap

    # Finished IDs
    finished_ids = set()
    for ex in exits:
        if ex.get("rc", 0) == 0:
            finished_ids.add(ex["id"])
    for ef in effects:
        finished_ids.add(ef["id"])

    lost_ids = [iid for iid in expected_ids if iid not in finished_ids]
    exec_counts = {iid: sum(1 for s in starts if s["id"] == iid) for iid in expected_ids}
    effect_counts = {iid: sum(1 for e in effects if e["id"] == iid) for iid in expected_ids}
    unguarded_counts = {iid: sum(1 for u in unguarded if u["id"] == iid) for iid in expected_ids}

    return {
        "total_starts": len(starts),
        "total_exits": len(exits),
        "total_effects": len(effects),
        "total_unguarded_effects": len(unguarded),
        "completed_before_kill": sorted(completed_before_kill),
        "in_flight_at_kill": sorted(in_flight_at_kill),
        "completed_re_executed": completed_re_executed,
        "in_flight_duplicated": in_flight_duplicated,
        "overlap_seconds": round(total_overlap_sec, 3),
        "overlaps_by_item": overlaps_by_item,
        "items_lost": lost_ids,
        "exec_counts": exec_counts,
        "effect_counts": effect_counts,
        "unguarded_counts": unguarded_counts,
        "success": len(lost_ids) == 0 and completed_re_executed == 0,
    }

def analyze_kt3_ledger(ledger_path: Path, t_reset: float) -> dict[str, Any]:
    rows = read_ledger(ledger_path)
    starts = [r for r in rows if r.get("kind") == "start"]
    refusals = [r for r in rows if r.get("kind") == "quota-refusal"]
    effects = [r for r in rows if r.get("kind") == "effect"]

    t_first_refusal = refusals[0]["t"] if refusals else None

    # Starts while parked: any start after first refusal but before t_reset
    if t_first_refusal is not None:
        starts_while_parked = [s for s in starts if s["t"] > (t_first_refusal + 0.1) and s["t"] < t_reset]
    else:
        starts_while_parked = [s for s in starts if s["t"] < t_reset]

    starts_after_t = [s for s in starts if s["t"] >= t_reset]

    first_start_delay = None
    if starts_after_t:
        first_start_delay = round(min(s["t"] for s in starts_after_t) - t_reset, 3)

    passed = len(starts_while_parked) == 0 and len(starts_after_t) >= 1 and len(effects) >= 1

    return {
        "total_starts": len(starts),
        "total_refusals": len(refusals),
        "total_effects": len(effects),
        "attempts_before_t": len(starts_while_parked),
        "attempts_after_t": len(starts_after_t),
        "first_start_delay_sec": first_start_delay,
        "retries_consumed": max(0, len(starts) - 2) if passed else len(starts) - 1,
        "passed": passed,
    }


def wilson_interval(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half) * 100, 1), round(min(1.0, centre + half) * 100, 1))
