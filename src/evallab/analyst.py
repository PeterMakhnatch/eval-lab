"""ANALYST: Durable agent analysis with stored reasoning trajectories.

Provides an analysis runner with an injectable Analyzer protocol, deterministic
stubbing for tests/CI, token-gated model dispatch, durable JSON storage with
lineage, analyst trajectory capture, and Parquet projection for DuckDB querying.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field, model_validator

from evallab.evidence_store import open_archive
from evallab.lance import TrajectoryWindowV1
from evallab.lineage import compute_file_digest
from evallab.queue import new_ulid
from evallab.schemas import (
    AnalysisRecord,
    ConfidenceClaim,
    ContractModel,
    EvidenceCitation,
)
from evallab.storage.attach import attach
from evallab.storage.paths import (
    derived_root_from_environment,
    shared_checkout_root,
)

ANALYSIS_DIR_NAME = "research/analysis"
DERIVED_ANALYSES_SUBDIR = "analyses"
DERIVED_TRAJECTORIES_SUBDIR = "analyst_trajectories"

# The category set is intentionally local to ANALYST.  AnalysisRecord remains a
# generic storage contract, while this rubric version gives model output a
# closed, reviewable vocabulary at the admission boundary.
ANALYST_RUBRIC_VERSION = "analyst-context-v2"


class AnalystCategory(StrEnum):
    """Closed category vocabulary for :data:`ANALYST_RUBRIC_VERSION`."""

    TASK_EXECUTION_FAILURE = "task_execution_failure"
    PARSER_FAILURE = "parser_failure"
    ASSERTION_ERROR = "assertion_error"
    TIMEOUT = "timeout"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    TOOL_ERROR = "tool_error"
    ENVIRONMENT_FAILURE = "environment_failure"
    TASK_SUCCESS = "task_success"
    CAPABILITY_DEMONSTRATION = "capability_demonstration"
    SPECULATIVE_GUESS = "speculative_guess"
    MODEL_ANALYSIS = "model_analysis"
    # Retain the two values used by the original deterministic multi-perspective
    # tests.  They are explicit rubric categories, not an open string fallback.
    HYPOTHESIS_1 = "hypothesis_1"
    HYPOTHESIS_2 = "hypothesis_2"
    MEMORY_FAILURE = "memory_failure"
    TOOL_COMPOSITION_FAILURE = "tool_composition_failure"
    RECOVERY_FAILURE = "recovery_failure"
    HARNESS_CAPTURE_FAILURE = "harness_capture_failure"
    UNCERTAIN = "uncertain"


ANALYST_CATEGORIES: tuple[str, ...] = tuple(item.value for item in AnalystCategory)


DEFAULT_RUBRIC = f"""# Trial Failure and Capability Analysis Rubric

Rubric version: {ANALYST_RUBRIC_VERSION}

Evaluate the provided agent trial trajectory, task requirements, execution outcome,
and verifier reward.

Verdict requirements:
1. Category: Identify the primary failure mode or capability demonstration.
2. Summary: Clear, concise explanation of what occurred and why.
3. Evidence: Cite specific files and trajectory step indices supporting the finding.
   Every conclusion MUST include at least one concrete citation.
4. Confidence: State confidence level (low, medium, high) and sample backing.
"""


class ModelProviderRefusedError(RuntimeError):
    """Raised when a model is invoked without explicit authorization or model selector."""

    pass


def _deterministic_ulid(identifier: str) -> str:
    """Generate a deterministic valid Crockford base32 ULID from an identifier string."""
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    h = hashlib.sha256(identifier.encode("utf-8")).digest()
    chars = [crockford[h[0] % 8]]
    for i in range(1, 26):
        chars.append(crockford[h[i] % 32])
    return "".join(chars)


@dataclass(frozen=True)
class AnalystResult:
    """Structured verdict returned by an Analyzer implementation."""

    category: str
    summary: str
    evidence: list[EvidenceCitation]
    confidence: ConfidenceClaim
    steps: list[dict[str, Any]] = field(default_factory=list)
    # EvidenceCitation predates event/tool references in the storage schema.
    # Keep those references at the analyst boundary so they can be validated
    # before an AnalysisRecord is constructed.
    citation_metadata: list[dict[str, Any]] = field(default_factory=list)
    contradicting_evidence: list[EvidenceCitation] = field(default_factory=list)
    alternative_explanations: list[str] = field(default_factory=list)


class Analyzer(Protocol):
    """Protocol for trial analysis backends."""

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        """Analyze a trial given a rubric prompt and context payload."""
        ...


class StubAnalyzer:
    """Deterministic stub analyzer for tests, CI, and local verification.

    Never performs network requests or invokes third-party model providers.
    """

    def __init__(
        self,
        category: str = "task_execution_failure",
        summary: str = "Deterministic evaluation: agent execution failed acceptance criteria.",
        evidence: list[EvidenceCitation] | None = None,
        contradicting_evidence: list[EvidenceCitation] | None = None,
        alternative_explanations: list[str] | None = None,
        confidence_level: Literal["low", "medium", "high"] = "high",
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.category = category
        self.summary = summary
        self.evidence = evidence
        self.contradicting_evidence = contradicting_evidence or []
        self.alternative_explanations = alternative_explanations or []
        self.confidence_level = confidence_level
        self.steps = steps

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        evidence = self.evidence
        if evidence is None:
            evidence = [
                EvidenceCitation(path="agent/trajectory.json", step=0),
                EvidenceCitation(path="result.json", step=None),
            ]
        steps = self.steps
        if steps is None:
            steps = [
                {
                    "step_id": 0,
                    "source": "stub",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": "Evaluated trial trajectory and context against rubric",
                }
            ]
        provenance_digest = f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"
        return AnalystResult(
            category=self.category,
            summary=self.summary,
            evidence=evidence,
            confidence=ConfidenceClaim(
                level=self.confidence_level,
                n=1,
                interval=None,
                provenance_digest=provenance_digest,
            ),
            steps=steps,
            contradicting_evidence=self.contradicting_evidence,
            alternative_explanations=self.alternative_explanations,
        )


def _first_json_object(raw_text: str) -> dict[str, Any] | None:
    """Return the first complete JSON object from fenced or annotated model output."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw_text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw_text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


