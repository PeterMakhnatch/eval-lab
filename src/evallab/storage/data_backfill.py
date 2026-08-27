"""Single-command all-durable completed-trial backfill orchestrator.

Binds every discovered durable trial to a reason-coded ANALYSIS_READY or HOLD
disposition. Identity is fail-closed: missing, ambiguous, duplicate, or foreign
CAS job bindings become HOLD with ``quarantine_job_identity_unresolved``.
Quarantined trials never enter interpretation. No judge, model, provider, or
auto-accept path is reachable from this module.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evallab.database import catalog_availability
from evallab.evidence_store import load_archive
from evallab.interpretation.trajectory_data_quality import (
    campaign_data_quality_report,
    load_cross_campaign_inventory,
)
from evallab.interpretation.trajectory_judgment import canonical_json_digest
from evallab.interpretation.trajectory_runtime import analyze_batch, load_campaign_analysis_manifest
from evallab.schemas import ContractModel

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
