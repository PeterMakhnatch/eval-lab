from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

SECRET_SENTINEL = "secret-must-not-reach-exec"


@dataclass(frozen=True)
class _Connection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)


class _MiniSweAgent:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.exec_calls: list[tuple[str, dict[str, str] | None]] = []

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
        _module(
            "harbor.agents.installed.mini_swe_agent",
            MiniSweAgent=_MiniSweAgent,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module(
            "harbor.agents.model_connection",
            ResolvedModelConnection=_Connection,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_deepseek", None)
    try:
        yield importlib.import_module("evallab.harbor_deepseek")
    finally:
        sys.modules.pop("evallab.harbor_deepseek", None)


def test_wrapper_replaces_real_key_and_keeps_it_out_of_exec(
    wrapper_module: ModuleType,
) -> None:
    module = wrapper_module
    agent = module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(
            provider="deepseek",
            api_key=SECRET_SENTINEL,
            env={"DEEPSEEK_API_KEY": SECRET_SENTINEL, "SAFE_FLAG": "present"},
        )
    )

    connection = agent.model_connection
    assert connection.api_key == "<mounted-compose-secret>"
    assert connection.env == {"SAFE_FLAG": "present"}

    result = asyncio.run(
        agent.exec_as_agent(
            object(),
            "mini-swe-agent --yolo",
            env={"MSWEA_CONFIGURED": "true"},
        )
    )

    assert result == "ok"
    command, exec_env = agent.exec_calls[0]
    assert module.DEEPSEEK_SECRET_PATH in command
    assert "DEEPSEEK_API_KEY" in command
    assert SECRET_SENTINEL not in command
    assert exec_env == {"MSWEA_CONFIGURED": "true"}


def test_wrapper_rejects_non_deepseek_models(wrapper_module: ModuleType) -> None:
    module = wrapper_module
    agent = module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(provider="openai", api_key=SECRET_SENTINEL)
    )

    with pytest.raises(ValueError, match="requires a deepseek/\\* model"):
        _ = agent.model_connection
