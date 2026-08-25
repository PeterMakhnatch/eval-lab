"""Projections from synthetic eval specs, certificates, and ATIF trajectories into DuckDB/Parquet surfaces.

Provides deterministic transformations of:
- SyntheticEvalSpec -> SyntheticLineageFact & TransformationFact
- Execution trajectory (ATIF) -> BehaviorEpisodeRecord
- Multi-source integration into in-memory DuckDB connections and PyArrow tables.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import duckdb
import pyarrow as pa

from evallab.synthetic_contracts import (
    BehaviorEpisodeRecord,
    BehaviorEpisodeStatus,
    ConfidenceLevel,
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticEvalSpec,
    SyntheticLineageFact,
    TransformationFact,
)


def _stable_hash(*components: Any) -> str:
    """Compute sha256 hex string from stringifiable components."""
    encoded = "\0".join(str(c) for c in components).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def extract_transformation_facts(
    spec: SyntheticEvalSpec,
    explicit_transformations: Sequence[TransformationFact] | None = None,
) -> list[TransformationFact]:
    """Extract ordered TransformationFact records for a SyntheticEvalSpec."""
    if explicit_transformations:
        return list(explicit_transformations)

    # Build primary transformation step from spec metadata
    primary_fact = TransformationFact(
        step_order=0,
        transformation_name=spec.perturbation_type,
        input_digest=spec.base_task_digest,
        output_digest=spec.generated_task_digest,
        parameters=spec.parameters,
        diff_summary=(
            f"Synthesized '{spec.construct_name}' ({spec.family.value}) "
            f"via {spec.perturbation_type} [seed={spec.seed}]"
        ),
    )
    return [primary_fact]


def project_synthetic_lineage(
    spec: SyntheticEvalSpec,
    transformations: Sequence[TransformationFact] | None = None,
) -> SyntheticLineageFact:
    """Transform SyntheticEvalSpec into an auditable SyntheticLineageFact."""
    facts = extract_transformation_facts(spec, explicit_transformations=transformations)
    return SyntheticLineageFact(
        schema_version=1,
        lineage_id=spec.lineage_id,
        family_id=spec.family_id,
        base_task_ref=spec.source_task_ref,
        partition=spec.partition,
        transformations=facts,
        created_at=datetime.now(UTC).isoformat(),
    )


def _extract_atif_steps(atif_payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Flatten ATIF steps from a payload or list of steps."""
    if isinstance(atif_payload, list):
        return [step for step in atif_payload if isinstance(step, dict)]
    if not isinstance(atif_payload, dict):
        return []

    # Check top-level steps
    steps = atif_payload.get("steps")
    if isinstance(steps, list):
        return [s for s in steps if isinstance(s, dict)]

    # Check trajectory document format
    trajectories = atif_payload.get("trajectories")
    if isinstance(trajectories, list) and trajectories:
        first = trajectories[0]
        if isinstance(first, dict) and isinstance(first.get("steps"), list):
            return [s for s in first["steps"] if isinstance(s, dict)]

    return []


