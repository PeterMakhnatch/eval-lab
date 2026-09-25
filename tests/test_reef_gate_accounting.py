"""Gate accounting repairs: validity, missing-score and timing semantics.

Covers the four reviewer findings without touching plugin/rules/known-effect
lifecycle: empty/error rows never dilute the publish denominator, wrong vector
shapes are invalid, zeros are observed while None/bool/nonnumeric/nonfinite are
missing, unpublished identity is unknown (None), and None/absent timings stay
unavailable with observed-only medians.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evallab_reef_gate import calibrate

TASKS = [
    "[sieve] How many primes are below 100000? Reply with the count as a plain integer.",
    "[fib] With fib(1) = 1 and fib(2) = 1, compute fib(90) exactly.",
    "[csv] Compute the median of the value column in this csv.",
]


def result_row(**overrides) -> dict:
    base = {
        "trial": 1,
        "condition": "aa",
        "scenario": "aa-gate",
        "status": "settled",
        "seconds": 10.0,
        "published": False,
        "skipped": None,
        "wins": 0,
        "losses": 0,
        "ties": 3,
        "candidate_scores": [0.0, 0.0, 0.0],
        "current_scores": [0.0, 0.0, 0.0],
        "files_identical": True,
    }
    base.update(overrides)
    return base


def decision_record(**overrides) -> dict:
    base = {
        "decision_seconds": 0.4,
        "evaluation_seconds": 130.2,
        "reef_commit": "4c3a6bb24949bd93a4566ca1d9877d4feeab023e",
        "reason_code": "publish",
        "pairs": [],
        "vetoes": [],
    }
    base.update(overrides)
    return base


def test_empty_and_error_rows_do_not_dilute_publish_rate() -> None:
    valid = result_row(
        trial=1,
        published=True,
        wins=2,
        losses=0,
        ties=1,
        candidate_scores=[1.0, 0.0, 1.0],
        current_scores=[0.0, 0.0, 1.0],
    )
    empty = result_row(
        trial=2, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[], current_scores=[],
    )
    evaluator_error = result_row(
        trial=3, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[], current_scores=[],
        gate={"reason_code": "evaluator_error"},
    )
    invalid_evaluation = result_row(
        trial=4, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[0.0, 0.0, 0.0], current_scores=[0.0, 0.0, 0.0],
        gate={"reason_code": "invalid_evaluation"},
    )
    record_error = result_row(
        trial=5, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[0.0, 0.0, 0.0], current_scores=[0.0, 0.0, 0.0],
        reason_code="decision_record_error",
    )
    summary = calibrate.summarize(
        [valid, empty, evaluator_error, invalid_evaluation, record_error],
        tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5,
    )
    assert summary["trials_attempted"] == 5
    assert summary["trials_settled"] == 5
    assert summary["trials_invalid"] == 4
    assert summary["denominator"] == 1
    assert summary["publishes"] == 1
    assert summary["publish_rate"] == 1.0
    # Never fabricate failed scores: the invalid rows contribute no episodes.
    assert summary["episodes_expected"] == 2 * len(TASKS) * 1 * 1
    assert summary["wlt_histogram"] == {"2/0/1": 1}


def test_wrong_vector_shape_is_invalid() -> None:
    rows = [
        result_row(trial=1, candidate_scores=[1.0, 0.0], current_scores=[1.0, 0.0]),
        result_row(trial=2, candidate_scores=[1.0, 0.0, 1.0], current_scores=[1.0, 0.0]),
        result_row(
            trial=3,
            candidate_scores=[1.0, 0.0, 1.0, 0.0],
            current_scores=[1.0, 0.0, 1.0, 0.0],
        ),
    ]
    summary = calibrate.summarize(
        rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5,
    )
    assert summary["trials_invalid"] == 3
    assert summary["denominator"] == 0
    assert summary["publish_rate"] is None
    assert summary["wilson95"] is None
    # Direct predicate: nonempty equal vectors alone are not enough without shape.
    assert calibrate._settled_row_is_valid(
        result_row(candidate_scores=[1.0, 0.0], current_scores=[1.0, 0.0])
    ) is True
    assert calibrate._settled_row_is_valid(
        result_row(candidate_scores=[1.0, 0.0], current_scores=[1.0, 0.0]),
        expected_len=3,
    ) is False


def test_valid_zeros_are_observed_missing_and_nonfinite_are_not_failures() -> None:
    labels = ["[sieve]", "[fib]", "[csv]"]
    rows = [
        {
            "candidate_scores": [0.0, None, float("nan")],
            "current_scores": [1.0, "bad", True],
        },
        {
            "candidate_scores": [float("inf"), 1.0, 0.0],
            "current_scores": [0.0, float("-inf"), None],
        },
    ]
    candidate_passes, candidate_missing = calibrate._side_passes(
        rows, "candidate_scores", labels, 1, 1.0
    )
    current_passes, current_missing = calibrate._side_passes(
        rows, "current_scores", labels, 1, 1.0
    )
    # 0.0 is a usable failing score, not missing; every None/bool/str/nonfinite is missing.
    assert candidate_passes == {"[sieve]": [False], "[fib]": [True], "[csv]": [False]}
    assert candidate_missing == 3
    assert current_passes == {"[sieve]": [True, False], "[fib]": [], "[csv]": []}
    assert current_missing == 4
    # End to end: zeros score, missing does not.
    summary = calibrate.summarize(
        [
            result_row(
                trial=1,
                candidate_scores=[0.0, None, float("nan")],
                current_scores=[1.0, 1.0, 1.0],
            )
        ],
        tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5,
    )
    assert summary["denominator"] == 1
    assert summary["episodes_scored"] == 4
    assert summary["episodes_missing"] == 2
    assert summary["pass_rate_by_task_pooled"]["[sieve]"] == pytest.approx(0.5)


def test_no_published_rows_identity_is_unknown() -> None:
    rows = [
        result_row(trial=1, published=False, files_identical=True),
        result_row(trial=2, published=False, files_identical=False),
    ]
    aa = calibrate.summarize(
        rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5,
    )
    assert aa["publishes"] == 0
    assert aa["denominator"] == 2
    assert aa["all_published_trees_identical"] is None
    known = calibrate.summarize(
        rows, tasks=TASKS, repeats=1, condition="known-effect",
        alpha=0.05, min_valid_pairs=5,
    )
    assert known["publishes"] == 0
    assert known["all_published_trees_changed"] is None


def test_none_and_missing_timing_stay_unavailable_with_observed_only_median(
    tmp_path: Path,
) -> None:
    record_dir = tmp_path / "gate-decisions"
    record_dir.mkdir()
    (record_dir / "r1.json").write_text(json.dumps(decision_record(
        decision_seconds=0.4, evaluation_seconds=100.0,
    )))
    (record_dir / "r2.json").write_text(json.dumps(decision_record(
        decision_seconds=0.6, evaluation_seconds=None,
    )))
    body = decision_record(decision_seconds=None)
    del body["evaluation_seconds"]
    (record_dir / "r3.json").write_text(json.dumps(body))
    out = calibrate.read_decision_records(record_dir)
    assert out["count"] == 3
    assert out["decision_walltime_s"] == pytest.approx([0.4, 0.6])
    assert out["decision_walltime_median_s"] == pytest.approx(0.5)
    assert out["decision_walltime_missing"] == 1
    assert out["evaluation_walltime_s"] == [100.0]
    assert out["evaluation_walltime_median_s"] == 100.0
    assert out["evaluation_walltime_missing"] == 2
    assert 0.0 not in out["decision_walltime_s"]
    assert 0.0 not in out["evaluation_walltime_s"]


def test_bad_timing_fields_remain_malformed(tmp_path: Path) -> None:
    record_dir = tmp_path / "gate-decisions"
    record_dir.mkdir()
    for bad in [
        decision_record(decision_seconds="0.4"),
        decision_record(evaluation_seconds="-1"),
        decision_record(decision_seconds=float("nan")),
        decision_record(evaluation_seconds=float("inf")),
        decision_record(decision_seconds=True),
    ]:
        (record_dir / "bad.json").write_text(json.dumps(bad))
        with pytest.raises(SystemExit, match="malformed decision record"):
            calibrate.read_decision_records(record_dir)
        (record_dir / "bad.json").unlink()
    (record_dir / "good.json").write_text(json.dumps(decision_record()))
    assert calibrate.read_decision_records(record_dir)["count"] == 1


def _write_producer_work_dir(work: Path, *, tasks: list[str], repeats: int) -> None:
    """A live-producer-shaped work dir: run-meta.json without tasks plus the served recipe."""
    import yaml

    (work / "serve-aa.yaml").write_text(
        yaml.safe_dump({"recipe": {"config": {"evolution": {"tasks": tasks, "episode_repeats": repeats}}}})
    )
    # Actual main() shape: every campaign field except tasks.
    (work / "run-meta.json").write_text(
        json.dumps(
            {
                "condition": "aa",
                "selection": "evallab_reef_gate.plugin:Factory",
                "model": "qwen2.5:7b",
                "ollama_url": "http://127.0.0.1:11434",
                "reef_commit": "4c3a6bb24949bd93a4566ca1d9877d4feeab023e",
                "repeats": repeats,
                "trials": 30,
                "alpha": 0.05,
                "min_valid_pairs": 5,
                "pass_threshold": 1.0,
                "started_utc": "2026-09-25T00:00:00+00:00",
            }
        )
    )


def test_analyze_uses_served_recipe_when_run_meta_has_no_tasks(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _write_producer_work_dir(work, tasks=TASKS, repeats=1)
    row = result_row(
        trial=1,
        published=False,
        wins=0,
        losses=0,
        ties=3,
        candidate_scores=[1.0, 0.0, 1.0],
        current_scores=[1.0, 0.0, 1.0],
    )
    (work / "results.jsonl").write_text(json.dumps(row) + "\n")
    meta_before = (work / "run-meta.json").read_text()
    serve_before = (work / "serve-aa.yaml").read_text()
    summary = calibrate.analyze(work)
    assert summary["denominator"] == 1
    assert summary["trials_invalid"] == 0
    assert summary["pass_rate_by_task_pooled"]["[sieve]"] == pytest.approx(1.0)
    assert summary["all_published_trees_identical"] is None
    # Live inputs are untouched; only summary.json is added.
    assert (work / "run-meta.json").read_text() == meta_before
    assert (work / "serve-aa.yaml").read_text() == serve_before
    assert "tasks" not in json.loads(meta_before)


def test_analyze_rejects_contradictory_retained_tasks(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _write_producer_work_dir(work, tasks=TASKS, repeats=1)
    meta = json.loads((work / "run-meta.json").read_text())
    meta["tasks"] = ["[other] task"]
    (work / "run-meta.json").write_text(json.dumps(meta))
    (work / "results.jsonl").write_text("")
    with pytest.raises(SystemExit, match="contradictory tasks"):
        calibrate.analyze(work)


def test_insufficient_evidence_outage_excluded_but_partial_pairs_count() -> None:
    valid = result_row(
        trial=1,
        published=True,
        wins=2,
        losses=0,
        ties=1,
        candidate_scores=[1.0, 0.0, 1.0],
        current_scores=[0.0, 0.0, 1.0],
    )
    outage_gate_hold = result_row(
        trial=2, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[None, None, None],
        current_scores=[None, None, None],
        gate={"reason_code": "insufficient_evidence"},
    )
    outage_no_reason = result_row(
        trial=3, published=False, wins=0, losses=0, ties=0,
        candidate_scores=[None, None, None],
        current_scores=[None, None, None],
    )
    held_with_some_evidence = result_row(
        trial=4, published=False, wins=1, losses=0, ties=0,
        candidate_scores=[1.0, None, None],
        current_scores=[0.0, 0.0, None],
        reason_code="insufficient_evidence",
    )
    partial_accepted = result_row(
        trial=5, published=False, wins=1, losses=0, ties=1,
        candidate_scores=[1.0, None, 0.0],
        current_scores=[0.0, 0.0, 0.0],
        gate={"reason_code": "not_significant"},
    )
    # Direct predicate: reason holds and vector outage exclude, partial stays eligible.
    assert calibrate._settled_row_is_valid(outage_gate_hold, expected_len=3) is False
    assert calibrate._settled_row_is_valid(outage_no_reason, expected_len=3) is False
    assert calibrate._settled_row_is_valid(held_with_some_evidence, expected_len=3) is False
    assert calibrate._settled_row_is_valid(partial_accepted, expected_len=3) is True
    assert calibrate._is_usable_score(0.0) is True
    assert calibrate._is_usable_score(None) is False
    summary = calibrate.summarize(
        [valid, outage_gate_hold, outage_no_reason, held_with_some_evidence, partial_accepted],
        tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5,
    )
    # The outage/hold rows stay in invalid counts but never dilute the Wilson denominator
    # as resolved negatives; the gate-accepted partial trial counts with its usable pairs.
    assert summary["trials_attempted"] == 5
    assert summary["trials_settled"] == 5
    assert summary["trials_invalid"] == 3
    assert summary["denominator"] == 2
    assert summary["publishes"] == 1
    assert summary["publish_rate"] == pytest.approx(0.5)
    # No imputation: the partial trial contributes its 5 observed episodes, with the one
    # None pair missing rather than scored as a failure.
    assert summary["episodes_expected"] == 2 * len(TASKS) * 1 * 2
    assert summary["episodes_scored"] == 6 + 5
    assert summary["episodes_missing"] == 1
    assert summary["wlt_histogram"] == {"1/0/1": 1, "2/0/1": 1}
