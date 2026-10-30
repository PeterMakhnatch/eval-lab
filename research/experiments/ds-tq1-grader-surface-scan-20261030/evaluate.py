#!/usr/bin/env python3
"""Score the frozen held-out scan against SK-1 witnesses and recorded defect classes."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw/TQ-1")
SK1 = BR.parent / "SK-1/receipts/mechanism-table.json"
TASKS = BR.parent / "SK-2/receipts/tasks.jsonl"
sys.path.insert(0, str(BR.parents[1]))
from stats import wilson  # noqa: E402

CLASSES = ["framework_config_injection", "runner_exit_status_forgery",
           "test_file_sabotage", "hardcoded_expected_output",
           "synthetic_artifact_shape_fill", "hidden_answer_read", "grader_tamper"]
MAP = {
    "answer_reachable": ["hidden_answer_read"],
    "grader_tamperable": ["grader_tamper", "framework_config_injection", "runner_exit_status_forgery"],
    "reference_validated_weak_test": ["hardcoded_expected_output", "synthetic_artifact_shape_fill"],
    "weak_check": ["framework_config_injection", "runner_exit_status_forgery",
                   "test_file_sabotage", "hardcoded_expected_output", "synthetic_artifact_shape_fill"],
    "grader_wrong_pass": [],
}

SCANNER = Path(__file__).resolve().parent / "scan_grader_surface.py"


def run_all() -> list[dict]:
    rows = {r["task_id"]: r for r in map(json.loads, TASKS.read_text().splitlines())}
    split = {s["task_id"]: s for s in json.loads((BR / "receipts/split.json").read_text())}
    out = []
    for tid in sorted(rows, key=lambda t: split[t]["sha12"]):
        p = subprocess.run([sys.executable, str(SCANNER), rows[tid]["original_task"]],
                           capture_output=True, text=True, check=True)
        d = json.loads(p.stdout)
        d["task_id"] = tid
        d["half"] = split[tid]["half"]
        d["sha12"] = split[tid]["sha12"]
        out.append(d)
    return out


def stats(k: int, n: int) -> dict:
    lo, hi = wilson(k, n) if n else (0.0, 1.0)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
            "wilson95": [round(lo, 4), round(hi, 4)]}


def main() -> None:
    scans = run_all()
    (BR / "receipts/scan-all.json").write_text(json.dumps(scans, indent=1, sort_keys=True))
    byid = {d["task_id"]: d for d in scans}
    held = [d["task_id"] for d in scans if d["half"] == "heldout"]

    witness: dict[str, set[str]] = {}
    for r in json.loads(SK1.read_text())["rows"]:
        witness.setdefault(r["task_id"].split("/")[-1], set()).update(r["classes"])
    recorded = json.loads((BR / "receipts/recorded-defects.json").read_text())

    def flagged(tid: str, cls: str) -> bool:
        return bool(byid[tid]["class_flags"].get(cls))

    report = {"n_heldout": len(held), "n_design": len(scans) - len(held),
              "two_by_two": {}, "recorded_class": {}, "task_level": {},
              "false_alarm_candidates": [], "missed": []}

    for cls in CLASSES:
        pos = [t for t in held if cls in witness.get(t, set())]
        pred = [t for t in held if flagged(t, cls)]
        tp = [t for t in pred if cls in witness.get(t, set())]
        report["two_by_two"][cls] = {
            "witness_positives": len(pos),
            "flagged": len(pred),
            "true_positive": len(tp),
            "recall": stats(len(tp), len(pos)),
            "precision": stats(len(tp), len(pred)),
        }

    for dcls, mapped in MAP.items():
        pos = [t for t in held if dcls in recorded.get(t, [])]
        pred = [t for t in held if any(flagged(t, m) for m in mapped)]
        tp = [t for t in pred if dcls in recorded.get(t, [])]
        report["recorded_class"][dcls] = {
            "mapped_to": mapped,
            "recorded_positives": len(pos),
            "flagged": len(pred),
            "true_positive": len(tp),
            "recall": stats(len(tp), len(pos)),
            "precision": stats(len(tp), len(pred)),
        }

    rec_pos = [t for t in held if recorded.get(t)]
    any_flag = [t for t in held if any(byid[t]["class_flags"].get(c) for c in CLASSES)]
    tp = [t for t in any_flag if recorded.get(t)]
    report["task_level"] = {
        "recorded_defect_tasks": len(rec_pos),
        "flagged_tasks": len(any_flag),
        "true_positive": len(tp),
        "recall": stats(len(tp), len(rec_pos)),
        "precision": stats(len(tp), len(any_flag)),
        "no_recorded_defect_flagged": [t for t in any_flag if not recorded.get(t)],
        "recorded_defect_unflagged": [t for t in rec_pos if not any(byid[t]["class_flags"].get(c) for c in CLASSES)],
    }

    for t in held:
        if not recorded.get(t) and any(byid[t]["class_flags"].get(c) for c in CLASSES):
            report["false_alarm_candidates"].append(
                {"task_id": t, "classes": [c for c in CLASSES if flagged(t, c)]})
        if recorded.get(t) and not any(byid[t]["class_flags"].get(c) for c in CLASSES):
            report["missed"].append({"task_id": t, "recorded": recorded[t],
                                     "witness": sorted(witness.get(t, []))})

    report["scan_all_sha12"] = __import__("hashlib").sha256(
        (BR / "receipts/scan-all.json").read_bytes()).hexdigest()[:12]
    (BR / "receipts/heldout-eval.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps({k: report[k] for k in ("task_level", "false_alarm_candidates", "missed")},
                     indent=1))
    for cls, v in report["two_by_two"].items():
        print(cls, v["recall"], v["precision"])
    for cls, v in report["recorded_class"].items():
        print("rec", cls, v["recall"], v["precision"])


if __name__ == "__main__":
    main()
