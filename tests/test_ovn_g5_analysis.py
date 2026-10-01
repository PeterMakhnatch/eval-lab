"""Missing-outcome and multiplicity regressions for the closed G5 analysis."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/ovn-sft-v0/har133_analysis.py"
_spec = importlib.util.spec_from_file_location("ovn_g5_analysis", SCRIPT)
assert _spec is not None and _spec.loader is not None
_analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analysis)


def _input(path: Path, overrides: dict[tuple[int, str], int | None]) -> Path:
    rows = []
    for task in [*range(1, 20), 169]:
        for arm in ("stock", "tuned", "gepa"):
            value = overrides.get((task, arm), 0)
            rows.append(
                {
                    "task_id": f"format-code-task-{task:06d}",
                    "repository": f"project-{task}",
                    "arm": arm,
                    "counts_verdict": (
                        "excluded" if value is None else "counted_pass" if value else "counted_fail"
                    ),
                    "outcome": "" if value is None else str(value),
                    "raw_reward": "" if value is None else str(value),
                }
            )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_mixed_missing_assignments_bound_what_uniform_imputation_misses(tmp_path: Path) -> None:
    path = _input(
        tmp_path / "outcomes.csv",
        {
            (1, "stock"): None,
            (2, "tuned"): None,
            (3, "gepa"): None,
            (3, "stock"): 1,
            (3, "tuned"): 1,
            (169, "tuned"): 1,
        },
    )
    result = _analysis.analyze(path)
    tuned = result["primary"]["tuned_minus_stock"]
    gepa = result["primary"]["gepa_minus_stock"]
    assert tuned["n_pairs"] == gepa["n_pairs"] == 18
    assert "format-code-task-000003" in tuned["complete_tasks"]
    assert "format-code-task-000002" in gepa["complete_tasks"]

    bounds = result["sharp_planned20_bounds"]["tuned_minus_stock"]
    assert (bounds["risk_difference_lower"], bounds["risk_difference_upper"]) == (0.0, 0.1)
    for scenario in ("all_missing_fail", "all_missing_pass"):
        assert (
            result["requested_sensitivity_scenarios"][scenario]["tuned_minus_stock"][
                "risk_difference"
            ]
            == 0.05
        )

    # A copied-pass judgment excludes the pair, never turns the pass into a fail
    # or mutates the canonical primary table.
    judgment = result["judgment_sensitivity"]["contrasts"]["tuned_minus_stock"]
    assert (judgment["n_pairs"], judgment["arm_a_passes"], judgment["arm_b_passes"]) == (17, 1, 1)
    assert (tuned["n_pairs"], tuned["arm_a_passes"], tuned["arm_b_passes"]) == (18, 2, 1)


def test_six_one_way_discordances_do_not_clear_the_frozen_two_test_family(tmp_path: Path) -> None:
    path = _input(tmp_path / "outcomes.csv", {(task, "tuned"): 1 for task in range(1, 7)})
    result = _analysis.analyze(path)
    tuned = result["primary"]["tuned_minus_stock"]
    assert (tuned["discordant_a_only"], tuned["discordant_b_only"]) == (6, 0)
    assert tuned["exact_p_value"] == 0.03125
    assert tuned["holm_adjusted_p_value"] == 0.0625
    assert tuned["reject_at_family_alpha_0_05"] is False
    assert result["primary"]["gepa_minus_stock"]["exact_p_value"] == 1.0
