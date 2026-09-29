"""HAR-92: step layers, continuation stitching, and the outcome/execution split."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.step_layers import (
    STEP_LAYERS_KEY,
    STEP_LAYERS_SCHEMA,
    ReplayedCommand,
    ReplayedParse,
    StitchStats,
    classify_stop_reason,
    coverage_record,
    discover_trajectory_parts,
    duplicate_segments,
    executed_output,
    execution_problems,
    feedback_error_text,
    reconstruct_layers,
    segment_fingerprint,
    stitch_steps,
    summarize_layers,
    synthesize_atif_calls,
    verifier_outcome,
    wrap_layers,
)
from evallab.traj import _resolve_chain_segments, trial_trajectory_path
from evallab.trial_diagnosis import _raw_steps, diagnose_atif


def _parse(text: str) -> ReplayedParse:
    """Fake stock parser: JSON objects parse, anything else errors."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ReplayedParse(error="No valid JSON found in response")
    if not isinstance(payload, dict):
        return ReplayedParse(error="not an object")
    commands = tuple(
        ReplayedCommand(
            keystrokes=command.get("keystrokes", ""),
            duration_sec=command.get("duration"),
        )
        for command in payload.get("commands", [])
        if isinstance(command, dict)
    )
    return ReplayedParse(
        error=None, commands=commands, task_complete=bool(payload.get("task_complete"))
    )


def _agent(message: str, **extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"source": "agent", "message": message}
    if extra:
        step["extra"] = dict(extra)
    return step


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- layers --------------------------------------------------------------- #


def test_exec_turn_reconstructs_calls_with_feedback_free_observation() -> None:
    message = json.dumps({"commands": [{"keystrokes": "ls", "duration": 0.1}]})
    step = _agent(message, **{})
    step["observation"] = {"results": [{"content": "app\n"}]}
    layers = reconstruct_layers(step, parse=_parse)
    assert layers is not None
    assert layers["provenance"] == "reconstructed"
    assert layers["accepted"]["kind"] == "calls"
    assert layers["accepted"]["calls"] == [{"keystrokes": "ls", "duration_sec": 0.1}]
    assert layers["executed"]["keystrokes_sent"] == ["ls\n"]
    assert layers["observed"]["output"] == "app\n"
    calls = synthesize_atif_calls(step, layers)
    assert calls[0]["function_name"] == "exec"
    # Synthesized ATIF carries the parsed text; the Enter the harness sends
    # lives in the executed layer above.
    assert calls[0]["arguments"]["keystrokes"] == "ls"


def test_feedback_observation_beats_todays_parser() -> None:
    # A shape today's parser accepts but the running treatment rejected:
    # the harness verdict in the observation wins, nothing executed.
    message = json.dumps({"commands": [{"keystrokes": "ls"}]})
    step = _agent(message)
    step["observation"] = {
        "results": [{"content": "Previous response had parsing errors:\nERROR: boom"}]
    }
    layers = reconstruct_layers(step, parse=_parse)
    assert layers is not None
    assert layers["accepted"]["kind"] == "parse_error"
    assert layers["accepted"]["parse_error"] == "ERROR: boom"
    assert layers["executed"]["keystrokes_sent"] is None
    assert synthesize_atif_calls(step, layers) == []


def test_recorded_layers_pass_through_untouched() -> None:
    recorded = wrap_layers(
        "recorded",
        {"message": "m", "reasoning": None, "reason": None},
        {
            "kind": "calls",
            "calls": [{"keystrokes": "ls\n"}],
            "task_complete": None,
            "parse_error": None,
            "prose_shaped": False,
            "reason": None,
        },
        {"keystrokes_sent": ["ls\n"], "durations_sec": [0.1],
         "sent_at": None, "timeout": False, "reason": None},
        {"output": "app\n", "truncated": False, "truncated_bytes": None,
         "timeout_template": False, "reason": None},
    )
    step = _agent("m", **{STEP_LAYERS_KEY: recorded})
    assert reconstruct_layers(step, parse=_parse) == recorded


def test_prompt_steps_get_no_layers_even_with_example_commands() -> None:
    # Terminus's instruction prompt shows an example response; HAR-90's
    # 0036-e report counted its commands as three executed tool calls.
    example = json.dumps(
        {"commands": [{"keystrokes": "ls -la\n"}, {"keystrokes": "cd project\n"}],
         "task_complete": True}
    )
    for source in ("user", "system"):
        step = {"source": source, "message": f"Respond like this:\n{example}"}
        assert reconstruct_layers(step, parse=lambda _: _parse(example)) is None


