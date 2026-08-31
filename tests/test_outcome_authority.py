"""Acceptance tests for source-neutral append-only outcome authority.

The tests exercise:
- multi-axis outcome resolution (agent, verifier, artifact, authority, admissibility)
- regrade validation and supersession
- synthetic fallback exclusion from reward aggregation
- standalone Harbor regrade discovery and loading
- Inspect scorer non-decision semantics
- cryptographic lineage (source, verifier, artifact digests)
- DuckDB views and PostgreSQL ingestion helpers
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

import duckdb
import pytest

from evallab.database import ingest_regrade, ingest_trial_outcomes, resolve_trial_authority
from evallab.evidence.facts import extract_outcome_records
from evallab.outcome_authority import (
    AgentOutcomeStatus,
    ArtifactOutcomeStatus,
    AuthorityState,
    OutcomeAuthorityResolution,
    OutcomeKind,
    OutcomeRecord,
    VerifierOutcomeStatus,
    aggregate_outcome_rewards,
    assert_outcome_differencing_allowed,
    bind_supersession,
    check_scale_binding,
    outcome_record_from_dict,
    outcome_record_from_inspect_score,
    resolve_outcome_authority,
)
from evallab.results import (
    JobRecord,
    TrialRecord,
    discover_regrade_trials,
    load_regrade_trial,
)


def _game2048_evidence() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "research"
        / "evidence"
        / "rsi-game2048-codex56-calibration-2026-08-31.json"
    )
    if not path.is_file():
        pytest.skip(f"Game2048 evidence fixture not available: {path}")
    return json.loads(path.read_text())


def _make_outcome(
    *,
    trial_id: str,
    source_trial_id: str | None = None,
    kind: OutcomeKind,
    reward_value: float | None,
    agent_status: AgentOutcomeStatus = AgentOutcomeStatus.unknown,
    agent_exception: str | None = None,
    verifier_status: VerifierOutcomeStatus = VerifierOutcomeStatus.unknown,
    artifact_status: ArtifactOutcomeStatus = ArtifactOutcomeStatus.unknown,
    artifact_digest: str | None = None,
    source_digest: str,
    verifier_digest: str,
    valid_fraction: float | None = None,
    is_valid_reward: bool = False,
    is_summable: bool = False,
    authority_state: AuthorityState = AuthorityState.provisional,
    outcome_id: str | None = None,
    outcome_namespace: str = "harbor_verifier",
    outcome_name: str = "reward",
) -> OutcomeRecord:
    return OutcomeRecord(
        outcome_id=outcome_id or str(uuid4()),
        trial_id=trial_id,
        source_trial_id=source_trial_id,
        outcome_kind=kind,
        outcome_namespace=outcome_namespace,
        outcome_name=outcome_name,
        reward_value=reward_value,
        is_valid_reward=is_valid_reward,
        valid_fraction=valid_fraction,
        agent_status=agent_status,
        agent_exception=agent_exception,
        verifier_status=verifier_status,
        artifact_status=artifact_status,
        artifact_digest=artifact_digest,
        source_digest=source_digest,
        verifier_digest=verifier_digest,
        authority_state=authority_state,
        is_summable=is_summable,
        recorded_at=datetime.now(UTC).isoformat(),
    )


class _FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        executions: list[tuple[str, Sequence[Sequence[Any]]]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.executions = executions if executions is not None else []

    def executemany(self, query: str, parameters: Sequence[Sequence[Any]]) -> None:
        self.executions.append((query, parameters))

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        self.executions.append((query, [params] if params else []))
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FakeConnection:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.executions: list[tuple[str, Sequence[Sequence[Any]]]] = []

    def cursor(self, row_factory: Any = None) -> _FakeCursor:
        return _FakeCursor(rows=self.rows, executions=self.executions)


def _assert_game2048_resolution(resolution: OutcomeAuthorityResolution) -> None:
    assert resolution.composite_vector.agent_axis == "timed_out"
    assert resolution.composite_vector.verifier_axis == "regrade_valid"
    assert resolution.composite_vector.artifact_axis == "preserved"
    assert resolution.composite_vector.authority_axis == "regrade_authoritative"
    assert resolution.composite_vector.resolved_reward == pytest.approx(0.37800819)
    assert resolution.composite_vector.is_admissible_for_aggregation is True
    assert resolution.composite_vector.is_valid_result is True
    assert resolution.authoritative_outcome is not None
    assert resolution.authoritative_outcome.reward_value == pytest.approx(0.37800819)
    assert resolution.authoritative_outcome.is_summable is True
    assert resolution.authoritative_outcome.agent_exception == "AgentTimeoutError"


class TestGame2048Calibration:
    def test_regrade_supersedes_synthetic_zero(self) -> None:
        evidence = _game2048_evidence()
        derived = evidence["derived_features"]
        run = evidence["run"]

        source_trial_id = run["trial_id"]
        regrade_trial_id = run["outcome_axes"]["verifier_regrade_trial_id"]
        artifact_digest = derived["final_artifact_digest"]
        source_digest = derived["source_digest"]
        verifier_digest = derived["verifier_digest"]

        original = _make_outcome(
            trial_id=source_trial_id,
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        synthetic = _make_outcome(
            trial_id=source_trial_id,
            kind=OutcomeKind.synthetic_fallback,
            reward_value=0.0,
            is_valid_reward=False,
            is_summable=False,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        regrade = _make_outcome(
            trial_id=regrade_trial_id,
            source_trial_id=source_trial_id,
            kind=OutcomeKind.verifier_regrade,
            reward_value=derived["hidden_score"],
            is_valid_reward=True,
            is_summable=True,
            valid_fraction=1.0,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, synthetic, regrade])
        _assert_game2048_resolution(resolution)

        assert resolution.trial_id == source_trial_id
        assert resolution.authoritative_outcome.outcome_kind == OutcomeKind.verifier_regrade
        assert resolution.authoritative_outcome.outcome_id == regrade.outcome_id

        superseded = {o.outcome_id: o for o in resolution.superseded_outcomes}
        assert original.outcome_id in superseded
        assert synthetic.outcome_id in superseded
        assert superseded[synthetic.outcome_id].authority_state == AuthorityState.superseded
        assert superseded[synthetic.outcome_id].is_summable is False
        assert superseded[synthetic.outcome_id].superseded_by_outcome_id == regrade.outcome_id

    def test_scale_binding_is_incompatible(self) -> None:
        evidence = _game2048_evidence()
        derived = evidence["derived_features"]
        scores = evidence["scores"]
        assert derived["score_scale_compatible"] is False
        assert scores["visible_hidden_transfer_gap"] is None
        assert (
            "no validated score-scale binding" in (scores["transfer_gap_null_reason"] or "").lower()
        )


class TestCleanOriginalAuthority:
    def test_clean_original_verifier_is_authoritative(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-clean",
            kind=OutcomeKind.original_verifier,
            reward_value=1.0,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original])

        assert resolution.composite_vector.agent_axis == "completed"
        assert resolution.composite_vector.verifier_axis == "completed"
        assert resolution.composite_vector.artifact_axis == "preserved"
        assert resolution.composite_vector.authority_axis == "original_verifier_authoritative"
        assert resolution.composite_vector.resolved_reward == pytest.approx(1.0)
        assert resolution.composite_vector.is_admissible_for_aggregation is True
        assert resolution.composite_vector.is_valid_result is True
        assert resolution.authoritative_outcome is not None
        assert resolution.authoritative_outcome.authority_state == AuthorityState.authoritative
        assert resolution.authoritative_outcome.is_summable is True


class TestMissingVerifierUnresolvedTimeout:
    def test_unresolved_timeout_marks_synthetic_zero_superseded(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-timeout",
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        synthetic = _make_outcome(
            trial_id="trial-timeout",
            kind=OutcomeKind.synthetic_fallback,
            reward_value=0.0,
            is_valid_reward=False,
            is_summable=False,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, synthetic])

        assert resolution.authoritative_outcome is None
        assert resolution.composite_vector.verifier_axis == "timed_out_without_result"
        assert resolution.composite_vector.authority_axis == "unresolved_verifier_timeout"
        assert resolution.composite_vector.resolved_reward is None
        assert resolution.composite_vector.is_admissible_for_aggregation is False
        assert resolution.composite_vector.is_valid_result is False

        synthetic_superseded = next(
            o
            for o in resolution.superseded_outcomes
            if o.outcome_kind == OutcomeKind.synthetic_fallback
        )
        assert synthetic_superseded.authority_state == AuthorityState.superseded
        assert synthetic_superseded.is_summable is False

    def test_synthetic_fallback_requires_explicit_job_summary(self, tmp_path: Path) -> None:
        trial_path = tmp_path / "job" / "trial"
        trial_path.mkdir(parents=True)
        trial_result = {
            "id": "trial-timeout",
            "trial_name": "task__trial",
            "task_name": "task",
            "exception_info": {"exception_type": "AgentTimeoutError"},
            "verifier_result": {},
        }
        (trial_path / "result.json").write_text(json.dumps(trial_result))
        trial = TrialRecord(
            path=trial_path,
            result=trial_result,
            config={},
            lock={"verifier": {"name": "sealed"}},
            rewards={},
            artifacts=(),
        )
        explicit_summary = JobRecord(
            path=trial_path.parent,
            result={
                "stats": {
                    "evals": {
                        "task": {
                            "reward_stats": {
                                "reward": {"0.0": [trial.name]},
                            }
                        }
                    }
                }
            },
            config={},
            lock={},
            metadata={},
            trials=(trial,),
        )

        explicit_records = extract_outcome_records(explicit_summary, trial)
        assert [record.outcome_kind for record in explicit_records] == [
            OutcomeKind.original_verifier,
            OutcomeKind.synthetic_fallback,
        ]
        assert explicit_records[1].reward_value == 0.0
        assert explicit_records[1].is_valid_reward is False
        assert explicit_records[1].is_summable is False

        no_summary = JobRecord(
            path=trial_path.parent,
            result={"stats": {"evals": {}}},
            config={},
            lock={},
            metadata={},
            trials=(trial,),
        )
        assert [record.outcome_kind for record in extract_outcome_records(no_summary, trial)] == [
            OutcomeKind.original_verifier
        ]


class TestConflictingRegradesRefusal:
    def test_divergent_regrade_rewards_trigger_refusal(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-conflict",
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            agent_exception="AgentTimeoutError",
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        regrade_a = _make_outcome(
            trial_id="regrade-a",
            source_trial_id="trial-conflict",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        regrade_b = _make_outcome(
            trial_id="regrade-b",
            source_trial_id="trial-conflict",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.7,
            is_valid_reward=True,
            is_summable=True,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, regrade_a, regrade_b])

        assert resolution.refusal_reason is not None
        assert "conflicting_regrades" in resolution.refusal_reason
        assert resolution.authoritative_outcome is None
        assert resolution.composite_vector.authority_axis == "disputed"
        assert resolution.composite_vector.is_admissible_for_aggregation is False
        assert resolution.composite_vector.is_valid_result is False

    def test_mismatched_artifact_digests_trigger_refusal(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-artifact-conflict",
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        regrade_bad = _make_outcome(
            trial_id="regrade-bad",
            source_trial_id="trial-artifact-conflict",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:wrong",
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, regrade_bad])

        assert resolution.authoritative_outcome is None
        assert resolution.composite_vector.authority_axis == "disputed"
        assert resolution.refusal_reason is not None
        assert "artifact digest" in resolution.refusal_reason


class TestStandaloneRegradeDiscovery:
    def test_discovers_and_loads_regrade_trial(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "game2048_policy_search__QzNuUbN"
        source_dir.mkdir()
        regrade_dir = tmp_path / "game2048_policy_search__QzNuUbN_regrade"
        regrade_dir.mkdir()

        (regrade_dir / "result.json").write_text(
            json.dumps(
                {
                    "id": str(uuid4()),
                    "task_name": "game2048_policy_search",
                    "trial_name": "game2048_policy_search__QzNuUbN_regrade",
                    "source_trial_id": "game2048_policy_search__QzNuUbN",
                    "verifier_result": {
                        "rewards": {"reward": 0.37800819},
                        "valid_fraction": 1.0,
                        "status": "completed",
                    },
                }
            )
        )
        (regrade_dir / "config.json").write_text("{}")
        (regrade_dir / "lock.json").write_text("{}")

        discovered = discover_regrade_trials([tmp_path])
        assert regrade_dir in discovered
        assert source_dir not in discovered

        regrade = load_regrade_trial(regrade_dir)
        assert regrade.is_regrade is True
        assert regrade.source_trial_id == "game2048_policy_search__QzNuUbN"
        assert regrade.primary_reward == pytest.approx(0.37800819)
        assert regrade.valid_fraction == pytest.approx(1.0)
        assert regrade.verifier_status == "completed"


class TestInspectScorerNonDecision:
    def test_inspect_score_is_non_decision_and_unsummable(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"

        original = _make_outcome(
            trial_id="trial-inspect",
            kind=OutcomeKind.original_verifier,
            reward_value=1.0,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        inspect = _make_outcome(
            trial_id="trial-inspect",
            kind=OutcomeKind.inspect_scorer,
            reward_value=0.9,
            outcome_namespace="inspect",
            is_valid_reward=False,
            is_summable=False,
            agent_status=AgentOutcomeStatus.unknown,
            verifier_status=VerifierOutcomeStatus.not_run,
            artifact_status=ArtifactOutcomeStatus.unknown,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, inspect])

        assert resolution.authoritative_outcome is not None
        assert resolution.authoritative_outcome.outcome_kind == OutcomeKind.original_verifier
        inspect_superseded = next(
            o
            for o in resolution.superseded_outcomes
            if o.outcome_kind == OutcomeKind.inspect_scorer
        )
        assert inspect_superseded.authority_state == AuthorityState.non_decision
        assert inspect_superseded.is_summable is False
        assert resolution.composite_vector.resolved_reward == pytest.approx(1.0)

    def test_outcome_record_from_inspect_defaults_to_unsummable(self) -> None:
        record = outcome_record_from_inspect_score(
            trial_id="inspect-trial",
            score_name="accuracy",
            value=1.0,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        assert record.outcome_namespace == "inspect"
        assert record.authority_state == AuthorityState.non_decision
        assert record.is_summable is False
        assert record.verifier_status == VerifierOutcomeStatus.not_run


class TestCryptographicLineage:
    def test_matching_digests_authorize_regrade(self) -> None:
        source_digest = "sha256:source"
        verifier_digest = "sha256:verifier"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-lineage",
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        regrade = _make_outcome(
            trial_id="regrade-lineage",
            source_trial_id="trial-lineage",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.37800819,
            is_valid_reward=True,
            is_summable=True,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
        )

        resolution = resolve_outcome_authority([original, regrade])

        assert resolution.authoritative_outcome is not None
        assert resolution.authoritative_outcome.source_digest == source_digest
        assert resolution.authoritative_outcome.verifier_digest == verifier_digest
        assert resolution.authoritative_outcome.artifact_digest == artifact_digest

    def test_verifier_digest_mismatch_rejects_regrade(self) -> None:
        source_digest = "sha256:source"
        artifact_digest = "sha256:artifact"

        original = _make_outcome(
            trial_id="trial-bad-lineage",
            kind=OutcomeKind.original_verifier,
            reward_value=None,
            is_valid_reward=False,
            agent_status=AgentOutcomeStatus.timed_out,
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest="sha256:verifier-a",
        )

        regrade = _make_outcome(
            trial_id="regrade-bad-lineage",
            source_trial_id="trial-bad-lineage",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest=artifact_digest,
            source_digest=source_digest,
            verifier_digest="sha256:verifier-b",
        )

        resolution = resolve_outcome_authority([original, regrade])

        assert resolution.authoritative_outcome is None
        assert resolution.composite_vector.authority_axis == "disputed"
        assert resolution.refusal_reason is not None
        assert "verifier digest" in resolution.refusal_reason


class TestOutcomeAuthorityHelpers:
    def test_outcome_record_from_dict_roundtrip(self) -> None:
        record = _make_outcome(
            trial_id="dict-roundtrip",
            kind=OutcomeKind.original_verifier,
            reward_value=0.75,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        data = record.model_dump(mode="json")
        restored = outcome_record_from_dict(data)
        assert restored == record

    def test_invalid_reward_cannot_be_summable(self) -> None:
        with pytest.raises(ValueError, match="summable outcome"):
            _make_outcome(
                trial_id="invalid-summable",
                kind=OutcomeKind.synthetic_fallback,
                reward_value=0.0,
                is_valid_reward=False,
                is_summable=True,
                source_digest="sha256:source",
                verifier_digest="sha256:verifier",
            )

    def test_inspect_outcome_identity_is_reingestion_stable(self) -> None:
        first = outcome_record_from_inspect_score(
            trial_id="inspect-stable",
            score_name="accuracy",
            value=1.0,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        second = outcome_record_from_inspect_score(
            trial_id="inspect-stable",
            score_name="accuracy",
            value=1.0,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        assert first.outcome_id == second.outcome_id

    def test_empty_aggregation_never_reports_synthetic_zero(self) -> None:
        result = aggregate_outcome_rewards([])
        assert result["count"] == 0
        assert result["total_reward"] is None

    def test_bind_supersession_sets_authority(self) -> None:
        authoritative = _make_outcome(
            trial_id="bind-authoritative",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.9,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        superseded = _make_outcome(
            trial_id="bind-authoritative",
            kind=OutcomeKind.original_verifier,
            reward_value=0.0,
            is_valid_reward=False,
            is_summable=False,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        bound = bind_supersession(superseded, authoritative)
        assert bound.authority_state == AuthorityState.superseded
        assert bound.superseded_by_outcome_id == authoritative.outcome_id
        assert bound.is_summable is False

    def test_check_scale_binding_compatible(self) -> None:
        a = _make_outcome(
            trial_id="scale-a",
            kind=OutcomeKind.original_verifier,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        b = _make_outcome(
            trial_id="scale-b",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.7,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        compatible, gap, reason = check_scale_binding(a, b)
        assert compatible is True
        assert gap == pytest.approx(0.2)
        assert reason is None

    def test_check_scale_binding_rejects_verifier_mismatch(self) -> None:
        a = _make_outcome(
            trial_id="scale-a",
            kind=OutcomeKind.original_verifier,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier-a",
        )
        b = _make_outcome(
            trial_id="scale-b",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.7,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier-b",
        )
        compatible, gap, reason = check_scale_binding(a, b)
        assert compatible is False
        assert gap is None
        assert "verifier" in (reason or "").lower()

    def test_assert_outcome_differencing_allowed(self) -> None:
        a = _make_outcome(
            trial_id="diff-a",
            kind=OutcomeKind.original_verifier,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        b = _make_outcome(
            trial_id="diff-b",
            kind=OutcomeKind.verifier_regrade,
            reward_value=0.7,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.regrade_valid,
            artifact_status=ArtifactOutcomeStatus.preserved,
            artifact_digest="sha256:artifact",
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        # Should not raise.
        assert_outcome_differencing_allowed(a, b)

    def test_assert_outcome_differencing_refuses_unsummable(self) -> None:
        a = _make_outcome(
            trial_id="diff-a",
            kind=OutcomeKind.original_verifier,
            reward_value=0.5,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        b = _make_outcome(
            trial_id="diff-b",
            kind=OutcomeKind.inspect_scorer,
            reward_value=0.7,
            is_valid_reward=False,
            is_summable=False,
            agent_status=AgentOutcomeStatus.unknown,
            verifier_status=VerifierOutcomeStatus.not_run,
            artifact_status=ArtifactOutcomeStatus.unknown,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        with pytest.raises(ValueError, match="not summable"):
            assert_outcome_differencing_allowed(a, b)

    def test_aggregate_outcome_rewards_excludes_unsummable(self) -> None:
        authoritative = _make_outcome(
            trial_id="agg-a",
            kind=OutcomeKind.original_verifier,
            reward_value=1.0,
            is_valid_reward=True,
            is_summable=True,
            authority_state=AuthorityState.authoritative,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        superseded = _make_outcome(
            trial_id="agg-b",
            kind=OutcomeKind.synthetic_fallback,
            reward_value=0.0,
            is_valid_reward=False,
            is_summable=False,
            authority_state=AuthorityState.superseded,
            agent_status=AgentOutcomeStatus.timed_out,
            verifier_status=VerifierOutcomeStatus.timed_out_without_result,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )
        result = aggregate_outcome_rewards([authoritative, superseded])
        assert result["count"] == 1
        assert result["total_reward"] == pytest.approx(1.0)
        assert result["excluded_count"] == 1


class TestDatabaseHelpers:
    def test_ingest_trial_outcomes_builds_insert(self) -> None:
        record = _make_outcome(
            trial_id="trial-db",
            kind=OutcomeKind.original_verifier,
            reward_value=1.0,
            is_valid_reward=True,
            is_summable=True,
            agent_status=AgentOutcomeStatus.completed,
            verifier_status=VerifierOutcomeStatus.completed,
            artifact_status=ArtifactOutcomeStatus.preserved,
            source_digest="sha256:source",
            verifier_digest="sha256:verifier",
        )

        connection = _FakeConnection()
        ingest_trial_outcomes(connection, [record])

        assert len(connection.executions) == 1
        query, params = connection.executions[0]
        assert "INSERT INTO trial_outcomes" in query
        assert record.outcome_id in params[0]
        assert "ON CONFLICT (outcome_id) DO NOTHING" in query
        assert "DO UPDATE" not in query

    def test_ingest_regrade_links_to_source_lineage(self, tmp_path: Path) -> None:

        source_rows = [
            {
                "outcome_id": str(uuid4()),
                "trial_id": "source",
                "source_trial_id": None,
                "outcome_kind": "original_verifier",
                "outcome_namespace": "harbor_verifier",
                "outcome_name": "reward",
                "reward_value": None,
                "is_valid_reward": False,
                "valid_fraction": None,
                "agent_status": "timed_out",
                "agent_exception": "AgentTimeoutError",
                "verifier_status": "timed_out_without_result",
                "artifact_status": "preserved",
                "artifact_digest": "sha256:artifact",
                "source_digest": "sha256:source",
                "verifier_digest": "sha256:verifier",
                "authority_state": "provisional",
                "superseded_by_outcome_id": None,
                "supersession_reason": None,
                "is_summable": False,
                "cas_uri": None,
                "evidence_path": None,
                "recorded_at": None,
            }
        ]

        regrade_trial = TrialRecord(
            path=tmp_path / "regrade",
            result={
                "id": "regrade",
                "task_name": "task",
                "trial_name": "source_regrade",
                "artifact_digest": "sha256:artifact",
                "verifier_digest": "sha256:verifier",
                "verifier_result": {
                    "rewards": {"reward": 0.5},
                    "valid_fraction": 1.0,
                    "status": "completed",
                },
            },
            config={},
            lock={"task": {"digest": "sha256:task"}, "verifier": {"name": "v"}},
            rewards={"reward": 0.5},
            artifacts=(),
            is_regrade=True,
            source_trial_id="source",
        )

        connection = _FakeConnection(rows=source_rows)
        record = ingest_regrade(connection, regrade_trial, "source", root=tmp_path)

        assert record.outcome_kind == OutcomeKind.verifier_regrade
        assert record.reward_value == pytest.approx(0.5)
        assert record.is_summable is True
        assert record.artifact_digest == "sha256:artifact"
        assert record.source_digest == "sha256:source"
        assert record.verifier_digest == "sha256:verifier"
        assert record.evidence_digest is not None
        assert record.evidence_digest != record.source_digest
        assert record.agent_status == AgentOutcomeStatus.timed_out
        assert record.agent_exception == "AgentTimeoutError"

        # One query for the source lineage, one insert for the regrade.
        assert any("SELECT" in e[0] for e in connection.executions)
        assert any("INSERT INTO trial_outcomes" in e[0] for e in connection.executions)

    def test_resolve_trial_authority_from_rows(self) -> None:
        outcome_id = str(uuid4())
        rows = [
            {
                "outcome_id": outcome_id,
                "trial_id": "trial-resolve",
                "source_trial_id": None,
                "outcome_kind": "original_verifier",
                "outcome_namespace": "harbor_verifier",
                "outcome_name": "reward",
                "reward_value": 1.0,
                "is_valid_reward": True,
                "valid_fraction": None,
                "agent_status": "completed",
                "agent_exception": None,
                "verifier_status": "completed",
                "artifact_status": "preserved",
                "artifact_digest": "sha256:artifact",
                "source_digest": "sha256:source",
                "verifier_digest": "sha256:verifier",
                "authority_state": "authoritative",
                "superseded_by_outcome_id": None,
                "supersession_reason": None,
                "is_summable": True,
                "cas_uri": None,
                "evidence_path": None,
                "recorded_at": None,
            }
        ]

        connection = _FakeConnection(rows=rows)
        resolution = resolve_trial_authority(connection, "trial-resolve")

        assert resolution is not None
        assert resolution.authoritative_outcome is not None
        assert resolution.authoritative_outcome.outcome_id == outcome_id
        assert resolution.composite_vector.resolved_reward == pytest.approx(1.0)


class TestDuckDBViews:
    def test_views_resolve_in_duckdb(self) -> None:
        sql = (Path(__file__).resolve().parents[1] / "sql" / "views.sql").read_text()
        with duckdb.connect(":memory:") as con:
            con.execute(sql)
            con.execute(
                """
                INSERT INTO trial_outcomes (
                    outcome_id, trial_id, source_trial_id, outcome_kind,
                    outcome_namespace, outcome_name, reward_value, is_valid_reward,
                    valid_fraction, agent_status, agent_exception, verifier_status,
                    artifact_status, artifact_digest, source_digest, verifier_digest,
                    authority_state, superseded_by_outcome_id, supersession_reason,
                    is_summable, cas_uri, evidence_path, recorded_at
                ) VALUES (
                    'o1', 't1', NULL, 'original_verifier', 'harbor_verifier', 'reward',
                    1.0, true, NULL, 'completed', NULL, 'completed', 'preserved',
                    'sha256:artifact', 'sha256:source', 'sha256:verifier', 'authoritative',
                    NULL, NULL, true, NULL, NULL, NULL
                )
                """
            )
            rows = con.execute("SELECT * FROM v_reward_authority").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "t1"
            assert rows[0][1] == pytest.approx(1.0)

            composite = con.execute("SELECT * FROM v_composite_outcome_validity").fetchall()
            assert len(composite) == 1
            assert composite[0][0] == "t1"

            insert = """
                INSERT INTO trial_outcomes (
                    outcome_id, trial_id, source_trial_id, outcome_kind,
                    outcome_namespace, outcome_name, reward_value, is_valid_reward,
                    valid_fraction, agent_status, agent_exception, verifier_status,
                    artifact_status, artifact_digest, source_digest, verifier_digest,
                    authority_state, superseded_by_outcome_id, supersession_reason,
                    is_summable, cas_uri, evidence_path, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            con.executemany(
                insert,
                [
                    (
                        "o2",
                        "t2",
                        None,
                        "original_verifier",
                        "harbor_verifier",
                        "reward",
                        None,
                        False,
                        None,
                        "timed_out",
                        "AgentTimeoutError",
                        "timed_out_without_result",
                        "preserved",
                        "sha256:artifact-2",
                        "sha256:source-2",
                        "sha256:verifier-2",
                        "provisional",
                        None,
                        None,
                        False,
                        None,
                        None,
                        None,
                    ),
                    (
                        "o3",
                        "t2",
                        None,
                        "synthetic_fallback",
                        "harbor_verifier",
                        "reward",
                        0.0,
                        False,
                        None,
                        "timed_out",
                        "AgentTimeoutError",
                        "timed_out_without_result",
                        "preserved",
                        "sha256:artifact-2",
                        "sha256:source-2",
                        "sha256:verifier-2",
                        "provisional",
                        None,
                        None,
                        False,
                        None,
                        None,
                        None,
                    ),
                    (
                        "o4",
                        "r2",
                        "t2",
                        "verifier_regrade",
                        "harbor_verifier",
                        "reward",
                        0.5,
                        True,
                        1.0,
                        "timed_out",
                        "AgentTimeoutError",
                        "regrade_valid",
                        "preserved",
                        "sha256:artifact-2",
                        "sha256:source-2",
                        "sha256:verifier-2",
                        "provisional",
                        None,
                        None,
                        True,
                        None,
                        None,
                        None,
                    ),
                    (
                        "o5",
                        "t3",
                        None,
                        "original_verifier",
                        "harbor_verifier",
                        "reward",
                        None,
                        False,
                        None,
                        "timed_out",
                        None,
                        "timed_out_without_result",
                        "preserved",
                        "sha256:artifact-3",
                        "sha256:source-3",
                        "sha256:verifier-3",
                        "provisional",
                        None,
                        None,
                        False,
                        None,
                        None,
                        None,
                    ),
                    (
                        "o6",
                        "r3",
                        "t3",
                        "verifier_regrade",
                        "harbor_verifier",
                        "reward",
                        0.75,
                        True,
                        1.0,
                        "timed_out",
                        None,
                        "regrade_valid",
                        "preserved",
                        "sha256:artifact-3",
                        "sha256:source-3",
                        "sha256:wrong-verifier",
                        "provisional",
                        None,
                        None,
                        True,
                        None,
                        None,
                        None,
                    ),
                    (
                        "o7",
                        "t4",
                        None,
                        "inspect_scorer",
                        "inspect",
                        "accuracy",
                        1.0,
                        False,
                        None,
                        "unknown",
                        None,
                        "not_run",
                        "unknown",
                        None,
                        "sha256:source-4",
                        "sha256:scorer-4",
                        "non_decision",
                        None,
                        None,
                        False,
                        None,
                        None,
                        None,
                    ),
                ],
            )

            regraded = con.execute(
                "SELECT * FROM v_composite_outcome_validity WHERE trial_id = 't2'"
            ).fetchone()
            assert regraded is not None
            assert regraded[5] == "regrade_authoritative"
            assert regraded[6] == pytest.approx(0.5)
            assert regraded[7] is True
            assert regraded[9] == "o4"

            disputed = con.execute(
                "SELECT authority_axis, refusal_reason "
                "FROM v_composite_outcome_validity WHERE trial_id = 't3'"
            ).fetchone()
            assert disputed == ("disputed", "invalid_regrade_lineage")

            inspect_only = con.execute(
                "SELECT authority_axis, resolved_reward, authoritative_outcome_id "
                "FROM v_composite_outcome_validity WHERE trial_id = 't4'"
            ).fetchone()
            assert inspect_only == ("non_decision", None, None)
