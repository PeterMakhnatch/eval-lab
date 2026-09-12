"""Focused behavioral tests for LabEvaluator.

Validates:
1. Candidate Safety & Immutability:
   - Candidates are UTF-8 text instructions, never executed on host.
   - Content-addressed files fail if existing content differs; never overwritten.
   - Directories restricted inside repository root; symlinks escaping rejected.
2. Dataset Boundaries & Task Integrity:
   - Sealed ('test'/'train') splits, undeclared tasks, path traversals rejected.
   - Incoming example fields compared strictly against immutable copied declaration.
   - Recomputed cryptographic digests match declared package digests.
3. Execution Policy & Agent Profiles:
   - Permitted: controls (oracle/nop with model=None) and native profiles
     (baseline/released/bootstrap-cwd with exact model anthropic/claude-opus-4-6).
   - Local controls execute directly via Executor.execute_direct with RunProvenance.
   - Newly executed and resumed jobs both validated for exact provenance.
   - Model-backed evaluations require genuine positive estimate, submit to queue, never approve.
   - Pending evaluations reuse existing queue spec on resume without duplicate submissions.
   - EvaluationPending raised for pending evaluations (stops optimizer immediately).
4. Result Fidelity & No NaN:
   - Missing rewards and infra errors raise EvaluationUnavailable (never return NaN).
   - Completed receipts matched by exact recorded provenance (task package digest,
     candidate preamble digest, exact model).
   - Resumption checks ONLY exact deterministic job name; no broad scanning.
   - Actual usage extracted from agent_result.
5. Exploratory Comparison Spec Export:
   - Requires exact full 'sha256:<64-hex>' candidate digests without prefix ambiguity.
   - Exports valid CohortComparisonSpec schema (mode='exploratory', no causal claim)
     across real job paths with explicit coverage summary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.execution_contracts import DEEPSEEK_ALLOWED_MODEL, DEEPSEEK_MODEL_SELECTOR
from evallab.gepa_optimizer.evaluator import (
    DEEPSEEK_TARGET_AGENT,
    DEEPSEEK_TARGET_IMPORT_PATH,
    NATIVE_ENTRYPOINTS,
    PERMITTED_NATIVE_MODEL,
    EvaluationPending,
    EvaluationUnavailable,
    ExampleDeclarationError,
    LabEvaluator,
    ProvenanceMismatchError,
    ProviderCeilings,
    TaskDigestMismatchError,
    deterministic_job_name,
)
from evallab.registry import task_directory_digest
from evallab.runner import RunRequest
from evallab.schemas import CohortComparisonSpec, ExperimentSpec

# --- Test Helpers ---


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def create_task_fixture(
    repo_root: Path, task_rel_path: str = "tasks/dev_task_001"
) -> dict[str, Any]:
    """Create a minimal valid task package and return its example declaration dict."""
    task_dir = repo_root / task_rel_path
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        'name = "dev-task-001"\nversion = "1.0"\n', encoding="utf-8"
    )
    (task_dir / "instruction.md").write_text(
        "Complete the assigned task correctly.\n", encoding="utf-8"
    )
    (task_dir / "environment").mkdir(exist_ok=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")

    digest = task_directory_digest(task_dir)
    return {
        "task_id": Path(task_rel_path).name,
        "task_path": task_rel_path,
        "task_package_digest": digest,
        "split": "development",
    }


def create_completed_job_fixture(
    jobs_dir: Path,
    *,
    job_name: str,
    agent: str,
    model: str | None,
    task_id: str,
    task_path: str,
    package_digest: str,
    candidate_sha256: str,
    reward: float | None = 1.0,
    exception_info: str | None = None,
) -> Path:
    """Create a fully compliant completed Harbor job directory on disk."""
    job_dir = jobs_dir / job_name
    trial_name = f"{task_id}__trial01"
    trial_dir = job_dir / trial_name
    agent_config = {
        "name": agent if agent in {"oracle", "nop"} else None,
        "import_path": NATIVE_ENTRYPOINTS.get(agent),
        "model_name": model,
    }
    trial_lock = {
        "schema_version": 2,
        "agent": agent_config,
        "extra_instructions": [{"digest": candidate_sha256}],
    }

    write_json(
        job_dir / "config.json",
        {
            "job_name": job_name,
            "agents": [agent_config],
        },
    )
    write_json(job_dir / "lock.json", {"harbor": {"version": "0.21.0"}, "trials": [trial_lock]})
    write_json(
        job_dir / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "started_at": "2026-09-08T10:00:00Z",
            "finished_at": "2026-09-08T10:01:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0 if not exception_info else 1},
        },
    )

    # Actual native producer writes RunProvenance into metadata["experiment"]
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "command": [
            "harbor",
            "run",
            "--agent",
            agent,
            "--extra-instruction-path",
            f"out/candidates/{candidate_sha256.split(':', 1)[-1]}.txt",
        ],
        "started_at": "2026-09-08T10:00:00Z",
        "finished_at": "2026-09-08T10:01:00Z",
        "exit_code": 0 if not exception_info else 1,
        "timed_out": False,
        "timed_out_trial": None,
        "experiment": {
            "spec_id": f"spec-{task_id}",
            "task": task_path,
            "task_path": task_path,
            "task_id": task_id,
            "package_digest": package_digest,
            "preamble_sha256": candidate_sha256,
        },
        "provider_usage": {
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_tokens": 165,
        },
    }
    if model:
        metadata["command"].extend(["--model", model])
    write_json(job_dir / "lab-metadata.json", metadata)

    # Trial level
    write_json(trial_dir / "config.json", {"agent": agent_config})
    write_json(trial_dir / "lock.json", trial_lock)

    rewards_dict: dict[str, float] = {}
    if reward is not None:
        rewards_dict["reward"] = float(reward)

    write_json(
        trial_dir / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "trial_name": trial_name,
            "task_name": task_id,
            "started_at": "2026-09-08T10:00:01Z",
            "finished_at": "2026-09-08T10:00:55Z",
            "agent_info": {"name": agent, "model_name": model},
            "agent_result": {
                "cost_usd": 0.0025 if model else None,
                "n_input_tokens": 120 if model else None,
                "n_cache_tokens": 30 if model else None,
                "n_output_tokens": 45 if model else None,
            },
            "duration_seconds": 54.0,
            "verifier_result": {"rewards": rewards_dict},
            "exception_info": exception_info,
        },
    )

    return job_dir


class MockExecutor:
    """Mock Executor recording interactions with execution boundary."""

    def __init__(self, repo_root: Path, *, auto_create_job: bool = True) -> None:
        self.repo_root = repo_root
        self.direct_requests: list[RunRequest] = []
        self.submitted_specs: list[ExperimentSpec] = []
        self.auto_create_job = auto_create_job

    def execute_direct(self, request: RunRequest, *, ingest: bool = True) -> Path:
        self.direct_requests.append(request)
        job_dir = request.jobs_dir / request.name
        if self.auto_create_job and not job_dir.exists():
            cand_hash = (
                f"sha256:{hashlib.sha256(request.extra_instruction_path.read_bytes()).hexdigest()}"
                if request.extra_instruction_path
                else "sha256:none"
            )
            pkg_digest = (
                request.provenance.package_digest
                if request.provenance and request.provenance.package_digest
                else task_directory_digest(request.task)
            )
            create_completed_job_fixture(
                request.jobs_dir,
                job_name=request.name,
                agent=request.agent,
                model=request.model,
                task_id=request.task.name,
                task_path=str(request.task.relative_to(self.repo_root)),
                package_digest=pkg_digest,
                candidate_sha256=cand_hash,
                reward=1.0,
            )
        return job_dir

    def submit(self, spec: ExperimentSpec) -> tuple[Path, Any]:
        self.submitted_specs.append(spec)
        spec_path = self.repo_root / "queue" / "pending" / f"{spec.agent}-{spec.spec_id}.json"
        write_json(spec_path, spec.model_dump(mode="json"))

        @dataclass
        class MockPolicyDecision:
            admitted: bool = False
            policy_rule: str | None = None
            reason: str = "awaiting operator policy approval"

        return spec_path, MockPolicyDecision()


# --- Test Cases ---


def test_sealed_split_rejected_at_init(tmp_path: Path) -> None:
    """Examples with sealed ('test') or non-development splits are rejected at init."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/sealed_task")
    task["split"] = "test"  # Forbidden

    with pytest.raises(
        ExampleDeclarationError, match="Only development split examples are permitted"
    ):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent="oracle",
        )


