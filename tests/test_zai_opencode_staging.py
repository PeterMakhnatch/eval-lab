"""Z.ai/OpenCode adapter checks with an isolated Harbor stub."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

SECRET_SENTINEL = "zai-secret-must-not-reach-exec"

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


def _make_agent(adapter_module, model_name: str, **kwargs: Any):  # type: ignore[no-untyped-def]
    return adapter_module.ZaiOpenCodeAgent(model_name=model_name, **kwargs)


# --------------------------------------------------------------------------
# Adapter tests
# --------------------------------------------------------------------------


def test_adapter_model_guard_accepts_zai_coding_plan(adapter_module) -> None:
    for model in ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"):
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


def test_adapter_model_guard_rejects_wrong_provider_at_construction(
    adapter_module,
) -> None:
    with pytest.raises(ValueError):
        _make_agent(adapter_module, "openai/gpt-5.2")


def test_adapter_link_run_cleanup_order(adapter_module) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]
    assert agent.run_calls == 1


def test_adapter_cleanup_runs_on_failure_and_preserves_original_error(
    adapter_module,
) -> None:
    class AgentBlewUp(RuntimeError):
        pass

    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    agent.run_error = AgentBlewUp("provider stream died")
    with pytest.raises(AgentBlewUp, match="provider stream died"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))
    # Cleanup still executed, after the failing parent run.
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]


def test_adapter_cleanup_failure_does_not_mask_original_error(adapter_module) -> None:
    class AgentBlewUp(RuntimeError):
        pass

    class CleanupBlewUp(RuntimeError):
        pass

    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    agent.run_error = AgentBlewUp("original failure")

    async def failing_cleanup(environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, env, kwargs
        if command.startswith("rm -f"):
            raise CleanupBlewUp("rm failed")
        agent.exec_commands.append(command)
        return "ok"

    agent.exec_as_agent = failing_cleanup  # type: ignore[method-assign]

    with pytest.raises(AgentBlewUp, match="original failure"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))


def test_adapter_commands_and_env_never_contain_secret_values(
    adapter_module,
) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    captured: list[dict[str, Any]] = []

    async def spying_exec(environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, kwargs
        captured.append({"command": command, "env": env})
        agent.exec_commands.append(command)
        return "ok"

    agent.exec_as_agent = spying_exec  # type: ignore[method-assign]
    asyncio.run(agent.run("do the task", environment=object(), context=object()))

    for record in captured:
        blob = json.dumps(record, default=str)
        assert SECRET_SENTINEL not in blob
        assert "api_key" not in blob
    # The two adapter commands are composed only of the constant paths.
    assert (
        f"mkdir -p {adapter_module.AUTH_LINK_DIR} && "
        f"ln -sfn {adapter_module.AUTH_SECRET_MOUNT} {adapter_module.AUTH_LINK_PATH}"
    ) == adapter_module.CREATE_AUTH_LINK_COMMAND
    assert f"rm -f {adapter_module.AUTH_LINK_PATH}" == adapter_module.REMOVE_AUTH_LINK_COMMAND


def test_adapter_run_refuses_wrong_provider_assigned_after_construction(
    adapter_module,
) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    # A wrong provider assigned after construction must fail closed BEFORE
    # the auth link is created.
    agent.model_name = "openai/gpt-5.2"
    with pytest.raises(ValueError, match="only accepts models under"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == []


def test_adapter_does_not_gate_plan_limited_models(adapter_module) -> None:
    """glm-5.3-highspeed passes the prefix guard; the provider answers 429.

    Observed provider fact (2026-08-29): the Coding Plan subscription does
    not yet include glm-5.3-highspeed and the provider returns HTTP 429. The
    adapter must NOT block the selector — the access failure has to surface
    as the provider's own error, distinctly from model outcomes, with no
    retry or fallback added here.
    """
    assert (
        adapter_module.validate_model_name("zai-coding-plan/glm-5.3-highspeed")
        == "zai-coding-plan/glm-5.3-highspeed"
    )
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-highspeed")
    asyncio.run(agent.run("do the task", environment=object(), context=object()))
    # The link/run/cleanup sequence executed normally; no retry loop exists.
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]


