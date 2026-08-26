"""Track A5 pack-only automated interpretation runtime.

Implements the first pack-only automated interpretation runtime over the merged
Agent Data + Platform contracts.  Every judgment is a deterministic abstention;
automatic acceptance is hard-disabled.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, ValidationError

from evallab.database import ingest_interpretation_artifacts
from evallab.evidence_pack import (
    DEFAULT_TOKEN_BUDGET,
    EvidencePack,
    build_evidence_pack,
    reopen_omitted_range,
)
from evallab.evidence_store import archive_evidence, load_archive, restore_evidence
from evallab.results import sha256_file
from evallab.schemas import ContractModel
from evallab.trajectory_acceptance import (
    DETERMINISTIC_GATE_ORDER,
    AcceptanceDecision,
    CalibrationClassGate,
    CrossJudgeRecord,
    GateResult,
    evaluate_acceptance,
)
from evallab.trajectory_calibration import (
    CalibrationReport,
    UnsupportedCalibrationVersion,
    calibration_report_can_enable_acceptance,
    parse_calibration_report,
)
from evallab.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    hydrate_citation,
)
from evallab.trajectory_ir import (
    CASTrialResolutionError,
    IREvent,
    TrajectoryIR,
    build_trajectory_ir,
)
from evallab.trajectory_judgment import (
    JudgmentConfidence,
    MachineJudgment,
    canonical_json_digest,
)

_SIDECAR_FILES = (
    "trajectory_ir.json",
    "evidence_pack.json",
    "machine_judgment.json",
    "acceptance_decision.json",
)

_IDENTITY_FIELDS = {
    "trajectory_ir.json": "ir_digest",
    "evidence_pack.json": "pack_digest",
    "machine_judgment.json": "judgment_id",
    "acceptance_decision.json": "decision_id",
}


_QUARANTINE_STATUSES = frozenset({"quarantine", "fail", "quarantined"})


def _classify_cas_restore_error(exc: BaseException) -> RuntimeError:
    """Map CAS restore failures onto missing_cas vs cas_integrity_error.

    ``FileNotFoundError`` from a missing blob stays ``missing_cas``. Corrupt
    gzip/tar bytes, digest mismatch, path-escape, and unsupported URI values
    are integrity failures. ``FileNotFoundError`` is an ``OSError`` subclass,
    so it must be classified before a generic OSError path.
    """
    if isinstance(exc, FileNotFoundError):
        return RuntimeError(f"missing_cas: {exc}")
    return RuntimeError(f"cas_integrity_error: {exc}")


# ---------------------------------------------------------------------------
# Durable filesystem helpers (mirror analysis_worker.py to avoid import cycle)
# ---------------------------------------------------------------------------


def _fsync_directory(directory: Path) -> None:
    """Fsync a directory so dirents created inside it survive a host crash."""
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_mkdir(directory: Path) -> None:
    """Create ``directory``, fsyncing the dirent of every level this adds."""
    created: list[Path] = []
    probe = directory
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
    directory.mkdir(parents=True, exist_ok=True)
    for path in reversed(created):
        _fsync_directory(path.parent)


def _durable_replace(source: Path, destination: Path) -> None:
    """Fsync file bytes before atomically publishing the stable sidecar path."""
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    source.replace(destination)
    _fsync_directory(destination.parent)


# ---------------------------------------------------------------------------
# Public artifact record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRecord:
    """Identity/index row for one persisted interpretation artifact."""

    artifact_digest: str
    kind: str
    trial_id: str
    job_id: str
    content_digest: str
    artifact_path: Path
    cas_uri: str
    pack_digest: str = ""
    judgment_id: str = ""
    decision_id: str = ""
    judgment_digest: str = ""
    decision_digest: str = ""
    produced_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    producer_kind: str | None = None
    validity: str | None = None
    citation_ids: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    calibration_version: str | None = None
    calibration_schema: str | None = None
    decision: str | None = None
    judgment_ids: list[str] = field(default_factory=list)
    status: str | None = None
    supersedes_decision_id: str | None = None


# ---------------------------------------------------------------------------
# Campaign manifest contract adapter
# ---------------------------------------------------------------------------


CampaignAttemptRole = Literal["primary", "retry", "control", "quarantined_attempt"]


class CampaignAnalysisItem(ContractModel):
    source_role: str
    cohort_included: bool
    attempt_role: CampaignAttemptRole
    spec_id: str | None = None
    spec_name: str | None = None
    job_id: str
    job_name: str
    trial_id: str
    trial_name: str
    task_name: str
    task_path: str | None = None
    task_digest: str | None = None
    verifier_digest: str | None = None
    agent_scaffold: str | None = None
    model_name: str | None = None
    harbor_environment: str | None = None
    reward: float | None = None
    exception_type: str | None = None
    cost_usd: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    atif_path: str | None = None
    atif_steps_count: int | None = None
    quality_status: str
    quality_findings: list[str] = Field(default_factory=list)
    quality_report_digest: str | None = None
    cas_uri: str | None
    ingestion_status: str | None = None

    def as_inventory_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CampaignAnalysisManifest(ContractModel):
    schema_version: Literal["campaign-analysis-manifest/v1"]
    manifest_id: str
    manifest_digest: str
    campaign_id: str
    source_campaign_manifest_digest: str
    source_commit: str | None
    authorizing_actor: str
    cas_store_root: str
    items: list[CampaignAnalysisItem]
    accounting: dict[str, Any]
    analysis_config: dict[str, Any]
    produced_at: datetime

    def cohort_items(self) -> list[CampaignAnalysisItem]:
        return [item for item in self.items if item.cohort_included]

    def accounting_items(self) -> list[CampaignAnalysisItem]:
        return [item for item in self.items if not item.cohort_included]


def _clean_digest(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    if s.lower() in {"n/a", "", "null"}:
        return None
    return s


def _make_campaign_item(
    raw: Mapping[str, Any], *, cohort_included: bool, attempt_role: CampaignAttemptRole
) -> CampaignAnalysisItem:
    findings = raw.get("quality_findings") or []
    if not isinstance(findings, list):
        findings = []
    return CampaignAnalysisItem(
        source_role=str(raw.get("role", "")),
        cohort_included=cohort_included,
        attempt_role=attempt_role,
        spec_id=raw.get("spec_id"),
        spec_name=raw.get("spec_name"),
        job_id=str(raw.get("job_id", "")),
        job_name=str(raw.get("job_name", "")),
        trial_id=str(raw.get("trial_id", "")),
        trial_name=str(raw.get("trial_name", "")),
        task_name=str(raw.get("task_name", "")),
        task_path=raw.get("task_path"),
        task_digest=_clean_digest(raw.get("task_digest")),
        verifier_digest=_clean_digest(raw.get("verifier_digest")),
        agent_scaffold=raw.get("agent_scaffold"),
        model_name=raw.get("model_name"),
        harbor_environment=raw.get("harbor_environment"),
        reward=raw.get("reward"),
        exception_type=raw.get("exception_type"),
        cost_usd=raw.get("cost_usd"),
        started_at=raw.get("started_at"),
        finished_at=raw.get("finished_at"),
        atif_path=raw.get("atif_path"),
        atif_steps_count=raw.get("atif_steps_count"),
        quality_status=str(raw.get("quality_status", "unknown")),
        quality_findings=findings,
        quality_report_digest=raw.get("quality_report_digest"),
        cas_uri=_clean_digest(raw.get("cas_uri")),
        ingestion_status=raw.get("ingestion_status"),
    )


def load_campaign_analysis_manifest(path: Path) -> CampaignAnalysisManifest:
    """Load the merged machine-analysis inventory as a typed Platform manifest.

    The committed TB3 inventory uses accounting keys such as
    ``total_planned_specs`` and ``total_executed_trials`` rather than the
    legacy ``analysis_cohort`` / ``executions`` keys.  This adapter derives the
    normalized counts from the actual item list and merges them into the
    accounting dict so both naming conventions are present.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    accounting = dict(data.get("accounting") or {})

    items: list[CampaignAnalysisItem] = []
    for raw in data.get("analysis_cohort_5_trials", []):
        role = raw.get("role", "")
        if role == "infrastructure_retry_1":
            attempt_role: CampaignAttemptRole = "retry"
        else:
            attempt_role = "primary"
        items.append(_make_campaign_item(raw, cohort_included=True, attempt_role=attempt_role))

    for raw in data.get("controls_and_quarantine_ledger", []):
        role = raw.get("role", "")
        if role == "free_control":
            attempt_role = "control"
        elif role == "quarantined_auth_attempt":
            attempt_role = "quarantined_attempt"
        else:
            raise ValueError(f"unsupported accounting role: {role!r}")
        items.append(_make_campaign_item(raw, cohort_included=False, attempt_role=attempt_role))

    cohort_count = sum(1 for item in items if item.cohort_included)
    execution_count = len(items)
    retry_count = sum(1 for item in items if item.attempt_role == "retry")
    control_count = sum(1 for item in items if item.attempt_role == "control")
    quarantine_count = sum(1 for item in items if item.attempt_role == "quarantined_attempt")

    accounting["planned_specs"] = accounting.get(
        "total_planned_specs", accounting.get("planned_specs", cohort_count)
    )
    accounting["executions"] = accounting.get(
        "total_executed_trials", accounting.get("executions", execution_count)
    )
    accounting["analysis_cohort"] = accounting.get(
        "valid_analysis_ready_trials", accounting.get("analysis_cohort", cohort_count)
    )
    accounting["controls"] = accounting.get(
        "free_local_controls", accounting.get("controls", control_count)
    )
    accounting["quarantine"] = accounting.get(
        "quarantined_infrastructure_attempts",
        accounting.get("quarantine", quarantine_count),
    )
    accounting["retries"] = accounting.get("retries", retry_count)
    accounting["unresolved"] = accounting.get(
        "unresolved_evidence_count", accounting.get("unresolved", 0)
    )

    if accounting["analysis_cohort"] != cohort_count:
        raise ValueError(
            "campaign cohort accounting does not match the item list: "
            f"{accounting['analysis_cohort']} != {cohort_count}"
        )
    if accounting["executions"] != execution_count:
        raise ValueError(
            "campaign execution accounting does not match the item list: "
            f"{accounting['executions']} != {execution_count}"
        )
    if accounting["controls"] != control_count:
        raise ValueError("campaign control accounting does not match the item list")
    if accounting["quarantine"] != quarantine_count:
        raise ValueError("campaign quarantine accounting does not match the item list")
    if accounting["retries"] != retry_count:
        raise ValueError("campaign retry accounting does not match the item list")
    if accounting["unresolved"] != 0:
        raise ValueError(f"campaign manifest has unresolved evidence: {accounting['unresolved']}")

    analysis_config = data.get("analysis_config")
    if not isinstance(analysis_config, dict) or not analysis_config:
        analysis_config = {
            "ir_builder_digest": _sha256_file(Path(build_trajectory_ir.__code__.co_filename)),
            "pack_builder_digest": _sha256_file(Path(build_evidence_pack.__code__.co_filename)),
            "token_budget": DEFAULT_TOKEN_BUDGET,
            "redaction_profile_digest": RedactionPolicy().compute_digest(),
            "judge_configuration_digests": [],
            "calibration_version": None,
            "acceptance_policy_digest": canonical_json_digest(
                {
                    "auto_acceptance_enabled": False,
                    "gate_order": list(DETERMINISTIC_GATE_ORDER),
                }
            ),
        }
    body = {
        "schema_version": "campaign-analysis-manifest/v1",
        "campaign_id": data.get("campaign", ""),
        "source_campaign_manifest_digest": _sha256_file(path),
        "source_commit": data.get("commit_sha"),
        "authorizing_actor": data.get("authorizing_actor", ""),
        "cas_store_root": data.get("cas_store_root", "derived/evidence-cas"),
        "items": [item.model_dump(mode="json") for item in items],
        "accounting": accounting,
        "analysis_config": analysis_config,
    }
    produced_at = datetime.now(UTC)
    manifest_id = canonical_json_digest(body)
    manifest_digest = canonical_json_digest({**body, "manifest_id": manifest_id})
    return CampaignAnalysisManifest(
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        produced_at=produced_at,
        **body,
    )