class ModelAnalyzer:
    """Real model-backed analyzer requiring explicit model selector and opt-in."""

    def __init__(
        self,
        model: str | None = None,
        *,
        adapter: Any | None = None,
    ) -> None:
        if not model:
            raise ModelProviderRefusedError(
                "Model analyzer requires an explicit model selector (e.g. --model gpt-4o). "
                "The default analysis path never invokes an external model provider."
            )
        self.model = model
        self.adapter = adapter

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        if self.adapter is None:
            raise ModelProviderRefusedError(
                f"Invoking external model '{self.model}' spends tokens and requires credentials. "
                "Model dispatch is token-gated; default runs use the deterministic stub."
            )
        full_prompt = f"{prompt}\n\n## Context\n\n{context}"
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ANALYST_CATEGORIES},
                "summary": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "step": {"type": ["integer", "null"]},
                            "event_id": {"type": ["string", "null"]},
                            "tool_call_id": {"type": ["string", "null"]},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
                "contradicting_evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "step": {"type": ["integer", "null"]},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
                "alternative_explanations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
            },
            "required": ["category", "summary"],
            "additionalProperties": False,
        }

        if callable(self.adapter):
            call_result = self.adapter(full_prompt, schema)
        else:
            raise ModelProviderRefusedError(f"Injected adapter for '{self.model}' is not callable.")

        category = "model_analysis"
        raw_text = getattr(call_result, "raw_output", str(call_result))
        summary = raw_text.strip()
        evidence: list[EvidenceCitation] = []
        citation_metadata: list[dict[str, Any]] = []
        contradicting_evidence: list[EvidenceCitation] = []
        alternative_explanations: list[str] = []
        confidence_level: Literal["low", "medium", "high"] = "medium"

        try:
            parsed = _first_json_object(raw_text)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("category"), str) and parsed["category"].strip():
                    category = parsed["category"].strip()
                if isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
                    summary = parsed["summary"].strip()
                if isinstance(parsed.get("evidence"), list):
                    for item in parsed["evidence"]:
                        if isinstance(item, dict) and "path" in item:
                            evidence.append(
                                EvidenceCitation(
                                    path=str(item["path"]),
                                    step=(
                                        int(item["step"]) if item.get("step") is not None else None
                                    ),
                                )
                            )
                            citation_metadata.append(
                                {
                                    key: item[key]
                                    for key in ("event_id", "tool_call_id")
                                    if item.get(key) is not None
                                }
                            )
                if isinstance(parsed.get("contradicting_evidence"), list):
                    for item in parsed["contradicting_evidence"]:
                        if isinstance(item, dict) and "path" in item:
                            contradicting_evidence.append(
                                EvidenceCitation(
                                    path=str(item["path"]),
                                    step=(
                                        int(item["step"]) if item.get("step") is not None else None
                                    ),
                                )
                            )
                if isinstance(parsed.get("alternative_explanations"), list):
                    alternative_explanations = [
                        str(item)
                        for item in parsed["alternative_explanations"]
                        if isinstance(item, str) and item.strip()
                    ]
                conf = parsed.get("confidence")
                if isinstance(conf, str) and conf in {"low", "medium", "high"}:
                    confidence_level = conf  # type: ignore[assignment]
                elif isinstance(conf, dict) and conf.get("level") in {"low", "medium", "high"}:
                    confidence_level = conf["level"]  # type: ignore[assignment]
        except Exception:
            pass

        if not evidence:
            evidence = [
                EvidenceCitation(path="agent/trajectory.json", step=0),
                EvidenceCitation(path="result.json", step=None),
            ]

        provenance_digest = f"sha256:{hashlib.sha256(full_prompt.encode('utf-8')).hexdigest()}"
        steps = [
            {
                "step_id": 0,
                "source": "model_adapter",
                "timestamp": datetime.now(UTC).isoformat(),
                "message": f"Completed model analysis with {self.model}",
                "model": self.model,
                "argv": getattr(call_result, "argv", []),
                "transport": getattr(call_result, "transport", "unknown"),
            }
        ]

        return AnalystResult(
            category=category,
            summary=summary,
            evidence=evidence,
            confidence=ConfidenceClaim(
                level=confidence_level,
                n=1,
                interval=None,
                provenance_digest=provenance_digest,
            ),
            steps=steps,
            citation_metadata=citation_metadata,
            contradicting_evidence=contradicting_evidence,
            alternative_explanations=alternative_explanations,
        )


class JudgeStage(StrEnum):
    TRIAGE = "triage"
    INSPECT = "inspect"
    FINAL = "final"


class JudgeWindowCitationV1(ContractModel):
    schema_version: Literal["judge-window-citation/v1"] = "judge-window-citation/v1"
    window_digest: str
    trial_id: str
    start_step: int = Field(ge=0)
    end_step: int = Field(ge=0)
    stance: Literal["supports", "contradicts"]


class JudgeDeterministicContextV1(ContractModel):
    schema_version: Literal["judge-deterministic-context/v1"] = "judge-deterministic-context/v1"
    context_digest: str
    snapshot_digest: str
    source_digest: str
    facts: dict[str, Any]
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_context(self) -> JudgeDeterministicContextV1:
        body = self.model_dump(mode="json", exclude={"context_digest"})
        if self.context_digest != _judge_digest(body):
            raise ValueError("deterministic judge context digest mismatch")
        return self


class JudgeStageResultV1(ContractModel):
    schema_version: Literal["judge-stage-result/v1"] = "judge-stage-result/v1"
    stage: JudgeStage
    stage_digest: str
    category: str
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_citations: tuple[JudgeWindowCitationV1, ...]
    contradicting_citations: tuple[JudgeWindowCitationV1, ...] = ()
    alternative_explanations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_stage(self) -> JudgeStageResultV1:
        if self.category not in ANALYST_CATEGORIES:
            raise ValueError(f"unsupported trajectory judge category: {self.category}")
        if not self.supporting_citations:
            raise ValueError("trajectory judge stage requires supporting citations")
        if self.stage == JudgeStage.FINAL:
            if not self.contradicting_citations:
                raise ValueError("final judge stage requires contradicting citations")
            if not self.alternative_explanations:
                raise ValueError("final judge stage requires alternative explanations")
        body = self.model_dump(mode="json", exclude={"stage_digest"})
        expected = _judge_digest(body)
        if self.stage_digest != expected:
            raise ValueError("judge stage digest does not match canonical content")
        return self


class TrajectoryJudgeRunV1(ContractModel):
    schema_version: Literal["trajectory-judge-run/v1"] = "trajectory-judge-run/v1"
    run_digest: str
    snapshot_digest: str
    judge_model: str
    rubric_digest: str
    repeat_index: int = Field(ge=0)
    input_window_digests: tuple[str, ...]
    deterministic_context_digest: str | None = None
    stages: tuple[JudgeStageResultV1, ...]
    final_category: str
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_run(self) -> TrajectoryJudgeRunV1:
        if tuple(stage.stage for stage in self.stages) != (
            JudgeStage.TRIAGE,
            JudgeStage.INSPECT,
            JudgeStage.FINAL,
        ):
            raise ValueError("trajectory judge requires triage, inspect, and final stages")
        if self.final_category != self.stages[-1].category:
            raise ValueError("final category must match the final judge stage")
        body = self.model_dump(mode="json", exclude={"run_digest"})
        if self.run_digest != _judge_digest(body):
            raise ValueError("judge run digest does not match canonical content")
        return self


