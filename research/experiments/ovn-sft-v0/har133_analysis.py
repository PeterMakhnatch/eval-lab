"""Reproduce HAR-133's closed G5 analysis from the published-counts snapshot.

Run from the repository root:
    uv run python research/experiments/ovn-sft-v0/har133_analysis.py

Missing outcomes remain missing in the primary analyses. Hypothetical completions
are descriptive sensitivity analyses, never rewritten counts or additional tests
in the confirmatory family. See PREREG.md sections 4–5.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
from collections import Counter
from pathlib import Path

import evallab.analysis_statistics as statistics
from evallab.analysis_statistics import PairedBinaryInput, exact_paired_binary_contrast

ARMS = ("stock", "tuned", "gepa")
PRIMARY = (("tuned", "stock"), ("gepa", "stock"))
PLANNED_TASKS = 20
CellTable = dict[str, dict[str, int | None]]


def contrast(cells: CellTable, arm_a: str, arm_b: str) -> dict:
    complete = [
        task for task, arms in cells.items() if arms[arm_a] is not None and arms[arm_b] is not None
    ]
    result = exact_paired_binary_contrast(
        [
            PairedBinaryInput(
                assignment_unit_id=task,
                arm_a_outcome=cells[task][arm_a],
                arm_b_outcome=cells[task][arm_b],
            )
            for task in complete
        ],
        arm_a_id=arm_a,
        arm_b_id=arm_b,
        confidence_level=0.95,
    ).model_dump(mode="json")
    if result["exact_p_value"] is None:
        raise ValueError(f"Unestimable {arm_a} minus {arm_b}: {result['refusal_code']}")
    result.update(
        arm_a=arm_a,
        arm_b=arm_b,
        complete_tasks=complete,
        omitted_tasks=[task for task in cells if task not in complete],
        arm_a_passes=sum(cells[task][arm_a] == 1 for task in complete),
        arm_b_passes=sum(cells[task][arm_b] == 1 for task in complete),
        discordant_tasks=[task for task in complete if cells[task][arm_a] != cells[task][arm_b]],
    )
    return result


def primary_family(cells: CellTable) -> dict[str, dict]:
    results = {f"{a}_minus_{b}": contrast(cells, a, b) for a, b in PRIMARY}
    running = 0.0
    for rank, name in enumerate(sorted(results, key=lambda key: results[key]["exact_p_value"])):
        running = max(running, min(1.0, (len(PRIMARY) - rank) * results[name]["exact_p_value"]))
        results[name]["holm_adjusted_p_value"] = running
        results[name]["reject_at_family_alpha_0_05"] = running <= 0.05
    return results


def complete_cells(cells: CellTable, assignments: dict[tuple[str, str], int]) -> CellTable:
    return {
        task: {arm: assignments.get((task, arm), value) for arm, value in arms.items()}
        for task, arms in cells.items()
    }


def analyze(path: Path) -> dict:
    raw = path.read_bytes()
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    cells: CellTable = {}
    repositories = {}
    for row in rows:
        task, arm = row["task_id"], row["arm"]
        if arm not in ARMS or arm in cells.setdefault(task, {}):
            raise ValueError(f"Unexpected or duplicate task-arm cell: {task}/{arm}")
        expected = {"counted_pass": "1", "counted_fail": "0", "excluded": ""}[row["counts_verdict"]]
        if row["outcome"] != expected:
            raise ValueError(f"Outcome disagrees with the counts verdict: {task}/{arm}")
        cells[task][arm] = int(expected) if expected else None
        repositories[task] = row["repository"]
    if len(cells) != PLANNED_TASKS or any(set(arms) != set(ARMS) for arms in cells.values()):
        raise ValueError("Expected the frozen twenty tasks, each with all three arm records")

    primary = primary_family(cells)
    missing = sorted(
        (task, arm) for task, arms in cells.items() for arm, value in arms.items() if value is None
    )
    scenarios = {}
    for value, label in ((0, "all_missing_fail"), (1, "all_missing_pass")):
        scenarios[label] = primary_family(complete_cells(cells, dict.fromkeys(missing, value)))
    scenarios["three_missing_stock_cells_fail_other_missing_unchanged"] = primary_family(
        complete_cells(cells, {key: 0 for key in missing if key[1] == "stock"})
    )

    # HAR-133 17:48Z disposition: a secondary rater-judgment sensitivity only.
    # Copied means excluded (not failed); never mutate the canonical input.
    judgment_cells = {task: arms.copy() for task, arms in cells.items()}
    judgment_cells["format-code-task-000169"]["tuned"] = None

    # Enumerate joint assignments, keeping the three missing stock outcomes shared
    # across contrasts. Uniform all-fail/all-pass scenarios are not sharp bounds.
    completions = []
    for values in itertools.product((0, 1), repeat=len(missing)):
        results = primary_family(complete_cells(cells, dict(zip(missing, values, strict=True))))
        completions.append(
            {
                "missing_outcomes_in_list_order": values,
                "contrasts": {
                    name: {
                        key: result[key]
                        for key in (
                            "risk_difference",
                            "discordant_a_only",
                            "discordant_b_only",
                            "exact_p_value",
                            "holm_adjusted_p_value",
                        )
                    }
                    for name, result in results.items()
                },
            }
        )
    bounds = {}
    for name in primary:
        lowest = min(completions, key=lambda row: row["contrasts"][name]["risk_difference"])
        highest = max(completions, key=lambda row: row["contrasts"][name]["risk_difference"])
        bounds[name] = {
            "denominator": PLANNED_TASKS,
            "risk_difference_lower": lowest["contrasts"][name]["risk_difference"],
            "risk_difference_upper": highest["contrasts"][name]["risk_difference"],
            "lower_witness": lowest["missing_outcomes_in_list_order"],
            "upper_witness": highest["missing_outcomes_in_list_order"],
            "minimum_hypothetical_raw_p": min(
                row["contrasts"][name]["exact_p_value"] for row in completions
            ),
            "minimum_hypothetical_holm_p": min(
                row["contrasts"][name]["holm_adjusted_p_value"] for row in completions
            ),
        }

    arm_summary = {}
    for arm in ARMS:
        counts = Counter(row["counts_verdict"] for row in rows if row["arm"] == arm)
        arm_summary[arm] = {
            "planned": PLANNED_TASKS,
            "raw_passes": sum(row["raw_reward"] == "1" for row in rows if row["arm"] == arm),
            "counted_passes": counts["counted_pass"],
            "counted_failures": counts["counted_fail"],
            "excluded": counts["excluded"],
            "counted_denominator": counts["counted_pass"] + counts["counted_fail"],
            "pass_tasks": [task for task, arms in cells.items() if arms[arm] == 1],
        }

    return {
        "experiment": "ovn-sft-v0",
        "schema": "har133.closed-g5-analysis/v1",
        "input_file": path.name,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "analysis_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "statistics_module_sha256": hashlib.sha256(
            Path(statistics.__file__).read_bytes()
        ).hexdigest(),
        "planned_tasks": PLANNED_TASKS,
        "physical_attempts": len(rows),
        "replacements": 0,
        "family": {
            "size": len(PRIMARY),
            "alpha": 0.05,
            "test": "exact two-sided McNemar",
            "adjustment": "Holm",
        },
        "interval_note": "95% paired score intervals are approximate, marginal and descriptive; not exact or family-wise.",
        "arm_summary": arm_summary,
        "tasks_per_repository": dict(Counter(repositories.values())),
        "primary": primary,
        "missing_cells": [{"task_id": task, "arm": arm} for task, arm in missing],
        "sharp_planned20_bounds": bounds,
        "requested_sensitivity_scenarios": scenarios,
        "sensitivity_note": "Hypothetical only; primary counts unchanged. Uniform all-fail/all-pass do not generally attain sharp bounds.",
        "judgment_sensitivity": {
            "source": "HAR-133 Research-Harbor 2026-10-01T17:48:36.331Z",
            "assumption": "Exclude 000169-tuned under the two blind G6 raters' copied-fix judgment.",
            "primary_unchanged": True,
            "contrasts": primary_family(judgment_cells),
        },
        "joint_missing_completions": completions,
        "exploratory_tuned_minus_gepa": contrast(cells, "tuned", "gepa"),
        "interpretation": "Neither primary test rejects. Non-rejection is not equivalence or evidence of no effect.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--input", type=Path, default=here / "har133_outcomes.csv")
    parser.add_argument("--output", type=Path, default=here / "har133_analysis.json")
    args = parser.parse_args()
    result = analyze(args.input)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"primary": result["primary"], "bounds": result["sharp_planned20_bounds"]}, indent=2
        )
    )


if __name__ == "__main__":
    main()
