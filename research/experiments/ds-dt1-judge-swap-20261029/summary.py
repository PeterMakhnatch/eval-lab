#!/usr/bin/env python3
"""DT-1: render the numbers from the result files as markdown, so the report quotes files rather than memory.

summary.py
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


def fmt(point: dict, key: str) -> str:
    block = point[key]
    ci = block["ci"]
    return f"{block['k']}/{block['n']} ({block['pct'] * 100:.1f}%; {ci[0] * 100:.1f}-{ci[1] * 100:.1f})"


def arms_table(arms: dict, title: str) -> str:
    lines = [
        f"**{title}**",
        "",
        "| Arm | Recall | Clean false alarms | Unknown (pos/neg) | Exact step | Within 5 | On labelled step | Windows per run | Tokens per run |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for arm, value in arms.items():
        point = value["operating_point"]
        loc = value["localization_on_80_enacted"]
        cost = value["cost"]
        lines.append(
            f"| {arm} | {fmt(point, 'recall')} | {fmt(point, 'far')} | "
            f"{point['unknown']['positives']}/{point['unknown']['negatives']} | "
            f"{loc['flag_exact']} | {loc['flag_within5']} | {loc['confirmed_on_labelled_step']} | "
            f"{cost['windows_per_run']} | {cost['tokens_per_run']:.0f} |"
        )
    return "\n".join(lines)


def main() -> int:
    out: list[str] = []
    single = json.loads((mod.RESULTS / "result-single.json").read_text())
    out.append(arms_table(single["arms"], "Single pass (pass 1, thinking low), 418 prompts"))
    out.append("")
    vote = json.loads((mod.RESULTS / "result-vote.json").read_text())
    out.append(f"Vote over {vote['passes']} passes:")
    out.append("")
    for name, variant in vote["variants"].items():
        out.append(arms_table(variant["arms"], name))
        out.append("")
    length = json.loads((mod.RESULTS / "length-control.json").read_text())
    out.append("**Length control (arm C):**")
    out.append("")
    out.append("| Judge | Tertile | Steps | Recall | Clean FAR |")
    out.append("|---|---|---|---|---|")
    for judge, arms in length["judges"].items():
        for key, block in arms["C"].items():
            if key == "detector_whole_sample":
                rng = "all"
            elif key.startswith("baseline"):
                rng = ">= 41"
            else:
                rng = f"{block['steps_range'][0]}-{block['steps_range'][1]}"
            out.append(
                f"| {judge} | {key} | {rng} | "
                f"{block['recall']['k']}/{block['recall']['n']} | "
                f"{block['clean_far']['k']}/{block['clean_far']['n']} |"
            )
    out.append("")
    if (mod.RESULTS / "compare.json").exists():
        compare = json.loads((mod.RESULTS / "compare.json").read_text())
        out.append("**Window-level (complete cases):**")
        out.append("")
        out.append(
            "| Pair | Judged both | Confirmed both | deepseek only | other only | sign test p |"
        )
        out.append("|---|---|---|---|---|---|")
        for name, block in compare["window_pairs"].items():
            out.append(
                f"| {name} | {block['judged_by_both']} | {block['confirmed_both']} | "
                f"{block['deepseek_only']} | {block['other_only']} | {block['sign_test_p']} |"
            )
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
