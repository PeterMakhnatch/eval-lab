from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.canary import load_canary_suite
from evallab.queue import DirectoryQueue, Executor, PaidRunAuthorization, PolicyGate
from evallab.registry import (
    TaskComponentMissingError,
    TaskControlEvidenceError,
    TaskDigestMismatchError,
    TaskNotRegisteredError,
    TaskPathRedirectionError,
    TaskRegistry,
    TaskStateInvalidError,
    TaskUsageNotAllowedError,
    TaskVersionMismatchError,
    audit_registry,
    compute_task_digests,
    inventory_tasks,
    promote_task,
    register_task,
)
from evallab.researchers import ResearcherLoop
from evallab.schemas import (
    ControlEvidenceRef,
    ExperimentSpec,
    StandingApprovalsPolicy,
    TaskControlEvidence,
    TaskLimits,
    TaskRegistryRecord,
)


def _make_dummy_task(
    root: Path,
    rel_path: str = "library/tasks/sample-task",
    *,
    instruction: str = "Solve this task.",
    verifier: str = "def test_solution(): assert True\n",
) -> Path:
    task_dir = root / rel_path
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text('schema_version = "1.4"\n[task]\nname = "sample"\n')
    (task_dir / "instruction.md").write_text(instruction)
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_task.py").write_text(verifier)
    return task_dir


def _make_control_evidence(
    root: Path,
    task_id: str,
    *,
    oracle_reward: float = 1.0,
    nop_reward: float = 0.0,
    oracle_agent: str = "oracle",
    nop_agent: str = "nop",
) -> tuple[ControlEvidenceRef, ControlEvidenceRef]:
    runs_dir = root / "research/evidence/runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Oracle evidence
    oracle_job = f"{task_id}-oracle-evidence"
    oracle_dir = runs_dir / oracle_job
    oracle_dir.mkdir(parents=True, exist_ok=True)
    oracle_payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "started_at": "2026-08-15T12:00:00Z",
        "stats": {
            "evals": {
                f"{oracle_agent}__adhoc": {
                    "metrics": [{"reward": oracle_reward}],
                }
            }
        },
    }
    oracle_file = oracle_dir / "result.json"
    oracle_file.write_text(json.dumps(oracle_payload, indent=2))
    oracle_digest = f"sha256:{hashlib.sha256(oracle_file.read_bytes()).hexdigest()}"
    oracle_ref = ControlEvidenceRef(
        job_name=oracle_job,
        reward=oracle_reward,
        evidence_path=oracle_file.relative_to(root).as_posix(),
        evidence_digest=oracle_digest,
        observed_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )

    # Nop evidence
    nop_job = f"{task_id}-nop-evidence"
    nop_dir = runs_dir / nop_job
    nop_dir.mkdir(parents=True, exist_ok=True)
    nop_payload = {
        "id": "22222222-2222-2222-2222-222222222222",
        "started_at": "2026-08-15T12:05:00Z",
        "stats": {
            "evals": {
                f"{nop_agent}__adhoc": {
                    "metrics": [{"reward": nop_reward}],
                }
            }
        },
    }
    nop_file = nop_dir / "result.json"
    nop_file.write_text(json.dumps(nop_payload, indent=2))
    nop_digest = f"sha256:{hashlib.sha256(nop_file.read_bytes()).hexdigest()}"
    nop_ref = ControlEvidenceRef(
        job_name=nop_job,
        reward=nop_reward,
        evidence_path=nop_file.relative_to(root).as_posix(),
        evidence_digest=nop_digest,
        observed_at=datetime(2026, 8, 15, 12, 5, tzinfo=UTC),
    )

    return oracle_ref, nop_ref


