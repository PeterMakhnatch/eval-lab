"""LabEvaluator integration for GEPA optimize_anything and MetaHarnessEngine.

This module provides the concrete evaluation boundary between GEPA's black-box
text optimization loops and Eval Lab's task execution runtime.

Key Invariants:
1. Candidate Safety & Immutability:
   - Candidates are immutable UTF-8 supplementary instruction text (preambles),
     NEVER executable host code.
   - Hash-addressed via full SHA-256 (``sha256:<64-hex>``).
   - Passed to Harbor via ``extra_instruction_path`` and ``extra_instruction_sha256``.
   - Existing candidate files must match content exactly; collisions raise errors and
     are never overwritten.
   - Output and jobs directories must be located inside the repository root; symlinks
     escaping the root are rejected.
2. Dataset Boundaries & Task Integrity:
   - Evaluator accepts only explicitly declared development examples (``split == 'development'``).
   - Incoming examples are compared field-by-field against immutable stored declarations.
   - Tasks must reside within the repository root (repo-relative, no traversal).
   - Task package digests are cryptographically verified against declared values.
3. Execution Policy Boundary:
   - Permitted agents: local controls (``oracle``, ``nop`` with model=None) or ready native
     profiles (``baseline``, ``released``, ``bootstrap-cwd`` with exact native model
     ``anthropic/claude-opus-4-6``).
   - Local controls execute directly via ``Executor.execute_direct`` with ``RunProvenance``.
   - Newly executed and resumed jobs are both validated against exact recorded native provenance.
   - Model-backed evaluations require a genuine cost estimate (``estimated_cost_usd > 0``),
     submit an exact ``ExperimentSpec`` with hypothesis and elicitation purpose to the
     standing-approvals queue, and NEVER approve it.
   - Resumption checks ONLY the deterministic job name or retained evaluation receipt;
     no broad directory discovery across unrelated runs.
   - Pending evaluations reuse existing queue specs on resume without duplicate submissions.
4. Reward Fidelity & No NaN:
   - When an evaluation is pending, ``EvaluationPending`` is raised to stop orchestration.
   - When an evaluation has missing reward or infra/runtime errors, the failure is recorded
     in ``records`` and on disk with status="error", and ``EvaluationUnavailable`` is raised
     immediately. NaN is NEVER returned.
   - Actual usage is parsed under native ``agent_result``.
5. Comparison Export:
   - ``write_comparison_spec(candidate_ids)`` requires exact full candidate digests without
     prefix ambiguity.
   - Emits an exploratory comparison (``mode="exploratory"``, no causal claim) using
     ``CohortComparisonSpec`` across real completed job paths.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.queue import Executor, new_ulid
from evallab.registry import task_directory_digest
from evallab.results import JobRecord, load_job
from evallab.runner import CONTROL_AGENTS, RunRequest
from evallab.schemas import CohortComparisonSpec, CohortSelector, ExperimentSpec, RunProvenance

PERMITTED_CONTROLS = frozenset({"oracle", "nop"})
NATIVE_ENTRYPOINTS = {
    "baseline": "evallab_metaharness.agents:Baseline",
    "released": "evallab_metaharness.agents:Released",
    "bootstrap-cwd": "evallab_metaharness.agents:BootstrapCwd",
}
PERMITTED_NATIVE_PROFILES = frozenset(NATIVE_ENTRYPOINTS)
PERMITTED_NATIVE_MODEL = "anthropic/claude-opus-4-6"
PERMITTED_AGENTS = PERMITTED_CONTROLS | PERMITTED_NATIVE_PROFILES


class EvaluationPending(Exception):
    """Raised when an evaluation is pending in the queue or awaiting execution."""

    def __init__(
        self,
        message: str,
        *,
        spec_id: str | None = None,
        spec_path: Path | None = None,
        candidate_hash: str | None = None,
        task_id: str | None = None,
        decision: Any = None,
    ) -> None:
        super().__init__(message)
        self.spec_id = spec_id
        self.spec_path = spec_path
        self.candidate_hash = candidate_hash
        self.task_id = task_id
        self.decision = decision


class EvaluationUnavailable(Exception):
    """Raised when an evaluation cannot complete due to missing reward, verifier failure, or error."""


class ProvenanceMismatchError(ValueError):
    """Raised when recorded job provenance differs from requested candidate, task, agent, or model."""


class TaskDigestMismatchError(ValueError):
    """Raised when a task's recomputed package digest does not match its declared digest."""


