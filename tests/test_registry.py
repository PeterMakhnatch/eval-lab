from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.canary import load_canary_suite
from evallab.queue import PolicyGate
from evallab.registry import (
    TaskDigestMismatchError,
    TaskNotRegisteredError,
    TaskPathRedirectionError,
    TaskRegistry,
    TaskStateInvalidError,
    TaskVersionMismatchError,
    audit_registry,
    compute_task_digests,
    inventory_tasks,
)
from evallab.researchers import ResearcherDeferred, ResearcherLoop
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


def _make_registry_record(
    task_dir: Path,
    repo_root: Path,
    *,
    task_id: str = "sample-task",
    version: str = "1.0.0",
    state: str = "registered",
    oracle_reward: float = 1.0,
    nop_reward: float = 0.0,
    approved_by: str | None = "Peter Makhnatch",
    approved_at: datetime | None = datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
) -> TaskRegistryRecord:
    rel_path = task_dir.relative_to(repo_root).as_posix()
    digests = compute_task_digests(task_dir)
    return TaskRegistryRecord(
        schema_version=1,
        task_id=task_id,
        version=version,
        task_path=rel_path,
        digests=digests,
        source_uri=f"local/{task_id}@{version}",
        source_ref="main",
        license="MIT",
        provenance_zone="02-local-evidence",
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=1800),
        control_evidence=TaskControlEvidence(
            oracle=ControlEvidenceRef(
                job_name=f"{task_id}-oracle",
                reward=oracle_reward,
            ),
            nop=ControlEvidenceRef(
                job_name=f"{task_id}-nop",
                reward=nop_reward,
            ),
        ),
        state=state,  # type: ignore[arg-type]
        allowed_uses=["measurement", "training"],
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
        task="registered/tampered-task",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskDigestMismatchError) as exc_info:
        reg.resolve_spec(spec, tmp_path)
    assert "bytes on disk have changed" in str(exc_info.value)


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
    # Attempt to redirect to other-task
    spec = ExperimentSpec(
        name="test-redirect",
        hypothesis="test",
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
        task="registered/versioned-task",
        task_version="2.0.0",
        agent="codex",
        submitted_by="test",
    )
    with pytest.raises(TaskVersionMismatchError):
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
        task="registered/non-existent",
        agent="codex",
        est_cost_usd=1.0,
        submitted_by="test",
    )
    decision = gate.decide(spec, spent_today_usd=0.0)
    assert not decision.admitted
    assert decision.reason_code == "unregistered_task"


def test_human_approval_does_not_override_unregistered_task(tmp_path: Path) -> None:
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20.0,
        per_job_cost_ceiling_usd=3.0,
        quiet_failure_rule=3,
        auto_run=[{"name": "test-controls", "agents": ["oracle"]}],
    )
    gate = PolicyGate(policy, repo_root=tmp_path)
    spec = ExperimentSpec(
        name="test-human-approved-unregistered",
        hypothesis="test",
        task="registered/unregistered-bypass",
        agent="codex",
        policy_rule="human-approval",
        est_cost_usd=1.0,
        submitted_by="test",
    )
    decision = gate.decide(spec, spent_today_usd=0.0, human_approved=True)
    assert not decision.admitted
    assert decision.reason_code == "unregistered_task"


def test_researcher_loop_defers_when_registry_empty(tmp_path: Path) -> None:
    # Set up empty library/tasks with task.toml to prove glob is not used
    _make_dummy_task(tmp_path, "library/tasks/some-task")

    class StubInvoker:
        def __call__(self, *args, **kwargs):
            raise AssertionError("invoker should not be called")

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
        invoker=StubInvoker(),
        policy=policy,
        evidence_loader=lambda day, path: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ResearcherDeferred) as exc_info:
        loop._registered_tasks()
    assert str(exc_info.value) == "no_registered_tasks"


def test_canaries_remain_independent_under_own_policy(tmp_path: Path) -> None:
    # Real repo canaries resolve without registry records
    real_repo = Path(__file__).resolve().parents[1]
    suite = load_canary_suite(real_repo / "policy/canary-suite.yaml")
    assert len(suite.members) == 3

    # All canary task paths exist and match digests
    for member in suite.members:
        task_path = real_repo / member.task_path
        assert task_path.is_dir()
        digests = compute_task_digests(task_path)
        assert digests.package == member.task_digest


def test_audit_detects_false_registration_pattern(tmp_path: Path) -> None:
    # Replicate the research-event-summary-b51328c0cd pattern in queue/proposed
    q_proposed = tmp_path / "queue/proposed"
    q_proposed.mkdir(parents=True, exist_ok=True)
    spec_payload = {
        "schema_version": 1,
        "spec_id": "01M023RP03KGSHB4WZ29WE9DGR",
        "name": "research-event-summary-b51328c0cd",
        "hypothesis": "Test hypothesis",
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

    # 3. Registered state requires oracle reward = 1.0
    bad_oracle_data = dict(unapproved_data)
    bad_oracle_data["approved_by"] = "Peter Makhnatch"
    bad_oracle_data["approved_at"] = datetime.now(UTC)
    bad_oracle_data["control_evidence"] = {
        "oracle": {"job_name": "oracle-job", "reward": 0.8},
        "nop": {"job_name": "nop-job", "reward": 0.0},
    }
    with pytest.raises(ValidationError) as exc_info:
        TaskRegistryRecord.model_validate(bad_oracle_data)
    assert "oracle reward 1.0" in str(exc_info.value)

    # 4. Registered state requires nop reward = 0.0
    bad_nop_data = dict(bad_oracle_data)
    bad_nop_data["control_evidence"] = {
        "oracle": {"job_name": "oracle-job", "reward": 1.0},
        "nop": {"job_name": "nop-job", "reward": 0.5},
    }
    with pytest.raises(ValidationError) as exc_info:
        TaskRegistryRecord.model_validate(bad_nop_data)
    assert "nop reward 0.0" in str(exc_info.value)


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