def _make_registry_record(
    task_dir: Path,
    repo_root: Path,
    *,
    task_id: str = "sample-task",
    version: str = "1.0.0",
    state: str = "registered",
    allowed_uses: list[str] | None = None,
    provenance_zone: str = "02-local-evidence",
    source_ref: str = "main",
    license_str: str = "MIT",
    create_control_evidence: bool = True,
    oracle_reward: float = 1.0,
    nop_reward: float = 0.0,
    approved_by: str | None = "Peter Makhnatch",
    approved_at: datetime | None = datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
) -> TaskRegistryRecord:
    rel_path = task_dir.relative_to(repo_root).as_posix()
    digests = compute_task_digests(task_dir)

    if create_control_evidence:
        oracle_ref, nop_ref = _make_control_evidence(
            repo_root,
            task_id,
            oracle_reward=oracle_reward,
            nop_reward=nop_reward,
        )
    else:
        oracle_ref = ControlEvidenceRef(
            job_name=f"{task_id}-oracle",
            reward=oracle_reward,
            evidence_path=f"research/evidence/runs/{task_id}-oracle/result.json",
            evidence_digest="sha256:" + "0" * 64,
            observed_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        )
        nop_ref = ControlEvidenceRef(
            job_name=f"{task_id}-nop",
            reward=nop_reward,
            evidence_path=f"research/evidence/runs/{task_id}-nop/result.json",
            evidence_digest="sha256:" + "0" * 64,
            observed_at=datetime(2026, 8, 15, 12, 5, tzinfo=UTC),
        )

    return TaskRegistryRecord(
        schema_version=1,
        task_id=task_id,
        version=version,
        task_path=rel_path,
        digests=digests,
        source_uri=f"local/{task_id}@{version}",
        source_ref=source_ref,
        license=license_str,
        provenance_zone=provenance_zone,  # type: ignore[arg-type]
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=1800),
        control_evidence=TaskControlEvidence(
            oracle=oracle_ref,
            nop=nop_ref,
        ),
        state=state,  # type: ignore[arg-type]
        allowed_uses=allowed_uses or ["measurement", "training"],  # type: ignore[arg-type]
        approved_by=approved_by if state == "registered" else None,
        approved_at=approved_at if state == "registered" else None,
    )


def test_no_registry_present_means_zero_registered_tasks(tmp_path: Path) -> None:
    reg = TaskRegistry.from_repo(tmp_path)
    assert len(reg.records) == 0
    assert reg.list_records("registered") == []