def test_prose_flag_maps_to_task_complete_without_finish_reason() -> None:
    step = _agent("done, all green", prose_completion=True)
    layers = reconstruct_layers(step, parse=_parse)
    assert layers is not None
    assert layers["accepted"]["kind"] == "prose_completion"
    assert layers["accepted"]["task_complete"] is True
    calls = synthesize_atif_calls(step, layers)
    assert len(calls) == 1 and calls[0]["function_name"] == "task_complete"


def test_task_complete_turn_synthesizes_completion_call() -> None:
    message = json.dumps({"commands": [], "task_complete": True})
    step = _agent(message)
    layers = reconstruct_layers(step, parse=_parse)
    assert layers is not None
    assert layers["accepted"]["task_complete"] is True
    assert synthesize_atif_calls(step, layers)[0]["function_name"] == "task_complete"


def test_feedback_detector_and_executed_verdict() -> None:
    assert feedback_error_text("Previous response had parsing errors:\nERROR: x") == "ERROR: x"
    assert feedback_error_text("plain output") is None
    assert feedback_error_text(None) is None
    assert executed_output({"observation": {"results": [{"content": "hi\n"}]}}) == "hi\n"
    assert executed_output({"observation": {"results": [{"content": "Previous response had parsing errors:\nERROR: x"}]}}) is None
    assert executed_output({"observation": {"results": [{"content": "Technical difficulties. Please continue with the task."}]}}) is None
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
        steps.append({"source": "agent", "message": f"old-{index}",
                      "is_copied_context": True})
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
    timeless = {"steps": [
        {"source": "agent", "message": "same"},
        {"source": "agent", "message": "same"},
    ]}
    unique, stats = stitch_steps([timeless])
    assert len(unique) == 2
    assert stats.duplicated_steps == 0

def test_within_part_repeats_are_kept() -> None:
    # A loop of identical timestamped calls inside one part is evidence,
    # not a recording duplicate: only cross-part repeats merge.
    loop = {"steps": [
        {"source": "agent", "message": "same", "timestamp": "2026-09-01T00:02:00Z"},
        {"source": "agent", "message": "same", "timestamp": "2026-09-01T00:02:00Z"},
    ]}
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
        [("trajectory.json", steps_a), ("trajectory.cont-1.json", steps_b),
         ("trajectory.cont-2.json", steps_c)]
    ) == {"trajectory.cont-1.json": "duplicate_of:trajectory.json"}


def test_coverage_uses_capture_names(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    _write(agent / "trajectory.json", _doc("a"))
    _write(agent / "trajectory.cont-1.json", _doc("a"))
    _write(agent / "trajectory.cont-3.json", _doc("b"))
    parts = discover_trajectory_parts(agent)
    assert [part.name for part in parts] == [
        "trajectory.json", "trajectory.cont-1.json", "trajectory.cont-3.json"]
    docs = [json.loads((agent / part.name).read_text()) for part in parts]
    unique, stats = stitch_steps(docs)
    assert len(unique) == 2
    coverage = coverage_record(
        parts, stats, summarization_count=3,
        step_lists={part.name: docs[index].get("steps") for index, part in enumerate(parts)},
    )
    assert coverage["trajectory_head"] is True
    assert coverage["continuation_indices"] == [1, 3]
    assert coverage["continuations_missing"] == [2]
    assert coverage["duplicate_segments"] == {
        "trajectory.cont-1.json": "duplicate_of:trajectory.json"}
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
        "trajectory.json", "trajectory.cont-1.json"]
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


def test_verifier_outcome_never_zero_for_missing() -> None:
    assert verifier_outcome({"reward": 1.0}) == "pass"
    assert verifier_outcome({"reward": 0.0}) == "fail"
    assert verifier_outcome({}) == "none"
    assert verifier_outcome({"reward": "nan"}) == "none"


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
    reason, _ = classify_stop_reason(
        agent_metadata={}, exception_info={}, last_task_complete=True
    )
    assert reason == "task_complete"
    reason, detail = classify_stop_reason(agent_metadata={}, exception_info={})
    assert reason == "unknown" and isinstance(detail, str) and detail


