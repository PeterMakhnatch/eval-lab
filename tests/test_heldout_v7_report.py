from __future__ import annotations

import hashlib
import json
import runpy
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

DRIVER = Path(__file__).resolve().parents[1] / "research/experiments/heldout-v7/report.py"
TASKS = [f"mimo-v2.6-rl/format-code-task-{index:06d}" for index in range(1, 34)]
T1, T2, T3, T4 = TASKS[:4]
SCHEDULE = {"base": [1], "A": [1], "B": [1, 2], "C": [1]}
ADMISSION = {
    "scoring_allowed": True,
    "manifest": {"path": "/admitted/heldout-v7.json", "sha256": "sha256:" + "a" * 64},
    "verifier": {"verdict": "PASS", "receipt": "/admitted/verifier-receipt.json"},
    "frozen_cohort_binding": "independently-verified-by-caller",
}


@pytest.fixture(scope="module")
def build_report():
    return runpy.run_path(str(DRIVER))["build_report"]


def source(
    arm: str,
    task: str = T1,
    attempt: int = 1,
    original: bool | None = True,
    *,
    error: str | None = None,
    population: str = "official",
    kept_diff: bool = True,
    suffix: str = "",
) -> dict[str, Any]:
    job = f"native-job-{arm}-{population}"
    trial = f"native-trial-{task.rsplit('-', 1)[-1]}-a{attempt}{suffix}"
    directory = f"/retained/{job}/{trial}"
    return {
        "source_uid": [job, trial],
        "source_trial_dir": directory,
        "arm": arm,
        "task_id": task,
        "attempt_index": attempt,
        "population": population,
        "v6_outcome": original,
        "v6_error": error,
        "diff": {
            "path": directory + "/agent/agent.kept.diff",
            "sha256": "sha256:" + hashlib.sha256(b"retained patch\n").hexdigest(),
        } if kept_diff else None,
        "source_digest": hashlib.sha256(directory.encode()).hexdigest(),
        "aliases": [{"path": directory + "/alias.jsonl", "line": 7}],
        "native_raw_ref": {"path": directory + "/result.json", "sha256": "sha256:" + "b" * 64},
        "canonical_source_ref": {"path": "/canonical/corrected-v6.jsonl", "line": 11},
    }


def observation(
    row: dict[str, Any],
    outcome: bool | None,
    *,
    error: str | None = None,
    replayed: bool = True,
) -> dict[str, Any]:
    path = "/owned/regrades/" + row["source_uid"][1]
    return {
        "source_uid": row["source_uid"].copy(),
        "v7_outcome": outcome,
        "v7_error": error,
        "replayed": replayed,
        "native_evidence": {
            "job_id": "new-native-job-" + row["arm"],
            "job_dir": path,
            "result": {"path": path + "/result.json", "sha256": "sha256:" + "c" * 64},
            "receipt": {"path": path + "/receipt.json", "sha256": "sha256:" + "d" * 64},
            "submitted_diff": deepcopy(row["diff"]),
            "source_digest": row["source_digest"],
            "comparison_label": "refused" if row["v6_outcome"] is None else "matched",
        },
    }


def inventory(rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema": "heldout-v7.inventory/v1",
        "frozen_task_ids": TASKS.copy(),
        "authorized_attempts": deepcopy(SCHEDULE),
        "sources": [{"path": "/canonical/source-inventory.json", "sha256": "sha256:" + "e" * 64}],
        "rows": rows,
        **extra,
    }


def complete_task(task: str = T1) -> list[dict[str, Any]]:
    return [source(arm, task) for arm in ("base", "A", "B", "C")]