def test_nondeclared_example_rejected_at_call(tmp_path: Path) -> None:
    """Evaluating on an undeclared task id raises ExampleDeclarationError."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task1 = create_task_fixture(repo_root, "tasks/task_1")
    task2 = create_task_fixture(repo_root, "tasks/task_2")

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=repo_root / "out",
        examples=[task1],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    with pytest.raises(
        ExampleDeclarationError, match="not an explicitly declared development task"
    ):
        evaluator("Some instruction candidate", task2)


def test_incoming_example_field_mutation_rejected(tmp_path: Path) -> None:
    """Incoming example fields differing from immutable copied declaration are rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=repo_root / "out",
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    tampered_example = dict(task)
    tampered_example["task_path"] = "tasks/other_path"

    with pytest.raises(ExampleDeclarationError, match="differs from immutable declared contract"):
        evaluator("Candidate text", tampered_example)


def test_path_traversal_rejected(tmp_path: Path) -> None:
    """Task paths escaping the repository root are rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    invalid_example = {
        "task_id": "escaped_task",
        "task_path": "../outside/task",
        "task_package_digest": "sha256:" + "0" * 64,
        "split": "development",
    }

    with pytest.raises(ExampleDeclarationError, match="task_path must stay relative"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[invalid_example],
            agent="oracle",
        )


def test_task_package_digest_mismatch_rejected(tmp_path: Path) -> None:
    """Mutating task files causes a cryptographic digest mismatch failure."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    # Tamper with declared digest
    task["task_package_digest"] = "sha256:" + "f" * 64

    with pytest.raises(TaskDigestMismatchError, match="Task package digest mismatch"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent="oracle",
        )