def test_execution_problems_keep_unknowns_null() -> None:
    summary = {"parse_errors": 2, "prose_completions": 1}
    lab = {"provider_usage": {
        "calls": [
            {"status": None, "state": "unresolved",
             "reason": "provider_http_400_usage_unknown"},
            {"status": 200, "state": "reconciled"},
        ],
        "unresolved_requests": 1,
    }}
    problems = execution_problems(layer_summary=summary, lab_metadata=lab)
    assert problems["parse_errors"] == 2
    assert problems["http_400_no_usage"] == 1
    assert problems["proxy_unresolved_requests"] == 1
    assert problems["proxy_usage_unreconciled"] is True
    # The ledger answers the last unknown, so this fixture is fully known.
    assert problems["coverage"] == "complete"
    empty = execution_problems()
    assert all(value is None for key, value in empty.items() if key != "coverage")


def test_summarize_counts_provenance_not_zeros() -> None:
    steps = [_agent("a"), {"source": "user", "message": "q"}, _agent("b")]
    layers = {0: wrap_layers("reconstructed", {}, {}, {}, {}), 2: None}
    summary = summarize_layers(steps, layers)
    assert summary["agent_steps"] == 2
    assert summary["reconstructed"] == 1
    assert summary["missing"] == 1


def test_missing_layers_read_unknown_not_zero() -> None:
    steps = [_agent("a"), _agent("b")]
    summary = summarize_layers(steps, {})
    assert summary["agent_steps"] == 2
    assert summary["missing"] == 2
    assert summary["parse_errors"] is None
    assert summary["prose_completions"] is None
    assert summary["executed_calls"] is None
    assert summary["task_complete_turns"] is None
    assert summary["layers_unknown_reason"] == (
        "layers missing for 2 of 2 agent steps: "
        "no recorded step_layers and no reconstructed layers"
    )
    problems = execution_problems(layer_summary=summary, lab_metadata={})
    assert problems["parse_errors"] is None
    assert problems["prose_completions"] is None
    assert problems["proxy_usage_unreconciled"] is None
    assert problems["coverage"] == (
        "unknown: http_400_no_usage, parse_errors, prose_completions, "
        "proxy_unresolved_requests, proxy_usage_unreconciled"
    )


def test_missing_layers_reason_names_parser_clause() -> None:
    summary = summarize_layers(
        [_agent("a")],
        {},
        layers_missing_why="no recorded step_layers and Harbor's Terminus parser is not importable",
    )
    assert summary["layers_unknown_reason"] == (
        "layers missing for 1 of 1 agent steps: "
        "no recorded step_layers and Harbor's Terminus parser is not importable"
    )


def test_copied_step_without_layers_is_copied_not_missing() -> None:
    live = _agent("a")
    replay = {"source": "agent", "message": "b", "is_copied_context": True}
    summary = summarize_layers([live, replay], {})
    assert summary["agent_steps"] == 2
    assert summary["copied"] == 1
    assert summary["missing"] == 1
    assert summary["parse_errors"] is None
    only_replay = summarize_layers([replay], {})
    assert only_replay["copied"] == 1
    assert only_replay["missing"] == 0
    assert only_replay["layers_unknown_reason"] is None
    assert only_replay["parse_errors"] == 0


def test_ledger_reconciled_is_false_not_none() -> None:
    lab = {"provider_usage": {
        "calls": [
            {"status": 200, "state": "reconciled"},
            {"status": 200, "state": "reconciled"},
        ],
        "unresolved_requests": 0,
    }}
    problems = execution_problems(layer_summary={}, lab_metadata=lab)
    assert problems["proxy_usage_unreconciled"] is False
    assert problems["proxy_unresolved_requests"] == 0
    assert problems["http_400_no_usage"] == 0


def test_diagnose_atif_scored_timeout_is_scored() -> None:
    trajectory = {"steps": [{"source": "agent", "message": "hi"}]}
    scored = diagnose_atif(
        trajectory, trial_id="t", trial_name="t", reward=1.0,
        exception_class="AgentTimeoutError",
    )
    assert scored.outcome == "scored"
    assert scored.reward == 1.0
    assert scored.exception_class == "AgentTimeoutError"
    assert scored.modes == ()
    infra = diagnose_atif(
        trajectory, trial_id="t", trial_name="t", reward=None,
        exception_class="AgentTimeoutError",
    )
    assert infra.outcome == "infra_failed"


def test_layer_schema_stamp() -> None:
    assert STEP_LAYERS_SCHEMA == "evallab.step_layers/v1"
    assert StitchStats().unique_steps == 0
