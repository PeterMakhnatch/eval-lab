"""Z.ai/OpenCode adapter checks with an isolated Harbor stub."""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

# --------------------------------------------------------------------------
# Harbor stubs (adapter under test)
# --------------------------------------------------------------------------


@dataclass
class _StubConnection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)


class _StubOpenCode:
    """Minimal stand-in for Harbor's installed OpenCode agent."""

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.received_version = version
        self.exec_commands: list[str] = []
        self.run_calls = 0
        self.run_error: Exception | None = None
        del args, kwargs

    @property
    def model_connection(self) -> _StubConnection:
        return _StubConnection(provider="zai-coding-plan")

    async def exec_as_agent(self, environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, env, kwargs
        self.exec_commands.append(command)
        return "ok"

    async def run(self, instruction, environment, context) -> None:  # type: ignore[no-untyped-def]
        del instruction, context
        self.run_calls += 1
        self.exec_commands.append("<parent-run>")
        if self.run_error is not None:
            raise self.run_error


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
def adapter_module(monkeypatch: pytest.MonkeyPatch):
    for name in ("harbor", "harbor.agents", "harbor.agents.installed", "harbor.environments"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.opencode",
        _module("harbor.agents.installed.opencode", OpenCode=_StubOpenCode),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module("harbor.agents.model_connection", ResolvedModelConnection=_StubConnection),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_zai_opencode", None)
    try:
        yield importlib.import_module("evallab.harbor_zai_opencode")
    finally:
        sys.modules.pop("evallab.harbor_zai_opencode", None)


# --------------------------------------------------------------------------
# Adapter tests
# --------------------------------------------------------------------------


def test_adapter_model_guard_accepts_zai_coding_plan(adapter_module) -> None:
    # Plan-limited models must reach the provider; access failures remain provider errors.
    for model in (
        "zai-coding-plan/glm-5.3",
        "zai-coding-plan/glm-5.3-flash",
        "zai-coding-plan/glm-5.3-highspeed",
    ):
        assert (
            adapter_module.validate_model_name(model) == model
        )


@pytest.mark.parametrize(
    "model_name",
    [
        "openai/gpt-5.2",
        "zai/glm-5.3",
        "glm-5.3-flash",
        "",
        None,
    ],
)
def test_adapter_model_guard_rejects_other_providers(
    adapter_module, model_name: str | None
) -> None:
    with pytest.raises(ValueError, match="only accepts models under|provider/model"):
        adapter_module.validate_model_name(model_name)


def test_adapter_model_guard_rejects_bare_provider_prefix(adapter_module) -> None:
    """``zai-coding-plan/`` with no model suffix must fail closed."""
    with pytest.raises(ValueError, match="non-empty model"):
        adapter_module.validate_model_name("zai-coding-plan/")


def test_adapter_run_stages_and_removes_proxy_auth(adapter_module) -> None:
    agent = adapter_module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash"
    )
    asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == [
        adapter_module.CREATE_PROXY_AUTH_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_PROXY_AUTH_COMMAND,
    ]
    assert agent.run_calls == 1


def test_adapter_cleanup_failure_does_not_mask_original_error(adapter_module) -> None:
    class AgentBlewUp(RuntimeError):
        pass

    class CleanupBlewUp(RuntimeError):
        pass

    agent = adapter_module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash"
    )
    agent.run_error = AgentBlewUp("original failure")

    async def failing_cleanup(environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, env, kwargs
        if command == adapter_module.REMOVE_PROXY_AUTH_COMMAND:
            raise CleanupBlewUp("rm failed")
        agent.exec_commands.append(command)
        return "ok"

    agent.exec_as_agent = failing_cleanup  # type: ignore[method-assign]

    with pytest.raises(AgentBlewUp, match="original failure"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))


def test_adapter_run_refuses_wrong_provider_assigned_after_construction(
    adapter_module,
) -> None:
    agent = adapter_module.SecretSafeZaiOpenCodeAgent(
        model_name="zai-coding-plan/glm-5.3-flash"
    )
    agent.model_name = "openai/gpt-5.2"
    with pytest.raises(ValueError, match="only accepts models under"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == []
