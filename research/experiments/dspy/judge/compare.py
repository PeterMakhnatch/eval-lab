"""Side-by-side table of every judge run's metrics plus instruction inspection.

    PYTHONPATH=research/experiments/dspy uv run python -m judge.compare

Reads ``artifacts/*/metrics-*.json`` and ``artifacts/*/program.json``; prints a
Markdown table (mean agreement per family/split, calls, cost) and, per compiled
program, the inspector's family-fact counts so shortcut content is visible next
to the scores. No model calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from .inspect_program import inspect

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
FAMILIES = ("checkout-pool-exhaustion", "retry-storm-backlog")
SPLITS = ("heldout", "all")


def main() -> int:
    runs = sorted(p for p in ARTIFACTS.iterdir() if p.is_dir())
    header = (
        ["run"] + [f"{f.split('-')[0]}/{s}" for f in FAMILIES for s in SPLITS] + ["calls", "≈$"]
    )
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for run in runs:
        cells, calls, cost = [], 0, 0.0
        for family in FAMILIES:
            for split in SPLITS:
                path = run / f"metrics-{family}-{split}.json"
                if not path.exists():
                    cells.append("—")
                    continue
                metrics = json.loads(path.read_text(encoding="utf-8"))
                cells.append(f"{metrics['mean_agreement']:.4f} (n={metrics['documents']})")
                calls += metrics["usage"]["calls"]
                cost += metrics["usage"]["litellm_estimated_cost_usd"]
        summary = run / "optimize-summary.json"
        if summary.exists():
            s = json.loads(summary.read_text(encoding="utf-8"))
            calls += s["task_lm_usage"]["calls"] + s["reflection_lm_usage"]["calls"]
            cost += (
                s["task_lm_usage"]["litellm_estimated_cost_usd"]
                + s["reflection_lm_usage"]["litellm_estimated_cost_usd"]
            )
        print(f"| {run.name} | " + " | ".join(cells) + f" | {calls} | {cost:.3f} |")
    print()
    for run in runs:
        program = run / "program.json"
        if not program.exists():
            continue
        report = inspect(program)
        counts = {
            family.split("-")[0]: (
                len(report[family]["family_specific_fact_tokens_mentioned"]),
                f"{report[family]['criterion_names_mentioned']}/{report[family]['criterion_names_total']}",
            )
            for family in FAMILIES
        }
        print(
            f"- **{run.name}**: {report['instruction_chars']} chars, {report['instruction_lines']} lines, "
            f"{report['demos']} demos; family-fact tokens / criterion names mentioned: {counts}; "
            f"fixture files named: {report['fixture_files_named'] or 'none'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