def project_behavior_episodes_from_atif(
    atif_payload: dict[str, Any] | list[Any],
    *,
    spec: SyntheticEvalSpec | None = None,
    trial_id: str | None = None,
    default_status: BehaviorEpisodeStatus = "candidate",
    default_confidence: ConfidenceLevel = "medium",
) -> list[BehaviorEpisodeRecord]:
    """Detect discrete behavioral episodes from an ATIF execution trajectory."""
    steps = _extract_atif_steps(atif_payload)
    resolved_trial_id = (
        trial_id
        or (atif_payload.get("trial_id") if isinstance(atif_payload, dict) else None)
        or "trial_unknown"
    )
    spec_id = (
        spec.spec_id
        if spec
        else (atif_payload.get("spec_id") if isinstance(atif_payload, dict) else None)
    )
    family = spec.family if spec else None

    episodes: list[BehaviorEpisodeRecord] = []

    # Scan steps for behavior patterns
    for idx, step in enumerate(steps):
        step_id = step.get("step_id", idx)
        action = step.get("action", {}) or {}
        observation = step.get("observation", {}) or {}
        model_output = str(
            step.get("model_output", "") or step.get("thought", "") or step.get("message", "")
        )
        action_name = str(
            action.get("function_name", "") or action.get("name", "") or action.get("tool", "")
        ).lower()
        obs_text = str(
            observation.get("output", "")
            or observation.get("stdout", "")
            or observation.get("stderr", "")
            or ""
        )
        exit_code = observation.get("exit_code")

        # 1. Tool Retry / Recovery Behavior
        is_error = (
            bool(exit_code and exit_code != 0)
            or ("error" in obs_text.lower())
            or ("failed" in obs_text.lower())
        )
        if is_error and idx + 1 < len(steps):
            next_step = steps[idx + 1]
            next_action = next_step.get("action", {}) or {}
            next_action_name = str(
                next_action.get("function_name", "") or next_action.get("name", "")
            ).lower()
            next_obs = next_step.get("observation", {}) or {}
            next_exit = next_obs.get("exit_code", 0)

            if (
                next_action_name == action_name
                or ("retry" in next_action_name)
                or ("bash" in next_action_name)
            ):
                recovered = next_exit == 0
                behavior_label = (
                    "tool_retry_recovery" if recovered else "tool_retry_subsequent_failure"
                )
                ep_id = _stable_hash(
                    resolved_trial_id, spec_id, step_id, step_id + 1, behavior_label
                )
                episodes.append(
                    BehaviorEpisodeRecord(
                        episode_id=ep_id,
                        trial_id=resolved_trial_id,
                        spec_id=spec_id,
                        behavior=behavior_label,
                        start_step=step_id,
                        end_step=step_id + 1,
                        intent="retry_after_tool_failure",
                        evidence_step_ids=[step_id, next_step.get("step_id", idx + 1)],
                        evidence_summary=f"Tool '{action_name}' failed at step {step_id}, retried at step {idx + 1} (recovered={recovered})",
                        status=default_status,
                        confidence="high" if recovered else "medium",
                        metadata={"action_name": action_name, "recovered": recovered},
                    )
                )

        # 2. Epistemic Restraint / Abstention Behavior
        abstain_keywords = (
            "cannot complete",
            "impossible",
            "missing prerequisite",
            "abstain",
            "insufficient information",
            "refuse",
        )
        if any(kw in model_output.lower() for kw in abstain_keywords):
            behavior_label = "epistemic_abstention"
            ep_id = _stable_hash(resolved_trial_id, spec_id, step_id, step_id, behavior_label)
            episodes.append(
                BehaviorEpisodeRecord(
                    episode_id=ep_id,
                    trial_id=resolved_trial_id,
                    spec_id=spec_id,
                    behavior=behavior_label,
                    start_step=step_id,
                    end_step=step_id,
                    intent="epistemic_refusal_or_abstention",
                    evidence_step_ids=[step_id],
                    evidence_summary=f"Agent articulated epistemic boundary or abstention at step {step_id}",
                    status=default_status,
                    confidence="high",
                    metadata={"reasoning_snippet": model_output[:200]},
                )
            )

        # 3. Pre-completion Verification Behavior
        if any(v in action_name for v in ("test", "verify", "pytest", "check", "assert")):
            behavior_label = "pre_completion_verification"
            ep_id = _stable_hash(resolved_trial_id, spec_id, step_id, step_id, behavior_label)
            episodes.append(
                BehaviorEpisodeRecord(
                    episode_id=ep_id,
                    trial_id=resolved_trial_id,
                    spec_id=spec_id,
                    behavior=behavior_label,
                    start_step=step_id,
                    end_step=step_id,
                    intent="verify_state_before_submission",
                    evidence_step_ids=[step_id],
                    evidence_summary=f"Agent performed verification tool call '{action_name}' at step {step_id}",
                    status=default_status,
                    confidence="high",
                    metadata={"verifier_tool": action_name},
                )
            )

        # 4. Context Filter / Distraction Handling Behavior
        if (
            family == PerturbationFamily.CONTEXT_PRESSURE
            or "distract" in model_output.lower()
            or "irrelevant" in model_output.lower()
        ) and (
            "ignore" in model_output.lower()
            or "irrelevant" in model_output.lower()
            or "filtered" in model_output.lower()
        ):
                behavior_label = "context_distraction_filtered"
                ep_id = _stable_hash(resolved_trial_id, spec_id, step_id, step_id, behavior_label)
                episodes.append(
                    BehaviorEpisodeRecord(
                        episode_id=ep_id,
                        trial_id=resolved_trial_id,
                        spec_id=spec_id,
                        behavior=behavior_label,
                        start_step=step_id,
                        end_step=step_id,
                        intent="filter_irrelevant_distractor_context",
                        evidence_step_ids=[step_id],
                        evidence_summary=f"Agent explicitly filtered distractors at step {step_id}",
                        status=default_status,
                        confidence="medium",
                        metadata={"distraction_filtering": True},
                    )
                )

        # 5. Function DAG Dependency Sequencing Behavior
        if (
            family == PerturbationFamily.FUNCTION_DAG
            or "dag" in action_name
            or "dependency" in model_output.lower()
        ):
            behavior_label = "dag_dependency_execution"
            ep_id = _stable_hash(resolved_trial_id, spec_id, step_id, step_id, behavior_label)
            episodes.append(
                BehaviorEpisodeRecord(
                    episode_id=ep_id,
                    trial_id=resolved_trial_id,
                    spec_id=spec_id,
                    behavior=behavior_label,
                    start_step=step_id,
                    end_step=step_id,
                    intent="execute_dependent_step",
                    evidence_step_ids=[step_id],
                    evidence_summary=f"Agent executed DAG function '{action_name}' at step {step_id}",
                    status=default_status,
                    confidence="medium",
                    metadata={"function_name": action_name},
                )
            )

    return episodes


