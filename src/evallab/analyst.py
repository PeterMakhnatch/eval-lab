"""ANALYST: Durable agent analysis with stored reasoning trajectories.

Provides an analysis runner with an injectable Analyzer protocol, deterministic
stubbing for tests/CI, token-gated model dispatch, durable JSON storage with
lineage, analyst trajectory capture, and Parquet projection for DuckDB querying.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.attach import attach
from evallab.lineage import compute_file_digest
from evallab.paths import (
    derived_root_from_environment,
    shared_checkout_root,
)
from evallab.queue import new_ulid
from evallab.schemas import (
    AnalysisRecord,
    ConfidenceClaim,
    EvidenceCitation,
)

ANALYSIS_DIR_NAME = "research/analysis"
DERIVED_ANALYSES_SUBDIR = "analyses"
DERIVED_TRAJECTORIES_SUBDIR = "analyst_trajectories"

DEFAULT_RUBRIC = """# Trial Failure and Capability Analysis Rubric

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
        confidence_level: Literal["low", "medium", "high"] = "high",
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.category = category
        self.summary = summary
        self.evidence = evidence
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
        )


class ModelAnalyzer:
    """Real model-backed analyzer requiring explicit model selector and opt-in."""

    def __init__(self, model: str | None = None) -> None:
        if not model:
            raise ModelProviderRefusedError(
                "Model analyzer requires an explicit model selector (e.g. --model gpt-4o). "
                "The default analysis path never invokes an external model provider."
            )
        self.model = model

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        raise ModelProviderRefusedError(
            f"Invoking external model '{self.model}' spends tokens and requires credentials. "
            "Model dispatch is token-gated; default runs use the deterministic stub."
        )


def _resolve_runs_roots(
    repo_root: Path, runs_root: Path | None = None
) -> list[Path]:
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
    if isinstance(data, dict):
        steps = data.get("steps")
        if isinstance(steps, list):
            return [s for s in steps if isinstance(s, dict)]
    return []


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


def resolve_trial(
    trial_identifier: str,
    repo_root: Path,
    *,
    explicit_derived: Path | None = None,
    runs_root: Path | None = None,
) -> TrialData:
    """Resolve trial metadata via DuckDB attach surface and find raw files."""
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

    # Search runs roots for raw trajectory and result
    candidate_roots = _resolve_runs_roots(repo_root, runs_root)
    found_trial_dir: Path | None = None
    found_traj: Path | None = None
    found_result: Path | None = None

    for root in candidate_roots:
        if not root.exists():
            continue
        # Check direct path matches
        direct_target = root / trial_identifier
        if direct_target.is_dir():
            found_trial_dir = direct_target
            break
        # Search by job/trial subdirectory
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

    steps = _load_trajectory_steps(found_traj) if found_traj else []

    # Build inputs for lineage
    inputs: list[dict[str, Any]] = []
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

    # Defaults from trial row or identifier
    raw_trial_id = str(trial_row.get("trial_id") if trial_row else trial_identifier)
    if len(raw_trial_id) == 26 and raw_trial_id[0] in "01234567":
        trial_id = raw_trial_id
    else:
        trial_id = _deterministic_ulid(raw_trial_id)
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
    task_name = str(
        trial_row.get("task_name") if trial_row else trial_name.split("__")[0]
    )
    reward = trial_row.get("primary_reward") if trial_row else None
    exception = trial_row.get("exception_class") if trial_row else None
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
    )


def assemble_context(trial: TrialData) -> tuple[str, str]:
    """Assemble analysis prompt and context text from resolved trial data."""
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
    for i, step in enumerate(trial.trajectory_steps):
        source = step.get("source", "agent")
        msg = step.get("message")
        msg_str = json.dumps(msg) if isinstance(msg, dict) else str(msg or "")
        truncated = (msg_str[:200] + "...") if len(msg_str) > 200 else msg_str
        context_lines.append(f"Step {i} [{source}]: {truncated}")

    context = "\n".join(context_lines)
    return prompt, context


def run_analysis(
    trial_identifier: str,
    *,
    analyzer: Analyzer | None = None,
    model: str | None = None,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
    runs_root: Path | None = None,
    analysis_id: str | None = None,
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
        analyzer = ModelAnalyzer(model=model) if model is not None else StubAnalyzer()

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

    # 2. Resolve trial data
    trial = resolve_trial(
        trial_identifier, root, explicit_derived=derived, runs_root=runs_root
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

    # Append any internal steps from the analyzer
    for extra_step in result.steps:
        next_step_id = len(analyst_steps)
        analyst_steps.append(
            {
                "step_id": next_step_id,
                "source": extra_step.get("source", "analyst"),
                "timestamp": extra_step.get(
                    "timestamp", datetime.now(UTC).isoformat()
                ),
                "message": extra_step.get("message", "Analyzed trial data"),
            }
        )

    # 5. Evidence requirement: reject unevidenced conclusion
    if not result.evidence or len(result.evidence) == 0:
        raise ValueError(
            "Analysis rejected: conclusion has no cited evidence. "
            "Every claim must cite concrete artifacts/steps."
        )
    # 6. Build durable AnalysisRecord
    rec_id = analysis_id or new_ulid()
    model_name = (
        model
        if model
        else ("stub" if isinstance(analyzer, StubAnalyzer) else "analyzer")
    )
    record = AnalysisRecord(
        analysis_id=rec_id,
        trial_id=trial.trial_id,
        rubric_digest=rubric_digest,
        model=model_name,
        category=result.category,
        evidence=result.evidence,
        confidence=result.confidence,
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


def project_analyses(
    repo_root: Path, explicit_derived: Path | None = None
) -> tuple[int, int]:
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
        t_trajectories = pa.Table.from_pylist(
            trajectory_rows, schema=traj_schema
        )
    else:
        t_trajectories = traj_schema.empty_table()

    tmp_traj = traj_parquet.with_suffix(".parquet.tmp")
    pq.write_table(t_trajectories, tmp_traj, compression="zstd")
    tmp_traj.replace(traj_parquet)

    return len(record_rows), len(trajectory_rows)


def list_analyses(
    repo_root: Path, trial_id: str | None = None
) -> list[dict[str, Any]]:
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
                    "created_at": data.get("created_at"),
                }
            )
        except Exception:
            continue

    return results


def show_analysis(
    analysis_id: str, repo_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retrieve an analysis conclusion and its recorded reasoning trajectory."""
    analysis_dir = repo_root / ANALYSIS_DIR_NAME
    conclusion_file = analysis_dir / f"{analysis_id}.json"
    trajectory_file = analysis_dir / f"{analysis_id}.trajectory.json"

    if not conclusion_file.is_file():
        raise FileNotFoundError(
            f"Analysis record '{analysis_id}' not found at {conclusion_file}"
        )

    conclusion_data = json.loads(conclusion_file.read_text(encoding="utf-8"))
    trajectory_data = (
        json.loads(trajectory_file.read_text(encoding="utf-8"))
        if trajectory_file.is_file()
        else {"analysis_id": analysis_id, "steps": []}
    )

    return conclusion_data, trajectory_data