class ExampleDeclarationError(ValueError):
    """Raised when an example violates declaration constraints (sealed split, undeclared, path traversal)."""


@dataclass(frozen=True)
class EvaluationRecord:
    """Retained record of one candidate evaluation on an example task.

    Main consumes:
        candidate_id: 'sha256:' + full 64-char hex digest
        task_id: task identifier
        status: 'completed' for scored native receipts; otherwise 'pending' or 'error'
        score: float | None (primary reward when scored, None for pending/error)
        job_path: str | None (path to completed job directory, None when pending)
    """

    candidate_id: str
    candidate_sha256: str
    candidate_path: str
    task_id: str
    task_path: str
    task_package_digest: str
    agent: str
    model: str | None
    split: str
    job_path: str | None
    receipt_paths: dict[str, str]
    score: float | None
    rewards: dict[str, float | None]
    usage: dict[str, Any]
    status: str
    error: str | None
    trial_id: str | None
    trial_name: str | None
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_cohort_label(full_digest: str) -> str:
    """Sanitize candidate digest for CohortSelector.label (^[a-z0-9][a-z0-9-]+$)."""
    hex_part = full_digest.split(":", 1)[-1] if ":" in full_digest else full_digest
    return f"c-{hex_part[:16]}"


def deterministic_job_name(
    *,
    agent: str,
    model: str | None,
    task_id: str,
    candidate_sha256: str,
) -> str:
    """Compute deterministic job directory name conforming to ^[a-z0-9][a-z0-9-]+$."""
    cand_tag = candidate_sha256.split(":", 1)[-1][:16]
    clean_agent = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")[:16]
    clean_task = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:20]
    clean_model = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:16] if model else "nomodel"
    raw = f"gepa-{clean_agent}-{clean_model}-{clean_task}-{cand_tag}"[:80]
    return re.sub(r"-+", "-", raw).strip("-")


def _artifact_task_tag(task_id: str) -> str:
    """Safe alphanumeric hash tag for task_id preventing any path traversal in filenames."""
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _validate_in_repo_dir(repo_root: Path, target_dir: Path, label: str) -> Path:
    """Ensure directory is within repository root and does not escape via symlinks."""
    resolved_root = repo_root.resolve()
    resolved_target = target_dir.resolve()

    if target_dir.is_symlink() or any(parent.is_symlink() for parent in target_dir.parents):
        raise ValueError(f"{label} cannot contain symlinks escaping repository root: {target_dir}")

    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(
            f"{label} must be inside the repository root ({resolved_root}): {target_dir}"
        )
    return resolved_target