def test_attempt1_primary_counterexample_is_not_pooled_B2(build_report) -> None:
    rows = complete_task() + [source("B", attempt=2)]
    values = {"base": False, "A": True, "C": False}
    observations = [
        observation(row, row["attempt_index"] == 1 if row["arm"] == "B" else values[row["arm"]])
        for row in rows
    ]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=257, seed=17)
    primary = report["primary"]["paired"]["B_vs_C"]
    secondary = report["secondary"]["v7"]["paired_full_schedule"]["B_vs_C"]
    assert primary["role"] == "primary"
    assert primary["matched_attempt_ids"] == [1]
    assert primary["attempts_counted"] == {"B": 1, "C": 1}
    assert primary["mean_difference_fraction"] == "1"
    assert primary["mean_difference"] == 1
    assert primary["mean_difference_ci95"] == [1, 1]
    assert primary["arms"]["B"]["solve_fraction"] == "1"
    assert secondary["comparison"]["role"] == "secondary"
    assert secondary["cohort"]["common_task_cohort"] == "1/33"
    assert secondary["comparison"]["attempt_ids_by_arm"] == {"B": [1, 2], "C": [1]}
    assert secondary["comparison"]["matched_attempt_ids"] is None
    assert secondary["comparison"]["mean_difference_fraction"] == "1/2"
    assert secondary["comparison"]["mean_difference"] == 0.5
    assert report["secondary"]["v7"]["B_full_two_attempts"]["task_mean_fraction"] == "1/2"
    assert report["arms"]["B"]["v7"]["passes"] == 1
    assert report["arms"]["B"]["v7"]["denominator"] == 2
    assert report["primary"]["cohort"]["requested_task_ids"] == TASKS
    assert report["primary"]["cohort"]["excluded_task_ids"] == TASKS[1:]


def test_every_primary_comparison_uses_identical_three_arm_cohort_despite_missingness(
    build_report,
) -> None:
    rows = [source(arm, task) for task in TASKS[:4] for arm in ("base", "B", "C")]
    rows += [source("B", T4, 2), source("A", T2)]
    observations = []
    errors = {("C", T2): "C native timeout", ("base", T3): "base native timeout", ("B", T4): "B1 missing native result"}
    for row in rows:
        error = errors.get((row["arm"], row["task_id"])) if row["attempt_index"] == 1 else None
        observations.append(observation(row, True, error=error))
    report = build_report(inventory(rows), observations, ADMISSION, replicates=31, seed=2)
    cohort = report["primary"]["cohort"]
    assert cohort["common_task_ids"] == [T1]
    assert cohort["common_task_cohort"] == "1/33"
    assert cohort["comparison_coverage"] == "observed_subset"
    assert T2 in cohort["missing_task_ids_by_measurement"]["C"]
    assert T3 in cohort["missing_task_ids_by_measurement"]["base"]
    assert T4 in cohort["missing_task_ids_by_measurement"]["B"]
    assert set(report["primary"]["paired"]) == {"B_vs_C", "B_vs_base", "C_vs_base"}
    for pair in report["primary"]["paired"].values():
        assert pair["common_task_ids"] == pair["requested_task_ids"] == [T1]
        assert pair["n"] == pair["tasks"] == 1
        assert pair["cohort_name"] == cohort["name"]
        assert pair["matched_attempt_ids"] == [1]
        assert pair["bootstrap"]["independent_units"] == 1
    assert report["four_arm_common"]["cohort"]["common_task_ids"] == []
    assert report["arms"]["B"]["v7"]["per_task"][T4]["measured_attempt_ids"] == [2]
    assert report["arms"]["B"]["attempt1"]["v7"]["per_task"][T4]["measured_attempt_ids"] == []


def test_four_way_and_cross_version_cohorts_are_separate_fixed_populations(build_report) -> None:
    rows = []
    for task in TASKS[:3]:
        for arm in ("base", "A", "B", "C"):
            old_null = (arm, task) in {("base", T2), ("B", T3)}
            rows.append(source(arm, task, original=None if old_null else True, error="original native error" if old_null else None))
    observations = [observation(row, row["arm"] == "B", error="A replay error" if row["arm"] == "A" and row["task_id"] == T3 else None) for row in rows]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=31, seed=3)
    assert report["primary"]["cohort"]["common_task_ids"] == TASKS[:3]
    assert report["four_arm_common"]["cohort"]["common_task_ids"] == TASKS[:2]
    assert set(report["four_arm_common"]["arms"]) == {"base", "A", "B", "C"}
    cross = report["cross_version_common"]
    assert cross["cohort"]["common_task_ids"] == [T1]
    assert cross["cohort"]["excluded_task_ids"] == TASKS[1:]
    assert cross["cohort"]["requested_tasks"] == 33
    for version in ("v6", "v7"):
        assert cross["versions"][version]["cohort"]["common_task_ids"] == [T1]
        for pair in cross["versions"][version]["paired"].values():
            assert pair["common_task_ids"] == [T1]
    for pair in cross["v7_vs_v6"].values():
        assert pair["common_task_ids"] == [T1]
        assert pair["n"] == 1
    assert cross["v7_vs_v6"]["base"]["mean_difference_fraction"] == "-1"
    assert cross["v7_vs_v6"]["B"]["mean_difference_fraction"] == "0"
    assert report["primary"]["paired"]["B_vs_C"]["n"] == 3


