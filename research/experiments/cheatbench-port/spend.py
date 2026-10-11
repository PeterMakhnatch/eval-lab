#!/usr/bin/env python3
"""Total provider spend of the CheatBench-port experiment (agents + every judge call).

Agent dollars come from each job's proxy ledger (`lab-metadata.json` cost block).
Judge dollars come from every row of every judge jsonl, including calls that
failed validation (their token usage is parsed from the recorded error text and
priced at the route's pinned rates), so retries and wasted calls are counted.

Usage: uv run --no-sync python research/experiments/cheatbench-port/spend.py \
           --runs runs --judge-glob '/private/tmp/cb-out/judge-*.jsonl' [--judge-glob ...] \
           [--jobs <job name or glob> ...]
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import glob
import json
import re
from collections import defaultdict
from pathlib import Path

PRICES = {  # USD per 1M tokens (in, out), pinned in judge/judge.py
    "glm-5.3": (1.40, 4.40), "glm-5.3-flash": (0.15, 0.50),
    "Qwen/Qwen3.8-27B": (1.86, 5.595), "Qwen/Qwen3.6-35B-A3B": (0.54, 1.335),
}


def failed_call_cost(row: dict) -> float:
    m = re.search(r"usage=(\{.*?\})\. Raw reply", row.get("judge_error") or "", re.S)
    if not m:
        return 0.0
    usage = ast.literal_eval(m.group(1))
    pin, pout = PRICES[row["judge_model"]]
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1e6


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=Path, default=Path("runs"))
    p.add_argument("--judge-glob", action="append", default=[])
    p.add_argument("--jobs", nargs="*", default=None,
                   help="limit agent spend to these job names/globs (default: every cb-* job)")
    args = p.parse_args()

    agent = defaultdict(float)
    for lab in args.runs.glob("cb-*/lab-metadata.json"):
        job = lab.parent.name
        if args.jobs and not any(fnmatch.fnmatchcase(job, pat) for pat in args.jobs):
            continue
        meta = json.loads(lab.read_text())
        cost = (meta.get("cost") or {})
        usd = float(cost.get("cost_usd") or 0) + float(cost.get("attempted_cost_usd") or 0)
        model = (meta.get("model_identity") or {}).get("requested")
        if not model:
            if usd == 0:
                continue  # free oracle/nop/canary controls carry no model identity
            model = f"unknown ({job})"
        agent[model] += usd
    judge = defaultdict(float)
    for pattern in args.judge_glob:
        for path in glob.glob(pattern):
            for line in Path(path).read_text().splitlines():
                row = json.loads(line)
                judge[row.get("judge_model", "?")] += float(row.get("judge_cost_usd") or 0)
                judge[row.get("judge_model", "?")] += failed_call_cost(row)
    print("| Item | USD |\n|---|---:|")
    for k, v in sorted(agent.items()):
        print(f"| agent {k} | {v:.2f} |")
    for k, v in sorted(judge.items()):
        print(f"| judge {k} | {v:.2f} |")
    print(f"| **total** | **{sum(agent.values()) + sum(judge.values()):.2f}** |")


if __name__ == "__main__":
    main()
