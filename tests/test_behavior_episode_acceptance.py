"""Adversarial acceptance coverage for the frozen BehaviorEpisode contract."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.behavior_calibration import calibrate_behavior_episodes
from evallab.behavior_catalog import load_behavior_catalog
from evallab.behavior_episodes import (
    BehaviorDetectionContext,
    detect_behavior_episodes,
    detect_effect_loop_candidates,
    deterministic_episode_id,
    load_behavior_episodes,
    normalize_behavior_actions,
    persist_behavior_episodes,
)
from evallab.evidence.event_mart import EventMartProjection
from evallab.labels import select_behavior_episode_review_queue
from evallab.phoenix_annotations import (
    publish_behavior_episodes,
    retrieve_reviewed_behavior_episodes,
)

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


class _FakePhoenixSpans:
    """Small fake of the official phoenix-client ``client.spans`` surface."""

    def __init__(self, reviewed: list[dict[str, Any]] | None = None) -> None:
        self.logged: list[Any] = []
        self.reviewed = reviewed or []
        self.log_calls: list[tuple[tuple[Any, ...], bool]] = []
        self.get_calls: list[dict[str, Any]] = []

    def log_span_annotations(self, *, span_annotations, sync: bool = True):
        batch = tuple(span_annotations)
        self.log_calls.append((batch, sync))
        self.logged.extend(batch)
        return tuple({"id": item["identifier"]} for item in batch)

    def get_span_annotations(
        self,
        *,
        span_ids,
        project_identifier: str,
        limit: int,
        include_annotation_names=None,
    ):
        self.get_calls.append(
            {
                "span_ids": tuple(span_ids),
                "project_identifier": project_identifier,
                "include_annotation_names": (
                    tuple(include_annotation_names) if include_annotation_names else ()
                ),
                "limit": limit,
            }
        )
        return self.reviewed


class _FakePhoenixClient:
    def __init__(self, reviewed: list[dict[str, Any]] | None = None) -> None:
        self.spans = _FakePhoenixSpans(reviewed)


def _item_field(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item[key]
    return getattr(item, key)


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


def test_phoenix_publish_and_reviewed_retrieve_preserve_identity_and_provenance(
    tmp_path: Path,
) -> None:
    episode = _result("recovered").episodes[0]
    reviewed_payload = {
        "id": "phoenix-annotation-41",
        "name": "evallab.behavior.recovered_progress",
        "updated_at": "2026-08-24T00:00:00Z",
        "span_id": "span-recover-1",
        "annotator_kind": "HUMAN",
        "source": "APP",
        "user_id": "reviewer-1",
        "result": {
            "label": "recovered_progress",
            "score": 0.92,
            "explanation": "Changed strategy succeeded with new state evidence.",
        },
        "metadata": {
            "schema_version": 1,
            "episode_id": episode.episode_id,
            "trial_id": episode.trial_id,
            "document_id": episode.document_id,
            "trajectory_id": episode.trajectory_id,
            "session_id": episode.session_id,
            "trace_id": "trace-behavior-acceptance",
            "root_span_id": "root-span",
            "start_step": episode.start_step,
            "end_step": episode.end_step,
            "evidence_step_ids": list(episode.evidence_step_ids),
            "evidence_span_ids": list(episode.evidence_span_ids),
            "annotator_kind": "human",
            "annotator_id": "reviewer-1",
            "detector_version": episode.detector_version,
            "rubric_version": episode.rubric_version,
            "catalog_version": episode.catalog_version,
            "source_sha256": episode.source_sha256,
            "input_digest": episode.input_digest,
            "provenance": {"kind": "human", "annotator_id": "reviewer-1"},
        },
    }
    client = _FakePhoenixClient([reviewed_payload])

    published = publish_behavior_episodes(
        client,
        [episode],
        project_name="behavior-acceptance",
        trace_id="trace-behavior-acceptance",
        root_span_id="root-span",
        span_ids_by_step={40: "span-recover-1", 41: "span-recover-2"},
    )

    assert published.published == 1
    assert published.annotation_ids == (episode.episode_id,)
    assert client.spans.log_calls[0][1] is True
    item = client.spans.logged[0]
    assert _item_field(item, "name") == "evallab.behavior.recovered_progress"
    metadata = _item_field(item, "metadata")
    assert metadata["trial_id"] == episode.trial_id
    assert metadata["document_id"] == episode.document_id
    assert metadata["trace_id"] == "trace-behavior-acceptance"
    assert _item_field(item, "span_id") == "span-recover-1"

    retrieved = retrieve_reviewed_behavior_episodes(
        client,
        project_name="behavior-acceptance",
        context=_context(),
        trace_id="trace-behavior-acceptance",
        root_span_id="root-span",
        span_ids_by_step={40: "span-recover-1", 41: "span-recover-2"},
    )

    assert retrieved[0].episode_id != episode.episode_id
    assert retrieved[0].trial_id == episode.trial_id
    assert retrieved[0].document_id == episode.document_id
    assert retrieved[0].annotator_kind == "human"
    assert retrieved[0].annotator_id == "reviewer-1"
    assert retrieved[0].provenance["source_episode_id"] == episode.episode_id
    assert retrieved[0].provenance["phoenix_annotation_id"] == "phoenix-annotation-41"
    stored = persist_behavior_episodes(
        [episode, retrieved[0]],
        derived_root=tmp_path / "derived",
    )
    assert {item.episode_id for item in stored} == {
        episode.episode_id,
        retrieved[0].episode_id,
    }
    assert {
        item.episode_id for item in load_behavior_episodes(derived_root=tmp_path / "derived")
    } == {episode.episode_id, retrieved[0].episode_id}
    wrong_span = {**reviewed_payload, "span_id": "span-recover-2"}
    assert (
        retrieve_reviewed_behavior_episodes(
            _FakePhoenixClient([wrong_span]),
            project_name="behavior-acceptance",
            context=_context(),
            trace_id="trace-behavior-acceptance",
            root_span_id="root-span",
            span_ids_by_step={40: "span-recover-1", 41: "span-recover-2"},
        )
        == ()
    )

    context_without_optional_ids = BehaviorDetectionContext(
        trial_id=episode.trial_id,
        document_id=episode.document_id,
        source_sha256=episode.source_sha256,
        observed_at=episode.created_at,
    )
    wrong_optional_identity = {
        **reviewed_payload,
        "metadata": {
            **reviewed_payload["metadata"],
            "trajectory_id": "other-trajectory",
            "session_id": "other-session",
        },
    }
    assert (
        retrieve_reviewed_behavior_episodes(
            _FakePhoenixClient([wrong_optional_identity]),
            project_name="behavior-acceptance",
            context=context_without_optional_ids,
            trace_id="trace-behavior-acceptance",
            root_span_id="root-span",
            span_ids_by_step={40: "span-recover-1", 41: "span-recover-2"},
        )
        == ()
    )


def test_catalog_exposes_exact_calibrated_dimensions() -> None:
    catalog = load_behavior_catalog()

    assert set(catalog.behaviors) == {
        "tool_error",
        "unchanged_retry",
        "recovered_progress",
        "verification_gap",
    }
    assert "effect_loop_candidate" not in catalog.behaviors


def test_review_queue_and_calibration_keep_experimental_dimension_separate() -> None:
    episode = _result("recovered").episodes[0]
    queue = select_behavior_episode_review_queue([episode], limit=1)
    report = calibrate_behavior_episodes([], [])

    assert len(queue) == 1
    assert queue[0].behavior == "recovered_progress"
    assert set(report.per_behavior) == {
        "tool_error",
        "unchanged_retry",
        "recovered_progress",
        "verification_gap",
    }


def test_calibration_uses_only_explicit_human_routes_and_unresolved_negative() -> None:
    candidate = _result("recovered").episodes[0]
    human_provenance = {"source": "human", "annotator_id": "reviewer-negative"}

    def human_route(
        *,
        label: str,
        start_step: int,
        end_step: int,
        evidence_step_ids: tuple[int, ...],
        evidence_span_ids: tuple[str, ...],
    ):
        episode_id = deterministic_episode_id(
            candidate.trial_id,
            candidate.document_id,
            start_step,
            end_step,
            label,
            evidence_step_ids=evidence_step_ids,
            evidence_span_ids=evidence_span_ids,
            detector_version=candidate.detector_version,
            catalog_version=candidate.catalog_version,
            annotator_kind="human",
            annotator_id="reviewer-negative",
            trajectory_id=candidate.trajectory_id,
            session_id=candidate.session_id,
            provenance=human_provenance,
        )
        return candidate.model_copy(
            update={
                "episode_id": episode_id,
                "start_step": start_step,
                "end_step": end_step,
                "label": label,
                "status": "confirmed",
                "evidence_step_ids": evidence_step_ids,
                "evidence_span_ids": evidence_span_ids,
                "annotator_kind": "human",
                "annotator_id": "reviewer-negative",
                "provenance": human_provenance,
                "reviewed_at": candidate.updated_at,
            }
        )

    human_same_route = human_route(
        label="unresolved_error",
        start_step=candidate.start_step,
        end_step=candidate.end_step,
        evidence_step_ids=candidate.evidence_step_ids,
        evidence_span_ids=candidate.evidence_span_ids,
    )
    human_missing_candidate = human_route(
        label="unresolved_error",
        start_step=90,
        end_step=90,
        evidence_step_ids=(90,),
        evidence_span_ids=("span-human-negative",),
    )
    report = calibrate_behavior_episodes(
        [candidate],
        [human_same_route, human_missing_candidate],
    )

    recovered = report.per_behavior["recovered_progress"]
    assert (recovered.tp, recovered.fp, recovered.fn, recovered.tn) == (0, 1, 0, 1)
    assert recovered.precision == 0.0
    assert recovered.recall is None
    for behavior, counts in report.per_behavior.items():
        if behavior != "recovered_progress":
            assert counts.evaluated_count == 0
            assert counts.precision is None
            assert counts.recall is None
