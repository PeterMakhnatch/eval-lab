"""ID-OQ-1: recall of the fixed audit under original vs enriched key text, with a confidence-floor sweep.

Inputs: bundles (findings with confidence/citation/eligibility, original Sonnet judge rows), and sol judge outputs for
condition O (original keys) and E (enriched MiMo key text) over ALL findings. A finding counts only if it is
confidence >= floor and every citation verified (the production eligibility rule at floor 0.5). Keys matched =
distinct key ids named by eligible findings. No model calls.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

FLOORS = (0.5, 0.4, 0.3, 0.2)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def load(directory: Path) -> dict[str, dict[str, dict]]:
    result = {}
    for path in sorted(directory.glob("D*.json")):
        rows = json.loads(path.read_text())["judgments"]
        result[path.stem] = {row["id"]: row for row in rows}
    return result


def evaluate(bundles: dict[str, dict], judged: dict[str, dict], floor: float, parity: str | None) -> dict:
    keys = matched_keys = eligible = matched_findings = unkeyed = false_alarm = tasks_with_key_hit = flagged = 0
    tasks = 0
    for case, bundle in bundles.items():
        if parity and bundle["task_parity"] != parity:
            continue
        tasks += 1
        keys += len(bundle["keys"])
        rows = judged.get(case, {})
        hit: set[str] = set()
        task_eligible = 0
        for finding in bundle["findings"]:
            if finding["confidence"] < floor or not finding["citation_verified"]:
                continue
            row = rows.get(finding["id"])
            eligible += 1
            task_eligible += 1
            if row is None:
                continue
            if row["matched_key_ids"]:
                matched_findings += 1
                hit.update(row["matched_key_ids"])
            elif row["verdict"] == "false_alarm":
                false_alarm += 1
            elif row["verdict"] == "plausible_unkeyed":
                unkeyed += 1
        matched_keys += len(hit)
        tasks_with_key_hit += bool(hit)
        flagged += task_eligible > 0
    return {
        "tasks": tasks,
        "keys": keys,
        "matched_keys": matched_keys,
        "key_recall": matched_keys / keys if keys else None,
        "key_recall_wilson95": wilson(matched_keys, keys),
        "tasks_with_key_hit": tasks_with_key_hit,
        "tasks_with_any_eligible_finding": flagged,
        "eligible_findings": eligible,
        "eligible_per_task": round(eligible / tasks, 2) if tasks else None,
        "matched_findings": matched_findings,
        "key_backed_precision": round(matched_findings / eligible, 3) if eligible else None,
        "plausible_unkeyed": unkeyed,
        "false_alarm": false_alarm,
    }


def calibration(bundles: dict[str, dict], judged_o: dict[str, dict]) -> dict:
    """Does sol with original keys reproduce the retained Sonnet verdicts on the gate-eligible findings?"""
    same = total = sonnet_match = sol_match = both = 0
    for case, bundle in bundles.items():
        for finding in bundle["findings"]:
            if not finding["eligible_new_gate"] or finding["judge"] is None:
                continue
            row = judged_o.get(case, {}).get(finding["id"])
            if row is None:
                continue
            total += 1
            a = set(finding["judge"]["matched_key_ids"])
            b = set(row["matched_key_ids"])
            same += a == b
            sonnet_match += bool(a)
            sol_match += bool(b)
            both += bool(a) and bool(b)
    return {
        "eligible_findings_compared": total,
        "identical_matched_key_sets": same,
        "sonnet_matches": sonnet_match,
        "sol_matches": sol_match,
        "both_match": both,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--judge-out", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundles = {p.stem: json.loads(p.read_text()) for p in sorted(args.bundles.glob("D*.json"))}
    judged = {cond: load(args.judge_out / cond) for cond in ("O", "E")}
    # Cases without enriched keys judge identically under E: reuse O so E covers all 44 tasks.
    for case in bundles:
        if case not in judged["E"] and case in judged["O"]:
            judged["E"][case] = judged["O"][case]
    result = {"calibration_sol_vs_retained_sonnet": calibration(bundles, judged["O"]), "recall": {}}
    for cond in ("O", "E"):
        for floor in FLOORS:
            for parity in (None, "even", "odd"):
                label = f"{cond}|floor={floor}|{parity or 'all'}"
                result["recall"][label] = evaluate(bundles, judged[cond], floor, parity)
    args.output.write_text(json.dumps(result, indent=2))
    for label, row in result["recall"].items():
        if label.endswith("|all"):
            print(label, row["matched_keys"], "/", row["keys"], row["key_recall_wilson95"], "elig", row["eligible_findings"])


if __name__ == "__main__":
    main()
