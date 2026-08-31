"""Focused contract and unit tests for the Agent Readiness Control Plane.

Deterministic per agents/CHECKS.md: injected home/clock/security-runner/environment/cli_runner/docker_checker;
zero dependence on live credentials, keychain, network, or Docker daemon.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evallab import profiles as profiles_module
from evallab.canary import task_directory_digest
from evallab.cli import _agents_doctor_command, _agents_list_command
from evallab.profiles import (
    GATE_CANARY,
    GATE_HARBOR_TRANSPORT,
    GATE_HOST_CREDENTIAL,
    GATE_SMOKE,
    AgentProfile,
    ProfileState,
    builtin_profiles,
    compute_qualification_digest,
    evaluate_profile_readiness,
    load_readiness_record,
)
from evallab.queue import DirectoryQueue, Executor, load_policy
from evallab.runner import RunRequest
from evallab.schemas import AgentReadinessRecord


def evaluate_offline(profile: AgentProfile, *, root: Path) -> AgentReadinessRecord:
    return evaluate_profile_readiness(
        profile,
        root=root,
        is_installed_fn=lambda _binary: False,
    )


def make_mock_job_dir(
    tmp_path: Path,
    job_name: str,
    *,
    reward: float | None = 1.0,
    task_name: str = "event-summary",
    step_count: int = 5,
    tool_calls_per_step: int = 2,
) -> Path:
    """Create a realistic Harbor job directory fixture."""
    job_dir = tmp_path / "runs" / job_name
    trial_dir = job_dir / f"{task_name}__trial1"
    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    agent_dir.mkdir(parents=True, exist_ok=True)
    verifier_dir.mkdir(parents=True, exist_ok=True)

    result_data = {
        "id": "job-123",
        "name": job_name,
        "n_total_trials": 1,
        "stats": {},
        "started_at": "2026-08-31T12:00:00Z",
        "finished_at": "2026-08-31T12:00:15Z",
        "trials": [
            {
                "id": "trial-123",
                "trial_name": trial_dir.name,
                "task_name": task_name,
                "started_at": "2026-08-31T12:00:00Z",
                "finished_at": "2026-08-31T12:00:15Z",
            }
        ],
    }
    (job_dir / "result.json").write_text(json.dumps(result_data))
    (job_dir / "config.json").write_text(json.dumps({}))
    (job_dir / "lock.json").write_text(json.dumps({}))

    trial_result_data = {
        "id": "trial-123",
        "trial_name": trial_dir.name,
        "task_name": task_name,
        "started_at": "2026-08-31T12:00:00Z",
        "finished_at": "2026-08-31T12:00:15Z",
        "verifier_result": {"rewards": ({"reward": reward} if reward is not None else {})},
    }
    (trial_dir / "result.json").write_text(json.dumps(trial_result_data))
    (trial_dir / "config.json").write_text(json.dumps({}))
    (trial_dir / "lock.json").write_text(json.dumps({}))
    if reward is not None:
        (verifier_dir / "reward.txt").write_text(str(reward))
        (verifier_dir / "reward.json").write_text(json.dumps({"reward": reward}))

    steps = [
        {
            "step_id": index,
            "source": "agent",
            "message": f"step {index}",
            "tool_calls": [
                {
                    "tool_call_id": f"call-{index}-{call_index}",
                    "function_name": "bash",
                    "arguments": {},
                }
                for call_index in range(tool_calls_per_step)
            ],
        }
        for index in range(1, step_count + 1)
    ]
    trajectory_data = {
        "schema_version": "ATIF-v1.0",
        "agent": {"name": "readiness-fixture", "version": "1"},
        "steps": steps,
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(trajectory_data))

    return job_dir


def register_event_summary_canary(root: Path) -> str:
    """Pin a three-member suite while returning the event-summary digest."""
    task_root = root / "library/tasks"
    members: list[tuple[str, Path]] = [("event-summary", task_root / "event-summary")]
    for name in ("readiness-fixture-a", "readiness-fixture-b"):
        path = task_root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.toml").write_text(f'[task]\nname = "{name}"\n', encoding="utf-8")
        members.append((name, path))

    lines = ["version: 1", "attempts: 3", "agents: [codex]", "members:"]
    digests: dict[str, str] = {}
    for name, path in members:
        digest = task_directory_digest(path)
        digests[name] = digest
        relative = path.relative_to(root).as_posix()
        lines.extend(
            [
                f"  - name: {name}",
                f"    task_path: {relative}",
                "    task_version: 1.0.0",
                f"    task_digest: {digest}",
                f"    source_ref: local/{name}@1.0.0",
                "    est_cost_usd: 1.0",
            ]
        )

    policy_path = root / "policy/canary-suite.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return digests["event-summary"]


# ---------------------------------------------------------------------------
# Acceptance 1: Gemini transport-ready classification via injected seams
# ---------------------------------------------------------------------------


def test_agents_doctor_gemini37_ready(tmp_path: Path) -> None:
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    readiness = evaluate_profile_readiness(
        profile,
        root=tmp_path,
        home=tmp_path,
        is_installed_fn=lambda bin_name: bin_name == "agy",
        cli_runner=lambda argv: (0, "14 models available including gemini-3.7-flash-high"),
        docker_checker=lambda: (True, "Docker daemon reachable"),
    )

    assert readiness.gates.declared == "pass"
    assert readiness.gates.installed == "pass"
    assert readiness.gates.host_credential == "pass"
    assert readiness.gates.harbor_transport == "pass"
    assert readiness.gates.environment_network == "pass"
    assert readiness.gates.structured_trajectory == "pass"
    assert readiness.gates.smoke == "blocked"
    assert readiness.gates.canary == "blocked"

    assert readiness.state == ProfileState.CREDENTIAL_READY.value
    assert readiness.blocker is not None
    assert readiness.blocker.gate == GATE_SMOKE
    assert "No verified smoke run on record" in readiness.blocker.reason


# ---------------------------------------------------------------------------
# Acceptance 2: Cursor host-ready but Harbor-transport-blocked
# ---------------------------------------------------------------------------


def test_agents_doctor_cursor_blocked_transport(tmp_path: Path) -> None:
    profile = builtin_profiles()["cursor-grok-4.6-high"]
    readiness = evaluate_profile_readiness(
        profile,
        root=tmp_path,
        home=tmp_path,
        is_installed_fn=lambda bin_name: bin_name == "cursor-agent",
        cli_runner=lambda argv: (0, "Logged in as p.makhnatch@gmail.com"),
        docker_checker=lambda: (True, "Docker daemon reachable"),
    )

    assert readiness.gates.declared == "pass"
    assert readiness.gates.installed == "pass"
    assert readiness.gates.host_credential == "pass"
    assert readiness.gates.harbor_transport == "fail"
    assert readiness.gates.environment_network == "blocked"
    assert readiness.gates.structured_trajectory == "blocked"

    assert readiness.state == ProfileState.INSTALLED.value
    assert readiness.blocker is not None
    assert readiness.blocker.gate == GATE_HARBOR_TRANSPORT
    assert "Harbor runner requires CURSOR_API_KEY" in readiness.blocker.reason
    assert "cursor-agent subscription session is not transported" in readiness.blocker.reason


# ---------------------------------------------------------------------------
# Acceptance 3: Exact Claude and DeepSeek credential blockers
# ---------------------------------------------------------------------------


def test_agents_doctor_claude_missing_keychain(tmp_path: Path) -> None:
    profile = builtin_profiles()["claude-code-fable-5"]
    readiness = evaluate_profile_readiness(
        profile,
        root=tmp_path,
        home=tmp_path,
        is_installed_fn=lambda bin_name: bin_name == "claude",
        security_runner=lambda argv: 1,  # Item absent in keychain
        keychain_account="peter",
        docker_checker=lambda: (True, "Docker daemon reachable"),
    )

    assert readiness.gates.declared == "pass"
    assert readiness.gates.installed == "pass"
    assert readiness.gates.host_credential == "fail"
    assert readiness.gates.harbor_transport == "blocked"

    assert readiness.state == ProfileState.INSTALLED.value
    assert readiness.blocker is not None
    assert readiness.blocker.gate == GATE_HOST_CREDENTIAL
    assert readiness.blocker.reason == "keychain item absent for harbor-practice-claude-oauth"


def test_agents_doctor_deepseek_missing_env(tmp_path: Path) -> None:
    profile = builtin_profiles()["mini-swe-agent-deepseek-v4-flash"]
    readiness = evaluate_profile_readiness(
        profile,
        root=tmp_path,
        home=tmp_path,
        environment={},  # Empty environment: no DEEPSEEK_API_KEY / MSWEA_API_KEY
        is_installed_fn=lambda bin_name: True,
        docker_checker=lambda: (True, "Docker daemon reachable"),
    )

    assert readiness.gates.declared == "pass"
    assert readiness.gates.installed == "pass"
    assert readiness.gates.host_credential == "fail"
    assert readiness.gates.harbor_transport == "blocked"

    assert readiness.state == ProfileState.INSTALLED.value
    assert readiness.blocker is not None
    assert readiness.blocker.gate == GATE_HOST_CREDENTIAL
    assert (
        readiness.blocker.reason
        == "credential environment missing: DEEPSEEK_API_KEY or MSWEA_API_KEY"
    )


# ---------------------------------------------------------------------------
# Acceptance 4: Blocked pre-trial smoke (fail closed before trial creation)
# ---------------------------------------------------------------------------


def test_agents_smoke_fail_closed_before_harbor(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')

    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )

    queue = DirectoryQueue(tmp_path / "queue", create=True)
    policy = load_policy(policy_file)

    runner_mock = MagicMock()
    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy,
        runner=runner_mock,
    )

    # Test 1: Cursor profile (blocked at Harbor transport)
    cursor_profile = builtin_profiles()["cursor-grok-4.6-high"]
    ok, smoke_rec, err = executor.execute_agent_smoke(
        cursor_profile,
        task_ref="library/tasks/event-summary",
        is_installed_fn=lambda bin_name: True,
        cli_runner=lambda argv: (0, "Logged in"),
        docker_checker=lambda: (True, "Docker reachable"),
    )
    assert not ok
    assert smoke_rec is None
    assert err is not None
    assert "Harbor runner requires CURSOR_API_KEY" in err
    runner_mock.assert_not_called()

    # Test 2: Claude profile with missing keychain (blocked at host_credential)
    claude_profile = builtin_profiles()["claude-code-fable-5"]
    ok, smoke_rec, err = executor.execute_agent_smoke(
        claude_profile,
        task_ref="library/tasks/event-summary",
        is_installed_fn=lambda bin_name: True,
        security_runner=lambda argv: 1,  # Missing
        docker_checker=lambda: (True, "Docker reachable"),
    )
    assert not ok
    assert smoke_rec is None
    assert err == "keychain item absent for harbor-practice-claude-oauth"
    runner_mock.assert_not_called()

    # Verify no evidence files were created
    readiness_dir = tmp_path / "research/evidence/readiness"
    assert not readiness_dir.exists() or not list(readiness_dir.glob("*.json"))


def test_agents_smoke_refuses_arbitrary_direct_task(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')
    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )
    runner_mock = MagicMock()
    executor = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue", create=True),
        policy=load_policy(policy_file),
        runner=runner_mock,
    )
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]

    ok, smoke_record, error = executor.execute_agent_smoke(
        profile,
        task_ref="library/tasks/event-summary",
        is_installed_fn=lambda _binary: True,
        cli_runner=lambda _argv: (0, "gemini-3.7-flash-high active"),
        docker_checker=lambda: (True, "Docker reachable"),
    )

    assert not ok
    assert smoke_record is None
    assert error == "readiness smoke only accepts a registered canary/<name> task"
    runner_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Acceptance 5: Evidence persistence on smoke success
# ---------------------------------------------------------------------------


def test_agents_smoke_success_persists_evidence(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')
    register_event_summary_canary(tmp_path)

    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )

    queue = DirectoryQueue(tmp_path / "queue", create=True)
    policy = load_policy(policy_file)

    def mock_runner(request: RunRequest) -> Path:
        return make_mock_job_dir(
            tmp_path,
            request.name,
            reward=1.0,
            step_count=7,
            tool_calls_per_step=3,
        )

    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy,
        runner=mock_runner,
    )

    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    ok, smoke_rec, err = executor.execute_agent_smoke(
        profile,
        task_ref="canary/event-summary",
        is_installed_fn=lambda bin_name: True,
        cli_runner=lambda argv: (0, "gemini-3.7-flash-high active"),
        docker_checker=lambda: (True, "Docker reachable"),
    )

    assert ok
    assert err is None
    assert smoke_rec is not None
    assert smoke_rec.reward == 1.0
    assert smoke_rec.step_count == 7
    assert smoke_rec.tool_call_count == 21
    assert smoke_rec.profile_id == "antigravity-gemini-3.7-flash-high"
    assert smoke_rec.profile_digest == profile.digest
    assert smoke_rec.atif_path == "agent/trajectory.json"
    assert smoke_rec.atif_digest.startswith("sha256:")

    # Verify persisted record
    persisted = load_readiness_record(profile.profile_id, root=tmp_path)
    assert persisted is not None
    assert persisted.state == ProfileState.SMOKE_PASSED.value
    assert persisted.gates.smoke == "pass"
    assert persisted.last_smoke == smoke_rec
    assert persisted.blocker is not None
    assert persisted.blocker.gate == GATE_CANARY


def test_agents_smoke_missing_reward_is_not_synthetic_zero(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')
    register_event_summary_canary(tmp_path)
    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )

    executor = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue", create=True),
        policy=load_policy(policy_file),
        runner=lambda request: make_mock_job_dir(tmp_path, request.name, reward=None),
    )
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    ok, smoke_record, error = executor.execute_agent_smoke(
        profile,
        is_installed_fn=lambda _binary: True,
        cli_runner=lambda _argv: (0, "gemini-3.7-flash-high active"),
        docker_checker=lambda: (True, "Docker reachable"),
    )

    assert not ok
    assert smoke_record is None
    assert error == "Smoke verifier produced no valid reward"
    assert load_readiness_record(profile.profile_id, root=tmp_path) is None


# ---------------------------------------------------------------------------
# Acceptance 6: Repeat qualification digest
# ---------------------------------------------------------------------------


def test_agents_qualify_repeats(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')
    register_event_summary_canary(tmp_path)

    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )

    queue = DirectoryQueue(tmp_path / "queue", create=True)
    policy = load_policy(policy_file)

    call_count = 0

    def mock_runner(request: RunRequest) -> Path:
        nonlocal call_count
        call_count += 1
        return make_mock_job_dir(
            tmp_path,
            f"{request.name}-{call_count}",
            reward=1.0,
            step_count=4,
            tool_calls_per_step=1,
        )

    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy,
        runner=mock_runner,
    )

    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    ok, qual_digest, err = executor.execute_agent_qualify(
        profile,
        repeats=3,
        task_ref="canary/event-summary",
        is_installed_fn=lambda bin_name: True,
        cli_runner=lambda argv: (0, "gemini-3.7-flash-high active"),
        docker_checker=lambda: (True, "Docker reachable"),
    )

    assert ok
    assert err is None
    assert qual_digest is not None
    assert qual_digest.repeats == 3
    assert qual_digest.success_count == 3
    assert len(qual_digest.smoke_records) == 3
    assert qual_digest.qualification_digest.startswith("sha256:")
    assert call_count == 3

    bounded_ok, bounded_digest, bounded_error = executor.execute_agent_qualify(
        profile,
        repeats=2,
    )
    assert not bounded_ok
    assert bounded_digest is None
    assert bounded_error == "Qualification requires exactly 3 canary repeats"
    assert call_count == 3

    # Verify deterministic qualification digest
    expected_digest = compute_qualification_digest(qual_digest.smoke_records)
    assert qual_digest.qualification_digest == expected_digest

    # Verify persisted readiness record
    persisted = load_readiness_record(profile.profile_id, root=tmp_path)
    assert persisted is not None
    assert persisted.state == ProfileState.CANARY_QUALIFIED.value
    assert persisted.gates.canary == "pass"
    assert persisted.qualification == qual_digest
    assert persisted.blocker is None


def test_agents_qualify_failure_on_repeat(tmp_path: Path) -> None:
    task_dir = tmp_path / "library/tasks/event-summary"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "event-summary"\n')
    register_event_summary_canary(tmp_path)

    policy_file = tmp_path / "policy/standing-approvals.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "version: 1\ndaily_cost_ceiling_usd: 100\nper_job_cost_ceiling_usd: 10\n"
        "quiet_failure_rule: 3\nauto_run:\n  - name: controls\n    agents: [oracle, nop]\n"
        "escalate_to_human:\n  - any_billable_agent\n"
    )

    queue = DirectoryQueue(tmp_path / "queue", create=True)
    policy = load_policy(policy_file)

    call_count = 0

    def mock_runner(request: RunRequest) -> Path:
        nonlocal call_count
        call_count += 1
        # Second call fails
        reward = 1.0 if call_count != 2 else 0.0
        return make_mock_job_dir(
            tmp_path,
            f"{request.name}-{call_count}",
            reward=reward,
        )

    executor = Executor(
        repo_root=tmp_path,
        queue=queue,
        policy=policy,
        runner=mock_runner,
    )

    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    ok, qual_digest, err = executor.execute_agent_qualify(
        profile,
        repeats=3,
        task_ref="canary/event-summary",
        is_installed_fn=lambda bin_name: True,
        cli_runner=lambda argv: (0, "gemini-3.7-flash-high active"),
        docker_checker=lambda: (True, "Docker reachable"),
    )

    assert not ok
    assert qual_digest is None
    assert err is not None
    assert "Repeat 2/3 failed" in err
    assert call_count == 2


# ---------------------------------------------------------------------------
# Acceptance 7: CLI Commands Surface
# ---------------------------------------------------------------------------


def test_cli_agents_list(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profiles_module, "evaluate_profile_readiness", evaluate_offline)
    ret = _agents_list_command(argparse.Namespace(json=False), tmp_path)
    assert ret == 0
    captured = capsys.readouterr()
    assert "Profile ID" in captured.out
    assert "antigravity-gemini-3.7-flash-high" in captured.out
    assert "cursor-grok-4.6-high" in captured.out

    ret_json = _agents_list_command(argparse.Namespace(json=True), tmp_path)
    assert ret_json == 0
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert isinstance(data, list)
    profile_ids = {item["profile_id"] for item in data}
    assert "antigravity-gemini-3.7-flash-high" in profile_ids


def test_cli_agents_doctor(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profiles_module, "evaluate_profile_readiness", evaluate_offline)
    args = argparse.Namespace(
        profile="antigravity-gemini-3.7-flash-high",
        json=False,
    )
    ret = _agents_doctor_command(args, tmp_path)
    assert ret == 1
    captured = capsys.readouterr()
    assert "Profile: antigravity-gemini-3.7-flash-high" in captured.out
    assert "declared" in captured.out
    assert "harbor_transport" in captured.out

    args_json = argparse.Namespace(
        profile="antigravity-gemini-3.7-flash-high",
        json=True,
    )
    ret_json = _agents_doctor_command(args_json, tmp_path)
    assert ret_json == 1
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert isinstance(data, list)
    assert data[0]["profile_id"] == "antigravity-gemini-3.7-flash-high"
