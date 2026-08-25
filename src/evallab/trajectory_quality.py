"""Deterministic evidence quality checks for trajectory ingestion and analysis.

Validates raw ATIF envelopes, Harbor trial-result consistency, projected facts,
and analysis readiness without scoring agent capability or mutating inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_ATIF_VERSIONS = {f"ATIF-v1.{minor}" for minor in range(10)} | {
    "v1.0",
    "v1.1",
    "v1.2",
    "v1.3",
    "v1.4",
    "v1.5",
    "v1.6",
    "v1.7",
}


class QualityFinding(BaseModel):
    """A frozen, reason-coded observation emitted by the quality gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Literal["info", "warning", "error"]
    reason_code: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    source_digest: str | None = None
    affected_identity: str | None = None


class TrajectoryQualityReport(BaseModel):
    """Frozen evaluation summary deciding whether evidence is trustworthy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pass", "warn", "fail", "quarantined"]
    check_version: str = "v1.0"
    check_digest: str
    is_ingestable: bool
    is_analysis_ready: bool
    quarantine_reason: str | None = None
    summary_counts: dict[str, int]
    findings: list[QualityFinding]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_check_digest(findings: list[QualityFinding], version: str = "v1.0") -> str:
    sorted_findings = sorted(
        findings,
        key=lambda f: (f.severity, f.reason_code, f.affected_identity or "", f.message),
    )
    serialized = [f.model_dump() for f in sorted_findings]
    payload = {"version": version, "findings": serialized}
    return f"sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


def evaluate_raw_envelope(
    atif_doc: Mapping[str, Any],
    trial_result: Mapping[str, Any] | None = None,
    artifacts_dir: Path | None = None,
) -> list[QualityFinding]:
    """Inspect raw ATIF envelope and Harbor TrialResult consistency."""
    findings: list[QualityFinding] = []

    # 1. ATIF schema version
    schema_version = atif_doc.get("schema_version")
    if not schema_version:
        findings.append(
            QualityFinding(
                severity="error",
                reason_code="ATIF_SCHEMA_MISSING",
                message="ATIF document is missing required 'schema_version'.",
                evidence_refs=["atif_doc.schema_version"],
            )
        )
    elif str(schema_version) not in SUPPORTED_ATIF_VERSIONS:
        findings.append(
            QualityFinding(
                severity="warning",
                reason_code="ATIF_SCHEMA_UNSUPPORTED",
                message=f"ATIF schema version '{schema_version}' is not in certified support list.",
                evidence_refs=[f"schema_version:{schema_version}"],
            )
        )

    # 2. Sequential Step IDs and monotonic ordering
    steps = atif_doc.get("steps")
    if steps is None:
        findings.append(
            QualityFinding(
                severity="error",
                reason_code="ATIF_STEPS_MISSING",
                message="ATIF document is missing required 'steps' list.",
                evidence_refs=["atif_doc.steps"],
            )
        )
    elif not isinstance(steps, list):
        findings.append(
            QualityFinding(
                severity="error",
                reason_code="ATIF_STEPS_MALFORMED",
                message="'steps' field in ATIF document is not a list.",
                evidence_refs=["atif_doc.steps"],
            )
        )
    else:
        seen_step_ids: set[int] = set()
        prev_step_id: int | None = None
        tool_call_ids: set[str] = set()
        tool_result_call_ids: list[str] = []

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                findings.append(
                    QualityFinding(
                        severity="error",
                        reason_code="ATIF_STEP_ENTRY_MALFORMED",
                        message=f"Step at index {idx} is not an object.",
                        evidence_refs=[f"steps[{idx}]"],
                    )
                )
                continue

            step_id = step.get("step_id")
            if step_id is None:
                findings.append(
                    QualityFinding(
                        severity="error",
                        reason_code="ATIF_STEP_ID_MISSING",
                        message=f"Step at index {idx} is missing 'step_id'.",
                        evidence_refs=[f"steps[{idx}]"],
                    )
                )
            elif not isinstance(step_id, int):
                findings.append(
                    QualityFinding(
                        severity="error",
                        reason_code="ATIF_STEP_ID_NON_INTEGER",
                        message=f"Step at index {idx} has non-integer 'step_id': {step_id}",
                        evidence_refs=[f"steps[{idx}].step_id"],
                        affected_identity=str(step_id),
                    )
                )
            else:
                if step_id in seen_step_ids:
                    findings.append(
                        QualityFinding(
                            severity="error",
                            reason_code="ATIF_STEP_ID_DUPLICATE",
                            message=f"Duplicate step_id {step_id} observed at index {idx}.",
                            evidence_refs=[f"steps[{idx}].step_id={step_id}"],
                            affected_identity=str(step_id),
                        )
                    )
                seen_step_ids.add(step_id)

                if (
                    prev_step_id is not None
                    and step_id != prev_step_id + 1
                    and not step.get("subagent_id")
                    and not step.get("parent_step_id")
                ):
                    findings.append(
                        QualityFinding(
                            severity="warning",
                            reason_code="ATIF_STEP_SEQUENCE_GAP",
                            message=f"Step ID gap or non-monotonic transition: {prev_step_id} -> {step_id}.",
                            evidence_refs=[f"prev:{prev_step_id}", f"curr:{step_id}"],
                            affected_identity=str(step_id),
                        )
                    )
                prev_step_id = step_id

            # Subagent / continuation resolvability
            parent_step_id = step.get("parent_step_id")
            if (
                parent_step_id is not None
                and isinstance(parent_step_id, int)
                and parent_step_id not in seen_step_ids
                and parent_step_id != step_id
            ):
                findings.append(
                    QualityFinding(
                        severity="warning",
                        reason_code="UNRESOLVED_PARENT_STEP_REF",
                        message=f"Step {step_id} references prior parent_step_id {parent_step_id} not yet seen.",
                        evidence_refs=[f"step:{step_id}", f"parent:{parent_step_id}"],
                        affected_identity=str(step_id),
                    )
                )

            # Token & Cache Invariants
            usage = step.get("usage") or step.get("metrics", {}).get("usage")
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                cached_tokens = (
                    usage.get("cached_tokens")
                    or usage.get("cache_read_tokens")
                    or usage.get("prompt_tokens_details", {}).get("cached_tokens")
                    or 0
                )
                if (
                    isinstance(prompt_tokens, int)
                    and isinstance(cached_tokens, int)
                    and cached_tokens > prompt_tokens
                ):
                    findings.append(
                        QualityFinding(
                            severity="error",
                            reason_code="CACHE_TOKENS_EXCEED_PROMPT",
                            message=(
                                f"Step {step_id}: cached tokens ({cached_tokens}) "
                                f"exceed prompt tokens ({prompt_tokens})."
                            ),
                            evidence_refs=[
                                f"step:{step_id}",
                                f"cached:{cached_tokens}",
                                f"prompt:{prompt_tokens}",
                            ],
                            affected_identity=str(step_id),
                        )
                    )

            # Tool calls & Tool results integrity
            tool_calls = step.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tc_id = tc.get("tool_call_id") or tc.get("id")
                        if tc_id:
                            str_id = str(tc_id)
                            if str_id in tool_call_ids:
                                findings.append(
                                    QualityFinding(
                                        severity="error",
                                        reason_code="DUPLICATE_TOOL_CALL_ID",
                                        message=f"Duplicate tool_call_id '{str_id}' defined.",
                                        evidence_refs=[f"tool_call_id:{str_id}"],
                                        affected_identity=str_id,
                                    )
                                )
                            tool_call_ids.add(str_id)

            tool_results = (
                step.get("tool_results")
                or step.get("observation", {}).get("tool_results")
                or []
            )
            if isinstance(tool_results, list):
                for tr in tool_results:
                    if isinstance(tr, dict):
                        target_id = tr.get("tool_call_id") or tr.get("id")
                        if target_id:
                            tool_result_call_ids.append(str(target_id))

        # Check for orphan tool results
        for r_id in tool_result_call_ids:
            if tool_call_ids and r_id not in tool_call_ids:
                findings.append(
                    QualityFinding(
                        severity="error",
                        reason_code="ORPHAN_TOOL_RESULT",
                        message=f"Tool result references tool_call_id '{r_id}' with no corresponding tool call.",
                        evidence_refs=[f"tool_call_id:{r_id}"],
                        affected_identity=r_id,
                    )
                )

    # 3. Identity and Document metadata
    meta = atif_doc.get("metadata") or atif_doc.get("run_metadata") or {}
    session_id = meta.get("session_id") or meta.get("run_id")
    if not session_id:
        findings.append(
            QualityFinding(
                severity="info",
                reason_code="DOCUMENT_SESSION_ID_MISSING",
                message="ATIF metadata does not specify explicit session_id or run_id.",
                evidence_refs=["atif_doc.metadata"],
            )
        )

    # 4. Token and Cost distinction
    cost = meta.get("total_cost_usd") or meta.get("cost_usd")
    if cost is None:
        findings.append(
            QualityFinding(
                severity="info",
                reason_code="COST_INDETERMINATE",
                message="Run cost is unspecified (indeterminate), not assumed zero.",
                evidence_refs=["cost:None"],
            )
        )
    elif isinstance(cost, (int, float)) and cost < 0:
        findings.append(
            QualityFinding(
                severity="error",
                reason_code="COST_NEGATIVE",
                message=f"Invalid negative cost encountered: {cost}",
                evidence_refs=[f"cost:{cost}"],
            )
        )

    # 5. Harbor TrialResult & Exception Consistency
    if trial_result is not None:
        task_id = trial_result.get("task_name") or trial_result.get("task_id")
        harbor_exception = trial_result.get("exception") or trial_result.get("error")
        verifier_res = trial_result.get("verifier_result") or trial_result.get("verifier") or {}
        rewards = trial_result.get("rewards") or verifier_res.get("rewards")

        checksum = trial_result.get("task_checksum") or trial_result.get("task_digest")
        if not checksum:
            findings.append(
                QualityFinding(
                    severity="warning",
                    reason_code="HARBOR_TASK_CHECKSUM_MISSING",
                    message=f"TrialResult for task '{task_id}' lacks explicit task_checksum.",
                    evidence_refs=[f"task_id:{task_id}"],
                    affected_identity=str(task_id),
                )
            )

        if harbor_exception:
            findings.append(
                QualityFinding(
                    severity="error",
                    reason_code="HARBOR_INFRASTRUCTURE_EXCEPTION",
                    message=f"Harbor infrastructure exception recorded: {str(harbor_exception)[:150]}",
                    evidence_refs=[f"exception:{str(harbor_exception)[:100]}"],
                    affected_identity=str(task_id),
                )
            )

        if rewards is None:
            findings.append(
                QualityFinding(
                    severity="warning",
                    reason_code="REWARD_INDETERMINATE",
                    message="TrialResult contains no reward entry (indeterminate, not explicit zero).",
                    evidence_refs=["rewards:None"],
                )
            )

        artifacts_manifest = trial_result.get("artifacts")
        if isinstance(artifacts_manifest, list) and artifacts_dir and artifacts_dir.exists():
            for art in artifacts_manifest:
                if isinstance(art, dict):
                    rel_path = art.get("path")
                    exp_sha = art.get("sha256")
                    if rel_path:
                        file_path = artifacts_dir / rel_path
                        if not file_path.exists():
                            findings.append(
                                QualityFinding(
                                    severity="error",
                                    reason_code="ARTIFACT_FILE_MISSING",
                                    message=f"Declared artifact file '{rel_path}' missing from disk.",
                                    evidence_refs=[f"path:{rel_path}"],
                                    affected_identity=rel_path,
                                )
                            )
                        elif exp_sha:
                            actual_sha = _sha256_file(file_path)
                            if actual_sha != exp_sha:
                                findings.append(
                                    QualityFinding(
                                        severity="error",
                                        reason_code="ARTIFACT_DIGEST_MISMATCH",
                                        message=(
                                            f"Artifact '{rel_path}' sha256 mismatch: "
                                            f"expected {exp_sha[:8]}, got {actual_sha[:8]}"
                                        ),
                                        evidence_refs=[
                                            f"path:{rel_path}",
                                            f"expected:{exp_sha}",
                                            f"actual:{actual_sha}",
                                        ],
                                        affected_identity=rel_path,
                                    )
                                )

    return findings


def evaluate_projection_fidelity(
    raw_atif: Mapping[str, Any],
    projected_facts: Mapping[str, list[Mapping[str, Any]]],
) -> list[QualityFinding]:
    """Verify fidelity between raw ATIF document and derived Parquet fact tables."""
    findings: list[QualityFinding] = []

    raw_steps = raw_atif.get("steps") or []
    projected_steps = projected_facts.get("trajectory_steps", [])
    projected_tool_calls = projected_facts.get("tool_calls", [])

    if len(raw_steps) != len(projected_steps):
        findings.append(
            QualityFinding(
                severity="error",
                reason_code="PROJECTION_STEP_COUNT_MISMATCH",
                message=f"Raw step count ({len(raw_steps)}) != projected step count ({len(projected_steps)}).",
                evidence_refs=[f"raw:{len(raw_steps)}", f"projected:{len(projected_steps)}"],
            )
        )

    raw_step_ids = {s.get("step_id") for s in raw_steps if isinstance(s, dict) and "step_id" in s}
    for p_step in projected_steps:
        p_id = p_step.get("step_id")
        if p_id is not None and p_id not in raw_step_ids:
            findings.append(
                QualityFinding(
                    severity="error",
                    reason_code="PROJECTION_ORPHAN_STEP",
                    message=f"Projected step_id {p_id} has no corresponding raw step in ATIF.",
                    evidence_refs=[f"step_id:{p_id}"],
                    affected_identity=str(p_id),
                )
            )

    raw_tool_calls = []
    for s in raw_steps:
        if isinstance(s, dict):
            for tc in s.get("tool_calls") or []:
                if isinstance(tc, dict):
                    raw_tool_calls.append(tc)

    if len(raw_tool_calls) != len(projected_tool_calls):
        findings.append(
            QualityFinding(
                severity="warning",
                reason_code="PROJECTION_TOOL_CALL_COUNT_MISMATCH",
                message=f"Raw tool calls ({len(raw_tool_calls)}) != projected tool calls ({len(projected_tool_calls)}).",
                evidence_refs=[
                    f"raw:{len(raw_tool_calls)}",
                    f"projected:{len(projected_tool_calls)}",
                ],
            )
        )

    return findings


def evaluate_analysis_readiness(
    findings: list[QualityFinding],
    trial_result: Mapping[str, Any] | None = None,
) -> tuple[Literal["pass", "warn", "fail", "quarantined"], bool, bool, str | None]:
    """Decide overall quality status, ingestion readiness, and quarantine isolation."""
    counts = Counter(f.severity for f in findings)
    reasons = {f.reason_code for f in findings}

    if "HARBOR_INFRASTRUCTURE_EXCEPTION" in reasons:
        return (
            "quarantined",
            False,
            False,
            "Harbor infrastructure exception encountered; isolated from capability scoring.",
        )

    if counts["error"] > 0:
        return "fail", False, False, None

    if counts["warning"] > 0:
        return "warn", True, True, None

    return "pass", True, True, None


def evaluate_trajectory_quality(
    raw_atif: Mapping[str, Any],
    trial_result: Mapping[str, Any] | None = None,
    projected_facts: Mapping[str, list[Mapping[str, Any]]] | None = None,
    artifacts_dir: Path | None = None,
    version: str = "v1.0",
) -> TrajectoryQualityReport:
    """Entry point: produce deterministic, pure-functional quality gate report."""
    findings: list[QualityFinding] = []

    findings.extend(evaluate_raw_envelope(raw_atif, trial_result, artifacts_dir))

    if projected_facts is not None:
        findings.extend(evaluate_projection_fidelity(raw_atif, projected_facts))

    status, is_ingestable, is_analysis_ready, quarantine_reason = evaluate_analysis_readiness(
        findings, trial_result
    )

    sorted_findings = sorted(
        findings,
        key=lambda f: (f.severity, f.reason_code, f.affected_identity or "", f.message),
    )
    check_digest = compute_check_digest(sorted_findings, version=version)

    summary_counts = {
        "total_findings": len(sorted_findings),
        "info": sum(1 for f in sorted_findings if f.severity == "info"),
        "warning": sum(1 for f in sorted_findings if f.severity == "warning"),
        "error": sum(1 for f in sorted_findings if f.severity == "error"),
    }

    return TrajectoryQualityReport(
        status=status,
        check_version=version,
        check_digest=check_digest,
        is_ingestable=is_ingestable,
        is_analysis_ready=is_analysis_ready,
        quarantine_reason=quarantine_reason,
        summary_counts=summary_counts,
        findings=sorted_findings,
    )