# ---------------------------------------------------------------------------
# Coverage gaps and deterministic judgment
# ---------------------------------------------------------------------------


def _coverage_gaps(ir: TrajectoryIR, pack: EvidencePack) -> list[str]:
    gaps: list[str] = ["judge_execution_disabled"]
    if pack.overflow_reason:
        gaps.append(pack.overflow_reason)
    if pack.quality_status in ("quarantine", "fail", "quarantined", "no_atif", "unknown"):
        gaps.append(pack.quality_status)
    if ir.quality_status == "warn" or pack.quality_status == "warn":
        gaps.append("quality_warning")
    if ir.unpaired_tool_calls_count > 0:
        gaps.append("ATIF_UNPAIRED_TOOL_CALL")
    if ir.linkage_coverage in ("degraded", "unlinked"):
        gaps.append("unpaired_linkage")
    if not pack.is_model_callable:
        gaps.append("pack_incomplete")
    return sorted(set(gaps))


def build_machine_judgment(
    pack: EvidencePack, ir: TrajectoryIR, coverage_gaps: list[str]
) -> MachineJudgment:
    """Build a deterministic-abstention MachineJudgment for this pack."""
    output_schema_digest = canonical_json_digest({"schema_name": "machine-judgment/v1"})
    coverage = sorted(set(coverage_gaps))
    finding_summary = "Judge execution disabled; deterministic abstention."
    if coverage:
        finding_summary = f"Judge execution disabled; coverage gaps: {', '.join(coverage)}."
    confidence = JudgmentConfidence(
        raw_label=None,
        raw_score=None,
        calibrated_probability=None,
        calibration_version=None,
    )
    body: dict[str, Any] = {
        "schema_version": "machine-judgment/v1",
        "producer_kind": "deterministic_abstention",
        "pack_id": pack.pack_digest,
        "pack_digest": pack.pack_digest,
        "validity": "insufficient_evidence",
        "primary_label": None,
        "finding_summary": finding_summary,
        "earliest_supported_event_id": None,
        "citation_ids": [],
        "alternative_explanations": [],
        "coverage_gaps": coverage,
        "proposed_discriminator": None,
        "confidence": confidence.model_dump(mode="json"),
        "model_identity": None,
        "prompt_digest": None,
        "rubric_digest": None,
        "output_schema_digest": output_schema_digest,
        "raw_response_digest": None,
    }
    judgment_id = canonical_json_digest(body)
    judgment_digest = canonical_json_digest({**body, "judgment_id": judgment_id})
    return MachineJudgment(
        judgment_id=judgment_id,
        judgment_digest=judgment_digest,
        produced_at=datetime.now(UTC),
        **body,
    )


# ---------------------------------------------------------------------------
# Pure ordered D-gates
# ---------------------------------------------------------------------------


def _platform_citation_id(handle: CitationHandle) -> str:
    return canonical_json_digest(handle.to_dict())


def _resolve_event_citation(
    platform_id: str, ir: TrajectoryIR, pack: EvidencePack
) -> tuple[IREvent, CitationHandle] | None:
    """Resolve one canonical locator; duplicate bindings are ambiguous and fail closed."""
    matches: dict[tuple[str, str], tuple[IREvent, CitationHandle]] = {}
    event_by_id = {event.event_id: event for event in ir.events}
    for event in ir.events:
        handle = event.source_citation
        if _platform_citation_id(handle) == platform_id:
            key = (event.event_id, canonical_json_digest(handle.to_dict()))
            matches[key] = (event, handle)
    for window in pack.selected_windows:
        for event_payload in window.events:
            source_citation = event_payload.get("source_citation")
            event_id = event_payload.get("event_id")
            if not isinstance(source_citation, dict) or not isinstance(event_id, str):
                continue
            try:
                handle = CitationHandle(**source_citation)
            except (TypeError, ValueError):
                continue
            if _platform_citation_id(handle) != platform_id or event_id not in event_by_id:
                continue
            key = (event_id, canonical_json_digest(handle.to_dict()))
            matches[key] = (event_by_id[event_id], handle)
    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


def _step_in_windows(step_index: int, pack: EvidencePack) -> tuple[bool, bool]:
    in_selected = any(w.step_start <= step_index <= w.step_end for w in pack.selected_windows)
    in_omitted = any(o.step_start <= step_index <= o.step_end for o in pack.omitted_ranges)
    return in_selected, in_omitted


