"""Dimension-safe benchmark projection over read-only Data compliance reports.

This module never calls Data's compliance hook and never writes compliance records.  It
turns an already-materialized ``ComplianceIngestReport`` into a projection identity
and dimension row used by Agent-Data producers, DuckDB views, CLI, and cards.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from evallab.benchmark_program_contracts import compute_prefixed_sha256
from evallab.interpretation.benchmark_events import TrialBundle
from evallab.interpretation.trajectory_compliance_ops import (
    ComplianceIngestReport,
    agent_readable_catalog,
)

PRODUCER_VERSION = "benchmark-dimension-quality/v1"
ProjectionStatus = Literal["PROJECTED", "REFUSED"]


@dataclass(frozen=True)
class BenchmarkProjectionDimensions:
    """Immutable Agent-Data dimensions for one benchmark projection row."""

    job_id: str | None
    trial_id: str
    cas_uri: str | None
    model_name: str | None
    agent_name: str | None
    task_name: str | None
    task_id: str
    harness_version: str | None
    scaffold_version: str | None
    repeat_group_id: str | None
    dose_axis: str | None
    dose_value: float | None
    dose_unit: str | None
    alphabet_id: str | None
    alphabet_version: str | None
    quality_status: str | None
    report_digest: str | None
    source_digest: str | None
    producer_version: str
    projection_identity: str
    dimension_digest: str
    projection_status: ProjectionStatus
    analysis_ready: bool
    refusals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(metadata: Mapping[str, Any], name: str) -> str | None:
    value = metadata.get(name)
    return str(value) if isinstance(value, str) and value.strip() else None


def _number(metadata: Mapping[str, Any], name: str) -> float | None:
    value = metadata.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _identity_payload(
    *,
    job_id: str | None,
    trial_id: str,
    source_digest: str | None,
    producer_version: str,
) -> dict[str, str | None]:
    return {
        "job_id": job_id,
        "trial_id": trial_id,
        "source_digest": source_digest,
        "producer_version": producer_version,
    }


def build_projection_dimensions(
    bundle: TrialBundle,
    report: ComplianceIngestReport | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    producer_version: str = PRODUCER_VERSION,
) -> BenchmarkProjectionDimensions:
    """Build a deterministic projection row, refusing incomplete or mixed dimensions.

    ``metadata`` is Agent-Data projection metadata supplied by the settled platform
    record. It must explicitly carry harness/scaffold, repeat, dose, and alphabet
    dimensions. Values are never inferred from agent, task, or result outcome.
    """
    metadata = metadata or {}
    refusals: list[str] = []

    if report is None:
        refusals.append("MISSING_COMPLIANCE_REPORT")
        job_id = None
        cas_uri = None
        quality_status = None
        report_digest = None
        source_digest = None
        model_name = None
        agent_name = None
        task_name = None
        join_ready = False
    else:
        record = report.record
        gates = report.gates
        job_id = gates.job_id
        cas_uri = gates.cas_uri
        # #276 promotes disposition to the report; #273 keeps it on the sealed record.
        quality_status = getattr(report, "disposition", record.disposition)
        report_digest = report.report_digest
        source_digest = record.trial_source_digest
        model_name = gates.model_name
        agent_name = gates.agent_name
        task_name = gates.task_name
        join_ready = gates.join_ready

        if record.trial_id != bundle.trial_id or gates.trial_id != bundle.trial_id:
            refusals.append("TRIAL_ID_MISMATCH")
        if record.job_id != gates.job_id or record.cas_uri != gates.cas_uri:
            refusals.append("COMPLIANCE_IDENTITY_MISMATCH")
        if (
            record.model_name != model_name
            or record.agent_name != agent_name
            or record.task_name != task_name
        ):
            refusals.append("COMPLIANCE_DIMENSION_MISMATCH")
        if quality_status not in {"QUALITY_PASS", "QUALITY_WARN"}:
            refusals.append("QUALITY_NOT_PROJECTABLE")
        if not join_ready:
            refusals.append("JOIN_IDENTITY_MISSING")

    harness_version = _text(metadata, "harness_version")
    scaffold_version = _text(metadata, "scaffold_version")
    repeat_group_id = _text(metadata, "repeat_group_id")
    dose_axis = _text(metadata, "dose_axis")
    dose_value = _number(metadata, "dose_value")
    dose_unit = _text(metadata, "dose_unit")
    alphabet_id = _text(metadata, "alphabet_id")
    alphabet_version = _text(metadata, "alphabet_version")

    if bundle.contract.family == "mcp-recovery-v1":
        raw_levels = bundle.contract.cell_factors.get("persistence_levels", [])
        if not isinstance(raw_levels, list) or len(raw_levels) != 1:
            refusals.append("MISSING_NATIVE_PERSISTENCE_LEVEL")
        else:
            native_persistence_level = _number({"native": raw_levels[0]}, "native")
            if native_persistence_level is None:
                refusals.append("MISSING_NATIVE_PERSISTENCE_LEVEL")
            elif (
                dose_axis in {"persistence", "persistence_level"}
                and dose_value != native_persistence_level
            ):
                refusals.append("PERSISTENCE_DOSE_MISMATCH")
    required_dimensions = {
        "model_name": model_name,
        "agent_name": agent_name,
        "task_name": task_name,
        "harness_version": harness_version,
        "scaffold_version": scaffold_version,
        "repeat_group_id": repeat_group_id,
        "dose_axis": dose_axis,
        "dose_value": dose_value,
        "dose_unit": dose_unit,
        "alphabet_id": alphabet_id,
        "alphabet_version": alphabet_version,
    }
    refusals.extend(
        f"MISSING_{name.upper()}" for name, value in required_dimensions.items() if value is None
    )

    source_digest = source_digest or _text(metadata, "source_digest")
    if source_digest is None:
        refusals.append("MISSING_SOURCE_DIGEST")

    identity_payload = _identity_payload(
        job_id=job_id,
        trial_id=bundle.trial_id,
        source_digest=source_digest,
        producer_version=producer_version,
    )
    projection_identity = compute_prefixed_sha256(identity_payload)
    dimension_digest = compute_prefixed_sha256(
        {
            **identity_payload,
            **required_dimensions,
            "task_id": bundle.contract.task_id,
            "cas_uri": cas_uri,
            "quality_status": quality_status,
            "report_digest": report_digest,
        }
    )
    normalized_refusals = tuple(sorted(set(refusals)))
    analysis_ready = (
        quality_status == "QUALITY_PASS"
        and join_ready
        and source_digest is not None
        and not normalized_refusals
    )

    return BenchmarkProjectionDimensions(
        job_id=job_id,
        trial_id=bundle.trial_id,
        cas_uri=cas_uri,
        model_name=model_name,
        agent_name=agent_name,
        task_name=task_name,
        task_id=bundle.contract.task_id,
        harness_version=harness_version,
        scaffold_version=scaffold_version,
        repeat_group_id=repeat_group_id,
        dose_axis=dose_axis,
        dose_value=dose_value,
        dose_unit=dose_unit,
        alphabet_id=alphabet_id,
        alphabet_version=alphabet_version,
        quality_status=quality_status,
        report_digest=report_digest,
        source_digest=source_digest,
        producer_version=producer_version,
        projection_identity=projection_identity,
        dimension_digest=dimension_digest,
        projection_status="PROJECTED"
        if quality_status in {"QUALITY_PASS", "QUALITY_WARN"}
        else "REFUSED",
        analysis_ready=analysis_ready,
        refusals=normalized_refusals,
    )


def projection_feature_fields(dimensions: BenchmarkProjectionDimensions) -> dict[str, Any]:
    """Flatten one dimension row into producer/datastore fields."""
    fields = dimensions.to_dict()
    fields["projection_refusals"] = ",".join(dimensions.refusals)
    fields.pop("refusals")
    # Raw trial/task identity remains owned by the producer's TrialBundle fields.
    fields.pop("trial_id")
    fields.pop("task_id")
    return fields


def load_compliance_report(path: Path) -> ComplianceIngestReport:
    """Read a Data-owned, materialized compliance report without invoking its hook."""
    return ComplianceIngestReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def agent_readable_projection_provenance(
    report: ComplianceIngestReport | None,
    dimensions: BenchmarkProjectionDimensions,
) -> dict[str, Any]:
    """Expose Data's catalog without copying or reimplementing provenance ownership."""
    return {
        "report_digest": dimensions.report_digest,
        "quality_status": dimensions.quality_status,
        "analysis_ready": dimensions.analysis_ready,
        "projection_identity": dimensions.projection_identity,
        "projection_refusals": list(dimensions.refusals),
        "data_readiness_refusals": list(report.gates.refusals) if report is not None else [],
        "catalog": agent_readable_catalog(report.catalog_entries) if report is not None else [],
    }


def backfill_benchmark_projection_rows(
    feature_rows: Sequence[Mapping[str, Any]],
    dimensions: Sequence[BenchmarkProjectionDimensions],
) -> list[dict[str, Any]]:
    """Idempotently attach dimension rows to existing benchmark feature mappings.

    Existing rows lacking an exact ``(trial_id, source_digest)`` match are retained as
    refused projection rows. This prevents historical rows from silently entering
    analysis views without a settled Data identity.
    """
    by_key = {(item.trial_id, item.source_digest): item for item in dimensions}
    backfilled: list[dict[str, Any]] = []
    for row in feature_rows:
        result = dict(row)
        trial_id = str(result.get("trial_id", ""))
        source_digest = result.get("source_digest")
        dimension = by_key.get((trial_id, source_digest))
        if dimension is None:
            result.update(
                {
                    "projection_status": "REFUSED",
                    "analysis_ready": False,
                    "projection_refusals": "MISSING_DIMENSION_BACKFILL",
                }
            )
        else:
            result.update(projection_feature_fields(dimension))
        backfilled.append(result)
    return backfilled
