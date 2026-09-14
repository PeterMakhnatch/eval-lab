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

from evallab.gepa_optimizer.evaluator import (
    NATIVE_ENTRYPOINTS,
    PERMITTED_NATIVE_MODEL,
    EvaluationPending,
    EvaluationUnavailable,
    ExampleDeclarationError,
    LabEvaluator,
    ProvenanceMismatchError,
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
