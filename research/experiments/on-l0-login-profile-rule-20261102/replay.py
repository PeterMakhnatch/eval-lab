#!/usr/bin/env python3
"""Replay evaluation of L0 login-profile short-circuit rule on known evasions and honest negatives.

Evaluates:
1. Known evasions cohort (n=4): K7C 000931, K7E 000256, 001205, 001431.
2. Honest benchmark negatives (n=399): TraceMonitor-Bench DEV v1 scored negatives.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from evallab.hack_drop import RolloutItem, evaluate_l0_stage
from evallab.trace_anatomy import LoadedRun, load_atif_file
from evallab.tripwires import scan_trace, to_findings

STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/stats.py")
sys.path.insert(0, str(STATS_PATH.parent))
import stats  # noqa: E402

BENCH_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
TW_DIR = Path(os.path.expanduser("~/.local/state/evallab-prod-live/factory/tw-dev-export"))
RAW_RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/L0-LOGIN-PROFILE-RULE/receipts")
DURABLE_RESULTS_DIR = Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/L0-LOGIN-PROFILE-RULE"))

KNOWN_EVASIONS = [
    {
        "pkg": "format-code-task-000931",
        "attempt": 1,
        "source": "K7",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7/attempts/000931/solve.sh")),
    },
    {
        "pkg": "format-code-task-000256",
        "attempt": 1,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/000256/attempt-1/solve.sh")),
    },
    {
        "pkg": "format-code-task-001205",
        "attempt": 2,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/001205/attempt-2/solve.sh")),
    },
    {
        "pkg": "format-code-task-001431",
        "attempt": 1,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/001431/attempt-1/solve.sh")),
    },
]


def l0_scan_script(script: str, pkg: str) -> dict[str, Any]:
    step = {
        "step_id": 1,
        "source": "agent",
        "tool_calls": [{"name": "bash", "arguments": {"command": script}}],
    }
    report = scan_trace([step])
    findings = to_findings(report, task_id=pkg)
    fired_rules = sorted({str(f.get("rule")) for f in findings})
    rollout = RolloutItem(
        item_id=pkg,
        task_id=pkg,
        run=LoadedRun(pkg, "synthetic", {"steps": [step]}),
        files=(("solve.sh", script),),
    )
    verdict = evaluate_l0_stage(rollout)
    return {
        "findings": fired_rules,
        "hackbook_hits": [h["rule"] for h in report.get("hackbook", [])],
        "decision": verdict.decision,
        "reasons": verdict.reasons,
        "details": verdict.details,
    }


def evaluate_evasions() -> dict[str, Any]:
    results = []
    before_bools = []
    after_bools = []
    for ev in KNOWN_EVASIONS:
        script = ev["path"].read_text()
        res = l0_scan_script(script, ev["pkg"])
        before_fired = False  # By measurement in K7B/K7C/K7E, none of these 4 fired before
        after_fired = "hb_login_profile_tamper" in res["findings"]
        before_bools.append(before_fired)
        after_bools.append(after_fired)
        results.append({
            "pkg": ev["pkg"],
            "attempt": ev["attempt"],
            "source": ev["source"],
            "before_fired": before_fired,
            "after_fired": after_fired,
            "decision": res["decision"],
            "reasons": res["reasons"],
            "findings": res["findings"],
        })
    paired_test = stats.paired(before_bools, after_bools)
    k_before = sum(before_bools)
    k_after = sum(after_bools)
    n = len(results)
    return {
        "n": n,
        "before_k": k_before,
        "before_wilson": stats.wilson(k_before, n),
        "after_k": k_after,
        "after_wilson": stats.wilson(k_after, n),
        "paired": paired_test,
        "cases": results,
    }


def clean_own_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "step_id": s.get("step_id", idx),
            "source": "agent",
            "message": s.get("message", ""),
            "tool_calls": s.get("tool_calls", []),
            "observation": s.get("observation", {"results": []}),
        }
        for idx, s in enumerate(steps)
    ]


def load_case_run(row: dict[str, Any]) -> LoadedRun | None:
    family = row["family"]
    source_id = row["source_id"]
    if family == "terminal_wrench":
        tw_file = TW_DIR / f"{source_id}.json"
        if tw_file.exists():
            return load_atif_file(tw_file)
    own_file = BENCH_ROOT / "own-trajectories" / f"{source_id}.json"
    if own_file.exists():
        traj = json.loads(own_file.read_text())
        steps = clean_own_steps(traj.get("steps") or [])
        doc = {"metadata": {"split": "dev"}, "steps": steps}
        return LoadedRun(source_id, "atif", doc, None, None, len(json.dumps(steps)))
    # Fallback to card JSON
    card_json_path = BENCH_ROOT / row["card_path"].replace(".md", ".json")
    if card_json_path.exists():
        card = json.loads(card_json_path.read_text())
        steps = []
        for mut in card.get("patch_and_mutations", {}).get("mutations", []):
            st = mut.get("step", 1)
            steps.append({"step_id": st, "source": "agent", "tool_calls": [{"name": mut.get("tool", "write"), "arguments": {"path": mut.get("path"), "command": mut.get("command")}}]})
        for read in card.get("high_signal_actions", {}).get("sensitive_reads", []):
            st = read.get("step", 1)
            steps.append({"step_id": st, "source": "agent", "tool_calls": [{"name": "bash", "arguments": {"command": read.get("command")}}]})
        return LoadedRun(source_id, "card_replay", {"steps": steps})
    return None


def evaluate_honest_negatives() -> dict[str, Any]:
    manifest = [json.loads(line) for line in (BENCH_ROOT / "manifest.jsonl").read_text().splitlines()]
    negatives = [r for r in manifest if r["label"] is False and r["split"] in ("tune", "report")]

    # Stored readings
    l0_stored_path = BENCH_ROOT / "readings" / "l0_tripwires.jsonl"
    stored_readings = {}
    if l0_stored_path.exists():
        for line in l0_stored_path.read_text().splitlines():
            row = json.loads(line)
            stored_readings[row["case_id"]] = row

    results = []
    new_hard_drops = []
    new_soft_reviews = []
    by_family: dict[str, dict[str, int]] = {}

    before_alarms = []
    after_alarms = []

    for r in negatives:
        cid = r["case_id"]
        fam = r["family"]
        f_stat = by_family.setdefault(fam, {"n": 0, "before_alert": 0, "after_alert": 0, "new_alert": 0, "hard_drop": 0, "soft_review": 0})
        f_stat["n"] += 1

        stored = stored_readings.get(cid, {})
        stored_alert = bool(stored.get("suspicion", 0) > 0 or stored.get("has_gate_card"))
        if stored_alert:
            f_stat["before_alert"] += 1
        before_alarms.append(stored_alert)

        # Replay case
        run = load_case_run(r)
        if run is None:
            after_alarms.append(stored_alert)
            continue

        steps = [s for s in run.doc.get("steps") or [] if isinstance(s, dict)]
        rep = scan_trace(steps)
        findings = to_findings(rep, task_id=cid)
        fired_rules = sorted({str(f.get("rule")) for f in findings})

        rollout = RolloutItem(item_id=cid, task_id=r.get("source_id", cid), run=run)
        verdict = evaluate_l0_stage(rollout)

        login_profile_fired = any("login_profile" in f.lower() for f in fired_rules) or any("login_profile" in r.lower() for r in verdict.reasons)
        is_hard = verdict.decision == "drop"
        is_review = verdict.decision == "review"

        after_alert = stored_alert or bool(fired_rules) or is_hard
        after_alarms.append(after_alert)
        if after_alert:
            f_stat["after_alert"] += 1

        if login_profile_fired:
            if is_hard:
                f_stat["hard_drop"] += 1
                new_hard_drops.append({"case_id": cid, "family": fam, "reasons": verdict.reasons, "details": verdict.details})
            elif is_review:
                f_stat["soft_review"] += 1
                new_soft_reviews.append({"case_id": cid, "family": fam, "reasons": verdict.reasons, "details": verdict.details})

        results.append({
            "case_id": cid,
            "family": fam,
            "stored_alert": stored_alert,
            "after_alert": after_alert,
            "login_profile_fired": login_profile_fired,
            "decision": verdict.decision,
            "reasons": verdict.reasons,
        })

    n = len(negatives)
    k_before = sum(before_alarms)
    k_after = sum(after_alarms)
    paired = stats.paired(before_alarms, after_alarms)

    return {
        "n": n,
        "before_alerts": k_before,
        "before_rate": stats.wilson(k_before, n),
        "after_alerts": k_after,
        "after_rate": stats.wilson(k_after, n),
        "new_hard_drops_count": len(new_hard_drops),
        "new_hard_drops": new_hard_drops,
        "new_soft_reviews_count": len(new_soft_reviews),
        "new_soft_reviews": new_soft_reviews,
        "by_family": by_family,
        "paired": paired,
    }


def main() -> int:
    RAW_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    DURABLE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Replaying known evasions cohort (n=4)...", flush=True)
    evasion_results = evaluate_evasions()
    print(f"Evasions: before {evasion_results['before_k']}/{evasion_results['n']}, after {evasion_results['after_k']}/{evasion_results['n']}", flush=True)

    print("Replaying honest negatives cohort (n=399)...", flush=True)
    neg_results = evaluate_honest_negatives()
    print(f"Honest Negatives: before {neg_results['before_alerts']}/{neg_results['n']}, after {neg_results['after_alerts']}/{neg_results['n']}", flush=True)
    print(f"New hard drops on honest runs: {neg_results['new_hard_drops_count']}", flush=True)
    print(f"New soft reviews on honest runs: {neg_results['new_soft_reviews_count']}", flush=True)

    summary = {
        "experiment": "L0-LOGIN-PROFILE-RULE",
        "evasions": evasion_results,
        "honest_negatives": neg_results,
    }

    summary_file = RAW_RECEIPTS_DIR / "summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))
    durable_file = DURABLE_RESULTS_DIR / "summary.json"
    durable_file.write_text(json.dumps(summary, indent=2))

    print(f"Wrote summary receipts to {summary_file} and {durable_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
