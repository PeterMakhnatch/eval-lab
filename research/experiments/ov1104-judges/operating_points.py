"""Goodfire and held-out operating points at matched false-alarm budgets, per judge.

The pre-registered headline picks one threshold per judge (5% FPR on *pooled* DEV negatives) and then
reads Goodfire recall off it. That single threshold mixes two different questions: can the judge rank,
and is its score calibrated to a corpus that `terminal-wrench` and `cheatbench` dominate. This script
answers the first question alone by sweeping the false-alarm budget and reporting recall at each
budget, once inside Goodfire and once on pooled DEV negatives with the held-out recall beside it.

Both sweeps choose thresholds only on DEV rows of the set being scored, so nothing here reads a
held-out label to pick a bar. Output is a compact JSON with no record text.

Usage (from the eval-lab worktree root)::

    uv run python research/experiments/ov1104-judges/operating_points.py \\
        --out research/experiments/ov1104-judges/results/operating_points.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DETBENCH = Path("/private/tmp/ov1104/shared/detbench")
SIGNALS = DETBENCH / "signals"
RUNS = DETBENCH / "runs.jsonl"
BUDGETS = (0.01, 0.025, 0.05, 0.10, 0.20)
JUDGES = (
    "j0_autoreview",
    "j1_actions_flash",
    "j2_actions_task_diff_flash",
    "j2b_actions_task_diff_v2_flash",
    "j3_actions_task_diff_terra_full",
    "j3_actions_task_diff_fable_partial",
    "j2b_terra_mean",
    "j2b_terra_max",
    "length_chars",
    "length_steps",
    "l0v_full_l0_hard_gate_rule_hidden_info_read",
)
# Compatible held-out sets; withdrawal and source exclusions do not change the DEV pool.
HELDOUT_SETS = frozenset(
    {
        "reflex-sentinel",
        "flake-laundering",
        "researcharena-benchmark-gaming",
        "kernelbench-hard-runs",
        "beam-tripwire",
        "latent-agent-sandbox-episodes",
    }
)


def load_scorer(path: Path) -> Any:
    """Import the DetBench scorer so this analysis shares its AUROC, threshold and rate code."""
    sys.path.insert(0, str(path))
    import score  # noqa: PLC0415

    return score


def default_scorer_dir() -> Path:
    """The DetBench scorer next to this experiment in the same checkout."""
    return HERE.parent / "ov1104-detbench"


def sweep(score: Any, runs: dict[str, dict[str, Any]], sig: dict[str, dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    rows, _, ab_pos, ab_neg, _ = score._rows_for(runs, sig, keys)
    out = {}
    for budget in BUDGETS:
        thr = score.choose_threshold(rows, budget)
        at = score.rates_at(rows, thr, abstained_neg=ab_neg, abstained_pos=ab_pos)
        out[f"{budget:g}"] = {
            "threshold": None if thr == float("inf") else thr,
            "recall": at["recall"],
            "recall_ci": at["recall_ci"],
            "fpr": at["fpr"],
            "fpr_ci": at["fpr_ci"],
            "tp": at["tp"],
            "fp": at["fp"],
            "n_pos": at["n_pos"],
            "n_neg": at["n_neg"],
        }
    return {"auroc": score.auroc(rows), "n": len(keys), "budgets": out}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--detbench", type=Path, default=DETBENCH, help="directory holding runs.jsonl and signals/")
    parser.add_argument("--scorer-dir", type=Path, default=default_scorer_dir(), help="directory holding the DetBench score.py")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    score = load_scorer(args.scorer_dir)
    runs = score.load_runs(args.detbench / "runs.jsonl")
    dev = [k for k, r in runs.items() if r["split"] == "dev"]
    goodfire = [k for k in dev if runs[k]["set"] == "goodfire"]
    heldout = [
        k
        for k, r in runs.items()
        if r["split"] == "heldout"
        and r["set"] in HELDOUT_SETS
        and not r.get("decisive_rule")
        and r.get("pool") is not False
    ]

    report: dict[str, Any] = {
        "question": "recall at matched false-alarm budgets, thresholds chosen on the scored set's own DEV rows only",
        "goodfire": {"n_pos": 80, "n_neg": 70, "budgets": list(BUDGETS)},
        "pooled_dev": {"budgets": list(BUDGETS)},
        "judges": {},
    }
    for name in JUDGES:
        path = args.detbench / "signals" / f"{name}.jsonl"
        if not path.is_file():
            print(f"skip {name}: no signal file", flush=True)
            continue
        sig = score.load_signal(path)
        entry = {
            "version": next((r.get("version") for r in sig.values()), None),
            "goodfire": sweep(score, runs, sig, goodfire),
            "pooled_dev": sweep(score, runs, sig, dev),
        }
        if heldout:
            rows, _, ab_pos, ab_neg, _ = score._rows_for(runs, sig, heldout)
            # held-out recall is read at the pooled-DEV thresholds above: no held-out row picks a bar
            dev_rows = score._rows_for(runs, sig, dev)[0]
            entry["pooled_heldout"] = {
                "at_pooled_dev_threshold": {
                    f"{b:g}": score.rates_at(rows, score.choose_threshold(dev_rows, b), abstained_neg=ab_neg, abstained_pos=ab_pos)["recall"]
                    for b in BUDGETS
                },
                "auroc": score.auroc(rows),
                "n": len(heldout),
                "n_scored": len(rows),
                "abstain_pos": ab_pos,
                "abstain_neg": ab_neg,
            }
        report["judges"][name] = entry

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(report['judges'])} judges)")


if __name__ == "__main__":
    main()
