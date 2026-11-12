"""Adversarial acceptance coverage for the frozen BehaviorEpisode contract."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.behavior_episodes import (
    BehaviorDetectionContext,
    detect_behavior_episodes,
    detect_effect_loop_candidates,
    load_behavior_episodes,
    normalize_behavior_actions,
    persist_behavior_episodes,
)
from evallab.evidence.event_mart import EventMartProjection

ROOT = Path(__file__).resolve().parents[1]
ACTION_FIXTURE = Path(__file__).parent / "fixtures/behavior_episodes/normalized_actions.json"
REAL_ATIF = ROOT / "research/explorations/harbor-021/fixtures/trajectory.json"


def _context() -> BehaviorDetectionContext:
    return BehaviorDetectionContext(
        trial_id="trial-behavior-acceptance",
        document_id="document-behavior-acceptance",
        trajectory_id="trajectory-behavior-acceptance",
        session_id="session-behavior-acceptance",
        source_sha256="a" * 64,
        observed_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _rows(name: str) -> list[dict[str, Any]]:
    payload = json.loads(ACTION_FIXTURE.read_text())
    rows = payload[name]
    # Span IDs are normalized action identity, not synthetic task evidence.
    return [{"span_id": f"span-{row['action_id']}", **row} for row in rows]


def _actions(name: str, *, effects: tuple[dict[str, Any], ...] = ()):
    return normalize_behavior_actions(
        EventMartProjection(
            trajectory_events=(),
            agent_actions=tuple(_rows(name)),
            llm_calls=(),
            trajectory_phases=(),
            action_effects=effects,
        )
    )


def _result(name: str, *, effects: tuple[dict[str, Any], ...] = ()):
    return detect_behavior_episodes(_context(), _actions(name, effects=effects))


def _labels(result) -> set[str]:
    return {episode.label for episode in result.episodes}


def test_polling_is_not_an_unchanged_retry_or_effect_loop() -> None:
    result = _result("polling")
    loop_result = detect_effect_loop_candidates(_context(), _actions("polling"))

    assert "unchanged_retry" not in _labels(result)
    assert "effect_loop_candidate" not in _labels(loop_result)


def test_update_status_is_a_mutation_not_implicit_polling() -> None:
    action = normalize_behavior_actions(
        EventMartProjection(
            trajectory_events=(),
            agent_actions=(
                {
                    "action_id": "update-status",
                    "step_id": 12,
                    "function_name": "update_status",
                    "action_family": "edit",
                    "arguments_sha256": "ab" * 32,
                    "observation_sha256": "cd" * 32,
                    "outcome": "success",
                    "exit_code": 0,
                },
            ),
            llm_calls=(),
            trajectory_phases=(),
            action_effects=(),
        )
    )[0]

    assert action.intent == "mutation"


def test_corrected_arguments_are_not_unchanged_retry() -> None:
    result = _result("corrected_args")
    assert "unchanged_retry" not in _labels(result)


def test_unchanged_retry_can_follow_an_intervening_action() -> None:
    result = _result("intervening_retry")
    retries = [episode for episode in result.episodes if episode.label == "unchanged_retry"]

    assert len(retries) == 1
    assert retries[0].evidence_step_ids == (110, 112)


def test_unrelated_success_does_not_claim_recovery() -> None:
    temporal_link = (
        {
            "action_id": "fail-1",
            "effect_id": "effect-from-later-read",
            "link_method": "temporal_precedence",
        },
    )
    result = _result("unrelated_success", effects=temporal_link)

    assert "recovered_progress" not in _labels(result)
    assert any(episode.label == "unresolved_error" for episode in result.episodes)


def test_real_atif_bytes_remain_immutable_while_actions_are_processed(tmp_path: Path) -> None:
    before = REAL_ATIF.read_bytes()
    document = json.loads(before)
    tool_call = next(call for step in document["steps"] for call in step.get("tool_calls", []))
    step_id = next(
        step["step_id"] for step in document["steps"] if tool_call in step.get("tool_calls", [])
    )
    arguments_digest = hashlib.sha256(
        json.dumps(tool_call.get("arguments", {}), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    projection = EventMartProjection(
        trajectory_events=(),
        agent_actions=(
            {
                "action_id": tool_call["tool_call_id"],
                "step_id": step_id,
                "span_id": None,
                "function_name": tool_call["function_name"],
                "action_family": "other",
                "arguments_sha256": arguments_digest,
                "observation_sha256": None,
                "outcome": "unknown",
                "exit_code": None,
            },
        ),
        llm_calls=(),
        trajectory_phases=(),
        action_effects=(),
    )
    actions = normalize_behavior_actions(projection)
    result = detect_behavior_episodes(
        BehaviorDetectionContext(
            trial_id="real-atif-trial",
            document_id="real-atif-document",
            trajectory_id=document.get("trajectory_id"),
            session_id=document.get("session_id"),
            source_sha256=hashlib.sha256(before).hexdigest(),
            observed_at=datetime.fromisoformat(
                document["steps"][0]["timestamp"].replace("Z", "+00:00")
            ),
        ),
        actions,
    )
    persist_behavior_episodes(result.episodes, derived_root=tmp_path / "derived")

    assert hashlib.sha256(REAL_ATIF.read_bytes()).hexdigest() == hashlib.sha256(before).hexdigest()
    assert REAL_ATIF.read_bytes() == before


def test_changed_strategy_relevant_success_with_new_evidence_recovers() -> None:
    result = _result("recovered")

    recovered = [episode for episode in result.episodes if episode.label == "recovered_progress"]
    assert len(recovered) == 1
    assert recovered[0].start_step == 40
    assert recovered[0].end_step == 41
    assert recovered[0].evidence_step_ids == (40, 41)
    assert recovered[0].evidence_span_ids == ("span-recover-1", "span-recover-2")


def test_final_mutation_without_later_explicit_verification_is_a_gap() -> None:
    gap = _result("verification_gap")
    verified = _result("verification_success")

    assert "verification_gap" in _labels(gap)
    assert "verification_gap" not in _labels(verified)


def test_missing_observation_or_relevance_is_unknown_not_tool_error() -> None:
    result = _result("unknown")

    assert "tool_error" not in _labels(result)
    assert result.unknowns
    assert all(unknown.reason for unknown in result.unknowns)


def test_effect_loop_requires_explicit_no_new_state_evidence() -> None:
    result = detect_effect_loop_candidates(_context(), _actions("effect_loop"))

    loops = [episode for episode in result.episodes if episode.label == "effect_loop_candidate"]
    assert len(loops) == 1
    assert loops[0].status == "candidate"
    assert loops[0].start_step == 80
    assert loops[0].end_step == 81


def test_effect_loop_is_bounded_by_state_coverage() -> None:
    bounded = detect_effect_loop_candidates(_context(), _actions("bounded_loop"))
    unknown = detect_effect_loop_candidates(_context(), _actions("loop_unknown"))

    assert "effect_loop_candidate" not in _labels(bounded)
    assert any(item.behavior == "effect_loop_candidate" for item in unknown.unknowns)


def test_duplicate_persistence_is_byte_identical_and_idempotent(tmp_path: Path) -> None:
    episodes = _result("recovered").episodes
    derived = tmp_path / "derived"

    first = persist_behavior_episodes(episodes, derived_root=derived)
    parquet = derived / "behavior_episodes/behavior_episodes.parquet"
    first_bytes = parquet.read_bytes()
    first_digest = hashlib.sha256(first_bytes).hexdigest()
    second = persist_behavior_episodes(episodes, derived_root=derived)

    assert first == second
    assert hashlib.sha256(parquet.read_bytes()).hexdigest() == first_digest
    assert load_behavior_episodes(derived_root=derived) == list(first)


def test_episode_provenance_is_deeply_immutable() -> None:
    episode = _result("recovered").episodes[0]
    payload = episode.model_dump(mode="python")
    payload["provenance"] = {"source": {"kind": "code"}}
    immutable = type(episode).model_validate(payload)

    with pytest.raises(TypeError):
        immutable.provenance["new"] = "value"
    with pytest.raises(TypeError):
        immutable.provenance["source"]["kind"] = "human"


