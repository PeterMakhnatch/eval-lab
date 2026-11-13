"""HAR-92: step layers, continuation stitching, and the outcome/execution split."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.step_layers import (
    STEP_LAYERS_SCHEMA,
    StitchStats,
    classify_stop_reason,
    coverage_record,
    discover_trajectory_parts,
    duplicate_segments,
    executed_output,
    feedback_error_text,
    limit_hit_summary,
    segment_fingerprint,
    stitch_steps,
    stop_category,
)
from evallab.traj import _resolve_chain_segments, trial_trajectory_path
from evallab.trial_diagnosis import _raw_steps


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_feedback_detector_and_executed_verdict() -> None:
    assert feedback_error_text("Previous response had parsing errors:\nERROR: x") == "ERROR: x"
    assert feedback_error_text("plain output") is None
    assert feedback_error_text(None) is None
    assert executed_output({"observation": {"results": [{"content": "hi\n"}]}}) == "hi\n"
    assert (
        executed_output(
            {
                "observation": {
                    "results": [{"content": "Previous response had parsing errors:\nERROR: x"}]
                }
            }
        )
        is None
    )
    assert (
        executed_output(
            {
                "observation": {
                    "results": [
                        {"content": "Technical difficulties. Please continue with the task."}
                    ]
                }
            }
        )
        is None
    )
    assert executed_output({"observation": {"results": [{"content": "  "}]}}) is None
    assert executed_output({}) is None


# --- stitching and coverage ----------------------------------------------- #


def _doc(*messages: str, session: str = "s", copied: int = 0) -> dict[str, Any]:
    # Timestamped like real ATIF steps so cross-part shared steps merge.
    steps = [
        {
            "source": "agent",
            "message": message,
            "timestamp": f"2026-09-29T00:00:{index:02d}+00:00",
        }
        for index, message in enumerate(messages)
    ]
    for index in range(copied):
        steps.append({"source": "agent", "message": f"old-{index}", "is_copied_context": True})
    return {"session_id": session, "steps": steps}


def test_duplicate_files_count_once() -> None:
    head = _doc("a", "b")
    unique, stats = stitch_steps([head, _doc("a", "b")])
    assert [step["message"] for step in unique] == ["a", "b"]
    assert stats.duplicated_steps == 2
    assert stats.unique_steps == 2


def test_prefix_overlap_and_copied_context() -> None:
    unique, stats = stitch_steps([_doc("a", "b"), _doc("a", "b", "c", copied=2)])
    assert [step["message"] for step in unique] == ["a", "b", "c"]
    assert stats.duplicated_steps == 2
    assert stats.copied_context_steps == 2


def test_timeless_steps_never_merge() -> None:
    # Without a timestamp a repeat is indistinguishable from a re-emitted
    # turn (a loop of identical calls), so it is always kept.
    timeless = {
        "steps": [
            {"source": "agent", "message": "same"},
            {"source": "agent", "message": "same"},
        ]
    }
    unique, stats = stitch_steps([timeless])
    assert len(unique) == 2
    assert stats.duplicated_steps == 0


def test_within_part_repeats_are_kept() -> None:
    # A loop of identical timestamped calls inside one part is evidence,
    # not a recording duplicate: only cross-part repeats merge.
    loop = {
        "steps": [
            {"source": "agent", "message": "same", "timestamp": "2026-09-01T00:02:00Z"},
            {"source": "agent", "message": "same", "timestamp": "2026-09-01T00:02:00Z"},
        ]
    }
    unique, stats = stitch_steps([loop])
    assert len(unique) == 2
    assert stats.duplicated_steps == 0


def test_whole_duplicate_segments_use_sft_vocabulary() -> None:
    steps_a = [{"source": "agent", "message": "a"}]
    steps_b = [{"source": "agent", "message": "a"}]
    steps_c = [{"source": "agent", "message": "c"}]
    assert segment_fingerprint(steps_a) == segment_fingerprint(steps_b)
    assert segment_fingerprint(steps_a) != segment_fingerprint(steps_c)
    assert segment_fingerprint(None) is None
    assert duplicate_segments(
        [
            ("trajectory.json", steps_a),
            ("trajectory.cont-1.json", steps_b),
            ("trajectory.cont-2.json", steps_c),
        ]
    ) == {"trajectory.cont-1.json": "duplicate_of:trajectory.json"}


def test_coverage_uses_capture_names(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    _write(agent / "trajectory.json", _doc("a"))
    _write(agent / "trajectory.cont-1.json", _doc("a"))
    _write(agent / "trajectory.cont-3.json", _doc("b"))
    parts = discover_trajectory_parts(agent)
    assert [part.name for part in parts] == [
        "trajectory.json",
        "trajectory.cont-1.json",
        "trajectory.cont-3.json",
    ]
    docs = [json.loads((agent / part.name).read_text()) for part in parts]
    unique, stats = stitch_steps(docs)
    assert len(unique) == 2
    coverage = coverage_record(
        parts,
        stats,
        summarization_count=3,
        step_lists={part.name: docs[index].get("steps") for index, part in enumerate(parts)},
    )
    assert coverage["trajectory_head"] is True
    assert coverage["continuation_indices"] == [1, 3]
    assert coverage["continuations_missing"] == [2]
    assert coverage["duplicate_segments"] == {
        "trajectory.cont-1.json": "duplicate_of:trajectory.json"
    }
    assert coverage["unique_steps"] == 2
    assert coverage["complete"] is True
    assert any("counted once" in note for note in coverage["notes"])


def test_head_missing_falls_back_to_first_continuation(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    _write(agent / "trajectory.cont-11.json", _doc("a"))
    assert trial_trajectory_path(tmp_path) == agent / "trajectory.cont-11.json"
    _write(agent / "trajectory.json", _doc("a"))
    assert trial_trajectory_path(tmp_path) == agent / "trajectory.json"


def test_chain_union_finds_unreferenced_continuations(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    head = _doc("a")
    _write(agent / "trajectory.json", head)
    _write(agent / "trajectory.cont-1.json", _doc("a", "b"))
    resolution = _resolve_chain_segments(agent / "trajectory.json", head, tmp_path)
    assert [path.name for path, _, _ in resolution.segments] == [
        "trajectory.json",
        "trajectory.cont-1.json",
    ]
    assert resolution.complete is True


def test_raw_steps_read_continuations_with_dedupe_notice(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    _write(agent / "trajectory.json", _doc("a"))
    _write(agent / "trajectory.cont-1.json", _doc("a", "b"))
    steps, notices = _raw_steps(tmp_path)
    assert [step["message"] for step in steps] == ["a", "b"]
    assert len(notices) == 1 and "2 trajectory parts" in notices[0]
    assert "1 shared steps counted once" in notices[0]


# --- outcome vs execution -------------------------------------------------- #


def test_scored_timeout_stops_as_timeout_not_infra() -> None:
    reason, _ = classify_stop_reason(
        agent_metadata={},
        exception_info={"exception_type": "AgentTimeoutError"},
    )
    assert reason == "agent_timeout"
    reason, _ = classify_stop_reason(
        agent_metadata={"stop_reason": "trial_budget_exhausted"},
        exception_info={"exception_type": "TrialBudgetExhaustedError"},
    )
    assert reason == "trial_budget_exhausted"
    reason, _ = classify_stop_reason(agent_metadata={}, exception_info={}, last_task_complete=True)
    assert reason == "task_complete"
    reason, detail = classify_stop_reason(agent_metadata={}, exception_info={})
    assert reason == "unknown" and isinstance(detail, str) and detail


@pytest.mark.parametrize(
    ("metadata", "exception", "expected"),
    [
        ({"stop_reason": "loop_break"}, {}, "loop_break"),
        ({}, {"exception_type": "LoopBreakStop"}, "loop_break"),
        ({"loop_break": {"fired": True, "stop_call": 8}}, {}, "loop_break"),
        ({"loop_break": {"fired": True, "stop_call": None}}, {}, "task_complete"),
        (
            {"stop_reason": "ceiling:input_tokens"},
            {"exception_type": "TrialBudgetExhaustedError"},
            "ceiling:input_tokens",
        ),
        ({"native_exit_status": "LimitsExceeded"}, {}, "harness_step_limit"),
        (
            {
                "native_exit_status": "LimitsExceeded",
                "native_exit_result": "Empty assistant response",
            },
            {},
            "error",
        ),
        ({"native_exit_status": "Idle"}, {}, "task_complete"),
        ({"native_exit_status": "Idle"}, {"exception_type": "AgentTimeoutError"}, "agent_timeout"),
        ({"native_exit_status": "ModelQueryError"}, {}, "error"),
        ({"native_exit_status": "InfraError"}, {}, "error"),
        (
            {"native_exit_status": "ModelQueryError", "stop_reason": "infra_error"},
            {"exception_type": "RuntimeError"},
            "infra_error",
        ),
        ({"stop_reason": "context_exhausted"}, {}, "context_exhausted"),
        ({"native_exit_status": "ContextExhausted"}, {}, "context_exhausted"),
        (
            {"native_exit_status": "ContextExhausted", "stop_reason": "context_exhausted"},
            {"exception_type": "RuntimeError"},
            "context_exhausted",
        ),
    ],
)
def test_recorded_stops_take_precedence_over_completion(metadata, exception, expected) -> None:
    reason, _ = classify_stop_reason(
        agent_metadata=metadata, exception_info=exception, last_task_complete=True
    )
    assert reason == expected


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("trial_budget_exhausted", "our_limit"),
        ("loop_break", "our_limit"),
        ("ceiling:output_tokens", "our_limit"),
        ("harness_step_limit", "harness_step_limit"),
        ("context_exhausted", "context_exhausted"),
        ("agent_timeout", "task_timeout"),
        ("task_complete", "model_end"),
        ("task_complete_confirmed", "model_end"),
        ("prose_completion", "model_end"),
        ("error", "error"),
        ("infra_error", "error"),
        ("unknown", "unknown"),
        (None, "unknown"),
    ],
)
def test_stop_categories_keep_limit_ownership_separate(reason, category) -> None:
    assert stop_category(reason) == category


def test_limit_hit_summary_strict_threshold_and_unknown_denominator() -> None:
    assert limit_hit_summary(iter(())) == {
        "trials": 0,
        "limit_hit_trials": 0,
        "limit_hit_share": None,
        "setup_limited": False,
    }
    boundary = limit_hit_summary(iter(["loop_break", *([None] * 19)]))
    assert boundary == {
        "trials": 20,
        "limit_hit_trials": 1,
        "limit_hit_share": 0.05,
        "setup_limited": False,
    }
    assert limit_hit_summary(["ceiling:requests", *([None] * 18)])["setup_limited"] is True
    assert limit_hit_summary(["harness_step_limit", "agent_timeout"]) == {
        "trials": 2,
        "limit_hit_trials": 0,
        "limit_hit_share": 0.0,
        "setup_limited": False,
    }


def test_layer_schema_stamp() -> None:
    assert STEP_LAYERS_SCHEMA == "evallab.step_layers/v1"
    assert StitchStats().unique_steps == 0
