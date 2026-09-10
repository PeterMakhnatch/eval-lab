"""Harness Mechanics Lab: Observational reporting over retained trajectories.

This module provides report generation and human-readable text rendering for the
Harness Mechanics Lab. It aggregates diagnostic observations, hypotheses, and unknowns
across analyzed trajectory records, while faithfully preserving unsupported and error
records in all denominators.

Strict non-claims:
- No pass-rate or causal ranking from unmatched historical data.
- Zero observations for a mechanism indicate absence of retained markers,
  NOT evidence of absence or harmlessness.
- Deterministic ordering with no volatile timestamps.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1
ANALYSIS_KIND = "harness-mechanics-observational"
VALID_EVIDENCE_KINDS = frozenset({"historical", "fixture", "model-run"})
VALID_STATUSES = frozenset({"analyzed", "unsupported", "error"})
VALID_OBSERVATION_EVIDENCE_KINDS = frozenset({"observed", "hypothesis"})
STANDARD_MECHANISMS: tuple[str, ...] = ("stopping", "clipping", "shell")
STANDARD_MECHANISM_SET = frozenset(STANDARD_MECHANISMS)

ALLOWED_RECORD_KEYS = frozenset({"source", "identity", "status", "diagnostics", "warnings"})
ALLOWED_SOURCE_KEYS = frozenset({"path", "sha256", "size_bytes"})
ALLOWED_IDENTITY_KEYS = frozenset({"session_id", "model", "harness"})
ALLOWED_DIAGNOSTIC_KEYS = frozenset({"mechanism", "observations", "unknowns"})
ALLOWED_OBSERVATION_KEYS = frozenset(
    {"code", "step_id", "tool_call_id", "locator", "evidence_kind", "summary"}
)


def _validate_observation(
    obs: Any,
    mechanism: str,
    obs_index: int,
    record_path: str,
    record_index: int,
) -> None:
    """Validate shape and types of an individual diagnostic observation."""
    if not isinstance(obs, dict):
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) must be a dict, got {type(obs).__name__}"
        )
    if set(obs.keys()) != ALLOWED_OBSERVATION_KEYS:
        extra = set(obs.keys()) - ALLOWED_OBSERVATION_KEYS
        missing = ALLOWED_OBSERVATION_KEYS - set(obs.keys())
        if missing:
            raise ValueError(
                f"Observation {obs_index} for {mechanism!r} in record {record_index} "
                f"({record_path}) missing required key(s): {sorted(missing)}"
            )
        if extra:
            raise ValueError(
                f"Observation {obs_index} for {mechanism!r} in record {record_index} "
                f"({record_path}) contains extraneous key(s): {sorted(extra)}"
            )

    code = obs["code"]
    if not isinstance(code, str) or not code.strip():
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) 'code' must be a non-empty string"
        )

    step_id = obs["step_id"]
    if step_id is not None and (
        isinstance(step_id, bool) or not isinstance(step_id, int) or step_id < 0
    ):
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) 'step_id' must be a non-negative int or None, got {step_id!r}"
        )

    tool_call_id = obs["tool_call_id"]
    if tool_call_id is not None and (
        isinstance(tool_call_id, bool) or not isinstance(tool_call_id, str)
    ):
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) 'tool_call_id' must be a string or None, got {tool_call_id!r}"
        )

    locator = obs["locator"]
    if not isinstance(locator, str) or not locator.strip():
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) 'locator' must be a non-empty string"
        )

    ev_kind = obs["evidence_kind"]
    if not isinstance(ev_kind, str) or ev_kind not in VALID_OBSERVATION_EVIDENCE_KINDS:
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) has invalid evidence_kind: {ev_kind!r}. "
            f"Expected one of: {sorted(VALID_OBSERVATION_EVIDENCE_KINDS)}"
        )

    summary = obs["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(
            f"Observation {obs_index} for {mechanism!r} in record {record_index} "
            f"({record_path}) 'summary' must be a non-empty string"
        )


def _validate_diagnostic(
    diag: Any,
    expected_mechanism: str,
    record_path: str,
    record_index: int,
) -> None:
    """Validate shape and types of a mechanism diagnostic result."""
    if not isinstance(diag, dict):
        raise ValueError(
            f"Diagnostic for {expected_mechanism!r} in record {record_index} "
            f"({record_path}) must be a dict, got {type(diag).__name__}"
        )
    if set(diag.keys()) != ALLOWED_DIAGNOSTIC_KEYS:
        extra = set(diag.keys()) - ALLOWED_DIAGNOSTIC_KEYS
        missing = ALLOWED_DIAGNOSTIC_KEYS - set(diag.keys())
        if missing:
            raise ValueError(
                f"Diagnostic for {expected_mechanism!r} in record {record_index} "
                f"({record_path}) missing required key(s): {sorted(missing)}"
            )
        if extra:
            raise ValueError(
                f"Diagnostic for {expected_mechanism!r} in record {record_index} "
                f"({record_path}) contains extraneous key(s): {sorted(extra)}"
            )

    mechanism = diag["mechanism"]
    if not isinstance(mechanism, str) or mechanism != expected_mechanism:
        raise ValueError(
            f"Diagnostic mechanism mismatch in record {record_index} ({record_path}): "
            f"expected {expected_mechanism!r}, got {mechanism!r}"
        )

    unknowns = diag["unknowns"]
    if not isinstance(unknowns, list):
        raise ValueError(
            f"Diagnostic {expected_mechanism!r} 'unknowns' in record {record_index} "
            f"({record_path}) must be a list, got {type(unknowns).__name__}"
        )
    for u_idx, unk in enumerate(unknowns):
        if not isinstance(unk, str) or not unk.strip():
            raise ValueError(
                f"Diagnostic {expected_mechanism!r} unknown at index {u_idx} in record "
                f"{record_index} ({record_path}) must be a non-empty string"
            )

    observations = diag["observations"]
    if not isinstance(observations, list):
        raise ValueError(
            f"Diagnostic {expected_mechanism!r} 'observations' in record {record_index} "
            f"({record_path}) must be a list, got {type(observations).__name__}"
        )
    for obs_idx, obs in enumerate(observations):
        _validate_observation(obs, expected_mechanism, obs_idx, record_path, record_index)


def _validate_record(record: Any, index: int) -> None:
    """Validate the schema and internal types of a single input record."""
    if not isinstance(record, dict):
        raise ValueError(f"Record at index {index} must be a dict, got {type(record).__name__}")

    if set(record.keys()) != ALLOWED_RECORD_KEYS:
        extra = set(record.keys()) - ALLOWED_RECORD_KEYS
        missing = ALLOWED_RECORD_KEYS - set(record.keys())
        if missing:
            raise ValueError(f"Record at index {index} missing required key(s): {sorted(missing)}")
        if extra:
            raise ValueError(f"Record at index {index} contains extraneous key(s): {sorted(extra)}")

    # Validate source
    source = record["source"]
    if not isinstance(source, dict):
        raise ValueError(
            f"Record at index {index} 'source' must be a dict, got {type(source).__name__}"
        )
    if set(source.keys()) != ALLOWED_SOURCE_KEYS:
        extra = set(source.keys()) - ALLOWED_SOURCE_KEYS
        missing = ALLOWED_SOURCE_KEYS - set(source.keys())
        if missing:
            raise ValueError(
                f"Record at index {index} 'source' missing required key(s): {sorted(missing)}"
            )
        if extra:
            raise ValueError(
                f"Record at index {index} 'source' contains extraneous key(s): {sorted(extra)}"
            )

    if not isinstance(source["path"], str) or not source["path"].strip():
        raise ValueError(f"Record at index {index} 'source.path' must be a non-empty string")
    sha256 = source["sha256"]
    if sha256 is not None and (isinstance(sha256, bool) or not isinstance(sha256, str)):
        raise ValueError(
            f"Record at index {index} 'source.sha256' must be str or None, got {type(sha256).__name__}"
        )
    size = source["size_bytes"]
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise ValueError(
            f"Record at index {index} 'source.size_bytes' must be non-negative int or None, got {size!r}"
        )

    # Validate identity
    identity = record["identity"]
    if not isinstance(identity, dict):
        raise ValueError(
            f"Record at index {index} 'identity' must be a dict, got {type(identity).__name__}"
        )
    if set(identity.keys()) != ALLOWED_IDENTITY_KEYS:
        extra = set(identity.keys()) - ALLOWED_IDENTITY_KEYS
        missing = ALLOWED_IDENTITY_KEYS - set(identity.keys())
        if missing:
            raise ValueError(
                f"Record at index {index} 'identity' missing required key(s): {sorted(missing)}"
            )
        if extra:
            raise ValueError(
                f"Record at index {index} 'identity' contains extraneous key(s): {sorted(extra)}"
            )

    for id_key in ("session_id", "model", "harness"):
        val = identity[id_key]
        if val is not None and (isinstance(val, bool) or not isinstance(val, str)):
            raise ValueError(
                f"Record at index {index} 'identity.{id_key}' must be str or None, got {type(val).__name__}"
            )

    # Validate status (reject unhashable or invalid types safely with ValueError)
    status = record["status"]
    if not isinstance(status, str) or status not in VALID_STATUSES:
        raise ValueError(
            f"Record at index {index} ({source['path']}) has invalid status: {status!r}. "
            f"Expected one of: {sorted(VALID_STATUSES)}"
        )

    # Validate warnings
    warnings = record["warnings"]
    if not isinstance(warnings, list):
        raise ValueError(
            f"Record at index {index} 'warnings' must be a list, got {type(warnings).__name__}"
        )
    for w_idx, warn in enumerate(warnings):
        if not isinstance(warn, str) or isinstance(warn, bool):
            raise ValueError(
                f"Record at index {index} warning at index {w_idx} must be a string, got {type(warn).__name__}"
            )

    # Validate diagnostics
    diagnostics = record["diagnostics"]
    if not isinstance(diagnostics, dict):
        raise ValueError(
            f"Record at index {index} 'diagnostics' must be a dict, got {type(diagnostics).__name__}"
        )

    if status in ("unsupported", "error"):
        if diagnostics:
            raise ValueError(
                f"Record at index {index} with status {status!r} must have empty diagnostics, "
                f"got {list(diagnostics.keys())}"
            )
    elif status == "analyzed":
        if set(diagnostics.keys()) != STANDARD_MECHANISM_SET:
            extra = set(diagnostics.keys()) - STANDARD_MECHANISM_SET
            missing = STANDARD_MECHANISM_SET - set(diagnostics.keys())
            if missing:
                raise ValueError(
                    f"Analyzed record at index {index} ({source['path']}) missing standard "
                    f"diagnostic mechanism: {sorted(missing)}"
                )
            if extra:
                raise ValueError(
                    f"Analyzed record at index {index} ({source['path']}) contains unsupported "
                    f"mechanism(s): {sorted(extra)}"
                )
        for mech_key in STANDARD_MECHANISMS:
            _validate_diagnostic(diagnostics[mech_key], mech_key, source["path"], index)


def _record_sort_key(record: dict[str, Any]) -> tuple[str, str, str]:
    """Deterministic sort key for records by source path, status, and sha256."""
    src = record.get("source") if isinstance(record, dict) else None
    path = str(src.get("path", "") if isinstance(src, dict) else "")
    status = str(record.get("status", "") if isinstance(record, dict) else "")
    sha256 = str(src.get("sha256") or "" if isinstance(src, dict) else "")
    return (path, status, sha256)


def _observation_sort_key(obs: dict[str, Any]) -> tuple[int, str, str, str, str]:
    """Deterministic sort key for observations within a diagnostic result."""
    step_id = obs.get("step_id")
    step_num = -1 if step_id is None else step_id
    tool_call_id = str(obs.get("tool_call_id") or "")
    code = str(obs.get("code") or "")
    locator = str(obs.get("locator") or "")
    evidence_kind = str(obs.get("evidence_kind") or "")
    return (step_num, tool_call_id, code, locator, evidence_kind)


def build_report(records: list[dict[str, Any]], *, evidence_kind: str) -> dict[str, Any]:
    """Construct an observational harness mechanics report over trajectory records.

    Args:
        records: List of trajectory inspection records matching record_contract.
        evidence_kind: Declared provenance ('historical', 'fixture', or 'model-run').

    Returns:
        Structured report dictionary matching report_api contract.

    Raises:
        ValueError: If evidence_kind is invalid, records or diagnostics are malformed,
            duplicate source paths are provided, or required structures are missing.
    """
    if not isinstance(evidence_kind, str) or evidence_kind not in VALID_EVIDENCE_KINDS:
        raise ValueError(
            f"Invalid evidence_kind: {evidence_kind!r}. Expected one of: {sorted(VALID_EVIDENCE_KINDS)}"
        )

    if not isinstance(records, list):
        raise ValueError(f"records must be a list, got {type(records).__name__}")

    # Validate all records and ensure unique resolved input paths
    seen_paths: set[str] = set()
    for index, rec in enumerate(records):
        _validate_record(rec, index)
        path = rec["source"]["path"]
        if path in seen_paths:
            raise ValueError(
                f"Duplicate source path detected at index {index}: {path!r}. "
                f"Each record must correspond to a unique resolved input path."
            )
        seen_paths.add(path)

    # Ingestion denominators
    source_count = len(records)
    analyzed_count = 0
    unsupported_count = 0
    error_count = 0

    # Exactly three standard mechanisms
    observed_counts_by_mechanism: dict[str, int] = {m: 0 for m in STANDARD_MECHANISMS}
    hypothesis_counts_by_mechanism: dict[str, int] = {m: 0 for m in STANDARD_MECHANISMS}
    unknown_counts_by_mechanism: dict[str, int] = {m: 0 for m in STANDARD_MECHANISMS}

    # Construct clean, projection-sanitized records to prevent arbitrary data leakage
    processed_records: list[dict[str, Any]] = []
    for rec in records:
        status = rec["status"]
        if status == "analyzed":
            analyzed_count += 1
            rec_diag: dict[str, Any] = {}
            for mech_name in STANDARD_MECHANISMS:
                diag = rec["diagnostics"][mech_name]
                sorted_unknowns = sorted(diag["unknowns"])
                unknown_counts_by_mechanism[mech_name] += len(sorted_unknowns)

                processed_obs: list[dict[str, Any]] = []
                for obs in diag["observations"]:
                    ev_k = obs["evidence_kind"]
                    if ev_k == "observed":
                        observed_counts_by_mechanism[mech_name] += 1
                    elif ev_k == "hypothesis":
                        hypothesis_counts_by_mechanism[mech_name] += 1
                    processed_obs.append(
                        {
                            "code": obs["code"],
                            "step_id": obs["step_id"],
                            "tool_call_id": obs["tool_call_id"],
                            "locator": obs["locator"],
                            "evidence_kind": obs["evidence_kind"],
                            "summary": obs["summary"],
                        }
                    )
                processed_obs.sort(key=_observation_sort_key)
                rec_diag[mech_name] = {
                    "mechanism": mech_name,
                    "observations": processed_obs,
                    "unknowns": sorted_unknowns,
                }
        else:
            if status == "unsupported":
                unsupported_count += 1
            elif status == "error":
                error_count += 1
            rec_diag = {}

        processed_records.append(
            {
                "source": {
                    "path": rec["source"]["path"],
                    "sha256": rec["source"]["sha256"],
                    "size_bytes": rec["source"]["size_bytes"],
                },
                "identity": {
                    "session_id": rec["identity"]["session_id"],
                    "model": rec["identity"]["model"],
                    "harness": rec["identity"]["harness"],
                },
                "status": status,
                "diagnostics": rec_diag,
                "warnings": list(rec["warnings"]),
            }
        )

    # Sort records deterministically by source path, status, and sha256
    sorted_records = sorted(processed_records, key=_record_sort_key)

    # Compile research limitations and non-claims
    limitations: list[str] = [
        "Zero observations for a mechanism indicate that no explicit retained markers were detected, "
        "not that the mechanism was absent or harmless (distinguishes absence of evidence from evidence of absence).",
        "Unmatched historical trajectory data cannot support causal comparisons, pass-rate rankings, "
        "or cross-model performance superiority claims.",
        "Hypotheses represent diagnostic patterns requiring controlled counterfactual ablation, "
        "not verified causal failure mechanisms.",
        "Retained trajectory evidence may be incomplete; uninstrumented state, omitted outputs, "
        "or unrecorded terminal conditions remain classified as unknowns.",
        "Unsupported and error records represent trajectories that could not be parsed or inspected; "
        "they are retained in denominators to prevent survivorship bias.",
        f"Evidence kind reflects user-declared provenance ('{evidence_kind}'), "
        "not automated cryptographic certification of origin.",
    ]

    if unsupported_count > 0 or error_count > 0:
        limitations.append(
            f"{unsupported_count + error_count} of {source_count} source records could not be analyzed "
            f"({unsupported_count} unsupported, {error_count} error); diagnostic mechanism counts "
            f"reflect only the {analyzed_count} analyzed trajectories."
        )

    if analyzed_count == 0:
        limitations.append(
            "No trajectories were successfully analyzed; diagnostic mechanism counts are all zero "
            "due to lack of analyzed inputs."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_kind": ANALYSIS_KIND,
        "evidence_kind": evidence_kind,
        "records": sorted_records,
        "summary": {
            "source_count": source_count,
            "analyzed_count": analyzed_count,
            "unsupported_count": unsupported_count,
            "error_count": error_count,
            "observed_counts_by_mechanism": observed_counts_by_mechanism,
            "hypothesis_counts_by_mechanism": hypothesis_counts_by_mechanism,
            "unknown_counts_by_mechanism": unknown_counts_by_mechanism,
        },
        "limitations": limitations,
    }


def _validate_report(report: Any) -> None:
    """Validate structure of report dictionary before rendering text."""
    if not isinstance(report, dict):
        raise ValueError(f"report must be a dict, got {type(report).__name__}")
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Invalid schema_version: {report.get('schema_version')!r}. Expected {SCHEMA_VERSION}"
        )
    if report.get("analysis_kind") != ANALYSIS_KIND:
        raise ValueError(
            f"Invalid analysis_kind: {report.get('analysis_kind')!r}. Expected {ANALYSIS_KIND!r}"
        )
    evidence_kind = report.get("evidence_kind")
    if not isinstance(evidence_kind, str) or evidence_kind not in VALID_EVIDENCE_KINDS:
        raise ValueError("report missing valid 'evidence_kind' string")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("report missing valid 'summary' dict")
    required_summary_keys = (
        "source_count",
        "analyzed_count",
        "unsupported_count",
        "error_count",
        "observed_counts_by_mechanism",
        "hypothesis_counts_by_mechanism",
        "unknown_counts_by_mechanism",
    )
    for key in required_summary_keys:
        if key not in summary:
            raise ValueError(f"summary missing required key: {key!r}")
    for count_key in ("source_count", "analyzed_count", "unsupported_count", "error_count"):
        c_val = summary[count_key]
        if isinstance(c_val, bool) or not isinstance(c_val, int) or c_val < 0:
            raise ValueError(f"summary '{count_key}' must be a non-negative int, got {c_val!r}")
    for mech_dict_key in (
        "observed_counts_by_mechanism",
        "hypothesis_counts_by_mechanism",
        "unknown_counts_by_mechanism",
    ):
        mech_counts = summary[mech_dict_key]
        if not isinstance(mech_counts, dict):
            raise ValueError(f"summary '{mech_dict_key}' must be a dict")

    if "records" not in report or not isinstance(report["records"], list):
        raise ValueError("report missing valid 'records' list")
    for idx, rec in enumerate(report["records"]):
        _validate_record(rec, idx)

    if "limitations" not in report or not isinstance(report["limitations"], list):
        raise ValueError("report missing valid 'limitations' list")


def render_text(report: dict[str, Any]) -> str:
    """Render a human-readable, console-friendly text summary of the report.

    Features:
    - Quickly displays observed evidence, hypotheses, and unknowns without dumping raw data.
    - Explicitly communicates denominators and failure rates without survival bias.
    - Explicitly distinguishes 0 observations from evidence of absence.
    - Fully deterministic output with no volatile timestamps.
    - No raw prompts, reasoning traces, or command output text.

    Args:
        report: Valid report dictionary produced by build_report.

    Returns:
        Formatted multi-line text report.

    Raises:
        ValueError: If report structure is invalid.
    """
    _validate_report(report)

    evidence_kind = report["evidence_kind"]
    schema_version = report["schema_version"]
    analysis_kind = report["analysis_kind"]
    summary = report["summary"]

    source_count = summary["source_count"]
    analyzed_count = summary["analyzed_count"]
    unsupported_count = summary["unsupported_count"]
    error_count = summary["error_count"]

    observed = summary["observed_counts_by_mechanism"]
    hypotheses = summary["hypothesis_counts_by_mechanism"]
    unknowns = summary["unknown_counts_by_mechanism"]

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("HARNESS MECHANICS OBSERVATIONAL ANALYSIS REPORT")
    lines.append("=" * 80)
    lines.append(f"Analysis Kind  : {analysis_kind}")
    lines.append(f"Evidence Kind  : {evidence_kind}")
    lines.append(f"Schema Version : {schema_version}")
    lines.append("")

    lines.append("-" * 80)
    lines.append("EXECUTION & INGESTION DENOMINATORS")
    lines.append("-" * 80)
    if source_count > 0:
        analyzed_pct = (analyzed_count / source_count) * 100.0
        unsupported_pct = (unsupported_count / source_count) * 100.0
        error_pct = (error_count / source_count) * 100.0
    else:
        analyzed_pct = unsupported_pct = error_pct = 0.0

    lines.append(f"Total Sources  : {source_count}")
    lines.append(f"  - Analyzed   : {analyzed_count:>4} ({analyzed_pct:5.1f}%)")
    lines.append(f"  - Unsupported: {unsupported_count:>4} ({unsupported_pct:5.1f}%)")
    lines.append(f"  - Errors     : {error_count:>4} ({error_pct:5.1f}%)")
    lines.append("")

    lines.append("-" * 80)
    lines.append("MECHANISM EVIDENCE COUNTS")
    lines.append("-" * 80)
    lines.append(
        f"{'Mechanism':<15} {'Observed (Facts)':<18} {'Hypotheses':<15} {'Unknowns (Gaps)':<15}"
    )
    lines.append("-" * 65)

    for mech in STANDARD_MECHANISMS:
        obs_c = observed.get(mech, 0)
        hyp_c = hypotheses.get(mech, 0)
        unk_c = unknowns.get(mech, 0)
        lines.append(f"{mech:<15} {obs_c:<18} {hyp_c:<15} {unk_c:<15}")

    lines.append("-" * 65)
    lines.append("Note: 0 observed count indicates no explicit retained markers were detected;")
    lines.append("      it is NOT evidence of mechanism absence or harmlessness.")
    lines.append("")

    # Diagnostic observations breakdown by code and mechanism
    lines.append("-" * 80)
    lines.append("DIAGNOSTIC OBSERVATIONS BREAKDOWN")
    lines.append("-" * 80)

    obs_by_mech_code: dict[str, dict[str, dict[str, Any]]] = {}
    unk_by_mech: dict[str, dict[str, int]] = {}

    for rec in report.get("records", []):
        if not isinstance(rec, dict) or rec.get("status") != "analyzed":
            continue
        diagnostics = rec.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        for mech in STANDARD_MECHANISMS:
            diag = diagnostics.get(mech)
            if not isinstance(diag, dict):
                continue
            if mech not in obs_by_mech_code:
                obs_by_mech_code[mech] = {}
            if mech not in unk_by_mech:
                unk_by_mech[mech] = {}

            for obs in diag.get("observations", []):
                code = obs["code"]
                kind = obs["evidence_kind"]
                if code not in obs_by_mech_code[mech]:
                    obs_by_mech_code[mech][code] = {
                        "observed": 0,
                        "hypothesis": 0,
                        "summary": obs.get("summary", ""),
                        "locators": set(),
                    }
                obs_by_mech_code[mech][code][kind] += 1
                if obs.get("locator"):
                    obs_by_mech_code[mech][code]["locators"].add(obs["locator"])

            for unk in diag.get("unknowns", []):
                unk_by_mech[mech][unk] = unk_by_mech[mech].get(unk, 0) + 1

    total_obs_found = sum(len(codes) for codes in obs_by_mech_code.values())
    if total_obs_found == 0:
        lines.append("No diagnostic observations recorded across analyzed trajectories.")
    else:
        for mech in STANDARD_MECHANISMS:
            codes = obs_by_mech_code.get(mech, {})
            if not codes:
                lines.append(f"[{mech}]: No observations recorded.")
                continue
            lines.append(f"[{mech}]")
            for code in sorted(codes.keys()):
                info = codes[code]
                obs_count = info["observed"]
                hyp_count = info["hypothesis"]
                summary_text = info["summary"]
                sorted_locators = sorted(info["locators"])
                locators_str = ", ".join(sorted_locators[:3])
                if len(sorted_locators) > 3:
                    locators_str += f" (+{len(sorted_locators) - 3} more)"
                lines.append(f"  - Code    : {code}")
                lines.append(f"    Counts  : observed={obs_count}, hypothesis={hyp_count}")
                lines.append(f"    Summary : {summary_text}")
                if locators_str:
                    lines.append(f"    Locators: {locators_str}")
    lines.append("")

    # Unobserved State and Unknowns
    lines.append("-" * 80)
    lines.append("UNOBSERVED STATE & MISSING INSTRUMENTATION (UNKNOWNS)")
    lines.append("-" * 80)
    total_unks_found = sum(len(unks) for unks in unk_by_mech.values())
    if total_unks_found == 0:
        lines.append("No explicit unknowns flagged across analyzed trajectories.")
    else:
        for mech in STANDARD_MECHANISMS:
            unks = unk_by_mech.get(mech, {})
            if not unks:
                continue
            lines.append(f"[{mech}]")
            for unk_desc in sorted(unks.keys()):
                count = unks[unk_desc]
                lines.append(f"  - {unk_desc} (occurrences: {count})")
    lines.append("")

    # Trajectory Inventory
    lines.append("-" * 80)
    lines.append("SOURCE TRAJECTORY INVENTORY (DETERMINISTIC ORDER)")
    lines.append("-" * 80)
    records = report.get("records", [])
    if not records:
        lines.append("No records in report.")
    else:
        for idx, rec in enumerate(records, 1):
            src_path = rec.get("source", {}).get("path", "unknown")
            status = rec.get("status", "unknown")
            ident = rec.get("identity", {})
            model = ident.get("model") or "unrecorded"
            harness = ident.get("harness") or "unrecorded"
            warns = rec.get("warnings", [])

            lines.append(f"[{idx}] {src_path}")
            lines.append(f"    Status  : {status}")
            lines.append(f"    Identity: model={model}, harness={harness}")
            if warns:
                lines.append(f"    Warnings: {'; '.join(warns)}")
            if status == "analyzed":
                diag_parts = []
                for m in STANDARD_MECHANISMS:
                    d = rec.get("diagnostics", {}).get(m, {})
                    o_cnt = sum(
                        1 for o in d.get("observations", []) if o.get("evidence_kind") == "observed"
                    )
                    h_cnt = sum(
                        1
                        for o in d.get("observations", [])
                        if o.get("evidence_kind") == "hypothesis"
                    )
                    u_cnt = len(d.get("unknowns", []))
                    diag_parts.append(f"{m}: {o_cnt} obs / {h_cnt} hyp / {u_cnt} unk")
                lines.append(f"    Details : {'; '.join(diag_parts)}")
    lines.append("")

    # Research limitations
    lines.append("-" * 80)
    lines.append("LIMITATIONS & RESEARCH INTEGRITY NON-CLAIMS")
    lines.append("-" * 80)
    for idx, lim in enumerate(report.get("limitations", []), 1):
        lines.append(f"{idx}. {lim}")
    lines.append("=" * 80)

    return "\n".join(lines) + "\n"
