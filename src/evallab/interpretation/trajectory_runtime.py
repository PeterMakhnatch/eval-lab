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
from evallab.evidence_store import (
    archive_evidence,
    read_archive,
    read_record,
    restore_evidence,
)
from evallab.interpretation.evidence_pack import (
    DEFAULT_TOKEN_BUDGET,
    EvidencePack,
    build_evidence_pack,
    reopen_omitted_range,
)
from evallab.interpretation.trajectory_acceptance import (
    DETERMINISTIC_GATE_ORDER,
    AcceptanceDecision,
    CalibrationClassGate,
    CrossJudgeRecord,
    GateResult,
    evaluate_acceptance,
)
from evallab.interpretation.trajectory_calibration import (
    CalibrationReport,
    UnsupportedCalibrationVersion,
    calibration_report_can_enable_acceptance,
    parse_calibration_report,
)
from evallab.interpretation.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    hydrate_citation,
)
from evallab.interpretation.trajectory_ir import (
    CASTrialResolutionError,
    IREvent,
    TrajectoryIR,
    build_trajectory_ir,
)
from evallab.interpretation.trajectory_judgment import (
    JudgmentConfidence,
    MachineJudgment,
    canonical_json_digest,
)
from evallab.analysis_capability import (
    AnalysisMethod,
    AnalysisStatus,
    AnalysisUnit,
    CampaignAnalysisConfigV1,
    CampaignAnalysisResultV1,
    CampaignAnalysisSpecV1,
    ContextCitation,
    NextRunAction,
    NextRunFeedbackV1,
    RefusalCode,
    RetrievalPolicyV1,
    ReviewQueueArtifactV1,
    ReviewQueueEntryV1,
    ReviewQueueRef,
    RunRecommendationV1,
    run_campaign_analysis,
)
from evallab.results import sha256_file
from evallab.schemas import ContractModel

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
    arm: str | None = None
    dose: float | int | str | None = None
    seed: int | str | None = None
    coverage_complete: bool | None = None
    order_exact: bool | None = None
    declared_features: dict[str, Any] = Field(default_factory=dict)
    capture_complete: bool = True
    capture_authority: str | None = None

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
    analysis_config: CampaignAnalysisConfigV1
    analysis_snapshot_digest: str
    produced_at: datetime

    def cohort_items(self) -> list[CampaignAnalysisItem]:
        return [item for item in self.items if item.cohort_included]

    def accounting_items(self) -> list[CampaignAnalysisItem]:
        return [item for item in self.items if not item.cohort_included]


def compute_analysis_snapshot_digest(
    manifest_or_data: Mapping[str, Any] | CampaignAnalysisManifest,
    config: CampaignAnalysisConfigV1,
) -> str:
    """Deterministic identity digest for the analysis snapshot.

    Excludes all timestamps and mutable artifact paths. Binds the cohort
    identities, task/verifier/spec digests, CAS URIs, and configuration fields
    that define a frozen analysis-ready snapshot.
    """
    if isinstance(manifest_or_data, CampaignAnalysisManifest):
        data = manifest_or_data.model_dump(mode="json")
    else:
        data = dict(manifest_or_data)

    items = data.get("items", [])
    cohort = [
        i for i in items
        if (i.get("cohort_included") if isinstance(i, dict) else getattr(i, "cohort_included", False))
    ]

    def _item_body(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            d = item
        else:
            d = item.model_dump(mode="json")
        return {
            "job_id": d.get("job_id", ""),
            "trial_id": d.get("trial_id", ""),
            "attempt_role": d.get("attempt_role", ""),
            "cohort_included": d.get("cohort_included", True),
            "task_digest": d.get("task_digest"),
            "verifier_digest": d.get("verifier_digest"),
            "quality_report_digest": d.get("quality_report_digest"),
            "cas_uri": d.get("cas_uri"),
            "arm": d.get("arm"),
            "dose": d.get("dose"),
            "seed": d.get("seed"),
            "reward": d.get("reward"),
            "coverage_complete": d.get("coverage_complete"),
            "order_exact": d.get("order_exact"),
            "declared_features": d.get("declared_features") or {},
        }

    cohort_body = sorted((_item_body(i) for i in cohort), key=lambda d: (d["job_id"], d["trial_id"]))
    config_dict = config.model_dump(mode="json")
    producer_digests = dict(config_dict.get("producer_digests") or {})
    retrieval = config.retrieval
    retrieval_digest = None
    if retrieval is not None:
        retrieval_digest = canonical_json_digest(retrieval.model_dump(mode="json"))

    snapshot_body = {
        "schema_version": "analysis-snapshot/v1",
        "campaign_id": data.get("campaign_id", ""),
        "source_campaign_manifest_digest": data.get("source_campaign_manifest_digest", ""),
        "source_commit": data.get("source_commit"),
        "cohort_items": cohort_body,
        "feature_registry_digest": config_dict.get("feature_registry_digest"),
        "producer_digests": dict(sorted(producer_digests.items())),
        "cohort_policy_digest": config_dict.get("cohort_policy_digest"),
        "redaction_policy_digest": config_dict.get("redaction_policy_digest"),
        "spec_digests": sorted(s.get("spec_digest") for s in config_dict.get("specs", [])),
        "retrieval_digest": retrieval_digest,
    }
    return canonical_json_digest(snapshot_body)


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
        arm=raw.get("arm"),
        dose=raw.get("dose"),
        seed=raw.get("seed"),
        coverage_complete=raw.get("coverage_complete"),
        order_exact=raw.get("order_exact"),
        declared_features=dict(raw.get("declared_features") or {}),
        capture_complete=bool(raw.get("capture_complete", True)),
        capture_authority=raw.get("capture_authority"),
    )