def test_task_toml_existence_does_not_imply_registration(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/unregistered-task")
    assert (task_dir / "task.toml").is_file()

    reg = TaskRegistry.from_repo(tmp_path)
    assert reg.get("unregistered-task") is None

    spec = ExperimentSpec(
        name="test-unregistered",
        hypothesis="test",
        purpose="practice",
        task="registered/unregistered-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskNotRegisteredError):
        reg.resolve_spec(spec, tmp_path)


def test_candidate_record_cannot_back_registered_work(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/candidate-task")
    record = _make_registry_record(task_dir, tmp_path, task_id="candidate-task", state="candidate")
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "candidate-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    assert reg.get("candidate-task") is not None
    assert reg.get("candidate-task").state == "candidate"

    spec = ExperimentSpec(
        name="test-candidate",
        hypothesis="test",
        purpose="practice",
        task="registered/candidate-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskStateInvalidError):
        reg.resolve_spec(spec, tmp_path)


def test_valid_registered_fixture_resolves_deterministically(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/valid-task")
    record = _make_registry_record(task_dir, tmp_path, task_id="valid-task", state="registered")
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "valid-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-valid",
        hypothesis="test",
        purpose="practice",
        task="registered/valid-task",
        task_path="library/tasks/valid-task",
        task_version="1.0.0",
        agent="codex",
        submitted_by="test",
    )
    resolved = reg.resolve_spec(spec, tmp_path)
    assert resolved is not None
    assert resolved.task_id == "valid-task"
    assert resolved.state == "registered"


def test_changed_task_bytes_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/tampered-task")
    record = _make_registry_record(task_dir, tmp_path, task_id="tampered-task", state="registered")
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "tampered-task.json").write_text(record.model_dump_json(indent=2))

    # Mutate instruction on disk
    (task_dir / "instruction.md").write_text("Modified instructions.")

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-tampered",
        hypothesis="test",
        purpose="practice",
        task="registered/tampered-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskDigestMismatchError) as exc_info:
        reg.resolve_spec(spec, tmp_path)
    assert "instruction bytes on disk have changed" in str(exc_info.value)


def test_changed_verifier_bytes_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/tampered-verifier")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="tampered-verifier", state="registered"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "tampered-verifier.json").write_text(record.model_dump_json(indent=2))

    # Mutate verifier test
    (task_dir / "tests/test_task.py").write_text("def test_modified(): assert False\n")

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-tampered-verifier",
        hypothesis="test",
        purpose="practice",
        task="registered/tampered-verifier",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskDigestMismatchError) as exc_info:
        reg.resolve_spec(spec, tmp_path)
    assert "verifier bytes on disk have changed" in str(exc_info.value)


def test_task_path_redirection_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/original-task")
    _make_dummy_task(tmp_path, "library/tasks/other-task")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="original-task", state="registered"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "original-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-redirect",
        hypothesis="test",
        purpose="practice",
        task="registered/original-task",
        task_path="library/tasks/other-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskPathRedirectionError):
        reg.resolve_spec(spec, tmp_path)


def test_task_version_mismatch_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/versioned-task")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="versioned-task", version="1.0.0", state="registered"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "versioned-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-version-mismatch",
        hypothesis="test",
        purpose="practice",
        task="registered/versioned-task",
        task_version="2.0.0",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskVersionMismatchError):
        reg.resolve_spec(spec, tmp_path)


def test_omitted_task_path_resolves_canonical_path(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/omitted-path-task")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="omitted-path-task", state="registered"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "omitted-path-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-omitted-path",
        hypothesis="test",
        purpose="practice",
        task="registered/omitted-path-task",
        task_path=None,
        agent="codex",
        submitted_by="test",
    )
    resolved = reg.resolve_spec(spec, tmp_path)
    assert resolved is not None
    assert spec.task_path == "library/tasks/omitted-path-task"


def test_control_evidence_missing_file_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/missing-evidence-task")
    record = _make_registry_record(
        task_dir,
        tmp_path,
        task_id="missing-evidence-task",
        state="registered",
        create_control_evidence=False,
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "missing-evidence-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-missing-evidence",
        hypothesis="test",
        purpose="practice",
        task="registered/missing-evidence-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskControlEvidenceError) as exc_info:
        reg.resolve_spec(spec, tmp_path)
    assert "missing on disk" in str(exc_info.value)


def test_control_evidence_digest_mismatch_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/tampered-evidence-task")
    record = _make_registry_record(
        task_dir,
        tmp_path,
        task_id="tampered-evidence-task",
        state="registered",
        create_control_evidence=True,
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "tampered-evidence-task.json").write_text(record.model_dump_json(indent=2))

    # Mutate oracle evidence file on disk
    oracle_file = tmp_path / record.control_evidence.oracle.evidence_path
    oracle_file.write_text('{"tampered": true}')

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-tampered-evidence",
        hypothesis="test",
        purpose="practice",
        task="registered/tampered-evidence-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskControlEvidenceError) as exc_info:
        reg.resolve_spec(spec, tmp_path)
    assert "digest mismatch" in str(exc_info.value)


def test_training_only_task_cannot_be_used_for_measurement(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/training-only-task")
    record = _make_registry_record(
        task_dir,
        tmp_path,
        task_id="training-only-task",
        state="registered",
        allowed_uses=["training"],
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "training-only-task.json").write_text(record.model_dump_json(indent=2))

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-training-only",
        hypothesis="test",
        purpose="practice",
        task="registered/training-only-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskUsageNotAllowedError):
        reg.resolve_spec(spec, tmp_path)


def test_package_missing_component_causes_refusal(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/incomplete-task")
    record = _make_registry_record(
        task_dir,
        tmp_path,
        task_id="incomplete-task",
        state="registered",
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "incomplete-task.json").write_text(record.model_dump_json(indent=2))

    # Remove verifier directory
    import shutil

    shutil.rmtree(task_dir / "tests")

    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-incomplete",
        hypothesis="test",
        purpose="practice",
        task="registered/incomplete-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskComponentMissingError):
        reg.resolve_spec(spec, tmp_path)


def test_policy_gate_refuses_unregistered_tasks(tmp_path: Path) -> None:
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[
            {"name": "researcher-followups", "tasks": ["registered/*"], "agents": ["codex"]}
        ],
    )
    gate = PolicyGate(policy, repo_root=tmp_path)
    spec = ExperimentSpec(
        name="test-policy-refusal",
        hypothesis="test",
        purpose="practice",
        task="registered/non-existent",
        agent="codex",
        est_cost_usd=1.0,
        submitted_by="test",
    )
    decision = gate.decide(spec, spent_today_usd=0.0)
    assert not decision.admitted
    assert decision.reason_code == "unregistered_task"


def _authorization(spec: ExperimentSpec) -> PaidRunAuthorization:
    """The recorded human authorisation that paid work now requires."""
    return PaidRunAuthorization(
        spec_id=str(spec.spec_id),
        actor="peter",
        authorized_at=spec.submitted_at or datetime.now(UTC),
    )


def test_human_approval_cannot_bypass_unregistered_or_candidate(tmp_path: Path) -> None:
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": "test-controls", "agents": ["oracle"]}],
    )
    gate = PolicyGate(policy, repo_root=tmp_path)

    # 1. Unregistered task
    spec_unregistered = ExperimentSpec(
        name="test-human-approved-unregistered",
        hypothesis="test",
        purpose="practice",
        task="registered/unregistered-bypass",
        agent="codex",
        policy_rule="human-approval",
        est_cost_usd=1.0,
        submitted_by="test",
    )
    decision = gate.decide(
        spec_unregistered,
        spent_today_usd=0.0,
        authorization=_authorization(spec_unregistered),
    )
    assert not decision.admitted
    assert decision.reason_code == "unregistered_task"

    # 2. Candidate task
    task_dir = _make_dummy_task(tmp_path, "library/tasks/candidate-bypass")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="candidate-bypass", state="candidate"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "candidate-bypass.json").write_text(record.model_dump_json(indent=2))

    spec_candidate = ExperimentSpec(
        name="test-human-approved-candidate",
        hypothesis="test",
        purpose="practice",
        task="registered/candidate-bypass",
        agent="codex",
        policy_rule="human-approval",
        est_cost_usd=1.0,
        submitted_by="test",
    )
    decision2 = gate.decide(
        spec_candidate,
        spent_today_usd=0.0,
        authorization=_authorization(spec_candidate),
    )
    assert not decision2.admitted
    assert decision2.reason_code == "task_not_registered"


def test_executor_tick_end_to_end_dispatch_and_provenance(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    queue = DirectoryQueue(queue_root)

    # Set up valid registered task
    task_dir = _make_dummy_task(tmp_path, "library/tasks/dispatched-task")
    record = _make_registry_record(
        task_dir, tmp_path, task_id="dispatched-task", state="registered"
    )
    reg_dir = tmp_path / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "dispatched-task.json").write_text(record.model_dump_json(indent=2))

    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[
            {"name": "registered-runs", "tasks": ["registered/*"], "agents": ["codex"]}
        ],
    )

    captured_requests = []

    def stub_runner(req):
        captured_requests.append(req)
        job_dir = tmp_path / "runs" / req.name
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy,
        runner=stub_runner,
        ingester=lambda path: None,
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: {"codex_auth", "claude_oauth"},
    )

    # Submit spec with omitted task_path
    spec = ExperimentSpec(
        name="test-dispatched-spec",
        hypothesis="test",
        purpose="practice",
        task="registered/dispatched-task",
        task_path=None,
        agent="codex",
        model="gpt-5",
        attempts=1,
        est_cost_usd=1.0,
        submitted_by="test",
    )
    waiting, decision = executor.submit(spec)
    # Billable work never auto-runs: it is authorised one spec at a time.
    assert decision.reason_code == "paid_run_unauthorized"
    path = queue.approve(str(queue.load(waiting).spec_id), actor="peter")
    assert path.parent.name == "approved"

    # Run executor tick
    dispatched = executor.tick()
    assert dispatched == 1
    assert len(captured_requests) == 1

    req = captured_requests[0]
    assert req.task == (tmp_path / "library/tasks/dispatched-task").resolve()
    assert req.provenance.package_digest == record.digests.package
    assert req.provenance.verifier_digest == record.digests.verifier
    assert req.provenance.task_path == "library/tasks/dispatched-task"


