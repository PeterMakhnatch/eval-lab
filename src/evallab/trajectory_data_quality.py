"""Deterministic per-campaign data-quality operator (Platform-only).

Reports readiness/HOLD, coverage and source gaps, pack selection/omission,
CAS identity, citation reopen availability, and projection availability
without judge calls, IR rebuild, pack rebuild, or CAS restore of
quarantined rows.

Missing PostgreSQL and missing jobs-parquet hive are unavailable/missing
with ``row_count=None``, never coerced to zero.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.database import catalog_availability
from evallab.trajectory_acceptance import AUTO_ACCEPTANCE_ENABLED
from evallab.trajectory_hydration import CitationHandle, hydrate_citation
from evallab.trajectory_judgment import canonical_json_digest
from evallab.trajectory_runtime import CampaignAnalysisItem, load_campaign_analysis_manifest

SCHEMA_VERSION = "campaign-data-quality/v1"
_QUARANTINE_STATUSES = frozenset({"quarantine", "fail", "quarantined"})
_SIDECAR_FILES = (
    "trajectory_ir.json",
    "evidence_pack.json",
    "machine_judgment.json",
    "acceptance_decision.json",
)
_SIDECAR_SEARCH_RELATIVE = (
    "interpretation",
    "analyses",
)


def _unknown_count(*, status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "row_count": None}


def _parquet_row_count(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        total += pq.read_table(path).num_rows
    return total


def _jobs_parquet_projection(derived_root: Path) -> dict[str, Any]:
    """Hive jobs parquet is ``job_id=*/jobs.parquet`` (job-level table).

    Stray ``job_id=*/trial_id=*/jobs.parquet`` files nested under ``trial_id``
    are recorded separately and are not treated as the hive or as zero jobs.
    """
    parquet_root = derived_root / "parquet"
    hive = [
        path
        for path in sorted(parquet_root.glob("job_id=*/jobs.parquet"))
        if path.parent.parent == parquet_root
    ]
    stray = sorted(parquet_root.glob("job_id=*/trial_id=*/jobs.parquet"))
    if not hive:
        return {
            "status": "missing",
            "reason": "jobs_parquet_hive_absent",
            "row_count": None,
            "stray_jobs_parquet_paths": [str(path) for path in stray],
        }
    return {
        "status": "present",
        "reason": None,
        "row_count": _parquet_row_count(hive),
        "stray_jobs_parquet_paths": [str(path) for path in stray],
    }


def _named_parquet_projection(derived_root: Path, relative: str) -> dict[str, Any]:
    path = derived_root / relative
    if path.is_file():
        return {"status": "present", "reason": None, "row_count": _parquet_row_count([path])}
    parent = path.parent
    matches = sorted(parent.glob(path.name)) if parent.is_dir() else []
    if not matches:
        return _unknown_count(status="missing", reason=f"{relative}_absent")
    return {
        "status": "present",
        "reason": None,
        "row_count": _parquet_row_count(matches),
    }


def _trial_facts_projection(derived_root: Path) -> dict[str, Any]:
    paths = sorted((derived_root / "parquet").glob("job_id=*/trial_id=*/trial_facts.parquet"))
    if not paths:
        return _unknown_count(status="missing", reason="trial_facts_hive_absent")
    return {"status": "present", "reason": None, "row_count": _parquet_row_count(paths)}


def _sidecar_search_roots(output_dir: Path, derived_root: Path) -> list[Path]:
    roots: list[Path] = []
    for candidate in (output_dir, *(derived_root / rel for rel in _SIDECAR_SEARCH_RELATIVE)):
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _find_trial_sidecar_dir(trial_id: str, roots: list[Path]) -> Path | None:
    for root in roots:
        trial_dir = root / trial_id
        if not trial_dir.is_dir():
            continue
        if all((trial_dir / name).is_file() for name in _SIDECAR_FILES):
            return trial_dir
        for child in sorted(trial_dir.iterdir()):
            if child.is_dir() and all((child / name).is_file() for name in _SIDECAR_FILES):
                return child
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _field_presence(value: Any) -> str:
    if value is None or value == "" or value == "n/a":
        return "unknown"
    return "present"


def _pack_selection(pack: dict[str, Any] | None) -> dict[str, Any]:
    if pack is None:
        return {
            "status": "unknown",
            "selected_events": None,
            "omitted_events": None,
            "budget_tokens": None,
            "overflow_reason": None,
            "is_model_callable": None,
        }
    selected = sum(
        int(window.get("event_count") or 0) for window in pack.get("selected_windows") or []
    )
    omitted = sum(
        int(omitted_range.get("event_count") or 0)
        for omitted_range in pack.get("omitted_ranges") or []
    )
    return {
        "status": "present",
        "selected_events": selected,
        "omitted_events": omitted,
        "budget_tokens": pack.get("budget_tokens"),
        "overflow_reason": pack.get("overflow_reason"),
        "is_model_callable": pack.get("is_model_callable"),
    }


def _collect_handles(ir: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    handles: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        key = canonical_json_digest(payload)
        if key in seen:
            return
        seen.add(key)
        handles.append(payload)

    for event in ir.get("events") or []:
        if isinstance(event, dict):
            _add(event.get("source_citation"))
    for window in pack.get("selected_windows") or []:
        if isinstance(window, dict):
            _add(window.get("reopening_citation"))
            for event in window.get("events") or []:
                if isinstance(event, dict):
                    _add(event.get("source_citation"))
    for omitted in pack.get("omitted_ranges") or []:
        if isinstance(omitted, dict):
            _add(omitted.get("reopening_citation"))
    return handles


def _citation_reopen(
    *,
    ir: dict[str, Any] | None,
    pack: dict[str, Any] | None,
    store_root: Path,
    quarantined: bool,
) -> dict[str, Any]:
    if quarantined:
        return {
            "status": "skipped",
            "reason": "quarantined_input",
            "available": None,
            "unavailable": None,
            "unreopenable": None,
        }
    if ir is None or pack is None:
        return {
            "status": "unknown",
            "reason": "sidecars_absent",
            "available": None,
            "unavailable": None,
            "unreopenable": None,
        }
    available = 0
    unavailable = 0
    unreopenable = 0
    for payload in _collect_handles(ir, pack):
        try:
            handle = CitationHandle(**payload)
            hydrated = hydrate_citation(handle, repo_root=store_root)
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            if limitation in {"omitted_unreopenable", "source_missing"}:
                unreopenable += 1
                unavailable += 1
            elif limitation:
                unavailable += 1
            else:
                available += 1
        except (TypeError, ValueError):
            unavailable += 1
            unreopenable += 1
    return {
        "status": "present",
        "reason": None,
        "available": available,
        "unavailable": unavailable,
        "unreopenable": unreopenable,
    }


def _item_source_gaps(item: CampaignAnalysisItem, ir: dict[str, Any] | None) -> dict[str, Any]:
    verifier = item.verifier_digest
    if ir is not None and "verifier_digest" in ir:
        verifier = ir.get("verifier_digest")
    unknowns = list(ir.get("unknowns") or []) if ir is not None else None
    return {
        "verifier_digest": verifier,
        "verifier_digest_presence": _field_presence(verifier),
        "task_digest": item.task_digest,
        "task_digest_presence": _field_presence(item.task_digest),
        "atif_path": item.atif_path,
        "atif_path_presence": _field_presence(item.atif_path),
        "ir_unknowns": unknowns,
        "ir_unknowns_status": "present" if ir is not None else "unknown",
    }


def _item_coverage(
    item: CampaignAnalysisItem, ir: dict[str, Any] | None, pack: dict[str, Any] | None
) -> list[str]:
    gaps: list[str] = []
    if not AUTO_ACCEPTANCE_ENABLED:
        gaps.append("judge_execution_disabled")
    if item.quality_status == "warn":
        gaps.append("quality_warning")
    findings = [str(finding) for finding in item.quality_findings]
    if any("UNPAIRED" in finding for finding in findings):
        gaps.append("ATIF_UNPAIRED_TOOL_CALL")
    if ir is not None:
        if ir.get("linkage_coverage") in {"degraded", "unlinked"}:
            gaps.append("unpaired_linkage")
        if int(ir.get("unpaired_tool_calls_count") or 0) > 0:
            gaps.append("ATIF_UNPAIRED_TOOL_CALL")
    if pack is not None and not pack.get("is_model_callable", True):
        gaps.append("pack_incomplete")
    if item.quality_status in _QUARANTINE_STATUSES:
        gaps.append("quarantined_input")
    if item.quality_status == "no_atif":
        gaps.append("no_atif")
    return sorted(set(gaps))


def campaign_data_quality_report(
    inventory_path: Path,
    *,
    repo_root: Path,
    store_root: Path,
    output_dir: Path,
    derived_root: Path | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic HOLD/coverage/CAS/projection report for one campaign."""
    del repo_root
    inventory_path = inventory_path.resolve()
    store_root = store_root.resolve()
    output_dir = output_dir.resolve()
    derived = (derived_root or output_dir.parent).resolve()
    manifest = load_campaign_analysis_manifest(inventory_path)
    sidecar_roots = _sidecar_search_roots(output_dir, derived)

    postgres = catalog_availability(database_url)
    jobs_parquet = _jobs_parquet_projection(derived)
    projections = {
        "postgres": postgres,
        "jobs_parquet": jobs_parquet,
        "trial_facts": _trial_facts_projection(derived),
        "interpretation_artifacts": _named_parquet_projection(
            derived, "interpretation_artifacts/interpretation_artifacts.parquet"
        ),
        "machine_judgments": _named_parquet_projection(
            derived, "machine_judgments/machine_judgments.parquet"
        ),
        "acceptance_decisions": _named_parquet_projection(
            derived, "acceptance_decisions/acceptance_decisions.parquet"
        ),
    }

    trials: list[dict[str, Any]] = []
    coverage_counts: Counter[str] = Counter()
    source_cas: Counter[str] = Counter()
    pack_selected: list[int] = []
    pack_omitted: list[int] = []
    budgets: list[int] = []
    sidecars_present = 0
    sidecars_unknown = 0

    for item in manifest.items:
        quarantined = item.quality_status in _QUARANTINE_STATUSES
        sidecar_dir = None if quarantined else _find_trial_sidecar_dir(item.trial_id, sidecar_roots)
        ir = None
        pack = None
        if sidecar_dir is not None:
            ir = _load_json(sidecar_dir / "trajectory_ir.json")
            pack = _load_json(sidecar_dir / "evidence_pack.json")
            if ir is not None and pack is not None:
                sidecars_present += 1
            else:
                sidecars_unknown += 1
        else:
            sidecars_unknown += 1

        selection = _pack_selection(pack)
        if selection["status"] == "present":
            if isinstance(selection["selected_events"], int):
                pack_selected.append(selection["selected_events"])
            if isinstance(selection["omitted_events"], int):
                pack_omitted.append(selection["omitted_events"])
            if isinstance(selection["budget_tokens"], int):
                budgets.append(selection["budget_tokens"])

        gaps = _item_coverage(item, ir, pack)
        for gap in gaps:
            coverage_counts[gap] += 1
        if item.cas_uri:
            source_cas[item.cas_uri] += 1

        artifact_cas_uri: str | None = None
        if pack is not None:
            artifact_cas_uri = pack.get("source_digests", {}).get("cas_uri")
        citation = _citation_reopen(
            ir=ir,
            pack=pack,
            store_root=store_root,
            quarantined=quarantined,
        )
        trials.append(
            {
                "trial_id": item.trial_id,
                "trial_name": item.trial_name,
                "job_id": item.job_id,
                "job_name": item.job_name,
                "attempt_role": item.attempt_role,
                "cohort_included": item.cohort_included,
                "quality_status": item.quality_status,
                "quality_findings": list(item.quality_findings),
                "verifier_digest": item.verifier_digest,
                "task_digest": item.task_digest,
                "cas_uri": item.cas_uri or None,
                "artifact_cas_uri": artifact_cas_uri,
                "sidecar_status": "present" if ir is not None and pack is not None else "unknown",
                "coverage_gaps": gaps,
                "source_gaps": _item_source_gaps(item, ir),
                "pack": selection,
                "citation_reopen": citation,
            }
        )

    hold_reasons = ["acceptance_enabling_disabled"] if not AUTO_ACCEPTANCE_ENABLED else []
    if any(item.quality_status in _QUARANTINE_STATUSES for item in manifest.items):
        hold_reasons.append("quarantined_input")
    if manifest.accounting.get("unresolved"):
        hold_reasons.append("unresolved_evidence")
    if coverage_counts:
        hold_reasons.append("coverage_gaps")
    if postgres["status"] != "attached":
        hold_reasons.append("postgres_unavailable")
    if jobs_parquet["status"] != "present":
        hold_reasons.append("jobs_parquet_missing")

    unique_source = [uri for uri, count in source_cas.items() if count == 1]
    shared_source = [
        {"cas_uri": uri, "trial_count": count}
        for uri, count in sorted(source_cas.items())
        if count > 1
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": manifest.campaign_id,
        "manifest_id": manifest.manifest_id,
        "manifest_digest": manifest.manifest_digest,
        "readiness": "HOLD",
        "hold_reasons": sorted(set(hold_reasons)),
        "auto_acceptance_enabled": AUTO_ACCEPTANCE_ENABLED,
        "accounting": {
            "planned_specs": manifest.accounting.get("planned_specs"),
            "executions": manifest.accounting.get("executions"),
            "analysis_cohort": manifest.accounting.get("analysis_cohort"),
            "controls": manifest.accounting.get("controls"),
            "quarantine": manifest.accounting.get("quarantine"),
            "retries": manifest.accounting.get("retries"),
            "unresolved": manifest.accounting.get("unresolved"),
            "total_planned_specs": manifest.accounting.get("total_planned_specs"),
            "total_executed_trials": manifest.accounting.get("total_executed_trials"),
            "valid_analysis_ready_trials": manifest.accounting.get("valid_analysis_ready_trials"),
            "quarantined_infrastructure_attempts": manifest.accounting.get(
                "quarantined_infrastructure_attempts"
            ),
            "free_local_controls": manifest.accounting.get("free_local_controls"),
            "unresolved_evidence_count": manifest.accounting.get("unresolved_evidence_count"),
        },
        "coverage_gaps": dict(sorted(coverage_counts.items())),
        "cas_identity": {
            "unique_source_cas_uris": unique_source,
            "shared_source_cas_uris": shared_source,
            "source_cas_uri_count": len(source_cas),
            "job_name_vs_job_id": [
                {
                    "trial_id": item.trial_id,
                    "job_name": item.job_name,
                    "job_id": item.job_id,
                }
                for item in manifest.items
            ],
        },
        "pack": {
            "status": "present" if pack_selected else "unknown",
            "selected_events": pack_selected or None,
            "omitted_events": pack_omitted or None,
            "budget_tokens": budgets or None,
            "sidecar_present": sidecars_present,
            "sidecar_unknown": sidecars_unknown,
        },
        "projections": projections,
        "trials": trials,
    }


def load_cross_campaign_inventory(path: Path) -> dict[str, Any]:
    """Load the committed cross-campaign inventory JSON as a plain dict."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cross-campaign inventory must be a JSON object")
    return payload