@pytest.mark.parametrize("arm,attempt", [("base", 2), ("A", 2), ("B", 3), ("C", 2)])
def test_unauthorized_raw_null_or_error_cell_refuses_before_primary_filtering(
    build_report, arm: str, attempt: int,
) -> None:
    rows = complete_task() + [source(arm, T2, attempt, None, error="old failure", kept_diff=False)]
    with pytest.raises(ValueError, match="unauthorized official cell"):
        build_report(inventory(rows), [], ADMISSION, replicates=1)


def test_unknown_raw_frozen_task_refuses_even_when_unmeasured(build_report) -> None:
    rows = complete_task() + [source("B", "mimo-v2.6-rl/format-code-task-999999", 2, None, error="old failure", kept_diff=False)]
    with pytest.raises(ValueError, match="unknown frozen task"):
        build_report(inventory(rows), [], ADMISSION, replicates=1)


def test_duplicate_raw_B2_cells_cannot_hide_behind_nulls_or_attempt1_view(build_report) -> None:
    rows = complete_task() + [
        source("B", T2, 2, None, error="old timeout", kept_diff=False),
        source("B", T2, 2, None, error="another old timeout", kept_diff=False, suffix="-other-native-trial"),
    ]
    with pytest.raises(ValueError, match="duplicate official cell"):
        build_report(inventory(rows), [], ADMISSION, replicates=1)


def test_duplicate_source_uid_across_populations_is_not_silently_collapsed(build_report) -> None:
    rows = complete_task()
    duplicate = deepcopy(rows[0])
    duplicate.update({"population": "quarantined", "task_id": T2, "v6_outcome": None, "v6_error": "quarantine"})
    with pytest.raises(ValueError, match="duplicate source UID"):
        build_report(inventory(rows + [duplicate]), [], ADMISSION, replicates=1)


def test_duplicate_and_foreign_observations_refuse_even_with_errors(build_report) -> None:
    rows = complete_task()
    observed = observation(rows[0], True, error="stale reward after replay crash")
    with pytest.raises(ValueError, match="duplicate observation UID"):
        build_report(inventory(rows), [observed, deepcopy(observed)], ADMISSION, replicates=1)
    foreign = deepcopy(observed)
    foreign["source_uid"] = ["unrecorded-native-job", "unrecorded-native-trial"]
    with pytest.raises(ValueError, match="foreign observation UID"):
        build_report(inventory(rows), [foreign], ADMISSION, replicates=1)


@pytest.mark.parametrize("original", [None, True])
def test_original_null_or_stale_error_does_not_discard_independent_new_zero(
    build_report, original: bool | None,
) -> None:
    rows = [source("base"), source("B", original=original, error="original native timeout"), source("C", original=False)]
    observations = [observation(row, row["arm"] == "C") for row in rows]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=13, seed=5)
    arm = report["arms"]["B"]
    assert arm["v6"]["denominator"] == 0
    assert arm["v6"]["solve_rate"] is None
    assert arm["v6"]["unmeasured_reasons"]["original native timeout"] == 1
    assert arm["v7"]["denominator"] == 1
    assert arm["v7"]["passes"] == 0
    assert arm["v7"]["solve_rate"] == 0
    assert arm["v7"]["solve_fraction"] == "0"
    assert arm["dual_measured"]["n"] == 0
    assert report["primary"]["cohort"]["common_task_ids"] == [T1]
    assert report["cross_version_common"]["cohort"]["common_task_ids"] == []
    assert {label["arm"] for label in report["per_pass_labels"]} == {"base"}
    record = next(row for row in report["source_results"] if row["arm"] == "B")
    assert record["v6_outcome"] is None
    assert record["v7_outcome"] is False
    assert record["native_evidence"] == observations[1]["native_evidence"]


