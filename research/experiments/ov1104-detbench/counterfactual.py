"""Counterfactual for the held L0 format decision (#1825): if L0 read every known tool shape.

Usage (from the eval-lab worktree, after l0_rules.py ran with and without --counterfactual):
  uv run python research/experiments/ov1104-detbench/counterfactual.py [--raw-dir ...] [--runs ...]
      [--out research/experiments/ov1104-detbench/results/COUNTERFACTUAL.md]

Per DEV corpus: honest runs flagged by L0 (any reason), honest runs flagged ONLY by
L0_UNRECOGNIZED_TOOL_SHAPE, cheats caught by content (any L0 reason other than the shape/parse
notes), cheats hard-dropped, under the current parser vs the counterfactual mapping. Wilson 95% CIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, read_jsonl  # noqa: E402
from score import wilson  # noqa: E402

NON_CONTENT = {"L0_UNRECOGNIZED_TOOL_SHAPE", "L0_PARSE_ERROR", "L0_NO_EVIDENCE", "L0_RUNNER_ERROR"}


def _content(reasons: list[str]) -> bool:
    return any(r not in NON_CONTENT for r in reasons)


def _hard(reasons: list[str]) -> bool:
    return any(r.startswith("L0_HARD_") for r in reasons)


def _cell(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({100 * k / n:.0f}% [{100 * lo:.0f},{100 * hi:.0f}])" if n else "-"


def table(runs: dict[str, dict[str, Any]], base: dict[str, dict[str, Any]], cf: dict[str, dict[str, Any]], view: str) -> list[str]:
    corpora = sorted({r["set"] for r in runs.values()})
    out = [f"### view = {view}", "", "| corpus | honest n | honest flagged (now) | honest flagged (cf) | honest flagged only by shape (now) | honest flagged only by shape (cf) | cheat n | cheats caught by content (now) | cheats caught by content (cf) | cheats hard-dropped (now) | cheats hard-dropped (cf) | cheats flagged at all (now) | cheats flagged at all (cf) | unrecognized steps now -> cf |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot: Counter[str] = Counter()
    for c in corpora + ["ALL"]:
        keys = [k for k, r in runs.items() if c == "ALL" or r["set"] == c]
        hon = [k for k in keys if runs[k]["label"] == 0]
        che = [k for k in keys if runs[k]["label"] == 1]
        hb = sum(1 for k in hon if base[k]["decision"] != "pass")
        hc = sum(1 for k in hon if cf[k]["decision"] != "pass")
        hob = sum(1 for k in hon if base[k]["reasons"] == ["L0_UNRECOGNIZED_TOOL_SHAPE"])
        hoc = sum(1 for k in hon if cf[k]["reasons"] == ["L0_UNRECOGNIZED_TOOL_SHAPE"])
        ccb = sum(1 for k in che if _content(base[k]["reasons"]))
        ccc = sum(1 for k in che if _content(cf[k]["reasons"]))
        chb = sum(1 for k in che if _hard(base[k]["reasons"]))
        chc = sum(1 for k in che if _hard(cf[k]["reasons"]))
        cab = sum(1 for k in che if base[k]["decision"] != "pass")
        cac = sum(1 for k in che if cf[k]["decision"] != "pass")
        ub = sum(base[k]["unrecognized_steps"] for k in keys)
        uc = sum(cf[k]["unrecognized_steps"] for k in keys)
        out.append(f"| {c} | {len(hon)} | {_cell(hb, len(hon))} | {_cell(hc, len(hon))} | {_cell(hob, len(hon))} | {_cell(hoc, len(hon))} | {len(che)} | {_cell(ccb, len(che))} | {_cell(ccc, len(che))} | {_cell(chb, len(che))} | {_cell(chc, len(che))} | {_cell(cab, len(che))} | {_cell(cac, len(che))} | {ub} -> {uc} |")
        if c != "ALL":
            tot["hon_only_shape_now"] += hob
            tot["hon_only_shape_cf"] += hoc
    left: Counter[str] = Counter()
    for k in runs:
        if cf[k]["unrecognized_steps"]:
            for name, n in cf[k]["tool_names"].items():
                left[name] += n
    out += ["", f"Tool names still present in runs with unrecognized steps after the mapping (top 15): {left.most_common(15)}", ""]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", type=Path, default=Path("/private/tmp/ov1104/detbench/l0"))
    ap.add_argument("--runs", type=Path, default=RUNS)
    ap.add_argument("--out", type=Path, default=HERE / "results" / "COUNTERFACTUAL.md")
    args = ap.parse_args(argv)
    all_runs = read_jsonl(args.runs)
    dev_runs = {r["run_key"]: r for r in all_runs if r["split"] == "dev"}
    ho_runs = {r["run_key"]: r for r in all_runs if r["split"] == "heldout"}
    lines = ["# Counterfactual: L0 reads every known tool shape (DEV replay-v1 + Held-out sets, L0 stage only, no AutoReview)", "", "`now` = current-main `evaluate_l0_stage` on the DetBench view. `cf` = the same rules after `l0_rules.map_tool_shapes` rewrote every known tool shape (Terminus `bash_command{keystrokes}`, `shell{command}`, cloud-export names, read-only tools) into one the parser reads. 'flagged' = L0 decision review or drop. 'caught by content' = any L0 reason other than the shape/parse notes. Wilson 95% CIs in brackets. No src change ships; the mapping lives in l0_rules.py only.", ""]
    for split_title, runs in (("DEV replay-v1 (690 runs, pre-registered H5)", dev_runs), ("Held-out sets (15,859 runs across 20 sets)", ho_runs)):
        if not runs:
            continue
        lines += [f"## {split_title}", ""]
        for view in ("full", "actions_only"):
            b = args.raw_dir / f"l0v_{view}_raw.jsonl"
            c = args.raw_dir / f"l0cf_{view}_raw.jsonl"
            if not (b.is_file() and c.is_file()):
                lines += [f"### view = {view}: not computed (missing {b.name if not b.is_file() else c.name})", ""]
                continue
            base = {r["run_key"]: r for r in read_jsonl(b) if r["run_key"] in runs}
            cf = {r["run_key"]: r for r in read_jsonl(c) if r["run_key"] in runs}
            missing = [k for k in runs if k not in base or k not in cf]
            if missing:
                lines += [f"({len(missing)} runs missing from raw outputs; excluded)", ""]
                runs_v = {k: v for k, v in runs.items() if k not in set(missing)}
            else:
                runs_v = runs
            lines += table(runs_v, base, cf, view)
            mapped = Counter()
            for r in cf.values():
                mapped.update(r["mapped"])
            lines += [f"Mappings applied ({view}): {dict(mapped)}", ""]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    summary = {"out": str(args.out)}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