def _data_contract_digest(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _expected_pack_source_digests(ir: TrajectoryIR, pack: EvidencePack) -> dict[str, str]:
    """Exact pack source map: IR source digests plus ir_digest and pack redaction.

    Pack ``source_digests`` must equal this map. Missing, extra, or mismatched
    keys are integrity failures; a subset match is not sufficient.
    """
    expected = dict(ir.source_digests)
    expected["ir_digest"] = ir.ir_digest
    expected["redaction_profile_digest"] = pack.redaction_profile_digest
    return expected


def _pack_payload_structure_errors(
    ir_payload: dict[str, Any],
    pack_payload: dict[str, Any],
) -> list[str]:
    """Validate exact lossless event partitioning for serialized IR and pack payloads."""
    errors: list[str] = []
    events = ir_payload.get("events")
    selected_windows = pack_payload.get("selected_windows")
    omitted_ranges = pack_payload.get("omitted_ranges")
    if not isinstance(events, list):
        return ["invalid_ir_events"]
    if not isinstance(selected_windows, list) or not isinstance(omitted_ranges, list):
        return ["invalid_pack_collection_shape"]

    event_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            errors.append("invalid_ir_event")
            continue
        event_id = str(event["event_id"])
        if event_id in event_by_id:
            errors.append("duplicate_ir_event_id")
        event_by_id[event_id] = event

    def _anchor_matches(
        anchor: Any,
        source: Any,
        *,
        step_start: Any,
    ) -> bool:
        if not isinstance(anchor, dict) or not isinstance(source, dict):
            return False
        anchor_cas = anchor.get("raw_cas_uri") or anchor.get("cas_uri")
        source_cas = source.get("raw_cas_uri") or source.get("cas_uri")
        anchor_step = anchor.get("step_id", anchor.get("step_index"))
        return (
            anchor.get("source_path") == source.get("source_path")
            and anchor.get("source_sha256") == source.get("source_sha256")
            and anchor_cas == source_cas
            and anchor_step == step_start
            and anchor.get("target_type") == "step"
        )

    selected_ids: list[str] = []
    for window in selected_windows:
        if not isinstance(window, dict):
            errors.append("invalid_selected_window")
            continue
        window_events = window.get("events")
        if not isinstance(window_events, list):
            errors.append("invalid_selected_events")
            continue
        if window.get("event_count") != len(window_events):
            errors.append("selected_event_count_mismatch")
        window_steps: list[int] = []
        first_source: dict[str, Any] | None = None
        for event_payload in window_events:
            if not isinstance(event_payload, dict):
                errors.append("invalid_selected_event")
                continue
            event_id = event_payload.get("event_id")
            if not isinstance(event_id, str) or event_id not in event_by_id:
                errors.append("selected_event_missing_from_ir")
                continue
            selected_ids.append(event_id)
            canonical_event = event_by_id[event_id]
            base_payload = {
                key: value for key, value in event_payload.items() if key != "hydrated_content"
            }
            if canonical_json_digest(base_payload) != canonical_json_digest(canonical_event):
                errors.append("selected_event_payload_mismatch")
            if not isinstance(event_payload.get("hydrated_content"), str):
                errors.append("selected_event_hydration_missing")
            step_index = canonical_event.get("step_index")
            if not isinstance(step_index, int) or isinstance(step_index, bool):
                errors.append("selected_event_step_invalid")
            else:
                window_steps.append(step_index)
            if first_source is None:
                source = canonical_event.get("source_citation")
                first_source = source if isinstance(source, dict) else None
        step_start = window.get("step_start")
        step_end = window.get("step_end")
        if (
            not window_steps
            or step_start != min(window_steps)
            or step_end != max(window_steps)
            or any(not (step_start <= step <= step_end) for step in window_steps)
        ):
            errors.append("selected_window_bounds_mismatch")
        if not _anchor_matches(
            window.get("reopening_citation"),
            first_source,
            step_start=step_start,
        ):
            errors.append("selected_reopening_locator_mismatch")

    omitted_ids: list[str] = []
    for omitted in omitted_ranges:
        if not isinstance(omitted, dict):
            errors.append("invalid_omitted_range")
            continue
        event_ids = omitted.get("event_ids")
        if not isinstance(event_ids, list) or not event_ids:
            errors.append("omitted_event_ids_empty")
            continue
        if omitted.get("event_count") != len(event_ids):
            errors.append("omitted_event_count_mismatch")
        if not all(isinstance(event_id, str) and event_id in event_by_id for event_id in event_ids):
            errors.append("omitted_event_missing_from_ir")
            continue
        omitted_events = [event_by_id[str(event_id)] for event_id in event_ids]
        if canonical_json_digest(omitted_events) != omitted.get("omitted_content_digest"):
            errors.append("omitted_content_digest_mismatch")
        omitted_ids.extend(str(event_id) for event_id in event_ids)
        omitted_steps: list[int] = []
        invalid_omitted_step = False
        for event in omitted_events:
            step = event.get("step_index")
            if isinstance(step, int) and not isinstance(step, bool):
                omitted_steps.append(step)
            else:
                invalid_omitted_step = True
        if (
            invalid_omitted_step
            or not omitted_steps
            or omitted.get("step_start") != min(omitted_steps)
            or omitted.get("step_end") != max(omitted_steps)
        ):
            errors.append("omitted_range_bounds_mismatch")
        first_source = omitted_events[0].get("source_citation")
        if not _anchor_matches(
            omitted.get("reopening_citation"),
            first_source,
            step_start=omitted.get("step_start"),
        ):
            errors.append("omitted_reopening_locator_mismatch")

    if len(selected_ids) != len(set(selected_ids)):
        errors.append("duplicate_selected_event")
    if len(omitted_ids) != len(set(omitted_ids)):
        errors.append("duplicate_omitted_event")
    if set(selected_ids) & set(omitted_ids):
        errors.append("selected_omitted_overlap")
    if set(selected_ids) | set(omitted_ids) != set(event_by_id):
        errors.append("pack_event_coverage_mismatch")
    return sorted(set(errors))


def _pack_structure_errors(ir: TrajectoryIR, pack: EvidencePack) -> list[str]:
    """Validate lossless, non-overlapping IR event accounting across the bounded pack."""
    return _pack_payload_structure_errors(ir.to_dict(), pack.to_dict())


def _validate_artifact_digests(
    ir: TrajectoryIR, pack: EvidencePack, judgment: MachineJudgment
) -> None:
    ir_payload = ir.to_dict()
    ir_digest = ir_payload.pop("ir_digest", "")
    if ir_digest != _data_contract_digest(ir_payload):
        raise ValueError("invalid ir_digest")

    pack_payload = pack.to_dict()
    pack_digest = pack_payload.pop("pack_digest", "")
    if pack_digest != _data_contract_digest(pack_payload):
        raise ValueError("invalid pack_digest")
    expected_sources = _expected_pack_source_digests(ir, pack)
    if pack.source_digests != expected_sources:
        raise ValueError("pack source_digests do not match IR")
    if (pack.trial_id, pack.job_id) != (ir.trial_id, ir.job_id):
        raise ValueError("pack trial identity does not match IR")

    structure_errors = _pack_structure_errors(ir, pack)
    if structure_errors:
        raise ValueError(f"invalid pack structure: {', '.join(structure_errors)}")
    MachineJudgment.model_validate(judgment.model_dump(mode="json"))


def _has_supported_claim(judgment: MachineJudgment) -> bool:
    return judgment.producer_kind == "model" and judgment.validity == "supported"


def evaluate_deterministic_gates(
    *, ir: TrajectoryIR, pack: EvidencePack, judgment: MachineJudgment, cas_store: Path
) -> list[GateResult]:
    """Evaluate the frozen 14 deterministic D-gates in normative order."""
    cas_store = cas_store.resolve()
    supported_claim = _has_supported_claim(judgment)

    resolved: list[tuple[str, IREvent, CitationHandle]] = []
    unresolved: list[str] = []
    for cid in judgment.citation_ids:
        match = _resolve_event_citation(cid, ir, pack)
        if match is None:
            unresolved.append(cid)
        else:
            resolved.append((cid, *match))

    all_cited = sorted(set(judgment.citation_ids))

    # C1_resolve
    if not judgment.citation_ids:
        c1 = GateResult(
            gate_id="C1_resolve",
            status="fail" if supported_claim else "pass",
            reason_code="citation_unresolved" if supported_claim else None,
            citation_ids=[],
        )
    elif unresolved:
        c1 = GateResult(
            gate_id="C1_resolve",
            status="fail",
            reason_code="citation_unresolved",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        c1 = GateResult(
            gate_id="C1_resolve",
            status="pass",
            reason_code=None,
            citation_ids=all_cited,
        )

    # C2_digest
    if not judgment.citation_ids:
        c2 = GateResult(
            gate_id="C2_digest",
            status="unknown" if supported_claim else "pass",
            reason_code="source_missing" if supported_claim else None,
            citation_ids=[],
        )
    elif unresolved:
        c2 = GateResult(
            gate_id="C2_digest",
            status="unknown",
            reason_code="source_missing",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        mismatched: list[str] = []
        missing_digest: list[str] = []
        for cid, _, handle in resolved:
            if not handle.content_sha256:
                missing_digest.append(cid)
                continue
            hydrated = hydrate_citation(handle, repo_root=cas_store)
            if hydrated.redaction_metadata.get("limitation_reason"):
                missing_digest.append(cid)
            elif hydrated.content_sha256 != handle.content_sha256:
                mismatched.append(cid)
        if mismatched:
            c2 = GateResult(
                gate_id="C2_digest",
                status="fail",
                reason_code="digest_mismatch",
                citation_ids=sorted(set(mismatched)),
            )
        elif missing_digest:
            c2 = GateResult(
                gate_id="C2_digest",
                status="unknown",
                reason_code="source_missing",
                citation_ids=sorted(set(missing_digest)),
            )
        else:
            c2 = GateResult(
                gate_id="C2_digest",
                status="pass",
                reason_code=None,
                citation_ids=all_cited,
            )

    # C3_source
    if not judgment.citation_ids:
        c3 = GateResult(
            gate_id="C3_source",
            status="unknown" if supported_claim else "pass",
            reason_code="source_missing" if supported_claim else None,
            citation_ids=[],
        )
    elif unresolved:
        c3 = GateResult(
            gate_id="C3_source",
            status="unknown",
            reason_code="source_missing",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        source_mismatch: list[str] = []
        source_missing: list[str] = []
        ir_source = ir.source_digests.get("source_sha256", "")
        ir_cas = ir.source_digests.get("cas_uri", "")
        for cid, _, handle in resolved:
            handle_cas = handle.raw_cas_uri or handle.cas_uri or ""
            if not handle.source_sha256 or not ir_source or not handle_cas or not ir_cas:
                source_missing.append(cid)
                continue
            if handle.source_sha256 != ir_source or handle_cas != ir_cas:
                source_mismatch.append(cid)
                continue
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    restore_evidence(cas_store, handle_cas, Path(temporary))
            except Exception:
                source_missing.append(cid)
        if source_mismatch:
            c3 = GateResult(
                gate_id="C3_source",
                status="fail",
                reason_code="source_digest_mismatch",
                citation_ids=sorted(set(source_mismatch)),
            )
        elif source_missing:
            c3 = GateResult(
                gate_id="C3_source",
                status="unknown",
                reason_code="source_missing",
                citation_ids=sorted(set(source_missing)),
            )
        else:
            c3 = GateResult(
                gate_id="C3_source",
                status="pass",
                reason_code=None,
                citation_ids=all_cited,
            )

    # C4_window
    if not judgment.citation_ids:
        c4 = GateResult(
            gate_id="C4_window",
            status="fail" if supported_claim else "pass",
            reason_code="citation_unresolved" if supported_claim else None,
            citation_ids=[],
        )
    elif unresolved:
        c4 = GateResult(
            gate_id="C4_window",
            status="unknown",
            reason_code="citation_unresolved",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        omitted_cits: list[str] = []
        outside_cits: list[str] = []
        for cid, ev, _ in resolved:
            in_selected, in_omitted = _step_in_windows(ev.step_index, pack)
            if in_selected:
                continue
            if in_omitted:
                omitted_cits.append(cid)
            else:
                outside_cits.append(cid)
        invalid_cits = sorted(set(outside_cits + omitted_cits))
        if invalid_cits:
            c4 = GateResult(
                gate_id="C4_window",
                status="fail",
                reason_code="citation_unresolved",
                citation_ids=invalid_cits,
            )
        else:
            c4 = GateResult(
                gate_id="C4_window",
                status="pass",
                reason_code=None,
                citation_ids=all_cited,
            )

    # C5_entail (never passes; no generated summary is evidence)
    c5 = GateResult(
        gate_id="C5_entail",
        status="unknown",
        reason_code="entailment_disabled",
        citation_ids=[],
    )

    # C6_no_future (abstention has no earliest supported event)
    if not judgment.citation_ids or judgment.earliest_supported_event_id is None:
        c6 = GateResult(gate_id="C6_no_future", status="pass", reason_code=None, citation_ids=[])
    elif unresolved:
        c6 = GateResult(
            gate_id="C6_no_future",
            status="unknown",
            reason_code="citation_unresolved",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        earliest_ordinal: int | None = None
        for ev in ir.events:
            if ev.event_id == judgment.earliest_supported_event_id:
                earliest_ordinal = ev.event_ordinal
                break
        if earliest_ordinal is None:
            c6 = GateResult(
                gate_id="C6_no_future",
                status="unknown",
                reason_code="earliest_event_unresolved",
                citation_ids=all_cited,
            )
        else:
            future_cits = [cid for cid, ev, _ in resolved if ev.event_ordinal > earliest_ordinal]
            if future_cits:
                c6 = GateResult(
                    gate_id="C6_no_future",
                    status="fail",
                    reason_code="no_future",
                    citation_ids=sorted(set(future_cits)),
                )
            else:
                c6 = GateResult(
                    gate_id="C6_no_future",
                    status="pass",
                    reason_code=None,
                    citation_ids=all_cited,
                )

    # C7_actor_cone
    if not judgment.citation_ids or judgment.producer_kind == "deterministic_abstention":
        c7 = GateResult(gate_id="C7_actor_cone", status="pass", reason_code=None, citation_ids=[])
    elif unresolved:
        c7 = GateResult(
            gate_id="C7_actor_cone",
            status="unknown",
            reason_code="citation_unresolved",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        c7 = GateResult(
            gate_id="C7_actor_cone",
            status="unknown",
            reason_code="actor_cone_unverified",
            citation_ids=all_cited,
        )

    # C8_search_before_absence
    negative_claim = (
        judgment.primary_label is not None
        and judgment.primary_label.class_id == "false_verification_or_unsupported_terminal_claim"
    )
    if judgment.coverage_gaps:
        c8 = GateResult(
            gate_id="C8_search_before_absence",
            status="unknown",
            reason_code="coverage_gap",
            citation_ids=[],
        )
    elif negative_claim:
        check_citations = sorted(
            {
                cid
                for cid, ev, _ in resolved
                if ev.action_family == "verification" or ev.event_type == "verifier_check"
            }
        )
        c8 = GateResult(
            gate_id="C8_search_before_absence",
            status="pass" if check_citations else "fail",
            reason_code=None if check_citations else "false_verification",
            citation_ids=check_citations,
        )
    else:
        c8 = GateResult(
            gate_id="C8_search_before_absence",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )

    # C9_verifier_priority (pass unless IR final_verdict is schema-invalid, which it is not)
    if judgment.producer_kind == "deterministic_abstention" or judgment.validity != "supported":
        c9 = GateResult(
            gate_id="C9_verifier_priority",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )
    else:
        c9 = GateResult(
            gate_id="C9_verifier_priority",
            status="unknown",
            reason_code="verifier_priority_unverified",
            citation_ids=all_cited,
        )

    # C10_omitted (every omitted range must reopen the exact hashed event set)
    unreopenable: list[str] = []
    for omitted in pack.omitted_ranges:
        handle = omitted.reopening_citation
        citation_id = _platform_citation_id(handle)
        handle_cas = handle.raw_cas_uri or handle.cas_uri
        if (
            not handle_cas
            or handle_cas != ir.source_digests.get("cas_uri")
            or handle.source_sha256 != ir.source_digests.get("source_sha256")
        ):
            unreopenable.append(citation_id)
            continue
        try:
            reopened = reopen_omitted_range(
                pack,
                omitted.range_id,
                ir=ir,
                store_root=cas_store,
            )
            hydrated = hydrate_citation(handle, repo_root=cas_store)
            reopened_ids = [event.get("event_id") for event in reopened.events]
            if reopened_ids != list(omitted.event_ids):
                raise ValueError("reopened event identities differ from omitted range")
            event_by_id = {event.event_id: event for event in ir.events}
            for reopened_event in reopened.events:
                event_id = reopened_event.get("event_id")
                source_event = event_by_id.get(str(event_id))
                if source_event is None:
                    raise ValueError("reopened event is absent from IR")
                member = hydrate_citation(source_event.source_citation, repo_root=cas_store)
                if (
                    member.redaction_metadata.get("limitation_reason")
                    or member.redaction_metadata.get("content_digest_mismatch")
                    or reopened_event.get("hydrated_content") != member.redacted_content
                ):
                    raise ValueError("reopened event hydration is incomplete")
        except Exception:
            unreopenable.append(citation_id)
            continue
        if hydrated.redaction_metadata.get("limitation_reason") or hydrated.redaction_metadata.get(
            "content_digest_mismatch"
        ):
            unreopenable.append(citation_id)
    if unreopenable:
        c10 = GateResult(
            gate_id="C10_omitted",
            status="unknown",
            reason_code="omitted_unreopenable",
            citation_ids=sorted(set(unreopenable)),
        )
    else:
        c10 = GateResult(
            gate_id="C10_omitted",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )

    # schema_valid
    try:
        _validate_artifact_digests(ir, pack, judgment)
        schema_valid = GateResult(
            gate_id="schema_valid",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )
    except Exception:
        schema_valid = GateResult(
            gate_id="schema_valid",
            status="fail",
            reason_code="schema_invalid",
            citation_ids=[],
        )

    # pack_complete
    structure_errors = _pack_structure_errors(ir, pack)
    if pack.is_model_callable and not pack.abstain_required and not structure_errors:
        pack_complete = GateResult(
            gate_id="pack_complete",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )
    elif pack.overflow_reason and "budget_overflow" in pack.overflow_reason:
        pack_complete = GateResult(
            gate_id="pack_complete",
            status="unknown",
            reason_code="mandatory_window_overflow",
            citation_ids=[],
        )
    else:
        pack_complete = GateResult(
            gate_id="pack_complete",
            status="unknown",
            reason_code="pack_incomplete",
            citation_ids=[],
        )

    # not_quarantined
    if pack.quality_status in _QUARANTINE_STATUSES:
        not_quarantined = GateResult(
            gate_id="not_quarantined",
            status="fail",
            reason_code="quarantined_input",
            citation_ids=[],
        )
    elif pack.quality_status == "no_atif":
        not_quarantined = GateResult(
            gate_id="not_quarantined",
            status="unknown",
            reason_code="no_atif_unsupported",
            citation_ids=[],
        )
    elif pack.quality_status in ("pass", "warn"):
        not_quarantined = GateResult(
            gate_id="not_quarantined",
            status="pass",
            reason_code=None,
            citation_ids=[],
        )
    else:
        not_quarantined = GateResult(
            gate_id="not_quarantined",
            status="unknown",
            reason_code="unrecognized_quality_status",
            citation_ids=[],
        )

    # not_hold_gold (calibration is hold-only, never pass)
    not_hold_gold = GateResult(
        gate_id="not_hold_gold",
        status="fail",
        reason_code="class_not_enabled",
        citation_ids=[],
    )

    return [
        c1,
        c2,
        c3,
        c4,
        c5,
        c6,
        c7,
        c8,
        c9,
        c10,
        schema_valid,
        pack_complete,
        not_quarantined,
        not_hold_gold,
    ]


# ---------------------------------------------------------------------------
# Calibration class gate
# ---------------------------------------------------------------------------


def build_calibration_class_gate(
    *,
    class_id: str | None = None,
    report_path: Path | None = None,
) -> CalibrationClassGate:
    """Build the exact hold-only class gate for a judgment and report."""
    gate_class_id = class_id or "unlabeled_deterministic_abstention"
    if report_path is not None:
        if not report_path.is_file():
            raise RuntimeError(f"source_missing: calibration report not found: {report_path}")
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        try:
            report = parse_calibration_report(payload)
        except (UnsupportedCalibrationVersion, ValidationError, ValueError) as exc:
            raise RuntimeError(f"schema_mismatch: cannot parse calibration report: {exc}") from exc

        report_schema: Literal["calibration-report-v1", "calibration-report-v1.1"]
        report_schema = (
            "calibration-report-v1"
            if isinstance(report, CalibrationReport)
            else "calibration-report-v1.1"
        )
        class_row = report.classes.get(gate_class_id)
        if class_row is None:
            reliability: dict[str, Any] = {}
            hold_reasons = ["class_not_calibrated"]
        else:
            reliability = class_row.model_dump(mode="json")
            hold_reasons = list(class_row.hold_reasons)
        if "acceptance_enabling_disabled" not in hold_reasons:
            hold_reasons.append("acceptance_enabling_disabled")

        report_digest = canonical_json_digest(report.model_dump(mode="json", by_alias=True))
        return CalibrationClassGate(
            class_id=gate_class_id,
            calibration_version=report.calibration_version,
            report_digest=report_digest,
            report_schema=report_schema,
            thresholds_digest=report.thresholds_digest,
            acceptance_enabling_allowed=False,
            acceptance_enabled=False,
            hold_reasons=sorted(set(hold_reasons)),
            reliability_snapshot=reliability,
        )

    unavailable = {
        "schema": "calibration-report-v1",
        "status": "unavailable",
        "reason": "calibration_report_unavailable",
    }
    return CalibrationClassGate(
        class_id=gate_class_id,
        calibration_version=canonical_json_digest(unavailable),
        report_digest=canonical_json_digest(unavailable),
        report_schema="calibration-report-v1",
        thresholds_digest=canonical_json_digest({"status": "unavailable", "thresholds": []}),
        acceptance_enabling_allowed=False,
        acceptance_enabled=False,
        hold_reasons=[
            "acceptance_enabling_disabled",
            "calibration_report_unavailable",
        ],
        reliability_snapshot={},
    )


# ---------------------------------------------------------------------------
# Acceptance decision
# ---------------------------------------------------------------------------


def build_acceptance_decision(
    judgment: MachineJudgment,
    pack: EvidencePack,
    ir: TrajectoryIR,
    *,
    calibration_class_gate: CalibrationClassGate,
    cas_store: Path,
) -> AcceptanceDecision:
    """Evaluate deterministic gates and build the immutable AcceptanceDecision."""
    deterministic_gates = evaluate_deterministic_gates(
        ir=ir, pack=pack, judgment=judgment, cas_store=cas_store
    )
    policy_body = {
        "auto_acceptance_enabled": False,
        "deterministic_gate_order": list(DETERMINISTIC_GATE_ORDER),
        "schema_version": "acceptance-decision/v1",
    }
    policy_digest = canonical_json_digest(policy_body)
    cross_judge = CrossJudgeRecord(
        required=False,
        judge_families=[],
        class_ids=[],
        agreement="not_required",
    )
    decision = evaluate_acceptance(
        judgment_ids=[judgment.judgment_id],
        pack_digest=pack.pack_digest,
        deterministic_gates=deterministic_gates,
        cross_judge=cross_judge,
        calibration_class_gate=calibration_class_gate,
        policy_digest=policy_digest,
        proposed_next_check=None,
        supersedes_decision_id=None,
    )
    if decision.decision == "accepted":
        raise RuntimeError("accepted: illegal while auto_acceptance is disabled")
    return decision


# ---------------------------------------------------------------------------
# Atomic immutable artifact writer
# ---------------------------------------------------------------------------


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return f"sha256:{sha256_file(path)}"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _write_artifact_sidecar(path: Path, payload: dict[str, Any]) -> str:
    """Write a canonical JSON sidecar with write-once identity idempotency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _durable_mkdir(path.parent)
    new_bytes = _canonical_json_bytes(payload)

    if path.exists():
        existing_bytes = path.read_bytes()
        if existing_bytes == new_bytes:
            return _sha256_file(path)
        existing_payload = json.loads(existing_bytes.decode("utf-8"))
        stable_existing = dict(existing_payload)
        stable_new = dict(payload)
        stable_existing.pop("produced_at", None)
        stable_new.pop("produced_at", None)
        if stable_existing == stable_new:
            return _sha256_file(path)
        raise ValueError(f"digest_mismatch: {path.name} already exists with different bytes")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(new_bytes)
    _durable_replace(tmp, path)
    return _sha256_file(path)


def write_interpretation_artifacts(
    ir: TrajectoryIR,
    pack: EvidencePack,
    judgment: MachineJudgment,
    decision: AcceptanceDecision,
    *,
    output_dir: Path,
    cas_store: Path,
) -> list[ArtifactRecord]:
    """Persist IR/pack/judgment/decision sidecars, archive them, and return records."""
    output_dir = output_dir.resolve()
    cas_store = cas_store.resolve()
    artifact_dir = output_dir / ir.trial_id / decision.decision_id.removeprefix("sha256:")
    _durable_mkdir(artifact_dir)

    ir_path = artifact_dir / "trajectory_ir.json"
    pack_path = artifact_dir / "evidence_pack.json"
    judgment_path = artifact_dir / "machine_judgment.json"
    decision_path = artifact_dir / "acceptance_decision.json"

    ir_payload = ir.to_dict()
    pack_payload = pack.to_dict()
    judgment_payload = judgment.model_dump(mode="json")
    decision_payload = decision.model_dump(mode="json")

    ir_content_digest = _write_artifact_sidecar(ir_path, ir_payload)
    pack_content_digest = _write_artifact_sidecar(pack_path, pack_payload)
    judgment_content_digest = _write_artifact_sidecar(judgment_path, judgment_payload)
    decision_content_digest = _write_artifact_sidecar(decision_path, decision_payload)

    archive = archive_evidence(
        source=artifact_dir,
        store_root=cas_store,
        record_id=decision.decision_id,
        kind="interpretation",
    )
    if (
        _load_interpretation_archive_record(
            cas_store,
            decision.decision_id,
            sidecar_dir=artifact_dir,
        )
        is None
    ):
        raise ValueError("interpretation archive integrity verification failed")

    ir_created = _parse_iso_datetime(ir.created_at) or judgment.produced_at
    pack_created = _parse_iso_datetime(pack.created_at) or judgment.produced_at

    return [
        ArtifactRecord(
            artifact_digest=ir.ir_digest,
            kind="ir",
            trial_id=ir.trial_id,
            job_id=ir.job_id,
            content_digest=ir_content_digest,
            artifact_path=ir_path,
            cas_uri=archive.uri,
            pack_digest=pack.pack_digest,
            judgment_id=judgment.judgment_id,
            decision_id=decision.decision_id,
            produced_at=ir_created,
        ),
        ArtifactRecord(
            artifact_digest=pack.pack_digest,
            kind="pack",
            trial_id=pack.trial_id,
            job_id=pack.job_id,
            content_digest=pack_content_digest,
            artifact_path=pack_path,
            cas_uri=archive.uri,
            pack_digest=pack.pack_digest,
            judgment_id=judgment.judgment_id,
            decision_id=decision.decision_id,
            produced_at=pack_created,
        ),
        ArtifactRecord(
            artifact_digest=judgment.judgment_id,
            kind="judgment",
            trial_id=ir.trial_id,
            job_id=ir.job_id,
            content_digest=judgment_content_digest,
            artifact_path=judgment_path,
            cas_uri=archive.uri,
            pack_digest=pack.pack_digest,
            judgment_id=judgment.judgment_id,
            decision_id=decision.decision_id,
            judgment_digest=judgment.judgment_digest,
            produced_at=judgment.produced_at,
            producer_kind=judgment.producer_kind,
            validity=judgment.validity,
            citation_ids=list(judgment.citation_ids),
            coverage_gaps=list(judgment.coverage_gaps),
        ),
        ArtifactRecord(
            artifact_digest=decision.decision_id,
            kind="decision",
            trial_id=ir.trial_id,
            job_id=ir.job_id,
            content_digest=decision_content_digest,
            artifact_path=decision_path,
            cas_uri=archive.uri,
            pack_digest=pack.pack_digest,
            judgment_id=judgment.judgment_id,
            decision_id=decision.decision_id,
            decision_digest=decision.decision_digest,
            produced_at=decision.produced_at,
            decision=decision.decision,
            judgment_ids=list(decision.judgment_ids),
            reason_codes=list(decision.reason_codes),
            calibration_version=decision.calibration_version,
            calibration_schema=decision.calibration_class_gate.report_schema,
            status=decision.decision,
            supersedes_decision_id=decision.supersedes_decision_id,
        ),
        ArtifactRecord(
            artifact_digest=archive.content_digest,
            kind="interpretation",
            trial_id=ir.trial_id,
            job_id=ir.job_id,
            content_digest=archive.content_digest,
            artifact_path=artifact_dir,
            cas_uri=archive.uri,
            pack_digest=pack.pack_digest,
            judgment_id=judgment.judgment_id,
            decision_id=decision.decision_id,
            judgment_digest=judgment.judgment_digest,
            decision_digest=decision.decision_digest,
            produced_at=decision.produced_at,
        ),
    ]


# ---------------------------------------------------------------------------
# Single-trial analysis
# ---------------------------------------------------------------------------


def _analyze_trial_core(
    target: str | Path | Mapping[str, Any],
    *,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path,
    database_url: str | None = None,
    calibration_report: Path | None = None,
    rebuild_projections: bool = True,
) -> tuple[dict[str, Any], list[ArtifactRecord]]:
    """Build, judge, and persist one interpretation; return result dict and records."""
    repo_root = repo_root.resolve()
    store_root = store_root.resolve()
    output_dir = output_dir.resolve()
    derived_root = derived_root.resolve()

    inventory: Mapping[str, Any]
    cas_uri: str | None = None
    temp_extract: tempfile.TemporaryDirectory[str] | None = None
    extracted: Path

    if isinstance(target, Mapping):
        inventory = target
        cas_uri = inventory.get("cas_uri")
        if not cas_uri:
            raise RuntimeError("missing_cas: mapping target requires cas_uri")
        if inventory.get("quality_status") in _QUARANTINE_STATUSES:
            raise RuntimeError(f"quarantined_input: {inventory.get('quality_status')}")
        temp_extract = tempfile.TemporaryDirectory()
        extracted = Path(temp_extract.name)
    elif isinstance(target, str) and target.startswith("cas://"):
        inventory = {}
        cas_uri = target
        temp_extract = tempfile.TemporaryDirectory()
        extracted = Path(temp_extract.name)
    else:
        inventory = {}
        local_target = target if isinstance(target, Path) else Path(target)
        extracted = (
            local_target if local_target.is_absolute() else repo_root / local_target
        ).resolve()
        if not extracted.is_dir():
            raise RuntimeError(f"schema_mismatch: trial directory does not exist: {target}")

    try:
        if cas_uri:
            try:
                restore_evidence(store_root, cas_uri, extracted)
            except FileNotFoundError as exc:
                raise _classify_cas_restore_error(exc) from exc
            except Exception as exc:
                raise _classify_cas_restore_error(exc) from exc

        try:
            ir = build_trajectory_ir(
                dict(inventory) if inventory else extracted,
                repo_root=repo_root,
                store_root=store_root,
            )
        except CASTrialResolutionError as exc:
            raise RuntimeError(f"cas_integrity_error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"schema_mismatch: {exc}") from exc

        pack = build_evidence_pack(
            ir,
            trial_dir=extracted,
            repo_root=repo_root,
            store_root=store_root,
        )

        if ir.quality_status in _QUARANTINE_STATUSES or pack.quality_status in _QUARANTINE_STATUSES:
            raise RuntimeError(f"quarantined_input: {pack.quality_status}")

        coverage_gaps = _coverage_gaps(ir, pack)
        judgment = build_machine_judgment(pack, ir, coverage_gaps)
        calibration_gate = build_calibration_class_gate(
            class_id=(
                judgment.primary_label.class_id if judgment.primary_label is not None else None
            ),
            report_path=calibration_report,
        )
        decision = build_acceptance_decision(
            judgment,
            pack,
            ir,
            calibration_class_gate=calibration_gate,
            cas_store=store_root,
        )

        records = write_interpretation_artifacts(
            ir,
            pack,
            judgment,
            decision,
            output_dir=output_dir,
            cas_store=store_root,
        )

        if rebuild_projections:
            rebuild_interpretation_projections(
                output_dir,
                derived_root,
                store_root=store_root,
            )

        if database_url:
            ingest_interpretation_artifacts(database_url, records)

        result = {
            "trial_id": ir.trial_id,
            "trial_name": ir.trial_name,
            "job_id": ir.job_id,
            "ir_digest": ir.ir_digest,
            "pack_digest": pack.pack_digest,
            "judgment_id": judgment.judgment_id,
            "decision_id": decision.decision_id,
            "decision": decision.decision,
            "reason_codes": decision.reason_codes,
            "coverage_gaps": judgment.coverage_gaps,
            "source_cas_uri": cas_uri,
            "artifact_cas_uri": records[-1].cas_uri,
        }
        return result, records
    finally:
        if temp_extract is not None:
            with contextlib.suppress(Exception):
                temp_extract.cleanup()


def analyze_trial(
    target: str | Path | Mapping[str, Any],
    *,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path | None = None,
    database_url: str | None = None,
    calibration_report: Path | None = None,
) -> dict[str, Any]:
    """Analyze one cohort-style input and return the JSON-shaped result."""
    derived = derived_root or output_dir.parent
    result, _ = _analyze_trial_core(
        target,
        repo_root=repo_root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived,
        database_url=database_url,
        calibration_report=calibration_report,
    )
    return result


# ---------------------------------------------------------------------------
# Batch campaign analysis
# ---------------------------------------------------------------------------


def build_campaign_report(
    manifest: CampaignAnalysisManifest,
    results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build the campaign report from manifest accounting and per-item results."""
    reason_counts: dict[str, int] = {}
    coverage_gap_counts: dict[str, int] = {}
    accepted = 0
    rejected = 0
    abstained = 0
    role_counts: dict[str, int] = {}
    source_refs: list[dict[str, Any]] = []

    for res in results:
        decision = res.get("decision", "abstained")
        if decision == "accepted":
            accepted += 1
        elif decision == "rejected":
            rejected += 1
        else:
            abstained += 1
        for rc in res.get("reason_codes", []):
            reason_counts[rc] = reason_counts.get(rc, 0) + 1
        for gap in res.get("coverage_gaps", []):
            coverage_gap_counts[gap] = coverage_gap_counts.get(gap, 0) + 1
        source_refs.append(
            {
                "trial_id": res["trial_id"],
                "source_cas_uri": res["source_cas_uri"],
                "artifact_cas_uri": res["artifact_cas_uri"],
                "ir_digest": res["ir_digest"],
                "pack_digest": res["pack_digest"],
                "judgment_id": res["judgment_id"],
                "decision_id": res["decision_id"],
            }
        )

    for item in manifest.items:
        role_counts[item.attempt_role] = role_counts.get(item.attempt_role, 0) + 1

    body = {
        "schema_version": "campaign-report/v1",
        "manifest_id": manifest.manifest_id,
        "manifest_digest": manifest.manifest_digest,
        "campaign_id": manifest.campaign_id,
        "cohort_accounted": len(results),
        "accepted": accepted,
        "rejected": rejected,
        "abstained": abstained,
        "role_counts": role_counts,
        "reason_counts": reason_counts,
        "coverage_gap_counts": coverage_gap_counts,
        "source_refs": source_refs,
        "accounting": {
            "total_planned_specs": manifest.accounting.get("total_planned_specs"),
            "total_executed_trials": manifest.accounting.get("total_executed_trials"),
            "valid_analysis_ready_trials": manifest.accounting.get("valid_analysis_ready_trials"),
            "quarantined_infrastructure_attempts": manifest.accounting.get(
                "quarantined_infrastructure_attempts"
            ),
            "free_local_controls": manifest.accounting.get("free_local_controls"),
            "unresolved_evidence_count": manifest.accounting.get("unresolved_evidence_count"),
        },
    }
    report_id = canonical_json_digest(body)
    return {
        **body,
        "report_id": report_id,
        "report_digest": canonical_json_digest({**body, "report_id": report_id}),
    }


def write_campaign_report(
    report: dict[str, Any],
    *,
    output_dir: Path,
    store_root: Path,
) -> dict[str, Any]:
    """Persist one immutable aggregate report and archive it to CAS."""
    body = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "report_id",
            "report_digest",
            "report_cas_uri",
            "report_artifact_path",
        }
    }
    report_id = report.get("report_id")
    report_digest = report.get("report_digest")
    if report_id != canonical_json_digest(body):
        raise ValueError("campaign report_id does not match canonical content identity")
    if report_digest != canonical_json_digest({**body, "report_id": report_id}):
        raise ValueError("campaign report_digest does not match canonical content identity")

    report_dir = output_dir.resolve() / "_campaigns" / str(report_id).removeprefix("sha256:")
    report_path = report_dir / "campaign_report.json"
    _write_artifact_sidecar(report_path, report)
    archive = archive_evidence(
        source=report_dir,
        store_root=store_root,
        record_id=str(report_id),
        kind="interpretation_campaign",
    )
    return {
        **report,
        "report_cas_uri": archive.uri,
        "report_artifact_path": str(report_path),
    }


def analyze_batch(
    inventory_path: Path,
    *,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path | None = None,
    database_url: str | None = None,
    calibration_report: Path | None = None,
) -> dict[str, Any]:
    """Consume a CampaignAnalysisManifest and interpret all cohort items."""
    repo_root = repo_root.resolve()
    store_root = store_root.resolve()
    output_dir = output_dir.resolve()
    derived = (derived_root or output_dir.parent).resolve()

    manifest = load_campaign_analysis_manifest(inventory_path)
    cohort = manifest.cohort_items()

    results: list[dict[str, Any]] = []
    for item in cohort:
        if item.quality_status in _QUARANTINE_STATUSES:
            raise RuntimeError(f"quarantined_input: {item.trial_id} {item.quality_status}")
        if item.quality_status not in ("pass", "warn"):
            raise RuntimeError(
                f"schema_mismatch: cohort item {item.trial_id} has unsupported quality_status {item.quality_status}"
            )
        if not item.cas_uri:
            raise RuntimeError(f"missing_cas: {item.trial_id}")

        result, _ = _analyze_trial_core(
            item.as_inventory_dict(),
            repo_root=repo_root,
            store_root=store_root,
            output_dir=output_dir,
            derived_root=derived,
            database_url=database_url,
            calibration_report=calibration_report,
            rebuild_projections=False,
        )
        results.append(result)
    rebuild_interpretation_projections(
        output_dir,
        derived,
        store_root=store_root,
    )

    report = build_campaign_report(manifest, results)
    return write_campaign_report(
        report,
        output_dir=output_dir,
        store_root=store_root,
    )


# ---------------------------------------------------------------------------
# Inspect and calibrate commands
# ---------------------------------------------------------------------------


def _find_sidecar_set(target: str, output_dir: Path) -> Path | None:
    """Find the artifact directory containing the target identity or path."""
    if not output_dir.is_dir():
        return None
    target_path = (output_dir / target).resolve()
    if target_path.is_relative_to(output_dir):
        if target_path.is_file() and target_path.suffix == ".json":
            return target_path.parent
        if target_path.is_dir():
            return target_path

    for trial_dir in output_dir.iterdir():
        if not trial_dir.is_dir():
            continue
        for artifact_dir in trial_dir.iterdir():
            if not artifact_dir.is_dir():
                continue
            for sidecar in _SIDECAR_FILES:
                sidecar_path = artifact_dir / sidecar
                if not sidecar_path.is_file():
                    continue
                try:
                    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                identity_field = _IDENTITY_FIELDS.get(sidecar)
                if identity_field and payload.get(identity_field) == target:
                    return artifact_dir
                if (
                    str(sidecar_path) == target
                    or str(sidecar_path.relative_to(output_dir)) == target
                ):
                    return artifact_dir
    return None


def _record_cas_uri(store_root: Path, decision_id: str) -> str | None:
    record = _load_interpretation_archive_record(store_root, decision_id)
    return record[0] if record is not None else None


def analyze_inspect(
    target: str,
    *,
    output_dir: Path,
    store_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Reopen all artifact lineage and exact citations for one id or sidecar path."""
    output_dir = output_dir.resolve()
    store_root = store_root.resolve()
    artifact_dir = _find_sidecar_set(target, output_dir)
    if artifact_dir is None:
        raise ValueError(f"inspect target not found: {target}")

    ir_path = artifact_dir / "trajectory_ir.json"
    pack_path = artifact_dir / "evidence_pack.json"
    judgment_path = artifact_dir / "machine_judgment.json"
    decision_path = artifact_dir / "acceptance_decision.json"

    def _load(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    ir = _load(ir_path)
    pack = _load(pack_path)
    judgment = _load(judgment_path)
    decision = _load(decision_path)

    interpretation_cas_uri = _record_cas_uri(store_root, decision["decision_id"])
    source_cas_uri = pack.get("source_digests", {}).get("cas_uri")

    handles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in ir.get("events", []):
        sc = ev.get("source_citation")
        if sc:
            key = canonical_json_digest(sc)
            if key not in seen:
                seen.add(key)
                handles.append(sc)
    for w in pack.get("selected_windows", []):
        rc = w.get("reopening_citation")
        if rc:
            key = canonical_json_digest(rc)
            if key not in seen:
                seen.add(key)
                handles.append(rc)
        for ev in w.get("events", []):
            sc = ev.get("source_citation")
            if sc:
                key = canonical_json_digest(sc)
                if key not in seen:
                    seen.add(key)
                    handles.append(sc)
    for om in pack.get("omitted_ranges", []):
        rc = om.get("reopening_citation")
        if rc:
            key = canonical_json_digest(rc)
            if key not in seen:
                seen.add(key)
                handles.append(rc)

    citation_evidence: list[dict[str, Any]] = []
    for payload in handles:
        citation_id = canonical_json_digest(payload)
        try:
            handle = CitationHandle(**payload)
            hydrated = hydrate_citation(handle, repo_root=store_root)
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            citation_evidence.append(
                {
                    "citation_id": citation_id,
                    "availability": "unavailable" if limitation else "available",
                    "content_sha256": hydrated.content_sha256,
                    "redacted_content": hydrated.redacted_content,
                    "redaction_metadata": hydrated.redaction_metadata,
                }
            )
        except (TypeError, ValueError) as exc:
            citation_evidence.append(
                {
                    "citation_id": citation_id,
                    "availability": "unavailable",
                    "content_sha256": None,
                    "redacted_content": None,
                    "redaction_metadata": {
                        "limitation_reason": "citation_hydration_error",
                        "error_detail": f"{type(exc).__name__}: {exc}",
                    },
                }
            )

    return {
        "trial_id": ir.get("trial_id"),
        "artifact_identities": {
            "ir_digest": ir.get("ir_digest"),
            "pack_digest": pack.get("pack_digest"),
            "judgment_id": judgment.get("judgment_id"),
            "decision_id": decision.get("decision_id"),
            "source_cas_uri": source_cas_uri,
            "interpretation_cas_uri": interpretation_cas_uri,
        },
        "cas_uris": {
            "source_cas_uri": source_cas_uri,
            "interpretation_cas_uri": interpretation_cas_uri,
        },
        "citation_handles": handles,
        "citation_evidence": citation_evidence,
        "gate_results": decision.get("deterministic_gates", []),
        "reason_codes": decision.get("reason_codes", []),
    }


def analyze_calibrate(report_path: Path) -> dict[str, Any]:
    """Parse a committed CalibrationReport and report its hold-only status."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        report = parse_calibration_report(payload)
        per_class = {cls: row.acceptance_enabled for cls, row in report.classes.items()}
        return {
            "schema_name": report.schema_name,
            "acceptance_enabling_allowed": report.acceptance_enabling_allowed,
            "per_class_acceptance_enabled": per_class,
            "calibration_report_can_enable_acceptance": calibration_report_can_enable_acceptance(
                report
            ),
        }
    except (UnsupportedCalibrationVersion, ValidationError, ValueError) as exc:
        schema = payload.get("schema")
        acceptance_allowed = payload.get("acceptance_enabling_allowed")
        classes = payload.get("classes") or {}
        per_class = {cls: row.get("acceptance_enabled") for cls, row in classes.items()}
        return {
            "schema_name": schema,
            "acceptance_enabling_allowed": acceptance_allowed,
            "per_class_acceptance_enabled": per_class,
            "calibration_report_can_enable_acceptance": False,
            "parse_error": str(exc),
        }


# ---------------------------------------------------------------------------
# Parquet projections
# ---------------------------------------------------------------------------


INTERPRETATION_ARTIFACT_SCHEMA = pa.schema(
    [
        pa.field("artifact_digest", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("content_digest", pa.string(), nullable=False),
        pa.field("artifact_path", pa.string(), nullable=False),
        pa.field("cas_uri", pa.string(), nullable=True),
        pa.field("pack_digest", pa.string(), nullable=True),
        pa.field("judgment_id", pa.string(), nullable=True),
        pa.field("decision_id", pa.string(), nullable=True),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

MACHINE_JUDGMENT_SCHEMA = pa.schema(
    [
        pa.field("judgment_id", pa.string(), nullable=False),
        pa.field("judgment_digest", pa.string(), nullable=False),
        pa.field("pack_digest", pa.string(), nullable=False),
        pa.field("producer_kind", pa.string(), nullable=False),
        pa.field("validity", pa.string(), nullable=False),
        pa.field("citation_ids_json", pa.string(), nullable=False),
        pa.field("coverage_gaps_json", pa.string(), nullable=False),
        pa.field("artifact_path", pa.string(), nullable=False),
        pa.field("cas_uri", pa.string(), nullable=True),
        pa.field("produced_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

ACCEPTANCE_DECISION_SCHEMA = pa.schema(
    [
        pa.field("decision_id", pa.string(), nullable=False),
        pa.field("decision_digest", pa.string(), nullable=False),
        pa.field("decision", pa.string(), nullable=False),
        pa.field("judgment_ids_json", pa.string(), nullable=False),
        pa.field("pack_digest", pa.string(), nullable=False),
        pa.field("reason_codes_json", pa.string(), nullable=False),
        pa.field("calibration_version", pa.string(), nullable=True),
        pa.field("calibration_schema", pa.string(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("supersedes_decision_id", pa.string(), nullable=True),
        pa.field("artifact_path", pa.string(), nullable=False),
        pa.field("cas_uri", pa.string(), nullable=True),
        pa.field("produced_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


def _parse_timestamp_for_parquet(value: Any) -> datetime:
    dt = _parse_iso_datetime(value)
    return dt if dt is not None else datetime(1970, 1, 1, tzinfo=UTC)


def _write_parquet(path: Path, schema: pa.Schema, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        table = pa.Table.from_pydict({name: [] for name in schema.names}, schema=schema)
    else:
        table = pa.Table.from_pylist(rows, schema=schema)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    tmp.replace(path)


def _load_interpretation_archive_record(
    store_root: Path,
    decision_id: str,
    *,
    sidecar_dir: Path | None = None,
) -> tuple[str, str] | None:
    """Validate the record, archive bytes, restored content, and sidecar byte identity."""
    record_path = store_root.resolve() / "records" / "interpretation" / f"{decision_id}.json"
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        uri = str(record["uri"])
        content_digest = str(record["content_digest"])
        archive_digest = str(record["archive_digest"])
        if (
            record.get("record_id") != decision_id
            or record.get("kind") != "interpretation"
            or uri != f"cas://sha256/{content_digest.removeprefix('sha256:')}"
        ):
            return None
        blob = load_archive(store_root, uri)
        actual_archive_digest = f"sha256:{hashlib.sha256(blob.read_bytes()).hexdigest()}"
        if actual_archive_digest != archive_digest:
            return None
        if sidecar_dir is not None:
            with tempfile.TemporaryDirectory() as temporary:
                restored = restore_evidence(store_root, uri, Path(temporary))
                for filename in _SIDECAR_FILES:
                    restored_path = restored / filename
                    sidecar_path = sidecar_dir / filename
                    if not restored_path.is_file() or _sha256_file(restored_path) != _sha256_file(
                        sidecar_path
                    ):
                        return None
    except Exception:
        return None
    return uri, content_digest


def _projection_sidecars_valid(
    *,
    ir: Any,
    pack: Any,
    judgment: Any,
    decision: Any,
    trial_id: str,
    decision_dirname: str,
) -> bool:
    if not all(isinstance(payload, dict) for payload in (ir, pack, judgment, decision)):
        return False
    ir_body = {key: value for key, value in ir.items() if key != "ir_digest"}
    pack_body = {key: value for key, value in pack.items() if key != "pack_digest"}
    if ir.get("ir_digest") != _data_contract_digest(ir_body):
        return False
    if pack.get("pack_digest") != _data_contract_digest(pack_body):
        return False
    if ir.get("trial_id") != trial_id or pack.get("trial_id") != trial_id:
        return False
    if ir.get("job_id") != pack.get("job_id"):
        return False
    source_digests = ir.get("source_digests")
    if not isinstance(source_digests, dict):
        return False
    expected_sources = dict(source_digests)
    expected_sources["ir_digest"] = ir.get("ir_digest")
    expected_sources["redaction_profile_digest"] = pack.get("redaction_profile_digest")
    if pack.get("source_digests") != expected_sources:
        return False
    if _pack_payload_structure_errors(ir, pack):
        return False
    try:
        validated_judgment = MachineJudgment.model_validate(judgment)
        validated_decision = AcceptanceDecision.model_validate(decision)
    except (TypeError, ValueError, ValidationError):
        return False
    return not (
        validated_judgment.pack_id != pack.get("pack_digest")
        or validated_judgment.pack_digest != pack.get("pack_digest")
        or validated_decision.pack_digest != pack.get("pack_digest")
        or validated_decision.judgment_ids != [validated_judgment.judgment_id]
        or validated_decision.decision_id.removeprefix("sha256:") != decision_dirname
    )


def rebuild_interpretation_projections(
    sidecar_root: Path,
    derived_root: Path,
    *,
    store_root: Path,
) -> list[Path]:
    """Rebuild deterministic Parquet projections from CAS-backed JSON sidecars."""
    sidecar_root = sidecar_root.resolve()
    derived_root = derived_root.resolve()
    store_root = store_root.resolve()

    artifact_rows: list[dict[str, Any]] = []
    judgment_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

    for decision_path in sidecar_root.rglob("acceptance_decision.json"):
        artifact_dir = decision_path.parent
        trial_id = artifact_dir.parent.name
        rel_dir = artifact_dir.relative_to(sidecar_root)
        if not all((artifact_dir / filename).is_file() for filename in _SIDECAR_FILES):
            continue

        try:
            ir = json.loads((artifact_dir / "trajectory_ir.json").read_text(encoding="utf-8"))
            pack = json.loads((artifact_dir / "evidence_pack.json").read_text(encoding="utf-8"))
            judgment = json.loads(
                (artifact_dir / "machine_judgment.json").read_text(encoding="utf-8")
            )
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not _projection_sidecars_valid(
            ir=ir,
            pack=pack,
            judgment=judgment,
            decision=decision,
            trial_id=trial_id,
            decision_dirname=artifact_dir.name,
        ):
            continue

        pack_digest = pack["pack_digest"]
        judgment_id = judgment["judgment_id"]
        decision_id = decision["decision_id"]
        archive_record = _load_interpretation_archive_record(
            store_root,
            decision_id,
            sidecar_dir=artifact_dir,
        )
        if archive_record is None:
            continue
        artifact_cas_uri, archive_content_digest = archive_record

        ir_row = {
            "artifact_digest": ir.get("ir_digest", ""),
            "kind": "ir",
            "trial_id": ir.get("trial_id", trial_id),
            "job_id": ir.get("job_id", ""),
            "content_digest": _sha256_file(artifact_dir / "trajectory_ir.json"),
            "artifact_path": str(rel_dir / "trajectory_ir.json"),
            "cas_uri": artifact_cas_uri,
            "pack_digest": pack_digest,
            "judgment_id": judgment_id,
            "decision_id": decision_id,
            "ingested_at": _parse_timestamp_for_parquet(ir.get("created_at")),
        }
        pack_row = {
            "artifact_digest": pack.get("pack_digest", ""),
            "kind": "pack",
            "trial_id": pack.get("trial_id", trial_id),
            "job_id": pack.get("job_id", ""),
            "content_digest": _sha256_file(artifact_dir / "evidence_pack.json"),
            "artifact_path": str(rel_dir / "evidence_pack.json"),
            "cas_uri": artifact_cas_uri,
            "pack_digest": pack_digest,
            "judgment_id": judgment_id,
            "decision_id": decision_id,
            "ingested_at": _parse_timestamp_for_parquet(pack.get("created_at")),
        }
        judgment_row = {
            "artifact_digest": judgment.get("judgment_id", ""),
            "kind": "judgment",
            "trial_id": ir.get("trial_id", trial_id),
            "job_id": ir.get("job_id", ""),
            "content_digest": _sha256_file(artifact_dir / "machine_judgment.json"),
            "artifact_path": str(rel_dir / "machine_judgment.json"),
            "cas_uri": artifact_cas_uri,
            "pack_digest": pack_digest,
            "judgment_id": judgment_id,
            "decision_id": decision_id,
            "ingested_at": _parse_timestamp_for_parquet(judgment.get("produced_at")),
        }
        decision_row = {
            "artifact_digest": decision.get("decision_id", ""),
            "kind": "decision",
            "trial_id": ir.get("trial_id", trial_id),
            "job_id": ir.get("job_id", ""),
            "content_digest": _sha256_file(artifact_dir / "acceptance_decision.json"),
            "artifact_path": str(rel_dir / "acceptance_decision.json"),
            "cas_uri": artifact_cas_uri,
            "pack_digest": pack_digest,
            "judgment_id": judgment_id,
            "decision_id": decision_id,
            "ingested_at": _parse_timestamp_for_parquet(decision.get("produced_at")),
        }
        interpretation_row = {
            "artifact_digest": archive_content_digest,
            "kind": "interpretation",
            "trial_id": ir.get("trial_id", trial_id),
            "job_id": ir.get("job_id", ""),
            "content_digest": archive_content_digest,
            "artifact_path": str(rel_dir),
            "cas_uri": artifact_cas_uri,
            "pack_digest": pack_digest,
            "judgment_id": judgment_id,
            "decision_id": decision_id,
            "ingested_at": _parse_timestamp_for_parquet(decision.get("produced_at")),
        }
        artifact_rows.extend([ir_row, pack_row, judgment_row, decision_row, interpretation_row])

        judgment_rows.append(
            {
                "judgment_id": judgment_id,
                "judgment_digest": judgment.get("judgment_digest", ""),
                "pack_digest": pack_digest,
                "producer_kind": judgment.get("producer_kind", ""),
                "validity": judgment.get("validity", ""),
                "citation_ids_json": json.dumps(judgment.get("citation_ids", [])),
                "coverage_gaps_json": json.dumps(judgment.get("coverage_gaps", [])),
                "artifact_path": str(rel_dir / "machine_judgment.json"),
                "cas_uri": artifact_cas_uri,
                "produced_at": _parse_timestamp_for_parquet(judgment.get("produced_at")),
                "ingested_at": _parse_timestamp_for_parquet(judgment.get("produced_at")),
            }
        )

        decision_rows.append(
            {
                "decision_id": decision_id,
                "decision_digest": decision.get("decision_digest", ""),
                "decision": decision.get("decision", ""),
                "judgment_ids_json": json.dumps(decision.get("judgment_ids", [])),
                "pack_digest": pack_digest,
                "reason_codes_json": json.dumps(decision.get("reason_codes", [])),
                "calibration_version": decision.get("calibration_version"),
                "calibration_schema": decision.get("calibration_class_gate", {}).get(
                    "report_schema"
                ),
                "status": decision.get("decision", ""),
                "supersedes_decision_id": decision.get("supersedes_decision_id"),
                "artifact_path": str(rel_dir / "acceptance_decision.json"),
                "cas_uri": artifact_cas_uri,
                "produced_at": _parse_timestamp_for_parquet(decision.get("produced_at")),
                "ingested_at": _parse_timestamp_for_parquet(decision.get("produced_at")),
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[str, ...]:
        return (row.get("artifact_digest", ""), row.get("kind", ""))

    artifact_rows.sort(key=sort_key)
    judgment_rows.sort(key=lambda r: r["judgment_id"])
    decision_rows.sort(key=lambda r: r["decision_id"])

    artifact_path = derived_root / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    judgment_path = derived_root / "machine_judgments" / "machine_judgments.parquet"
    decision_path = derived_root / "acceptance_decisions" / "acceptance_decisions.parquet"

    _write_parquet(artifact_path, INTERPRETATION_ARTIFACT_SCHEMA, artifact_rows)
    _write_parquet(judgment_path, MACHINE_JUDGMENT_SCHEMA, judgment_rows)
    _write_parquet(decision_path, ACCEPTANCE_DECISION_SCHEMA, decision_rows)

    return [artifact_path, judgment_path, decision_path]