def test_output_and_jobs_dir_outside_repo_rejected(tmp_path: Path) -> None:
    """Output and jobs directories must be located inside the repository root."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    outside_dir = tmp_path / "outside_out"
    outside_dir.mkdir()

    with pytest.raises(ValueError, match="must be inside the repository root"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=outside_dir,
            examples=[task],
            agent="oracle",
        )


def test_immutable_candidate_file_collision_rejected(tmp_path: Path) -> None:
    """Content-addressed candidate files fail on collision; never overwritten."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    candidate1 = "Content A"
    cand1_hash = f"sha256:{hashlib.sha256(candidate1.encode('utf-8')).hexdigest()}"
    digest_hex = cand1_hash.split(":", 1)[-1]

    # Pre-write a file with differing text at the expected hash location
    collision_file = output_dir / "candidates" / f"{digest_hex}.txt"
    collision_file.parent.mkdir(parents=True, exist_ok=True)
    collision_file.write_text("Conflicting Content B", encoding="utf-8")

    with pytest.raises(ValueError, match="Immutable candidate file collision"):
        evaluator(candidate1, task)


def test_control_rejects_model(tmp_path: Path) -> None:
    """Local controls (oracle, nop) must never accept a model."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    with pytest.raises(ValueError, match="the oracle control does not accept a model"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent="oracle",
            model="gpt-5",
        )

    with pytest.raises(ValueError, match="the nop control does not accept a model"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent="nop",
            model=PERMITTED_NATIVE_MODEL,
        )


def test_native_profile_requires_exact_native_model(tmp_path: Path) -> None:
    """Native profiles (baseline, released, bootstrap-cwd) require exact native model anthropic/claude-opus-4-6."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    # Mismatched model
    with pytest.raises(ValueError, match="requires exact native model"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent="released",
            model="anthropic/claude-3-5-sonnet",
        )

    # Valid native profile and model accepted
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=repo_root / "out",
        examples=[task],
        agent="released",
        model=PERMITTED_NATIVE_MODEL,
        estimated_cost_usd=0.25,
        executor=MockExecutor(repo_root),
    )
    assert evaluator.agent == "released"
    assert evaluator.model == PERMITTED_NATIVE_MODEL


def test_local_control_direct_execution(tmp_path: Path) -> None:
    """Local controls execute directly via Executor.execute_direct with candidate bytes."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=executor,
    )

    candidate_text = "Always check bounds before indexing into arrays."
    score, info = evaluator(candidate_text, task)

    # Verify execution path
    assert len(executor.direct_requests) == 1
    req = executor.direct_requests[0]
    assert req.agent == "oracle"
    assert req.model is None
    assert req.extra_instruction_path is not None
    assert req.extra_instruction_path.read_text(encoding="utf-8") == candidate_text
    assert req.provenance is not None
    assert req.provenance.package_digest == task["task_package_digest"]

    # Verify result
    assert score == 1.0
    assert info["status"] == "completed"
    assert info["score"] == 1.0

    # Verify record contract for Main
    assert len(evaluator.records) == 1
    record = evaluator.records[0]
    expected_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"
    assert record.candidate_id == expected_hash
    assert record.candidate_sha256 == expected_hash
    assert record.task_id == "task_1"
    assert record.status == "completed"
    assert record.score == 1.0
    assert isinstance(record.job_path, str)
    assert Path(record.job_path).exists()


def test_pending_model_transition_raises_evaluation_pending(tmp_path: Path) -> None:
    """Model-backed evaluations submit exact ExperimentSpec and raise EvaluationPending (no fake reward)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="released",
        model=PERMITTED_NATIVE_MODEL,
        estimated_cost_usd=0.35,
        executor=executor,
    )

    candidate_text = "Propose systematic test-driven patches."
    expected_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    with pytest.raises(EvaluationPending) as exc_info:
        evaluator(candidate_text, task)

    # Verify exception details
    assert exc_info.value.candidate_hash == expected_hash
    assert exc_info.value.task_id == "task_1"
    assert exc_info.value.spec_path is not None
    assert exc_info.value.spec_path.exists()

    # Verify submitted spec
    assert len(executor.submitted_specs) == 1
    spec = executor.submitted_specs[0]
    assert spec.agent == "released"
    assert spec.model == PERMITTED_NATIVE_MODEL
    assert spec.est_cost_usd == 0.35
    assert spec.purpose == "elicitation"
    assert spec.hypothesis != ""
    assert "_" not in spec.name
    assert spec.extra_instruction_sha256 == expected_hash

    # Verify preserved evaluation artifact on disk
    artifacts = list((output_dir / "evaluations").glob("*.json"))
    assert len(artifacts) == 1
    artifact_payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact_payload["status"] == "pending"
    assert artifact_payload["candidate_sha256"] == expected_hash

    # Verify retained record contract
    assert len(evaluator.records) == 1
    rec = evaluator.records[0]
    assert rec.candidate_id == expected_hash
    assert rec.status == "pending"
    assert rec.score is None
    assert rec.job_path is None