def test_researcher_loop_preflight_makes_zero_invoker_calls_when_empty(tmp_path: Path) -> None:
    _make_dummy_task(tmp_path, "library/tasks/some-task")
    invoker_called = False

    def stub_invoker(*args, **kwargs):
        nonlocal invoker_called
        invoker_called = True
        raise AssertionError("invoker must not be called when registry is empty")

    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[
            {"name": "researcher-followups", "tasks": ["registered/*"], "agents": ["codex"]}
        ],
    )
    loop = ResearcherLoop(
        repo_root=tmp_path,
        invoker=stub_invoker,
        policy=policy,
        evidence_loader=lambda day, path: None,  # type: ignore[arg-type]
    )

    res = loop.run()
    assert res.deferred_reason == "no_registered_tasks"
    assert res.invocation_count == 0
    assert not invoker_called


def test_canaries_remain_independent_under_own_policy(tmp_path: Path) -> None:
    real_repo = Path(__file__).resolve().parents[1]
    suite = load_canary_suite(real_repo / "policy/canary-suite.yaml")
    assert len(suite.members) == 3

    for member in suite.members:
        task_path = real_repo / member.task_path
        assert task_path.is_dir()
        digests = compute_task_digests(task_path)
        assert digests.package == member.task_digest


def test_audit_reports_malformed_queue_specs_without_swallowing_errors(tmp_path: Path) -> None:
    q_proposed = tmp_path / "queue/proposed"
    q_proposed.mkdir(parents=True, exist_ok=True)
    (q_proposed / "corrupted.json").write_text("{ unparseable json garbage ...")

    report = audit_registry(tmp_path)
    assert not report.passed
    malformed = [f for f in report.findings if f.category == "malformed_queue_spec"]
    assert len(malformed) == 1
    assert "corrupted.json" in malformed[0].target