class JudgeDisagreementV1(ContractModel):
    schema_version: Literal["judge-disagreement/v1"] = "judge-disagreement/v1"
    disagreement_digest: str
    snapshot_digest: str
    run_digests: tuple[str, ...]
    category_counts: dict[str, int]
    consensus_category: str | None
    agreement_rate: float = Field(ge=0.0, le=1.0)
    unresolved: bool
    decision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_disagreement(self) -> JudgeDisagreementV1:
        if sum(self.category_counts.values()) != len(self.run_digests):
            raise ValueError("category counts must reconcile judge runs")
        body = self.model_dump(mode="json", exclude={"disagreement_digest"})
        if self.disagreement_digest != _judge_digest(body):
            raise ValueError("judge disagreement digest does not match canonical content")
        return self


def _judge_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def create_judge_deterministic_context(
    *,
    snapshot_digest: str,
    source_digest: str,
    facts: Mapping[str, Any],
) -> JudgeDeterministicContextV1:
    """Bind verifier and mechanical facts supplied to every judge stage."""
    body = {
        "schema_version": "judge-deterministic-context/v1",
        "snapshot_digest": snapshot_digest,
        "source_digest": source_digest,
        "facts": dict(facts),
        "decision_eligible": False,
    }
    return JudgeDeterministicContextV1.model_validate(
        {**body, "context_digest": _judge_digest(body)}
    )