def validate_example_dict(repo_root: Path, example: dict[str, Any]) -> dict[str, Any]:
    """Validate that an example conforms strictly to the explicit development contract."""
    split = example.get("split")
    if split != "development":
        raise ExampleDeclarationError(
            f"Only development split examples are permitted; split '{split}' is forbidden (sealed tasks must not reach GEPA)"
        )

    task_id = example.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise ExampleDeclarationError(f"task_id must be a non-empty string, got {task_id!r}")

    task_path_str = example.get("task_path")
    if not task_path_str or not isinstance(task_path_str, str):
        raise ExampleDeclarationError(
            f"task_path must be a non-empty string, got {task_path_str!r}"
        )

    if task_path_str.startswith("/") or ".." in task_path_str.split("/"):
        raise ExampleDeclarationError(
            f"task_path must stay relative to the repository without path traversal: {task_path_str!r}"
        )

    resolved_root = repo_root.resolve()
    abs_task_path = (repo_root / task_path_str).resolve()
    if abs_task_path != resolved_root and resolved_root not in abs_task_path.parents:
        raise ExampleDeclarationError(f"task_path escapes repository root: {task_path_str!r}")

    if not abs_task_path.is_dir():
        raise ExampleDeclarationError(
            f"task directory not found in repository: {task_path_str!r} ({abs_task_path})"
        )

    declared_digest = example.get("task_package_digest")
    if (
        not declared_digest
        or not isinstance(declared_digest, str)
        or not re.match(r"^sha256:[0-9a-f]{64}$", declared_digest)
    ):
        raise ExampleDeclarationError(
            f"task_package_digest must be a sha256:... 64-hex string, got {declared_digest!r}"
        )

    computed_digest = task_directory_digest(abs_task_path)
    if computed_digest != declared_digest:
        raise TaskDigestMismatchError(
            f"Task package digest mismatch for {task_id}: declared {declared_digest}, computed {computed_digest}"
        )

    return {
        "task_id": str(task_id),
        "task_path": str(task_path_str),
        "task_package_digest": str(declared_digest),
        "split": "development",
    }


def _check_job_provenance(
    job: JobRecord,
    *,
    expected_agent: str,
    expected_model: str | None,
    expected_task_id: str,
    expected_package_digest: str,
    expected_candidate_sha256: str,
) -> bool:
    """Bind the single native trial to its recorded request and fixed profile."""
    locked_trials = job.lock.get("trials")
    if len(job.trials) != 1 or not isinstance(locked_trials, list) or len(locked_trials) != 1:
        return False
    # config.json intentionally omits defaults in Harbor 0.21; locks retain the
    # effective request. Never reconstruct historical settings from defaults.
    for locked in (locked_trials[0], job.trials[0].lock):
        extras = locked.get("extra_instructions", [])
        if len(extras) != 1 or extras[0].get("digest") != expected_candidate_sha256:
            return False
        agent = locked.get("agent")
        if not isinstance(agent, dict) or agent.get("model_name") != expected_model:
            return False
        if expected_agent in NATIVE_ENTRYPOINTS:
            if agent.get("import_path") != NATIVE_ENTRYPOINTS[expected_agent]:
                return False
        elif agent.get("name") != expected_agent or agent.get("import_path"):
            return False

    exp = job.metadata.get("experiment")
    if not isinstance(exp, dict):
        return False

    if exp.get("task_id") != expected_task_id:
        return False
    if exp.get("package_digest") != expected_package_digest:
        return False
    return exp.get("preamble_sha256") == expected_candidate_sha256