def test_resume_reuses_pending_spec_without_duplicate_submit(tmp_path: Path) -> None:
    """Calling evaluator again while a spec is already pending reuses the spec without resubmitting."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="released",
        model=PERMITTED_NATIVE_MODEL,
        estimated_cost_usd=0.35,
        executor=executor,
    )

    candidate_text = "Candidate instruction."

    # First call: submits to queue
    with pytest.raises(EvaluationPending):
        evaluator(candidate_text, task)
    assert len(executor.submitted_specs) == 1

    # Second call: reuses pending spec, does NOT submit a second spec
    with pytest.raises(EvaluationPending) as exc_info:
        evaluator(candidate_text, task)
    assert len(executor.submitted_specs) == 1
    assert "already pending" in str(exc_info.value)


@pytest.mark.parametrize("reward", [None, float("nan")], ids=["missing", "nonfinite"])
def test_absent_reward_raises_evaluation_unavailable(tmp_path: Path, reward: float | None) -> None:
    """Missing rewards raise EvaluationUnavailable and record status='error' (never return NaN)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    candidate_text = "Return valid JSON formatted outputs."
    cand_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    job_name = deterministic_job_name(
        agent="oracle",
        model=None,
        task_id="task_1",
        candidate_sha256=cand_hash,
    )

    create_completed_job_fixture(
        repo_root / "runs",
        job_name=job_name,
        agent="oracle",
        model=None,
        task_id="task_1",
        task_path="tasks/task_1",
        package_digest=task["task_package_digest"],
        candidate_sha256=cand_hash,
        reward=reward,
    )

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    with pytest.raises(EvaluationUnavailable):
        evaluator(candidate_text, task)

    # In records: score is None, status is error
    assert len(evaluator.records) == 1
    record = evaluator.records[0]
    assert record.score is None
    assert record.status == "error"


def test_infra_error_raises_evaluation_unavailable(tmp_path: Path) -> None:
    """Infrastructure errors raise EvaluationUnavailable and record status='error' (never return NaN)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    candidate_text = "Candidate triggering trial timeout."
    cand_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    job_name = deterministic_job_name(
        agent="oracle",
        model=None,
        task_id="task_1",
        candidate_sha256=cand_hash,
    )

    create_completed_job_fixture(
        repo_root / "runs",
        job_name=job_name,
        agent="oracle",
        model=None,
        task_id="task_1",
        task_path="tasks/task_1",
        package_digest=task["task_package_digest"],
        candidate_sha256=cand_hash,
        reward=None,
        exception_info="TrialTimeoutFailure: exceeded 1200 seconds",
    )

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    with pytest.raises(EvaluationUnavailable, match="TrialTimeoutFailure"):
        evaluator(candidate_text, task)

    assert len(evaluator.records) == 1
    record = evaluator.records[0]
    assert record.score is None
    assert record.status == "error"
    assert "TrialTimeoutFailure" in str(record.error)


def test_resumption_from_matching_completed_job(tmp_path: Path) -> None:
    """Evaluator resumes from exact deterministic job name without re-executing."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    candidate_text = "Instruction with pre-existing execution evidence."
    cand_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    job_name = deterministic_job_name(
        agent="oracle",
        model=None,
        task_id="task_1",
        candidate_sha256=cand_hash,
    )

    job_dir = create_completed_job_fixture(
        repo_root / "runs",
        job_name=job_name,
        agent="oracle",
        model=None,
        task_id="task_1",
        task_path="tasks/task_1",
        package_digest=task["task_package_digest"],
        candidate_sha256=cand_hash,
        reward=1.0,
    )

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=executor,
    )

    score, info = evaluator(candidate_text, task)

    assert len(executor.direct_requests) == 0
    assert score == 1.0
    assert info["job_path"] == str(job_dir)


