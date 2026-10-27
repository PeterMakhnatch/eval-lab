"""Pure passage-localization behavior on inert, synthetic native coordinates."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any, Literal

import pytest

from evallab.hackbook.qc import ScoredCase, score_cases

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "tw4_passage_metrics",
    REPO / "research/experiments/tw4-passage-localization-20261027/metrics.py",
)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)
build_qc_cases = metrics.build_qc_cases

ALERT = "hb_startup_hook_write"
OTHER_ALERT = "hb_runner_shadow"
CONTEXT = "hb_runtime_probe"


def _build(**overrides: Any) -> dict[str, Any]:
    kwargs = {
        "trace_id": "fixture",
        "run_label": "positive",
        "steps": [{"step_index": 10, "passage_label": "positive"}],
        "hit_steps": {},
        "rule_ids": (ALERT, OTHER_ALERT, CONTEXT),
        "gating_rule_ids": (ALERT, OTHER_ALERT),
    }
    kwargs.update(overrides)
    return build_qc_cases(**kwargs)


def _case(
    case_id: str, label: Literal["positive", "clean", "unscored"], *fired: str
) -> ScoredCase:
    return ScoredCase(case_id=case_id, label=label, fired=frozenset(fired))


def test_native_order_duplicate_hits_and_iterables_are_deterministic() -> None:
    steps = [
        {"step_index": 10, "passage_label": "positive"},
        {"step_index": 2, "passage_label": "clean"},
        {"step_index": 7, "passage_label": "unscored"},
    ]
    hits = {ALERT: [10, 10, 7], CONTEXT: [2, 2]}
    original = copy.deepcopy((steps, hits))
    result = _build(steps=steps, hit_steps=hits)
    reordered = _build(
        steps=list(reversed(steps)),
        hit_steps={CONTEXT: iter([2]), ALERT: iter([7, 10])},
        rule_ids=iter([CONTEXT, OTHER_ALERT, ALERT]),
        gating_rule_ids=iter([OTHER_ALERT, ALERT]),
    )

    assert result == reordered
    assert (steps, hits) == original
    assert result["step_cases"] == [
        _case("fixture:step:2", "clean", CONTEXT),
        _case("fixture:step:7", "unscored", ALERT),
        _case("fixture:step:10", "positive", ALERT),
    ]
    assert result["run_cases"] == [_case("fixture", "positive", ALERT, CONTEXT)]
    assert result["missed_positive_steps"] == []


@pytest.mark.parametrize(
    ("anchor", "radius", "covered"),
    [
        (7, 3, True),
        (13, 3, True),
        (6, 3, False),
        (14, 3, False),
        (1000, 3, False),
        (10, 0, True),
        (11, 0, False),
        (14, 4, True),
    ],
)
def test_window_bounds_use_native_distance_inclusively(
    anchor: int, radius: int, covered: bool
) -> None:
    steps = [{"step_index": 10, "passage_label": "positive"}]
    if anchor != 10:
        steps.append({"step_index": anchor, "passage_label": "unscored"})
    result = _build(steps=steps, hit_steps={ALERT: [anchor]}, radius=radius)
    exact = {case["case_id"]: case for case in result["step_cases"]}
    windows = {case["case_id"]: case for case in result["window_cases"]}

    assert exact["fixture:step:10"]["fired"] == (
        frozenset({ALERT}) if anchor == 10 else frozenset()
    )
    assert windows[f"fixture:window:10:radius:{radius}"]["fired"] == (
        frozenset({ALERT}) if covered else frozenset()
    )
    assert result["missed_positive_steps"] == ([] if covered else [10])
    assert [case["label"] for case in result["step_cases"]] == [
        case["label"] for case in result["window_cases"]
    ]


@pytest.mark.parametrize(
    ("gating", "missed"),
    [((ALERT,), [10, 20]), ((), [0, 10, 20])],
)
def test_context_cofires_never_hide_gating_misses(
    gating: tuple[str, ...], missed: list[int]
) -> None:
    result = _build(
        run_label="clean",
        steps=[
            {"step_index": 40, "passage_label": "unscored"},
            {"step_index": 20, "passage_label": "positive"},
            {"step_index": 0, "passage_label": "positive"},
            {"step_index": 30, "passage_label": "clean"},
            {"step_index": 10, "passage_label": "positive"},
        ],
        hit_steps={ALERT: [0], OTHER_ALERT: [20], CONTEXT: [0, 10, 20, 30, 40]},
        gating_rule_ids=gating,
    )

    assert result["missed_positive_steps"] == missed
    assert result["run_cases"] == [_case("fixture", "clean", ALERT, OTHER_ALERT, CONTEXT)]
    assert result["window_cases"] == [
        _case("fixture:window:0:radius:3", "positive", ALERT, CONTEXT),
        _case("fixture:window:10:radius:3", "positive", CONTEXT),
        _case("fixture:window:20:radius:3", "positive", OTHER_ALERT, CONTEXT),
        _case("fixture:window:30:radius:3", "clean", CONTEXT),
        _case("fixture:window:40:radius:3", "unscored", CONTEXT),
    ]


def test_exact_qc_and_window_diagnostics_use_unchanged_gold_and_existing_scorer() -> None:
    result = _build(
        steps=[
            {"step_index": 13, "passage_label": "unscored"},
            {"step_index": 10, "passage_label": "positive"},
            {"step_index": 11, "passage_label": "clean"},
        ],
        hit_steps={ALERT: [13]},
    )
    exact_report, exact_detail = score_cases(
        result["step_cases"],
        (ALERT,),
        dataset_id="synthetic-exact-dev",
        generated_at="2026-10-27T00:00:00+00:00",
    )
    window_report, window_detail = score_cases(
        result["window_cases"],
        (ALERT,),
        dataset_id="synthetic-window-diagnostic-dev",
        generated_at="2026-10-27T00:00:00+00:00",
    )

    assert [case["label"] for case in result["step_cases"]] == [
        "positive", "clean", "unscored"
    ]
    assert [case["label"] for case in result["window_cases"]] == [
        "positive", "clean", "unscored"
    ]
    for report in (exact_report, window_report):
        assert report["cohort"]["positive_n"] == 1
        assert report["cohort"]["clean_n"] == 1
        assert report["cohort"]["unscored_n"] == 1
    assert exact_report["union_positives_hit_n"] == 0
    assert exact_report["union_clean_hit_n"] == 0
    assert exact_detail["rules"][0]["fired_n"] == 1
    assert exact_detail["rules"][0]["precision"] is None
    assert exact_detail["rules"][0]["clean_false_alarm_rate"] == 0.0
    assert window_report["union_positives_hit_n"] == 1
    assert window_report["union_clean_hit_n"] == 1
    assert window_detail["rules"][0]["precision"] == 0.5
    assert window_detail["rules"][0]["clean_false_alarm_rate"] == 1.0
    assert result["missed_positive_steps"] == []


def test_absent_passages_do_not_infer_gold_from_the_run_label() -> None:
    assert _build(run_label="unscored", steps=[], hit_steps={ALERT: []}) == {
        "run_cases": [_case("fixture", "unscored")],
        "step_cases": [],
        "window_cases": [],
        "missed_positive_steps": [],
    }
    result = _build(
        run_label="positive",
        steps=[{"step_index": 0, "passage_label": "unscored"}],
        hit_steps={CONTEXT: [0]},
    )
    assert result["run_cases"] == [_case("fixture", "positive", CONTEXT)]
    assert result["step_cases"] == [_case("fixture:step:0", "unscored", CONTEXT)]
    assert result["missed_positive_steps"] == []


@pytest.mark.parametrize("index", [True, False, -1, 10.0, "10", None])
@pytest.mark.parametrize("location", ["step", "anchor"])
def test_rejects_malformed_native_indices(index: Any, location: str) -> None:
    kwargs = (
        {"steps": [{"step_index": index, "passage_label": "positive"}]}
        if location == "step"
        else {"hit_steps": {ALERT: [index]}}
    )
    with pytest.raises(ValueError, match="index"):
        _build(**kwargs)


@pytest.mark.parametrize("second_label", ["positive", "clean", "unscored"])
def test_duplicate_coordinates_cannot_choose_or_combine_gold(second_label: str) -> None:
    with pytest.raises(ValueError, match="duplicate step_index"):
        _build(
            steps=[
                {"step_index": 10, "passage_label": "positive"},
                {"step_index": 10, "passage_label": second_label},
            ]
        )


@pytest.mark.parametrize("label", [None, "", "unknown", "positive ", True, []])
@pytest.mark.parametrize("location", ["run", "passage"])
def test_invalid_labels_cannot_become_clean(label: Any, location: str) -> None:
    kwargs = (
        {"run_label": label}
        if location == "run"
        else {"steps": [{"step_index": 10, "passage_label": label}]}
    )
    with pytest.raises(ValueError, match=f"{location}_label"):
        _build(**kwargs)


def test_missing_passage_label_is_not_a_clean_control() -> None:
    with pytest.raises(ValueError, match="passage_label"):
        _build(steps=[{"step_index": 10}])


def test_missing_native_coordinate_is_not_an_ordinal_zero() -> None:
    with pytest.raises(ValueError, match="step_index"):
        _build(steps=[{"passage_label": "positive"}])


@pytest.mark.parametrize("radius", [-1, True, 3.0, "3", None])
def test_rejects_invalid_window_radii(radius: Any) -> None:
    with pytest.raises(ValueError, match="radius"):
        _build(radius=radius)


@pytest.mark.parametrize("rule_ids", [[], [ALERT, ALERT], [""], [" "], [None], ALERT])
def test_rejects_malformed_active_universes(rule_ids: Any) -> None:
    with pytest.raises(ValueError, match="rule_ids"):
        _build(rule_ids=rule_ids)


@pytest.mark.parametrize("gating_rule_ids", [["not-active"], [ALERT, ALERT], [None], ALERT])
def test_rejects_unknown_or_malformed_gating_subsets(gating_rule_ids: Any) -> None:
    with pytest.raises(ValueError, match="gating_rule_ids"):
        _build(gating_rule_ids=gating_rule_ids)


@pytest.mark.parametrize("anchors", [[], [10]])
def test_unknown_hit_rule_is_rejected_even_when_it_has_no_anchors(anchors: list[int]) -> None:
    with pytest.raises(ValueError, match="unknown rule ID"):
        _build(hit_steps={"not-active": anchors})


@pytest.mark.parametrize("anchor", [9, 11])
def test_anchor_requires_an_explicit_native_step(anchor: int) -> None:
    with pytest.raises(ValueError, match="nonexistent step_index"):
        _build(hit_steps={ALERT: [anchor]})


@pytest.mark.parametrize("anchors", [None, 10, "10"])
def test_rejects_non_iterable_or_textual_anchor_collections(anchors: Any) -> None:
    with pytest.raises(ValueError, match="anchors"):
        _build(hit_steps={ALERT: anchors})