def specs_to_arrow(specs: Sequence[SyntheticEvalSpec]) -> pa.Table:
    """Convert sequence of SyntheticEvalSpec contracts to PyArrow Table."""
    records = []
    for s in specs:
        records.append(
            {
                "spec_id": s.spec_id,
                "spec_version": s.spec_version,
                "construct_name": s.construct_name,
                "family": s.family.value,
                "perturbation_type": s.perturbation_type,
                "seed": s.seed,
                "source_task_ref": s.source_task_ref,
                "base_task_digest": s.base_task_digest,
                "generated_task_digest": s.generated_task_digest,
                "expected_behavior": s.expected_behavior,
                "capability_opportunity": s.capability_opportunity,
                "partition": s.partition,
                "family_id": s.family_id,
                "lineage_id": s.lineage_id,
                "parameters_json": json.dumps(s.parameters, sort_keys=True),
            }
        )

    schema = pa.schema(
        [
            pa.field("spec_id", pa.string(), nullable=False),
            pa.field("spec_version", pa.string(), nullable=False),
            pa.field("construct_name", pa.string(), nullable=False),
            pa.field("family", pa.string(), nullable=False),
            pa.field("perturbation_type", pa.string(), nullable=False),
            pa.field("seed", pa.int64(), nullable=False),
            pa.field("source_task_ref", pa.string(), nullable=False),
            pa.field("base_task_digest", pa.string(), nullable=False),
            pa.field("generated_task_digest", pa.string(), nullable=False),
            pa.field("expected_behavior", pa.string(), nullable=False),
            pa.field("capability_opportunity", pa.string(), nullable=False),
            pa.field("partition", pa.string(), nullable=False),
            pa.field("family_id", pa.string(), nullable=False),
            pa.field("lineage_id", pa.string(), nullable=False),
            pa.field("parameters_json", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(records, schema=schema)


def certificates_to_arrow(certs: Sequence[SyntheticCertificate]) -> pa.Table:
    """Convert sequence of SyntheticCertificate contracts to PyArrow Table."""
    records = []
    for c in certs:
        records.append(
            {
                "spec_id": c.spec_id,
                "cert_version": c.cert_version,
                "status": c.status,
                "static_reachability": c.static_reachability,
                "clean_reset_passed": c.clean_reset_passed,
                "oracle_3x_passed": c.oracle_3x_passed,
                "nop_failed": c.nop_failed,
                "mutants_tested_count": c.mutants_tested_count,
                "mutants_failed_count": c.mutants_failed_count,
                "alignment_audit_passed": c.alignment_audit_passed,
                "regeneration_idempotent": c.regeneration_idempotent,
                "secret_isolation_passed": c.secret_isolation_passed,
                "is_passing": c.is_passing,
                "certified_at": c.certified_at,
                "notes": c.notes,
            }
        )

    schema = pa.schema(
        [
            pa.field("spec_id", pa.string(), nullable=False),
            pa.field("cert_version", pa.string(), nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("static_reachability", pa.bool_(), nullable=False),
            pa.field("clean_reset_passed", pa.bool_(), nullable=False),
            pa.field("oracle_3x_passed", pa.bool_(), nullable=False),
            pa.field("nop_failed", pa.bool_(), nullable=False),
            pa.field("mutants_tested_count", pa.int64(), nullable=False),
            pa.field("mutants_failed_count", pa.int64(), nullable=False),
            pa.field("alignment_audit_passed", pa.bool_(), nullable=False),
            pa.field("regeneration_idempotent", pa.bool_(), nullable=False),
            pa.field("secret_isolation_passed", pa.bool_(), nullable=False),
            pa.field("is_passing", pa.bool_(), nullable=False),
            pa.field("certified_at", pa.string(), nullable=False),
            pa.field("notes", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(records, schema=schema)


def transformations_to_arrow(transformations: Sequence[TransformationFact]) -> pa.Table:
    """Convert sequence of TransformationFact contracts to PyArrow Table."""
    records = []
    for t in transformations:
        records.append(
            {
                "step_order": t.step_order,
                "transformation_name": t.transformation_name,
                "input_digest": t.input_digest,
                "output_digest": t.output_digest,
                "parameters_json": json.dumps(t.parameters, sort_keys=True),
                "diff_summary": t.diff_summary,
            }
        )

    schema = pa.schema(
        [
            pa.field("step_order", pa.int64(), nullable=False),
            pa.field("transformation_name", pa.string(), nullable=False),
            pa.field("input_digest", pa.string(), nullable=False),
            pa.field("output_digest", pa.string(), nullable=False),
            pa.field("parameters_json", pa.string(), nullable=False),
            pa.field("diff_summary", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(records, schema=schema)


def lineages_to_arrow(lineages: Sequence[SyntheticLineageFact]) -> pa.Table:
    """Convert sequence of SyntheticLineageFact contracts to PyArrow Table."""
    records = []
    for lin in lineages:
        records.append(
            {
                "schema_version": lin.schema_version,
                "lineage_id": lin.lineage_id,
                "family_id": lin.family_id,
                "base_task_ref": lin.base_task_ref,
                "partition": lin.partition,
                "transformations_count": len(lin.transformations),
                "created_at": lin.created_at,
            }
        )

    schema = pa.schema(
        [
            pa.field("schema_version", pa.int64(), nullable=False),
            pa.field("lineage_id", pa.string(), nullable=False),
            pa.field("family_id", pa.string(), nullable=False),
            pa.field("base_task_ref", pa.string(), nullable=False),
            pa.field("partition", pa.string(), nullable=False),
            pa.field("transformations_count", pa.int64(), nullable=False),
            pa.field("created_at", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(records, schema=schema)


def episodes_to_arrow(episodes: Sequence[BehaviorEpisodeRecord]) -> pa.Table:
    """Convert sequence of BehaviorEpisodeRecord contracts to PyArrow Table."""
    records = []
    for ep in episodes:
        records.append(
            {
                "schema_version": ep.schema_version,
                "episode_id": ep.episode_id,
                "trial_id": ep.trial_id,
                "spec_id": ep.spec_id or "",
                "behavior": ep.behavior,
                "start_step": ep.start_step,
                "end_step": ep.end_step,
                "intent": ep.intent,
                "evidence_steps_count": len(ep.evidence_step_ids),
                "evidence_summary": ep.evidence_summary,
                "status": ep.status,
                "confidence": ep.confidence,
                "metadata_json": json.dumps(ep.metadata, sort_keys=True),
            }
        )

    schema = pa.schema(
        [
            pa.field("schema_version", pa.int64(), nullable=False),
            pa.field("episode_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("spec_id", pa.string(), nullable=False),
            pa.field("behavior", pa.string(), nullable=False),
            pa.field("start_step", pa.int64(), nullable=False),
            pa.field("end_step", pa.int64(), nullable=False),
            pa.field("intent", pa.string(), nullable=False),
            pa.field("evidence_steps_count", pa.int64(), nullable=False),
            pa.field("evidence_summary", pa.string(), nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("confidence", pa.string(), nullable=False),
            pa.field("metadata_json", pa.string(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(records, schema=schema)


def register_synthetic_tables_in_duckdb(
    conn: duckdb.DuckDBPyConnection,
    *,
    specs: Sequence[SyntheticEvalSpec] = (),
    certs: Sequence[SyntheticCertificate] = (),
    lineages: Sequence[SyntheticLineageFact] = (),
    transformations: Sequence[TransformationFact] = (),
    episodes: Sequence[BehaviorEpisodeRecord] = (),
) -> None:
    """Register PyArrow tables and analytical views into a DuckDB connection."""
    conn.register("synthetic_specs", specs_to_arrow(specs))
    conn.register("synthetic_certificates", certificates_to_arrow(certs))
    conn.register("synthetic_lineages", lineages_to_arrow(lineages))
    conn.register("transformation_facts", transformations_to_arrow(transformations))
    conn.register("behavior_episode_records", episodes_to_arrow(episodes))

    # Analytical Views
    conn.execute("""
    CREATE OR REPLACE VIEW v_synthetic_capability_summary AS
    SELECT
        s.spec_id,
        s.construct_name,
        s.family,
        s.perturbation_type,
        s.partition,
        COALESCE(c.status, 'uncertified') AS cert_status,
        COALESCE(c.is_passing, FALSE) AS cert_passed,
        COUNT(e.episode_id) AS episode_count
    FROM synthetic_specs s
    LEFT JOIN synthetic_certificates c ON s.spec_id = c.spec_id
    LEFT JOIN behavior_episode_records e ON s.spec_id = e.spec_id
    GROUP BY s.spec_id, s.construct_name, s.family, s.perturbation_type, s.partition, c.status, c.is_passing;
    """)

    conn.execute("""
    CREATE OR REPLACE VIEW v_behavior_by_perturbation_family AS
    SELECT
        s.family,
        e.behavior,
        e.status AS review_status,
        e.confidence,
        COUNT(*) AS occurrence_count
    FROM behavior_episode_records e
    JOIN synthetic_specs s ON e.spec_id = s.spec_id
    GROUP BY s.family, e.behavior, e.status, e.confidence;
    """)


def create_synthetic_duckdb(
    *,
    specs: Sequence[SyntheticEvalSpec] = (),
    certs: Sequence[SyntheticCertificate] = (),
    lineages: Sequence[SyntheticLineageFact] = (),
    transformations: Sequence[TransformationFact] = (),
    episodes: Sequence[BehaviorEpisodeRecord] = (),
) -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB database populated with synthetic facts and views."""
    conn = duckdb.connect(":memory:")
    register_synthetic_tables_in_duckdb(
        conn,
        specs=specs,
        certs=certs,
        lineages=lineages,
        transformations=transformations,
        episodes=episodes,
    )
    return conn