def test_provenance_mismatch_raises_error(tmp_path: Path) -> None:
    """If exact deterministic job dir exists but metadata provenance differs, raises ProvenanceMismatchError."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    candidate_text = "Instruction A."
    cand_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    job_name = deterministic_job_name(
        agent="oracle",
        model=None,
        task_id="task_1",
        candidate_sha256=cand_hash,
    )

    # Pre-create job with differing recorded preamble_sha256
    create_completed_job_fixture(
        repo_root / "runs",
        job_name=job_name,
        agent="oracle",
        model=None,
        task_id="task_1",
        task_path="tasks/task_1",
        package_digest=task["task_package_digest"],
        candidate_sha256="sha256:" + "0" * 64,  # Differs from cand_hash
        reward=1.0,
    )

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    with pytest.raises(
        ProvenanceMismatchError, match="does not match exact candidate/task/model provenance"
    ):
        evaluator(candidate_text, task)


def test_candidate_never_executes_host_code(tmp_path: Path) -> None:
    """Candidate string containing arbitrary executable Python is never executed on the host."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    marker_file = tmp_path / "malicious_execution_marker.txt"
    dangerous_candidate = f"""import os
Path({str(marker_file)!r}).write_text("HOST_CODE_WAS_EXECUTED")
"""

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=executor,
    )

    evaluator(dangerous_candidate, task)

    # Host execution marker must NOT exist
    assert not marker_file.exists()


def test_write_comparison_spec_generates_valid_exploratory_schema(tmp_path: Path) -> None:
    """write_comparison_spec exports valid exploratory CohortComparisonSpec using exact full digests."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task1 = create_task_fixture(repo_root, "tasks/task_1")
    task2 = create_task_fixture(repo_root, "tasks/task_2")
    output_dir = repo_root / "out"

    executor = MockExecutor(repo_root)
    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task1, task2],
        agent="oracle",
        executor=executor,
    )

    cand1 = "Candidate prompt 1"
    cand2 = "Candidate prompt 2"

    evaluator(cand1, task1)
    evaluator(cand1, task2)
    evaluator(cand2, task1)
    evaluator(cand2, task2)

    cand1_hash = f"sha256:{hashlib.sha256(cand1.encode('utf-8')).hexdigest()}"
    cand2_hash = f"sha256:{hashlib.sha256(cand2.encode('utf-8')).hexdigest()}"

    spec_path = evaluator.write_comparison_spec([cand1_hash, cand2_hash])

    assert spec_path.is_file()
    spec = CohortComparisonSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))

    assert len(spec.cohorts) == 2
    assert spec.declared_variable == "preamble_content_sha256"
    assert spec.mode == "exploratory"  # Exploratory, no causal claim
    assert spec.reward_name == "reward"

    # Verify coverage summary file
    coverage_files = list((output_dir / "comparisons").glob("*_coverage.json"))
    assert len(coverage_files) == 1
    coverage_data = json.loads(coverage_files[0].read_text(encoding="utf-8"))
    assert cand1_hash in coverage_data
    assert coverage_data[cand1_hash]["coverage_ratio"] == 1.0
    assert len(coverage_data[cand1_hash]["missing_tasks"]) == 0


def test_write_comparison_spec_rejects_prefix_ambiguity(tmp_path: Path) -> None:
    """Candidate IDs must be exact full 'sha256:<64-hex>' strings; short prefixes are rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    output_dir = repo_root / "out"

    evaluator = LabEvaluator(
        repo_root=repo_root,
        output_dir=output_dir,
        examples=[task],
        agent="oracle",
        executor=MockExecutor(repo_root),
    )

    with pytest.raises(
        ValueError, match="exact full 'sha256:<64-hex>' digests without prefix ambiguity"
    ):
        evaluator.write_comparison_spec(["short_hash_1", "short_hash_2"])


# --- DeepSeek-first target (HAR-23) ---


def make_ceilings(**overrides: Any) -> ProviderCeilings:
    """Valid ceilings with per-test overrides."""
    values: dict[str, Any] = {
        "max_requests": 4,
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "max_total_tokens": 10000,
        "cost_limit_usd": 0.5,
    }
    values.update(overrides)
    return ProviderCeilings(**values)


_UNSET: Any = object()


