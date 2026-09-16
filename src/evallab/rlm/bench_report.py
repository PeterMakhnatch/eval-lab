"""Aggregate synthetic-bench result rows into a comparison report.

Reads every ``*.jsonl`` produced by ``evallab.rlm.bench_runner`` under one
directory, groups rows by policy (pooling repeats), and renders:

- an overall table (accuracy, mean cost, iterations, sub-calls, tokens, errors);
- per-family accuracy per policy;
- a paired comparison against a baseline policy over the task ids both ran
  (wins / losses / ties on score, plus the exact two-sided sign-test p-value so
  small-n differences are not oversold).

Usage::

    python -m evallab.rlm.bench_report --dir runs/x/bench --baseline stock [--markdown out.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from math import comb
from pathlib import Path
from typing import Any


def load_rows(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                row["_file"] = path.name
                rows.append(row)
    return rows


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test p-value for wins vs losses (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / 2**n
    return min(1.0, 2 * tail)


def policy_blocks(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["policy"]].append(row)
    blocks: dict[str, dict[str, Any]] = {}
    for policy, items in grouped.items():
        families: dict[str, list[float]] = defaultdict(list)
        for row in items:
            families[row["family"]].append(row["score"])
        blocks[policy] = {
            "n": len(items),
            "accuracy": _mean([r["score"] for r in items]),
            "errors": sum(1 for r in items if r.get("error")),
            "budget_stopped": sum(1 for r in items if r.get("budget_stopped")),
            "mean_iterations": _mean([r["iterations"] for r in items]),
            "mean_sub_calls": _mean([r["sub_calls"] for r in items]),
            "mean_input_tokens": _mean([r["input_tokens"] for r in items]),
            "mean_output_tokens": _mean([r["output_tokens"] for r in items]),
            "mean_reasoning_tokens": _mean([r.get("reasoning_tokens", 0) for r in items]),
            "mean_cost_usd": _mean([r["cost_usd"] for r in items]),
            "total_cost_usd": sum(r["cost_usd"] for r in items),
            "mean_wall_seconds": _mean([r["wall_seconds"] for r in items]),
            "by_family": {family: _mean(scores) for family, scores in sorted(families.items())},
            "family_n": {family: len(scores) for family, scores in sorted(families.items())},
            "files": sorted({r["_file"] for r in items}),
        }
    return blocks


def paired(rows: list[dict[str, Any]], baseline: str, candidate: str) -> dict[str, Any]:
    """Pair on (task_id, repeat-rank) so repeats compare like with like."""

    def keyed(policy: str) -> dict[tuple[str, int], float]:
        per_task: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            if row["policy"] == policy:
                per_task[row["task_id"]].append(row["score"])
        out: dict[tuple[str, int], float] = {}
        for task_id, scores in per_task.items():
            for rank, value in enumerate(scores):
                out[(task_id, rank)] = value
        return out

    base = keyed(baseline)
    cand = keyed(candidate)
    shared = sorted(set(base) & set(cand))
    wins = sum(1 for key in shared if cand[key] > base[key])
    losses = sum(1 for key in shared if cand[key] < base[key])
    ties = len(shared) - wins - losses
    delta = _mean([cand[k] for k in shared]) - _mean([base[k] for k in shared]) if shared else 0.0
    return {
        "candidate": candidate,
        "baseline": baseline,
        "paired_n": len(shared),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "accuracy_delta": delta,
        "sign_test_p": sign_test_p(wins, losses),
    }


def render(blocks: dict[str, dict[str, Any]], pairs: list[dict[str, Any]], baseline: str) -> str:
    families = sorted({family for block in blocks.values() for family in block["by_family"]})
    lines = ["| policy | n | acc | " + " | ".join(families) + " | err | budget | iters | sub | in tok | out tok | reason tok | $/task | wall s |",
             "|---|" + "---|" * (10 + len(families))]
    for policy, block in sorted(blocks.items(), key=lambda kv: -kv[1]["accuracy"]):
        fam = " | ".join(f"{block['by_family'].get(f, float('nan')):.2f}" for f in families)
        lines.append(
            f"| {policy} | {block['n']} | {block['accuracy']:.3f} | {fam} | {block['errors']} | {block['budget_stopped']} | "
            f"{block['mean_iterations']:.1f} | {block['mean_sub_calls']:.1f} | {block['mean_input_tokens']:.0f} | "
            f"{block['mean_output_tokens']:.0f} | {block['mean_reasoning_tokens']:.0f} | {block['mean_cost_usd']:.3f} | {block['mean_wall_seconds']:.0f} |"
        )
    lines.append("")
    lines.append(f"Paired against `{baseline}` (same task ids; exact two-sided sign test):")
    lines.append("")
    lines.append("| candidate | paired n | wins | losses | ties | acc delta | p |")
    lines.append("|---|---|---|---|---|---|---|")
    for pair in pairs:
        lines.append(
            f"| {pair['candidate']} | {pair['paired_n']} | {pair['wins']} | {pair['losses']} | {pair['ties']} | "
            f"{pair['accuracy_delta']:+.3f} | {pair['sign_test_p']:.3f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--baseline", default="stock")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    rows = load_rows(args.dir)
    if not rows:
        raise SystemExit(f"no result rows under {args.dir}")
    blocks = policy_blocks(rows)
    pairs = [paired(rows, args.baseline, policy) for policy in sorted(blocks) if policy != args.baseline]
    text = render(blocks, pairs, args.baseline)
    print(text)
    if args.markdown:
        args.markdown.write_text(text + "\n")
    if args.json:
        args.json.write_text(json.dumps({"policies": blocks, "paired": pairs}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
