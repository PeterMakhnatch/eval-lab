"""Single-command all-durable completed-trial backfill orchestrator.

Binds every discovered durable trial to a reason-coded ANALYSIS_READY or HOLD
disposition. Identity is fail-closed: missing, ambiguous, duplicate, or foreign
CAS job bindings become HOLD with ``quarantine_job_identity_unresolved``.
Quarantined trials never enter interpretation. No judge, model, provider, or
auto-accept path is reachable from this module.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from evallab.database import catalog_availability
from evallab.evidence_store import load_archive
from evallab.interpretation.trajectory_data_quality import (
    campaign_data_quality_report,
    load_cross_campaign_inventory,
)
from evallab.interpretation.trajectory_judgment import canonical_json_digest
from evallab.interpretation.trajectory_runtime import analyze_batch, load_campaign_analysis_manifest
from evallab.schemas import ContractModel, NetworkIsolationStatus
from evallab.storage.historical_git_snapshot import (
    HistoricalGitBlobV1,
    HistoricalRegenerationError,
    HistoricalSnapshotInvalid,
    HistoricalSourceCapture,
    HistoricalSourceSnapshotV1,
    capture_historical_source_snapshot,
    normalize_runs_root,
    reopen_historical_source_snapshot,
    resolve_git_repository,
)

SCHEMA_VERSION = "completed-trial-backfill-ledger/v1"
IDENTITY_UNRESOLVED = "quarantine_job_identity_unresolved"
STORE_JOIN_UNAVAILABLE = "store_join_unavailable"
ORPHAN_PARQUET_PARTITION = "orphan_parquet_partition"
MISSING_QUARANTINE_REASON = "quarantine_hold_reason_unspecified"
_QUARANTINE_STATUSES = frozenset({"quarantine", "fail", "quarantined"})
_KNOWN_EXCEPTION_REASONS = frozenset(
    {
        "missing_cas",
        "cas_integrity_error",
        "schema_mismatch",
        "quarantined_input",
        IDENTITY_UNRESOLVED,
        STORE_JOIN_UNAVAILABLE,
        ORPHAN_PARQUET_PARTITION,
    }
)


class BackfillDisposition(ContractModel):
    """One durable trial's data-layer disposition."""

    trial_id: str
    task_name: str
    campaign: str | None
    job_id: str | None
    cas_uri: str | None
    ir_digest: str | None = None
    pack_digest: str | None = None
    judgment_id: str | None = None
    decision_id: str | None = None
    readiness: Literal["ANALYSIS_READY", "HOLD"]
    hold_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ready_iff_empty_hold_reasons(self) -> BackfillDisposition:
        reasons = sorted(set(self.hold_reasons))
        readiness = self.readiness
        if reasons:
            readiness = "HOLD"
        elif readiness == "HOLD":
            reasons = [MISSING_QUARANTINE_REASON]
        if reasons == self.hold_reasons and readiness == self.readiness:
            return self
        return self.model_copy(update={"hold_reasons": reasons, "readiness": readiness})


class BackfillLedger(ContractModel):
    """Deterministic unified backfill ledger."""

    schema_version: str
    dispositions: list[BackfillDisposition]
    disposition_count: int
    ready_count: int
    hold_count: int
    discovered_count: int
    content_digest: str
    exit_code: int
    ledger_path: str | None = None


@dataclass
class _DurableTrial:
    trial_id: str
    task_name: str
    campaign: str | None
    job_id: str | None
    job_name: str | None
    cas_uri: str | None
    trial_name: str | None = None
    quality_status: str | None = None
    original_reasons: list[str] = field(default_factory=list)
    quarantined: bool = False
    manifest_path: Path | None = None
    identity_reasons: list[str] = field(default_factory=list)


def assemble_disposition_rows(
    rows: Sequence[BackfillDisposition],
) -> list[BackfillDisposition]:
    """Return canonically ordered disposition rows.

    Tests monkeypatch this hook to prove a short row list is a non-zero exit.
    """
    return sorted(rows, key=lambda row: (row.campaign or "", row.trial_id))


def run_all_durable_backfill(
    *,
    inventory_path: Path,
    manifest_dir: Path,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path,
    database_url: str | None = None,
) -> BackfillLedger:
    """Discover, bind, interpret, persist, and dispose every durable trial."""
    repo_root = repo_root.resolve()
    inventory_path = inventory_path.resolve()
    manifest_dir = manifest_dir.resolve()
    store_root = store_root.resolve()
    output_dir = output_dir.resolve()
    derived_root = derived_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    inventory, discovery_ok = _load_inventory(inventory_path)
    trials, manifests, discovery_ok = _discover_trials(
        inventory,
        manifest_dir=manifest_dir,
        repo_root=repo_root,
        discovery_ok=discovery_ok,
    )
    job_records = _load_job_records(store_root)
    trial_id_counts = Counter(trial.trial_id for trial in trials)
    for trial in trials:
        _bind_trial_identity(trial, job_records, trial_id_counts)

    source_refs = _interpret_bound_cohorts(
        trials,
        manifests,
        repo_root=repo_root,
        store_root=store_root,
        output_dir=output_dir,
        derived_root=derived_root,
        database_url=database_url,
    )
    parquet_ids, parquet_status = _parquet_trial_ids(derived_root)
    if parquet_ids is not None:
        extra = parquet_ids - {trial.trial_id for trial in trials}
        if extra:
            parquet_status = "orphan"
    duckdb_reason = _duckdb_cross_check(repo_root, derived_root)
    postgres_reason = _postgres_cross_check(database_url)

    rows = [
        _disposition_for_trial(
            trial,
            source_refs=source_refs,
            store_root=store_root,
            parquet_ids=parquet_ids,
            parquet_status=parquet_status,
            duckdb_reason=duckdb_reason,
            postgres_reason=postgres_reason,
        )
        for trial in trials
    ]
    rows = assemble_disposition_rows(rows)

    declared = _declared_population(inventory)
    discovered_count = len(trials)
    disposition_count = len(rows)
    ready_count = sum(1 for row in rows if row.readiness == "ANALYSIS_READY")
    hold_count = disposition_count - ready_count
    exit_code = _exit_code(
        discovery_ok=discovery_ok,
        discovered_count=discovered_count,
        disposition_count=disposition_count,
        declared=declared,
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "dispositions": [row.model_dump(mode="json") for row in rows],
        "disposition_count": disposition_count,
        "ready_count": ready_count,
        "hold_count": hold_count,
        "discovered_count": discovered_count,
        "exit_code": exit_code,
    }
    content_digest = canonical_json_digest(body)
    ledger = BackfillLedger(
        schema_version=SCHEMA_VERSION,
        dispositions=rows,
        disposition_count=disposition_count,
        ready_count=ready_count,
        hold_count=hold_count,
        discovered_count=discovered_count,
        content_digest=content_digest,
        exit_code=exit_code,
    )
    payload = ledger.model_dump(mode="json")
    payload.pop("ledger_path", None)
    ledger_path = output_dir / "ledger.json"
    ledger_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return ledger.model_copy(update={"ledger_path": str(ledger_path)})


