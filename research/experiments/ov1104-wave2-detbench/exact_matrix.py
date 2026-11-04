#!/usr/bin/env python3
"""Reproduce selected DEV matrix points; never deserialize non-DEV JSONL rows."""

import json
import math
import sys
from collections import Counter
from pathlib import Path

import analyze as a

SOURCE = (
    Path.home()
    / "Developer/eval-lab/.worktrees/ov1104-detbench/research/experiments/ov1104-detbench"
)
sys.path.insert(0, str(SOURCE))
import score  # noqa: E402

MATRIX = Path("/private/tmp/ov1104/detbench/matrix.json")
BATCH = (
    Path.home()
    / "Developer/eval-lab-results/line/replay-v1/batches/replay-v1-replay-f4ea98c6ccd94192a4f5ef19a29c521e"
)
STEMS = {**a.NAMES, "length": "length_chars"}


def main():
    runs = {r["run_key"]: r for r in a.lines(a.ROOT / "runs.jsonl") if r["split"] == "dev"}
    assert len(runs) == 690
    sigs = {
        n: {
            r["run_key"]: r
            for r in a.lines(a.ROOT / "signals" / f"{stem}.jsonl")
            if r["run_key"] in runs
        }
        for n, stem in STEMS.items()
    }
    selected = {
        r["signal"]: {
            "method": r["method"],
            "members": r["members"],
            "threshold": r["thresholds"]["0.05"],
            "dev": r["per_set"]["pooled:dev"]["at"]["0.05"],
        }
        for r in json.loads(MATRIX.read_text())
        if r.get("signal") in a.COMBOS
    }
    assert set(selected) == set(a.COMBOS)
    results = {}
    for name, (method, members) in a.COMBOS.items():
        entry = selected[name]
        assert entry["method"] == method and entry["members"] == [STEMS[n] for n in members]
        signals = [sigs[n] for n in members]
        combined = score.combine(signals, runs, method, names=entry["members"])
        threshold = entry["threshold"] if entry["threshold"] is not None else math.inf
        decisions = {
            k: ("flag" if v["score"] >= threshold else "keep")
            for k, v in combined.items()
            if v["score"] is not None
        }
        rows, _, ap, an, _ = score._rows_for(runs, combined, runs)
        rates = score.rates_at(rows, threshold, abstained_neg=an, abstained_pos=ap)
        parity = {
            key: {
                "computed": rates[key],
                "matrix": entry["dev"][key],
                "match": rates[key] == entry["dev"][key],
            }
            for key in ("fp", "n_neg", "tp", "n_pos", "fpr", "recall")
        }
        component_thresholds = (
            {
                n: (lambda t: None if math.isinf(t) else t)(
                    score.choose_threshold(score._rows_for(runs, sigs[n], runs)[0], 0.05)
                )
                for n in members
            }
            if method == "or"
            else None
        )
        missing = {
            n: {
                "pos": sum(
                    runs[k]["label"] == 1 and (k not in sigs[n] or sigs[n][k]["score"] is None)
                    for k in runs
                ),
                "neg": sum(
                    runs[k]["label"] == 0 and (k not in sigs[n] or sigs[n][k]["score"] is None)
                    for k in runs
                ),
            }
            for n in members
        }
        incomplete = sum(
            any(k not in sigs[n] or sigs[n][k]["score"] is None for n in members) for k in runs
        )
        results[name] = {
            "threshold": entry["threshold"],
            "component_thresholds": component_thresholds,
            "missing_by_component": missing,
            "any_missing": incomplete,
            "parity": parity,
            "groups": a.summarize(runs, decisions),
            "dev_rates": rates,
        }
    baseline = json.loads(a.BASE.read_text())["decisions"]
    base = {(r["corpus"], r["run_id"]): r["decision"] for r in baseline}
    recorded = {}
    for line in (BATCH / "decisions.jsonl").open():
        r = json.loads(line)
        key = (r["corpus"], r["run_id"])
        assert key not in recorded
        recorded[key] = r["decision"]
    export = {}
    for decision, filename in [("keep", "kept"), ("drop", "dropped"), ("review", "review")]:
        for line in (BATCH / "export" / f"{filename}.jsonl").open():
            r = json.loads(line)
            key = (r["corpus"], r["run_id"])
            assert key not in export
            export[key] = decision
    validation = {
        "baseline_counts": dict(Counter(base.values())),
        "recorded_counts": dict(Counter(recorded.values())),
        "export_counts": dict(Counter(export.values())),
        "baseline_vs_recorded_mismatches": sum(
            base.get(k) != recorded.get(k) for k in base.keys() | recorded.keys()
        ),
        "baseline_vs_export_mismatches": sum(
            base.get(k) != export.get(k) for k in base.keys() | export.keys()
        ),
    }
    Path("exact-matrix-aggregates.json").write_text(
        json.dumps(
            {"combinations": results, "replay_validation": validation}, indent=2, sort_keys=True
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
