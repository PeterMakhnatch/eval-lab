from __future__ import annotations

import csv
import hashlib
import json
import runpy
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest


DRIVER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/repaired-data-sft-v2/analyze.py"
)


@pytest.fixture(scope="module")
def analysis() -> dict[str, Any]:
    return runpy.run_path(str(DRIVER))


def digest(task: str) -> str:
    return hashlib.sha256(task.encode()).hexdigest()


def result(
    task: str,
    attempt: int,
    outcome: bool | None,
    *,
    error: str | None = None,
    package_digest: str | None = None,
) -> dict[str, Any]:
    package = package_digest or digest(task)[:12]
    return {
        "task": f"/frozen/variants/mimo__{task}/{package}",
        "name": f"trial-{task}-{package}-a{attempt}",
        "reward": float(outcome) if outcome is not None else None,
        "passed": outcome,
        "error": error,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_case(
    tmp_path: Path,
    tasks: list[str],
    arms: dict[str, tuple[int, list[dict[str, Any]]]],
    *,
    cohort: list[str] | None = None,
    selected: list[int] | None = None,
    authorized_tasks: dict[str, list[str]] | None = None,
    pins: list[str] | None = None,
    replicates: int = 512,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    heldout = {"heldout": tasks, "scoring_allowed": True, "freeze_ready": True}
    (tmp_path / "heldout.json").write_text(json.dumps(heldout), encoding="utf-8")
    with (tmp_path / "package-pairs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["task_id", "v6_package_digest"])
        writer.writeheader()
        writer.writerows(
            {"task_id": task, "v6_package_digest": "sha256:" + digest(task)} for task in tasks
        )
    arm_plan = {}
    for name, (attempts, rows) in arms.items():
        path = tmp_path / f"{name}.jsonl"
        write_jsonl(path, rows)
        arm_plan[name] = {"results": path.name, "authorized_attempts": attempts}
        if authorized_tasks and name in authorized_tasks:
            arm_plan[name]["authorized_task_ids"] = authorized_tasks[name]
    plan: dict[str, Any] = {
        "heldout_manifest": "heldout.json",
        "arms": arm_plan,
        "bootstrap_replicates": replicates,
        "seed": 2718,
    }
    if cohort is not None:
        plan["cohort_task_ids"] = cohort
    if selected is not None:
        plan["selected_attempts"] = selected
    if pins is not None:
        plan["pin_task_ids"] = pins
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_attempt1_primary_and_full_schedule_secondary_are_different(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["t"], {
        "B": (2, [result("t", 1, True), result("t", 2, False)]),
        "C": (1, [result("t", 1, False)]),
    })
    report = analysis["analyze_plan"](path)
    primary = report["paired"]["B_vs_C"]
    secondary = report["secondary"]["paired_full_schedule"]["B_vs_C"]
    assert primary["role"] == "primary"
    assert primary["matched_attempt_ids"] == [1]
    assert primary["attempts_counted"] == {"B": 1, "C": 1}
    assert primary["mean_difference"] == 1
    assert primary["mean_difference_fraction"] == "1"
    assert primary["arms"]["B"]["solve_fraction"] == "1"
    assert primary["task_wins"] == 1
    assert primary["task_losses"] == primary["tied_tasks"] == 0
    assert secondary["role"] == "secondary"
    assert secondary["attempts_counted"] == {"B": 2, "C": 1}
    assert secondary["matched_attempt_ids"] is None
    assert secondary["mean_difference"] == 0.5
    assert secondary["mean_difference_fraction"] == "1/2"
    assert secondary["arms"]["B"]["solve_fraction"] == "1/2"
    assert report["arms"]["B"]["raw"]["solve_rate"] == 0.5
    assert report["arms"]["B"]["raw"]["role"] == "secondary_full_authorized_schedule"
    assert report["arms"]["B"]["view"]["solve_rate"] == 1


def test_unauthorized_raw_a2_refuses_even_outside_attempt1_cohort_view(
    tmp_path: Path, analysis: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_case(tmp_path, ["visible", "hidden"], {
        "A": (1, [result("visible", 1, True), result("hidden", 2, True)]),
        "C": (1, [result("visible", 1, False)]),
    }, cohort=["visible"], selected=[1])
    out = tmp_path / "report.json"
    assert analysis["main"](["--plan", str(path), "--out", str(out)]) == 2
    assert not out.exists()
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert "unauthorized" in stderr
    assert "hidden:a2" in stderr


@pytest.mark.parametrize("outside_view", [False, True])
def test_duplicate_original_raw_cells_cannot_be_hidden_by_view_or_dictionary_conversion(
    tmp_path: Path, analysis: dict[str, Any], outside_view: bool
) -> None:
    duplicate_task = "hidden" if outside_view else "visible"
    path = write_case(tmp_path, ["visible", "hidden"], {
        "B": (2, [
            result("visible", 1, True),
            result(duplicate_task, 2, True),
            result(duplicate_task, 2, False),
        ]),
        "C": (1, [result("visible", 1, False)]),
    }, cohort=["visible"])
    with pytest.raises(ValueError, match="duplicate"):
        analysis["analyze_plan"](path)


def test_per_arm_authorized_tasks_have_distinct_raw_denominators(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["accepted", "not-authorized-for-A"], {
        "A": (1, [result("accepted", 1, True)]),
        "B": (2, [
            result("accepted", 1, False), result("accepted", 2, True),
            result("not-authorized-for-A", 1, True),
            result("not-authorized-for-A", 2, False),
        ]),
    }, cohort=["accepted"], authorized_tasks={"A": ["accepted"]})
    report = analysis["analyze_plan"](path)
    assert report["heldout_tasks"] == 2
    assert report["cohort_tasks"] == 1
    assert report["arms"]["A"]["raw"]["planned_slots"] == 1
    assert report["arms"]["A"]["raw"]["solve_coverage"] == "complete"
    assert report["arms"]["A"]["raw"]["missing_rows"] == 0
    assert report["arms"]["B"]["raw"]["planned_slots"] == 4
    assert report["arms"]["B"]["raw"]["solve_coverage"] == "complete"
    assert report["all_arms_common"]["common_task_cohort"] == "1/1"
    # An extra row on the other heldout task is still unauthorized for A.
    write_jsonl(tmp_path / "A.jsonl", [
        result("accepted", 1, True), result("not-authorized-for-A", 1, False),
    ])
    with pytest.raises(ValueError, match="unauthorized"):
        analysis["analyze_plan"](path)


def test_every_selected_attempt_must_be_measured_in_both_arms(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["missing", "error", "complete"], {
        "B": (2, [
            result("missing", 1, True),
            result("error", 1, False, error="trial crashed"),
            result("error", 2, True),
            result("complete", 1, True), result("complete", 2, False),
        ]),
        "C": (2, [
            result(task, attempt, task == "complete" and attempt == 2)
            for task in ["missing", "error", "complete"] for attempt in [1, 2]
        ]),
    }, selected=[1, 2])
    report = analysis["analyze_plan"](path)
    primary = report["paired"]["B_vs_C"]
    assert primary["common_task_ids"] == ["complete"]
    assert primary["common_task_cohort"] == "1/3"
    assert primary["comparison_coverage"] == "observed_subset"
    assert primary["arms"]["B"]["measured_attempts"] == 2
    assert primary["arms"]["C"]["measured_attempts"] == 2
    assert primary["mean_difference"] == 0
    assert primary["tied_tasks"] == 1
    assert primary["exact_sign_fraction"] == "1"
    assert primary["mean_difference_ci95"] == [0, 0]
    raw = report["arms"]["B"]["raw"]
    assert raw["planned_slots"] == 6
    assert raw["measured_attempts"] == 4
    assert raw["missing_rows"] == 1
    assert raw["error_or_null_rows"] == 1
    assert raw["unmeasured_reasons"] == {"error": 1, "missing_row": 1}


def test_attempt2_never_replaces_a_missing_attempt1(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["t"], {
        "B": (2, [result("t", 2, True)]),
        "C": (1, [result("t", 1, False)]),
    })
    report = analysis["analyze_plan"](path)
    primary = report["paired"]["B_vs_C"]
    assert primary["status"] == "unmeasured"
    assert primary["common_task_ids"] == []
    assert primary["mean_difference"] is None
    assert primary["exact_sign_p"] is None
    assert primary["mean_difference_ci95"] is None
    assert primary["arms"]["B"]["solve_rate"] is None
    assert report["arms"]["B"]["view"]["solve_rate"] is None
    assert report["secondary"]["paired_full_schedule"]["B_vs_C"]["mean_difference"] == 1


@pytest.mark.parametrize("stale_passed", [True, False])
def test_null_rewards_and_errors_are_unmeasured_not_failures(
    tmp_path: Path, analysis: dict[str, Any], stale_passed: bool
) -> None:
    null = result("t", 1, None)
    null["passed"] = stale_passed
    path = write_case(tmp_path, ["t"], {
        "B": (1, [null]),
        "C": (1, [result("t", 1, False, error="transport error")]),
    })
    report = analysis["analyze_plan"](path)
    for arm in ["B", "C"]:
        assert report["arms"][arm]["raw"]["solve_coverage"] == "unmeasured"
        assert report["arms"][arm]["raw"]["solve_rate"] is None
        assert report["arms"][arm]["raw"]["solve_fraction"] is None
        assert report["arms"][arm]["raw"]["measured_attempts"] == 0
    primary = report["paired"]["B_vs_C"]
    assert primary["mean_difference_fraction"] is None
    assert primary["exact_sign_fraction"] is None
    assert primary["bootstrap"]["ci95"] is None
    assert report["all_arms_common"]["comparison_coverage"] == "unmeasured"


def test_noncontiguous_selected_attempt_ids_share_the_same_primary_estimand(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["t"], {
        "B-v2": (3, [result("t", 1, True), result("t", 2, True), result("t", 3, False)]),
        "C-v2": (3, [result("t", 1, False), result("t", 2, True), result("t", 3, False)]),
        "D-v2": (3, [result("t", 1, True), result("t", 2, False), result("t", 3, True)]),
    }, selected=[3, 1])
    report = analysis["analyze_plan"](path)
    assert report["selected_attempts"] == [1, 3]
    assert len(report["paired"]) == 3
    for pair in report["paired"].values():
        assert pair["matched_attempt_ids"] == [1, 3]
        assert set(pair["attempts_counted"].values()) == {2}
    pair = report["paired"]["B-v2_vs_C-v2"]
    assert pair["mean_difference_fraction"] == "1/2"
    assert pair["arms"]["B-v2"]["solve_fraction"] == "1/2"
    assert pair["arms"]["C-v2"]["solve_fraction"] == "0"


def test_bootstrap_resamples_tasks_not_iid_attempts_or_pseudo_replicates(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    outcomes = {"t1": [True, False], "t2": [True, True]}
    paths = []
    for copies in [1, 2]:
        attempts = 2 * copies
        paths.append(write_case(tmp_path / str(copies), ["t1", "t2"], {
            "B": (attempts, [
                result(task, attempt, values[(attempt - 1) % 2])
                for task, values in outcomes.items() for attempt in range(1, attempts + 1)
            ]),
            "C": (attempts, [
                result(task, attempt, False)
                for task in outcomes for attempt in range(1, attempts + 1)
            ]),
        }, selected=list(range(1, attempts + 1)), replicates=4096))
    first, duplicated = [analysis["analyze_plan"](path)["paired"]["B_vs_C"] for path in paths]
    assert first["mean_difference_fraction"] == "3/4"
    assert first["mean_difference_ci95"] == [0.5, 1]
    assert first["bootstrap"]["independent_units"] == 2
    assert first["bootstrap"]["resampling_unit"] == "task"
    assert first["bootstrap"] == duplicated["bootstrap"]
    assert first["arms"]["B"]["measured_attempts"] == 4
    assert duplicated["arms"]["B"]["measured_attempts"] == 8


def test_bootstrap_is_deterministic_and_exact_at_degenerate_edges(
    analysis: dict[str, Any]
) -> None:
    bootstrap = analysis["paired_task_bootstrap"]
    differences = [Fraction(-2, 3), Fraction(1, 3), Fraction(1)]
    assert bootstrap(differences, 1001, 17) == bootstrap(differences, 1001, 17)
    degenerate = bootstrap([Fraction(1, 3)] * 7, 513, 19)
    assert degenerate["ci95"] == [1 / 3, 1 / 3]
    assert degenerate["ci95_fraction"] == ["1/3", "1/3"]
    assert bootstrap([Fraction(0)], 1, 0)["ci95"] == [0, 0]
    assert bootstrap([], 20, 0)["ci95"] is None


def test_exact_arm_mean_and_sign_fractions_are_not_rounded(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    tasks = [f"t{i}" for i in range(6)]
    path = write_case(tmp_path, tasks, {
        "B": (1, [result(task, 1, i < 5) for i, task in enumerate(tasks)]),
        "C": (1, [result(task, 1, i == 0) for i, task in enumerate(tasks)]),
    })
    pair = analysis["analyze_plan"](path)["paired"]["B_vs_C"]
    assert pair["arms"]["B"]["solve_fraction"] == "5/6"
    assert pair["arms"]["C"]["solve_fraction"] == "1/6"
    assert pair["mean_difference_fraction"] == "2/3"
    assert pair["task_wins"] == 4
    assert pair["task_losses"] == 0
    assert pair["tied_tasks"] == 2
    assert pair["exact_sign_fraction"] == "1/8"
    assert pair["exact_sign_p"] == 0.125


def test_tiny_exact_sign_p_is_not_rounded_to_zero(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    tasks = [f"t{i:03}" for i in range(70)]
    path = write_case(tmp_path, tasks, {
        "B": (1, [result(task, 1, True) for task in tasks]),
        "C": (1, [result(task, 1, False) for task in tasks]),
    }, replicates=2)
    pair = analysis["analyze_plan"](path)["paired"]["B_vs_C"]
    assert pair["exact_sign_fraction"] == str(Fraction(1, 2**69))
    assert pair["exact_sign_p"] > 0
    assert pair["mean_difference_ci95"] == [1, 1]


def test_four_way_shared_coverage_does_not_silently_shrink_to_complete(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["t1", "t2"], {
        "Base": (1, [result("t1", 1, False)]),
        "A": (1, [result("t1", 1, True), result("t2", 1, None)]),
        "B": (2, [
            result("t1", 1, True), result("t1", 2, True),
            result("t2", 1, True), result("t2", 2, False),
        ]),
        "C": (1, [result("t1", 1, False), result("t2", 1, True)]),
    })
    report = analysis["analyze_plan"](path)
    assert len(report["paired"]) == 6
    assert report["paired"]["B_vs_C"]["comparison_coverage"] == "complete"
    assert report["paired"]["B_vs_C"]["common_task_ids"] == ["t1", "t2"]
    shared = report["all_arms_common"]
    assert shared["common_task_ids"] == ["t1"]
    assert shared["requested_task_ids"] == ["t1", "t2"]
    assert shared["common_task_cohort"] == "1/2"
    assert shared["comparison_coverage"] == "observed_subset"
    assert shared["missing_or_unpaired_task_ids"] == ["t2"]
    assert len(shared["table"]) == 2
    assert shared["table"][1]["arm_solve_fractions"]["Base"] is None
    assert shared["table"][1]["arm_solve_fractions"]["A"] is None
    assert shared["arms"]["B"]["measured_attempts"] == 1


def test_budget_skipped_pin_variants_are_separately_validated_not_headline_failures(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    skipped = [
        result("pin", attempt, None, error="skipped_by_budget: never launched", package_digest="abcdef123456")
        for attempt in [1, 2]
    ]
    path = write_case(tmp_path, ["pin"], {
        "B": (2, [result("pin", 1, True), result("pin", 2, False), *skipped]),
        "C": (1, [result("pin", 1, False)]),
    }, pins=["pin"])
    report = analysis["analyze_plan"](path)
    assert report["paired"]["B_vs_C"]["mean_difference"] == 1
    raw = report["arms"]["B"]["raw"]
    assert raw["present_rows"] == raw["measured_attempts"] == 2
    assert raw["solve_fraction"] == "1/2"
    diagnostic = report["arms"]["B"]["diagnostic"]
    assert diagnostic["role"] == "diagnostic_only_never_headline"
    assert diagnostic["skipped_by_budget_rows"] == 2
    assert diagnostic["present_rows"] == 2
    assert diagnostic["solve_rate"] is None
    assert diagnostic["measured_attempts"] == 0
    assert diagnostic["unmeasured_reasons"] == {"skipped_by_budget": 2}
    # A skipped marker is still a raw scheduled cell: a3 must refuse before view a1.
    write_jsonl(tmp_path / "B.jsonl", [
        result("pin", 1, True), result("pin", 2, False), *skipped,
        result("pin", 3, None, error="skipped_by_budget", package_digest="abcdef123456"),
    ])
    with pytest.raises(ValueError, match="unauthorized"):
        analysis["analyze_plan"](path)


def test_pin_variant_without_explicit_known_pin_cannot_enter_headline(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["pin"], {
        "B": (1, [result("pin", 1, True, package_digest="abcdef123456")]),
        "C": (1, [result("pin", 1, False)]),
    })
    with pytest.raises(ValueError, match="not the frozen v6"):
        analysis["analyze_plan"](path)


def test_optional_pin_verdicts_never_modify_headline_or_count_null_regrades(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["pin"], {
        "B": (1, [result("pin", 1, True)]),
        "C": (1, [result("pin", 1, False)]),
    }, pins=["pin"])
    original = analysis["analyze_plan"](path)
    manifest = tmp_path / "pin-manifest.csv"
    manifest.write_text("task_id,final_digest\npin,sha256:abcdef123456\n", encoding="utf-8")
    regrades = tmp_path / "pin-regrades.jsonl"
    write_jsonl(regrades, [{
        "arm": "B", "trial": result("pin", 1, True)["name"], "repaired_pass": None,
    }])
    plan = json.loads(path.read_text())
    plan.update({"paired_pin_regrades": regrades.name, "pin_manifest": manifest.name})
    path.write_text(json.dumps(plan), encoding="utf-8")
    report = analysis["analyze_plan"](path)
    assert report["paired"] == original["paired"]
    pins = report["paired_pins"]["arms"]["B"]
    assert pins["v6_pin_passes"] == 1
    assert pins["regrade_measured"] == 0
    assert pins["unmeasured_pin_passes"] == 1
    assert pins["pin_incorrect_pass_rate"] is None
    assert pins["pin_incorrect_pass_fraction"] is None
    # A verdict for a non-passing source/control cannot enter a paired-pass denominator.
    write_jsonl(regrades, [{
        "arm": "C", "trial": result("pin", 1, False)["name"], "repaired_pass": True,
    }])
    with pytest.raises(ValueError, match="not from a measured frozen pin pass"):
        analysis["analyze_plan"](path)


def test_frozen_heldout_hash_mismatch_refuses_without_report(
    tmp_path: Path, analysis: dict[str, Any]
) -> None:
    path = write_case(tmp_path, ["t"], {
        "B": (1, [result("t", 1, True)]), "C": (1, [result("t", 1, False)]),
    })
    plan = json.loads(path.read_text())
    plan["heldout_sha256"] = "0" * 64
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="heldout_sha256"):
        analysis["analyze_plan"](path)
