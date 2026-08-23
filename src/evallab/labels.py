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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.facts import AnalyzerCallable
from evallab.paths import derived_root_from_environment
from evallab.schemas import (
    AnalysisEvidenceCitation,
    AnalysisProvenance,
    BehaviorLabel,
    TrialAnalysisSidecar,
)
from evallab.traj import (
    CONTROL_AGENTS,
    TrajectoryError,
    TrajectoryOutline,
    _resolve_candidate_roots,
    outline_trajectory,
)

LabelProvenance = Literal["human", "heuristic", "model"]
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


@dataclass(frozen=True)
class ReviewQueueItem:
    trial_id: str
    trial_name: str
    job_id: str
    job_name: str
    task_name: str
    agent_name: str
    model_name: str
    reward: float | None
    duration_seconds: float | None
    steps: int
    tool_calls: int
    errors: int
    loop_score: float
    suggested_taxonomy: str | None
    suggestion_reason: str | None
    outline_preview: str
    next_command: str


@dataclass(frozen=True)
class PrecisionReport:
    human_label_count: int
    heuristic_proposal_count: int
    matched_trials_count: int
    exact_taxonomy_matches: int
    precision: float
    disagreements: tuple[dict[str, Any], ...]


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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



def load_behavior_labels(
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> list[BehaviorLabel]:
    root = (repo_root or Path.cwd()).resolve()
    path = _canonical_path(root, derived_root)
    with _labels_lock(path, exclusive=False):
        return _load_labels_unlocked(path)


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


def label_trajectory(
    trial: str | Path,
    label: str,
    note: str | None = None,
    provenance: LabelProvenance = "human",
    author: str = "peter",
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> BehaviorLabel:
    root = (repo_root or Path.cwd()).resolve()
    outline = outline_trajectory(trial, repo_root=root)
    if outline.status != "featured":
        raise TrajectoryError(
            f"cannot label unavailable trajectory {outline.trial_name}: "
            f"{outline.unavailable_reason}"
        )
    normalized = label.strip().lower()
    if not normalized:
        raise TrajectoryError("label must not be empty")
    identity = _label_id(
        target_type="trajectory", target_id=outline.trial_id,
        taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
        provenance=provenance, author=author,
    )
    existing = next(
        (item for item in load_behavior_labels(root, derived_root)
         if item.label_id == identity),
        None,
    )
    created_at = existing.created_at if existing else datetime.now(UTC)
    behavior_label = BehaviorLabel(
        label_id=identity,
        target_type="trajectory",
        target_id=outline.trial_id,
        job_id=outline.job_id,
        trial_id=outline.trial_id,
        trial_name=outline.trial_name,
        task_name=outline.task_name,
        taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
        label=normalized,
        rationale=note.strip() if note else None,
        provenance=provenance,
        author=author,
        created_at=created_at,
        source_sha256=_normalize_digest(outline.source_sha256),
    )
    return persist_behavior_label(
        behavior_label, repo_root=root, derived_root=derived_root
    )


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


def _model_label_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["label", "rationale", "confidence", "evidence"],
        "properties": {
            "label": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["step_id", "supports"],
                    "properties": {
                        "step_id": {"type": "integer", "minimum": 1},
                        "supports": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def _model_confidence(value: object) -> Literal["low", "medium", "high"]:
    if value == "low":
        return "low"
    if value == "medium":
        return "medium"
    if value == "high":
        return "high"
    raise TrajectoryError("model label confidence must be low, medium, or high")


def label_trajectory_with_model(
    outline: TrajectoryOutline,
    *,
    adapter: AnalyzerCallable,
    model: str,
    agent: str,
    agent_version: str,
    rubric_digest: str,
    author: str | None = None,
) -> BehaviorLabel:
    """Call a guarded analyzer and validate one semantic trajectory label."""
    if outline.status != "featured":
        raise TrajectoryError("cannot model-label an unavailable trajectory")
    schema = _model_label_schema()
    prompt_payload = {
        "instruction": (
            "Label the dominant agent behavior. Cite only recorded step IDs; "
            "do not infer unobserved filesystem effects."
        ),
        "trajectory": outline.to_dict(),
    }
    prompt = json.dumps(prompt_payload, sort_keys=True, separators=(",", ":"))
    result = adapter(prompt, schema)
    response = json.loads(result.raw_output)
    if not isinstance(response, dict):
        raise TrajectoryError("model label response must be an object")
    required = {"label", "rationale", "confidence", "evidence"}
    if set(response) != required:
        raise TrajectoryError("model label response fields do not match output schema")
    valid_steps = {step.step_id for step in outline.steps}
    evidence: list[AnalysisEvidenceCitation] = []
    raw_evidence = response["evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise TrajectoryError("model label requires at least one evidence citation")
    for item in raw_evidence:
        if not isinstance(item, dict) or set(item) != {"step_id", "supports"}:
            raise TrajectoryError("invalid model-label evidence citation")
        step_id = item["step_id"]
        if not isinstance(step_id, int) or step_id not in valid_steps:
            raise TrajectoryError(f"model label cites unknown step {step_id!r}")
        evidence.append(AnalysisEvidenceCitation(
            path="agent/trajectory.json", step_id=step_id, supports=str(item["supports"])
        ))
    normalized = str(response["label"]).strip().lower()
    if not normalized:
        raise TrajectoryError("model label must not be empty")
    provenance = AnalysisProvenance(
        agent=agent,
        agent_version=agent_version,
        model=model,
        prompt_digest=_digest_json(prompt_payload),
        rubric_digest=rubric_digest,
        output_schema_digest=_digest_json(schema),
        created_at=datetime.now(UTC),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    label_author = author or f"{agent}:{model}"
    return BehaviorLabel(
        label_id=_label_id(
            target_type="trajectory", target_id=outline.trial_id,
            taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
            provenance="model", author=label_author,
        ),
        target_type="trajectory",
        target_id=outline.trial_id,
        job_id=outline.job_id,
        trial_id=outline.trial_id,
        trial_name=outline.trial_name,
        task_name=outline.task_name,
        taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
        label=normalized,
        rationale=str(response["rationale"]),
        provenance="model",
        author=label_author,
        created_at=provenance.created_at,
        confidence=_model_confidence(response["confidence"]),
        evidence=evidence,
        source_sha256=_normalize_digest(outline.source_sha256),
        model_provenance=provenance,
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


def select_review_queue(
    limit: int = 3,
    runs_roots: Sequence[Path] | None = None,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> list[ReviewQueueItem]:
    if limit <= 0:
        return []
    root = (repo_root or Path.cwd()).resolve()
    candidate_roots = list(runs_roots) if runs_roots else _resolve_candidate_roots(root)
    labels = load_behavior_labels(root, derived_root)
    human_labeled = {
        item.trial_id for item in labels
        if item.provenance == "human" and item.target_type == "trajectory"
    }
    candidates: list[TrajectoryOutline] = []
    discovered: set[Path] = set()
    for candidate_root in candidate_roots:
        if not candidate_root.exists():
            continue
        for job_dir in candidate_root.iterdir():
            if not job_dir.is_dir() or job_dir.name.startswith("."):
                continue
            discovered.update(
                path for path in job_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            )
    for trial_dir in sorted(discovered, key=str):
        try:
            outline = outline_trajectory(
                trial_dir, repo_root=root, explicit_runs_root=trial_dir.parent.parent
            )
        except Exception:
            continue
        if (
            outline.status == "featured"
            and outline.agent_name.lower() not in CONTROL_AGENTS
            and outline.trial_id not in human_labeled
        ):
            candidates.append(outline)
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item.loop_suspicion.score, -item.total_errors,
            item.task_name, item.trial_id,
        ),
    )
    selected: list[TrajectoryOutline] = []
    seen_tasks: set[str] = set()
    for candidate in ordered:
        if candidate.task_name not in seen_tasks:
            selected.append(candidate)
            seen_tasks.add(candidate.task_name)
            if len(selected) == limit:
                break
    for candidate in ordered:
        if len(selected) == limit:
            break
        if candidate not in selected:
            selected.append(candidate)
    items: list[ReviewQueueItem] = []
    for outline in selected:
        heuristic = propose_heuristic_label(outline)
        preview = (
            f"{outline.total_steps} steps, {outline.total_tool_calls} tools, "
            f"{outline.total_errors} errors, loop_score={outline.loop_suspicion.score:.2f}"
        )
        if outline.loop_suspicion.reasons:
            preview += f" | Loop signals: {', '.join(outline.loop_suspicion.reasons)}"
        items.append(ReviewQueueItem(
            trial_id=outline.trial_id,
            trial_name=outline.trial_name,
            job_id=outline.job_id,
            job_name=outline.job_name,
            task_name=outline.task_name,
            agent_name=outline.agent_name,
            model_name=outline.model_name,
            reward=outline.primary_reward,
            duration_seconds=outline.duration_seconds,
            steps=outline.total_steps,
            tool_calls=outline.total_tool_calls,
            errors=outline.total_errors,
            loop_score=outline.loop_suspicion.score,
            suggested_taxonomy=heuristic.label,
            suggestion_reason=heuristic.rationale,
            outline_preview=preview,
            next_command=(
                f"uv run evallab traj label {json.dumps(outline.source_path)} "
                f"{heuristic.label} --note {json.dumps(heuristic.rationale)}"
            ),
        ))
    return items


def evaluate_heuristic_precision(
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> PrecisionReport:
    root = (repo_root or Path.cwd()).resolve()
    labels = load_behavior_labels(root, derived_root)
    human = sorted(
        (
            item for item in labels
            if item.provenance == "human"
            and item.taxonomy == TRAJECTORY_BEHAVIOR_TAXONOMY
        ),
        key=lambda item: (item.trial_id, item.author, item.label_id),
    )
    heuristic = {
        item.trial_id: item for item in labels
        if item.provenance == "heuristic"
        and item.taxonomy == TRAJECTORY_BEHAVIOR_TAXONOMY
    }
    for human_label in human:
        trial_id = human_label.trial_id
        if trial_id not in heuristic:
            with suppress(Exception):
                heuristic[trial_id] = propose_heuristic_label(
                    outline_trajectory(human_label.trial_name or trial_id, repo_root=root)
                )
    matched = 0
    exact = 0
    disagreements: list[dict[str, Any]] = []
    for human_label in human:
        trial_id = human_label.trial_id
        proposal = heuristic.get(trial_id)
        if proposal is None:
            continue
        matched += 1
        if human_label.label == proposal.label:
            exact += 1
        else:
            disagreements.append({
                "trial_id": trial_id,
                "task_name": human_label.task_name,
                "human_label": human_label.label,
                "human_rationale": human_label.rationale,
                "human_author": human_label.author,
                "heuristic_label": proposal.label,
                "heuristic_rationale": proposal.rationale,
            })
    return PrecisionReport(
        human_label_count=len(human),
        heuristic_proposal_count=len(heuristic),
        matched_trials_count=matched,
        exact_taxonomy_matches=exact,
        precision=round(exact / matched, 4) if matched else 0.0,
        disagreements=tuple(disagreements),
    )