def test_new_error_blocks_stale_boolean_but_real_zero_is_measured(build_report) -> None:
    rows = [source(arm) for arm in ("base", "B", "C")]
    observations = [observation(rows[0], True), observation(rows[1], True, error="new native transport error"), observation(rows[2], False)]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=13)
    labels = {row["arm"]: row for row in report["per_pass_labels"]}
    assert labels["base"]["label"] == "real_pass"
    assert labels["B"]["label"] == "unmeasured"
    assert labels["B"]["v7_outcome"] is None
    assert labels["B"]["reason"] == "new native transport error"
    assert labels["C"]["label"] == "wrong_pass"
    assert labels["C"]["v7_outcome"] is False
    assert report["arms"]["B"]["v7"]["denominator"] == 0
    assert report["arms"]["B"]["v7"]["solve_rate"] is None
    assert report["arms"]["C"]["v7"]["denominator"] == 1
    assert report["arms"]["C"]["v7"]["solve_rate"] == 0
    assert report["arms"]["B"]["coverage"]["replayed_native_sources"] == 1
    assert report["arms"]["B"]["coverage"]["qualified_v7_sources"] == 0
    assert report["primary"]["paired"]["B_vs_C"]["status"] == "unmeasured"


def test_null_new_native_grade_remains_unmeasured_without_becoming_zero(build_report) -> None:
    row = source("B")
    observed = observation(row, None)
    report = build_report(inventory([row]), [observed], ADMISSION, replicates=1)
    assert report["arms"]["B"]["v6"]["denominator"] == 1
    assert report["arms"]["B"]["v7"]["denominator"] == 0
    assert report["arms"]["B"]["v7"]["solve_fraction"] is None
    assert report["arms"]["B"]["v7"]["unmeasured_reasons"]["null_v7_outcome"] == 1
    assert report["arms"]["B"]["coverage"]["replayed_native_sources"] == 1
    assert report["arms"]["B"]["coverage"]["qualified_v7_sources"] == 0
    assert report["per_pass_labels"][0]["label"] == "unmeasured"
    assert report["per_pass_labels"][0]["reason"] == "null_v7_outcome"


def test_all_original_passes_get_labels_including_unreplayed_and_nonofficial_sources(
    build_report,
) -> None:
    rows = [
        source("base", original=False),
        source("B"),
        source("B", attempt=2),
        source("C", kept_diff=False),
        source("A"),
        source("A", T2, population="diagnostic"),
        source("C", "mimo-v2.6-rl/format-code-task-999999", population="quarantined", kept_diff=False),
        source("base", T3, 2, population="supplementary"),
        source("B", T2, original=None),
        source("C", T2, original=False),
    ]
    observations = [observation(rows[index], value) for index, value in [(0, False), (1, True), (2, False), (5, False), (7, True), (8, True), (9, False)]]
    inv = inventory(rows)
    admission = deepcopy(ADMISSION)
    original_inputs = deepcopy((inv, observations, admission))
    report = build_report(inv, observations, admission, replicates=13)
    labels = {tuple(row["source_uid"]): row for row in report["per_pass_labels"]}
    assert len(report["source_results"]) == len(rows)
    assert len(labels) == 7
    assert labels[tuple(rows[1]["source_uid"])]["label"] == "real_pass"
    assert labels[tuple(rows[2]["source_uid"])]["label"] == "wrong_pass"
    assert labels[tuple(rows[2]["source_uid"])]["primary_exclusion_reason"] == "not_attempt1"
    assert labels[tuple(rows[3]["source_uid"])]["reason"] == "missing_retained_diff"
    assert labels[tuple(rows[4]["source_uid"])]["reason"] == "missing_observation"
    assert labels[tuple(rows[5]["source_uid"])]["label"] == "wrong_pass"
    assert labels[tuple(rows[6]["source_uid"])]["label"] == "unmeasured"
    for row in rows[5:8]:
        label = labels[tuple(row["source_uid"])]
        assert label["excluded_from_primary"] is True
        assert label["primary_exclusion_reason"] == "non_official_population"
        assert label["source"] == row
        assert label["source_trial_dir"] == row["source_trial_dir"]
    for row in (rows[0], rows[8], rows[9]):
        assert tuple(row["source_uid"]) not in labels
    assert report["inventory"] == inv
    assert report["admission"] == admission
    assert (inv, observations, admission) == original_inputs
    assert json.loads(json.dumps(report))["schema"] == "heldout-v7.report/v1"
    measured_label = labels[tuple(rows[1]["source_uid"])]
    assert measured_label["native_evidence"] == observations[1]["native_evidence"]
    assert measured_label["source"]["diff"]["sha256"] == rows[1]["diff"]["sha256"]
    report["admission"]["verifier"]["verdict"] = "changed-copy"
    report["inventory"]["rows"][0]["aliases"].clear()
    assert (inv, observations, admission) == original_inputs