def make_deepseek_evaluator(
    repo_root: Path,
    task: dict[str, Any],
    ceilings: Any = _UNSET,
    **kwargs: Any,
) -> LabEvaluator:
    """DeepSeek-targeted evaluator with a recording executor stub."""
    params: dict[str, Any] = {
        "repo_root": repo_root,
        "output_dir": repo_root / "out",
        "examples": [task],
        "agent": DEEPSEEK_TARGET_AGENT,
        "model": DEEPSEEK_MODEL_SELECTOR,
        "estimated_cost_usd": 0.5,
        "ceilings": make_ceilings() if ceilings is _UNSET else ceilings,
        "executor": MockExecutor(repo_root),
    }
    params.update(kwargs)
    return LabEvaluator(**params)


def create_deepseek_job_fixture(
    jobs_dir: Path,
    *,
    job_name: str,
    task_id: str,
    task_path: str,
    package_digest: str,
    candidate_sha256: str,
    ceilings: ProviderCeilings,
    lock_model: str = DEEPSEEK_MODEL_SELECTOR,
    returned_models: tuple[str, ...] = (),
    declared_model: str | None = None,
    include_provider_usage: bool = True,
    limits_override: dict[str, Any] | None = None,
    unresolved_requests: int = 0,
    lock_agent_override: dict[str, Any] | None = None,
    reward: float | None = 1.0,
) -> Path:
    """Completed DeepSeek job as the runner persists it: adapter import path in the
    trial lock, enforced ceilings and one reconciled proxy call per entry of
    ``returned_models`` in the accounting report (empty: no provider-side identity),
    and ``declared_model`` only in Harbor's agent-declared ``agent_info``."""
    job_dir = jobs_dir / job_name
    trial_name = f"{task_id}__trial01"
    trial_dir = job_dir / trial_name
    agent_config = {
        "name": DEEPSEEK_TARGET_IMPORT_PATH,
        "model_name": lock_model,
    }
    if lock_agent_override is not None:
        agent_config.update(lock_agent_override)
    trial_lock = {
        "schema_version": 2,
        "agent": agent_config,
        "extra_instructions": [{"digest": candidate_sha256}],
    }
    write_json(job_dir / "config.json", {"job_name": job_name, "agents": [agent_config]})
    write_json(job_dir / "lock.json", {"harbor": {"version": "0.21.0"}, "trials": [trial_lock]})
    write_json(
        job_dir / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "started_at": "2026-09-08T10:00:00Z",
            "finished_at": "2026-09-08T10:01:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "command": ["harbor", "run", "--agent", DEEPSEEK_TARGET_IMPORT_PATH],
        "started_at": "2026-09-08T10:00:00Z",
        "finished_at": "2026-09-08T10:01:00Z",
        "exit_code": 0,
        "timed_out": False,
        "timed_out_trial": None,
        "experiment": {
            "spec_id": f"spec-{task_id}",
            "task": task_path,
            "task_path": task_path,
            "task_id": task_id,
            "package_digest": package_digest,
            "preamble_sha256": candidate_sha256,
        },
    }
    if include_provider_usage:
        metadata["provider_usage"] = {
            "schema_version": 1,
            "capability_id": "sha256:" + "b" * 64,
            "attempt_id": "gepa-test-attempt",
            "sequence": 0,
            "limits": (
                limits_override if limits_override is not None else ceilings.expected_usage_limits()
            ),
            "totals": {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_micros": 0,
            },
            "unresolved_requests": unresolved_requests,
            "calls": [
                {
                    "call_id": index,
                    "state": "reconciled",
                    "status": 200,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "returned_model": returned_model,
                }
                for index, returned_model in enumerate(returned_models, start=1)
            ],
        }
    write_json(job_dir / "lab-metadata.json", metadata)
    write_json(trial_dir / "config.json", {"agent": agent_config})
    write_json(trial_dir / "lock.json", trial_lock)
    agent_info: dict[str, Any] = {"name": DEEPSEEK_TARGET_AGENT}
    if declared_model is not None:
        agent_info["model_info"] = {"name": declared_model}
    rewards_dict: dict[str, float] = {}
    if reward is not None:
        rewards_dict["reward"] = float(reward)
    write_json(
        trial_dir / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "trial_name": trial_name,
            "task_name": task_id,
            "started_at": "2026-09-08T10:00:01Z",
            "finished_at": "2026-09-08T10:00:55Z",
            "agent_info": agent_info,
            "agent_result": {
                "cost_usd": 0.0025,
                "n_input_tokens": 120,
                "n_cache_tokens": 30,
                "n_output_tokens": 45,
            },
            "duration_seconds": 54.0,
            "verifier_result": {"rewards": rewards_dict},
            "exception_info": None,
        },
    )
    return job_dir


