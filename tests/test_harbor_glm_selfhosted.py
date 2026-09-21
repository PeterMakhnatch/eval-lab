"""Focused tests for the mini-SWE-agent GLM self-hosted and fine-tuned endpoint seam (HAR-63).

Covers:
1. Adapter unit tests (SecretSafeGlmSelfhostedMiniSweAgent / SecretSafeOpenAICompatMiniSweAgent)
2. Identity discipline: requested_selector, effective_endpoint_base, provider_returned_model_id
3. Explicit inference_settings (effort, max_tokens) recorded at request time
4. Fail-closed endpoint validation (unset -> precise error naming GLM_SELFHOSTED_BASE_URL; invalid http(s))
5. Secret-safety (GLM_SELFHOSTED_API_KEY never in exec env, command, or connection env)
6. Trajectory sanitization passthrough
7. Profiles and credentials integration (built-in profiles, probes, requirements, missing credentials)
8. Execution contracts (resolve_harbor_agent, build_command, subscription_command, subscription_environment)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab.credentials import (
    DEFAULT_PROFILE_FOR_ADAPTER,
    GLM_SELFHOSTED_API_CREDENTIAL,
    available_credentials,
    missing_credential_for,
    probe_glm_selfhosted_api,
)
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
    GLM_SELFHOSTED_BASE_URL_ENV,
    GLM_SELFHOSTED_FT_MODEL_SELECTOR,
    GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH,
    GLM_SELFHOSTED_PROXY_TOKEN,
    PRIVATE_PERSIST_MODE,
    REDACTED_SECRET_VALUE,
    ZAI_MINISWE_AGENT_IMPORT_PATH,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ProfileInferenceSettings,
    RunRequest,
    build_command,
    redact_environment,
    resolve_harbor_agent,
    subscription_command,
    subscription_environment,
)
from evallab.profiles import (
    AgentProfile,
    ProfileLimits,
    builtin_profiles,
)

SECRET_SENTINEL = "test-glm-selfhosted-secret-key-67890"
TEST_BASE_URL = "http://127.0.0.1:8000/v1"


@dataclass(frozen=True)
class _Connection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)
    model: str | None = None
    model_name: str | None = None


class _MiniSweAgent:
    def __init__(self, connection: _Connection, **kwargs: Any) -> None:
        self.connection = connection
        self.exec_calls: list[tuple[str, dict[str, str] | None]] = []
        self.logs_dir = Path(".")
        self.model_name = kwargs.get("model_name") or getattr(connection, "model", None)

    @property
    def model_connection(self) -> _Connection:
        return self.connection

    async def exec_as_agent(
        self,
        environment: Any,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> str:
        del environment, cwd, timeout_sec
        self.exec_calls.append((command, env))
        return "ok"

    def populate_context_post_run(self, context: Any) -> None:
        del context
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps({"authorization": SECRET_SENTINEL, "ok": True}) + "\n"
        )


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


@pytest.fixture
def wrapper_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for name in ("harbor", "harbor.agents", "harbor.agents.installed", "harbor.environments"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.mini_swe_agent",
        _module("harbor.agents.installed.mini_swe_agent", MiniSweAgent=_MiniSweAgent),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module("harbor.agents.model_connection", ResolvedModelConnection=_Connection),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_glm_selfhosted", None)
    try:
        yield importlib.import_module("evallab.harbor_glm_selfhosted")
    finally:
        sys.modules.pop("evallab.harbor_glm_selfhosted", None)


# ---------------------------------------------------------------------------
# 1. Adapter unit tests
# ---------------------------------------------------------------------------


def test_adapter_accepts_glm_selfhosted_and_ft(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)

    # Base selector
    conn_base = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent_base = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(
        conn_base, model_name="glm-selfhosted/glm-5.3-flash"
    )
    resolved_base = agent_base.model_connection
    assert resolved_base.base_url == TEST_BASE_URL
    assert resolved_base.api_key == GLM_SELFHOSTED_PROXY_TOKEN

    # Fine-tuned selector
    conn_ft = _Connection(
        provider="glm-ft",
        model="glm-ft/checkpoint-500",
    )
    agent_ft = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(
        conn_ft, model_name="glm-ft/checkpoint-500"
    )
    resolved_ft = agent_ft.model_connection
    assert resolved_ft.base_url == TEST_BASE_URL
    assert resolved_ft.api_key == GLM_SELFHOSTED_PROXY_TOKEN


def test_adapter_alias_openai_compat(wrapper_module: ModuleType) -> None:
    assert (
        wrapper_module.SecretSafeOpenAICompatMiniSweAgent
        is wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent
    )


def test_adapter_rejects_unallowed_providers(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)

    with pytest.raises(ValueError, match="requires provider in"):
        conn = _Connection(provider="openai", model="openai/gpt-4o")
        agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn, model_name="openai/gpt-4o")
        _ = agent.model_connection

    with pytest.raises(ValueError, match="requires provider in"):
        conn = _Connection(provider="zai", model="zai/glm-5.3-flash")
        agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(
            conn, model_name="zai/glm-5.3-flash"
        )
        _ = agent.model_connection

    with pytest.raises(ValueError, match="model without provider"):
        conn = _Connection(provider="glm-selfhosted", model="bare-model-id")
        wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn, model_name="bare-model-id")


def test_adapter_fail_closed_when_endpoint_unset(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GLM_SELFHOSTED_BASE_URL_ENV, raising=False)
    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)
    with pytest.raises(ValueError, match=f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set"):
        _ = agent.model_connection


def test_adapter_fail_closed_when_endpoint_invalid_url(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, "ftp://invalid-scheme/v1")
    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)
    with pytest.raises(ValueError, match=f"{GLM_SELFHOSTED_BASE_URL_ENV} must be a valid http or https URL"):
        _ = agent.model_connection


def test_adapter_scrubs_secret_from_connection_env(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)
    monkeypatch.setenv("GLM_SELFHOSTED_API_KEY", SECRET_SENTINEL)

    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
        env={
            "GLM_SELFHOSTED_API_KEY": SECRET_SENTINEL,
            "OPENAI_BASE_URL": "http://old-url",
            "CUSTOM_VAR": "keep-me",
        },
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)
    rewired = agent.model_connection
    assert "GLM_SELFHOSTED_API_KEY" not in rewired.env
    assert rewired.env["OPENAI_BASE_URL"] == TEST_BASE_URL
    assert rewired.env["OPENAI_API_BASE"] == TEST_BASE_URL
    assert rewired.env["MSWEA_API_KEY"] == GLM_SELFHOSTED_PROXY_TOKEN
    assert rewired.env["CUSTOM_VAR"] == "keep-me"


def test_adapter_blocks_secret_in_exec_env(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)
    monkeypatch.setenv("GLM_SELFHOSTED_API_KEY", SECRET_SENTINEL)

    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)

    # Rejects explicit credential name in exec env
    with pytest.raises(
        ValueError,
        match="GLM self-hosted provider credential cannot enter the task exec environment",
    ):
        asyncio.run(
            agent.exec_as_agent(
                object(), "ls", env={"GLM_SELFHOSTED_API_KEY": "leaked-val"}
            )
        )

    # Rejects host secret value under any key name
    with pytest.raises(
        ValueError,
        match="GLM self-hosted provider credential cannot enter the task exec environment",
    ):
        asyncio.run(
            agent.exec_as_agent(
                object(), "ls", env={"SOME_OTHER_VAR": SECRET_SENTINEL}
            )
        )


def test_adapter_blocks_secret_in_command(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)
    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)

    with pytest.raises(
        ValueError,
        match="GLM self-hosted provider credential cannot enter the task exec command",
    ):
        asyncio.run(agent.exec_as_agent(object(), "cat /run/secrets/api_key"))

    with pytest.raises(
        ValueError,
        match="GLM self-hosted provider credential cannot enter the task exec command",
    ):
        asyncio.run(
            agent.exec_as_agent(
                object(), 'export GLM_SELFHOSTED_API_KEY="$(cat /tmp/key)"'
            )
        )


def test_adapter_sanitizes_trajectories(
    wrapper_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLM_SELFHOSTED_API_KEY", SECRET_SENTINEL)
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)

    conn = _Connection(
        provider="glm-selfhosted",
        model="glm-selfhosted/glm-5.3-flash",
    )
    agent = wrapper_module.SecretSafeGlmSelfhostedMiniSweAgent(conn)
    agent.logs_dir = tmp_path

    agent.populate_context_post_run(object())

    native = tmp_path / "trajectory.json"
    assert native.is_file()
    text = native.read_text()
    assert SECRET_SENTINEL not in text
    assert REDACTED_SECRET_VALUE in text
    assert native.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


# ---------------------------------------------------------------------------
# 2. Identity discipline & inference settings tests
# ---------------------------------------------------------------------------


def test_identity_discipline_three_distinct_fields() -> None:
    """Requested selector, effective endpoint base, and provider model id are distinct."""
    # Profile level
    profile = builtin_profiles()["glm-selfhosted-base"]
    assert profile.requested_selector == GLM_SELFHOSTED_BASE_MODEL_SELECTOR
    assert profile.effective_endpoint_base is None
    assert profile.provider_returned_model_id is None

    # Stamped at runtime / request time
    stamped = profile.with_identity(
        requested_selector="glm-ft/checkpoint-epoch-3",
        effective_endpoint_base="http://10.0.0.50:8000/v1",
        provider_returned_model_id="THUDM/glm-5.3-flash",
    )
    assert stamped.requested_selector == "glm-ft/checkpoint-epoch-3"
    assert stamped.effective_endpoint_base == "http://10.0.0.50:8000/v1"
    assert stamped.provider_returned_model_id == "THUDM/glm-5.3-flash"
    assert stamped.requested_selector != stamped.effective_endpoint_base
    assert stamped.requested_selector != stamped.provider_returned_model_id

    # Contract level (RunRequest)
    req = RunRequest(
        task=Path("/tmp"),
        agent="mini-swe-agent",
        name="test-trial",
        jobs_dir=Path("/tmp"),
        model="glm-ft/checkpoint-epoch-3",
        requested_selector="glm-ft/checkpoint-epoch-3",
        effective_endpoint_base="http://10.0.0.50:8000/v1",
        provider_returned_model_id="THUDM/glm-5.3-flash",
    )
    assert req.requested_selector == "glm-ft/checkpoint-epoch-3"
    assert req.effective_endpoint_base == "http://10.0.0.50:8000/v1"
    assert req.provider_returned_model_id == "THUDM/glm-5.3-flash"


def test_inference_settings_separation_from_weights() -> None:
    """Settings changes must not be conflated with weight changes."""
    base_profile = builtin_profiles()["glm-selfhosted-base"]

    # Explicit inference_settings recorded at request time
    high_effort = base_profile.with_inference_settings(effort="high", max_tokens=4096)
    low_effort = base_profile.with_inference_settings(effort="low", max_tokens=8192)

    assert high_effort.inference_settings.effort == "high"
    assert high_effort.inference_settings.max_tokens == 4096
    assert low_effort.inference_settings.effort == "low"
    assert low_effort.inference_settings.max_tokens == 8192

    # Digest differs between setting changes even with identical weights/model pin
    assert high_effort.digest != low_effort.digest
    assert high_effort.digest != base_profile.digest

    # RunRequest carries explicit inference settings
    req = RunRequest(
        task=Path("/tmp"),
        agent="mini-swe-agent",
        name="test-run",
        jobs_dir=Path("/tmp"),
        model=GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
        inference_settings=ProfileInferenceSettings(effort="high", max_tokens=4096),
    )
    assert req.inference_settings is not None
    assert req.inference_settings.effort == "high"
    assert req.inference_settings.max_tokens == 4096


# ---------------------------------------------------------------------------
# 3. Profiles and credentials tests
# ---------------------------------------------------------------------------


def test_builtin_profiles_have_glm_selfhosted_entries() -> None:
    profiles = builtin_profiles()
    assert "glm-selfhosted-base" in profiles
    assert "glm-selfhosted-ft" in profiles

    base = profiles["glm-selfhosted-base"]
    assert base.adapter == "mini-swe-agent"
    assert base.model == GLM_SELFHOSTED_BASE_MODEL_SELECTOR
    assert base.requested_selector == GLM_SELFHOSTED_BASE_MODEL_SELECTOR
    assert base.auth_mode == "api-key-environment"
    assert base.secret_source == "env:GLM_SELFHOSTED_API_KEY"

    ft = profiles["glm-selfhosted-ft"]
    assert ft.adapter == "mini-swe-agent"
    assert ft.model == GLM_SELFHOSTED_FT_MODEL_SELECTOR
    assert ft.requested_selector == GLM_SELFHOSTED_FT_MODEL_SELECTOR
    assert ft.auth_mode == "api-key-environment"
    assert ft.secret_source == "env:GLM_SELFHOSTED_API_KEY"


def test_custom_profile_ids_configurable() -> None:
    """Exact model IDs are configurable via profile."""
    custom_base = AgentProfile(
        profile_id="glm-selfhosted-custom-base",
        adapter="mini-swe-agent",
        model="glm-selfhosted/custom-checkpoint-v1",
        requested_selector="glm-selfhosted/custom-checkpoint-v1",
        auth_mode="api-key-environment",
        secret_source="env:GLM_SELFHOSTED_API_KEY",
        limits=ProfileLimits(max_timeout_seconds=600, max_attempts=1, max_concurrency=1),
    )
    assert custom_base.model == "glm-selfhosted/custom-checkpoint-v1"

    custom_ft = AgentProfile(
        profile_id="glm-selfhosted-custom-ft",
        adapter="mini-swe-agent",
        model="glm-ft/sft-step-1200",
        requested_selector="glm-ft/sft-step-1200",
        auth_mode="api-key-environment",
        secret_source="env:GLM_SELFHOSTED_API_KEY",
        limits=ProfileLimits(max_timeout_seconds=600, max_attempts=1, max_concurrency=1),
    )
    assert custom_ft.model == "glm-ft/sft-step-1200"


def test_secret_source_validator_admits_glm_selfhosted_key() -> None:
    # Valid
    p = AgentProfile(
        profile_id="valid-glm-profile",
        adapter="mini-swe-agent",
        model=GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
        auth_mode="api-key-environment",
        secret_source="env:GLM_SELFHOSTED_API_KEY",
        limits=ProfileLimits(max_timeout_seconds=600, max_attempts=1, max_concurrency=1),
    )
    assert p.secret_source == "env:GLM_SELFHOSTED_API_KEY"

    # Invalid secret source is rejected
    with pytest.raises(ValueError, match="only the admitted DeepSeek"):
        AgentProfile(
            profile_id="bad-glm-profile",
            adapter="mini-swe-agent",
            model=GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
            auth_mode="api-key-environment",
            secret_source="env:FORBIDDEN_KEY",
            limits=ProfileLimits(max_timeout_seconds=600, max_attempts=1, max_concurrency=1),
        )


def test_credentials_probing_and_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLM_SELFHOSTED_API_KEY", raising=False)
    assert not probe_glm_selfhosted_api()
    assert GLM_SELFHOSTED_API_CREDENTIAL not in available_credentials()

    # Missing credential report
    assert (
        missing_credential_for(
            "mini-swe-agent", frozenset(), GLM_SELFHOSTED_BASE_MODEL_SELECTOR
        )
        == GLM_SELFHOSTED_API_CREDENTIAL
    )
    assert (
        missing_credential_for(
            "mini-swe-agent", frozenset(), GLM_SELFHOSTED_FT_MODEL_SELECTOR
        )
        == GLM_SELFHOSTED_API_CREDENTIAL
    )
    assert (
        missing_credential_for(
            "mini-swe-agent", frozenset(), "glm-ft/custom-checkpoint"
        )
        == GLM_SELFHOSTED_API_CREDENTIAL
    )

    # When present in environment
    monkeypatch.setenv("GLM_SELFHOSTED_API_KEY", SECRET_SENTINEL)
    assert probe_glm_selfhosted_api()
    available = available_credentials()
    assert GLM_SELFHOSTED_API_CREDENTIAL in available
    assert (
        missing_credential_for(
            "mini-swe-agent", available, GLM_SELFHOSTED_BASE_MODEL_SELECTOR
        )
        is None
    )


def test_default_profile_for_adapter_mapping() -> None:
    assert (
        DEFAULT_PROFILE_FOR_ADAPTER[
            ("mini-swe-agent", GLM_SELFHOSTED_BASE_MODEL_SELECTOR)
        ]
        == "glm-selfhosted-base"
    )
    assert (
        DEFAULT_PROFILE_FOR_ADAPTER[
            ("mini-swe-agent", GLM_SELFHOSTED_FT_MODEL_SELECTOR)
        ]
        == "glm-selfhosted-ft"
    )


# ---------------------------------------------------------------------------
# 4. execution_contracts routing and commands tests
# ---------------------------------------------------------------------------


def test_resolve_harbor_agent_routes_selfhosted() -> None:
    assert (
        resolve_harbor_agent("mini-swe-agent", GLM_SELFHOSTED_BASE_MODEL_SELECTOR)
        == GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH
    )
    assert (
        resolve_harbor_agent("mini-swe-agent", GLM_SELFHOSTED_FT_MODEL_SELECTOR)
        == GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH
    )
    assert (
        resolve_harbor_agent("mini-swe-agent", "glm-ft/custom-checkpoint")
        == GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH
    )
    # Existing routes unchanged
    assert (
        resolve_harbor_agent("mini-swe-agent", ZAI_OPENAPI_MODEL_SELECTOR)
        == ZAI_MINISWE_AGENT_IMPORT_PATH
    )
    assert (
        resolve_harbor_agent("mini-swe-agent", DEEPSEEK_MODEL_SELECTOR)
        == "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
    )


def test_build_command_selfhosted_glm(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[agent]\ntimeout_sec = 600\n')

    req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        name="test-trial",
        jobs_dir=tmp_path / "jobs",
        model=GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
        inference_settings=ProfileInferenceSettings(effort="high", max_tokens=4096),
    )
    cmd = build_command(req)
    assert "--agent" in cmd
    idx = cmd.index("--agent")
    assert cmd[idx + 1] == GLM_SELFHOSTED_MINISWE_AGENT_IMPORT_PATH
    assert "--agent-kwarg" in cmd
    assert "cost_limit=2.5" in cmd
    assert "max_tokens=4096" in cmd
    assert "reasoning_effort=high" in cmd


def test_subscription_command_selfhosted_glm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[agent]\ntimeout_sec = 600\n')

    req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        name="test-trial",
        jobs_dir=tmp_path / "jobs",
        model=GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
    )

    # Unset endpoint raises ValueError naming GLM_SELFHOSTED_BASE_URL
    monkeypatch.delenv(GLM_SELFHOSTED_BASE_URL_ENV, raising=False)
    with pytest.raises(ValueError, match=f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set"):
        subscription_command(req, ["harbor", "run"], repo_root=tmp_path)

    # Invalid endpoint scheme raises ValueError
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, "ftp://bad-scheme")
    with pytest.raises(ValueError, match=f"{GLM_SELFHOSTED_BASE_URL_ENV} must be a valid http or https URL"):
        subscription_command(req, ["harbor", "run"], repo_root=tmp_path)

    # Valid endpoint passes through command cleanly without overlays
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)
    cmd = subscription_command(req, ["harbor", "run"], repo_root=tmp_path)
    assert cmd == ["harbor", "run"]


def test_subscription_environment_selfhosted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset endpoint fails closed
    monkeypatch.delenv(GLM_SELFHOSTED_BASE_URL_ENV, raising=False)
    with pytest.raises(ValueError, match=f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set"):
        subscription_environment(include_glm_selfhosted_credentials=True)

    # Valid endpoint sets base_url and proxy placeholder, key is NEVER included
    monkeypatch.setenv(GLM_SELFHOSTED_BASE_URL_ENV, TEST_BASE_URL)
    monkeypatch.setenv("GLM_SELFHOSTED_API_KEY", SECRET_SENTINEL)

    env = subscription_environment(include_glm_selfhosted_credentials=True)
    assert env[GLM_SELFHOSTED_BASE_URL_ENV] == TEST_BASE_URL
    assert env["OPENAI_BASE_URL"] == TEST_BASE_URL
    assert env["MSWEA_API_KEY"] == GLM_SELFHOSTED_PROXY_TOKEN
    assert "GLM_SELFHOSTED_API_KEY" not in env
    assert SECRET_SENTINEL not in env.values()


def test_redact_environment_includes_glm_selfhosted_key() -> None:
    raw = {
        "GLM_SELFHOSTED_API_KEY": SECRET_SENTINEL,
        "OTHER_ENV": "safe",
    }
    redacted = redact_environment(raw)
    assert redacted["GLM_SELFHOSTED_API_KEY"] == REDACTED_SECRET_VALUE
    assert redacted["OTHER_ENV"] == "safe"
