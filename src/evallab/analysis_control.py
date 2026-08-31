"""Stable operator views over readiness, outcome authority, and feature governance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pydantic import Field

from evallab.autonomous_research import (
    AutonomousResearchFeatures,
    ResearchRunTraceV1,
    extract_autonomous_research_features,
)
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    create_predictor_eligibility_duckdb_view,
)
from evallab.outcome_authority import (
    AgentOutcomeStatus,
    ArtifactOutcomeStatus,
    AuthorityState,
    OutcomeKind,
    OutcomeRecord,
    VerifierOutcomeStatus,
)
from evallab.profiles import AgentProfile, builtin_profiles, evaluate_profile_readiness
from evallab.schemas import AgentReadinessRecord, ContractModel

CONTROL_VIEW_NAMES = (
    "v_agent_readiness",
    "v_composite_outcome_validity",
    "v_reward_authority",
    "v_headline_binding",
    "v_scale_binding_status",
    "v_selection_reconstructibility",
    "v_feature_activation_map",
    "v_predictor_eligibility",
)

_OUTCOME_COLUMNS = (
    "outcome_id",
    "trial_id",
    "source_trial_id",
    "outcome_kind",
    "outcome_namespace",
    "outcome_name",
    "reward_value",
    "is_valid_reward",
    "valid_fraction",
    "agent_status",
    "agent_exception",
    "verifier_status",
    "artifact_status",
    "artifact_digest",
    "source_digest",
    "verifier_digest",
    "evidence_digest",
    "authority_state",
    "superseded_by_outcome_id",
    "supersession_reason",
    "is_summable",
    "cas_uri",
    "evidence_path",
    "recorded_at",
)


class AnalysisBinding(ContractModel):
    run_id: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)
    headline_visible_scalar: str = Field(min_length=1)
    visible_alternatives: tuple[str, ...] = Field(min_length=1)
    job_summary_fallback_reward: float | None = None


class AnalysisBindingPolicy(ContractModel):
    schema_version: str = "analysis-bindings/v1"
    bindings: tuple[AnalysisBinding, ...]


@dataclass(frozen=True)
class CalibrationEvidence:
    path: Path
    relative_path: str
    payload: dict[str, Any]
    binding: AnalysisBinding
    trace: ResearchRunTraceV1
    features: AutonomousResearchFeatures
    evidence_digest: str


@dataclass(frozen=True)
class ControlMaterialization:
    readiness_profiles: int
    calibration_runs: int
    outcome_facts: int
    activation_rows: int
    predictor_rows: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def load_analysis_bindings(root: Path) -> AnalysisBindingPolicy:
    path = root / "policy/analysis-bindings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = AnalysisBindingPolicy.model_validate(raw)
    if policy.schema_version != "analysis-bindings/v1":
        raise ValueError(f"unsupported analysis binding schema: {policy.schema_version}")
    run_ids = [binding.run_id for binding in policy.bindings]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("analysis binding run_id values must be unique")
    return policy


def load_calibration_evidence(
    root: Path,
    *,
    evidence_paths: Sequence[Path] | None = None,
) -> tuple[CalibrationEvidence, ...]:
    policy = load_analysis_bindings(root)
    requested = {path.resolve() for path in evidence_paths} if evidence_paths else None
    evidence: list[CalibrationEvidence] = []
    for binding in policy.bindings:
        path = (root / binding.evidence_path).resolve()
        if requested is not None and path not in requested:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "evallab-rsi-calibration-evidence/v1":
            raise ValueError(f"unsupported calibration evidence schema in {path}")
        trace_raw = payload.get("autonomous_research_trace")
        if not isinstance(trace_raw, dict):
            raise ValueError(f"missing autonomous_research_trace in {path}")
        trace = ResearchRunTraceV1.model_validate(trace_raw)
        if trace.run_id != binding.run_id:
            raise ValueError(
                f"binding run_id {binding.run_id} does not match evidence run_id {trace.run_id}"
            )
        evidence.append(
            CalibrationEvidence(
                path=path,
                relative_path=_relative(path, root),
                payload=payload,
                binding=binding,
                trace=trace,
                features=extract_autonomous_research_features(trace),
                evidence_digest=_file_digest(path),
            )
        )
    return tuple(evidence)


def _enum_or_default(enum_type: Any, value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _outcome_id(record: OutcomeRecord) -> str:
    identity = record.model_dump(
        mode="json",
        exclude={
            "outcome_id",
            "authority_state",
            "superseded_by_outcome_id",
            "supersession_reason",
            "is_summable",
            "recorded_at",
        },
    )
    return _digest(identity)


def _calibration_outcomes(evidence: CalibrationEvidence) -> tuple[OutcomeRecord, ...]:
    payload = evidence.payload
    trace = evidence.trace
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    axes = run.get("outcome_axes") if isinstance(run.get("outcome_axes"), dict) else {}
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    sealed = scores.get("sealed") if isinstance(scores.get("sealed"), dict) else {}
    trial_id = str(run.get("trial_id") or trace.run_id)
    artifact_status = (
        ArtifactOutcomeStatus.preserved
        if trace.artifact_replay_verified is True and trace.final_artifact_digest
        else ArtifactOutcomeStatus.unknown
    )
    agent_status = _enum_or_default(
        AgentOutcomeStatus,
        axes.get("agent_status"),
        AgentOutcomeStatus.unknown,
    )
    agent_exception = str(axes["agent_exception"]) if axes.get("agent_exception") else None
    initial_status = axes.get("initial_verifier_status") or axes.get("verifier_status")
    verifier_status = _enum_or_default(
        VerifierOutcomeStatus,
        initial_status,
        VerifierOutcomeStatus.unknown,
    )
    initial_valid = bool(
        axes.get("initial_verifier_reward_valid", axes.get("verifier_reward_valid", False))
    )
    reward = sealed.get("reward") if initial_valid else None
    reward_value = (
        float(reward) if isinstance(reward, int | float) and not isinstance(reward, bool) else None
    )
    source_digest = trace.source_digest
    verifier_digest = trace.verifier_digest or evidence.evidence_digest

    original = OutcomeRecord(
        trial_id=trial_id,
        outcome_kind=OutcomeKind.original_verifier,
        reward_value=reward_value,
        is_valid_reward=initial_valid and reward_value is not None,
        valid_fraction=(
            float(sealed["valid_fraction"])
            if isinstance(sealed.get("valid_fraction"), int | float)
            else None
        ),
        agent_status=agent_status,
        agent_exception=agent_exception,
        verifier_status=verifier_status,
        artifact_status=artifact_status,
        artifact_digest=trace.final_artifact_digest,
        source_digest=source_digest,
        verifier_digest=verifier_digest,
        evidence_digest=evidence.evidence_digest,
        authority_state=AuthorityState.provisional,
        is_summable=initial_valid and reward_value is not None,
        evidence_path=evidence.relative_path,
        recorded_at=str(payload.get("recorded_at")) if payload.get("recorded_at") else None,
    )
    original = original.model_copy(update={"outcome_id": _outcome_id(original)})
    records = [original]

    fallback = evidence.binding.job_summary_fallback_reward
    if fallback is not None:
        synthetic = original.model_copy(
            update={
                "outcome_kind": OutcomeKind.synthetic_fallback,
                "reward_value": fallback,
                "is_valid_reward": False,
                "is_summable": False,
            }
        )
        records.append(synthetic.model_copy(update={"outcome_id": _outcome_id(synthetic)}))

    regrade_id = axes.get("verifier_regrade_trial_id")
    regrade_valid = bool(axes.get("verifier_regrade_reward_valid", False))
    if regrade_id:
        regrade_reward = sealed.get("reward")
        regrade_value = (
            float(regrade_reward)
            if isinstance(regrade_reward, int | float) and not isinstance(regrade_reward, bool)
            else None
        )
        regrade = OutcomeRecord(
            trial_id=str(regrade_id),
            source_trial_id=trial_id,
            outcome_kind=OutcomeKind.verifier_regrade,
            reward_value=regrade_value,
            is_valid_reward=regrade_valid and regrade_value is not None,
            valid_fraction=(
                float(sealed["valid_fraction"])
                if isinstance(sealed.get("valid_fraction"), int | float)
                else None
            ),
            agent_status=agent_status,
            agent_exception=agent_exception,
            verifier_status=(
                VerifierOutcomeStatus.regrade_valid
                if regrade_valid
                else VerifierOutcomeStatus.error
            ),
            artifact_status=artifact_status,
            artifact_digest=trace.final_artifact_digest,
            source_digest=source_digest,
            verifier_digest=verifier_digest,
            evidence_digest=evidence.evidence_digest,
            authority_state=AuthorityState.provisional,
            is_summable=regrade_valid and regrade_value is not None,
            evidence_path=evidence.relative_path,
            recorded_at=str(payload.get("recorded_at")) if payload.get("recorded_at") else None,
        )
        records.append(regrade.model_copy(update={"outcome_id": _outcome_id(regrade)}))
    return tuple(records)


def _readiness_rows(
    root: Path,
    evaluator: Callable[[AgentProfile], AgentReadinessRecord] | None,
) -> list[tuple[Any, ...]]:
    rows = []
    for profile in builtin_profiles().values():
        record = (
            evaluator(profile)
            if evaluator is not None
            else evaluate_profile_readiness(profile, root=root)
        )
        blocker = record.blocker
        rows.append(
            (
                record.profile_id,
                record.adapter,
                record.model,
                record.profile_digest,
                record.state,
                record.gates.declared,
                record.gates.installed,
                record.gates.host_credential,
                record.gates.harbor_transport,
                record.gates.environment_network,
                record.gates.structured_trajectory,
                record.gates.smoke,
                record.gates.canary,
                blocker.gate if blocker else None,
                blocker.reason if blocker else None,
                blocker.remediation if blocker else None,
                record.last_smoke.atif_digest if record.last_smoke else None,
                record.qualification.qualification_digest if record.qualification else None,
                record.updated_at.isoformat(),
            )
        )
    return rows


def _visible_candidates(evidence: CalibrationEvidence) -> dict[str, float | None]:
    scores = evidence.payload.get("scores")
    visible = scores.get("visible") if isinstance(scores, dict) else None
    if not isinstance(visible, dict):
        return {name: None for name in evidence.binding.visible_alternatives}
    candidates: dict[str, float | None] = {}
    for name in evidence.binding.visible_alternatives:
        value = visible.get(name)
        candidates[name] = (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
        )
    return candidates


def _calibration_row(evidence: CalibrationEvidence) -> tuple[Any, ...]:
    trace = evidence.trace
    features = evidence.features
    process = (
        evidence.payload.get("research_process")
        if isinstance(evidence.payload.get("research_process"), dict)
        else {}
    )
    scores = (
        evidence.payload.get("scores") if isinstance(evidence.payload.get("scores"), dict) else {}
    )
    candidates = _visible_candidates(evidence)
    scalar = evidence.binding.headline_visible_scalar
    selected_value = candidates.get(scalar)
    deltas = {
        name: (value - selected_value if value is not None and selected_value is not None else None)
        for name, value in candidates.items()
        if name != scalar
    }
    scalar_present = scalar in candidates
    binding_status = (
        "declared_and_observed"
        if scalar_present and selected_value is not None
        else "declared_but_unmeasured"
        if scalar_present
        else "declared_scalar_missing"
    )
    binding_complete = bool(
        scalar_present
        and trace.final_artifact_digest
        and trace.task_digest
        and trace.verifier_digest
        and trace.metric_config_digest
        and trace.visible_outcome_binding_digest
    )

    scale_binding = trace.score_scale_binding
    arithmetic_permitted = bool(
        scale_binding is not None
        and selected_value is not None
        and trace.hidden_score is not None
        and features.visible_hidden_transfer_gap is not None
    )
    scale_reason = None
    if not arithmetic_permitted:
        explicit_reason = (
            scores.get("transfer_gap_null_reason") if isinstance(scores, dict) else None
        )
        scale_reason = str(explicit_reason) if explicit_reason else "validated scale binding absent"

    selected_version = (
        str(process.get("selected_version") or trace.selected_iteration_id or "") or None
    )
    artifact_versions = [str(value) for value in process.get("artifact_versions", [])]
    log_versions = [str(value) for value in process.get("versions_recorded_in_experiment_log", [])]
    unlogged_versions = [str(value) for value in process.get("unlogged_artifact_versions", [])]
    selected_in_log = selected_version is not None and selected_version in log_versions
    artifact_present = bool(trace.final_artifact_digest)
    visible_covered = bool(process.get("selected_artifact_visible_full_suite_validated", False))
    sealed_covered = bool(process.get("selected_artifact_sealed_replay_validated", False))
    selection_reconstructible = bool(
        selected_version
        and selected_in_log
        and artifact_present
        and visible_covered
        and sealed_covered
    )
    refusal = None
    if not selection_reconstructible:
        reasons = []
        if not selected_version:
            reasons.append("selected_version_missing")
        elif not selected_in_log:
            reasons.append("selected_version_unlogged")
        if not artifact_present:
            reasons.append("selected_artifact_missing")
        if not visible_covered:
            reasons.append("visible_validation_incomplete")
        if not sealed_covered:
            reasons.append("sealed_replay_incomplete")
        refusal = ";".join(reasons)

    return (
        trace.run_id,
        evidence.relative_path,
        trace.benchmark_family,
        features.source_digest,
        features.feature_digest,
        scalar,
        selected_value,
        _canonical_json(candidates),
        _canonical_json(deltas),
        trace.final_artifact_digest,
        trace.score_direction,
        trace.task_digest,
        trace.verifier_digest,
        trace.metric_config_digest,
        trace.visible_outcome_binding_digest,
        trace.hidden_outcome_binding_digest,
        True,
        binding_complete,
        binding_status,
        features.scale_binding_digest,
        features.score_scale_compatible,
        arithmetic_permitted,
        features.visible_hidden_transfer_gap if arithmetic_permitted else None,
        scale_reason,
        selected_version,
        selected_in_log,
        _canonical_json(artifact_versions),
        _canonical_json(log_versions),
        _canonical_json(unlogged_versions),
        artifact_present,
        visible_covered,
        sealed_covered,
        selection_reconstructible,
        refusal,
    )


def _activation_rows(evidence: Iterable[CalibrationEvidence]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    features = TRAJECTORY_FEATURE_REGISTRY.all_features()
    for item in evidence:
        values = item.features.to_dict()
        for feature in features.values():
            value = values.get(feature.column_name)
            if (
                feature.source_table != "autonomous_research_runs"
                or feature.column_name not in values
            ):
                status = "dormant"
                reason = "producer not activated by this benchmark evidence"
                value_json = None
            elif value is None:
                status = (
                    "null_with_denominator"
                    if feature.denominator_policy == "required"
                    else "null_not_applicable"
                )
                reason = feature.null_condition
                value_json = None
            elif (isinstance(value, bool) and not value) or (
                isinstance(value, int | float) and not isinstance(value, bool) and value == 0
            ):
                status = "zero"
                reason = None
                value_json = _canonical_json(value)
            else:
                status = "populated"
                reason = None
                value_json = _canonical_json(value)
            rows.append(
                (
                    item.trace.run_id,
                    item.trace.benchmark_family,
                    feature.column_name,
                    feature.family,
                    feature.construct,
                    feature.producer_module,
                    status,
                    value_json,
                    feature.denominator_policy,
                    feature.denominator_sibling,
                    reason,
                )
            )
    return rows


def materialize_analysis_control_views(
    connection: duckdb.DuckDBPyConnection,
    *,
    root: Path,
    evidence_paths: Sequence[Path] | None = None,
    readiness_evaluator: Callable[[AgentProfile], AgentReadinessRecord] | None = None,
) -> ControlMaterialization:
    """Materialize all stable control-plane views in one DuckDB connection."""
    connection.execute((root / "sql/views.sql").read_text(encoding="utf-8"))
    create_predictor_eligibility_duckdb_view(connection)

    connection.execute(
        """
        CREATE TABLE analysis_agent_readiness (
            profile_id VARCHAR PRIMARY KEY,
            adapter VARCHAR NOT NULL,
            model VARCHAR,
            profile_digest VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            declared VARCHAR NOT NULL,
            installed VARCHAR NOT NULL,
            host_credential VARCHAR NOT NULL,
            harbor_transport VARCHAR NOT NULL,
            environment_network VARCHAR NOT NULL,
            structured_trajectory VARCHAR NOT NULL,
            smoke VARCHAR NOT NULL,
            canary VARCHAR NOT NULL,
            blocker_gate VARCHAR,
            blocker_reason VARCHAR,
            remediation VARCHAR,
            smoke_evidence_digest VARCHAR,
            qualification_digest VARCHAR,
            updated_at VARCHAR NOT NULL
        );
        CREATE OR REPLACE VIEW v_agent_readiness AS
        SELECT * FROM analysis_agent_readiness ORDER BY profile_id;
        """
    )
    readiness_rows = _readiness_rows(root, readiness_evaluator)
    connection.executemany(
        "INSERT INTO analysis_agent_readiness VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        readiness_rows,
    )

    evidence = load_calibration_evidence(root, evidence_paths=evidence_paths)
    connection.execute(
        """
        CREATE TABLE analysis_calibration_runs (
            run_id VARCHAR PRIMARY KEY,
            evidence_path VARCHAR NOT NULL,
            benchmark_family VARCHAR NOT NULL,
            source_digest VARCHAR NOT NULL,
            feature_digest VARCHAR NOT NULL,
            headline_visible_scalar VARCHAR NOT NULL,
            selected_visible_value DOUBLE,
            visible_alternatives_json VARCHAR NOT NULL,
            alternative_deltas_json VARCHAR NOT NULL,
            selected_artifact_digest VARCHAR,
            score_direction VARCHAR NOT NULL,
            task_digest VARCHAR,
            verifier_digest VARCHAR,
            metric_config_digest VARCHAR,
            visible_outcome_binding_digest VARCHAR,
            hidden_outcome_binding_digest VARCHAR,
            binding_declared BOOLEAN NOT NULL,
            binding_complete BOOLEAN NOT NULL,
            binding_status VARCHAR NOT NULL,
            scale_binding_digest VARCHAR,
            score_scale_compatible BOOLEAN NOT NULL,
            arithmetic_permitted BOOLEAN NOT NULL,
            visible_hidden_transfer_gap DOUBLE,
            scale_refusal_reason VARCHAR,
            selected_version VARCHAR,
            selected_version_in_experiment_log BOOLEAN NOT NULL,
            artifact_versions_json VARCHAR NOT NULL,
            experiment_log_versions_json VARCHAR NOT NULL,
            unlogged_artifact_versions_json VARCHAR NOT NULL,
            selected_artifact_present BOOLEAN NOT NULL,
            visible_validation_covered BOOLEAN NOT NULL,
            sealed_replay_covered BOOLEAN NOT NULL,
            selection_reconstructible BOOLEAN NOT NULL,
            selection_refusal_reason VARCHAR
        );
        CREATE OR REPLACE VIEW v_headline_binding AS
        SELECT
            run_id, benchmark_family, headline_visible_scalar, selected_visible_value,
            visible_alternatives_json, alternative_deltas_json, selected_artifact_digest,
            score_direction, task_digest, verifier_digest, metric_config_digest,
            visible_outcome_binding_digest, hidden_outcome_binding_digest,
            binding_declared, binding_complete, binding_status, evidence_path
        FROM analysis_calibration_runs;
        CREATE OR REPLACE VIEW v_scale_binding_status AS
        SELECT
            run_id, benchmark_family, headline_visible_scalar, selected_visible_value,
            scale_binding_digest, score_scale_compatible, arithmetic_permitted,
            visible_hidden_transfer_gap, scale_refusal_reason, task_digest,
            verifier_digest, metric_config_digest, visible_outcome_binding_digest,
            hidden_outcome_binding_digest
        FROM analysis_calibration_runs;
        CREATE OR REPLACE VIEW v_selection_reconstructibility AS
        SELECT
            run_id, benchmark_family, selected_version,
            selected_version_in_experiment_log, selected_artifact_digest,
            selected_artifact_present, artifact_versions_json,
            experiment_log_versions_json, unlogged_artifact_versions_json,
            visible_validation_covered, sealed_replay_covered,
            selection_reconstructible, selection_refusal_reason
        FROM analysis_calibration_runs;
        """
    )
    calibration_rows = [_calibration_row(item) for item in evidence]
    if calibration_rows:
        connection.executemany(
            "INSERT INTO analysis_calibration_runs VALUES ("
            + ", ".join("?" for _ in range(34))
            + ")",
            calibration_rows,
        )

    outcomes = [record for item in evidence for record in _calibration_outcomes(item)]
    if outcomes:
        connection.executemany(
            "INSERT INTO trial_outcomes ("
            + ", ".join(_OUTCOME_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _OUTCOME_COLUMNS)
            + ")",
            [
                tuple(record.model_dump(mode="json")[column] for column in _OUTCOME_COLUMNS)
                for record in outcomes
            ],
        )

    connection.execute(
        """
        CREATE TABLE analysis_feature_activation (
            run_id VARCHAR NOT NULL,
            benchmark_family VARCHAR NOT NULL,
            feature_name VARCHAR NOT NULL,
            feature_family VARCHAR,
            construct VARCHAR,
            producer_module VARCHAR NOT NULL,
            activation_status VARCHAR NOT NULL,
            value_json VARCHAR,
            denominator_policy VARCHAR,
            denominator_sibling VARCHAR,
            status_reason VARCHAR,
            PRIMARY KEY (run_id, feature_name)
        );
        CREATE OR REPLACE VIEW v_feature_activation_map AS
        SELECT * FROM analysis_feature_activation
        ORDER BY run_id, feature_name;
        """
    )
    activation_rows = _activation_rows(evidence)
    if activation_rows:
        connection.executemany(
            "INSERT INTO analysis_feature_activation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            activation_rows,
        )

    predictor_count = connection.execute("SELECT count(*) FROM v_predictor_eligibility").fetchone()[
        0
    ]
    return ControlMaterialization(
        readiness_profiles=len(readiness_rows),
        calibration_runs=len(evidence),
        outcome_facts=len(outcomes),
        activation_rows=len(activation_rows),
        predictor_rows=int(predictor_count),
    )


def query_control_view(
    connection: duckdb.DuckDBPyConnection,
    view_name: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if view_name not in CONTROL_VIEW_NAMES:
        raise ValueError(f"unknown control view: {view_name}")
    query = f"SELECT * FROM {view_name}"
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        query += f" LIMIT {limit}"
    cursor = connection.execute(query)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