def test_deepseek_target_accepted_with_exact_model_and_ceilings(tmp_path: Path) -> None:
    """The DeepSeek target requires its exact pinned model plus explicit ceilings."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    ceilings = make_ceilings()

    evaluator = make_deepseek_evaluator(repo_root, task, ceilings)

    assert evaluator.model == DEEPSEEK_MODEL_SELECTOR
    assert evaluator.ceilings == ceilings


def test_deepseek_target_rejects_different_model(tmp_path: Path) -> None:
    """Any model other than the exact pinned selector is refused."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    with pytest.raises(ValueError, match="requires exact model"):
        make_deepseek_evaluator(repo_root, task, model="deepseek/deepseek-chat")


def test_deepseek_target_rejects_missing_ceilings(tmp_path: Path) -> None:
    """Unknown ceilings are never coerced: the DeepSeek target refuses to run."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    with pytest.raises(ValueError, match="requires explicit provider ceilings"):
        make_deepseek_evaluator(repo_root, task, ceilings=None)


@pytest.mark.parametrize("agent,model", [("oracle", None), ("baseline", PERMITTED_NATIVE_MODEL)])
def test_non_deepseek_targets_reject_ceilings(
    tmp_path: Path, agent: str, model: str | None
) -> None:
    """Only the DeepSeek runtime implements these provider ceilings."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")

    with pytest.raises(ValueError, match="does not accept provider ceilings"):
        LabEvaluator(
            repo_root=repo_root,
            output_dir=repo_root / "out",
            examples=[task],
            agent=agent,
            model=model,
            ceilings=make_ceilings(),
            executor=MockExecutor(repo_root),
        )


def test_provider_ceilings_reject_non_positive_and_unbounded_totals() -> None:
    """Ceiling boundaries mirror validate_request: positive, total within components."""
    with pytest.raises(ValueError, match="must be a positive integer"):
        make_ceilings(max_requests=0)
    with pytest.raises(ValueError, match="must be a genuine positive finite"):
        make_ceilings(cost_limit_usd=0.0)
    with pytest.raises(ValueError, match="exceeds input plus output"):
        make_ceilings(max_input_tokens=100, max_output_tokens=100, max_total_tokens=201)


def test_deepseek_submitted_spec_carries_ceilings_model_and_digest(tmp_path: Path) -> None:
    """The queued spec pins the model, the candidate digest, and every ceiling."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    ceilings = make_ceilings()
    evaluator = make_deepseek_evaluator(repo_root, task, ceilings)
    candidate_text = "Study the requirements before acting."
    expected_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"

    with pytest.raises(EvaluationPending):
        evaluator(candidate_text, task)

    executor = evaluator.executor
    assert isinstance(executor, MockExecutor)
    assert len(executor.submitted_specs) == 1
    spec = executor.submitted_specs[0]
    assert spec.agent == DEEPSEEK_TARGET_AGENT
    assert spec.model == DEEPSEEK_MODEL_SELECTOR
    assert spec.extra_instruction_sha256 == expected_hash
    assert spec.max_requests == ceilings.max_requests
    assert spec.max_input_tokens == ceilings.max_input_tokens
    assert spec.max_output_tokens == ceilings.max_output_tokens
    assert spec.max_total_tokens == ceilings.max_total_tokens
    assert spec.cost_limit_usd == ceilings.cost_limit_usd


def _resume_deepseek_job(
    tmp_path: Path, candidate_text: str, ceilings: ProviderCeilings, **job_kwargs: Any
) -> tuple[LabEvaluator, dict[str, Any]]:
    """Pre-create the deterministic DeepSeek job dir, then evaluate the candidate."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    cand_hash = f"sha256:{hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}"
    job_name = deterministic_job_name(
        agent=DEEPSEEK_TARGET_AGENT,
        model=DEEPSEEK_MODEL_SELECTOR,
        task_id="task_1",
        candidate_sha256=cand_hash,
    )
    create_deepseek_job_fixture(
        repo_root / "runs",
        job_name=job_name,
        task_id="task_1",
        task_path="tasks/task_1",
        package_digest=task["task_package_digest"],
        candidate_sha256=cand_hash,
        ceilings=ceilings,
        **job_kwargs,
    )
    evaluator = make_deepseek_evaluator(repo_root, task, ceilings)
    return evaluator, task


