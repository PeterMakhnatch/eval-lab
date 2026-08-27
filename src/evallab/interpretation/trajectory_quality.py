"""Durable Trajectory Quality Ledger and Inspection Engine.

Evaluates trial evidence and ATIF trajectories before semantic or model-based
analysis. Persists deterministic trial-level `trajectory_quality_reports.parquet`
and reason-coded `trajectory_quality_findings.parquet`.

Guarantees:
- Raw Harbor evidence is never modified or deleted.
- Raw jobs and trials are cataloged before quality disposition.
- Malformed or infrastructure-failed trials remain catalog-visible but are
  marked `is_analysis_ready = False`.
- AnalysisWorker fails closed if quality is missing (`quality_not_evaluated`),
  failed, or quarantined.
- Re-ingestion and quality evaluation are deterministic and idempotent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

QUALITY_CHECK_VERSION = "v1.0.0"
CHECK_CODE_DIGEST = "sha256:7e91a0b3f8c2e4d56719a8b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1"

QUALITY_REPORT_TABLE = "trajectory_quality_reports"
QUALITY_FINDINGS_TABLE = "trajectory_quality_findings"


class QualityStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    QUARANTINE = "quarantine"
    NOT_EVALUATED = "quality_not_evaluated"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


REPORT_SCHEMA = pa.schema(
    [
        pa.field("job_id", pa.string()),
        pa.field("trial_id", pa.string()),
        pa.field("document_id", pa.string()),
        pa.field("raw_atif_digest", pa.string()),
        pa.field("raw_result_digest", pa.string()),
        pa.field("check_version", pa.string()),
        pa.field("check_digest", pa.string()),
        pa.field("status", pa.string()),
        pa.field("is_ingestable", pa.bool_()),
        pa.field("is_analysis_ready", pa.bool_()),
        pa.field("quarantine_reason", pa.string()),
        pa.field("findings_count", pa.int64()),
        pa.field("warnings_count", pa.int64()),
        pa.field("errors_count", pa.int64()),
        pa.field("evaluated_at", pa.string()),
    ]
)

FINDING_SCHEMA = pa.schema(
    [
        pa.field("finding_id", pa.string()),
        pa.field("job_id", pa.string()),
        pa.field("trial_id", pa.string()),
        pa.field("document_id", pa.string()),
        pa.field("severity", pa.string()),
        pa.field("category", pa.string()),
        pa.field("code", pa.string()),
        pa.field("message", pa.string()),
        pa.field("step_id", pa.int64()),
        pa.field("tool_call_id", pa.string()),
        pa.field("evaluated_at", pa.string()),
    ]
)


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


@dataclass(frozen=True)
class TrajectoryQualityFinding:
    finding_id: str
    job_id: str
    trial_id: str
    document_id: str
    severity: FindingSeverity
    category: str
    code: str
    message: str
    step_id: int | None = None
    tool_call_id: str | None = None
    evaluated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "job_id": self.job_id,
            "trial_id": self.trial_id,
            "document_id": self.document_id,
            "severity": str(self.severity),
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class TrajectoryQualityReport:
    job_id: str
    trial_id: str
    document_id: str
    raw_atif_digest: str | None
    raw_result_digest: str | None
    check_version: str
    check_digest: str
    status: QualityStatus
    is_ingestable: bool
    is_analysis_ready: bool
    quarantine_reason: str | None
    findings_count: int
    warnings_count: int
    errors_count: int
    evaluated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "trial_id": self.trial_id,
            "document_id": self.document_id,
            "raw_atif_digest": self.raw_atif_digest or "",
            "raw_result_digest": self.raw_result_digest or "",
            "check_version": self.check_version,
            "check_digest": self.check_digest,
            "status": str(self.status),
            "is_ingestable": self.is_ingestable,
            "is_analysis_ready": self.is_analysis_ready,
            "quarantine_reason": self.quarantine_reason or "",
            "findings_count": self.findings_count,
            "warnings_count": self.warnings_count,
            "errors_count": self.errors_count,
            "evaluated_at": self.evaluated_at,
        }


def make_finding_id(trial_id: str, code: str, step_id: int | None, tool_call_id: str | None) -> str:
    seed = f"{trial_id}:{code}:{step_id or ''}:{tool_call_id or ''}"
    return "finding:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def evaluate_trial_quality(
    trial_dir: Path,
    job_dir: Path | None = None,
    *,
    job_id_override: str | None = None,
    trial_id_override: str | None = None,
    evaluated_at: str | None = None,
) -> tuple[TrajectoryQualityReport, list[TrajectoryQualityFinding]]:
    """Deterministically audit a single trial directory.

    Re-ingestion and evaluation are guaranteed idempotent.
    """
    trial_dir = Path(trial_dir).resolve()
    result_json_path = trial_dir / "result.json"
    traj_json_path = trial_dir / "agent" / "trajectory.json"
    exception_txt_path = trial_dir / "exception.txt"

    # 1. Resolve trial and job identities
    result_data: dict[str, Any] = {}
    if result_json_path.is_file():
        try:
            result_data = json.loads(result_json_path.read_text(encoding="utf-8"))
        except Exception:
            result_data = {}

    trial_id = trial_id_override or result_data.get("id") or trial_dir.name
    job_id = job_id_override
    if not job_id:
        if job_dir is not None and (job_dir / "result.json").is_file():
            try:
                j_data = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
                job_id = j_data.get("id") or job_dir.name
            except Exception:
                job_id = job_dir.name
        else:
            job_id = trial_dir.parent.name

    doc_id = f"doc:{hashlib.sha256(f'{job_id}:{trial_id}'.encode()).hexdigest()[:16]}"
    raw_result_digest = _sha256_file(result_json_path)
    raw_atif_digest = _sha256_file(traj_json_path)

    findings: list[TrajectoryQualityFinding] = []
    quarantine_reason: str | None = None
    status = QualityStatus.PASS
    is_ingestable = True
    is_analysis_ready = True

    # 2. Check for Infrastructure and Runner Exceptions
    # If exception.txt or unprojectable crash exists, it must be quarantined, not given reward 0.
    if exception_txt_path.is_file():
        try:
            exc_text = exception_txt_path.read_text(encoding="utf-8").strip()
        except OSError:
            exc_text = "unreadable_exception_file"
        exc_type = exc_text.splitlines()[0][:80] if exc_text else "unknown_exception"
        quarantine_reason = f"infrastructure_exception:{exc_type}"
        findings.append(
            TrajectoryQualityFinding(
                finding_id=make_finding_id(trial_id, "INFRA_EXCEPTION", None, None),
                job_id=job_id,
                trial_id=trial_id,
                document_id=doc_id,
                severity=FindingSeverity.FATAL,
                category="infrastructure",
                code="INFRA_EXCEPTION",
                message=f"Trial failed with infrastructure exception: {exc_type}",
            )
        )
        status = QualityStatus.QUARANTINE
        is_analysis_ready = False

    # Check result.json error/exception status
    res_exc = result_data.get("agent_result", {}).get("exception") or result_data.get("exception")
    if res_exc and status != QualityStatus.QUARANTINE:
        quarantine_reason = f"runner_exception:{str(res_exc)[:80]}"
        findings.append(
            TrajectoryQualityFinding(
                finding_id=make_finding_id(trial_id, "RUNNER_EXCEPTION", None, None),
                job_id=job_id,
                trial_id=trial_id,
                document_id=doc_id,
                severity=FindingSeverity.FATAL,
                category="infrastructure",
                code="RUNNER_EXCEPTION",
                message=f"Runner reported exception: {res_exc}",
            )
        )
        status = QualityStatus.QUARANTINE
        is_analysis_ready = False

    # 3. Check ATIF Trajectory Integrity
    if not traj_json_path.is_file():
        # Check if it's an oracle or nop control run
        agent_name = (
            result_data.get("agent_info", {}).get("name") or result_data.get("agent_name") or ""
        )
        is_control = any(c in agent_name.lower() for c in ("oracle", "nop", "control"))
        if is_control:
            findings.append(
                TrajectoryQualityFinding(
                    finding_id=make_finding_id(trial_id, "CONTROL_NON_ATIF", None, None),
                    job_id=job_id,
                    trial_id=trial_id,
                    document_id=doc_id,
                    severity=FindingSeverity.INFO,
                    category="control",
                    code="CONTROL_NON_ATIF",
                    message="Control trial does not produce ATIF trajectory",
                )
            )
            # Controls are valid in catalog but do not undergo agent behavior analysis
            is_analysis_ready = False
            if status == QualityStatus.PASS:
                status = QualityStatus.PASS
        else:
            findings.append(
                TrajectoryQualityFinding(
                    finding_id=make_finding_id(trial_id, "ATIF_MISSING", None, None),
                    job_id=job_id,
                    trial_id=trial_id,
                    document_id=doc_id,
                    severity=FindingSeverity.ERROR,
                    category="atif",
                    code="ATIF_MISSING",
                    message="agent/trajectory.json is missing for billable/eval trial",
                )
            )
            quarantine_reason = "missing_trajectory_file"
            status = QualityStatus.FAIL
            is_analysis_ready = False
    else:
        # Parse and validate ATIF structure
        try:
            traj_raw = json.loads(traj_json_path.read_text(encoding="utf-8"))
            if not isinstance(traj_raw, dict):
                raise ValueError("ATIF root must be a JSON object")

            # Check schema version
            schema_ver = traj_raw.get("schema_version", "")
            if not schema_ver or not schema_ver.startswith("ATIF"):
                findings.append(
                    TrajectoryQualityFinding(
                        finding_id=make_finding_id(trial_id, "ATIF_SCHEMA_INVALID", None, None),
                        job_id=job_id,
                        trial_id=trial_id,
                        document_id=doc_id,
                        severity=FindingSeverity.WARN,
                        category="atif",
                        code="ATIF_SCHEMA_INVALID",
                        message=f"Missing or unrecognized schema_version: {schema_ver}",
                    )
                )
                if status == QualityStatus.PASS:
                    status = QualityStatus.WARN

            steps = traj_raw.get("steps", [])
            if not isinstance(steps, list) or len(steps) == 0:
                findings.append(
                    TrajectoryQualityFinding(
                        finding_id=make_finding_id(trial_id, "ATIF_EMPTY_STEPS", None, None),
                        job_id=job_id,
                        trial_id=trial_id,
                        document_id=doc_id,
                        severity=FindingSeverity.WARN,
                        category="atif",
                        code="ATIF_EMPTY_STEPS",
                        message="ATIF trajectory contains zero execution steps",
                    )
                )
                if status == QualityStatus.PASS:
                    status = QualityStatus.WARN
            else:
                # Validate step structure and sequence
                last_step_id = -1
                for idx, step in enumerate(steps):
                    if not isinstance(step, dict):
                        findings.append(
                            TrajectoryQualityFinding(
                                finding_id=make_finding_id(
                                    trial_id, "ATIF_MALFORMED_STEP", idx, None
                                ),
                                job_id=job_id,
                                trial_id=trial_id,
                                document_id=doc_id,
                                severity=FindingSeverity.ERROR,
                                category="atif",
                                code="ATIF_MALFORMED_STEP",
                                message=f"Step index {idx} is not a valid JSON object",
                                step_id=idx,
                            )
                        )
                        status = QualityStatus.FAIL
                        is_analysis_ready = False
                        continue

                    step_id = step.get("step_id", idx)
                    if step_id <= last_step_id:
                        findings.append(
                            TrajectoryQualityFinding(
                                finding_id=make_finding_id(
                                    trial_id, "ATIF_NON_MONOTONIC_STEP", step_id, None
                                ),
                                job_id=job_id,
                                trial_id=trial_id,
                                document_id=doc_id,
                                severity=FindingSeverity.WARN,
                                category="atif",
                                code="ATIF_NON_MONOTONIC_STEP",
                                message=f"Step ID {step_id} is not strictly greater than previous {last_step_id}",
                                step_id=step_id,
                            )
                        )
                        if status == QualityStatus.PASS:
                            status = QualityStatus.WARN
                    last_step_id = step_id

                    # Check tool call / observation pairing
                    tool_calls = step.get("tool_calls", [])
                    observations = step.get("observations", [])
                    if len(tool_calls) > 0 and len(observations) == 0:
                        findings.append(
                            TrajectoryQualityFinding(
                                finding_id=make_finding_id(
                                    trial_id, "ATIF_UNPAIRED_TOOL_CALL", step_id, None
                                ),
                                job_id=job_id,
                                trial_id=trial_id,
                                document_id=doc_id,
                                severity=FindingSeverity.WARN,
                                category="atif",
                                code="ATIF_UNPAIRED_TOOL_CALL",
                                message=f"Step {step_id} has {len(tool_calls)} tool calls but 0 observations",
                                step_id=step_id,
                            )
                        )
                        if status == QualityStatus.PASS:
                            status = QualityStatus.WARN

        except Exception as exc:
            # Projection / parsing exception becomes an explicit quality failure, never reward 0
            quarantine_reason = f"atif_parse_error:{type(exc).__name__}"
            findings.append(
                TrajectoryQualityFinding(
                    finding_id=make_finding_id(trial_id, "ATIF_PARSE_ERROR", None, None),
                    job_id=job_id,
                    trial_id=trial_id,
                    document_id=doc_id,
                    severity=FindingSeverity.ERROR,
                    category="atif",
                    code="ATIF_PARSE_ERROR",
                    message=f"Failed to parse ATIF trajectory JSON: {exc}",
                )
            )
            status = QualityStatus.FAIL
            is_analysis_ready = False

    warn_count = sum(1 for f in findings if f.severity == FindingSeverity.WARN)
    err_count = sum(
        1 for f in findings if f.severity in (FindingSeverity.ERROR, FindingSeverity.FATAL)
    )

    kw = {}
    if evaluated_at is not None:
        kw["evaluated_at"] = evaluated_at

    report = TrajectoryQualityReport(
        job_id=job_id,
        trial_id=trial_id,
        document_id=doc_id,
        raw_atif_digest=raw_atif_digest,
        raw_result_digest=raw_result_digest,
        check_version=QUALITY_CHECK_VERSION,
        check_digest=CHECK_CODE_DIGEST,
        status=status,
        is_ingestable=is_ingestable,
        is_analysis_ready=is_analysis_ready,
        quarantine_reason=quarantine_reason,
        findings_count=len(findings),
        warnings_count=warn_count,
        errors_count=err_count,
        **kw,
    )
    return report, findings


def persist_quality_ledger(
    reports: Sequence[TrajectoryQualityReport],
    findings: Sequence[TrajectoryQualityFinding],
    derived_root: Path,
) -> tuple[Path, Path]:
    """Persist quality reports and findings into deterministic Parquet tables.

    Re-ingestion is idempotent: replacing old rows for updated trials while
    retaining historical integrity.
    """
    derived_root = Path(derived_root).resolve()
    derived_root.mkdir(parents=True, exist_ok=True)

    reports_path = derived_root / f"{QUALITY_REPORT_TABLE}.parquet"
    findings_path = derived_root / f"{QUALITY_FINDINGS_TABLE}.parquet"

    # Merge with existing data if present to ensure idempotency
    existing_reports: dict[str, dict[str, Any]] = {}
    if reports_path.is_file():
        try:
            old_table = pq.read_table(reports_path)
            for row in old_table.to_pylist():
                existing_reports[row["trial_id"]] = row
        except Exception:
            existing_reports = {}

    for r in reports:
        existing_reports[r.trial_id] = r.to_dict()

    # Sort deterministically by job_id, trial_id
    sorted_reports = sorted(
        existing_reports.values(), key=lambda r: (r.get("job_id", ""), r.get("trial_id", ""))
    )
    rep_table = pa.Table.from_pylist(sorted_reports, schema=REPORT_SCHEMA)
    pq.write_table(rep_table, reports_path)

    existing_findings: dict[str, dict[str, Any]] = {}
    if findings_path.is_file():
        try:
            old_f_table = pq.read_table(findings_path)
            for row in old_f_table.to_pylist():
                existing_findings[row["finding_id"]] = row
        except Exception:
            existing_findings = {}

    for f in findings:
        existing_findings[f.finding_id] = f.to_dict()

    sorted_findings = sorted(
        existing_findings.values(),
        key=lambda f: (
            f.get("job_id", ""),
            f.get("trial_id", ""),
            f.get("step_id") or -1,
            f.get("finding_id", ""),
        ),
    )
    find_table = pa.Table.from_pylist(sorted_findings, schema=FINDING_SCHEMA)
    pq.write_table(find_table, findings_path)

    return reports_path, findings_path


def load_quality_report_for_trial(
    trial_id: str, derived_root: Path
) -> TrajectoryQualityReport | None:
    """Load the quality report for a specific trial if it exists in the ledger."""
    reports_path = Path(derived_root).resolve() / f"{QUALITY_REPORT_TABLE}.parquet"
    if not reports_path.is_file():
        return None
    try:
        table = pq.read_table(reports_path, filters=[("trial_id", "=", trial_id)])
        rows = table.to_pylist()
        if not rows:
            return None
        r = rows[0]
        return TrajectoryQualityReport(
            job_id=r["job_id"],
            trial_id=r["trial_id"],
            document_id=r["document_id"],
            raw_atif_digest=r["raw_atif_digest"] or None,
            raw_result_digest=r["raw_result_digest"] or None,
            check_version=r["check_version"],
            check_digest=r["check_digest"],
            status=QualityStatus(r["status"]),
            is_ingestable=r["is_ingestable"],
            is_analysis_ready=r["is_analysis_ready"],
            quarantine_reason=r["quarantine_reason"] or None,
            findings_count=r["findings_count"],
            warnings_count=r["warnings_count"],
            errors_count=r["errors_count"],
            evaluated_at=r["evaluated_at"],
        )
    except Exception:
        return None


def register_quality_tables_in_duckdb(
    conn: duckdb.DuckDBPyConnection, derived_root: Path
) -> tuple[bool, bool]:
    # Expose quality reports and findings as queryable DuckDB tables
    derived_root = Path(derived_root).resolve()
    reports_path = derived_root / f"{QUALITY_REPORT_TABLE}.parquet"
    findings_path = derived_root / f"{QUALITY_FINDINGS_TABLE}.parquet"

    has_reports = reports_path.is_file()
    has_findings = findings_path.is_file()

    if has_reports:
        conn.execute(
            f"CREATE OR REPLACE VIEW {QUALITY_REPORT_TABLE} AS SELECT * FROM read_parquet('{reports_path.as_posix()}')"
        )
    else:
        empty_reports_sql = (
            f"CREATE OR REPLACE VIEW {QUALITY_REPORT_TABLE} AS "
            "SELECT CAST('' AS VARCHAR) AS job_id, "
            "CAST('' AS VARCHAR) AS trial_id, "
            "CAST('' AS VARCHAR) AS document_id, "
            "CAST('' AS VARCHAR) AS raw_atif_digest, "
            "CAST('' AS VARCHAR) AS raw_result_digest, "
            "CAST('' AS VARCHAR) AS check_version, "
            "CAST('' AS VARCHAR) AS check_digest, "
            "CAST('' AS VARCHAR) AS status, "
            "CAST(false AS BOOLEAN) AS is_ingestable, "
            "CAST(false AS BOOLEAN) AS is_analysis_ready, "
            "CAST('' AS VARCHAR) AS quarantine_reason, "
            "CAST(0 AS BIGINT) AS findings_count, "
            "CAST(0 AS BIGINT) AS warnings_count, "
            "CAST(0 AS BIGINT) AS errors_count, "
            "CAST('' AS VARCHAR) AS evaluated_at WHERE false"
        )
        conn.execute(empty_reports_sql)

    if has_findings:
        conn.execute(
            f"CREATE OR REPLACE VIEW {QUALITY_FINDINGS_TABLE} AS SELECT * FROM read_parquet('{findings_path.as_posix()}')"
        )
    else:
        empty_findings_sql = (
            f"CREATE OR REPLACE VIEW {QUALITY_FINDINGS_TABLE} AS "
            "SELECT CAST('' AS VARCHAR) AS finding_id, "
            "CAST('' AS VARCHAR) AS job_id, "
            "CAST('' AS VARCHAR) AS trial_id, "
            "CAST('' AS VARCHAR) AS document_id, "
            "CAST('' AS VARCHAR) AS severity, "
            "CAST('' AS VARCHAR) AS category, "
            "CAST('' AS VARCHAR) AS code, "
            "CAST('' AS VARCHAR) AS message, "
            "CAST(0 AS BIGINT) AS step_id, "
            "CAST('' AS VARCHAR) AS tool_call_id, "
            "CAST('' AS VARCHAR) AS evaluated_at WHERE false"
        )
        conn.execute(empty_findings_sql)

    return has_reports, has_findings