class LabEvaluator:
    """Concrete evaluation boundary for GEPA optimize_anything on Eval Lab."""

    def __init__(
        self,
        repo_root: Path,
        output_dir: Path,
        examples: list[dict[str, Any]],
        agent: str,
        model: str | None = None,
        timeout_seconds: int = 1200,
        estimated_cost_usd: float | None = None,
        *,
        executor: Executor | None = None,
        jobs_dir: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.output_dir = _validate_in_repo_dir(self.repo_root, Path(output_dir), "output_dir")
        self.jobs_dir = _validate_in_repo_dir(
            self.repo_root,
            Path(jobs_dir) if jobs_dir else (self.repo_root / "runs"),
            "jobs_dir",
        )

        self.agent = str(agent)
        self.model = str(model) if model is not None else None
        self.timeout_seconds = int(timeout_seconds)
        self.estimated_cost_usd = (
            float(estimated_cost_usd) if estimated_cost_usd is not None else None
        )

        # Agent/profile and model validation
        if self.agent in PERMITTED_CONTROLS:
            if self.model is not None:
                raise ValueError(f"the {self.agent} control does not accept a model")
        elif self.agent in PERMITTED_NATIVE_PROFILES:
            if self.model != PERMITTED_NATIVE_MODEL:
                raise ValueError(
                    f"Native profile '{self.agent}' requires exact native model '{PERMITTED_NATIVE_MODEL}', got {self.model!r}"
                )
        else:
            raise ValueError(
                f"Agent '{self.agent}' not permitted: must be one of controls {sorted(PERMITTED_CONTROLS)} "
                f"or native profiles {sorted(PERMITTED_NATIVE_PROFILES)}"
            )

        # Validate and store immutable copies of declared development examples
        if not examples:
            raise ValueError(
                "examples list cannot be empty; explicit development tasks are required"
            )

        self._declared_examples: dict[str, dict[str, Any]] = {}
        for ex in examples:
            validated = validate_example_dict(self.repo_root, ex)
            task_id = validated["task_id"]
            if task_id in self._declared_examples:
                raise ExampleDeclarationError(f"Duplicate task_id in declared examples: {task_id}")
            self._declared_examples[task_id] = copy.deepcopy(validated)

        # Executor boundary
        self.executor = executor or Executor.from_repo(self.repo_root)

        # In-memory record retention
        self._records: list[EvaluationRecord] = []

        # Local output subdirectories
        (self.output_dir / "candidates").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "evaluations").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "comparisons").mkdir(parents=True, exist_ok=True)

    @property
    def records(self) -> list[EvaluationRecord]:
        """Expose immutable view of evaluation records."""
        return list(self._records)

    def _ensure_candidate_stored(self, candidate: str) -> tuple[str, Path, str]:
        """Store candidate text addressed by sha256. Fails if existing content differs."""
        if not isinstance(candidate, str):
            raise TypeError(f"Candidate must be a UTF-8 string, got {type(candidate).__name__}")

        candidate_bytes = candidate.encode("utf-8")
        candidate_sha256 = f"sha256:{hashlib.sha256(candidate_bytes).hexdigest()}"
        digest_hex = candidate_sha256.split(":", 1)[-1]

        local_candidate_file = self.output_dir / "candidates" / f"{digest_hex}.txt"
        if local_candidate_file.exists():
            existing_text = local_candidate_file.read_text(encoding="utf-8")
            if existing_text != candidate:
                raise ValueError(
                    f"Immutable candidate file collision: {local_candidate_file} exists but content differs from hash {candidate_sha256}"
                )
        else:
            local_candidate_file.write_text(candidate, encoding="utf-8")

        rel_candidate_path = local_candidate_file.relative_to(self.repo_root).as_posix()
        return candidate_sha256, local_candidate_file, rel_candidate_path

    def _record_evaluation(
        self,
        *,
        candidate_sha256: str,
        local_candidate_file: Path,
        example: dict[str, Any],
        status: str,
        job_path: str | None,
        receipt_paths: dict[str, str],
        score: float | None,
        rewards: dict[str, float | None],
        usage: dict[str, Any],
        error: str | None,
        trial_id: str | None = None,
        trial_name: str | None = None,
    ) -> EvaluationRecord:
        """Shared constructor: build EvaluationRecord, retain in memory, and persist receipt on disk."""
        record = EvaluationRecord(
            candidate_id=candidate_sha256,
            candidate_sha256=candidate_sha256,
            candidate_path=str(local_candidate_file),
            task_id=example["task_id"],
            task_path=example["task_path"],
            task_package_digest=example["task_package_digest"],
            agent=self.agent,
            model=self.model,
            split=example["split"],
            job_path=job_path,
            receipt_paths=receipt_paths,
            score=score,
            rewards=rewards,
            usage=usage,
            status=status,
            error=error,
            trial_id=trial_id,
            trial_name=trial_name,
            evaluated_at=datetime.now(UTC).isoformat(),
        )
        self._records.append(record)

        # Persist receipt artifact to disk under evaluations/
        cand_hex = candidate_sha256.split(":", 1)[-1]
        task_tag = _artifact_task_tag(example["task_id"])
        receipt_file = self.output_dir / "evaluations" / f"{cand_hex}_{task_tag}.json"
        receipt_file.write_text(
            json.dumps(record.to_dict(), indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )

        return record

    def _consume_completed_job(
        self,
        job_record: JobRecord,
        example: dict[str, Any],
        candidate_sha256: str,
        local_candidate_file: Path,
    ) -> tuple[float, dict[str, Any]]:
        """Parse completed job and trial records; stop immediately on missing reward or error."""
        job_dir = job_record.path
        trial = job_record.trials[0] if job_record.trials else None

        receipt_paths: dict[str, str] = {
            "job_result": str(job_dir / "result.json"),
            "job_config": str(job_dir / "config.json"),
            "job_lock": str(job_dir / "lock.json"),
        }
        if (job_dir / "lab-metadata.json").is_file():
            receipt_paths["lab_metadata"] = str(job_dir / "lab-metadata.json")
        if trial is not None:
            receipt_paths["trial_result"] = str(trial.path / "result.json")
            receipt_paths["trial_config"] = str(trial.path / "config.json")
            receipt_paths["trial_lock"] = str(trial.path / "lock.json")

        agent_result: dict[str, Any] = {}
        error_info: str | None = None
        if trial is not None:
            raw_ar = trial.result.get("agent_result")
            if isinstance(raw_ar, dict):
                agent_result = raw_ar

            if trial.result.get("exception_info"):
                error_info = str(trial.result["exception_info"])
            elif trial.result.get("error"):
                error_info = str(trial.result["error"])

        usage: dict[str, Any] = {
            "cost_usd": agent_result.get("cost_usd"),
            "n_input_tokens": agent_result.get("n_input_tokens"),
            "n_cache_tokens": agent_result.get("n_cache_tokens"),
            "n_output_tokens": agent_result.get("n_output_tokens"),
            "n_total_tokens": (
                agent_result.get("n_input_tokens", 0) + agent_result.get("n_output_tokens", 0)
                if agent_result.get("n_input_tokens") is not None
                and agent_result.get("n_output_tokens") is not None
                else None
            ),
        }
        if "provider_usage" in job_record.metadata:
            usage["provider_usage"] = job_record.metadata["provider_usage"]
        if trial is not None:
            accounting_path = trial.path / "agent" / "request-accounting.json"
            if accounting_path.is_file() and not accounting_path.is_symlink():
                accounting = json.loads(accounting_path.read_text())
                receipt_paths["request_accounting"] = str(accounting_path)
                usage["native_request_totals"] = accounting.get("totals")
                usage["remote_outcomes_pending"] = accounting.get("remote_outcomes_pending")
                usage["role_accounting_receipt"] = str(accounting_path)
            elif self.agent in PERMITTED_NATIVE_PROFILES:
                usage["native_request_accounting"] = "missing"

        primary_reward = trial.primary_reward if trial is not None else None
        rewards = {
            name: value if math.isfinite(value) else None
            for name, value in (trial.rewards.items() if trial is not None else ())
        }

        # Handle failure cases: persist error record and raise EvaluationUnavailable (never return NaN)
        if error_info:
            self._record_evaluation(
                candidate_sha256=candidate_sha256,
                local_candidate_file=local_candidate_file,
                example=example,
                status="error",
                job_path=str(job_dir),
                receipt_paths=receipt_paths,
                score=None,
                rewards=rewards,
                usage=usage,
                error=error_info,
                trial_id=trial.id if trial else None,
                trial_name=trial.name if trial else None,
            )
            raise EvaluationUnavailable(
                f"Evaluation failed with error on task {example['task_id']}: {error_info}"
            )

        if primary_reward is None or not math.isfinite(float(primary_reward)):
            self._record_evaluation(
                candidate_sha256=candidate_sha256,
                local_candidate_file=local_candidate_file,
                example=example,
                status="error",
                job_path=str(job_dir),
                receipt_paths=receipt_paths,
                score=None,
                rewards=rewards,
                usage=usage,
                error="missing_or_nonfinite_reward",
                trial_id=trial.id if trial else None,
                trial_name=trial.name if trial else None,
            )
            raise EvaluationUnavailable(
                f"Evaluation completed without a finite primary reward on task {example['task_id']}"
            )

        # Successful evaluation
        score = float(primary_reward)
        self._record_evaluation(
            candidate_sha256=candidate_sha256,
            local_candidate_file=local_candidate_file,
            example=example,
            status="completed",
            job_path=str(job_dir),
            receipt_paths=receipt_paths,
            score=score,
            rewards=rewards,
            usage=usage,
            error=None,
            trial_id=trial.id if trial else None,
            trial_name=trial.name if trial else None,
        )

        info: dict[str, Any] = {
            "task_id": example["task_id"],
            "candidate_hash": candidate_sha256,
            "candidate_id": candidate_sha256,
            "job_path": str(job_dir),
            "status": "completed",
            "score": score,
            "rewards": rewards,
            "usage": usage,
            "error": None,
            "receipt_paths": receipt_paths,
        }
        return score, info

    def __call__(self, candidate: str, example: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Evaluate one candidate instruction string on one development example.

        Returns:
            (score, info): Scalar score and diagnostic feedback dict.
        Raises:
            EvaluationPending: If a model-backed evaluation is pending in the queue.
            EvaluationUnavailable: If trial has missing reward or infra failure (never NaN).
            ExampleDeclarationError: If example does not match immutable declared specification.
            TaskDigestMismatchError: If task package digest has mutated.
            ProvenanceMismatchError: If recorded job provenance does not match.
            TypeError: If candidate is not a string.
        """
        candidate_sha256, local_candidate_file, rel_candidate_path = self._ensure_candidate_stored(
            candidate
        )

        task_id = example.get("task_id")
        if not task_id or task_id not in self._declared_examples:
            raise ExampleDeclarationError(
                f"Example task '{task_id}' is not an explicitly declared development task"
            )

        # Compare incoming example fields against immutable declaration
        declared = self._declared_examples[task_id]
        for key in ("task_id", "task_path", "task_package_digest", "split"):
            if example.get(key) != declared[key]:
                raise ExampleDeclarationError(
                    f"Incoming example field '{key}' differs from immutable declared contract: "
                    f"{example.get(key)!r} != {declared[key]!r}"
                )

        abs_task_path = (self.repo_root / declared["task_path"]).resolve()
        current_digest = task_directory_digest(abs_task_path)
        if current_digest != declared["task_package_digest"]:
            raise TaskDigestMismatchError(
                f"Current task directory digest for {task_id} has mutated: "
                f"expected {declared['task_package_digest']}, computed {current_digest}"
            )

        job_name = deterministic_job_name(
            agent=self.agent,
            model=self.model,
            task_id=task_id,
            candidate_sha256=candidate_sha256,
        )
        exact_job_dir = self.jobs_dir / job_name

        # Check ONLY exact deterministic job path (no broad scanning)
        if exact_job_dir.is_dir() and (exact_job_dir / "result.json").is_file():
            job_record = load_job(exact_job_dir)
            if not _check_job_provenance(
                job_record,
                expected_agent=self.agent,
                expected_model=self.model,
                expected_task_id=task_id,
                expected_package_digest=declared["task_package_digest"],
                expected_candidate_sha256=candidate_sha256,
            ):
                raise ProvenanceMismatchError(
                    f"Job at {exact_job_dir} exists but does not match exact candidate/task/model provenance"
                )
            return self._consume_completed_job(
                job_record,
                declared,
                candidate_sha256,
                local_candidate_file,
            )

        # If local control, execute directly via Executor.execute_direct
        if self.agent in CONTROL_AGENTS:
            request = RunRequest(
                task=abs_task_path,
                agent=self.agent,
                name=job_name,
                jobs_dir=self.jobs_dir,
                model=None,
                extra_instruction_path=local_candidate_file,
                timeout_seconds=self.timeout_seconds,
                provenance=RunProvenance(
                    spec_id=f"gepa-{new_ulid()}",
                    task=declared["task_path"],
                    task_path=declared["task_path"],
                    task_id=task_id,
                    package_digest=declared["task_package_digest"],
                    preamble_path=rel_candidate_path,
                    preamble_sha256=candidate_sha256,
                ),
            )
            job_dir = self.executor.execute_direct(request)
            job_record = load_job(job_dir)
            if not _check_job_provenance(
                job_record,
                expected_agent=self.agent,
                expected_model=self.model,
                expected_task_id=task_id,
                expected_package_digest=declared["task_package_digest"],
                expected_candidate_sha256=candidate_sha256,
            ):
                raise ProvenanceMismatchError(
                    f"Executed job at {job_dir} failed provenance verification: recorded metadata does not match requested agent/model/task/candidate"
                )
            return self._consume_completed_job(
                job_record,
                declared,
                candidate_sha256,
                local_candidate_file,
            )

        # Model-backed evaluation: verify estimate, reuse pending spec if already queued
        if self.estimated_cost_usd is None or self.estimated_cost_usd <= 0.0:
            raise ValueError(
                f"Model-backed evaluation for agent '{self.agent}' requires a genuine positive estimated_cost_usd, got {self.estimated_cost_usd!r}"
            )

        cand_hex = candidate_sha256.split(":", 1)[-1]
        task_tag = _artifact_task_tag(task_id)
        eval_artifact_path = self.output_dir / "evaluations" / f"{cand_hex}_{task_tag}.json"

        if eval_artifact_path.is_file():
            artifact_data = json.loads(eval_artifact_path.read_text(encoding="utf-8"))
            if any(
                artifact_data.get(key) != expected
                for key, expected in {
                    "agent": self.agent,
                    "model": self.model,
                    "candidate_id": candidate_sha256,
                    "task_id": task_id,
                    "task_package_digest": declared["task_package_digest"],
                }.items()
            ):
                raise ProvenanceMismatchError("Retained evaluation belongs to a different request")
            existing_spec = artifact_data.get("receipt_paths", {}).get("spec_file")
            if artifact_data.get("status") == "pending" and existing_spec:
                existing_spec_path = Path(existing_spec)
                # Native queue transitions move files. A retained submission must
                # never be resubmitted merely because its original path moved.
                self._record_evaluation(
                    candidate_sha256=candidate_sha256,
                    local_candidate_file=local_candidate_file,
                    example=declared,
                    status="pending",
                    job_path=None,
                    receipt_paths={
                        "spec_file": str(existing_spec_path),
                        "evaluation_artifact": str(eval_artifact_path),
                    },
                    score=None,
                    rewards={},
                    usage={"estimated_cost_usd": self.estimated_cost_usd},
                    error=None,
                )
                raise EvaluationPending(
                    f"Model evaluation for candidate {candidate_sha256[:16]} on task '{task_id}' is already pending in queue ({existing_spec_path})",
                    spec_id=existing_spec_path.stem.rsplit("-", 1)[-1],
                    spec_path=existing_spec_path,
                    candidate_hash=candidate_sha256,
                    task_id=task_id,
                )

        # Submit new ExperimentSpec to queue with required name pattern, hypothesis, and elicitation purpose
        clean_spec_name = deterministic_job_name(
            agent=self.agent,
            model=self.model,
            task_id=task_id,
            candidate_sha256=candidate_sha256,
        )
        spec = ExperimentSpec(
            spec_id=new_ulid(),
            name=clean_spec_name,
            hypothesis=f"Instruction preamble improves performance on {task_id}",
            purpose="elicitation",
            task=declared["task_path"],
            task_path=declared["task_path"],
            task_id=task_id,
            task_package_digest=declared["task_package_digest"],
            agent=self.agent,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            est_cost_usd=self.estimated_cost_usd,
            submitted_by="gepa-evaluator",
            extra_instruction_path=rel_candidate_path,
            extra_instruction_sha256=candidate_sha256,
            jobs_dir=self.jobs_dir.relative_to(self.repo_root).as_posix(),
        )

        spec_path, decision = self.executor.submit(spec)

        self._record_evaluation(
            candidate_sha256=candidate_sha256,
            local_candidate_file=local_candidate_file,
            example=declared,
            status="pending",
            job_path=None,
            receipt_paths={
                "spec_file": str(spec_path),
                "evaluation_artifact": str(eval_artifact_path),
            },
            score=None,
            rewards={},
            usage={"estimated_cost_usd": self.estimated_cost_usd},
            error=None,
        )

        raise EvaluationPending(
            f"Model evaluation for candidate {candidate_sha256[:16]} on task '{task_id}' is pending in queue ({spec_path})",
            spec_id=spec.spec_id,
            spec_path=spec_path,
            candidate_hash=candidate_sha256,
            task_id=task_id,
            decision=decision,
        )

    def write_comparison_spec(self, candidate_ids: list[str]) -> Path:
        """Write an exploratory CohortComparisonSpec comparing two or more candidates across actual job paths.

        Args:
            candidate_ids: List of exact full 'sha256:<64-hex>' candidate digests without prefix ambiguity.

        Returns:
            Path to the written CohortComparisonSpec JSON file.
        """
        if len(candidate_ids) < 2:
            raise ValueError(
                f"CohortComparisonSpec requires at least 2 candidates to compare, got {len(candidate_ids)}"
            )

        cohorts: list[CohortSelector] = []
        coverage_summary: dict[str, Any] = {}
        all_declared_task_ids = set(self._declared_examples.keys())

        for cid in candidate_ids:
            if (
                not isinstance(cid, str)
                or not cid.startswith("sha256:")
                or len(cid) != 71
                or not re.match(r"^sha256:[0-9a-f]{64}$", cid)
            ):
                raise ValueError(
                    f"candidate_ids must be exact full 'sha256:<64-hex>' digests without prefix ambiguity, got {cid!r}"
                )

            matching_records = [r for r in self._records if r.candidate_id == cid]
            if not matching_records:
                raise ValueError(f"No evaluation records found for candidate {cid!r}")

            completed_job_paths: list[str] = []
            covered_task_ids: set[str] = set()

            for r in matching_records:
                if r.status == "completed" and r.job_path is not None and Path(r.job_path).is_dir():
                    covered_task_ids.add(r.task_id)
                    try:
                        rel_path = (
                            Path(r.job_path)
                            .resolve()
                            .relative_to(self.repo_root.resolve())
                            .as_posix()
                        )
                    except ValueError:
                        rel_path = str(r.job_path)
                    if rel_path not in completed_job_paths:
                        completed_job_paths.append(rel_path)

            missing_tasks = sorted(all_declared_task_ids - covered_task_ids)
            coverage_summary[cid] = {
                "covered_tasks": sorted(covered_task_ids),
                "missing_tasks": missing_tasks,
                "coverage_ratio": len(covered_task_ids) / max(1, len(all_declared_task_ids)),
                "completed_job_paths": completed_job_paths,
            }

            if not completed_job_paths:
                raise ValueError(
                    f"Candidate {cid!r} has no completed job paths (coverage: {len(covered_task_ids)}/{len(all_declared_task_ids)} tasks)"
                )

            cohort_label = sanitize_cohort_label(cid)
            cohorts.append(CohortSelector(label=cohort_label, paths=completed_job_paths))

        comparison_id = f"gepa-cmp-{uuid.uuid4().hex[:12]}"
        spec = CohortComparisonSpec(
            schema_version=1,
            comparison_id=comparison_id,
            experiment_id="gepa-optimization",
            declared_variable="preamble_content_sha256",
            mode="exploratory",  # exploratory, no causal claim
            reward_name="reward",
            pass_threshold=1.0,
            pass_k=[1],
            pairing_key="task_digest",
            cohorts=cohorts,
        )

        spec_file = self.output_dir / "comparisons" / f"{comparison_id}.json"
        spec_file.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")

        coverage_file = self.output_dir / "comparisons" / f"{comparison_id}_coverage.json"
        coverage_file.write_text(json.dumps(coverage_summary, indent=2) + "\n", encoding="utf-8")

        return spec_file