def test_same_dual_measured_rates_and_exact_pass_to_fail_flips(build_report) -> None:
    rows = [source("B", T1), source("B", T1, 2, False), source("B", T2), source("B", T2, 2)]
    observations = [observation(rows[0], False), observation(rows[1], True), observation(rows[2], True)]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=7)
    dual = report["arms"]["B"]["dual_measured"]
    assert dual["n"] == 3
    assert dual["v6"]["passes"] == dual["v7"]["passes"] == 2
    assert dual["v6"]["fraction"] == dual["v7"]["fraction"] == "2/3"
    assert dual["transitions"] == {"0->0": 0, "0->1": 1, "1->0": 1, "1->1": 1}
    assert dual["pass_to_fail"] == 1
    assert dual["dual_measured_original_passes"] == 2
    assert dual["pass_to_fail_fraction"] == "1/2"
    assert dual["pass_to_fail_item_ids"] == [rows[0]["source_uid"][0] + "/" + rows[0]["source_uid"][1]]
    assert report["arms"]["B"]["v6"]["denominator"] == 4
    assert report["arms"]["B"]["v7"]["denominator"] == 3


def test_measured_all_ties_are_not_confused_with_empty_cohorts(build_report) -> None:
    rows = complete_task()
    measured = build_report(inventory(rows), [observation(row, False) for row in rows], ADMISSION, replicates=1, seed=0)
    empty = build_report(inventory(rows), [], ADMISSION, replicates=1, seed=0)
    for pair in measured["primary"]["paired"].values():
        assert pair["status"] == "measured"
        assert pair["n"] == pair["tied_tasks"] == 1
        assert pair["mean_difference_fraction"] == "0"
        assert pair["exact_sign_fraction"] == "1"
        assert pair["exact_sign_p"] == 1
        assert pair["mean_difference_ci95"] == [0, 0]
        assert pair["bootstrap"]["ci95_fraction"] == ["0", "0"]
    for pair in empty["primary"]["paired"].values():
        assert pair["status"] == "unmeasured"
        assert pair["n"] == 0
        assert pair["mean_difference"] is None
        assert pair["mean_difference_fraction"] is None
        assert pair["exact_sign_p"] is None
        assert pair["exact_sign_fraction"] is None
        assert pair["mean_difference_ci95"] is None
        assert pair["bootstrap"]["ci95_fraction"] is None
        assert pair["arms"][pair["left"]]["solve_rate"] is None
    assert empty["primary"]["cohort"]["common_task_cohort"] == "0/33"
    assert empty["primary"]["cohort"]["comparison_coverage"] == "unmeasured"