def load_campaign_analysis_manifest(path: Path) -> CampaignAnalysisManifest:
    """Load the machine-analysis inventory as a typed Platform manifest.

    Recognizes both typed ``campaign-analysis-manifest/v1`` artifacts with an
    ``items`` list and legacy machine-analysis inventories (e.g. TB3 five-trial).
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    # Case 1: Already typed campaign-analysis-manifest/v1
    if data.get("schema_version") == "campaign-analysis-manifest/v1" and "items" in data:
        manifest = CampaignAnalysisManifest.model_validate(data)
        body = {
            "schema_version": manifest.schema_version,
            "campaign_id": manifest.campaign_id,
            "source_campaign_manifest_digest": manifest.source_campaign_manifest_digest,
            "source_commit": manifest.source_commit,
            "authorizing_actor": manifest.authorizing_actor,
            "cas_store_root": manifest.cas_store_root,
            "items": [item.model_dump(mode="json") for item in manifest.items],
            "accounting": manifest.accounting,
            "analysis_config": manifest.analysis_config.model_dump(mode="json"),
            "analysis_snapshot_digest": manifest.analysis_snapshot_digest,
        }
        computed_id = canonical_json_digest(body)
        if manifest.manifest_id != computed_id:
            raise ValueError(f"manifest_id {manifest.manifest_id} does not match canonical {computed_id}")
        computed_digest = canonical_json_digest({**body, "manifest_id": computed_id})
        if manifest.manifest_digest != computed_digest:
            raise ValueError(f"manifest_digest {manifest.manifest_digest} does not match canonical {computed_digest}")
        return manifest

    # Case 2: Legacy inventory adapter
    accounting = dict(data.get("accounting") or {})
    items: list[CampaignAnalysisItem] = []
    for raw in data.get("analysis_cohort_5_trials", []):
        role = raw.get("role", "")
        attempt_role: CampaignAttemptRole = "retry" if role == "infrastructure_retry_1" else "primary"
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

    raw_config = data.get("analysis_config") or {}
    if not isinstance(raw_config, dict):
        raw_config = {}

    from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY
    feature_registry_digest = canonical_json_digest(
        sorted([f.column_name for f in TRAJECTORY_FEATURE_REGISTRY.all_features().values()])
    )
    cohort_policy_digest = canonical_json_digest({"policy": "tb3_analysis_ready_cohort_v1"})
    redaction_policy_digest = RedactionPolicy().compute_digest()

    producer_digests = {
        "ir_builder": _sha256_file(Path(build_trajectory_ir.__code__.co_filename)),
        "pack_builder": _sha256_file(Path(build_evidence_pack.__code__.co_filename)),
        "acceptance_policy": canonical_json_digest(
            {
                "auto_acceptance_enabled": False,
                "gate_order": list(DETERMINISTIC_GATE_ORDER),
            }
        ),
    }

    if raw_config.get("feature_registry_digest"):
        feature_registry_digest = raw_config["feature_registry_digest"]
    if raw_config.get("producer_digests"):
        producer_digests.update(raw_config["producer_digests"])
    if raw_config.get("cohort_policy_digest"):
        cohort_policy_digest = raw_config["cohort_policy_digest"]
    if raw_config.get("redaction_policy_digest"):
        redaction_policy_digest = raw_config["redaction_policy_digest"]

    parsed_specs: list[CampaignAnalysisSpecV1] = []
    if "specs" in raw_config and isinstance(raw_config["specs"], (list, tuple)):
        for s in raw_config["specs"]:
            if isinstance(s, CampaignAnalysisSpecV1):
                parsed_specs.append(s)
            elif isinstance(s, dict):
                parsed_specs.append(CampaignAnalysisSpecV1.model_validate(s))

    parsed_retrieval = None
    if "retrieval" in raw_config and raw_config["retrieval"]:
        if isinstance(raw_config["retrieval"], RetrievalPolicyV1):
            parsed_retrieval = raw_config["retrieval"]
        elif isinstance(raw_config["retrieval"], dict):
            parsed_retrieval = RetrievalPolicyV1.model_validate(raw_config["retrieval"])

    analysis_config = CampaignAnalysisConfigV1(
        feature_registry_digest=feature_registry_digest,
        producer_digests=producer_digests,
        cohort_policy_digest=cohort_policy_digest,
        redaction_policy_digest=redaction_policy_digest,
        specs=tuple(parsed_specs),
        retrieval=parsed_retrieval,
    )

    body_pre_id = {
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
    analysis_snapshot_digest = compute_analysis_snapshot_digest(body_pre_id, analysis_config)
    body_pre_id["analysis_snapshot_digest"] = analysis_snapshot_digest
    produced_at = datetime.now(UTC)
    manifest_id = canonical_json_digest(body_pre_id)
    manifest_digest = canonical_json_digest({**body_pre_id, "manifest_id": manifest_id})
    return CampaignAnalysisManifest(
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        produced_at=produced_at,
        **body_pre_id,
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

    selected_bounds: list[tuple[int, int]] = []
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
        if (
            isinstance(step_start, int)
            and not isinstance(step_start, bool)
            and isinstance(step_end, int)
            and not isinstance(step_end, bool)
            and step_start <= step_end
        ):
            selected_bounds.append((step_start, step_end))

    omitted_bounds: list[tuple[int, int]] = []
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
        omitted_start = omitted.get("step_start")
        omitted_end = omitted.get("step_end")
        if (
            isinstance(omitted_start, int)
            and not isinstance(omitted_start, bool)
            and isinstance(omitted_end, int)
            and not isinstance(omitted_end, bool)
            and omitted_start <= omitted_end
        ):
            omitted_bounds.append((omitted_start, omitted_end))

    if len(selected_ids) != len(set(selected_ids)):
        errors.append("duplicate_selected_event")
    if len(omitted_ids) != len(set(omitted_ids)):
        errors.append("duplicate_omitted_event")
    if set(selected_ids) & set(omitted_ids):
        errors.append("selected_omitted_overlap")
    if set(selected_ids) | set(omitted_ids) != set(event_by_id):
        errors.append("pack_event_coverage_mismatch")
    if any(
        selected_start <= omitted_end and omitted_start <= selected_end
        for selected_start, selected_end in selected_bounds
        for omitted_start, omitted_end in omitted_bounds
    ):
        errors.append("selected_omitted_range_overlap")
    return sorted(set(errors))


def _pack_structure_errors(ir: TrajectoryIR, pack: EvidencePack) -> list[str]:
    """Validate lossless, non-overlapping IR event accounting across the bounded pack."""
    return _pack_payload_structure_errors(ir.to_dict(), pack.to_dict())


def _selected_hydration_errors(
    ir: TrajectoryIR,
    pack: EvidencePack,
    *,
    cas_store: Path,
) -> list[str]:
    """Reopen every CAS-backed selected event and compare the exact redacted bytes."""
    errors: list[str] = []
    try:
        policy = RedactionPolicy.from_pack_config(
            pack.redaction_policy_config,
            pack.redaction_profile_digest,
        )
    except ValueError:
        return ["redaction_policy_config_invalid"]
    event_by_id = {event.event_id: event for event in ir.events}
    for window in pack.selected_windows:
        for payload in window.events:
            event_id = payload.get("event_id")
            event = event_by_id.get(str(event_id))
            if event is None:
                continue
            handle = event.source_citation
            if not (handle.raw_cas_uri or handle.cas_uri):
                continue
            hydrated = hydrate_citation(handle, repo_root=cas_store, policy=policy)
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            if limitation == "cas_load_error":
                errors.append("selected_hydration_cas_integrity_error")
                continue
            if limitation or hydrated.redaction_metadata.get("content_digest_mismatch"):
                errors.append("selected_event_hydration_unreopenable")
                continue
            if payload.get("hydrated_content") != hydrated.redacted_content:
                errors.append("selected_event_hydration_mismatch")
    return sorted(set(errors))


def _validate_artifact_digests(
    ir: TrajectoryIR,
    pack: EvidencePack,
    judgment: MachineJudgment,
    *,
    cas_store: Path,
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
    hydration_errors = _selected_hydration_errors(ir, pack, cas_store=cas_store)
    if hydration_errors:
        raise ValueError(f"invalid selected hydration: {', '.join(hydration_errors)}")
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
        integrity_errors: list[str] = []
        for cid, _, handle in resolved:
            hydrated = hydrate_citation(handle, repo_root=cas_store)
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            if limitation == "cas_load_error":
                integrity_errors.append(cid)
            elif not handle.content_sha256 or limitation:
                missing_digest.append(cid)
            elif (
                hydrated.redaction_metadata.get("content_digest_mismatch")
                or hydrated.content_sha256 != handle.content_sha256
            ):
                mismatched.append(cid)
        if integrity_errors:
            c2 = GateResult(
                gate_id="C2_digest",
                status="fail",
                reason_code="cas_integrity_error",
                citation_ids=sorted(set(integrity_errors)),
            )
        elif mismatched:
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
        source_integrity: list[str] = []
        for cid, event, handle in resolved:
            hydrated = hydrate_citation(handle, repo_root=cas_store)
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            if limitation == "cas_load_error":
                source_integrity.append(cid)
            elif (
                not handle.source_path
                or not handle.source_sha256
                or not (handle.raw_cas_uri or handle.cas_uri)
                or limitation
            ):
                source_missing.append(cid)
            elif (
                hydrated.redaction_metadata.get("source_digest_mismatch")
                or handle.source_sha256 != event.source_citation.source_sha256
            ):
                source_mismatch.append(cid)
        if source_integrity:
            c3 = GateResult(
                gate_id="C3_source",
                status="fail",
                reason_code="cas_integrity_error",
                citation_ids=sorted(set(source_integrity)),
            )
        elif source_mismatch:
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

    # C4_in_pack
    if not judgment.citation_ids:
        c4 = GateResult(
            gate_id="C4_in_pack",
            status="unknown" if supported_claim else "pass",
            reason_code="source_missing" if supported_claim else None,
            citation_ids=[],
        )
    elif unresolved:
        c4 = GateResult(
            gate_id="C4_in_pack",
            status="unknown",
            reason_code="source_missing",
            citation_ids=sorted(set(unresolved)),
        )
    else:
        out_of_pack: list[str] = []
        for cid, event, _ in resolved:
            in_sel, _ = _step_in_windows(event.step_index, pack)
            if not in_sel:
                out_of_pack.append(cid)
        if out_of_pack:
            c4 = GateResult(
                gate_id="C4_in_pack",
                status="fail",
                reason_code="citation_out_of_pack",
                citation_ids=sorted(set(out_of_pack)),
            )
        else:
            c4 = GateResult(
                gate_id="C4_in_pack",
                status="pass",
                reason_code=None,
                citation_ids=all_cited,
            )

    # C5_entail
    c5 = GateResult(
        gate_id="C5_entail",
        status="fail" if supported_claim else "pass",
        reason_code="entailment_disabled" if supported_claim else None,
        citation_ids=all_cited,
    )

    # C6_contradict
    c6 = GateResult(
        gate_id="C6_contradict",
        status="pass",
        reason_code=None,
        citation_ids=all_cited,
    )

    # C7_earliest
    c7 = GateResult(
        gate_id="C7_earliest",
        status="pass",
        reason_code=None,
        citation_ids=all_cited,
    )

    # C8_alt_expl
    c8 = GateResult(
        gate_id="C8_alt_expl",
        status="pass",
        reason_code=None,
        citation_ids=all_cited,
    )

    # C9_profile_fit
    c9 = GateResult(
        gate_id="C9_profile_fit",
        status="unknown",
        reason_code="profile_missing",
        citation_ids=all_cited,
    )

    # C10_schema
    try:
        _validate_artifact_digests(ir, pack, judgment, cas_store=cas_store)
        c10 = GateResult(
            gate_id="C10_schema",
            status="pass",
            reason_code=None,
            citation_ids=all_cited,
        )
    except Exception:
        c10 = GateResult(
            gate_id="C10_schema",
            status="fail",
            reason_code="schema_invalid",
            citation_ids=all_cited,
        )

    # C11_pack_complete
    if not pack.is_model_callable:
        reason = pack.overflow_reason or "pack_incomplete"
        c11 = GateResult(
            gate_id="C11_pack_complete",
            status="unknown",
            reason_code=reason,
            citation_ids=all_cited,
        )
    else:
        c11 = GateResult(
            gate_id="C11_pack_complete",
            status="pass",
            reason_code=None,
            citation_ids=all_cited,
        )

    # C12_coverage_complete
    c12 = GateResult(
        gate_id="C12_coverage_complete",
        status="unknown",
        reason_code="judge_execution_disabled",
        citation_ids=all_cited,
    )

    # C13_auto_accept_en
    c13 = GateResult(
        gate_id="C13_auto_accept_en",
        status="unknown",
        reason_code="acceptance_disabled",
        citation_ids=all_cited,
    )

    # C14_not_hold_gold
    c14 = GateResult(
        gate_id="C14_not_hold_gold",
        status="pass",
        reason_code=None,
        citation_ids=all_cited,
    )

    return [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14]


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


def _validated_campaign_result_identities(
    manifest: CampaignAnalysisManifest,
    results: Sequence[dict[str, Any]],
) -> list[tuple[str, str]]:
    expected = [(item.job_id, item.trial_id) for item in manifest.cohort_items()]
    if len(expected) != len(set(expected)):
        raise ValueError("campaign manifest contains duplicate cohort identities")
    actual: list[tuple[str, str]] = []
    for result in results:
        job_id = result.get("job_id")
        trial_id = result.get("trial_id")
        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(trial_id, str)
            or not trial_id
        ):
            raise ValueError("campaign result is missing job_id/trial_id identity")
        actual.append((job_id, trial_id))
    if len(actual) != len(set(actual)):
        raise ValueError("campaign results contain duplicate identities")
    expected_set = set(expected)
    actual_set = set(actual)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        foreign = sorted(actual_set - expected_set)
        raise ValueError(f"campaign result identity mismatch: missing={missing}, foreign={foreign}")
    return actual


def _spec_for_result(manifest: CampaignAnalysisManifest, spec_id: str) -> CampaignAnalysisSpecV1 | None:
    for spec in manifest.analysis_config.specs:
        if spec.spec_id == spec_id:
            return spec
    return None


def _build_next_run_feedback(
    manifest: CampaignAnalysisManifest,
    analysis_results: Sequence[CampaignAnalysisResultV1],
    source_report_digest: str,
) -> NextRunFeedbackV1:
    recommendations: list[RunRecommendationV1] = []
    all_digests = tuple(r.result_digest for r in analysis_results)

    lineage_digests = tuple(
        r.result_digest
        for r in analysis_results
        if any(
            code in (RefusalCode.MISSING_LINEAGE_DECLARATION, RefusalCode.OUTCOME_LINEAGE_VIOLATION)
            for code in r.refusals
        )
    )
    if lineage_digests:
        recommendations.append(
            RunRecommendationV1(
                action=NextRunAction.BACKFILL_FEATURE_LINEAGE,
                basis_result_digests=lineage_digests,
                target_estimand="declared_outcome_features",
                target_unit=AnalysisUnit.TRIAL,
                blocking=True,
                reason_codes=("MISSING_LINEAGE_DECLARATION", "OUTCOME_LINEAGE_VIOLATION"),
            )
        )

    underpowered = [
        r for r in analysis_results
        if r.status == AnalysisStatus.REFUSAL and RefusalCode.UNDERPOWERED in r.refusals
    ]
    for result in underpowered:
        spec = _spec_for_result(manifest, result.spec_id)
        target_estimand = spec.outcome_feature if spec else "outcome"
        target_unit = spec.unit if spec else AnalysisUnit.PAIRED_SEED
        requested = spec.minimum_informative_units if spec and spec.minimum_informative_units is not None else result.informative_units
        recommendations.append(
            RunRecommendationV1(
                action=NextRunAction.ADD_INDEPENDENT_SEEDS,
                basis_result_digests=(result.result_digest,),
                target_estimand=target_estimand,
                target_unit=target_unit,
                requested_units=requested,
                blocking=False,
                reason_codes=("UNDERPOWERED", "attainable_p_floor_above_alpha"),
            )
        )

    recommendations.append(
        RunRecommendationV1(
            action=NextRunAction.HOLD_SEMANTIC_DECISION_ANALYSIS,
            basis_result_digests=all_digests,
            target_estimand="all_outcomes",
            target_unit=AnalysisUnit.TRIAL,
            blocking=True,
            reason_codes=("execution_not_authorized",),
        )
    )

    feedback_body = {
        "schema_version": "next-run-feedback/v1",
        "source_report_digest": source_report_digest,
        "source_snapshot_digest": manifest.analysis_snapshot_digest,
        "recommendations": tuple(r.model_dump(mode="json") for r in recommendations),
        "execution_authorized": False,
        "authorizing_actor_required": True,
    }
    feedback_digest = canonical_json_digest(feedback_body)
    return NextRunFeedbackV1.model_validate(
        {
            **feedback_body,
            "feedback_digest": feedback_digest,
            "recommendations": recommendations,
        }
    )


def build_campaign_report(
    manifest: CampaignAnalysisManifest,
    results: Sequence[dict[str, Any]],
    *,
    analysis_results: Sequence[CampaignAnalysisResultV1] = (),
    review_queue_ref: ReviewQueueRef | None = None,
) -> dict[str, Any]:
    """Build the campaign report from manifest accounting and per-item results."""
    _validated_campaign_result_identities(manifest, results)
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
                "job_id": res["job_id"],
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
        "schema_version": "campaign-report/v2",
        "manifest_id": manifest.manifest_id,
        "manifest_digest": manifest.manifest_digest,
        "analysis_snapshot_digest": manifest.analysis_snapshot_digest,
        "campaign_id": manifest.campaign_id,
        "cohort_accounted": len(results),
        "accepted": accepted,
        "rejected": rejected,
        "abstained": abstained,
        "role_counts": role_counts,
        "reason_counts": reason_counts,
        "coverage_gap_counts": coverage_gap_counts,
        "source_refs": source_refs,
        "analysis_results": [r.model_dump(mode="json") for r in analysis_results],
        "review_queue_ref": review_queue_ref.model_dump(mode="json") if review_queue_ref is not None else None,
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
    pre_feedback_report_id = canonical_json_digest(body)
    next_run_feedback = _build_next_run_feedback(manifest, analysis_results, pre_feedback_report_id)
    body["next_run_feedback"] = next_run_feedback.model_dump(mode="json")
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


def _extract_analysis_rows(
    manifest: CampaignAnalysisManifest,
    results: Sequence[dict[str, Any]],
    output_dir: Path,
    store_root: Path,
) -> list[dict[str, Any]]:
    """Return analysis-ready rows from deterministic manifest items and interpretation results."""
    rows: list[dict[str, Any]] = []
    for item, res in zip(manifest.cohort_items(), results, strict=False):
        row: dict[str, Any] = dict(res)
        row["job_id"] = item.job_id
        row["trial_id"] = item.trial_id
        row["spec_id"] = item.spec_id
        row["task_name"] = item.task_name
        row["task_digest"] = item.task_digest
        row["verifier_digest"] = item.verifier_digest
        row["reward"] = item.reward if item.reward is not None else res.get("reward")
        row["decision"] = res.get("decision")
        row["task_success"] = (row["reward"] is not None and row["reward"] > 0) or (res.get("decision") == "accepted")
        row["arm"] = item.arm or res.get("arm")
        row["dose"] = item.dose or res.get("dose")
        row["seed"] = item.seed or res.get("seed")
        row["coverage_complete"] = item.coverage_complete if item.coverage_complete is not None else (res.get("coverage_gaps") == ["judge_execution_disabled"])
        row["order_exact"] = item.order_exact if item.order_exact is not None else res.get("order_exact")
        row["capture_complete"] = item.capture_complete and (item.quality_status in ("pass", "warn")) and ("ATIF_UNPAIRED_TOOL_CALL" not in res.get("coverage_gaps", []))
        row["capture_authority"] = item.capture_authority or "concordant"
        if item.declared_features:
            for feat_k, feat_v in item.declared_features.items():
                row.setdefault(feat_k, feat_v)
        rows.append(row)
    return rows


def _build_review_queue_artifact(
    manifest: CampaignAnalysisManifest,
    retrieval: RetrievalPolicyV1,
    store_root: Path,
) -> ReviewQueueRef:
    """Build a non-decision review queue artifact using deterministic lexical HashingEmbedder."""
    from evallab.lance import HashingEmbedder

    cohort = manifest.cohort_items()
    analysis_ready = [i for i in cohort if i.quality_status in ("pass", "warn") and i.cas_uri]
    if len(analysis_ready) < len(cohort) or not analysis_ready:
        query_digest = canonical_json_digest({"purpose": retrieval.purpose, "manifest_id": manifest.manifest_id})
        candidate_pool_digest = canonical_json_digest([{"trial_id": i.trial_id} for i in analysis_ready])
        index_digest = canonical_json_digest({"embedder": "hashing_embedder", "dim": retrieval.dimension})
        body = {
            "schema_version": "review-queue/v1",
            "queue_id": f"{manifest.campaign_id}-review-queue",
            "manifest_digest": manifest.manifest_digest,
            "snapshot_digest": manifest.analysis_snapshot_digest,
            "policy": retrieval.model_dump(mode="json"),
            "query_digest": query_digest,
            "candidate_pool_digest": candidate_pool_digest,
            "index_digest": index_digest,
            "coverage_complete": False,
            "entries": [],
            "refusals": [RefusalCode.REVIEW_QUEUE_INELIGIBLE],
            "decision_eligible": False,
        }
        queue_digest = canonical_json_digest(body)
        artifact = ReviewQueueArtifactV1.model_validate({**body, "queue_digest": queue_digest})
        queue_dir = store_root.parent / "review_queues" / str(queue_digest).removeprefix("sha256:")
        queue_path = queue_dir / "review_queue.json"
        _write_artifact_sidecar(queue_path, artifact.model_dump(mode="json"))
        archive = archive_evidence(
            source=queue_dir,
            store_root=store_root,
            record_id=str(queue_digest),
            kind="review_queue",
        )
        return ReviewQueueRef(
            queue_id=artifact.queue_id,
            queue_digest=queue_digest,
            queue_cas_uri=archive.uri,
            decision_eligible=False,
        )

    embedder = HashingEmbedder(dim=retrieval.dimension)
    query_text = f"review queue {retrieval.purpose} for {manifest.campaign_id}"
    query_vec = embedder.embed([query_text])[0]

    item_texts = [f"{i.task_name} {i.trial_name} {' '.join(i.quality_findings)}" for i in analysis_ready]
    item_vecs = embedder.embed(item_texts)

    scored: list[tuple[float, CampaignAnalysisItem]] = []
    for item, vec in zip(analysis_ready, item_vecs, strict=False):
        dist = 1.0 - sum(q * v for q, v in zip(query_vec, vec, strict=False))
        scored.append((dist, item))

    scored.sort(key=lambda pair: (pair[0], pair[1].trial_id))
    top_entries: list[ReviewQueueEntryV1] = []
    for rank, (dist, item) in enumerate(scored[: retrieval.k], start=1):
        window_digest = item.verifier_digest or item.task_digest or ("sha256:" + "0" * 64)
        citation = ContextCitation(path=f"{item.trial_id}/evidence_pack.json", digest=item.task_digest)
        top_entries.append(
            ReviewQueueEntryV1(
                rank=rank,
                job_id=item.job_id,
                trial_id=item.trial_id,
                source_cas_uri=item.cas_uri or "",
                citation=citation,
                window_start_step=0,
                window_end_step=item.atif_steps_count or 1,
                window_digest=window_digest,
                distance=round(float(dist), 6),
                reason=retrieval.purpose,
            )
        )

    candidate_pool = [
        {
            "job_id": item.job_id,
            "trial_id": item.trial_id,
            "cas_uri": item.cas_uri,
            "task_digest": item.task_digest,
            "verifier_digest": item.verifier_digest,
        }
        for item in sorted(analysis_ready, key=lambda i: (i.job_id, i.trial_id))
    ]
    candidate_pool_digest = canonical_json_digest(candidate_pool)
    query_digest = canonical_json_digest({"query": query_text, "purpose": retrieval.purpose})
    index_digest = canonical_json_digest({"embedder": embedder.digest, "dim": retrieval.dimension})

    body = {
        "schema_version": "review-queue/v1",
        "queue_id": f"{manifest.campaign_id}-review-queue",
        "manifest_digest": manifest.manifest_digest,
        "snapshot_digest": manifest.analysis_snapshot_digest,
        "policy": retrieval.model_dump(mode="json"),
        "query_digest": query_digest,
        "candidate_pool_digest": candidate_pool_digest,
        "index_digest": index_digest,
        "coverage_complete": True,
        "entries": [e.model_dump(mode="json") for e in top_entries],
        "refusals": [],
        "decision_eligible": False,
    }
    queue_digest = canonical_json_digest(body)
    artifact = ReviewQueueArtifactV1.model_validate({**body, "queue_digest": queue_digest})

    queue_dir = store_root.parent / "review_queues" / str(queue_digest).removeprefix("sha256:")
    queue_path = queue_dir / "review_queue.json"
    _write_artifact_sidecar(queue_path, artifact.model_dump(mode="json"))
    archive = archive_evidence(
        source=queue_dir,
        store_root=store_root,
        record_id=str(queue_digest),
        kind="review_queue",
    )
    return ReviewQueueRef(
        queue_id=artifact.queue_id,
        queue_digest=queue_digest,
        queue_cas_uri=archive.uri,
        decision_eligible=False,
    )


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
    recomputed_snapshot_digest = compute_analysis_snapshot_digest(manifest, manifest.analysis_config)
    if recomputed_snapshot_digest != manifest.analysis_snapshot_digest:
        raise RuntimeError(
            f"STALE_SNAPSHOT: manifest snapshot digest {manifest.analysis_snapshot_digest} "
            f"does not match computed {recomputed_snapshot_digest}"
        )

    cohort = manifest.cohort_items()
    cohort_identities = [(item.job_id, item.trial_id) for item in cohort]
    if len(cohort_identities) != len(set(cohort_identities)):
        raise RuntimeError("schema_mismatch: duplicate cohort job_id/trial_id identity")

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

    analysis_results: list[CampaignAnalysisResultV1] = []
    rows = _extract_analysis_rows(manifest, results, output_dir, store_root)
    for spec in manifest.analysis_config.specs:
        analysis_results.append(
            run_campaign_analysis(
                spec,
                rows,
                snapshot_digest=manifest.analysis_snapshot_digest,
            )
        )

    review_queue_ref = None
    if manifest.analysis_config.retrieval is not None and manifest.analysis_config.retrieval.enabled:
        review_queue_ref = _build_review_queue_artifact(
            manifest,
            manifest.analysis_config.retrieval,
            store_root,
        )

    report = build_campaign_report(
        manifest,
        results,
        analysis_results=analysis_results,
        review_queue_ref=review_queue_ref,
    )
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
    try:
        record = json.loads(
            read_record(
                store_root,
                kind="interpretation",
                record_id=decision_id,
            )
        )
    except FileNotFoundError:
        return None
    try:
        uri = str(record["uri"])
        content_digest = str(record["content_digest"])
        archive_digest = str(record["archive_digest"])
        if (
            record.get("record_id") != decision_id
            or record.get("kind") != "interpretation"
            or uri != f"cas://sha256/{content_digest.removeprefix('sha256:')}"
        ):
            return None
        archive_bytes = read_archive(store_root, uri)
        actual_archive_digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
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


__all__ = [
    "ArtifactRecord",
    "CampaignAnalysisItem",
    "CampaignAnalysisManifest",
    "CampaignAttemptRole",
    "analyze_batch",
    "analyze_calibrate",
    "analyze_inspect",
    "analyze_trial",
    "build_acceptance_decision",
    "build_calibration_class_gate",
    "build_campaign_report",
    "build_machine_judgment",
    "compute_analysis_snapshot_digest",
    "evaluate_deterministic_gates",
    "load_campaign_analysis_manifest",
    "rebuild_interpretation_projections",
    "write_campaign_report",
    "write_interpretation_artifacts",
]