def test_audit_detects_false_registration_pattern(tmp_path: Path) -> None:
    q_proposed = tmp_path / "queue/proposed"
    q_proposed.mkdir(parents=True, exist_ok=True)
    spec_payload = {
        "schema_version": 1,
        "spec_id": "01M023RP03KGSHB4WZ29WE9DGR",
        "name": "research-event-summary-b51328c0cd",
        "hypothesis": "Test hypothesis",
        "purpose": "practice",
        "task": "registered/event-summary",
        "task_path": "library/tasks/event-summary",
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "environment": "docker",
        "jobs_dir": "runs",
        "attempts": 1,
        "concurrency": 1,
        "timeout_seconds": 1800,
        "submitted_by": "autopilot-researcher",
        "priority": 200,
        "est_cost_usd": 3.0,
        "requires": ["schema_valid", "dedup_pass", "calibrated_judges_only"],
    }
    (q_proposed / "codex-01M023RP03KGSHB4WZ29WE9DGR.json").write_text(
        json.dumps(spec_payload, indent=2)
    )

    report = audit_registry(tmp_path)
    assert not report.passed
    false_claims = [f for f in report.findings if f.category == "false_registered_claim"]
    assert len(false_claims) == 1
    assert "event-summary" in false_claims[0].message


def test_task_registry_schema_strictness(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/schema-test")
    digests = compute_task_digests(task_dir)

    # 1. Unknown fields fail
    invalid_data = {
        "schema_version": 1,
        "task_id": "schema-test",
        "version": "1.0.0",
        "task_path": "library/tasks/schema-test",
        "digests": digests.model_dump(mode="json"),
        "source_uri": "local/schema-test@1.0.0",
        "provenance_zone": "02-local-evidence",
        "is_synthetic": False,
        "control_evidence": {
            "oracle": {"job_name": "oracle-job", "reward": 1.0},
            "nop": {"job_name": "nop-job", "reward": 0.0},
        },
        "state": "candidate",
        "allowed_uses": ["measurement"],
        "unexpected_extra_field": "disallowed",
    }
    with pytest.raises(ValidationError):
        TaskRegistryRecord.model_validate(invalid_data)

    # 2. Registered state requires approved_by and approved_at
    unapproved_data = dict(invalid_data)
    del unapproved_data["unexpected_extra_field"]
    unapproved_data["state"] = "registered"
    with pytest.raises(ValidationError) as exc_info:
        TaskRegistryRecord.model_validate(unapproved_data)
    assert "approved_by" in str(exc_info.value)


def test_inventory_tasks_categorization(tmp_path: Path) -> None:
    _make_dummy_task(tmp_path, "library/tasks/task-a")
    _make_dummy_task(tmp_path, "library/benchmarks/benchmark-b")
    _make_dummy_task(tmp_path, "library/adapters/quixbugs/src/quixbugs/task-template")

    curated_dir = tmp_path / "library/curated/card-c"
    curated_dir.mkdir(parents=True, exist_ok=True)
    (curated_dir / "CARD.md").write_text("# Card C\n")

    inv = inventory_tasks(tmp_path)
    assert inv.total_packages == 4
    assert inv.runnable_packages == 2
    assert inv.curated_cards_only == 1
    assert inv.template_packages == 1

def _make_control_job(
    root: Path,
    task_dir: Path,
    agent: str,
    reward: float,
    *,
    job_name: str | None = None,
    jobs_dir: str = "runs",
) -> Path:
    runs_dir = root / jobs_dir
    runs_dir.mkdir(parents=True, exist_ok=True)
    task_id = task_dir.name
    job_name = job_name or f"gymv0-{agent}-{task_id}"
    job_dir = runs_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "job_name": job_name,
        "jobs_dir": str(runs_dir),
        "tasks": [{"path": str(task_dir.resolve())}],
    }
    (job_dir / "config.json").write_text(json.dumps(config, indent=2))

    payload = {
        "id": f"{agent}-id-12345",
        "started_at": "2026-08-19T12:00:00Z",
        "finished_at": "2026-08-19T12:01:00Z",
        "n_total_trials": 1,
        "stats": {
            "n_completed_trials": 1,
            "evals": {
                f"{agent}__adhoc": {
                    "metrics": [{"reward": reward}],
                }
            },
        },
    }
    (job_dir / "result.json").write_text(json.dumps(payload, indent=2))
    return job_dir