def test_seeded_task_bootstrap_and_exact_sign_remain_exact_at_boundary(build_report) -> None:
    rows = [source(arm, task) for task in TASKS for arm in ("base", "B", "C")]
    observations = [observation(row, row["arm"] == "B") for row in rows]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=1, seed=0)
    pair = report["primary"]["paired"]["B_vs_C"]
    assert pair["n"] == 33
    assert pair["mean_difference_fraction"] == "1"
    assert pair["exact_sign_fraction"] == str(Fraction(1, 2**32))
    assert pair["exact_sign_p"] > 0
    assert pair["bootstrap"]["ci95_fraction"] == ["1", "1"]
    assert pair["bootstrap"]["replicates"] == 1
    assert pair["bootstrap"]["seed"] == 0
    assert pair["bootstrap"]["independent_units"] == 33
    assert pair["bootstrap"]["resampling_unit"] == "task"
    records = {row["item_id"]: row for row in report["source_results"]}
    for task, values in pair["per_task"].items():
        for arm, item_ids in values["source_item_ids_by_arm"].items():
            assert len(item_ids) == 1
            assert records[item_ids[0]]["task_id"] == task
            assert records[item_ids[0]]["arm"] == arm
            assert records[item_ids[0]]["native_evidence"] is not None


def test_nonconstant_bootstrap_is_deterministic_for_same_seed(build_report) -> None:
    rows = [source(arm, task) for task in TASKS[:3] for arm in ("base", "B", "C")]
    values = {T1: {"B": True, "C": False}, T2: {"B": False, "C": True}, T3: {"B": True, "C": True}}
    observations = [observation(row, values[row["task_id"]].get(row["arm"], False)) for row in rows]
    first = build_report(inventory(rows), observations, ADMISSION, replicates=257, seed=29)
    second = build_report(inventory(rows), observations, ADMISSION, replicates=257, seed=29)
    pair = first["primary"]["paired"]["B_vs_C"]
    assert pair["mean_difference_fraction"] == "0"
    assert pair["n"] == 3
    assert pair["task_wins"] == pair["task_losses"] == pair["tied_tasks"] == 1
    assert pair["bootstrap"] == second["primary"]["paired"]["B_vs_C"]["bootstrap"]
    assert pair["bootstrap"]["independent_units"] == 3
    assert pair["bootstrap"]["ci95_fraction"] is not None


def test_full_B2_secondary_requires_both_attempts_and_names_completeness(build_report) -> None:
    rows = [source(arm, task) for task in TASKS[:2] for arm in ("base", "B", "C")]
    rows += [source("B", T1, 2), source("B", T2, 2)]
    observations = [observation(row, row["arm"] == "B" and row["task_id"] == T2 and row["attempt_index"] == 1) for row in rows if not (row["task_id"] == T1 and row["attempt_index"] == 2)]
    report = build_report(inventory(rows), observations, ADMISSION, replicates=31)
    assert report["primary"]["cohort"]["common_task_ids"] == TASKS[:2]
    full = report["secondary"]["v7"]["B_full_two_attempts"]
    assert full["cohort"]["common_task_ids"] == [T2]
    assert T1 in full["cohort"]["excluded_task_ids"]
    assert full["task_mean_fraction"] == "1/2"
    assert full["requested_coverage"]["observed_task_mean_fraction"] == "1/4"
    pair = report["secondary"]["v7"]["paired_full_schedule"]["B_vs_C"]
    assert pair["cohort"]["common_task_ids"] == [T2]
    assert pair["comparison"]["common_task_ids"] == [T2]
    assert pair["comparison"]["n"] == 1
    assert pair["comparison"]["mean_difference_fraction"] == "1/2"
    assert pair["comparison"]["require_every_selected_attempt"] is True


