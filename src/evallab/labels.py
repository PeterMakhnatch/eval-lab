"""Unified human, heuristic, and model behavior labels.

Labels share one versioned envelope regardless of target granularity and use
append/update-by-identity Parquet storage.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.schemas import (
    AnalysisEvidenceCitation,
    AnalysisProvenance,
    BehaviorLabel,
    TrialAnalysisSidecar,
)
from evallab.storage.paths import derived_root_from_environment
from evallab.traj import (
    TrajectoryError,
    TrajectoryOutline,
)

TRAJECTORY_BEHAVIOR_TAXONOMY = "trajectory_behavior/v1"
TRIAL_ANALYSIS_TAXONOMY = "trial_analysis/failure_category/v1"


BEHAVIOR_LABELS_PARQUET_SCHEMA = pa.schema([
    pa.field("schema_version", pa.int64(), nullable=False),
    pa.field("label_id", pa.string(), nullable=False),
    pa.field("target_type", pa.string(), nullable=False),
    pa.field("target_id", pa.string(), nullable=False),
    pa.field("job_id", pa.string()),
    pa.field("trial_id", pa.string(), nullable=False),
    pa.field("trial_name", pa.string(), nullable=False),
    pa.field("task_name", pa.string(), nullable=False),
    pa.field("taxonomy", pa.string(), nullable=False),
    pa.field("label", pa.string(), nullable=False),
    pa.field("rationale", pa.string()),
    pa.field("provenance", pa.string(), nullable=False),
    pa.field("author", pa.string(), nullable=False),
    pa.field("created_at", pa.string(), nullable=False),
    pa.field("confidence", pa.string()),
    pa.field("evidence_json", pa.string(), nullable=False),
    pa.field("source_sha256", pa.string()),
    pa.field("analysis_id", pa.string()),
    pa.field("model_agent", pa.string()),
    pa.field("model_agent_version", pa.string()),
    pa.field("model_name", pa.string()),
    pa.field("prompt_digest", pa.string()),
    pa.field("rubric_digest", pa.string()),
    pa.field("output_schema_digest", pa.string()),
    pa.field("model_created_at", pa.string()),
    pa.field("input_tokens", pa.int64()),
    pa.field("output_tokens", pa.int64()),
    pa.field("cost_usd", pa.float64()),
])


def _label_id(
    *, target_type: str, target_id: str, taxonomy: str, provenance: str, author: str
) -> str:
    value = "\0".join((target_type, target_id, taxonomy, provenance, author)).encode()
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
def _normalize_digest(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value)
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw.lower()):
        raise TrajectoryError(f"invalid sha256 digest: {value!r}")
    return f"sha256:{raw.lower()}"


def _labels_root(repo_root: Path, derived_root: Path | None) -> Path:
    return derived_root or (derived_root_from_environment(repo_root) / "behavior_labels")


def _canonical_path(repo_root: Path, derived_root: Path | None) -> Path:
    return _labels_root(repo_root, derived_root) / "behavior_labels.parquet"


def _label_row(label: BehaviorLabel) -> dict[str, Any]:
    provenance = label.model_provenance
    return {
        "schema_version": label.schema_version,
        "label_id": label.label_id,
        "target_type": label.target_type,
        "target_id": label.target_id,
        "job_id": label.job_id,
        "trial_id": label.trial_id,
        "trial_name": label.trial_name,
        "task_name": label.task_name,
        "taxonomy": label.taxonomy,
        "label": label.label,
        "rationale": label.rationale,
        "provenance": label.provenance,
        "author": label.author,
        "created_at": label.created_at.isoformat(),
        "confidence": label.confidence,
        "evidence_json": json.dumps(
            [item.model_dump(mode="json") for item in label.evidence],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "source_sha256": label.source_sha256,
        "analysis_id": str(label.analysis_id) if label.analysis_id else None,
        "model_agent": provenance.agent if provenance else None,
        "model_agent_version": provenance.agent_version if provenance else None,
        "model_name": provenance.model if provenance else None,
        "prompt_digest": provenance.prompt_digest if provenance else None,
        "rubric_digest": provenance.rubric_digest if provenance else None,
        "output_schema_digest": provenance.output_schema_digest if provenance else None,
        "model_created_at": provenance.created_at.isoformat() if provenance else None,
        "input_tokens": provenance.input_tokens if provenance else None,
        "output_tokens": provenance.output_tokens if provenance else None,
        "cost_usd": provenance.cost_usd if provenance else None,
    }


def _label_from_row(row: dict[str, Any]) -> BehaviorLabel:
    provenance = None
    if row.get("provenance") == "model":
        provenance = AnalysisProvenance(
            agent=str(row["model_agent"]),
            agent_version=str(row["model_agent_version"]),
            model=str(row["model_name"]),
            prompt_digest=str(row["prompt_digest"]),
            rubric_digest=str(row["rubric_digest"]),
            output_schema_digest=str(row["output_schema_digest"]),
            created_at=datetime.fromisoformat(str(row["model_created_at"])),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cost_usd=row.get("cost_usd"),
        )
    return BehaviorLabel.model_validate(dict(
        schema_version=int(row["schema_version"]),
        label_id=str(row["label_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        job_id=str(row["job_id"]) if row.get("job_id") else None,
        trial_id=str(row["trial_id"]),
        trial_name=str(row["trial_name"]),
        task_name=str(row["task_name"]),
        taxonomy=str(row["taxonomy"]),
        label=str(row["label"]),
        rationale=row.get("rationale"),
        provenance=str(row["provenance"]),
        author=str(row["author"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        confidence=row.get("confidence"),
        evidence=[
            AnalysisEvidenceCitation.model_validate(item)
            for item in json.loads(str(row.get("evidence_json") or "[]"))
        ],
        source_sha256=_normalize_digest(row.get("source_sha256")),
        analysis_id=UUID(str(row["analysis_id"])) if row.get("analysis_id") else None,
        model_provenance=provenance,
    ))


@contextmanager
def _labels_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".behavior_labels.lock"
    with lock_path.open("a+b") as lock_file:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_labels_unlocked(path: Path) -> list[BehaviorLabel]:
    if not path.is_file():
        return []
    return [_label_from_row(row) for row in pq.read_table(path).to_pylist()]


def _write_labels(path: Path, labels: Sequence[BehaviorLabel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(labels, key=lambda item: item.label_id)
    table = pa.Table.from_pylist(
        [_label_row(item) for item in ordered], schema=BEHAVIOR_LABELS_PARQUET_SCHEMA
    )
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as staging_file:
        temporary = Path(staging_file.name)
    try:
        pq.write_table(table, temporary, compression="zstd", use_dictionary=False)
        temporary.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def persist_behavior_label(
    label: BehaviorLabel,
    *,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> BehaviorLabel:
    root = (repo_root or Path.cwd()).resolve()
    path = _canonical_path(root, derived_root)
    with _labels_lock(path, exclusive=True):
        existing = _load_labels_unlocked(path)
        previous = next(
            (item for item in existing if item.label_id == label.label_id), None
        )
        if previous == label:
            return previous
        updated = [item for item in existing if item.label_id != label.label_id]
        updated.append(label)
        _write_labels(path, updated)
        return label


def propose_heuristic_label(outline: TrajectoryOutline) -> BehaviorLabel:
    label = "unclassified"
    rationale = "No specific heuristic triggered"
    if outline.status != "featured":
        label = "missing_data"
        rationale = f"Trajectory unavailable: {outline.unavailable_reason}"
    elif outline.loop_suspicion.detected:
        label = "tool_use_loop"
        rationale = (
            f"Loop suspicion detected ({outline.loop_suspicion.score:.2f}): "
            f"{', '.join(outline.loop_suspicion.reasons)}"
        )
    elif outline.agent_steps == 0:
        label = "setup_failure"
        rationale = "Zero agent execution steps observed"
    elif outline.step_to_first_edit is None and outline.total_tool_calls > 0:
        label = "planning_no_edit"
        rationale = "Tool calls occurred but zero file modifications were performed"
    elif outline.total_errors > 0 and outline.recovery_count > 0 and outline.primary_reward == 1.0:
        label = "recovered_success"
        rationale = f"Recovered from {outline.total_errors} error(s) and completed task"
    elif outline.total_errors > 0 and outline.primary_reward == 0.0:
        label = "unrecovered_error"
        rationale = f"Encountered {outline.total_errors} error(s) without successful completion"
    elif outline.primary_reward == 1.0:
        label = "clean_success"
        rationale = "Task passed without notable errors or loops"
    else:
        label = "failed_verification"
        rationale = "Execution completed but verifier scored zero"
    author = "evallab-heuristic-v1"
    return BehaviorLabel(
        label_id=_label_id(
            target_type="trajectory", target_id=outline.trial_id,
            taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
            provenance="heuristic", author=author,
        ),
        target_type="trajectory",
        target_id=outline.trial_id,
        job_id=outline.job_id,
        trial_id=outline.trial_id,
        trial_name=outline.trial_name,
        task_name=outline.task_name,
        taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
        label=label,
        rationale=rationale,
        provenance="heuristic",
        author=author,
        created_at=datetime.now(UTC),
        source_sha256=_normalize_digest(outline.source_sha256),
    )


def label_from_analysis_sidecar(sidecar: TrialAnalysisSidecar) -> BehaviorLabel:
    output = sidecar.output
    provenance = sidecar.analysis_provenance
    trial_id = str(sidecar.source_trial_id)
    author = f"{provenance.agent}:{provenance.model}"
    return BehaviorLabel(
        label_id=_label_id(
            target_type="trial", target_id=trial_id,
            taxonomy=TRIAL_ANALYSIS_TAXONOMY,
            provenance="model", author=author,
        ),
        target_type="trial",
        target_id=trial_id,
        job_id=str(sidecar.job_id),
        trial_id=trial_id,
        trial_name=Path(sidecar.source_trial_path).name,
        task_name="unknown",
        taxonomy=TRIAL_ANALYSIS_TAXONOMY,
        label=str(output.primary_category),
        rationale=output.summary,
        provenance="model",
        author=author,
        created_at=provenance.created_at,
        confidence=output.confidence,
        evidence=output.evidence,
        source_sha256=_normalize_digest(sidecar.source_digests.trajectory),
        analysis_id=sidecar.analysis_id,
        model_provenance=provenance,
    )