def test_promote_task_discovers_control_evidence_and_creates_candidate(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/event-summary")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    record = promote_task("library/tasks/event-summary", tmp_path)
    assert record.task_id == "event-summary"
    assert record.version == "1.0.0"
    assert record.state == "candidate"
    assert record.approved_by is None
    assert record.approved_at is None
    assert record.control_evidence.oracle.reward == 1.0
    assert record.control_evidence.nop.reward == 0.0
    assert record.digests.package.startswith("sha256:")
    assert record.digests.verifier.startswith("sha256:")
    assert record.digests.task_toml.startswith("sha256:")
    assert record.digests.instruction.startswith("sha256:")
    assert record.digests.environment.startswith("sha256:")

    record_file = tmp_path / "library/registry/event-summary.json"
    assert record_file.is_file()

    reg = TaskRegistry.from_repo(tmp_path)
    loaded = reg.get("event-summary")
    assert loaded is not None
    assert loaded.state == "candidate"
    assert loaded.digests.package == record.digests.package


def test_promote_task_refuses_when_oracle_evidence_missing(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/no-oracle-task")
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    with pytest.raises(TaskControlEvidenceError) as exc_info:
        promote_task("library/tasks/no-oracle-task", tmp_path)

    err = str(exc_info.value)
    assert "missing oracle control evidence" in err
    cmd = (
        "uv run python -m evallab.cli run --task library/tasks/no-oracle-task "
        "--agent oracle --name no-oracle-task-oracle --jobs-dir runs"
    )
    assert cmd in err

def test_promote_task_refuses_when_nop_evidence_missing(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/no-nop-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)

    with pytest.raises(TaskControlEvidenceError) as exc_info:
        promote_task("library/tasks/no-nop-task", tmp_path)

    err = str(exc_info.value)
    assert "missing nop control evidence" in err
    cmd = (
        "uv run python -m evallab.cli run --task library/tasks/no-nop-task "
        "--agent nop --name no-nop-task-nop --jobs-dir runs"
    )
    assert cmd in err

def test_promote_task_refuses_contradictory_oracle_evidence(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/broken-oracle-task")
    _make_control_job(tmp_path, task_dir, "oracle", 0.0)  # Oracle failed!
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    with pytest.raises(TaskControlEvidenceError) as exc_info:
        promote_task("library/tasks/broken-oracle-task", tmp_path)

    err = str(exc_info.value)
    assert "oracle control evidence for 'broken-oracle-task' did not pass" in err
    assert "instrument is broken" in err


def test_promote_task_refuses_contradictory_nop_evidence(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/broken-nop-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 1.0)  # Nop passed!

    with pytest.raises(TaskControlEvidenceError) as exc_info:
        promote_task("library/tasks/broken-nop-task", tmp_path)

    err = str(exc_info.value)
    assert "nop control evidence for 'broken-nop-task' did not fail" in err
    assert "instrument is broken" in err


def test_register_task_requires_actor_and_records_approval(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/promoted-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    cand = promote_task("library/tasks/promoted-task", tmp_path)
    assert cand.state == "candidate"

    # Candidate cannot resolve registered/*
    reg = TaskRegistry.from_repo(tmp_path)
    spec = ExperimentSpec(
        name="test-exp",
        task="registered/promoted-task",
        agent="oracle",
        hypothesis="test hypothesis",
        purpose="baseline",
        submitted_by="Peter Makhnatch",
    )
    with pytest.raises(TaskStateInvalidError):
        reg.resolve_spec(spec, tmp_path)

    # Now register with actor
    registered = register_task("promoted-task", actor="Peter Makhnatch", repo_root=tmp_path)
    assert registered.state == "registered"
    assert registered.approved_by == "Peter Makhnatch"
    assert registered.approved_at is not None

    # Now resolve_spec succeeds
    reg_reloaded = TaskRegistry.from_repo(tmp_path)
    resolved = reg_reloaded.resolve_spec(spec, tmp_path)
    assert resolved is not None
    assert resolved.state == "registered"
    assert resolved.approved_by == "Peter Makhnatch"


def test_register_task_without_actor_refuses(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/unapproved-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    promote_task("library/tasks/unapproved-task", tmp_path)

    with pytest.raises(ValueError) as exc_info:
        register_task("unapproved-task", actor="", repo_root=tmp_path)
    assert "registered task records require approved_by / --actor" in str(exc_info.value)


def test_promote_task_idempotence_unchanged_package(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/idempotent-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    record1 = promote_task("library/tasks/idempotent-task", tmp_path)
    record2 = promote_task("library/tasks/idempotent-task", tmp_path)

    assert record1.digests.package == record2.digests.package
    assert record1.version == record2.version
    assert record1.state == record2.state


def test_promote_task_refuses_tampered_package_without_version_bump(tmp_path: Path) -> None:
    task_dir = _make_dummy_task(tmp_path, "library/tasks/tampered-bump-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    promote_task("library/tasks/tampered-bump-task", tmp_path, version="1.0.0")

    # Tamper with instruction on disk
    (task_dir / "instruction.md").write_text("Modified instruction bytes on disk.\n")

    with pytest.raises(TaskDigestMismatchError) as exc_info:
        promote_task("library/tasks/tampered-bump-task", tmp_path, version="1.0.0")

    err = str(exc_info.value)
    assert "task package bytes on disk have changed" in err
    assert "bump --version to register a new version" in err

    # Bumping version succeeds
    record_v2 = promote_task("library/tasks/tampered-bump-task", tmp_path, version="1.0.1")
    assert record_v2.version == "1.0.1"


def test_cli_registry_promote_and_register_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from evallab.cli import (
        _registry_audit_command,
        _registry_list_command,
        _registry_promote_command,
        _registry_register_command,
    )

    task_dir = _make_dummy_task(tmp_path, "library/tasks/cli-test-task")
    _make_control_job(tmp_path, task_dir, "oracle", 1.0)
    _make_control_job(tmp_path, task_dir, "nop", 0.0)

    # 1. Promote via CLI
    promote_args = argparse.Namespace(
        task_path="library/tasks/cli-test-task",
        task_id=None,
        version=None,
        source_uri=None,
        source_ref=None,
        license=None,
        provenance_zone=None,
        synthetic=False,
        timeout_seconds=None,
        max_memory_mb=None,
        max_cpus=None,
        allowed_uses=None,
        human_minutes=None,
        state="candidate",
        actor=None,
        register=False,
        jobs_dir=None,
        registry_dir=str(tmp_path / "library/registry"),
        json=False,
    )
    exit_code = _registry_promote_command(promote_args, tmp_path)
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "promoted: cli-test-task@1.0.0 (state: candidate)" in out

    # 2. List shows candidate
    list_args = argparse.Namespace(state=None, json=False)
    # Point registry from_repo to tmp_path
    exit_code = _registry_list_command(list_args, tmp_path)
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "cli-test-task" in out
    assert "candidate" in out

    # 3. Register via CLI
    reg_args = argparse.Namespace(
        task_id="cli-test-task",
        actor="Peter Makhnatch",
        registry_dir=str(tmp_path / "library/registry"),
        json=False,
    )
    exit_code = _registry_register_command(reg_args, tmp_path)
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "registered: cli-test-task@1.0.0 (state: registered)" in out
    assert "approved by: Peter Makhnatch" in out

    # 4. Audit passes
    audit_args = argparse.Namespace(json=False)
    exit_code = _registry_audit_command(audit_args, tmp_path)
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "PASS: zero audit findings" in out


def test_cli_registry_promote_refuses_missing_evidence_with_exact_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from evallab.cli import _registry_promote_command

    _make_dummy_task(tmp_path, "library/tasks/missing-evidence")
    promote_args = argparse.Namespace(
        task_path="library/tasks/missing-evidence",
        task_id=None,
        version=None,
        source_uri=None,
        source_ref=None,
        license=None,
        provenance_zone=None,
        synthetic=False,
        timeout_seconds=None,
        max_memory_mb=None,
        max_cpus=None,
        allowed_uses=None,
        human_minutes=None,
        state="candidate",
        actor=None,
        register=False,
        jobs_dir=None,
        registry_dir=str(tmp_path / "library/registry"),
        json=False,
    )
    exit_code = _registry_promote_command(promote_args, tmp_path)
    assert exit_code == 1
    _, err = capsys.readouterr()
    assert "missing oracle control evidence" in err
    cmd = (
        "uv run python -m evallab.cli run --task library/tasks/missing-evidence "
        "--agent oracle --name missing-evidence-oracle --jobs-dir runs"
    )
    assert cmd in err