def test_native_coverage_keeps_requested33_distinct_from_historical26_and_missing_trials(
    build_report,
) -> None:
    rows = []
    for arm, count, kept, measured in [("base", 26, 26, 25), ("A", 19, 10, 9), ("C", 33, 28, 26)]:
        rows.extend(source(arm, task, original=True if index < measured else None, error=None if index < measured else "canonical original error", kept_diff=index < kept) for index, task in enumerate(TASKS[:count]))
    for index, (task, attempt) in enumerate((task, attempt) for task in TASKS for attempt in (1, 2)):
        rows.append(source("B", task, attempt, True if index < 65 else None, error=None if index < 65 else "canonical original error"))
    canaries = [source("A", task, population="quarantined", kept_diff=index < 2) for index, task in enumerate(TASKS[:3])]
    non_native = [{"arm": "A", "task_id": task, "attempt_index": 1, "population": "official", "reason": "scheduled_native_trial_not_created", "canonical_source_ref": {"path": "/canonical/A.jsonl", "line": index}} for index, task in enumerate(TASKS[19:26], 20)]
    non_native += [{"arm": "B", "task_id": task, "attempt_index": 1, "population": "diagnostic", "reason": "skipped_by_budget", "canonical_source_ref": {"path": "/canonical/B-diagnostics.jsonl", "line": index}} for index, task in enumerate(TASKS[:6], 1)]
    inv = inventory(rows + canaries, non_native_cells=non_native, unscheduled_tasks_by_arm={"base": TASKS[26:], "A": TASKS[26:]})
    report = build_report(inv, [], ADMISSION, replicates=1)
    for arm, native, kept, measured in [("base", 26, 26, 25), ("A", 19, 10, 9), ("B", 66, 66, 65), ("C", 33, 28, 26)]:
        coverage = report["arms"][arm]["coverage"]
        assert coverage["requested_slots"] == 33 * len(SCHEDULE[arm])
        assert coverage["native_sources"] == native
        assert coverage["kept_diff_sources"] == kept
        assert coverage["qualified_v7_sources"] == 0
        assert report["arms"][arm]["v6"]["denominator"] == measured
    assert sum(report["arms"][arm]["coverage"]["native_sources"] for arm in SCHEDULE) == 144
    assert sum(report["arms"][arm]["coverage"]["kept_diff_sources"] for arm in SCHEDULE) == 130
    assert len(report["source_results"]) == 147
    assert report["arms"]["base"]["coverage"]["source_schedule_slots"] == 26
    assert report["arms"]["A"]["coverage"]["source_schedule_slots"] == 26
    assert report["arms"]["A"]["coverage"]["scheduled_without_native_slots"] == 7
    assert report["arms"]["A"]["v7"]["missing_rows"] == 14
    reasons = report["arms"]["A"]["v7"]["unmeasured_reasons"]
    assert reasons == {"not_in_historical_source_schedule": 7, "scheduled_native_trial_not_created": 7, "missing_retained_diff": 9, "missing_observation": 10}
    missing = report["arms"]["A"]["v7"]["missing_cells"]
    scheduled = [cell for cell in missing if cell["source_status"] == "scheduled_without_native"]
    assert len(scheduled) == 7
    assert all("source_uid" not in cell["canonical_source"] for cell in scheduled)
    assert all(cell["canonical_source"]["canonical_source_ref"]["path"] == "/canonical/A.jsonl" for cell in scheduled)
    assert len(report["arms"]["B"]["populations"]["diagnostic"]["non_native_cells"]) == 6
    assert len(report["arms"]["A"]["populations"]["quarantined"]["item_ids"]) == 3
    assert len(report["per_pass_labels"]) == 128
    assert all(row["label"] == "unmeasured" for row in report["per_pass_labels"])
    assert all(row["excluded_from_primary"] for row in report["per_pass_labels"] if row["population"] != "official")
    assert report["primary"]["cohort"]["requested_tasks"] == 33
    assert report["inventory"]["non_native_cells"] == non_native


