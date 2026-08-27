"""Deterministic per-campaign data-quality operator (Platform-only).

Reports readiness/HOLD, coverage and source gaps, pack selection/omission,
CAS identity, citation reopen availability, and projection availability
without judge calls, IR rebuild, pack rebuild, or CAS restore of
quarantined rows.

Missing PostgreSQL and missing jobs-parquet hive are unavailable/missing
with ``row_count=None``, never coerced to zero.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.database import catalog_availability
from evallab.evidence_store import archive_evidence, load_archive, restore_evidence
from evallab.interpretation.trajectory_acceptance import AUTO_ACCEPTANCE_ENABLED, AcceptanceDecision
from evallab.interpretation.trajectory_hydration import (
    CitationHandle,
    RedactionPolicy,
    hydrate_citation,
)
from evallab.interpretation.trajectory_judgment import MachineJudgment, canonical_json_digest
from evallab.interpretation.trajectory_runtime import (
    CampaignAnalysisItem,
    _load_interpretation_archive_record,
    _pack_payload_structure_errors,
    load_campaign_analysis_manifest,
)

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
            "stray_jobs_parquet_paths": [
                path.relative_to(derived_root).as_posix() for path in stray
            ],
        }
    try:
        row_count = _parquet_row_count(hive)
    except Exception as exc:
        return {
            "status": "invalid",
            "reason": f"jobs_parquet_hive_unreadable:{type(exc).__name__}",
            "row_count": None,
            "stray_jobs_parquet_paths": [
                path.relative_to(derived_root).as_posix() for path in stray
            ],
        }
    return {
        "status": "present",
        "reason": None,
        "row_count": row_count,
        "stray_jobs_parquet_paths": [path.relative_to(derived_root).as_posix() for path in stray],
    }


def _named_parquet_projection(derived_root: Path, relative: str) -> dict[str, Any]:
    path = derived_root / relative
    matches = (
        [path]
        if path.is_file()
        else (sorted(path.parent.glob(path.name)) if path.parent.is_dir() else [])
    )
    if not matches:
        return _unknown_count(status="missing", reason=f"{relative}_absent")
    try:
        row_count = _parquet_row_count(matches)
    except Exception as exc:
        return _unknown_count(
            status="invalid",
            reason=f"{relative}_unreadable:{type(exc).__name__}",
        )
    return {"status": "present", "reason": None, "row_count": row_count}


def _trial_facts_projection(derived_root: Path) -> dict[str, Any]:
    paths = sorted((derived_root / "parquet").glob("job_id=*/trial_id=*/trial_facts.parquet"))
    if not paths:
        return _unknown_count(status="missing", reason="trial_facts_hive_absent")
    try:
        row_count = _parquet_row_count(paths)
    except Exception as exc:
        return _unknown_count(
            status="invalid",
            reason=f"trial_facts_hive_unreadable:{type(exc).__name__}",
        )
    return {"status": "present", "reason": None, "row_count": row_count}


def _add_campaign_projection_joins(
    projections: dict[str, dict[str, Any]],
    *,
    derived_root: Path,
    trials: list[dict[str, Any]],
) -> None:
    """Annotate global projections with current-vs-historical campaign identity joins."""
    cohort = [trial for trial in trials if trial["cohort_included"]]
    trial_ids = {trial["trial_id"] for trial in cohort}
    trial_identities = {(str(trial["job_id"]), str(trial["trial_id"])) for trial in cohort}
    identities = {
        (str(trial["job_id"]), str(trial["trial_id"])): trial["sidecar_identity"]
        for trial in cohort
        if trial.get("sidecar_identity")
    }

    trial_fact_paths = sorted(
        (derived_root / "parquet").glob("job_id=*/trial_id=*/trial_facts.parquet")
    )
    if projections["trial_facts"]["status"] == "present":
        rows = [
            row
            for path in trial_fact_paths
            for row in pq.read_table(path).to_pylist()
            if (str(row.get("job_id")), str(row.get("trial_id"))) in trial_identities
        ]
        present = {(str(row.get("job_id")), str(row.get("trial_id"))) for row in rows}
        missing = trial_identities - present
        projections["trial_facts"].update(
            {
                "row_count_scope": "global",
                "campaign_row_count": len(rows),
                "campaign_trial_count": len(present),
                "expected_current_row_count": len(trial_identities),
                "current_row_count": len(rows),
                "duplicate_current_rows": len(rows) - len(present),
                "missing_campaign_trials": sorted(trial_id for _, trial_id in missing),
                "missing_current_identities": [
                    {"job_id": job_id, "trial_id": trial_id} for job_id, trial_id in sorted(missing)
                ],
            }
        )

    artifact_path = derived_root / "interpretation_artifacts" / "interpretation_artifacts.parquet"
    if projections["interpretation_artifacts"]["status"] == "present":
        rows = [
            row
            for row in pq.read_table(artifact_path).to_pylist()
            if str(row.get("trial_id")) in trial_ids
        ]
        current_keys: set[tuple[str, str, str, str, str, str]] = set()
        for (job_id, trial_id), identity in identities.items():
            for kind in ("ir", "pack", "judgment", "decision", "interpretation"):
                current_keys.add(
                    (
                        job_id,
                        trial_id,
                        kind,
                        str(identity["pack_digest"]),
                        str(identity["judgment_id"]),
                        str(identity["decision_id"]),
                    )
                )

        def _artifact_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
            return (
                str(row.get("job_id")),
                str(row.get("trial_id")),
                str(row.get("kind")),
                str(row.get("pack_digest")),
                str(row.get("judgment_id")),
                str(row.get("decision_id")),
            )

        current_rows = [row for row in rows if _artifact_key(row) in current_keys]
        present_keys = {_artifact_key(row) for row in current_rows}
        foreign_identity_rows = [
            row
            for row in rows
            if (str(row.get("job_id")), str(row.get("trial_id"))) not in trial_identities
        ]
        projections["interpretation_artifacts"].update(
            {
                "row_count_scope": "global",
                "campaign_row_count": len(rows),
                "campaign_trial_count": len(
                    {(str(row.get("job_id")), str(row.get("trial_id"))) for row in rows}
                ),
                "current_row_count": len(current_rows),
                "expected_current_row_count": 5 * len(identities),
                "duplicate_current_rows": len(current_rows) - len(present_keys),
                "historical_row_count": len(rows) - len(current_rows) - len(foreign_identity_rows),
                "orphan_row_count": len(foreign_identity_rows),
                "foreign_campaign_identities": sorted(
                    {
                        (str(row.get("job_id")), str(row.get("trial_id")))
                        for row in foreign_identity_rows
                    }
                ),
                "missing_current_identities": [
                    {
                        "job_id": job_id,
                        "trial_id": trial_id,
                        "kind": kind,
                        "pack_digest": pack_digest,
                        "judgment_id": judgment_id,
                        "decision_id": decision_id,
                    }
                    for (
                        job_id,
                        trial_id,
                        kind,
                        pack_digest,
                        judgment_id,
                        decision_id,
                    ) in sorted(current_keys - present_keys)
                ],
                "kind_counts": dict(sorted(Counter(str(row.get("kind")) for row in rows).items())),
            }
        )

    if projections["machine_judgments"]["status"] == "present":
        expected = {
            (str(identity["judgment_id"]), str(identity["pack_digest"]))
            for identity in identities.values()
        }
        rows = pq.read_table(
            derived_root / "machine_judgments/machine_judgments.parquet"
        ).to_pylist()
        current_rows = [
            row
            for row in rows
            if (str(row.get("judgment_id")), str(row.get("pack_digest"))) in expected
        ]
        present = {
            (str(row.get("judgment_id")), str(row.get("pack_digest"))) for row in current_rows
        }
        projections["machine_judgments"].update(
            {
                "row_count_scope": "global",
                "current_row_count": len(current_rows),
                "expected_current_row_count": len(expected),
                "duplicate_current_rows": len(current_rows) - len(present),
                "missing_current_identities": [
                    {"judgment_id": judgment_id, "pack_digest": pack_digest}
                    for judgment_id, pack_digest in sorted(expected - present)
                ],
            }
        )

    if projections["acceptance_decisions"]["status"] == "present":
        expected = {
            (
                str(identity["decision_id"]),
                str(identity["pack_digest"]),
                json.dumps([identity["judgment_id"]], separators=(",", ":")),
            )
            for identity in identities.values()
        }
        rows = pq.read_table(
            derived_root / "acceptance_decisions/acceptance_decisions.parquet"
        ).to_pylist()
        present_rows: list[tuple[dict[str, Any], tuple[str, str, str]]] = []
        for row in rows:
            try:
                judgment_ids = json.dumps(
                    json.loads(str(row.get("judgment_ids_json"))),
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            key = (
                str(row.get("decision_id")),
                str(row.get("pack_digest")),
                judgment_ids,
            )
            if key in expected:
                present_rows.append((row, key))
        present = {key for _, key in present_rows}
        projections["acceptance_decisions"].update(
            {
                "row_count_scope": "global",
                "current_row_count": len(present_rows),
                "expected_current_row_count": len(expected),
                "duplicate_current_rows": len(present_rows) - len(present),
                "missing_current_identities": [
                    {
                        "decision_id": decision_id,
                        "pack_digest": pack_digest,
                        "judgment_ids_json": judgment_ids,
                    }
                    for decision_id, pack_digest, judgment_ids in sorted(expected - present)
                ],
            }
        )


def _sidecar_search_roots(output_dir: Path, derived_root: Path) -> list[Path]:
    roots: list[Path] = []
    for candidate in (output_dir, *(derived_root / rel for rel in _SIDECAR_SEARCH_RELATIVE)):
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _sidecar_locator(
    path: Path,
    *,
    output_dir: Path,
    derived_root: Path,
) -> str:
    for prefix, root in (("output", output_dir), ("derived", derived_root)):
        try:
            return f"{prefix}/{path.relative_to(root).as_posix()}"
        except ValueError:
            continue
    return f"external/{path.name}"


def _find_trial_sidecar_dirs(trial_id: str, roots: list[Path]) -> list[Path]:
    """Return every candidate generation; never silently collapse append-only versions."""
    candidates: list[Path] = []
    for root in roots:
        trial_dir = root / trial_id
        if not trial_dir.is_dir():
            continue
        if any((trial_dir / name).exists() for name in _SIDECAR_FILES):
            candidates.append(trial_dir)
        for child in sorted(trial_dir.iterdir()):
            if child.is_dir() and any((child / name).exists() for name in _SIDECAR_FILES):
                candidates.append(child)
    return sorted(set(candidates), key=lambda path: str(path))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_sidecar_generation(
    path: Path,
    item: CampaignAnalysisItem,
    *,
    store_root: Path,
    locator: str | None = None,
) -> dict[str, Any]:
    display_path = locator or path.name
    missing = [name for name in _SIDECAR_FILES if not (path / name).is_file()]
    if missing:
        return {
            "status": "partial",
            "reason": [f"missing:{name}" for name in missing],
            "path": display_path,
        }

    payloads = {name: _load_json(path / name) for name in _SIDECAR_FILES}
    invalid_json = [name for name, payload in payloads.items() if payload is None]
    if invalid_json:
        return {
            "status": "invalid",
            "reason": [f"invalid_json:{name}" for name in invalid_json],
            "path": display_path,
        }

    ir = payloads["trajectory_ir.json"]
    pack = payloads["evidence_pack.json"]
    judgment = payloads["machine_judgment.json"]
    decision = payloads["acceptance_decision.json"]
    assert ir is not None and pack is not None and judgment is not None and decision is not None
    errors: list[str] = []
    if canonical_json_digest(
        {key: value for key, value in ir.items() if key != "ir_digest"}
    ) != ir.get("ir_digest"):
        errors.append("invalid_ir_digest")
    if canonical_json_digest(
        {key: value for key, value in pack.items() if key != "pack_digest"}
    ) != pack.get("pack_digest"):
        errors.append("invalid_pack_digest")
    if ir.get("trial_id") != item.trial_id or pack.get("trial_id") != item.trial_id:
        errors.append("foreign_trial_identity")
    if ir.get("job_id") != item.job_id or pack.get("job_id") != item.job_id:
        errors.append("foreign_job_identity")
    ir_sources = ir.get("source_digests")
    if not isinstance(ir_sources, dict):
        errors.append("invalid_ir_source_digests")
    else:
        expected_sources = dict(ir_sources)
        expected_sources["ir_digest"] = ir.get("ir_digest")
        expected_sources["redaction_profile_digest"] = pack.get("redaction_profile_digest")
        if pack.get("source_digests") != expected_sources:
            errors.append("pack_source_digest_mismatch")
    artifact_cas_uri: str | None = None
    try:
        validated_judgment = MachineJudgment.model_validate(judgment)
        validated_decision = AcceptanceDecision.model_validate(decision)
    except Exception as exc:
        errors.append(f"invalid_platform_contract:{type(exc).__name__}")
    else:
        if validated_judgment.pack_id != pack.get("pack_digest"):
            errors.append("judgment_pack_id_mismatch")
        if validated_judgment.pack_digest != pack.get("pack_digest"):
            errors.append("judgment_pack_mismatch")
        if validated_decision.pack_digest != pack.get("pack_digest"):
            errors.append("decision_pack_mismatch")
        if validated_decision.judgment_ids != [validated_judgment.judgment_id]:
            errors.append("decision_judgment_mismatch")
        archive_record = _load_interpretation_archive_record(
            store_root,
            validated_decision.decision_id,
            sidecar_dir=path,
        )
        if archive_record is None:
            errors.append("interpretation_cas_mismatch")
        else:
            artifact_cas_uri = archive_record[0]
    return {
        "status": "invalid" if errors else "valid",
        "reason": sorted(set(errors)) or None,
        "path": display_path,
        "produced_at": decision.get("produced_at"),
        "artifact_cas_uri": artifact_cas_uri,
        "ir": ir,
        "pack": pack,
        "judgment": judgment,
        "decision": decision,
    }


def _field_presence(value: Any) -> str:
    if value is None or value == "" or value == "n/a":
        return "unknown"
    return "present"


def _select_sidecar_generation(
    generations: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    if len(generations) > 1:
        return "multiple", None
    if not generations:
        return "unknown", None
    generation = generations[0]
    if generation["status"] == "valid":
        return "present", generation
    if generation["status"] == "partial":
        return "partial", None
    return "invalid", None


def _cas_record_anti_join(
    store_root: Path,
    items: list[CampaignAnalysisItem],
) -> dict[str, Any]:
    """Validate the current campaign's exact job-identity-to-CAS relation."""
    expected_jobs: dict[str, dict[str, set[str]]] = {}
    for item in items:
        if not item.cas_uri or str(item.cas_uri) == "None":
            continue
        job_id = item.job_id
        expectation = expected_jobs.setdefault(job_id, {"aliases": set(), "uris": set()})
        expectation["aliases"].update({job_id, item.job_name})
        expectation["uris"].add(str(item.cas_uri))
    conflicting_expected_job_ids = sorted(
        job_id
        for job_id, expectation in expected_jobs.items()
        if len(expectation["uris"]) != 1
    )
    alias_to_jobs: dict[str, set[str]] = {}
    for job_id, expectation in expected_jobs.items():
        for alias in expectation["aliases"]:
            if alias:
                alias_to_jobs.setdefault(alias, set()).add(job_id)

    expected_bindings = {
        (job_id, uri)
        for job_id, expectation in expected_jobs.items()
        for uri in expectation["uris"]
    }
    expected_uris = {uri for _, uri in expected_bindings}
    records_root = store_root / "records" / "job"
    matched_bindings: set[tuple[str, str]] = set()
    binding_counts: Counter[tuple[str, str]] = Counter()
    orphan_records: list[dict[str, str]] = []
    invalid_records: list[str] = []
    for path in sorted(records_root.glob("*.json")):
        record_path = path.relative_to(store_root).as_posix()
        path_id = path.stem
        path_jobs = alias_to_jobs.get(path_id, set())
        payload = _load_json(path)
        if payload is None:
            if path_jobs:
                invalid_records.append(record_path)
            continue
        record_id = payload.get("record_id")
        record_jobs = alias_to_jobs.get(record_id, set()) if isinstance(record_id, str) else set()
        if (
            payload.get("kind") != "job"
            or record_id != path_id
            or len(record_jobs) != 1
        ):
            if path_jobs or record_jobs:
                invalid_records.append(record_path)
            continue
        job_id = next(iter(record_jobs))
        uri = payload.get("uri")
        if not isinstance(uri, str):
            invalid_records.append(record_path)
            continue
        binding = (job_id, uri)
        if binding not in expected_bindings:
            orphan_records.append(
                {
                    "job_id": job_id,
                    "record_id": record_id,
                    "uri": uri,
                    "path": record_path,
                }
            )
            continue
        matched_bindings.add(binding)
        binding_counts[binding] += 1

    missing_bindings = expected_bindings - matched_bindings
    missing_uris = sorted({uri for _, uri in missing_bindings})
    missing_record_ids = sorted({job_id for job_id, _ in missing_bindings})
    duplicate_records = [
        {"job_id": job_id, "uri": uri, "record_count": count}
        for (job_id, uri), count in sorted(binding_counts.items())
        if count > 1
    ]
    status = (
        "invalid"
        if (
            conflicting_expected_job_ids
            or invalid_records
            or orphan_records
            or duplicate_records
        )
        else ("missing" if missing_bindings else "present")
    )
    return {
        "status": status,
        "reason": (
            "cas_record_integrity_failure"
            if status == "invalid"
            else ("cas_record_missing" if missing_bindings else None)
        ),
        "expected_uri_count": len(expected_uris),
        "matched_uri_count": len(expected_uris - set(missing_uris)),
        "missing_uris": missing_uris,
        "expected_record_count": len(expected_bindings),
        "matched_record_count": len(matched_bindings),
        "missing_record_ids": missing_record_ids,
        "conflicting_expected_job_ids": conflicting_expected_job_ids,
        "orphan_record_count": len(orphan_records),
        "orphan_records": orphan_records,
        "invalid_record_count": len(invalid_records),
        "invalid_records": invalid_records,
        "duplicate_record_uris": duplicate_records,
    }