def _load_inventory(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        payload = load_cross_campaign_inventory(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, False
    if not isinstance(payload, dict):
        return {}, False
    return payload, True


def _discover_trials(
    inventory: Mapping[str, Any],
    *,
    manifest_dir: Path,
    repo_root: Path,
    discovery_ok: bool,
) -> tuple[list[_DurableTrial], dict[str, tuple[Any, Path]], bool]:
    manifests: dict[str, tuple[Any, Path]] = {}
    trials_by_id: dict[str, _DurableTrial] = {}
    ok = discovery_ok
    for entry in inventory.get("batch_interpreted_campaigns") or []:
        if not isinstance(entry, dict):
            ok = False
            continue
        campaign_id = entry.get("campaign_id")
        manifest_rel = entry.get("manifest_path")
        if not isinstance(campaign_id, str) or not campaign_id:
            ok = False
            continue
        if not isinstance(manifest_rel, str) or not manifest_rel:
            ok = False
            continue
        path = _resolve_manifest(repo_root, manifest_dir, manifest_rel)
        if path is None:
            ok = False
            continue
        try:
            manifest = load_campaign_analysis_manifest(path)
        except (OSError, ValueError, json.JSONDecodeError):
            ok = False
            continue
        manifests[campaign_id] = (manifest, path)
        for item in manifest.cohort_items():
            _put_trial(
                trials_by_id,
                _DurableTrial(
                    trial_id=item.trial_id,
                    task_name=item.task_name,
                    campaign=campaign_id,
                    job_id=item.job_id or None,
                    job_name=item.job_name or None,
                    cas_uri=item.cas_uri,
                    trial_name=item.trial_name,
                    quality_status=item.quality_status,
                    quarantined=item.quality_status in _QUARANTINE_STATUSES,
                    original_reasons=(
                        [f"quarantined_input:{item.quality_status}"]
                        if item.quality_status in _QUARANTINE_STATUSES
                        else []
                    ),
                    manifest_path=path,
                ),
            )

    for raw in inventory.get("quarantined_and_hold_trials") or []:
        if not isinstance(raw, dict):
            ok = False
            continue
        trial_id = raw.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id:
            ok = False
            continue
        reason = raw.get("reason")
        original = [reason] if isinstance(reason, str) and reason else [MISSING_QUARANTINE_REASON]
        matches = _manifest_matches(manifests, trial_id)
        extra_identity: list[str] = []
        campaign_id = None
        job_name = _optional_str(raw.get("job_name"))
        job_id = _optional_str(raw.get("job_id"))
        cas_uri = _optional_str(raw.get("cas_uri"))
        trial_name = _optional_str(raw.get("trial_name"))
        task_name = str(raw.get("task_name") or "")
        manifest_path = None
        quality_status = "quarantine"
        if len(matches) > 1:
            extra_identity.append(IDENTITY_UNRESOLVED)
        elif len(matches) == 1:
            campaign_id, item, manifest_path = matches[0]
            if job_name and item.job_name and job_name != item.job_name:
                extra_identity.append(IDENTITY_UNRESOLVED)
            if trial_name and item.trial_name and trial_name != item.trial_name:
                extra_identity.append(IDENTITY_UNRESOLVED)
            if task_name and item.task_name and task_name != item.task_name:
                extra_identity.append(IDENTITY_UNRESOLVED)
            if cas_uri and item.cas_uri and cas_uri != item.cas_uri:
                extra_identity.append(IDENTITY_UNRESOLVED)
            if job_id and item.job_id and job_id != item.job_id:
                extra_identity.append(IDENTITY_UNRESOLVED)
            if IDENTITY_UNRESOLVED not in extra_identity:
                job_name = job_name or item.job_name or None
                job_id = job_id or item.job_id or None
                cas_uri = cas_uri or item.cas_uri
                trial_name = trial_name or item.trial_name
                task_name = task_name or item.task_name
                quality_status = item.quality_status or quality_status
        existing = trials_by_id.get(trial_id)
        if existing is None:
            trials_by_id[trial_id] = _DurableTrial(
                trial_id=trial_id,
                task_name=task_name,
                campaign=campaign_id,
                job_name=job_name,
                job_id=job_id,
                cas_uri=cas_uri,
                trial_name=trial_name,
                quality_status=quality_status,
                quarantined=True,
                original_reasons=original,
                manifest_path=manifest_path,
                identity_reasons=extra_identity,
            )
        else:
            existing.quarantined = True
            existing.original_reasons = list(dict.fromkeys([*existing.original_reasons, *original]))
            existing.identity_reasons.extend(extra_identity)

    trials = [trials_by_id[key] for key in sorted(trials_by_id)]
    return trials, manifests, ok


def _put_trial(trials_by_id: dict[str, _DurableTrial], trial: _DurableTrial) -> None:
    existing = trials_by_id.get(trial.trial_id)
    if existing is None:
        trials_by_id[trial.trial_id] = trial
        return
    existing.identity_reasons.append(IDENTITY_UNRESOLVED)


def _manifest_matches(
    manifests: Mapping[str, tuple[Any, Path]], trial_id: str
) -> list[tuple[str, Any, Path]]:
    matches: list[tuple[str, Any, Path]] = []
    for campaign_id, (manifest, path) in manifests.items():
        for item in manifest.items:
            if item.trial_id == trial_id:
                matches.append((campaign_id, item, path))
    return matches


def _resolve_manifest(repo_root: Path, manifest_dir: Path, relative: str) -> Path | None:
    candidates = [
        (repo_root / relative).resolve(),
        (manifest_dir / relative).resolve(),
        (manifest_dir / Path(relative).name).resolve(),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _load_job_records(store_root: Path) -> list[dict[str, Any]]:
    records_dir = store_root / "records" / "job"
    if not records_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(records_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == "job":
            records.append(payload)
    return records


def _bind_trial_identity(
    trial: _DurableTrial,
    records: Sequence[Mapping[str, Any]],
    trial_id_counts: Counter[str],
) -> None:
    if trial_id_counts.get(trial.trial_id, 0) > 1:
        trial.identity_reasons.append(IDENTITY_UNRESOLVED)
        return

    job_name = trial.job_name
    cas_uri = trial.cas_uri
    if not job_name or not cas_uri:
        trial.identity_reasons.append(IDENTITY_UNRESOLVED)
        return

    matches = [record for record in records if record.get("record_id") == job_name]
    if len(matches) != 1:
        trial.identity_reasons.append(IDENTITY_UNRESOLVED)
        return

    record_uri = _optional_str(matches[0].get("uri"))
    if record_uri != cas_uri:
        trial.identity_reasons.append(IDENTITY_UNRESOLVED)
        return

    trial.cas_uri = record_uri


def _interpret_bound_cohorts(
    trials: Sequence[_DurableTrial],
    manifests: Mapping[str, tuple[Any, Path]],
    *,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path,
    database_url: str | None,
) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    by_campaign: dict[str, list[_DurableTrial]] = {}
    for trial in trials:
        if (
            trial.quarantined
            or trial.identity_reasons
            or not trial.campaign
            or trial.manifest_path is None
        ):
            continue
        by_campaign.setdefault(trial.campaign, []).append(trial)

    for campaign_id in sorted(by_campaign):
        bound = by_campaign[campaign_id]
        packed = manifests.get(campaign_id)
        if packed is None:
            for trial in bound:
                trial.identity_reasons.append(IDENTITY_UNRESOLVED)
            continue
        _manifest, manifest_path = packed
        interpret_path = _manifest_for_interpretation(
            manifest_path, {trial.trial_id for trial in bound}, output_dir
        )
        try:
            report = analyze_batch(
                interpret_path,
                repo_root=repo_root,
                store_root=store_root,
                output_dir=output_dir,
                derived_root=derived_root,
                database_url=database_url,
            )
        except Exception as exc:
            reason = _reason_from_exception(exc)
            for trial in bound:
                trial.identity_reasons.append(reason)
            continue
        for ref in report.get("source_refs") or []:
            if isinstance(ref, dict) and isinstance(ref.get("trial_id"), str):
                refs[ref["trial_id"]] = ref
        try:
            campaign_data_quality_report(
                interpret_path,
                repo_root=repo_root,
                store_root=store_root,
                output_dir=output_dir,
                derived_root=derived_root,
                database_url=database_url,
            )
        except Exception as exc:
            reason = _reason_from_exception(exc)
            for trial in bound:
                if trial.trial_id not in refs:
                    trial.identity_reasons.append(reason)
    return refs


def _manifest_for_interpretation(
    manifest_path: Path, bound_ids: set[str], output_dir: Path
) -> Path:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return manifest_path
    cohort = [row for row in raw.get("analysis_cohort_5_trials") or [] if isinstance(row, dict)]
    cohort_ids = {str(row.get("trial_id")) for row in cohort}
    if cohort_ids == bound_ids:
        return manifest_path
    filtered = [row for row in cohort if str(row.get("trial_id")) in bound_ids]
    ledger = [
        row for row in raw.get("controls_and_quarantine_ledger") or [] if isinstance(row, dict)
    ]
    raw["analysis_cohort_5_trials"] = filtered
    accounting = dict(raw.get("accounting") or {})
    retry_count = sum(1 for row in filtered if row.get("role") == "infrastructure_retry_1")
    control_count = sum(1 for row in ledger if row.get("role") == "free_control")
    quarantine_count = sum(1 for row in ledger if row.get("role") == "quarantined_auth_attempt")
    accounting["total_planned_specs"] = len(filtered)
    accounting["valid_analysis_ready_trials"] = len(filtered)
    accounting["total_executed_trials"] = len(filtered) + len(ledger)
    accounting["free_local_controls"] = control_count
    accounting["quarantined_infrastructure_attempts"] = quarantine_count
    accounting["retries"] = retry_count
    accounting["unresolved_evidence_count"] = 0
    raw["accounting"] = accounting
    dest_dir = output_dir / ".filtered-manifests"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / manifest_path.name
    dest.write_text(json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return dest


def _disposition_for_trial(
    trial: _DurableTrial,
    *,
    source_refs: Mapping[str, Mapping[str, Any]],
    store_root: Path,
    parquet_ids: set[str] | None,
    parquet_status: str | None,
    duckdb_reason: str | None,
    postgres_reason: str | None,
) -> BackfillDisposition:
    reasons = list(trial.original_reasons if trial.quarantined else [])
    reasons.extend(trial.identity_reasons)
    ref = source_refs.get(trial.trial_id)
    ir_digest = _optional_str(ref.get("ir_digest")) if ref else None
    pack_digest = _optional_str(ref.get("pack_digest")) if ref else None
    judgment_id = _optional_str(ref.get("judgment_id")) if ref else None
    decision_id = _optional_str(ref.get("decision_id")) if ref else None

    if trial.quarantined:
        if not reasons:
            reasons.append(MISSING_QUARANTINE_REASON)
        readiness: Literal["ANALYSIS_READY", "HOLD"] = "HOLD"
    elif trial.identity_reasons:
        readiness = "HOLD"
    elif ref is None:
        reasons.append("interpretation_failed")
        readiness = "HOLD"
    else:
        readiness = "ANALYSIS_READY"

    if trial.cas_uri:
        cas_reason = _cas_cross_check(store_root, trial.cas_uri)
        if cas_reason:
            reasons.append(cas_reason)
    if readiness == "ANALYSIS_READY":
        if parquet_status in {"missing", "unreadable"} or (
            parquet_ids is not None and trial.trial_id not in parquet_ids
        ):
            reasons.append(STORE_JOIN_UNAVAILABLE)
        if duckdb_reason:
            reasons.append(duckdb_reason)
        if postgres_reason:
            reasons.append(postgres_reason)
    if parquet_status == "orphan":
        reasons.append(ORPHAN_PARQUET_PARTITION)

    unique_reasons = sorted(set(reasons))
    if unique_reasons:
        readiness = "HOLD"
    return BackfillDisposition(
        trial_id=trial.trial_id,
        task_name=trial.task_name,
        campaign=trial.campaign,
        job_id=trial.job_id,
        cas_uri=trial.cas_uri,
        ir_digest=ir_digest,
        pack_digest=pack_digest,
        judgment_id=judgment_id,
        decision_id=decision_id,
        readiness=readiness,
        hold_reasons=unique_reasons,
    )


def _cas_cross_check(store_root: Path, cas_uri: str) -> str | None:
    try:
        load_archive(store_root, cas_uri)
    except Exception:
        return STORE_JOIN_UNAVAILABLE
    return None


def _parquet_trial_ids(derived_root: Path) -> tuple[set[str] | None, str | None]:
    path = derived_root / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    if not path.is_file():
        return None, "missing"
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["trial_id"])
        ids = {str(value) for value in table.column("trial_id").to_pylist() if value}
    except Exception:
        return None, "unreadable"
    return ids, None


def _duckdb_cross_check(repo_root: Path, derived_root: Path) -> str | None:
    try:
        from evallab.storage.attach import attach

        result = attach(repo_root=repo_root, explicit_derived=derived_root, environ={})
        result.connection.close()
    except Exception:
        return STORE_JOIN_UNAVAILABLE
    return None


def _postgres_cross_check(database_url: str | None) -> str | None:
    if not database_url:
        return None
    availability = catalog_availability(database_url)
    if availability.get("status") != "attached":
        return STORE_JOIN_UNAVAILABLE
    return None


def _reason_from_exception(exc: BaseException) -> str:
    text = str(exc)
    prefix = text.split(":", 1)[0].strip()
    if prefix in _KNOWN_EXCEPTION_REASONS:
        return prefix
    module = type(exc).__module__ or ""
    lowered = text.lower()
    if "psycopg" in module or "postgres" in lowered or "connection" in lowered:
        return STORE_JOIN_UNAVAILABLE
    return "interpretation_failed"


def _declared_population(inventory: Mapping[str, Any]) -> int | None:
    summary = inventory.get("status_summary")
    if isinstance(summary, dict) and isinstance(summary.get("total_indexed_trials"), int):
        return summary["total_indexed_trials"]
    return None


def _exit_code(
    *,
    discovery_ok: bool,
    discovered_count: int,
    disposition_count: int,
    declared: int | None,
) -> int:
    if not discovery_ok:
        return 1
    if discovered_count == 0:
        return 1
    if disposition_count != discovered_count:
        return 1
    if declared is not None and discovered_count != declared:
        return 1
    return 0


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value and value != "None":
        return value
    return None


HISTORICAL_CONTRACT_SCHEMA_VERSION = "historical-descriptive-contract/v2"
HISTORICAL_REGENERATION_SCHEMA_VERSION = "historical-contract-regeneration/v2"
HISTORICAL_REGENERATION_CODE_VERSION = "strict-git-snapshot/v2"
HISTORICAL_CONTRACT_FILENAME = "historical-contract.json"
HISTORICAL_CONTRACT_DOMAIN = b"evallab.historical-descriptive-contract.v2\x00"
HISTORICAL_MANIFEST_DOMAIN = b"evallab.historical-contract-regeneration.v2\x00"

TASK_REGISTRY_REVISION_UNBOUND = "task_registry_revision_unbound"
FAMILY_BINDING_ABSENT = "family_binding_absent"
DESIGN_CELL_BINDING_ABSENT = "design_cell_binding_absent"
OPPORTUNITY_COUNTS_UNBOUND = "opportunity_counts_unbound"
RUNTIME_PLATFORM_AUTHORITY_ABSENT = "runtime_platform_authority_absent"
NETWORK_ISOLATION_EVIDENCE_ABSENT = "network_isolation_evidence_absent"
VERIFIER_TRUTH_DIGEST_ABSENT = "verifier_truth_digest_absent"
EVENT_JOURNAL_ABSENT = "event_journal_absent"
FINAL_STATE_ABSENT = "final_state_absent"
TASK_CONTENT_DIGEST_ABSENT = "task_content_digest_absent"

_HISTORICAL_AUTHORITY_HOLDS = (
    TASK_REGISTRY_REVISION_UNBOUND,
    FAMILY_BINDING_ABSENT,
    DESIGN_CELL_BINDING_ABSENT,
    OPPORTUNITY_COUNTS_UNBOUND,
    RUNTIME_PLATFORM_AUTHORITY_ABSENT,
    NETWORK_ISOLATION_EVIDENCE_ABSENT,
)


class HistoricalRegenerationCountMismatch(HistoricalRegenerationError):
    """Raised before writes when the evidence-determined cohort drifts."""


class HistoricalRegenerationExpectationMismatch(HistoricalRegenerationError):
    """Raised before writes when a reviewed snapshot or plan digest differs."""


class HistoricalRegenerationConflict(HistoricalRegenerationError):
    """Raised rather than replacing a non-identical historical output."""


class HistoricalArtifactDigest(ContractModel):
    """One exact trial-relative source artifact."""

    path: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def path_is_trial_relative(self) -> HistoricalArtifactDigest:
        candidate = Path(self.path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("historical artifact paths must stay trial-relative")
        return self


class HistoricalDescriptiveContract(ContractModel):
    """Evidence-only historical description; never an admission contract."""

    model_config = ConfigDict(serialize_by_alias=True)

    schema_version: Literal["historical-descriptive-contract/v2"]
    evidence_class: Literal["descriptive-only"]
    source_snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_content_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    task_id: None = None
    registry_revision_digest: None = None
    family: None = None
    version: None = None
    benchmark_construct: None = Field(
        default=None,
        validation_alias="construct",
        serialization_alias="construct",
    )
    seed: None = None
    cell_id: None = None
    arm: None = None
    dose: None = None
    representation: None = None
    representation_order: None = None
    opportunity_counts: None = None
    runtime_platform: None = None
    network_isolation_status: NetworkIsolationStatus
    verifier_truth_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_inventory: tuple[HistoricalArtifactDigest, ...]
    source_complete: bool
    loadable: bool
    analysis_ready: Literal[False]
    admissible: Literal[False]
    hold_reasons: tuple[str, ...] = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def strict_historical_authority(self) -> HistoricalDescriptiveContract:
        paths = tuple(artifact.path for artifact in self.artifact_inventory)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("historical artifact inventory must be unique and sorted")
        if self.network_isolation_status != "unavailable":
            raise ValueError("historical network isolation must remain unavailable")
        if self.loadable != self.source_complete:
            raise ValueError("historical loadability must match exact source completeness")
        reasons = tuple(sorted(set(self.hold_reasons)))
        if reasons != self.hold_reasons:
            raise ValueError("historical hold reasons must be unique and sorted")
        body = self.model_dump(mode="json", exclude={"content_digest"})
        if self.content_digest != _domain_json_digest(HISTORICAL_CONTRACT_DOMAIN, body):
            raise ValueError("historical descriptive contract digest mismatch")
        return self


class HistoricalContractDisposition(ContractModel):
    """Reason-coded outcome for one promoted historical trial locator."""

    trial_locator: str = Field(min_length=1)
    source_inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    classification: Literal[
        "descriptive-complete",
        "descriptive-incomplete",
        "refused",
    ]
    descriptive_record_emitted: bool
    descriptive_record_path: str | None = None
    descriptive_record_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    readiness: Literal["HOLD"]
    admissible: Literal[False]
    hold_reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def disposition_is_fail_closed(self) -> HistoricalContractDisposition:
        locator = Path(self.trial_locator)
        if locator.is_absolute() or ".." in locator.parts:
            raise ValueError("historical trial locator must stay runs-root-relative")
        reasons = tuple(sorted(set(self.hold_reasons)))
        if reasons != self.hold_reasons:
            raise ValueError("historical disposition reasons must be unique and sorted")
        has_record = (
            self.descriptive_record_path is not None and self.descriptive_record_digest is not None
        )
        if self.descriptive_record_emitted != has_record:
            raise ValueError("historical disposition record projection mismatch")
        if self.classification == "refused" and self.descriptive_record_emitted:
            raise ValueError("refused historical trials cannot emit descriptive records")
        return self


class HistoricalPlannedOutput(ContractModel):
    """One exact output predicted by both dry-run and apply."""

    path: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class HistoricalRegenerationManifest(ContractModel):
    """Canonical dry-run/apply prediction over every promoted trial."""

    schema_version: Literal["historical-contract-regeneration/v2"]
    code_version: Literal["strict-git-snapshot/v2"]
    source_snapshot: HistoricalSourceSnapshotV1
    dispositions: tuple[HistoricalContractDisposition, ...]
    outputs: tuple[HistoricalPlannedOutput, ...]
    promoted_count: int = Field(ge=0)
    event_journal_count: int = Field(ge=0)
    truth_digest_count: int = Field(ge=0)
    truth_with_final_state_count: int = Field(ge=0)
    truth_missing_final_state_count: int = Field(ge=0)
    truth_missing_count: int = Field(ge=0)
    truth_missing_events_count: int = Field(ge=0)
    descriptive_record_count: int = Field(ge=0)
    analysis_ready_count: Literal[0]
    admissible_count: Literal[0]
    source_inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def counts_and_digest_match(self) -> HistoricalRegenerationManifest:
        locators = tuple(row.trial_locator for row in self.dispositions)
        if locators != tuple(sorted(set(locators))):
            raise ValueError("historical dispositions must be unique and sorted")
        output_paths = tuple(output.path for output in self.outputs)
        if output_paths != tuple(sorted(set(output_paths))):
            raise ValueError("historical outputs must be unique and sorted")
        output_by_path = {output.path: output for output in self.outputs}
        if self.promoted_count != len(self.dispositions):
            raise ValueError("promoted count does not match historical dispositions")
        if self.descriptive_record_count != len(self.outputs):
            raise ValueError("descriptive count does not match historical outputs")
        if self.truth_digest_count != self.descriptive_record_count:
            raise ValueError("truth-digest and descriptive counts must match")
        body = self.model_dump(mode="json", exclude={"content_digest"})
        if self.content_digest != _domain_json_digest(HISTORICAL_MANIFEST_DOMAIN, body):
            raise ValueError("historical regeneration manifest digest mismatch")
        snapshot_prefix = f"{self.source_snapshot.runs_root}/"
        for disposition in self.dispositions:
            trial_prefix = f"{snapshot_prefix}{disposition.trial_locator}/"
            trial_blobs = tuple(
                blob for blob in self.source_snapshot.blobs if blob.path.startswith(trial_prefix)
            )
            if disposition.source_inventory_digest != _source_inventory_digest(
                trial_prefix,
                trial_blobs,
            ):
                raise ValueError("historical disposition source inventory does not match snapshot")
            expected_output = (
                f"{disposition.trial_locator}/artifacts/{HISTORICAL_CONTRACT_FILENAME}"
            )
            if disposition.descriptive_record_emitted:
                planned = output_by_path.get(expected_output)
                if disposition.descriptive_record_path != expected_output:
                    raise ValueError("historical output path does not match trial locator")
                if planned is None or planned.digest != disposition.descriptive_record_digest:
                    raise ValueError("historical planned output digest does not match disposition")
            elif expected_output in output_by_path:
                raise ValueError("refused historical trial cannot appear in outputs")
        if any(
            blob.path.endswith(f"/artifacts/{HISTORICAL_CONTRACT_FILENAME}")
            for blob in self.source_snapshot.blobs
        ):
            raise ValueError("generated historical contracts cannot be snapshot inputs")
        return self


@dataclass(frozen=True)
class HistoricalRegenerationResult:
    """Operational result outside canonical manifest bytes."""

    manifest: HistoricalRegenerationManifest
    manifest_bytes: bytes
    created_output_count: int
    verified_output_count: int
    applied: bool
    resolved_commit: str


@dataclass(frozen=True)
class _HistoricalRegenerationPlan:
    manifest: HistoricalRegenerationManifest
    manifest_bytes: bytes
    outputs: tuple[tuple[Path, bytes], ...]


@dataclass(frozen=True)
class _HistoricalNamedTarget:
    content: bytes
    device: int
    inode: int


@dataclass
class _HistoricalRunsAnchor:
    repository: Path
    runs_root: str
    repository_fd: int
    runs_fd: int
    runs_device: int
    runs_inode: int

    @classmethod
    def open(cls, repository: Path, runs_root: str) -> _HistoricalRunsAnchor:
        _require_historical_descriptor_support()
        repository_fd: int | None = None
        runs_fd: int | None = None
        try:
            repository_fd = os.open(repository, _historical_directory_flags())
            repository_metadata = os.fstat(repository_fd)
            if not stat.S_ISDIR(repository_metadata.st_mode):
                raise HistoricalRegenerationConflict(
                    f"resolved Git repository is not a directory: {repository}"
                )
            runs_fd = _traverse_historical_directory(
                repository_fd,
                tuple(runs_root.split("/")),
                display_root=repository,
                stage="initial_runs_root",
            )
            runs_metadata = os.fstat(runs_fd)
            return cls(
                repository=repository,
                runs_root=runs_root,
                repository_fd=repository_fd,
                runs_fd=runs_fd,
                runs_device=runs_metadata.st_dev,
                runs_inode=runs_metadata.st_ino,
            )
        except BaseException:
            if runs_fd is not None:
                os.close(runs_fd)
            if repository_fd is not None:
                os.close(repository_fd)
            raise

    @property
    def display_path(self) -> Path:
        return self.repository.joinpath(*self.runs_root.split("/"))

    def open_parent(self, relative_path: Path) -> tuple[int, str]:
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise HistoricalRegenerationConflict(
                f"invalid anchored historical target: {relative_path}"
            )
        parent_fd = os.dup(self.runs_fd)
        try:
            if len(relative_path.parts) > 1:
                next_fd = _traverse_historical_directory(
                    parent_fd,
                    relative_path.parts[:-1],
                    display_root=self.display_path,
                    stage="output_parent",
                )
                os.close(parent_fd)
                parent_fd = next_fd
            return parent_fd, relative_path.parts[-1]
        except BaseException:
            os.close(parent_fd)
            raise

    def open_runs_directory(self) -> int:
        try:
            reopened = os.open(
                ".",
                _historical_directory_flags(),
                dir_fd=self.runs_fd,
            )
        except OSError as exc:
            raise HistoricalRegenerationConflict(
                f"held historical runs root became unavailable: {self.display_path}"
            ) from exc
        metadata = os.fstat(reopened)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != self.runs_device
            or metadata.st_ino != self.runs_inode
        ):
            os.close(reopened)
            raise HistoricalRegenerationConflict(
                f"held historical runs root identity changed: {self.display_path}"
            )
        return reopened

    def recheck(self) -> None:
        _historical_anchor_boundary("before_final_recheck", self.runs_root)
        current_fd: int | None = None
        try:
            current_fd = _traverse_historical_directory(
                self.repository_fd,
                tuple(self.runs_root.split("/")),
                display_root=self.repository,
                stage="final_recheck",
            )
            current = os.fstat(current_fd)
            if current.st_dev != self.runs_device or current.st_ino != self.runs_inode:
                raise HistoricalRegenerationConflict(
                    f"historical runs root was replaced: {self.display_path}"
                )
        finally:
            if current_fd is not None:
                os.close(current_fd)

    def close(self) -> None:
        os.close(self.runs_fd)
        os.close(self.repository_fd)

    def __enter__(self) -> _HistoricalRunsAnchor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_historical_contract_regeneration(
    *,
    repo_root: Path,
    runs_root: Path,
    source_revision: str,
    manifest_out: Path,
    expect_promoted: int,
    expect_derivable: int,
    expect_source_snapshot: str | None = None,
    expect_plan_digest: str | None = None,
    apply: bool = False,
) -> HistoricalRegenerationResult:
    """Plan exclusively from immutable Git blobs and publish without clobbering."""
    if apply and (expect_source_snapshot is None or expect_plan_digest is None):
        raise HistoricalRegenerationExpectationMismatch(
            "apply requires --expect-source-snapshot and --expect-plan-digest"
        )
    repository = resolve_git_repository(repo_root)
    normalized_runs_root = normalize_runs_root(runs_root)
    destination_runs_root = repository.joinpath(*normalized_runs_root.split("/"))
    manifest_parent = manifest_out.parent.resolve()
    manifest_out = manifest_parent / manifest_out.name
    if not manifest_parent.is_dir():
        raise HistoricalRegenerationError(
            f"manifest output parent is not a directory: {manifest_parent}"
        )
    try:
        manifest_out.relative_to(destination_runs_root)
    except ValueError:
        pass
    else:
        raise HistoricalRegenerationError("manifest output must stay outside the runs root")

    capture = capture_historical_source_snapshot(
        repo_root=repository,
        runs_root=Path(normalized_runs_root),
        source_revision=source_revision,
    )
    plan = _plan_historical_contract_regeneration(
        capture,
        destination_runs_root=destination_runs_root,
    )
    manifest = plan.manifest
    if manifest.promoted_count != expect_promoted:
        raise HistoricalRegenerationCountMismatch(
            "promoted trial count mismatch: "
            f"expected {expect_promoted}, observed {manifest.promoted_count}"
        )
    if manifest.descriptive_record_count != expect_derivable:
        raise HistoricalRegenerationCountMismatch(
            "derivable trial count mismatch: "
            f"expected {expect_derivable}, observed {manifest.descriptive_record_count}"
        )
    if (
        expect_source_snapshot is not None
        and manifest.source_snapshot.snapshot_digest != expect_source_snapshot
    ):
        raise HistoricalRegenerationExpectationMismatch(
            "historical source snapshot digest mismatch: "
            f"expected {expect_source_snapshot}, "
            f"observed {manifest.source_snapshot.snapshot_digest}"
        )
    if expect_plan_digest is not None and manifest.content_digest != expect_plan_digest:
        raise HistoricalRegenerationExpectationMismatch(
            "historical regeneration plan digest mismatch: "
            f"expected {expect_plan_digest}, observed {manifest.content_digest}"
        )

    created = 0
    verified = 0
    with _HistoricalRunsAnchor.open(repository, normalized_runs_root) as anchor:
        _preflight_manifest_identical_or_absent(
            manifest_out,
            plan.manifest_bytes,
            boundary=manifest_parent,
        )
        for output_path, output_bytes in plan.outputs:
            relative_path = output_path.relative_to(destination_runs_root)
            _preflight_identical_or_absent_anchored(
                anchor,
                relative_path,
                output_bytes,
            )

        if apply:
            anchor.recheck()
            for output_path, output_bytes in plan.outputs:
                relative_path = output_path.relative_to(destination_runs_root)
                was_created = _atomic_create_or_verify_historical_anchored(
                    anchor,
                    relative_path,
                    output_bytes,
                )
                if was_created:
                    created += 1
                else:
                    verified += 1
        _atomic_create_or_verify_manifest(
            manifest_out,
            plan.manifest_bytes,
            boundary=manifest_parent,
        )
        anchor.recheck()
        return HistoricalRegenerationResult(
            manifest=manifest,
            manifest_bytes=plan.manifest_bytes,
            created_output_count=created,
            verified_output_count=verified,
            applied=apply,
            resolved_commit=capture.resolved_commit,
        )


def _source_inventory_digest(
    trial_prefix: str,
    trial_blobs: tuple[HistoricalGitBlobV1, ...],
) -> str:
    rows = []
    for blob in trial_blobs:
        if not blob.path.startswith(trial_prefix):
            raise HistoricalSnapshotInvalid(
                f"snapshot blob does not belong to trial prefix {trial_prefix}: {blob.path}"
            )
        rows.append(
            {
                "path": blob.path.removeprefix(trial_prefix),
                "mode": blob.mode,
                "git_oid": blob.git_oid,
                "sha256_digest": blob.sha256_digest,
                "size_bytes": blob.size_bytes,
            }
        )
    return _domain_json_digest(
        HISTORICAL_CONTRACT_DOMAIN,
        {"source_blobs": rows},
    )


def _plan_historical_contract_regeneration(
    capture: HistoricalSourceCapture,
    *,
    destination_runs_root: Path,
) -> _HistoricalRegenerationPlan:
    snapshot = capture.snapshot
    runs_prefix = f"{snapshot.runs_root}/"
    markers = tuple(
        blob.path.removeprefix(runs_prefix)
        for blob in snapshot.blobs
        if blob.path.removeprefix(runs_prefix).endswith("/artifacts/manifest.json")
    )
    trial_locators = tuple(
        sorted(marker.removesuffix("/artifacts/manifest.json") for marker in markers)
    )

    dispositions: list[HistoricalContractDisposition] = []
    outputs: list[tuple[Path, bytes]] = []
    event_count = 0
    truth_count = 0
    truth_with_final_count = 0
    truth_missing_final_count = 0
    truth_missing_count = 0
    truth_missing_events_count = 0
    source_inventory_rows: list[dict[str, Any]] = []

    for locator in trial_locators:
        trial_prefix = f"{runs_prefix}{locator}/"
        trial_blobs = tuple(blob for blob in snapshot.blobs if blob.path.startswith(trial_prefix))
        (
            disposition,
            output,
            has_events,
            has_truth,
            has_final,
        ) = _historical_disposition_from_snapshot(
            capture,
            locator=locator,
            trial_blobs=trial_blobs,
            destination_runs_root=destination_runs_root,
        )
        dispositions.append(disposition)
        source_inventory_rows.append(
            {
                "trial_locator": disposition.trial_locator,
                "source_inventory_digest": disposition.source_inventory_digest,
            }
        )
        if output is not None:
            outputs.append(output)
        if has_events:
            event_count += 1
        if has_truth:
            truth_count += 1
            if has_final:
                truth_with_final_count += 1
            else:
                truth_missing_final_count += 1
        else:
            truth_missing_count += 1
            if not has_events:
                truth_missing_events_count += 1

    dispositions.sort(key=lambda row: row.trial_locator)
    outputs.sort(key=lambda item: item[0].relative_to(destination_runs_root).as_posix())
    output_rows = tuple(
        HistoricalPlannedOutput(
            path=path.relative_to(destination_runs_root).as_posix(),
            digest=_digest_bytes_str(content),
        )
        for path, content in outputs
    )
    body = {
        "schema_version": HISTORICAL_REGENERATION_SCHEMA_VERSION,
        "code_version": HISTORICAL_REGENERATION_CODE_VERSION,
        "source_snapshot": snapshot.model_dump(mode="json"),
        "dispositions": [row.model_dump(mode="json") for row in dispositions],
        "outputs": [row.model_dump(mode="json") for row in output_rows],
        "promoted_count": len(dispositions),
        "event_journal_count": event_count,
        "truth_digest_count": truth_count,
        "truth_with_final_state_count": truth_with_final_count,
        "truth_missing_final_state_count": truth_missing_final_count,
        "truth_missing_count": truth_missing_count,
        "truth_missing_events_count": truth_missing_events_count,
        "descriptive_record_count": len(outputs),
        "analysis_ready_count": 0,
        "admissible_count": 0,
        "source_inventory_digest": _domain_json_digest(
            HISTORICAL_MANIFEST_DOMAIN,
            {"source_inventories": source_inventory_rows},
        ),
    }
    manifest = HistoricalRegenerationManifest.model_validate(
        {
            **body,
            "content_digest": _domain_json_digest(HISTORICAL_MANIFEST_DOMAIN, body),
        }
    )
    manifest_bytes = _canonical_json_bytes(manifest.model_dump(mode="json"))
    return _HistoricalRegenerationPlan(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        outputs=tuple(outputs),
    )


def _historical_disposition_from_snapshot(
    capture: HistoricalSourceCapture,
    *,
    locator: str,
    trial_blobs: tuple[HistoricalGitBlobV1, ...],
    destination_runs_root: Path,
) -> tuple[
    HistoricalContractDisposition,
    tuple[Path, bytes] | None,
    bool,
    bool,
    bool,
]:
    snapshot = capture.snapshot
    trial_prefix = f"{snapshot.runs_root}/{locator}/"
    output_path = destination_runs_root / Path(locator) / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    inventory = tuple(
        HistoricalArtifactDigest(
            path=blob.path.removeprefix(trial_prefix),
            digest=blob.sha256_digest,
            size_bytes=blob.size_bytes,
        )
        for blob in trial_blobs
    )
    inventory_body = [artifact.model_dump(mode="json") for artifact in inventory]
    source_inventory_digest = _source_inventory_digest(trial_prefix, trial_blobs)
    paths = {artifact.path: artifact for artifact in inventory}
    has_events = "artifacts/app/output/benchmark-events.jsonl" in paths
    has_final = "artifacts/app/output/final-state.json" in paths

    lock = _load_json_object_bytes(capture.document_bytes.get(f"{trial_prefix}lock.json"))
    lock_task = lock.get("task") if lock is not None else None
    raw_task_digest = lock_task.get("digest") if isinstance(lock_task, dict) else None
    task_content_digest = raw_task_digest if _is_sha256_digest(raw_task_digest) else None

    verifier = _load_json_object_bytes(
        capture.document_bytes.get(f"{trial_prefix}verifier/result.json")
    )
    raw_truth_digest = verifier.get("truth_digest") if verifier is not None else None
    truth_digest = raw_truth_digest if _is_sha256_digest(raw_truth_digest) else None
    has_truth = truth_digest is not None

    reasons = list(_HISTORICAL_AUTHORITY_HOLDS)
    if task_content_digest is None:
        reasons.append(TASK_CONTENT_DIGEST_ABSENT)
    if not has_truth:
        reasons.append(VERIFIER_TRUTH_DIGEST_ABSENT)
    if not has_events:
        reasons.append(EVENT_JOURNAL_ABSENT)
    if not has_final:
        reasons.append(FINAL_STATE_ABSENT)
    hold_reasons = tuple(sorted(set(reasons)))

    if not has_truth:
        return (
            HistoricalContractDisposition(
                trial_locator=locator,
                source_inventory_digest=source_inventory_digest,
                classification="refused",
                descriptive_record_emitted=False,
                readiness="HOLD",
                admissible=False,
                hold_reasons=hold_reasons,
            ),
            None,
            has_events,
            False,
            has_final,
        )

    source_complete = has_events and has_final
    contract_body = {
        "schema_version": HISTORICAL_CONTRACT_SCHEMA_VERSION,
        "evidence_class": "descriptive-only",
        "source_snapshot_digest": snapshot.snapshot_digest,
        "task_content_digest": task_content_digest,
        "task_id": None,
        "registry_revision_digest": None,
        "family": None,
        "version": None,
        "construct": None,
        "seed": None,
        "cell_id": None,
        "arm": None,
        "dose": None,
        "representation": None,
        "representation_order": None,
        "opportunity_counts": None,
        "runtime_platform": None,
        "network_isolation_status": "unavailable",
        "verifier_truth_digest": truth_digest,
        "artifact_inventory": inventory_body,
        "source_complete": source_complete,
        "loadable": source_complete,
        "analysis_ready": False,
        "admissible": False,
        "hold_reasons": hold_reasons,
    }
    contract = HistoricalDescriptiveContract.model_validate(
        {
            **contract_body,
            "content_digest": _domain_json_digest(
                HISTORICAL_CONTRACT_DOMAIN,
                contract_body,
            ),
        }
    )
    output_bytes = _canonical_json_bytes(contract.model_dump(mode="json"))
    output_relative = output_path.relative_to(destination_runs_root).as_posix()
    disposition = HistoricalContractDisposition(
        trial_locator=locator,
        source_inventory_digest=source_inventory_digest,
        classification=("descriptive-complete" if source_complete else "descriptive-incomplete"),
        descriptive_record_emitted=True,
        descriptive_record_path=output_relative,
        descriptive_record_digest=_digest_bytes_str(output_bytes),
        readiness="HOLD",
        admissible=False,
        hold_reasons=hold_reasons,
    )
    return disposition, (output_path, output_bytes), has_events, True, has_final


def _read_regular_file_no_follow(path: Path) -> bytes | None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise HistoricalRegenerationError("platform lacks required no-follow file support")
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        with os.fdopen(file_fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(file_fd)


def _load_json_object_bytes(content: bytes | None) -> dict[str, Any] | None:
    if content is None:
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _is_sha256_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    suffix = value.removeprefix("sha256:")
    return len(suffix) == 64 and all(character in "0123456789abcdef" for character in suffix)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _domain_json_digest(domain: bytes, value: Any) -> str:
    return _digest_bytes_str(domain + _canonical_json_bytes(value))


def _digest_bytes_str(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _historical_publication_boundary(stage: str) -> None:
    """Deterministic test seam for mutable destination publication only."""
    del stage


def _historical_anchor_boundary(stage: str, component: str) -> None:
    """Deterministic test seam for anchored destination traversal."""
    del stage, component


def _require_historical_descriptor_support() -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HistoricalRegenerationConflict(
            "platform lacks required no-follow directory publication support"
        )


def _historical_directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _traverse_historical_directory(
    parent_fd: int,
    components: tuple[str, ...],
    *,
    display_root: Path,
    stage: str,
) -> int:
    if not components or any(component in {"", ".", ".."} for component in components):
        raise HistoricalRegenerationConflict(
            f"invalid historical directory traversal below: {display_root}"
        )
    current_fd = os.dup(parent_fd)
    traversed = display_root
    try:
        for component in components:
            traversed /= component
            try:
                before = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise HistoricalRegenerationConflict(
                    f"historical directory is missing or unreadable: {traversed}"
                ) from exc
            if not stat.S_ISDIR(before.st_mode):
                raise HistoricalRegenerationConflict(
                    f"historical directory component is symlinked or non-directory: {traversed}"
                )
            _historical_anchor_boundary(stage, component)
            try:
                next_fd = os.open(
                    component,
                    _historical_directory_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise HistoricalRegenerationConflict(
                    f"historical directory component changed or is unsafe: {traversed}"
                ) from exc
            after = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(after.st_mode)
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                os.close(next_fd)
                raise HistoricalRegenerationConflict(
                    f"historical directory component changed during traversal: {traversed}"
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_manifest_parent(*, boundary: Path, target: Path) -> tuple[int, str]:
    try:
        relative = target.relative_to(boundary)
    except ValueError as exc:
        raise HistoricalRegenerationConflict(
            f"historical target escapes its publication boundary: {target}"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HistoricalRegenerationConflict(f"invalid historical target: {target}")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HistoricalRegenerationConflict(
            "platform lacks required no-follow directory publication support"
        )

    directory_fd: int | None = None
    try:
        directory_fd = os.open(
            boundary,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        raise HistoricalRegenerationConflict(
            f"historical target has a missing, symlinked, or non-directory parent: {target}"
        ) from exc
    return directory_fd, relative.parts[-1]


def _read_existing_historical_target(
    *,
    directory_fd: int,
    filename: str,
    display_path: Path,
) -> _HistoricalNamedTarget | None:
    try:
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HistoricalRegenerationConflict(
            f"historical target is symlinked or unreadable: {display_path}"
        ) from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise HistoricalRegenerationConflict(
                f"historical target is not a regular file: {display_path}"
            )
        with os.fdopen(file_fd, "rb", closefd=False) as handle:
            content = handle.read()
        return _HistoricalNamedTarget(
            content=content,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    finally:
        os.close(file_fd)


def _preflight_manifest_identical_or_absent(
    path: Path,
    content: bytes,
    *,
    boundary: Path,
) -> None:
    directory_fd, filename = _open_manifest_parent(boundary=boundary, target=path)
    try:
        existing = _read_existing_historical_target(
            directory_fd=directory_fd,
            filename=filename,
            display_path=path,
        )
        if existing is not None and existing.content != content:
            raise HistoricalRegenerationConflict(
                f"refusing to replace non-identical historical record: {path}"
            )
    finally:
        os.close(directory_fd)


def _preflight_identical_or_absent_anchored(
    anchor: _HistoricalRunsAnchor,
    relative_path: Path,
    content: bytes,
) -> None:
    directory_fd, filename = anchor.open_parent(relative_path)
    display_path = anchor.display_path / relative_path
    try:
        existing = _read_existing_historical_target(
            directory_fd=directory_fd,
            filename=filename,
            display_path=display_path,
        )
        if existing is not None and existing.content != content:
            raise HistoricalRegenerationConflict(
                f"refusing to replace non-identical historical record: {display_path}"
            )
    finally:
        os.close(directory_fd)


def _link_historical_temp(
    temporary_name: str,
    target_name: str,
    *,
    directory_fd: int,
) -> None:
    os.link(
        temporary_name,
        target_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )


def _create_or_verify_historical_at(
    *,
    directory_fd: int,
    filename: str,
    display_path: Path,
    content: bytes,
) -> bool:
    temporary_name = f".{filename}.historical-{os.getpid()}-{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        existing = _read_existing_historical_target(
            directory_fd=directory_fd,
            filename=filename,
            display_path=display_path,
        )
        if existing is not None:
            if existing.content != content:
                raise HistoricalRegenerationConflict(
                    f"refusing to replace non-identical historical record: {display_path}"
                )
            _historical_publication_boundary("before_existing_identical_return")
            verified = _read_existing_historical_target(
                directory_fd=directory_fd,
                filename=filename,
                display_path=display_path,
            )
            if (
                verified is None
                or verified.content != content
                or verified.device != existing.device
                or verified.inode != existing.inode
            ):
                raise HistoricalRegenerationConflict(
                    f"existing historical target changed at verification commit: {display_path}"
                )
            return False

        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            view = memoryview(content)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise HistoricalRegenerationConflict(
                        f"failed to stage complete historical record: {display_path}"
                    )
                view = view[written:]
            staged_metadata = os.fstat(temporary_fd)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)

        created = False
        try:
            _link_historical_temp(
                temporary_name,
                filename,
                directory_fd=directory_fd,
            )
            created = True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            winner = _read_existing_historical_target(
                directory_fd=directory_fd,
                filename=filename,
                display_path=display_path,
            )
            if winner is None or winner.content != content:
                raise HistoricalRegenerationConflict(
                    f"concurrent non-identical historical record won publication: {display_path}"
                ) from exc
        published = _read_existing_historical_target(
            directory_fd=directory_fd,
            filename=filename,
            display_path=display_path,
        )
        if published is None or published.content != content:
            raise HistoricalRegenerationConflict(
                f"historical target changed at publication commit: {display_path}"
            )
        if created and (
            published.device != staged_metadata.st_dev or published.inode != staged_metadata.st_ino
        ):
            raise HistoricalRegenerationConflict(
                f"created historical target was replaced at publication commit: {display_path}"
            )
        return created
    finally:
        if temporary_created:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)


def _atomic_create_or_verify_manifest(
    path: Path,
    content: bytes,
    *,
    boundary: Path,
) -> bool:
    """Create without clobbering or verify exact regular-file bytes."""
    directory_fd, filename = _open_manifest_parent(boundary=boundary, target=path)
    try:
        return _create_or_verify_historical_at(
            directory_fd=directory_fd,
            filename=filename,
            display_path=path,
            content=content,
        )
    finally:
        os.close(directory_fd)


def _atomic_create_or_verify_historical_anchored(
    anchor: _HistoricalRunsAnchor,
    relative_path: Path,
    content: bytes,
) -> bool:
    directory_fd, filename = anchor.open_parent(relative_path)
    try:
        return _create_or_verify_historical_at(
            directory_fd=directory_fd,
            filename=filename,
            display_path=anchor.display_path / relative_path,
            content=content,
        )
    finally:
        os.close(directory_fd)


class HistoricalContractSetVerificationError(HistoricalRegenerationError):
    """Raised when a manifest, source snapshot, or complete output set fails reopen."""


def _enumerate_historical_contract_namespace(
    anchor: _HistoricalRunsAnchor,
) -> frozenset[str]:
    """Enumerate exact generated names below one held runs-root descriptor."""
    try:
        root_fd = anchor.open_runs_directory()
    except HistoricalRegenerationConflict as exc:
        raise HistoricalContractSetVerificationError(
            f"historical runs root is unavailable: {anchor.display_path}"
        ) from exc

    found: set[str] = set()
    visited: set[tuple[int, int]] = set()

    def walk(directory_fd: int, parts: tuple[str, ...]) -> None:
        metadata = os.fstat(directory_fd)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in visited:
            raise HistoricalContractSetVerificationError(
                "historical output namespace contains a directory cycle"
            )
        visited.add(identity)
        try:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        child_metadata = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise HistoricalContractSetVerificationError(
                            f"historical output namespace changed during enumeration: {name!r}"
                        ) from exc
                    if name != HISTORICAL_CONTRACT_FILENAME and not stat.S_ISDIR(
                        child_metadata.st_mode
                    ):
                        continue

                    child_parts = (*parts, name)
                    try:
                        relative_path = PurePosixPath(*child_parts).as_posix()
                        relative_path.encode("utf-8", errors="strict")
                    except (UnicodeEncodeError, ValueError) as exc:
                        raise HistoricalContractSetVerificationError(
                            "historical output namespace contains an unsafe path"
                        ) from exc
                    if (
                        not name
                        or name in {".", ".."}
                        or "/" in name
                        or relative_path != "/".join(child_parts)
                    ):
                        raise HistoricalContractSetVerificationError(
                            f"historical output namespace path is not canonical: {relative_path}"
                        )

                    if name == HISTORICAL_CONTRACT_FILENAME:
                        if not stat.S_ISREG(child_metadata.st_mode):
                            raise HistoricalContractSetVerificationError(
                                "named historical contract occurrence is symlinked or nonregular: "
                                f"{relative_path}"
                            )
                        found.add(relative_path)
                        continue
                    if stat.S_ISLNK(child_metadata.st_mode):
                        continue
                    if not stat.S_ISDIR(child_metadata.st_mode):
                        continue
                    try:
                        child_fd = os.open(
                            name,
                            _historical_directory_flags(),
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise HistoricalContractSetVerificationError(
                            f"historical output directory changed during enumeration: {relative_path}"
                        ) from exc
                    try:
                        opened = os.fstat(child_fd)
                        if (
                            opened.st_dev != child_metadata.st_dev
                            or opened.st_ino != child_metadata.st_ino
                        ):
                            raise HistoricalContractSetVerificationError(
                                "historical output directory changed during enumeration: "
                                f"{relative_path}"
                            )
                        walk(child_fd, child_parts)
                    finally:
                        os.close(child_fd)
        finally:
            visited.remove(identity)

    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    return frozenset(found)


def _verify_anchored_historical_outputs(
    *,
    anchor: _HistoricalRunsAnchor,
    manifest: HistoricalRegenerationManifest,
    output_by_path: Mapping[str, HistoricalPlannedOutput],
    disposition_by_path: Mapping[str, HistoricalContractDisposition],
    snapshot_prefix: str,
) -> None:
    observed_output_paths = _enumerate_historical_contract_namespace(anchor)
    if observed_output_paths != frozenset(output_by_path):
        missing = sorted(set(output_by_path).difference(observed_output_paths))
        extra = sorted(observed_output_paths.difference(output_by_path))
        raise HistoricalContractSetVerificationError(
            f"historical output namespace differs from manifest: missing={missing}; extra={extra}"
        )

    for relative_path, planned in output_by_path.items():
        relative_target = Path(relative_path)
        target = anchor.display_path / relative_target
        directory_fd, filename = anchor.open_parent(relative_target)
        try:
            named_target = _read_existing_historical_target(
                directory_fd=directory_fd,
                filename=filename,
                display_path=target,
            )
        finally:
            os.close(directory_fd)
        if named_target is None:
            raise HistoricalContractSetVerificationError(
                f"historical contract is missing: {relative_path}"
            )
        if _digest_bytes_str(named_target.content) != planned.digest:
            raise HistoricalContractSetVerificationError(
                f"historical contract bytes differ from plan: {relative_path}"
            )
        try:
            contract = HistoricalDescriptiveContract.model_validate_json(named_target.content)
        except ValueError as exc:
            raise HistoricalContractSetVerificationError(
                f"historical contract is not valid v2 data: {relative_path}"
            ) from exc
        if _canonical_json_bytes(contract.model_dump(mode="json")) != named_target.content:
            raise HistoricalContractSetVerificationError(
                f"historical contract is not canonical: {relative_path}"
            )
        if contract.source_snapshot_digest != manifest.source_snapshot.snapshot_digest:
            raise HistoricalContractSetVerificationError(
                f"historical contract binds a different source snapshot: {relative_path}"
            )
        disposition = disposition_by_path[relative_path]
        contract_trial_prefix = f"{snapshot_prefix}{disposition.trial_locator}/"
        if planned.digest != disposition.descriptive_record_digest:
            raise HistoricalContractSetVerificationError(
                f"planned output digest differs from disposition: {relative_path}"
            )
        expected_inventory = tuple(
            HistoricalArtifactDigest(
                path=blob.path.removeprefix(contract_trial_prefix),
                digest=blob.sha256_digest,
                size_bytes=blob.size_bytes,
            )
            for blob in manifest.source_snapshot.blobs
            if blob.path.startswith(contract_trial_prefix)
        )
        if contract.artifact_inventory != expected_inventory:
            raise HistoricalContractSetVerificationError(
                f"historical contract inventory differs from snapshot: {relative_path}"
            )


def verify_historical_contract_set(
    *,
    repo_root: Path,
    runs_root: Path,
    manifest_bytes: bytes,
    expected_source_snapshot: str,
    expected_plan_digest: str,
    offered_live_sources: Mapping[str, Path] | None = None,
) -> HistoricalRegenerationManifest:
    """Authenticate one v2 manifest, its Git blobs, and its complete output set."""
    try:
        manifest = HistoricalRegenerationManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise HistoricalContractSetVerificationError(
            "historical regeneration manifest is not valid v2 canonical data"
        ) from exc
    if _canonical_json_bytes(manifest.model_dump(mode="json")) != manifest_bytes:
        raise HistoricalContractSetVerificationError(
            "historical regeneration manifest bytes are not canonical"
        )
    if manifest.source_snapshot.snapshot_digest != expected_source_snapshot:
        raise HistoricalContractSetVerificationError(
            "historical source snapshot does not match independent expectation"
        )
    if manifest.content_digest != expected_plan_digest:
        raise HistoricalContractSetVerificationError(
            "historical plan does not match independent expectation"
        )

    repository = resolve_git_repository(repo_root)
    normalized_runs_root = normalize_runs_root(runs_root)
    if manifest.source_snapshot.runs_root != normalized_runs_root:
        raise HistoricalContractSetVerificationError(
            "manifest runs root does not match verifier destination"
        )
    reopen_historical_source_snapshot(
        repo_root=repository,
        snapshot=manifest.source_snapshot,
    )
    output_by_path = {row.path: row for row in manifest.outputs}
    disposition_by_path = {
        row.descriptive_record_path: row
        for row in manifest.dispositions
        if row.descriptive_record_path is not None
    }
    if set(output_by_path) != set(disposition_by_path):
        raise HistoricalContractSetVerificationError(
            "manifest dispositions and planned outputs disagree"
        )

    snapshot_prefix = f"{manifest.source_snapshot.runs_root}/"
    for disposition in manifest.dispositions:
        trial_prefix = f"{snapshot_prefix}{disposition.trial_locator}/"
        trial_blobs = tuple(
            blob for blob in manifest.source_snapshot.blobs if blob.path.startswith(trial_prefix)
        )
        expected_inventory_digest = _source_inventory_digest(
            trial_prefix,
            trial_blobs,
        )
        if disposition.source_inventory_digest != expected_inventory_digest:
            raise HistoricalContractSetVerificationError(
                f"trial source inventory does not match snapshot: {disposition.trial_locator}"
            )

    with _HistoricalRunsAnchor.open(repository, normalized_runs_root) as anchor:
        _verify_anchored_historical_outputs(
            anchor=anchor,
            manifest=manifest,
            output_by_path=output_by_path,
            disposition_by_path=disposition_by_path,
            snapshot_prefix=snapshot_prefix,
        )
        selected_by_path = {blob.path: blob for blob in manifest.source_snapshot.blobs}
        for repo_relative, live_path in (offered_live_sources or {}).items():
            selected = selected_by_path.get(repo_relative)
            if selected is None:
                raise HistoricalContractSetVerificationError(
                    f"offered live source is not selected by manifest: {repo_relative}"
                )
            live_bytes = _read_regular_file_no_follow(live_path)
            if (
                live_bytes is None
                or len(live_bytes) != selected.size_bytes
                or _digest_bytes_str(live_bytes) != selected.sha256_digest
            ):
                raise HistoricalContractSetVerificationError(
                    f"offered live source differs from selected Git blob: {repo_relative}"
                )
        anchor.recheck()
    return manifest