@pytest.mark.parametrize("conflict", ["duplicate_marker", "native_overlap", "unauthorized", "unknown_task", "invented_uid", "unscheduled_overlap"])
def test_raw_non_native_cells_cannot_hide_invalid_official_membership(build_report, conflict: str) -> None:
    rows = complete_task()
    marker = {"arm": "A", "task_id": T2, "attempt_index": 1, "population": "official", "reason": "never_created", "canonical_source_ref": "/canonical/A.jsonl:8"}
    extra: dict[str, Any] = {"non_native_cells": [marker]}
    match = "duplicate official cell"
    if conflict == "duplicate_marker":
        extra["non_native_cells"].append(deepcopy(marker))
    elif conflict == "native_overlap":
        marker["task_id"] = T1
    elif conflict == "unauthorized":
        marker["attempt_index"] = 2
        match = "unauthorized official cell"
    elif conflict == "unknown_task":
        marker["task_id"] = "mimo-v2.6-rl/format-code-task-999999"
        match = "unknown frozen task"
    elif conflict == "invented_uid":
        marker["source_uid"] = ["invented", "invented"]
        match = "cannot claim a native source UID"
    else:
        extra["unscheduled_tasks_by_arm"] = {"A": [T2]}
        match = "unscheduled task conflicts"
    with pytest.raises(ValueError, match=match):
        build_report(inventory(rows, **extra), [], ADMISSION, replicates=1)


def test_empty_retained_diff_is_kept_and_refused_observation_is_not_replayed(
    build_report, tmp_path: Path,
) -> None:
    row = source("B")
    empty = tmp_path / "empty.kept.diff"
    empty.write_bytes(b"")
    row["diff"] = {"path": str(empty), "sha256": "sha256:" + hashlib.sha256(b"").hexdigest()}
    refused = observation(row, None, error="exact replay preflight refusal", replayed=False)
    report = build_report(inventory([row]), [refused], ADMISSION, replicates=1)
    assert report["arms"]["B"]["coverage"]["kept_diff_sources"] == 1
    assert report["arms"]["B"]["coverage"]["observation_sources"] == 1
    assert report["arms"]["B"]["coverage"]["replayed_native_sources"] == 0
    assert report["arms"]["B"]["coverage"]["qualified_v7_sources"] == 0
    assert report["per_pass_labels"][0]["reason"] == "exact replay preflight refusal"
    assert report["per_pass_labels"][0]["source"]["diff"] == row["diff"]
    assert empty.read_bytes() == b""


def test_unknown_replay_status_is_not_reported_as_known_replay(build_report) -> None:
    row = source("B")
    observed = observation(row, False)
    del observed["replayed"]
    report = build_report(inventory([row]), [observed], ADMISSION, replicates=1)
    assert report["arms"]["B"]["coverage"]["replayed_native_sources"] == 0
    assert report["arms"]["B"]["coverage"]["replay_status_unknown_sources"] == 1
    assert report["arms"]["B"]["coverage"]["qualified_v7_sources"] == 1
    assert report["source_results"][0]["replayed"] is None


@pytest.mark.parametrize("allowed", [False, None, 1, "true"])
def test_admitted_scoring_must_be_explicit_true(build_report, allowed: Any) -> None:
    with pytest.raises(ValueError, match="scoring_allowed must be true"):
        build_report(inventory([]), [], {**ADMISSION, "scoring_allowed": allowed}, replicates=1)


@pytest.mark.parametrize("field,value", [("v6_outcome", 0), ("v6_outcome", 1), ("v6_outcome", "false")])
def test_nonboolean_normalized_original_scores_refuse(build_report, field: str, value: Any) -> None:
    row = source("B")
    row[field] = value
    with pytest.raises(ValueError, match="boolean or null"):
        build_report(inventory([row]), [], ADMISSION, replicates=1)


def test_nonboolean_new_score_and_reduced_frozen_roster_refuse(build_report) -> None:
    row = source("B")
    observed = observation(row, False)
    observed["v7_outcome"] = 0
    with pytest.raises(ValueError, match="boolean or null"):
        build_report(inventory([row]), [observed], ADMISSION, replicates=1)
    with pytest.raises(ValueError, match="all 33 unique"):
        build_report(inventory([row], frozen_task_ids=[T1]), [], ADMISSION, replicates=1)


@pytest.mark.parametrize("replicates,seed", [(0, 1), (True, 1), (1, False)])
def test_invalid_bootstrap_parameters_refuse_even_for_empty_cohort(
    build_report, replicates: Any, seed: Any,
) -> None:
    with pytest.raises(ValueError):
        build_report(inventory([]), [], ADMISSION, replicates=replicates, seed=seed)
