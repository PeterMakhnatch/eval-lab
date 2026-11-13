"""Mandatory Daytona egress lock for MiMo runs (HAR-140).

Behavior: the default on (MiMo model or MiMo task on Daytona, controls
included), each dispatch refusal, the treatment-key split, the lock-failure
infra classification, and the nop lock path (a model-free agent issues no
exec, so the verifier's first command takes the lock).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab import egress_verification  # noqa: E402
from evallab.execution_contracts import (  # noqa: E402
    LOCKED_MODAL_ENVIRONMENT_IMPORT_PATH,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    TERMINUS_LOCAL_MODEL_SELECTOR,
    RunRequest,
    build_command,
    is_mimo_dataset_task,
    is_mimo_family_model,
    is_mimo_run,
    resolve_egress_lock,
    validate_request,
)

MIMO_TASK_TOML = """\
[task]
name = "mimo-v2.6-rl/format-code-task-000001"

[metadata]
source_dataset = "XiaomiMiMo/MiMo-V2.6-RL-oss"

[agent]
timeout_sec = 900.0
"""

PLAIN_TASK_TOML = """\
[task]
name = "terminal-bench/html-js-filter"

[agent]
timeout_sec = 900.0
"""

CEILINGS: dict[str, Any] = {
    "max_requests": 2000,
    "max_input_tokens": 64_000_000,
    "max_output_tokens": 1_000_000,
    "max_total_tokens": 65_000_000,
    "cost_limit_usd": 1.0,
}


def _task(tmp_path: Path, *, mimo: bool, compose: bool = False, phase: bool = False) -> Path:
    task = tmp_path / ("mimo-task" if mimo else "plain-task")
    task.mkdir(parents=True, exist_ok=True)
    text = MIMO_TASK_TOML if mimo else PLAIN_TASK_TOML
    if phase:
        text += '\n[verifier]\nnetwork_mode = "no-network"\n'
    (task / "task.toml").write_text(text, encoding="utf-8")
    if compose:
        env = task / "environment"
        env.mkdir(parents=True, exist_ok=True)
        (env / "docker-compose.yaml").write_text("services:\n  main: {}\n", encoding="utf-8")
    return task


def _request(task: Path, tmp_path: Path, **overrides: Any) -> RunRequest:
    base: dict[str, Any] = {
        "task": task,
        "agent": "terminus-2",
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "name": "har140-test-run",
        "jobs_dir": tmp_path / "runs",
        "environment": "daytona",
        "allow_billable": True,
    }
    base.update(overrides)
    agent = base.get("agent")
    model = base.get("model")
    metered = agent in {"mini-swe-agent", "mimoagent", "zai-opencode"} or (
        agent == "terminus-2" and model is not None and model != TERMINUS_LOCAL_MODEL_SELECTOR
    )
    if metered:
        for key, value in CEILINGS.items():
            base.setdefault(key, value)
    else:
        for key in CEILINGS:
            base.pop(key, None)
    if agent in {"nop", "oracle"}:
        base.pop("allow_billable", None)
    return RunRequest(**base)  # type: ignore[arg-type]


# --- default on ---------------------------------------------------------------


def test_mimo_model_daytona_defaults_locked(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=False), tmp_path)
    assert is_mimo_family_model(request.model)
    assert is_mimo_run(request.task, request.model)
    assert resolve_egress_lock(request) is True
    validate_request(request)
    assert "--environment-kwarg" in build_command(request)
    assert "egress_lock=true" in build_command(request)


def test_non_mimo_and_non_daytona_default_unlocked(tmp_path: Path) -> None:
    task = _task(tmp_path, mimo=False)
    other = _request(task, tmp_path, agent="terminus-2", model="zai/glm-5.3-flash")
    assert not is_mimo_family_model(other.model)
    assert resolve_egress_lock(other) is False
    assert "egress_lock=true" not in build_command(other)
    docker = _request(task, tmp_path, environment="docker")
    assert resolve_egress_lock(docker) is False


def test_openrouter_mimo_locks_on_mimo_task_and_stays_unlocked_on_non_mimo_task(
    tmp_path: Path,
) -> None:
    model = "openrouter-metered/xiaomi/mimo-v2.6-flash"
    non_mimo = _request(_task(tmp_path, mimo=False), tmp_path, model=model)
    assert is_mimo_family_model(non_mimo.model)
    assert not is_mimo_run(non_mimo.task, non_mimo.model)
    assert resolve_egress_lock(non_mimo) is False
    validate_request(non_mimo)

    mimo_task = _task(tmp_path, mimo=True)
    on_mimo = _request(mimo_task, tmp_path, model=model)
    assert is_mimo_run(on_mimo.task, on_mimo.model)
    assert resolve_egress_lock(on_mimo) is True
    validate_request(on_mimo)
    with pytest.raises(ValueError, match="explicit egress_lock=false"):
        validate_request(_request(mimo_task, tmp_path, model=model, egress_lock=False))


def test_nop_on_mimo_task_defaults_locked_without_a_model(tmp_path: Path) -> None:
    task = _task(tmp_path, mimo=True)
    assert is_mimo_dataset_task(task)
    nop = _request(task, tmp_path, agent="nop", model=None)
    assert is_mimo_run(nop.task, nop.model)
    assert resolve_egress_lock(nop) is True
    validate_request(nop)
    assert "egress_lock=true" in build_command(nop)


def test_nop_on_plain_task_stays_unlocked(tmp_path: Path) -> None:
    nop = _request(_task(tmp_path, mimo=False), tmp_path, agent="nop", model=None)
    assert resolve_egress_lock(nop) is False
    assert "egress_lock=true" not in build_command(nop)


def test_explicit_true_wins_on_plain_task(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=False), tmp_path, agent="nop", model=None, egress_lock=True
    )
    assert resolve_egress_lock(request) is True
    assert "egress_lock=true" in build_command(request)


# --- refusals ------------------------------------------------------------------


def test_explicit_false_on_mimo_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True), tmp_path, agent="nop", model=None, egress_lock=False
    )
    with pytest.raises(ValueError, match="explicit egress_lock=false"):
        validate_request(request)


def test_compose_task_is_refused(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=True, compose=True), tmp_path, agent="nop", model=None)
    with pytest.raises(ValueError, match="single-container"):
        validate_request(request)


def test_phase_network_policy_is_refused(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=True, phase=True), tmp_path, agent="nop", model=None)
    with pytest.raises(ValueError, match="phase network policies"):
        validate_request(request)


@pytest.mark.parametrize(
    ("agent", "model"),
    [
        ("codex", None),
        ("claude-code", None),
        ("terminus-2", TERMINUS_LOCAL_MODEL_SELECTOR),
    ],
)
def test_no_network_task_does_not_admit_in_container_model_agents(
    tmp_path: Path, agent: str, model: str | None
) -> None:
    task = tmp_path / "in-container-model"
    task.mkdir()
    (task / "task.toml").write_text('[agent]\nnetwork_mode = "no-network"\n', encoding="utf-8")
    request = _request(
        task, tmp_path, environment="docker", agent=agent, model=model, egress_lock=True
    )

    with pytest.raises(ValueError, match="sandbox"):
        validate_request(request)


@pytest.mark.parametrize(
    ("agent", "phase"),
    [
        ("terminus-2", '[agent]\nnetwork_mode = "no-network"\n'),
        (
            "mimoagent",
            '[agent]\nnetwork_mode = "no-network"\n[verifier]\nnetwork_mode = "no-network"\n',
        ),
        ("oracle", '[agent]\nnetwork_mode = "no-network"\n'),
        ("nop", '[verifier]\nnetwork_mode = "no-network"\n'),
    ],
)
def test_locked_docker_admits_no_network_phases_for_host_loops(
    tmp_path: Path, agent: str, phase: str
) -> None:
    task = tmp_path / "airgapped-task"
    task.mkdir()
    (task / "task.toml").write_text(phase, encoding="utf-8")
    request = _request(
        task,
        tmp_path,
        environment="docker",
        agent=agent,
        model=MIMO_SELFHOSTED_MODEL_SELECTOR if agent not in {"oracle", "nop"} else None,
        egress_lock=True,
    )

    validate_request(request)
    assert resolve_egress_lock(request) is True


@pytest.mark.parametrize("phase", ["agent", "verifier"])
@pytest.mark.parametrize("mode", ['"public"', '"allowlist"', "false"])
def test_locked_docker_refuses_phase_network_grants(tmp_path: Path, phase: str, mode: str) -> None:
    task = tmp_path / "network-grant"
    task.mkdir()
    (task / "task.toml").write_text(f"[{phase}]\nnetwork_mode = {mode}\n", encoding="utf-8")
    request = _request(
        task, tmp_path, environment="docker", agent="nop", model=None, egress_lock=True
    )

    with pytest.raises(ValueError, match="phase network policies"):
        validate_request(request)


def test_installed_local_model_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True),
        tmp_path,
        agent="terminus-2",
        model=TERMINUS_LOCAL_MODEL_SELECTOR,
    )
    with pytest.raises(ValueError, match="inside the sandbox"):
        validate_request(request)


def test_in_sandbox_agent_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True), tmp_path, agent="codex", model=None, allow_billable=True
    )
    with pytest.raises(ValueError):
        validate_request(request)


# --- treatment key -------------------------------------------------------------







def test_lock_enforced_never_reads_an_acknowledgement_as_a_block() -> None:
    enforced = egress_verification.lock_enforced
    daytona = "daytona update_network_settings(network_block_all=True)"
    # Daytona record from before HAR-205: acknowledged, never verified.
    assert enforced({"applied": True, "network_block_all": True, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": None, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": False, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": True, "mechanism": daytona}) is True
    assert enforced({"applied": True, "verified": True, "error": "late failure"}) is False
    assert enforced({"applied": False, "verified": True}) is False
    # Backends that record their own enforcement observation keep their meaning.
    assert enforced({"applied": True, "mechanism": "docker-compose main network_mode=none"}) is True
    assert enforced({"applied": True}) is True
    assert enforced(None) is False and enforced([]) is False


def test_acknowledged_only_trial_reads_open_in_fingerprint_and_posture(tmp_path: Path) -> None:
    from evallab.interpretation.trial_posture import _observed_egress_lock
    from evallab.setup_fingerprint import trial_fingerprint

    daytona = "daytona update_network_settings(network_block_all=True)"
    for name, record, enforced in (
        ("acked", {"applied": True, "mechanism": daytona, "network_block_all": True}, False),
        ("leaky", {"applied": True, "verified": False, "mechanism": daytona}, False),
        ("proved", {"applied": True, "verified": True, "mechanism": daytona}, True),
    ):
        trial = tmp_path / name
        trial.mkdir()
        (trial / "egress-lock.json").write_text(json.dumps(record), encoding="utf-8")
        lock = trial_fingerprint({"lock": {"mode": "locked"}}, trial)["lock"]
        assert lock["mode"] == ("locked" if enforced else "open")
        assert lock["observed_applied"] is True
        assert lock["observed_enforced"] is enforced
        assert _observed_egress_lock(trial) is enforced


# --- Modal lock ---------------------------------------------------------------


def _modal_task(tmp_path: Path, *, agent_network: str) -> Path:
    task = tmp_path / "modal-mimo-task"
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(
        '[task]\nname = "mimo-v2.6-rl/format-code-task-000001"\n\n'
        "[metadata]\n"
        'source_dataset = "XiaomiMiMo/MiMo-V2.6-RL-oss"\n\n'
        f'[agent]\ntimeout_sec = 900.0\nnetwork_mode = "{agent_network}"\n',
        encoding="utf-8",
    )
    return task


def test_modal_mimoagent_lock_admitted_with_exact_upstream_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://trainer.example/modal")
    request = _request(
        _modal_task(tmp_path, agent_network="no-network"),
        tmp_path,
        environment="modal",
        agent="mimoagent",
        egress_lock=True,
    )
    assert resolve_egress_lock(request) is True
    command = build_command(request)
    assert command[command.index("--allow-agent-host") + 1] == "trainer.example"
    assert "egress_lock=true" not in command
    # The lock record comes from LockedModalEnvironment, which records the
    # policy Modal actually applies into egress-lock.json.
    assert command[command.index("--env") + 1] == LOCKED_MODAL_ENVIRONMENT_IMPORT_PATH
    assert "evallab_egress_lock=true" in command


def test_modal_lock_refused_when_agent_baseline_is_public(tmp_path: Path) -> None:
    request = _request(
        _modal_task(tmp_path, agent_network="public"),
        tmp_path,
        environment="modal",
        agent="mimoagent",
        egress_lock=True,
    )
    with pytest.raises(ValueError, match="no-network"):
        build_command(request)


def test_modal_lock_refused_for_terminus(tmp_path: Path) -> None:
    request = _request(
        _modal_task(tmp_path, agent_network="no-network"),
        tmp_path,
        environment="modal",
        agent="terminus-2",
        egress_lock=True,
    )
    with pytest.raises(ValueError, match="cannot be combined with agent"):
        build_command(request)