def _cas_availability(uri: str | None, store_root: Path) -> dict[str, Any]:
    if not uri:
        return {"status": "unknown", "reason": "cas_uri_absent"}
    try:
        load_archive(store_root, uri)
        with tempfile.TemporaryDirectory() as temporary:
            restore_evidence(store_root, uri, Path(temporary))
    except FileNotFoundError:
        return {"status": "missing", "reason": "cas_blob_absent"}
    except Exception as exc:
        return {"status": "invalid", "reason": f"cas_restore_failed:{type(exc).__name__}"}
    return {"status": "present", "reason": None}


def _pack_selection(
    pack: dict[str, Any] | None,
    ir: dict[str, Any] | None,
) -> dict[str, Any]:
    unknown = {
        "status": "unknown",
        "reason": "sidecar_absent",
        "selected_events": None,
        "omitted_events": None,
        "accounted_events": None,
        "ir_events": None,
        "omitted_ranges": None,
        "omitted_ranges_verified": None,
        "budget_tokens": None,
        "overflow_reason": None,
        "is_model_callable": None,
    }
    if pack is None or ir is None:
        return unknown
    selected_windows = pack.get("selected_windows")
    omitted_ranges = pack.get("omitted_ranges")
    events = ir.get("events")
    if (
        not isinstance(selected_windows, list)
        or not isinstance(omitted_ranges, list)
        or not isinstance(events, list)
    ):
        return {**unknown, "status": "invalid", "reason": "invalid_pack_collection_shape"}

    event_by_id: dict[str, dict[str, Any]] = {}
    integrity_errors: list[str] = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            integrity_errors.append("invalid_ir_event")
            continue
        event_id = str(event["event_id"])
        if event_id in event_by_id:
            integrity_errors.append("duplicate_ir_event_id")
        event_by_id[event_id] = event

    selected_ids: list[str] = []
    for window in selected_windows:
        if not isinstance(window, dict):
            integrity_errors.append("invalid_selected_window")
            continue
        window_events = window.get("events")
        event_count = window.get("event_count")
        if not isinstance(window_events, list):
            integrity_errors.append("invalid_selected_events")
            continue
        if (
            not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count != len(window_events)
        ):
            integrity_errors.append("selected_event_count_mismatch")
        for event_payload in window_events:
            if not isinstance(event_payload, dict):
                integrity_errors.append("invalid_selected_event")
                continue
            event_id = event_payload.get("event_id")
            if not isinstance(event_id, str) or event_id not in event_by_id:
                integrity_errors.append("selected_event_missing_from_ir")
                continue
            selected_ids.append(event_id)
            base_payload = {
                key: value for key, value in event_payload.items() if key != "hydrated_content"
            }
            if canonical_json_digest(base_payload) != canonical_json_digest(event_by_id[event_id]):
                integrity_errors.append("selected_event_payload_mismatch")
            if not isinstance(event_payload.get("hydrated_content"), str):
                integrity_errors.append("selected_event_hydration_missing")

    omitted_ids: list[str] = []
    omitted_verified = 0
    for omitted_range in omitted_ranges:
        if not isinstance(omitted_range, dict):
            integrity_errors.append("invalid_omitted_range")
            continue
        event_ids = omitted_range.get("event_ids")
        event_count = omitted_range.get("event_count")
        expected = omitted_range.get("omitted_content_digest")
        if (
            not isinstance(event_ids, list)
            or not event_ids
            or not all(isinstance(event_id, str) for event_id in event_ids)
        ):
            integrity_errors.append("missing_event_ids")
            continue
        if (
            not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count != len(event_ids)
        ):
            integrity_errors.append("omitted_event_count_mismatch")
        if any(event_id not in event_by_id for event_id in event_ids):
            integrity_errors.append("missing_ir_event")
            continue
        omitted_ids.extend(event_ids)
        actual = canonical_json_digest([event_by_id[event_id] for event_id in event_ids])
        if not isinstance(expected, str) or actual != expected:
            integrity_errors.append("omitted_content_digest_mismatch")
            continue
        omitted_verified += 1

    if len(selected_ids) != len(set(selected_ids)):
        integrity_errors.append("duplicate_selected_event")
    if len(omitted_ids) != len(set(omitted_ids)):
        integrity_errors.append("duplicate_omitted_event")
    if set(selected_ids) & set(omitted_ids):
        integrity_errors.append("selected_omitted_overlap")
    if set(selected_ids) | set(omitted_ids) != set(event_by_id):
        integrity_errors.append("pack_event_coverage_mismatch")
    integrity_errors.extend(_pack_payload_structure_errors(ir, pack))
    reason = sorted(set(integrity_errors)) or None
    return {
        "status": "invalid" if reason else "present",
        "reason": reason,
        "selected_events": len(selected_ids),
        "omitted_events": len(omitted_ids),
        "accounted_events": len(selected_ids) + len(omitted_ids),
        "ir_events": len(events),
        "omitted_ranges": len(omitted_ranges),
        "omitted_ranges_verified": omitted_verified,
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


def _platform_citation_ids(handles: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    for payload in handles:
        try:
            handle = CitationHandle(**payload)
        except (TypeError, ValueError):
            continue
        identities.add(canonical_json_digest(handle.to_dict()))
    return identities


def _citation_reopen(
    *,
    ir: dict[str, Any] | None,
    pack: dict[str, Any] | None,
    store_root: Path,
    quarantined: bool,
) -> dict[str, Any]:
    empty = {
        "available": None,
        "unavailable": None,
        "unreopenable": None,
        "integrity_failures": None,
        "handle_count": None,
        "reason_counts": None,
    }
    if quarantined:
        return {"status": "skipped", "reason": "quarantined_input", **empty}
    if ir is None or pack is None:
        return {"status": "unknown", "reason": "sidecars_absent", **empty}

    handles = _collect_handles(ir, pack)
    try:
        policy = RedactionPolicy.from_pack_config(
            pack.get("redaction_policy_config"),
            pack.get("redaction_profile_digest"),
        )
    except ValueError:
        return {
            "status": "invalid",
            "reason": "invalid_redaction_policy",
            "available": 0,
            "unavailable": len(handles),
            "unreopenable": len(handles),
            "integrity_failures": len(handles),
            "handle_count": len(handles),
            "reason_counts": {"invalid_redaction_policy": len(handles)},
        }
    if not handles:
        return {"status": "unknown", "reason": "citation_handles_absent", **empty}
    selected_hydrated: dict[str, list[Any]] = {}
    for window in pack.get("selected_windows") or []:
        if not isinstance(window, dict):
            continue
        for event in window.get("events") or []:
            if not isinstance(event, dict) or not isinstance(event.get("source_citation"), dict):
                continue
            for identity in _platform_citation_ids([event["source_citation"]]):
                selected_hydrated.setdefault(identity, []).append(event.get("hydrated_content"))
    available = 0
    unavailable = 0
    unreopenable = 0
    reasons: Counter[str] = Counter()
    integrity_failures = 0
    source_digests = ir.get("source_digests")
    if not isinstance(source_digests, dict):
        return {
            "status": "invalid",
            "reason": "invalid_ir_source_digests",
            "available": 0,
            "unavailable": len(handles),
            "unreopenable": len(handles),
            "integrity_failures": len(handles),
            "handle_count": len(handles),
            "reason_counts": {"invalid_ir_source_digests": len(handles)},
        }
    expected_cas = str(source_digests.get("cas_uri") or "")
    expected_source = str(source_digests.get("source_sha256") or "")
    temporary_restore: tempfile.TemporaryDirectory[str] | None = None
    restored_root: Path | None = None
    if expected_cas:
        try:
            temporary_restore = tempfile.TemporaryDirectory()
            restored_root = restore_evidence(
                store_root,
                expected_cas,
                Path(temporary_restore.name),
            )
        except Exception as exc:
            if temporary_restore is not None:
                temporary_restore.cleanup()
            return {
                "status": "invalid",
                "reason": "citation_source_cas_unavailable",
                "available": 0,
                "unavailable": len(handles),
                "unreopenable": len(handles),
                "integrity_failures": len(handles),
                "handle_count": len(handles),
                "reason_counts": {f"citation_source_cas_error:{type(exc).__name__}": len(handles)},
            }
    allowed_targets = {
        "step",
        "tool_call",
        "observation",
        "stderr",
        "stdout",
        "arguments",
        "file",
    }
    required_fields = {"citation_id", "source_path", "source_sha256", "target_type"}
    for payload in handles:
        if (
            any(field not in payload for field in required_fields)
            or payload.get("target_type") not in allowed_targets
        ):
            reasons["invalid_citation_locator"] += 1
            unavailable += 1
            unreopenable += 1
            continue
        try:
            handle = CitationHandle(**payload)
            handle_cas = handle.raw_cas_uri or handle.cas_uri or ""
            if handle.availability != "available":
                reasons["citation_marked_unavailable"] += 1
                unavailable += 1
                unreopenable += 1
                continue
            if not expected_cas or not handle_cas:
                reasons["missing_cas_binding"] += 1
                unavailable += 1
                unreopenable += 1
                continue
            if handle_cas != expected_cas:
                reasons["citation_cas_mismatch"] += 1
                unavailable += 1
                integrity_failures += 1
                continue
            if not expected_source or not handle.source_sha256:
                reasons["missing_source_digest"] += 1
                unavailable += 1
                unreopenable += 1
                continue
            if handle.source_sha256 != expected_source:
                reasons["citation_source_digest_mismatch"] += 1
                unavailable += 1
                integrity_failures += 1
                continue
            if restored_root is None:
                reasons["source_archive_unavailable"] += 1
                unavailable += 1
                unreopenable += 1
                continue
            source_path = (restored_root / handle.source_path).resolve()
            try:
                source_path.relative_to(restored_root)
            except ValueError:
                reasons["citation_path_escapes_archive"] += 1
                unavailable += 1
                integrity_failures += 1
                continue
            if not source_path.is_file():
                reasons["citation_source_member_missing"] += 1
                unavailable += 1
                unreopenable += 1
                continue
            actual_source = f"sha256:{hashlib.sha256(source_path.read_bytes()).hexdigest()}"
            if actual_source != expected_source:
                reasons["source_archive_digest_mismatch"] += 1
                unavailable += 1
                integrity_failures += 1
                continue
            hydrated = hydrate_citation(handle, repo_root=store_root, policy=policy)
            platform_id = canonical_json_digest(handle.to_dict())
            selected_contents = selected_hydrated.get(platform_id, [])
            if any(content != hydrated.redacted_content for content in selected_contents):
                reasons["selected_hydrated_content_mismatch"] += 1
                unavailable += 1
                integrity_failures += 1
                continue
            limitation = hydrated.redaction_metadata.get("limitation_reason")
            digest_mismatch = bool(hydrated.redaction_metadata.get("content_digest_mismatch"))
            if digest_mismatch:
                reasons["content_digest_mismatch"] += 1
                unavailable += 1
                integrity_failures += 1
            elif limitation:
                reasons[str(limitation)] += 1
                unavailable += 1
                if limitation in {
                    "omitted_unreopenable",
                    "source_missing",
                    "cited_element_not_found",
                    "cas_member_not_found",
                    "cas_archive_not_found",
                }:
                    unreopenable += 1
            else:
                available += 1
        except Exception as exc:
            reasons[f"citation_error:{type(exc).__name__}"] += 1
            unavailable += 1
            unreopenable += 1
    if temporary_restore is not None:
        temporary_restore.cleanup()
    if integrity_failures or unreopenable:
        status = "invalid"
    elif unavailable:
        status = "degraded"
    else:
        status = "present"
    return {
        "status": status,
        "reason": None if status == "present" else "citation_reopen_failures",
        "available": available,
        "unavailable": unavailable,
        "unreopenable": unreopenable,
        "integrity_failures": integrity_failures,
        "handle_count": len(handles),
        "reason_counts": dict(sorted(reasons.items())),
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
    item: CampaignAnalysisItem,
    ir: dict[str, Any] | None,
    pack: dict[str, Any] | None,
    *,
    selection: dict[str, Any],
    citation: dict[str, Any],
    cas: dict[str, Any],
    sidecar_status: str,
) -> list[str]:
    gaps: list[str] = []
    if item.quality_status == "warn":
        gaps.append("quality_warning")
    if item.quality_status not in {
        "pass",
        "warn",
        "no_atif",
        "quarantine",
        "fail",
        "quarantined",
    }:
        gaps.append("quality_status_unknown")
    findings = [str(finding) for finding in item.quality_findings]
    if any("UNPAIRED" in finding for finding in findings):
        gaps.append("ATIF_UNPAIRED_TOOL_CALL")
    if ir is not None:
        if ir.get("linkage_coverage") in {"degraded", "unlinked"}:
            gaps.append("unpaired_linkage")
        unpaired = ir.get("unpaired_tool_calls_count")
        if isinstance(unpaired, int) and not isinstance(unpaired, bool) and unpaired > 0:
            gaps.append("ATIF_UNPAIRED_TOOL_CALL")
    if pack is not None and not pack.get("is_model_callable", True):
        gaps.append("pack_incomplete")
    if selection["status"] == "invalid":
        gaps.append("pack_integrity_invalid")
    if citation["status"] in {"degraded", "invalid", "unknown"}:
        gaps.append("citation_reopen_unavailable")
    if cas["status"] != "present":
        gaps.append(f"source_cas_{cas['status']}")
    if sidecar_status in {"unknown", "invalid", "partial", "multiple"}:
        gaps.append(f"sidecar_{sidecar_status}")
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
    repo_root = repo_root.resolve()
    inventory_path = inventory_path.resolve()
    store_root = store_root.resolve()
    output_dir = output_dir.resolve()
    derived = (derived_root or output_dir.parent).resolve()
    raw_inventory = _load_json(inventory_path)
    if raw_inventory is None:
        raise ValueError(f"campaign inventory must be a JSON object: {inventory_path}")
    inventory_digest = canonical_json_digest(raw_inventory)
    try:
        inventory_locator = inventory_path.relative_to(repo_root).as_posix()
    except ValueError:
        inventory_locator = f"external/{inventory_path.name}"
    manifest = load_campaign_analysis_manifest(inventory_path)
    sidecar_roots = _sidecar_search_roots(output_dir, derived)
    cas_record_joins = _cas_record_anti_join(store_root, manifest.items)

    postgres = catalog_availability(database_url)
    if database_url and postgres["status"] == "unavailable":
        reason_type = str(postgres.get("reason") or "unknown").partition(":")[0]
        postgres["reason"] = f"database_connection_failed:{reason_type}"
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
    trial_id_counts = Counter(item.trial_id for item in manifest.items)
    duplicate_trial_ids = sorted(
        trial_id for trial_id, count in trial_id_counts.items() if count > 1
    )

    trials: list[dict[str, Any]] = []
    coverage_by_scope: dict[str, Counter[str]] = {
        "analysis_cohort": Counter(),
        "excluded": Counter(),
    }
    policy_by_scope: dict[str, Counter[str]] = {
        "analysis_cohort": Counter(),
        "excluded": Counter(),
    }
    source_cas: Counter[str] = Counter()
    cas_statuses: Counter[str] = Counter()
    cas_cache: dict[str | None, dict[str, Any]] = {}
    citation_totals: Counter[str] = Counter()
    pack_selected: list[int] = []
    pack_omitted: list[int] = []
    budgets: list[int] = []
    sidecar_statuses: Counter[str] = Counter()

    for item in manifest.items:
        quarantined = item.quality_status in _QUARANTINE_STATUSES
        generations: list[dict[str, Any]] = []
        selected_generation: dict[str, Any] | None = None
        if quarantined:
            sidecar_status = "skipped"
        else:
            candidates = _find_trial_sidecar_dirs(item.trial_id, sidecar_roots)
            generations = [
                _load_sidecar_generation(
                    path,
                    item,
                    store_root=store_root,
                    locator=_sidecar_locator(
                        path,
                        output_dir=output_dir,
                        derived_root=derived,
                    ),
                )
                for path in candidates
            ]
            sidecar_status, selected_generation = _select_sidecar_generation(generations)
        sidecar_statuses[sidecar_status] += 1

        ir = selected_generation.get("ir") if selected_generation else None
        pack = selected_generation.get("pack") if selected_generation else None
        judgment = selected_generation.get("judgment") if selected_generation else None
        selection = _pack_selection(pack, ir)
        if selection["status"] == "present":
            pack_selected.append(selection["selected_events"])
            pack_omitted.append(selection["omitted_events"])
            if isinstance(selection["budget_tokens"], int):
                budgets.append(selection["budget_tokens"])

        cas_uri = item.cas_uri if item.cas_uri and item.cas_uri != "None" else None
        if quarantined:
            cas = {"status": "skipped", "reason": "quarantined_input"}
        else:
            if cas_uri not in cas_cache:
                cas_cache[cas_uri] = _cas_availability(cas_uri, store_root)
            cas = cas_cache[cas_uri]
        cas_statuses[cas["status"]] += 1
        if cas_uri:
            source_cas[cas_uri] += 1

        citation = _citation_reopen(
            ir=ir,
            pack=pack,
            store_root=store_root,
            quarantined=quarantined,
        )
        known_citation_ids = (
            _platform_citation_ids(_collect_handles(ir, pack))
            if ir is not None and pack is not None
            else set()
        )
        judgment_citations = judgment.get("citation_ids") if isinstance(judgment, dict) else []
        if not isinstance(judgment_citations, list):
            judgment_citations = []
            citation["status"] = "invalid"
            citation["reason"] = "invalid_judgment_citations"
        unresolved_judgment_citations = sorted(
            citation_id
            for citation_id in judgment_citations
            if not isinstance(citation_id, str) or citation_id not in known_citation_ids
        )
        citation["judgment_citations"] = len(judgment_citations)
        citation["unresolved_judgment_citations"] = unresolved_judgment_citations
        if unresolved_judgment_citations:
            citation["status"] = "invalid"
            citation["reason"] = "unresolved_judgment_citations"
            citation["integrity_failures"] = int(citation["integrity_failures"] or 0) + len(
                unresolved_judgment_citations
            )
        for key in ("available", "unavailable", "unreopenable", "integrity_failures"):
            value = citation.get(key)
            if isinstance(value, int):
                citation_totals[key] += value

        gaps = _item_coverage(
            item,
            ir,
            pack,
            selection=selection,
            citation=citation,
            cas=cas,
            sidecar_status=sidecar_status,
        )
        pack_sources = pack.get("source_digests") if isinstance(pack, dict) else None
        pack_source_cas_uri = (
            pack_sources.get("cas_uri") if isinstance(pack_sources, dict) else None
        )
        if pack_source_cas_uri and cas_uri and pack_source_cas_uri != cas_uri:
            gaps.append("manifest_pack_cas_mismatch")
        gaps = sorted(set(gaps))
        scope = "analysis_cohort" if item.cohort_included else "excluded"
        for gap in gaps:
            coverage_by_scope[scope][gap] += 1
        if not AUTO_ACCEPTANCE_ENABLED:
            policy_by_scope[scope]["judge_execution_disabled"] += 1

        generation_summaries = [
            {key: generation.get(key) for key in ("status", "reason", "path", "produced_at")}
            for generation in generations
        ]
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
                "cas_uri": cas_uri,
                "cas_availability": cas,
                "pack_source_cas_uri": pack_source_cas_uri,
                "sidecar_status": sidecar_status,
                "sidecar_generation_count": len(generations),
                "selected_sidecar_path": (
                    selected_generation.get("path") if selected_generation else None
                ),
                "sidecar_generations": generation_summaries,
                "sidecar_identity": (
                    {
                        "job_id": item.job_id,
                        "pack_digest": pack.get("pack_digest"),
                        "judgment_id": judgment.get("judgment_id"),
                        "decision_id": selected_generation["decision"].get("decision_id"),
                    }
                    if isinstance(pack, dict)
                    and isinstance(judgment, dict)
                    and selected_generation is not None
                    and isinstance(selected_generation.get("decision"), dict)
                    else None
                ),
                "coverage_gaps": gaps,
                "policy_gaps": [] if AUTO_ACCEPTANCE_ENABLED else ["judge_execution_disabled"],
                "source_gaps": _item_source_gaps(item, ir),
                "pack": selection,
                "citation_reopen": citation,
            }
        )
    _add_campaign_projection_joins(
        projections,
        derived_root=derived,
        trials=trials,
    )

    cohort_coverage = coverage_by_scope["analysis_cohort"]
    hold_reasons = ["acceptance_enabling_disabled"] if not AUTO_ACCEPTANCE_ENABLED else []
    if duplicate_trial_ids:
        hold_reasons.append("manifest_duplicate_trial_id")
    if any(item.quality_status in _QUARANTINE_STATUSES for item in manifest.items):
        hold_reasons.append("quarantined_input")
    if manifest.accounting.get("unresolved"):
        hold_reasons.append("unresolved_evidence")
    if cohort_coverage:
        hold_reasons.append("coverage_gaps")
    if sidecar_statuses["multiple"]:
        hold_reasons.append("sidecar_generation_ambiguity")
    if citation_totals["unavailable"] or citation_totals["integrity_failures"]:
        hold_reasons.append("citation_reopen_incomplete")
    if any(projection.get("missing_current_identities") for projection in projections.values()):
        hold_reasons.append("projection_identity_join_incomplete")
    if any(
        int(projection.get("duplicate_current_rows") or 0) > 0
        for projection in projections.values()
    ):
        hold_reasons.append("projection_duplicate_identities")
    if any(
        trial["cohort_included"] and trial["cas_availability"]["status"] != "present"
        for trial in trials
    ):
        hold_reasons.append("source_cas_unavailable")
    if cas_record_joins["status"] != "present" or cas_record_joins["missing_uris"]:
        hold_reasons.append("cas_record_join_incomplete")
    if any(int(projection.get("orphan_row_count") or 0) > 0 for projection in projections.values()):
        hold_reasons.append("projection_orphan_rows")
    if postgres["status"] != "attached":
        hold_reasons.append("postgres_unavailable")
    if jobs_parquet["status"] != "present":
        hold_reasons.append("jobs_parquet_missing")
    for name in (
        "trial_facts",
        "interpretation_artifacts",
        "machine_judgments",
        "acceptance_decisions",
    ):
        if projections[name]["status"] != "present":
            hold_reasons.append(f"{name}_{projections[name]['status']}")

    unique_source = [uri for uri, count in source_cas.items() if count == 1]
    shared_source = [
        {"cas_uri": uri, "trial_count": count}
        for uri, count in sorted(source_cas.items())
        if count > 1
    ]
    cohort_count = sum(1 for item in manifest.items if item.cohort_included)
    complete_pack_count = sum(
        1 for trial in trials if trial["cohort_included"] and trial["pack"]["status"] == "present"
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": manifest.campaign_id,
        "manifest_id": manifest.manifest_id,
        "manifest_digest": manifest.manifest_digest,
        "source_inventory": {
            "path": inventory_locator,
            "digest": inventory_digest,
            "commit_sha": raw_inventory.get("commit_sha"),
            "source_campaign_manifest_digest": raw_inventory.get("source_campaign_manifest_digest"),
        },
        "readiness": "HOLD" if hold_reasons else "READY",
        "hold_reasons": sorted(set(hold_reasons)),
        "auto_acceptance_enabled": AUTO_ACCEPTANCE_ENABLED,
        "manifest_duplicate_trial_ids": duplicate_trial_ids,
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
        "coverage_gaps": dict(sorted(cohort_coverage.items())),
        "coverage_gaps_by_scope": {
            scope: dict(sorted(counts.items())) for scope, counts in coverage_by_scope.items()
        },
        "policy_gaps_by_scope": {
            scope: dict(sorted(counts.items())) for scope, counts in policy_by_scope.items()
        },
        "citation_reopen": dict(sorted(citation_totals.items())),
        "cas_identity": {
            "unique_source_cas_uris": unique_source,
            "record_anti_join": cas_record_joins,
            "shared_source_cas_uris": shared_source,
            "source_cas_uri_count": len(source_cas),
            "availability_counts": dict(sorted(cas_statuses.items())),
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
            "status": "present" if complete_pack_count == cohort_count else "incomplete",
            "selected_events": pack_selected or None,
            "omitted_events": pack_omitted or None,
            "budget_tokens": budgets or None,
            "complete_cohort_packs": complete_pack_count,
            "expected_cohort_packs": cohort_count,
            "sidecar_status_counts": dict(sorted(sidecar_statuses.items())),
        },
        "projections": projections,
        "trials": trials,
    }
    report_id = canonical_json_digest(report)
    report["report_id"] = report_id
    report_dir = output_dir / "campaigns" / report_id.removeprefix("sha256:")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "campaign_data_quality_report.json"
    report_text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(report_text, encoding="utf-8")
    temporary.replace(report_path)
    with tempfile.TemporaryDirectory() as staging:
        archive_source = Path(staging) / "report"
        archive_source.mkdir()
        (archive_source / report_path.name).write_text(report_text, encoding="utf-8")
        archive = archive_evidence(
            archive_source,
            store_root,
            record_id=report_id,
            kind="campaign-data-quality",
        )
    result = dict(report)
    result["report_path"] = str(report_path)
    result["report_cas_uri"] = archive.uri
    if _cas_availability(archive.uri, store_root)["status"] != "present":
        raise ValueError("campaign data-quality report archive integrity verification failed")
    return result


def load_cross_campaign_inventory(path: Path) -> dict[str, Any]:
    """Load the committed cross-campaign inventory JSON as a plain dict."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cross-campaign inventory must be a JSON object")
    return payload
