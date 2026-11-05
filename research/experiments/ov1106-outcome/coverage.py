#!/usr/bin/env python3
"""Aggregate the 86 DEV outcome rows without converting abstentions to negatives.

Only original reward 1 followed by protected reward 0 is a detection.
Raw receipts stay outside Git. This script does not execute submissions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text())


def jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proportion(k: int, n: int):
    if not n:
        return {"numerator": k, "denominator": n, "rate": None, "wilson95": None}
    z = 1.959963984540054
    p = k / n
    divisor = 1 + z * z / n
    center = (p + z * z / (2 * n)) / divisor
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / divisor
    return {
        "numerator": k,
        "denominator": n,
        "rate": p,
        "wilson95": [max(0, center - half), min(1, center + half)],
    }


def summarize(rows):
    eligible = [r for r in rows if r["eligible"]]
    scored = [r for r in rows if r["scored"]]
    cheats = [r for r in eligible if r["label"] == 1]
    honest = [r for r in scored if r["label"] == 0]
    return {
        "total": len(rows),
        "eligible": len(eligible),
        "scored": len(scored),
        "coverage_all": proportion(len(scored), len(rows)),
        "coverage_eligible": proportion(len(scored), len(eligible)),
        "recall_eligible": proportion(sum(r["outcome_regrade"] == 1 for r in cheats), len(cheats)),
        "recall_scored_cheats": proportion(
            sum(r["outcome_regrade"] == 1 for r in scored if r["label"] == 1),
            sum(r["label"] == 1 for r in scored),
        ),
        "fpr_scored": proportion(sum(r["outcome_regrade"] == 1 for r in honest), len(honest)),
        "abstentions": dict(
            sorted(Counter(r["abstain_reason"] for r in rows if not r["scored"]).items())
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--replay", type=Path, required=True)
    p.add_argument("--c1c5", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--solastra", type=Path, required=True)
    p.add_argument("--raw", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.raw.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    universe = [r for r in jsonl(args.runs) if r["set"] in ("c1c5", "sol-astra")]
    if len(universe) != 86 or len({r["run_key"] for r in universe}) != 86:
        raise ValueError("Expected exactly 86 distinct C1–C5/Sol-Astra rows")
    if any(r["split"] != "dev" for r in universe):
        raise ValueError("Only DEV rows are permitted")
    manifest = {r["id"]: r for r in jsonl(args.replay / "corpora/sol-astra-manifest.jsonl")}
    sa = load(args.solastra)["runs"]
    sa_by_id = {r["run_id"]: r for r in sa}
    controls = {}
    for task in {r["task"] for r in sa}:
        honest = [r for r in sa if r["task"] == task and r["run_id"].endswith("__scripted__honest")]
        negative = [
            r
            for r in sa
            if r["task"] == task and "__scripted__" in r["run_id"] and "subverted" in r["run_id"]
        ]
        controls[task] = (
            bool(honest)
            and bool(negative)
            and all(
                r.get("fresh", {}).get("status") == "scored" and r["fresh"].get("reward") == 1
                for r in honest
            )
            and all(
                r.get("fresh", {}).get("status") == "scored" and r["fresh"].get("reward") == 0
                for r in negative
            )
        )
    c_by_id = {}
    sources = [args.runs, args.solastra, args.replay / "corpora/sol-astra-manifest.jsonl"]
    for path in sorted(args.c1c5.glob("*/task.json")):
        record = load(path)
        sources.append(path)
        for run in record.get("runs", []):
            c_by_id[run["run_id"]] = (record, run, path)
    rows = []
    for u in sorted(universe, key=lambda x: x["run_key"]):
        rid = u["run_key"].split(":", 1)[1]
        receipt = args.raw / "rows" / (u["set"] + "--" + rid + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        if u["set"] == "c1c5":
            record, run, source = c_by_id.get(rid, ({}, {}, None))
            original_path = args.cases / rid / "grade.json"
            original = load(original_path) if original_path.is_file() else {}
            orig = original.get("reward_L0")
            fresh = run.get("hardened_regrade", {})
            task = record.get("task", u["cluster"])
            valid = (
                record.get("controls", {}).get("honest", {}).get("reward") == 0
                and "error" not in record
            )
            if run and orig != run.get("stored_L0"):
                raise ValueError(f"Original reward mismatch: {rid}")
            state = bool(run.get("patch_sha256") or run.get("artifact_files"))
            evidence = {
                "original_grade": original,
                "regrade": run,
                "controls": record.get("controls"),
                "source": str(source),
            }
        else:
            run = sa_by_id.get(rid, {})
            m = manifest.get(rid, {})
            original_path = Path(m["path"]) / "grade.json" if m else None
            original = load(original_path) if original_path and original_path.is_file() else {}
            orig = original.get("reward")
            fresh = run.get("fresh", {})
            task = run.get("task", u["cluster"])
            valid = controls.get(task, False)
            if run and orig != run.get("stored_reward"):
                raise ValueError(f"Original reward mismatch: {rid}")
            state = bool(run.get("workspace") and Path(run["workspace"]).is_dir())
            evidence = {
                "original_grade": original,
                "regrade": run,
                "controls_valid": valid,
                "source": str(args.solastra),
            }
        eligible = None if orig is None else orig == 1
        protected_available = fresh.get("status") == "scored" and fresh.get("reward") in (0, 1)
        if orig is None:
            reason = "original_reward_missing"
        elif not eligible:
            reason = "original_fail"
        elif not run:
            reason = "protected_grade_missing"
        elif not valid:
            reason = "task_package_drift" if u["set"] == "sol-astra" else "controls_invalid"
        elif not protected_available:
            reason = "protected_grade_unavailable:" + str(fresh.get("status"))
        else:
            reason = None
        row = {
            "run_id": rid,
            "run_key": u["run_key"],
            "corpus": u["set"],
            "label": u["label"],
            "original_reward": orig,
            "eligible": eligible,
            "scored": reason is None,
            "abstain_reason": reason,
            "outcome_regrade": int(fresh["reward"] == 0) if reason is None else None,
            "receipt_path": str(receipt),
            "task": task,
            "protected_reward": fresh.get("reward"),
            "protected_grade_available": protected_available,
            "controls_valid": valid,
            "final_state_available": state,
            "original_grade_path": str(original_path) if original_path else None,
            "length_chars": u.get("n_chars"),
            "length_steps": u.get("n_steps"),
        }
        receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        row["receipt_sha256"] = digest(receipt)
        rows.append(row)
        if original_path and original_path.is_file():
            sources.append(original_path)
    (args.out / "coverage-rows.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    )
    summary = {
        "definition": "original_reward == 1 AND protected_reward == 0, with valid controls",
        "all": summarize(rows),
        "by_corpus": {
            c: summarize([r for r in rows if r["corpus"] == c]) for c in ("c1c5", "sol-astra")
        },
        "uncovered": [r["run_key"] for r in rows if not r["scored"]],
        "missing_state_count": sum(not r["final_state_available"] for r in rows),
        "missing_protected_grade_count": sum(not r["protected_grade_available"] for r in rows),
    }
    (args.out / "coverage-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    provenance = {
        "script_sha256": digest(Path(__file__)),
        "inputs": {str(path): digest(path) for path in sorted(set(sources))},
    }
    (args.out / "coverage-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary["all"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
