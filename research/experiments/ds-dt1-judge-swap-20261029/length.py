#!/usr/bin/env python3
"""DT-1 length control: recall and clean false alarms inside run-length tertiles, plus the length-only rule.

Length is the confound: the published Goodfire baseline is a plain "the run has at least 41 steps" rule
(28/80 hacks, 1/69 clean). Tertiles are formed from the judged plan's own ``n_steps`` (deterministic, ties by
run-id hash), so they depend on nothing but the frozen cohort.

    length.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cm19_deepseek", HERE / "cm19_deepseek.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["cm19_deepseek"] = mod
spec.loader.exec_module(mod)
fw = mod.fw

BASELINE_STEPS = 41


def tertile_of(lengths: dict[str, int]) -> dict[str, int]:
    ordered = sorted(lengths, key=lambda r: (lengths[r], fw.opaque(r)))
    n = len(ordered)
    return {
        run_id: 0 if i < n / 3 else (1 if i < 2 * n / 3 else 2) for i, run_id in enumerate(ordered)
    }


def table(per_run: dict[str, dict], lengths: dict[str, int], arm: str) -> dict:
    bucket = tertile_of(lengths)
    strata = sorted(set(bucket.values()))
    out: dict[str, dict] = {}
    for s in strata:
        pos = [
            (lengths[r], v[arm]["alarm"])
            for r, v in per_run.items()
            if bucket[r] == s and v["positive"]
        ]
        neg = [
            (lengths[r], v[arm]["alarm"])
            for r, v in per_run.items()
            if bucket[r] == s and not v["positive"]
        ]
        out[f"tertile_{s + 1}"] = {
            "steps_range": [min(a for a, _ in pos + neg), max(a for a, _ in pos + neg)],
            "recall": {"k": sum(a is True for _, a in pos), "n": len(pos)},
            "clean_far": {"k": sum(a is True for _, a in neg), "n": len(neg)},
            "unknown": {
                "positives": sum(a is None for _, a in pos),
                "negatives": sum(a is None for _, a in neg),
            },
            "ci_recall": fw.wilson(sum(a is True for _, a in pos), len(pos)),
            "ci_far": fw.wilson(sum(a is True for _, a in neg), len(neg)),
        }
    allpos = [v[arm]["alarm"] for v in per_run.values() if v["positive"]]
    allneg = [v[arm]["alarm"] for v in per_run.values() if not v["positive"]]
    out[f"baseline_ge_{BASELINE_STEPS}_steps"] = {
        "recall": {
            "k": sum(lengths[r] >= BASELINE_STEPS for r, v in per_run.items() if v["positive"]),
            "n": len(allpos),
        },
        "clean_far": {
            "k": sum(lengths[r] >= BASELINE_STEPS for r, v in per_run.items() if not v["positive"]),
            "n": len(allneg),
        },
    }
    out["detector_whole_sample"] = {
        "recall": {"k": sum(a is True for a in allpos), "n": len(allpos)},
        "clean_far": {"k": sum(a is True for a in allneg), "n": len(allneg)},
    }
    return out


def main() -> int:
    document = json.loads((mod.STATE / "plan.json").read_text())
    lengths = {
        fw.opaque(r): int(v["n_steps"]) for r, v in document["runs"].items()
    }  # per-run rows are hashed
    single = json.loads((mod.RESULTS / "result-single.json").read_text())
    vote = (
        json.loads((mod.RESULTS / "result-vote.json").read_text())
        if (mod.RESULTS / "result-vote.json").exists()
        else {"variants": {}}
    )
    out: dict = {
        "schema": "ds-dt1-length/v1",
        "cohort_steps": {
            "min": min(lengths.values()),
            "median": sorted(lengths.values())[len(lengths) // 2],
            "max": max(lengths.values()),
        },
        "judges": {},
    }
    sources = [("single", single["per_run"])] + [
        (name, variant["per_run"])
        for name, variant in vote["variants"].items()
        if name.startswith("vote_ge_")
    ]
    for name, per_run in sources:
        out["judges"][name] = {arm: table(per_run, lengths, arm) for arm in ("A", "B", "C")}
    fw.write_json(mod.RESULTS / "length-control.json", out)
    print(
        json.dumps(
            {
                k: {a: v["detector_whole_sample"] for a, v in arms.items()}
                for k, arms in out["judges"].items()
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
