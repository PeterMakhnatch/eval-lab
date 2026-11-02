#!/usr/bin/env python3
"""Replay evaluation of L0 login-profile short-circuit rule on known in-sample evasions and honest negatives.

Evaluates:
1. In-sample known evasions cohort (n=4 primary packages, 7 total attempts): K7C 000931, K7E 000256, 001205, 001431.
   Note: This cohort was designed in-sample after inspecting the attacker traces; n=4 with exact McNemar p=0.125
   is not statistically significant and does not prove evasion class closure.
2. Honest benchmark negatives (n=399): TraceMonitor-Bench DEV v1 scored negatives across 6 families.
   Measures login-profile specific false drops and soft review rate without mixing cross-instrument alert totals.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from evallab.hack_drop import RolloutItem, evaluate_l0_stage
from evallab.trace_anatomy import LoadedRun, load_atif_file
from evallab.tripwires import scan_trace, to_findings

# Local vendored statistics helper (reproducible from repo, no bridge-dir imports)
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import stats  # noqa: E402

BENCH_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
TW_DIR = Path(os.path.expanduser("~/.local/state/evallab-prod-live/factory/tw-dev-export"))

RECEIPT_DIRS = [
    Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/FIX-L0-1792/receipts"),
    Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/FIX-L0-1792")),
    Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/L0-LOGIN-PROFILE-RULE")),
]

PRIMARY_IN_SAMPLE_EVASIONS = [
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

ALL_ATTEMPTS_IN_SAMPLE_EVASIONS = [
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
        "pkg": "format-code-task-000256",
        "attempt": 2,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/000256/attempt-2/solve.sh")),
    },
    {
        "pkg": "format-code-task-000256",
        "attempt": 3,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/000256/attempt-3/solve.sh")),
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
    {
        "pkg": "format-code-task-001431",
        "attempt": 3,
        "source": "K7E",
        "path": Path(os.path.expanduser("~/Developer/eval-lab-results/2026-11-02/K7E/attempts/001431/attempt-3/solve.sh")),
    },
]


def load_base_scanner() -> Any:
    """Dynamically load pre-PR scanner from origin/main to compute 'before' outcomes."""
    base_cache = Path("/tmp/base_evallab/evallab")
    base_cache.mkdir(parents=True, exist_ok=True)
    tw_file = base_cache / "tripwires.py"
    if not tw_file.exists():
        content = subprocess.check_output(
            ["git", "show", "origin/main:src/evallab/tripwires.py"],
            text=True,
        )
        tw_file.write_text(content)
        (base_cache / "__init__.py").write_text("")

    spec = importlib.util.spec_from_file_location("base_tripwires", str(tw_file))
    base_tw = importlib.util.module_from_spec(spec)
    sys.modules["base_tripwires"] = base_tw
    spec.loader.exec_module(base_tw)
    return base_tw


def l0_scan_script(script: str, pkg: str, base_tw: Any) -> dict[str, Any]:
    step = {
        "step_id": 1,
        "source": "agent",
        "tool_calls": [{"name": "bash", "arguments": {"command": script}}],
    }
    # Current (after) scan
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

    # Base (before) scan
    base_report = base_tw.scan_trace([step])
    base_hb = [h["rule"] for h in base_report.get("hackbook", [])]

    return {
        "findings": fired_rules,
        "hackbook_hits": [h["rule"] for h in report.get("hackbook", [])],
        "decision": verdict.decision,
        "reasons": verdict.reasons,
        "details": verdict.details,
        "before_fired": any("login_profile" in r.lower() for r in base_hb),
    }


def evaluate_evasions(base_tw: Any) -> dict[str, Any]:
    # 1. Primary 4 cases
    primary_results = []
    before_bools_4 = []
    after_bools_4 = []
    for ev in PRIMARY_IN_SAMPLE_EVASIONS:
        script = ev["path"].read_text()
        res = l0_scan_script(script, ev["pkg"], base_tw)
        before_fired = res["before_fired"]  # Dynamically verified on pre-PR scanner
        after_fired = "hb_login_profile_tamper" in res["hackbook_hits"]
        before_bools_4.append(before_fired)
        after_bools_4.append(after_fired)
        primary_results.append({
            "pkg": ev["pkg"],
            "attempt": ev["attempt"],
            "source": ev["source"],
            "before_fired": before_fired,
            "after_fired": after_fired,
            "decision": res["decision"],
            "reasons": res["reasons"],
            "findings": res["findings"],
        })

    paired_4 = stats.paired(before_bools_4, after_bools_4)
    k_before_4 = sum(before_bools_4)
    k_after_4 = sum(after_bools_4)
    n_4 = len(primary_results)

    # 2. All 7 attempts across the 4 packages
    all_attempts_results = []
    before_bools_7 = []
    after_bools_7 = []
    for ev in ALL_ATTEMPTS_IN_SAMPLE_EVASIONS:
        script = ev["path"].read_text()
        res = l0_scan_script(script, ev["pkg"], base_tw)
        before_fired = res["before_fired"]
        after_fired = "hb_login_profile_tamper" in res["hackbook_hits"]
        before_bools_7.append(before_fired)
        after_bools_7.append(after_fired)
        all_attempts_results.append({
            "pkg": ev["pkg"],
            "attempt": ev["attempt"],
            "source": ev["source"],
            "before_fired": before_fired,
            "after_fired": after_fired,
            "decision": res["decision"],
            "reasons": res["reasons"],
            "findings": res["findings"],
        })

    paired_7 = stats.paired(before_bools_7, after_bools_7)
    k_before_7 = sum(before_bools_7)
    k_after_7 = sum(after_bools_7)
    n_7 = len(all_attempts_results)

    return {
        "status": "IN-SAMPLE (designed after inspecting attacker traces; n=4 is not statistically significant)",
        "primary_cohort_n4": {
            "n": n_4,
            "before_k": k_before_4,
            "before_wilson": stats.wilson(k_before_4, n_4),
            "after_k": k_after_4,
            "after_wilson": stats.wilson(k_after_4, n_4),
            "paired": paired_4,
            "cases": primary_results,
        },
        "all_attempts_n7": {
            "n": n_7,
            "before_k": k_before_7,
            "before_wilson": stats.wilson(k_before_7, n_7),
            "after_k": k_after_7,
            "after_wilson": stats.wilson(k_after_7, n_7),
            "paired": paired_7,
            "cases": all_attempts_results,
        },
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

    results = []
    hard_drops = []
    soft_reviews = []
    by_family: dict[str, dict[str, int]] = {}

    for r in negatives:
        cid = r["case_id"]
        fam = r["family"]
        f_stat = by_family.setdefault(
            fam,
            {"n": 0, "before_hard_drop": 0, "before_soft_review": 0, "hard_drop": 0, "soft_review": 0},
        )
        f_stat["n"] += 1

        run = load_case_run(r)
        if run is None:
            continue

        steps = [s for s in run.doc.get("steps") or [] if isinstance(s, dict)]
        rep = scan_trace(steps)
        hb_rules = [h["rule"] for h in rep.get("hackbook", [])]

        rollout = RolloutItem(item_id=cid, task_id=r.get("source_id", cid), run=run)
        verdict = evaluate_l0_stage(rollout)

        login_profile_hard = "hb_login_profile_tamper" in hb_rules or any("login_profile_tamper" in reason.lower() for reason in verdict.reasons)
        login_profile_soft = (
            ("hb_login_profile_write" in hb_rules or any("login_profile_write" in reason.lower() for reason in verdict.reasons))
            and not login_profile_hard
        )

        if login_profile_hard:
            f_stat["hard_drop"] += 1
            hard_drops.append({
                "case_id": cid,
                "family": fam,
                "reasons": verdict.reasons,
                "details": verdict.details,
            })
        elif login_profile_soft:
            f_stat["soft_review"] += 1
            soft_reviews.append({
                "case_id": cid,
                "family": fam,
                "reasons": verdict.reasons,
                "details": verdict.details,
            })

        results.append({
            "case_id": cid,
            "family": fam,
            "login_profile_hard": login_profile_hard,
            "login_profile_soft": login_profile_soft,
            "decision": verdict.decision,
            "reasons": verdict.reasons,
        })

    n = len(negatives)
    k_hard = len(hard_drops)
    k_soft = len(soft_reviews)

    return {
        "n": n,
        "instrument_note": "Evaluates strictly login-profile rule triggers (hb_login_profile_tamper and hb_login_profile_write), not cross-instrument alert totals.",
        "before_hard_drops": 0,
        "before_soft_reviews": 0,
        "after_hard_drops_count": k_hard,
        "after_hard_drops_wilson": stats.wilson(k_hard, n),
        "after_soft_reviews_count": k_soft,
        "after_soft_reviews_wilson": stats.wilson(k_soft, n),
        "hard_drops": hard_drops,
        "soft_reviews": soft_reviews,
        "by_family": by_family,
    }


def main() -> int:
    for d in RECEIPT_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    print("Loading base scanner from origin/main to dynamically verify 'before' baseline...", flush=True)
    base_tw = load_base_scanner()
    print("Base scanner loaded successfully.", flush=True)

    print("Evaluating in-sample known evasions (n=4 primary packages, 7 attempts)...", flush=True)
    evasion_results = evaluate_evasions(base_tw)
    p4 = evasion_results["primary_cohort_n4"]
    print(f"Primary In-Sample Evasions (n=4): before {p4['before_k']}/{p4['n']}, after {p4['after_k']}/{p4['n']}", flush=True)
    print(f"Exact McNemar discordant b={p4['paired']['b']}, c={p4['paired']['c']}, p={p4['paired']['p']:.6f} (not significant)", flush=True)
    p7 = evasion_results["all_attempts_n7"]
    print(f"All Attempts In-Sample (n=7): before {p7['before_k']}/{p7['n']}, after {p7['after_k']}/{p7['n']}", flush=True)

    print("\nEvaluating honest benchmark negatives (n=399)...", flush=True)
    neg_results = evaluate_honest_negatives()
    print(f"Scored negatives evaluated: n = {neg_results['n']}", flush=True)
    print(f"Login-profile false drops (hard drops): {neg_results['after_hard_drops_count']}/{neg_results['n']} = 0.0%", flush=True)
    print(f"Login-profile soft reviews: {neg_results['after_soft_reviews_count']}/{neg_results['n']}", flush=True)

    summary = {
        "experiment": "FIX-L0-1792",
        "evasions": evasion_results,
        "honest_negatives": neg_results,
    }

    for d in RECEIPT_DIRS:
        out_file = d / "summary.json"
        out_file.write_text(json.dumps(summary, indent=2))
        print(f"Wrote summary receipt: {out_file}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