def _judge_context(
    windows: Sequence[TrajectoryWindowV1],
    prior: str = "",
    deterministic_context: JudgeDeterministicContextV1 | None = None,
) -> str:
    rows = []
    if deterministic_context is not None:
        rows.append(
            "Authoritative deterministic facts "
            f"[{deterministic_context.context_digest}]:\n"
            + json.dumps(
                deterministic_context.facts,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if prior:
        rows.append(f"Prior stage findings:\n{prior}")
    for window in windows:
        rows.append(
            f"[{window.window_digest}] trial={window.trial_id} "
            f"steps={window.start_step}-{window.end_step}\n{window.text}"
        )
    return "\n\n".join(rows)


def _confidence_value(claim: ConfidenceClaim) -> float:
    return {"low": 0.3, "medium": 0.6, "high": 0.9}[claim.level]


def _citation_from_evidence(
    citation: EvidenceCitation,
    windows: dict[str, TrajectoryWindowV1],
    stance: Literal["supports", "contradicts"],
) -> JudgeWindowCitationV1:
    normalized_path = citation.path.strip()
    if normalized_path.startswith("[") and normalized_path.endswith("]"):
        normalized_path = normalized_path[1:-1].strip()
    window = windows.get(normalized_path)
    if window is None:
        raise ValueError("trajectory judge citations must use an exact input window_digest as path")
    return JudgeWindowCitationV1(
        window_digest=window.window_digest,
        trial_id=window.trial_id,
        start_step=window.start_step,
        end_step=window.end_step,
        stance=stance,
    )


def _judge_stage_result(
    stage: JudgeStage,
    result: AnalystResult,
    windows: Sequence[TrajectoryWindowV1],
) -> JudgeStageResultV1:
    by_digest = {window.window_digest: window for window in windows}
    supporting = tuple(
        _citation_from_evidence(citation, by_digest, "supports") for citation in result.evidence
    )
    contradicting = tuple(
        _citation_from_evidence(citation, by_digest, "contradicts")
        for citation in result.contradicting_evidence
    )
    body = {
        "schema_version": "judge-stage-result/v1",
        "stage": stage,
        "category": result.category,
        "summary": result.summary,
        "confidence": _confidence_value(result.confidence),
        "supporting_citations": [citation.model_dump(mode="json") for citation in supporting],
        "contradicting_citations": [citation.model_dump(mode="json") for citation in contradicting],
        "alternative_explanations": tuple(result.alternative_explanations),
    }
    return JudgeStageResultV1.model_validate(
        {
            **body,
            "stage_digest": _judge_digest(body),
        }
    )


def _analyze_judge_stage(
    analyzer: Analyzer,
    stage: JudgeStage,
    windows: Sequence[TrajectoryWindowV1],
    *,
    prompt: str,
    context: str,
    attempts: int = 2,
) -> JudgeStageResultV1:
    """Retry a stage only when its structured or citation contract is invalid."""
    allowed = ", ".join(window.window_digest for window in windows)
    current_prompt = (
        prompt + "\nAllowed evidence.path values (copy exactly, without brackets): " + allowed
    )
    last_error: ValueError | None = None
    for attempt in range(attempts):
        raw = analyzer.analyze(current_prompt, context)
        try:
            return _judge_stage_result(stage, raw, windows)
        except ValueError as exc:
            last_error = exc
            current_prompt = (
                prompt
                + "\nYour previous stage output violated this contract: "
                + str(exc)
                + "\nReturn a corrected result. Allowed evidence.path values: "
                + allowed
                + f"\nCitation repair attempt {attempt + 1}."
            )
    assert last_error is not None
    raise last_error


def run_trajectory_judge(
    analyzer: Analyzer,
    windows: Sequence[TrajectoryWindowV1],
    *,
    rubric: str,
    deterministic_context: JudgeDeterministicContextV1 | None = None,
    repeats: int = 3,
) -> tuple[tuple[TrajectoryJudgeRunV1, ...], JudgeDisagreementV1]:
    """Run TRACE-style triage, inspection, and final judgment with repeated calls."""
    if not windows or repeats <= 0:
        raise ValueError("trajectory judge requires windows and positive repeats")
    snapshots = {window.snapshot_digest for window in windows}
    if len(snapshots) != 1:
        raise ValueError("trajectory judge cannot mix snapshot vintages")
    snapshot_digest = next(iter(snapshots))
    if (
        deterministic_context is not None
        and deterministic_context.snapshot_digest != snapshot_digest
    ):
        raise ValueError("deterministic judge context snapshot does not match windows")
    rubric_digest = _judge_digest({"rubric": rubric})
    judge_model = str(getattr(analyzer, "model", analyzer.__class__.__name__))
    runs = []
    for repeat_index in range(repeats):
        triage = _analyze_judge_stage(
            analyzer,
            JudgeStage.TRIAGE,
            windows,
            prompt=(
                rubric
                + "\nStage: TRIAGE. Select high-signal windows. Cite window digests in evidence.path."
            ),
            context=_judge_context(
                windows,
                deterministic_context=deterministic_context,
            ),
        )
        selected_digests = {citation.window_digest for citation in triage.supporting_citations}
        selected = [window for window in windows if window.window_digest in selected_digests]
        inspect = _analyze_judge_stage(
            analyzer,
            JudgeStage.INSPECT,
            selected,
            prompt=(
                rubric
                + "\nStage: INSPECT. Analyze the selected evidence and cite exact window digests."
            ),
            context=_judge_context(
                selected,
                triage.summary,
                deterministic_context,
            ),
        )
        # Final synthesis sees the full frozen pool so it can cite counterevidence
        # outside the triaged subset while retaining the inspection summary.
        final_windows = list(windows)
        final = _analyze_judge_stage(
            analyzer,
            JudgeStage.FINAL,
            final_windows,
            prompt=(
                rubric
                + "\nStage: FINAL. Give a category, supporting and contradicting window citations, "
                "and at least one alternative explanation."
            ),
            context=_judge_context(
                final_windows,
                f"Triage: {triage.summary}\nInspect: {inspect.summary}",
                deterministic_context,
            ),
        )
        stages = (triage, inspect, final)
        body = {
            "schema_version": "trajectory-judge-run/v1",
            "snapshot_digest": snapshot_digest,
            "judge_model": judge_model,
            "rubric_digest": rubric_digest,
            "repeat_index": repeat_index,
            "input_window_digests": tuple(window.window_digest for window in windows),
            "deterministic_context_digest": (
                deterministic_context.context_digest if deterministic_context is not None else None
            ),
            "stages": [stage.model_dump(mode="json") for stage in stages],
            "final_category": final.category,
            "decision_eligible": False,
        }
        runs.append(
            TrajectoryJudgeRunV1.model_validate(
                {
                    **body,
                    "run_digest": _judge_digest(body),
                }
            )
        )
    counts = Counter(run.final_category for run in runs)
    max_count = max(counts.values())
    winners = sorted(category for category, count in counts.items() if count == max_count)
    consensus = winners[0] if len(winners) == 1 else None
    disagreement_body = {
        "schema_version": "judge-disagreement/v1",
        "snapshot_digest": snapshot_digest,
        "run_digests": tuple(run.run_digest for run in runs),
        "category_counts": dict(sorted(counts.items())),
        "consensus_category": consensus,
        "agreement_rate": max_count / len(runs),
        "unresolved": consensus is None or len(counts) > 1,
        "decision_eligible": False,
    }
    disagreement = JudgeDisagreementV1.model_validate(
        {
            **disagreement_body,
            "disagreement_digest": _judge_digest(disagreement_body),
        }
    )
    return tuple(runs), disagreement


def _resolve_runs_roots(repo_root: Path, runs_root: Path | None = None) -> list[Path]:
    """Resolve ordered candidate roots for locating raw trial runs."""
    if runs_root is not None:
        return [runs_root.resolve()]
    env = os.environ.get("EVALLAB_RUNS_ROOT")
    if env:
        return [Path(env).resolve()]
    primary = shared_checkout_root(repo_root)
    candidates = [
        repo_root / "runs",
        repo_root / "research/evidence/runs",
        repo_root / "evidence/runs",
        primary / "runs",
        primary / "research/evidence/runs",
        primary / "evidence/runs",
    ]
    seen: set[Path] = set()
    roots: list[Path] = []
    for c in candidates:
        rc = c.resolve()
        if rc not in seen and rc.exists():
            seen.add(rc)
            roots.append(c)
    if not roots:
        roots = [repo_root / "runs", primary / "runs"]
    return roots


def _load_trajectory_steps(path: Path) -> list[dict[str, Any]]:
    """Load raw trajectory steps from a trajectory.json file if present."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return _steps_from_payload(data)


def _steps_from_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("steps"), list):
        return [step for step in data["steps"] if isinstance(step, dict)]
    return []


def _load_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalized_archive_member(name: str) -> str:
    normalized = name
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid CAS evidence member path: {name}")
    return path.as_posix()


def _load_verified_cas_members(store_root: Path, uri: str) -> dict[str, bytes]:
    """Read and content-verify a CAS archive without extracting it."""
    expected_digest = f"sha256:{uri.removeprefix('cas://sha256/')}"
    members: dict[str, bytes] = {}
    with (
        open_archive(store_root, uri) as blob,
        tarfile.open(
            fileobj=blob,
            mode="r:gz",
        ) as archive,
    ):
        entries: list[tuple[str, tarfile.TarInfo]] = []
        for member in archive.getmembers():
            if not member.isfile():
                raise ValueError(f"CAS evidence archive contains non-file member: {member.name}")
            entries.append((_normalized_archive_member(member.name), member))
        digest = hashlib.sha256()
        for name, member in sorted(entries, key=lambda item: item[0]):
            if name in members:
                raise ValueError(f"duplicate CAS evidence member: {name}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read CAS evidence member: {member.name}")
            content = source.read()
            members[name] = content
            encoded_name = name.encode()
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    actual_digest = f"sha256:{digest.hexdigest()}"
    if actual_digest != expected_digest:
        raise ValueError(
            f"CAS evidence digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
    return members


def _select_cas_member(
    members: Mapping[str, bytes],
    names: tuple[str, ...],
) -> tuple[str, bytes] | None:
    for name in names:
        if name in members:
            return name, members[name]
        matches = sorted(
            (member_name, content)
            for member_name, content in members.items()
            if member_name.endswith("/" + name)
        )
        if len(matches) > 1:
            raise ValueError(f"ambiguous CAS evidence member: {name}")
        if matches:
            return matches[0]
    return None


def _cas_uri_for_trial(
    trial_identifier: str,
    trial_row: dict[str, Any] | None,
    store_root: Path,
) -> str | None:
    targets = {trial_identifier}
    if trial_row:
        targets.update(str(trial_row.get(key)) for key in ("trial_id", "trial_name", "job_id"))
    records_root = store_root / "records"
    if not records_root.is_dir():
        return None
    for manifest_path in sorted(records_root.rglob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(manifest.get("record_id")) in targets:
            uri = manifest.get("uri")
            if isinstance(uri, str):
                return uri
    return None


@dataclass(frozen=True)
class TrialData:
    """Resolved trial metadata and raw artifact references."""

    trial_id: str
    job_id: str
    job_name: str
    trial_name: str
    task_name: str
    primary_reward: float | None
    exception_class: str | None
    agent_name: str
    model_name: str
    trajectory_path: Path | None
    result_path: Path | None
    trajectory_steps: list[dict[str, Any]]
    inputs: list[dict[str, Any]]
    result_payload: dict[str, Any] = field(default_factory=dict)
    cas_uri: str | None = None


def resolve_trial(
    trial_identifier: str,
    repo_root: Path,
    *,
    explicit_derived: Path | None = None,
    runs_root: Path | None = None,
    evidence_store_root: Path | None = None,
    cas_uri: str | None = None,
) -> TrialData:
    """Resolve trial metadata and, when explicitly requested, hydrate CAS bytes."""
    att = attach(repo_root=repo_root, explicit_derived=explicit_derived)
    trial_row: dict[str, Any] | None = None
    try:
        try:
            cur = att.connection.execute(
                "SELECT trial_id, job_id, job_name, trial_name, task_name, primary_reward, "
                "exception_class, agent_name, model_name FROM trial_facts "
                "WHERE trial_id = ? OR trial_name = ? LIMIT 1",
                (trial_identifier, trial_identifier),
            )
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            if rows:
                trial_row = dict(zip(cols, rows[0], strict=False))
        except Exception:
            pass
    finally:
        att.connection.close()

    candidate_roots = _resolve_runs_roots(repo_root, runs_root)
    found_trial_dir: Path | None = None
    found_traj: Path | None = None
    found_result: Path | None = None
    for root in candidate_roots:
        if not root.exists():
            continue
        direct_target = root / trial_identifier
        if direct_target.is_dir():
            found_trial_dir = direct_target
            break
        for job_dir in root.iterdir():
            if not job_dir.is_dir():
                continue
            trial_dir = job_dir / trial_identifier
            if trial_dir.is_dir():
                found_trial_dir = trial_dir
                break
            if trial_row and job_dir.name == trial_row.get("job_name"):
                trial_dir = job_dir / str(trial_row.get("trial_name"))
                if trial_dir.is_dir():
                    found_trial_dir = trial_dir
                    break
        if found_trial_dir is not None:
            break

    if found_trial_dir is not None:
        t_path = found_trial_dir / "agent" / "trajectory.json"
        if not t_path.is_file():
            t_path = found_trial_dir / "trajectory.json"
        if t_path.is_file():
            found_traj = t_path
        r_path = found_trial_dir / "result.json"
        if r_path.is_file():
            found_result = r_path

    # A CAS URI is an explicit source selection.  It never silently falls back
    # to an absent filesystem artifact, and hydration is read-only.
    store_root = evidence_store_root
    if store_root is None and os.environ.get("EVALLAB_EVIDENCE_STORE_ROOT"):
        store_root = Path(os.environ["EVALLAB_EVIDENCE_STORE_ROOT"])
    if cas_uri is None and store_root is not None and found_traj is None and found_result is None:
        cas_uri = _cas_uri_for_trial(trial_identifier, trial_row, store_root)
    hydrated_steps: list[dict[str, Any]] | None = None
    hydrated_result: dict[str, Any] | None = None
    trajectory_member: tuple[str, bytes] | None = None
    result_member: tuple[str, bytes] | None = None
    if cas_uri is not None:
        if store_root is None:
            raise ValueError("CAS hydration requested without an evidence store root")
        members = _load_verified_cas_members(store_root, cas_uri)
        trajectory_member = _select_cas_member(
            members,
            ("agent/trajectory.json", "trajectory.json"),
        )
        result_member = _select_cas_member(members, ("result.json",))
        if trajectory_member is None and result_member is None:
            raise FileNotFoundError(f"CAS evidence has no trajectory or result: {cas_uri}")
        if trajectory_member is not None:
            try:
                hydrated_steps = _steps_from_payload(json.loads(trajectory_member[1]))
            except Exception as exc:
                raise ValueError(f"CAS trajectory is not valid JSON: {cas_uri}") from exc
        if result_member is not None:
            try:
                parsed_result = json.loads(result_member[1])
            except Exception as exc:
                raise ValueError(f"CAS result is not valid JSON: {cas_uri}") from exc
            if not isinstance(parsed_result, dict):
                raise ValueError(f"CAS result is not a JSON object: {cas_uri}")
            hydrated_result = parsed_result
        found_traj = None
        found_result = None

    steps = (
        hydrated_steps
        if hydrated_steps is not None
        else _load_trajectory_steps(found_traj)
        if found_traj
        else []
    )
    result_payload = (
        hydrated_result if hydrated_result is not None else _load_json_object(found_result)
    )

    inputs: list[dict[str, Any]] = []
    if cas_uri is not None:
        content_digest = f"sha256:{cas_uri.removeprefix('cas://sha256/')}"
        for selected in (trajectory_member, result_member):
            if selected is None:
                continue
            member_name, member_content = selected
            inputs.append(
                {
                    "path": cas_uri,
                    "member": member_name,
                    "digest": f"sha256:{hashlib.sha256(member_content).hexdigest()}",
                    "content_digest": content_digest,
                }
            )
    else:
        if found_traj and found_traj.is_file():
            inputs.append(
                {
                    "path": str(found_traj.relative_to(repo_root))
                    if found_traj.is_relative_to(repo_root)
                    else found_traj.as_posix(),
                    "digest": compute_file_digest(found_traj),
                }
            )
        if found_result and found_result.is_file():
            inputs.append(
                {
                    "path": str(found_result.relative_to(repo_root))
                    if found_result.is_relative_to(repo_root)
                    else found_result.as_posix(),
                    "digest": compute_file_digest(found_result),
                }
            )

    raw_trial_id = str(trial_row.get("trial_id") if trial_row else trial_identifier)
    trial_id = (
        raw_trial_id
        if len(raw_trial_id) == 26 and raw_trial_id[0] in "01234567"
        else _deterministic_ulid(raw_trial_id)
    )
    job_id = str(trial_row.get("job_id") if trial_row else new_ulid())
    job_name = str(
        trial_row.get("job_name")
        if trial_row
        else (found_trial_dir.parent.name if found_trial_dir else "unknown_job")
    )
    trial_name = str(
        trial_row.get("trial_name")
        if trial_row
        else (found_trial_dir.name if found_trial_dir else trial_identifier)
    )
    task_name = str(trial_row.get("task_name") if trial_row else trial_name.split("__")[0])
    reward = trial_row.get("primary_reward") if trial_row else result_payload.get("primary_reward")
    exception = (
        trial_row.get("exception_class") if trial_row else result_payload.get("exception_class")
    )
    agent_name = str(trial_row.get("agent_name") if trial_row else "unknown_agent")
    model_name = str(trial_row.get("model_name") if trial_row else "unknown_model")

    return TrialData(
        trial_id=trial_id,
        job_id=job_id,
        job_name=job_name,
        trial_name=trial_name,
        task_name=task_name,
        primary_reward=reward,
        exception_class=exception,
        agent_name=agent_name,
        model_name=model_name,
        trajectory_path=found_traj,
        result_path=found_result,
        trajectory_steps=steps,
        inputs=inputs,
        result_payload=result_payload,
        cas_uri=cas_uri,
    )


MAX_ORDINARY_STEPS = 24
MAX_ORDINARY_STEP_CHARS = 240
_ERROR_KEYS = frozenset(
    {"stderr", "traceback", "error", "exception", "exception_info", "error_message", "error_type"}
)


def _step_error(step: Mapping[str, Any]) -> bool:
    def walk(value: Any, key: str = "") -> bool:
        if key.lower() in _ERROR_KEYS and value not in (None, "", [], {}):
            return True
        if isinstance(value, Mapping):
            return any(walk(child, str(child_key)) for child_key, child in value.items())
        if isinstance(value, list):
            return any(walk(child, key) for child in value)
        return key.lower() in {"exit_code", "returncode"} and value not in (None, 0)

    return walk(step)


def _error_lines(value: Any, prefix: str = "Error") -> list[str]:
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = value[key]
            if str(key).lower() in _ERROR_KEYS and child not in (None, "", [], {}):
                rendered = (
                    child
                    if isinstance(child, str)
                    else json.dumps(child, sort_keys=True, default=str)
                )
                lines.append(f"{prefix} {key}: {rendered}")
            elif isinstance(child, (Mapping, list)):
                lines.extend(_error_lines(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            lines.extend(_error_lines(child, f"{prefix}[{index}]"))
    return lines


def _citation_field(citation: Any, name: str, default: Any = None) -> Any:
    if isinstance(citation, Mapping):
        return citation.get(name, default)
    return getattr(citation, name, default)


def _normalize_citation_path(path: str) -> str:
    if "\\" in path:
        raise ValueError(f"Analysis rejected: invalid citation path {path!r}")
    normalized = path
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parsed = PurePosixPath(normalized)
    if (
        not normalized
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise ValueError(f"Analysis rejected: invalid citation path {path!r}")
    return parsed.as_posix()


def _available_citation_paths(trial: TrialData) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    trajectory_paths: set[str] = set()
    for input_record in trial.inputs:
        member = input_record.get("member")
        raw_path = member if isinstance(member, str) else input_record.get("path")
        if not isinstance(raw_path, str):
            continue
        canonical_path: str | None = None
        with suppress(ValueError):
            canonical_path = _normalize_citation_path(raw_path)
        is_trajectory = raw_path.endswith("trajectory.json")
        is_result = raw_path.endswith("result.json")
        if canonical_path is None:
            if is_trajectory:
                canonical_path = "agent/trajectory.json"
            elif is_result:
                canonical_path = "result.json"
            else:
                continue
        aliases[canonical_path] = canonical_path
        if is_trajectory:
            aliases["trajectory.json"] = canonical_path
            aliases["agent/trajectory.json"] = canonical_path
            trajectory_paths.add(canonical_path)
        elif is_result:
            aliases["result.json"] = canonical_path
    return aliases, trajectory_paths


def _step_reference_ids(step: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    event_ids: set[str] = set()
    tool_call_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "event_id" and child is not None:
                    event_ids.add(str(child))
                elif key in {"tool_call_id", "source_call_id", "call_id"} and child is not None:
                    tool_call_ids.add(str(child))
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(step)
    return event_ids, tool_call_ids


def _validate_result(
    result: AnalystResult,
    trial: TrialData,
) -> list[EvidenceCitation]:
    try:
        AnalystCategory(result.category)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Analysis rejected: category {result.category!r} is not in "
            f"{ANALYST_RUBRIC_VERSION} enum"
        ) from exc
    if not result.evidence:
        raise ValueError(
            "Analysis rejected: conclusion has no cited evidence. "
            "Every claim must cite concrete artifacts/steps."
        )

    available_paths, trajectory_paths = _available_citation_paths(trial)
    steps_by_id: dict[int, Mapping[str, Any]] = {}
    for index, step_payload in enumerate(trial.trajectory_steps):
        steps_by_id.setdefault(index, step_payload)
        step_id = step_payload.get("step_id")
        if isinstance(step_id, int) and not isinstance(step_id, bool):
            steps_by_id[step_id] = step_payload

    normalized: list[EvidenceCitation] = []
    for index, citation in enumerate(result.evidence):
        path = _citation_field(citation, "path")
        step = _citation_field(citation, "step")
        event_id = _citation_field(citation, "event_id")
        tool_call_id = _citation_field(citation, "tool_call_id")
        if index < len(result.citation_metadata):
            metadata = result.citation_metadata[index]
            event_id = metadata.get("event_id", event_id)
            tool_call_id = metadata.get("tool_call_id", tool_call_id)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Analysis rejected: citation path is empty")
        normalized_path = _normalize_citation_path(path.strip())
        resolved_path = available_paths.get(normalized_path)
        if resolved_path is None:
            raise ValueError(
                f"Analysis rejected: citation path is not a resolved trial input: {normalized_path}"
            )
        if step is None:
            if event_id is not None or tool_call_id is not None:
                raise ValueError("Analysis rejected: event/tool citation requires a cited step")
            normalized.append(EvidenceCitation(path=resolved_path, step=None))
            continue
        if not isinstance(step, int) or isinstance(step, bool) or step not in steps_by_id:
            raise ValueError(f"Analysis rejected: nonexistent step citation {step!r}")
        if resolved_path not in trajectory_paths:
            raise ValueError(
                f"Analysis rejected: step citation targets non-trajectory input {resolved_path}"
            )
        step_event_ids, step_tool_call_ids = _step_reference_ids(steps_by_id[step])
        if event_id is not None and str(event_id) not in step_event_ids:
            raise ValueError(
                f"Analysis rejected: nonexistent event citation {event_id!r} at step {step}"
            )
        if tool_call_id is not None and str(tool_call_id) not in step_tool_call_ids:
            raise ValueError(
                f"Analysis rejected: nonexistent tool_call citation {tool_call_id!r} at step {step}"
            )
        normalized.append(EvidenceCitation(path=resolved_path, step=step))
    return normalized


def assemble_context(trial: TrialData) -> tuple[str, str]:
    """Build deterministic, error-adaptive context without mutating raw artifacts."""
    prompt = DEFAULT_RUBRIC
    context_lines = [
        f"Trial ID: {trial.trial_id}",
        f"Job Name: {trial.job_name}",
        f"Trial Name: {trial.trial_name}",
        f"Task: {trial.task_name}",
        f"Agent: {trial.agent_name}",
        f"Evaluated Model: {trial.model_name}",
        f"Primary Reward: {trial.primary_reward}",
        f"Exception: {trial.exception_class or 'None'}",
        f"Total Steps: {len(trial.trajectory_steps)}",
        "",
        "## Trajectory Summary:",
    ]
    ordinary_total = 0
    ordinary_included = 0
    error_lines: list[str] = []
    for index, step in enumerate(trial.trajectory_steps):
        if _step_error(step):
            step_id = step.get("step_id", index)
            error_lines.append(f"Error Step {step_id}:")
            error_lines.extend(_error_lines(step, f"Step {step_id}"))
            if not any(line.startswith(f"Step {step_id} ") for line in error_lines):
                message = step.get("message")
                if message:
                    error_lines.append(f"Step {step_id} message: {message}")
            continue
        ordinary_total += 1
        if ordinary_included >= MAX_ORDINARY_STEPS:
            continue
        ordinary_included += 1
        source = step.get("source", "agent")
        msg = step.get("message")
        msg_str = (
            json.dumps(msg, sort_keys=True, default=str)
            if isinstance(msg, dict)
            else str(msg or "")
        )
        bounded = (
            (msg_str[:MAX_ORDINARY_STEP_CHARS] + "...")
            if len(msg_str) > MAX_ORDINARY_STEP_CHARS
            else msg_str
        )
        context_lines.append(f"Step {step.get('step_id', index)} [{source}]: {bounded}")

    if error_lines or trial.result_payload:
        context_lines.extend(["", "## Complete Error Evidence:"])
        context_lines.extend(error_lines)
        context_lines.extend(_error_lines(trial.result_payload, "Result"))
    if ordinary_total > ordinary_included:
        context_lines.append(
            f"[ordinary steps bounded: included {ordinary_included} of {ordinary_total}]"
        )
    context = "\n".join(context_lines)
    return prompt, context


def run_analysis(
    trial_identifier: str,
    *,
    analyzer: Analyzer | None = None,
    model: str | None = None,
    adapter: Any | None = None,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
    runs_root: Path | None = None,
    evidence_store_root: Path | None = None,
    cas_uri: str | None = None,
    analysis_id: str | None = None,
    analysis_role: Literal[
        "trial_review", "review_queue_review", "counterexample_review"
    ] = "trial_review",
    source_manifest_digest: str | None = None,
    source_snapshot_digest: str | None = None,
    source_queue_digest: str | None = None,
) -> tuple[AnalysisRecord, dict[str, Any], Path, Path]:
    """Execute analysis on a trial, validate evidence, and store artifacts.

    Re-running analysis for the same trial generates a fresh analysis_id and
    persists both records without overwriting.

    Raises:
        ModelProviderRefusedError: If a model call is attempted without explicit selector.
        ValueError: If the analyzer returns a verdict with empty evidence.
    """
    root = repo_root or Path.cwd()
    derived = derived_root_from_environment(root, explicit=derived_root)

    # 1. Resolve analyzer
    if analyzer is None:
        analyzer = (
            ModelAnalyzer(model=model, adapter=adapter) if model is not None else StubAnalyzer()
        )
    # Track analyst's own trajectory steps
    analyst_steps: list[dict[str, Any]] = []
    t_start = datetime.now(UTC).isoformat()
    analyst_steps.append(
        {
            "step_id": 0,
            "source": "attacher",
            "timestamp": t_start,
            "message": f"Attached unified surface and resolved trial '{trial_identifier}'",
        }
    )
    trial = resolve_trial(
        trial_identifier,
        root,
        explicit_derived=derived,
        runs_root=runs_root,
        evidence_store_root=evidence_store_root,
        cas_uri=cas_uri,
    )
    analyst_steps.append(
        {
            "step_id": 1,
            "source": "reader",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": f"Loaded {len(trial.trajectory_steps)} trajectory steps from raw artifacts",
        }
    )

    # 3. Assemble prompt & context
    prompt, context = assemble_context(trial)
    rubric_digest = f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"

    analyst_steps.append(
        {
            "step_id": 2,
            "source": "analyzer",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": f"Executing analysis with {analyzer.__class__.__name__}",
        }
    )

    # 4. Run analysis
    result = analyzer.analyze(prompt, context)

    # Append any internal steps from the analyzer.
    for extra_step in result.steps:
        next_step_id = len(analyst_steps)
        analyst_steps.append(
            {
                "step_id": next_step_id,
                "source": extra_step.get("source", "analyst"),
                "timestamp": extra_step.get("timestamp", datetime.now(UTC).isoformat()),
                "message": extra_step.get("message", "Analyzed trial data"),
            }
        )
    # Validate category and every citation before allocating an analysis ID or
    # creating the durable analysis directory.
    normalized_evidence = _validate_result(result, trial)
    # 5. Build durable AnalysisRecord only after all model output is admitted.
    rec_id = analysis_id or new_ulid()
    model_name = model if model else ("stub" if isinstance(analyzer, StubAnalyzer) else "analyzer")
    record = AnalysisRecord(
        analysis_id=rec_id,
        trial_id=trial.trial_id,
        rubric_digest=rubric_digest,
        model=model_name,
        category=result.category,
        evidence=normalized_evidence,
        confidence=result.confidence,
        analysis_role=analysis_role,
        source_manifest_digest=source_manifest_digest,
        source_snapshot_digest=source_snapshot_digest,
        source_queue_digest=source_queue_digest,
    )

    # 7. Write conclusion JSON to research/analysis/<analysis_id>.json
    analysis_dir = root / ANALYSIS_DIR_NAME
    analysis_dir.mkdir(parents=True, exist_ok=True)
    conclusion_file = analysis_dir / f"{rec_id}.json"

    conclusion_payload = record.model_dump(mode="json")
    conclusion_payload["summary"] = result.summary
    conclusion_payload["created_at"] = datetime.now(UTC).isoformat()
    conclusion_payload["inputs"] = trial.inputs

    conclusion_file.write_text(
        json.dumps(conclusion_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 8. Write analyst's own trajectory to research/analysis/<analysis_id>.trajectory.json
    trajectory_file = analysis_dir / f"{rec_id}.trajectory.json"
    trajectory_payload = {
        "analysis_id": rec_id,
        "trial_id": trial.trial_id,
        "created_at": datetime.now(UTC).isoformat(),
        "steps": analyst_steps,
    }
    trajectory_file.write_text(
        json.dumps(trajectory_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 9. Project into Parquet under derived root
    project_analyses(repo_root=root, explicit_derived=derived)

    return record, trajectory_payload, conclusion_file, trajectory_file


def project_analyses(repo_root: Path, explicit_derived: Path | None = None) -> tuple[int, int]:
    """Project all stored analysis records and trajectories into Parquet.

    Writes:
      derived/parquet/analyses/analyses.parquet
      derived/parquet/analyst_trajectories/analyst_trajectories.parquet
    """
    derived = derived_root_from_environment(repo_root, explicit=explicit_derived)
    analysis_dir = repo_root / ANALYSIS_DIR_NAME
    if not analysis_dir.exists():
        return 0, 0

    record_rows: list[dict[str, Any]] = []
    trajectory_rows: list[dict[str, Any]] = []

    for path in sorted(analysis_dir.glob("*.json")):
        if (
            path.name.endswith(".trajectory.json")
            or path.name.endswith(".provenance.json")
            or path.name.startswith("stage5-")
            or path.name.startswith("stub-")
            or path.name.startswith("control-")
        ):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "analysis_id" not in data:
                continue
            conf = data.get("confidence") or {}
            interval = conf.get("interval")
            has_int = isinstance(interval, list | tuple) and len(interval) >= 2
            int_low = interval[0] if has_int else None
            int_high = interval[1] if has_int else None
            record_rows.append(
                {
                    "analysis_id": str(data["analysis_id"]),
                    "trial_id": str(data.get("trial_id", "")),
                    "rubric_digest": str(data.get("rubric_digest", "")),
                    "model": str(data.get("model", "")),
                    "category": str(data.get("category", "")),
                    "evidence_count": len(data.get("evidence", [])),
                    "confidence_level": str(conf.get("level", "medium")),
                    "confidence_n": conf.get("n"),
                    "confidence_interval_low": int_low,
                    "confidence_interval_high": int_high,
                    "confidence_provenance": conf.get("provenance_digest"),
                    "analysis_role": str(data.get("analysis_role", "trial_review")),
                    "source_manifest_digest": (
                        str(data["source_manifest_digest"])
                        if data.get("source_manifest_digest") is not None
                        else None
                    ),
                    "source_snapshot_digest": (
                        str(data["source_snapshot_digest"])
                        if data.get("source_snapshot_digest") is not None
                        else None
                    ),
                    "source_queue_digest": (
                        str(data["source_queue_digest"])
                        if data.get("source_queue_digest") is not None
                        else None
                    ),
                    "decision_eligible": bool(data.get("decision_eligible", False)),
                    "created_at": str(data.get("created_at", "")),
                }
            )
        except Exception:
            continue

    for traj_path in sorted(analysis_dir.glob("*.trajectory.json")):
        try:
            t_data = json.loads(traj_path.read_text(encoding="utf-8"))
            if not isinstance(t_data, dict):
                continue
            a_id = str(t_data.get("analysis_id", ""))
            for step in t_data.get("steps", []):
                if not isinstance(step, dict):
                    continue
                trajectory_rows.append(
                    {
                        "analysis_id": a_id,
                        "step_id": int(step.get("step_id", 0)),
                        "source": str(step.get("source", "")),
                        "timestamp": str(step.get("timestamp", "")),
                        "message": str(step.get("message", "")),
                    }
                )
        except Exception:
            continue

    # Write Parquet tables atomically
    analyses_out_dir = derived / DERIVED_ANALYSES_SUBDIR
    analyses_out_dir.mkdir(parents=True, exist_ok=True)
    analyses_parquet = analyses_out_dir / "analyses.parquet"

    rec_schema = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("trial_id", pa.string()),
            ("rubric_digest", pa.string()),
            ("model", pa.string()),
            ("category", pa.string()),
            ("evidence_count", pa.int64()),
            ("confidence_level", pa.string()),
            ("confidence_n", pa.int64()),
            ("confidence_interval_low", pa.float64()),
            ("confidence_interval_high", pa.float64()),
            ("confidence_provenance", pa.string()),
            ("analysis_role", pa.string()),
            ("source_manifest_digest", pa.string()),
            ("source_snapshot_digest", pa.string()),
            ("source_queue_digest", pa.string()),
            ("decision_eligible", pa.bool_()),
            ("created_at", pa.string()),
        ]
    )

    if record_rows:
        t_records = pa.Table.from_pylist(record_rows, schema=rec_schema)
    else:
        t_records = rec_schema.empty_table()

    tmp_rec = analyses_parquet.with_suffix(".parquet.tmp")
    pq.write_table(t_records, tmp_rec, compression="zstd")
    tmp_rec.replace(analyses_parquet)

    traj_out_dir = derived / DERIVED_TRAJECTORIES_SUBDIR
    traj_out_dir.mkdir(parents=True, exist_ok=True)
    traj_parquet = traj_out_dir / "analyst_trajectories.parquet"

    traj_schema = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("step_id", pa.int64()),
            ("source", pa.string()),
            ("timestamp", pa.string()),
            ("message", pa.string()),
        ]
    )

    if trajectory_rows:
        t_trajectories = pa.Table.from_pylist(trajectory_rows, schema=traj_schema)
    else:
        t_trajectories = traj_schema.empty_table()

    tmp_traj = traj_parquet.with_suffix(".parquet.tmp")
    pq.write_table(t_trajectories, tmp_traj, compression="zstd")
    tmp_traj.replace(traj_parquet)

    return len(record_rows), len(trajectory_rows)


def list_analyses(repo_root: Path, trial_id: str | None = None) -> list[dict[str, Any]]:
    """List stored analysis conclusions, optionally filtered by trial_id."""
    analysis_dir = repo_root / ANALYSIS_DIR_NAME
    if not analysis_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    for path in sorted(analysis_dir.glob("*.json")):
        if (
            path.name.endswith(".trajectory.json")
            or path.name.endswith(".provenance.json")
            or path.name.startswith("stage5-")
            or path.name.startswith("stub-")
            or path.name.startswith("control-")
        ):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "analysis_id" not in data:
                continue
            if trial_id is not None and data.get("trial_id") != trial_id:
                continue
            conf = data.get("confidence") or {}
            results.append(
                {
                    "analysis_id": data["analysis_id"],
                    "trial_id": data.get("trial_id"),
                    "model": data.get("model"),
                    "category": data.get("category"),
                    "confidence": conf.get("level") if isinstance(conf, dict) else str(conf),
                    "evidence_count": len(data.get("evidence", [])),
                    "analysis_role": data.get("analysis_role", "trial_review"),
                    "source_manifest_digest": data.get("source_manifest_digest"),
                    "source_snapshot_digest": data.get("source_snapshot_digest"),
                    "source_queue_digest": data.get("source_queue_digest"),
                    "decision_eligible": data.get("decision_eligible", False),
                    "created_at": data.get("created_at"),
                }
            )
        except Exception:
            continue

    return results


def show_analysis(analysis_id: str, repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retrieve an analysis conclusion and its recorded reasoning trajectory."""
    analysis_dir = repo_root / ANALYSIS_DIR_NAME
    conclusion_file = analysis_dir / f"{analysis_id}.json"
    trajectory_file = analysis_dir / f"{analysis_id}.trajectory.json"

    if not conclusion_file.is_file():
        raise FileNotFoundError(f"Analysis record '{analysis_id}' not found at {conclusion_file}")

    conclusion_data = json.loads(conclusion_file.read_text(encoding="utf-8"))
    trajectory_data = (
        json.loads(trajectory_file.read_text(encoding="utf-8"))
        if trajectory_file.is_file()
        else {"analysis_id": analysis_id, "steps": []}
    )

    return conclusion_data, trajectory_data