def test_deepseek_provenance_mismatch_on_locked_trial_model_differs(tmp_path: Path) -> None:
    """A resumed job whose locked trial records a different model never matches."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        ceilings,
        lock_model="deepseek/deepseek-chat",
        returned_models=("deepseek/deepseek-chat",),
    )

    with pytest.raises(ProvenanceMismatchError, match="does not match exact"):
        evaluator("Instruction A.", task)


def test_deepseek_provenance_mismatch_on_ceilings_difference(tmp_path: Path) -> None:
    """A resumed job whose accounting report records different ceilings never matches."""
    ceilings = make_ceilings()
    tampered = dict(ceilings.expected_usage_limits())
    tampered["max_requests"] = tampered["max_requests"] + 1
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        ceilings,
        returned_models=(DEEPSEEK_MODEL_SELECTOR,),
        limits_override=tampered,
    )

    with pytest.raises(ProvenanceMismatchError, match="does not match exact"):
        evaluator("Instruction A.", task)


def test_deepseek_provenance_mismatch_on_missing_accounting(tmp_path: Path) -> None:
    """Missing accounting evidence is never a match for a ceilings-bound target."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        ceilings,
        returned_models=(DEEPSEEK_MODEL_SELECTOR,),
        include_provider_usage=False,
    )

    with pytest.raises(ProvenanceMismatchError, match="does not match exact"):
        evaluator("Instruction A.", task)


def test_deepseek_identity_unknown_when_no_returned_model(tmp_path: Path) -> None:
    """No returned model in the usage record is recorded as unknown, never matched."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(tmp_path, "Instruction A.", ceilings)

    score, info = evaluator("Instruction A.", task)

    assert score == 1.0
    assert info["usage"]["identity"] == {
        "requested_model": DEEPSEEK_MODEL_SELECTOR,
        "observed_model": None,
        "status": "unknown",
    }


def test_deepseek_identity_matched_when_every_call_returns_the_pin(tmp_path: Path) -> None:
    """Verbatim returned models equal to the pin on every proxy call are matched."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        ceilings,
        returned_models=(DEEPSEEK_ALLOWED_MODEL, DEEPSEEK_ALLOWED_MODEL),
    )

    score, info = evaluator("Instruction A.", task)

    assert score == 1.0
    assert info["usage"]["identity"] == {
        "requested_model": DEEPSEEK_MODEL_SELECTOR,
        "observed_model": DEEPSEEK_ALLOWED_MODEL,
        "status": "matched",
    }


def test_deepseek_identity_ignores_agent_declared_model(tmp_path: Path) -> None:
    """Harbor's agent-declared model is a request echo, not provider evidence."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path, "Instruction A.", ceilings, declared_model=DEEPSEEK_MODEL_SELECTOR
    )

    _, info = evaluator("Instruction A.", task)

    assert info["usage"]["identity"]["status"] == "unknown"
    assert info["usage"]["identity"]["observed_model"] is None


@pytest.mark.parametrize(
    "returned_models",
    [("deepseek-chat",), (DEEPSEEK_ALLOWED_MODEL, "deepseek-chat")],
    ids=["differs-from-pin", "calls-disagree"],
)
def test_deepseek_identity_mismatch_raises(
    tmp_path: Path, returned_models: tuple[str, ...]
) -> None:
    """Any proxy call returning a model other than the pin is a provenance error."""
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path, "Instruction A.", ceilings, returned_models=returned_models
    )

    with pytest.raises(ProvenanceMismatchError, match="returned model"):
        evaluator("Instruction A.", task)


def test_deepseek_partial_identity_coverage_stays_unknown(tmp_path: Path) -> None:
    ceilings = make_ceilings()
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        ceilings,
        returned_models=(DEEPSEEK_ALLOWED_MODEL, ""),
    )

    _, info = evaluator("Instruction A.", task)

    assert info["usage"]["identity"]["status"] == "unknown"
    assert info["usage"]["identity"]["observed_model"] == DEEPSEEK_ALLOWED_MODEL


def test_deepseek_resume_rejects_conflicting_adapter_identity(tmp_path: Path) -> None:
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        make_ceilings(),
        lock_agent_override={"import_path": "wrong.module:Agent"},
    )
    with pytest.raises(ProvenanceMismatchError, match="does not match exact"):
        evaluator("Instruction A.", task)


def test_deepseek_resume_preserves_unresolved_accounting_hold(tmp_path: Path) -> None:
    evaluator, task = _resume_deepseek_job(
        tmp_path,
        "Instruction A.",
        make_ceilings(),
        unresolved_requests=1,
        returned_models=(DEEPSEEK_ALLOWED_MODEL,),
    )
    with pytest.raises(ProvenanceMismatchError, match="does not match exact"):
        evaluator("Instruction A.", task)


def test_pending_deepseek_evaluation_cannot_change_ceilings_on_resume(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task = create_task_fixture(repo_root, "tasks/task_1")
    initial = make_deepseek_evaluator(repo_root, task, make_ceilings(max_requests=4))
    with pytest.raises(EvaluationPending):
        initial("Instruction A.", task)
    resumed = make_deepseek_evaluator(repo_root, task, make_ceilings(max_requests=1))
    with pytest.raises(ProvenanceMismatchError, match="exact provider ceilings"):
        resumed("Instruction A.", task)
    assert resumed.executor.submitted_specs == []
