"""Focused behavioural tests for the rlm harness lane (LabRlmAgent).

Covers the lab-owned rlm lane end to end at the contract boundary:
build_command flags, validate_request rules, host-secret-file transport in
run_harbor_process, ExperimentSpec round-trip, and profile/preflight wiring.
No model, Docker, or Harbor execution is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evallab import profiles as profiles_module
from evallab.credentials import (
    AGENT_CREDENTIAL_REQUIREMENTS,
    DEFAULT_PROFILE_FOR_ADAPTER,
    ZAI_OPENCODE_AUTH,
)
from evallab.execution_contracts import (
    HARBOR_AGENT_IMPORT_PATHS,
    RLM_AGENT,
    ZAI_OPENCODE_MODEL_SELECTORS,
    ZAI_SECRET_FILE_ENV,
    RunRequest,
    build_command,
    resolve_harbor_agent,
    validate_request,
)
from evallab.profiles import (
    OpenCodeProviderAuthProbe,
    ProbeResult,
    builtin_profiles,
    validate_model_pin,
)
from evallab.runner import preflight_request, profile_for_request, run_harbor_process
from evallab.schemas import ExperimentSpec

FLASH = "zai-coding-plan/glm-5.3-flash"
RLM_IMPORT_PATH = "evallab.harbor_rlm:LabRlmAgent"


def _task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text('schema_version = "1.4"\n[agent]\ntimeout_sec = 60.0\n')
    return task_dir


def _rlm_request(tmp_path: Path, **overrides) -> RunRequest:
    fields: dict = {
        "task": _task(tmp_path),
        "agent": "rlm",
        "model": FLASH,
        "name": "rlm-lane-test",
        "jobs_dir": tmp_path / "runs",
        "allow_billable": True,
        "attempts": 1,
        "concurrency": 1,
        "timeout_seconds": 300,
    }
    fields.update(overrides)
    return RunRequest(**fields)


def test_rlm_resolves_to_the_lab_owned_import_path() -> None:
    assert RLM_AGENT == "rlm"
    assert HARBOR_AGENT_IMPORT_PATHS["rlm"] == RLM_IMPORT_PATH
    assert resolve_harbor_agent("rlm") == RLM_IMPORT_PATH


def test_rlm_build_command_carries_exact_harness_flags(tmp_path: Path) -> None:
    request = _rlm_request(tmp_path, harness_policy="stock", cost_limit_usd=2.0)
    command = build_command(request)
    assert command[command.index("--agent") + 1] == RLM_IMPORT_PATH
    assert command[command.index("--model") + 1] == FLASH
    assert command[command.index("--n-concurrent-agents") + 1] == "1"
    assert command[command.index("--n-tasks") + 1] == "1"
    assert command[command.index("--max-retries") + 1] == "0"
    kwargs = [
        command[index + 1] for index, arg in enumerate(command[:-1]) if arg == "--agent-kwarg"
    ]
    assert "policy=stock" in kwargs
    assert "cost_limit_usd=2.0" in kwargs


def test_rlm_build_command_defaults_policy_and_cost(tmp_path: Path) -> None:
    command = build_command(_rlm_request(tmp_path))
    kwargs = [
        command[index + 1] for index, arg in enumerate(command[:-1]) if arg == "--agent-kwarg"
    ]
    assert "policy=stock" in kwargs
    assert "cost_limit_usd=1.0" in kwargs


def test_rlm_build_command_rejects_non_selector_model(tmp_path: Path) -> None:
    import pytest

    request = _rlm_request(tmp_path, model="unsupported/model")
    with pytest.raises(ValueError, match="rlm requires one of the exact models"):
        build_command(request)


def test_rlm_validate_accepts_the_valid_request(tmp_path: Path) -> None:
    validate_request(_rlm_request(tmp_path, harness_policy="stock", cost_limit_usd=2.0))


def test_rlm_validate_rejects_second_attempt(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="bind exactly one trial"):
        validate_request(_rlm_request(tmp_path, attempts=2))


def test_rlm_validate_rejects_second_concurrent_slot(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="bind exactly one trial"):
        validate_request(_rlm_request(tmp_path, concurrency=2))


def test_rlm_validate_rejects_non_selector_model(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="rlm requires one of the exact models"):
        validate_request(_rlm_request(tmp_path, model="unsupported/model"))


def test_rlm_validate_requires_billable_acknowledgement(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="allow-billable"):
        validate_request(_rlm_request(tmp_path, allow_billable=False, model=FLASH))


def test_harness_policy_is_rejected_on_non_rlm_agents(tmp_path: Path) -> None:
    import pytest

    oracle = RunRequest(
        task=_task(tmp_path),
        agent="oracle",
        name="oracle-policy-test",
        jobs_dir=tmp_path / "runs",
        harness_policy="stock",
    )
    with pytest.raises(ValueError, match="harness_policy is supported only by the rlm lane"):
        validate_request(oracle)


def test_rlm_needs_no_proxy_ceilings(tmp_path: Path) -> None:
    # rlm is not a metered agent: a bare cost limit rides along as an
    # agent-kwarg without demanding the full proxy ceiling set.
    validate_request(_rlm_request(tmp_path, cost_limit_usd=1.0))


def test_rlm_model_selectors_match_zai_opencode() -> None:
    assert ZAI_OPENCODE_MODEL_SELECTORS == frozenset(
        {"zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"}
    )


def test_rlm_secret_file_transport_redacts_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    secret = "rlm-lane-secret-never-in-log"
    fake_home = tmp_path / "home"
    auth_path = fake_home / ".local/share/opencode/auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"zai-coding-plan": {"key": secret}}))
    auth_path.chmod(0o600)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # Ambient proxy-shaped values must not leak into the host-secret-file lane.
    monkeypatch.setenv("ZAI_BASE_URL", "http://ambient-proxy:9999")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://ambient-proxy:9999")
    monkeypatch.setenv("EVALLAB_ZAI_PROXY_CAPABILITY", "ambient-capability")

    script = (
        "import os; "
        "from pathlib import Path; "
        f"print('secret_file=' + os.environ.get('{ZAI_SECRET_FILE_ENV}', 'unset')); "
        "print('zai_base=' + os.environ.get('ZAI_BASE_URL', 'unset')); "
        "print('openai_base=' + os.environ.get('OPENAI_BASE_URL', 'unset')); "
        "print('capability=' + os.environ.get('EVALLAB_ZAI_PROXY_CAPABILITY', 'unset')); "
        f"secret_path = os.environ.get('{ZAI_SECRET_FILE_ENV}', ''); "
        "print('secret=' + Path(secret_path).read_text().strip()); "
        "print('secret_dir=' + str(Path(secret_path).parent))"
    )
    log_path = tmp_path / "rlm.log"
    result = run_harbor_process(
        [sys.executable, "-c", script, resolve_harbor_agent("rlm")],
        cwd=tmp_path,
        timeout_seconds=30,
        log_path=log_path,
    )

    assert result.returncode == 0
    assert result.proxy_usage is None
    log_text = log_path.read_text(encoding="utf-8")
    assert secret not in log_text
    assert "secret=<redacted>" in log_text
    observed = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in log_text.splitlines()
        if "=" in line
    }
    assert observed["zai_base"] == "unset"
    assert observed["openai_base"] == "unset"
    assert observed["capability"] == "unset"
    assert observed["secret_file"] != "unset"
    assert not Path(observed["secret_file"]).exists()
    assert not Path(observed["secret_dir"]).exists()


def test_experiment_spec_round_trips_harness_policy() -> None:
    spec = ExperimentSpec(
        name="rlm-harness-policy-test",
        hypothesis="harness variants hill-climb on the synthetic bench",
        purpose="practice",
        task="library/tasks/event-summary",
        agent="rlm",
        model=FLASH,
        submitted_by="test-agent",
        harness_policy="stock",
    )
    assert spec.harness_policy == "stock"
    assert ExperimentSpec.model_validate(spec.model_dump()) == spec
    assert (
        ExperimentSpec(
            name="rlm-harness-default-test",
            hypothesis="absence is the default",
            purpose="practice",
            task="library/tasks/event-summary",
            agent="rlm",
            submitted_by="test-agent",
        ).harness_policy
        is None
    )


def test_experiment_spec_golden_freeze_covers_harness_policy() -> None:
    golden = json.loads(
        (Path(__file__).parent / "fixtures/contracts/ExperimentSpec.json").read_text()
    )
    assert "harness_policy" in golden["properties"]


def test_rlm_profile_and_preflight_wiring(tmp_path: Path) -> None:
    request = _rlm_request(tmp_path)
    profile = profile_for_request(request)
    assert profile.profile_id == "rlm-glm-5.3-flash"
    assert profile == builtin_profiles()["rlm-glm-5.3-flash"]
    validate_model_pin(profile, FLASH)
    assert profile.limits.max_attempts == 1
    assert profile.limits.max_concurrency == 1
    assert "credential-transport:host-secret-file" in profile.capabilities
    assert "structured-trajectory:rlm-trajectory-json" in profile.capabilities

    probe = profiles_module.default_probe_for(
        profile,
        home=tmp_path,
        security_runner=lambda argv: 1,
        keychain_account="",
    )
    assert isinstance(probe, OpenCodeProviderAuthProbe)

    decision = preflight_request(request, probe=lambda _profile: ProbeResult(ok=True))
    assert decision.proceed
    assert decision.profile_digest == profile.digest


def test_rlm_credential_and_default_profile_maps() -> None:
    assert AGENT_CREDENTIAL_REQUIREMENTS["rlm"] == ZAI_OPENCODE_AUTH
    assert DEFAULT_PROFILE_FOR_ADAPTER["rlm"] == "rlm-glm-5.3-flash"
