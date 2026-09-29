"""Focused tests for the MiMo answer-leak blocklist on the Terminus-2 path.

HAR-83 found that ``SecretSafeTerminus2`` never applied FineEnvs'
``/var/lib/mimo/blocklist`` (``agents/mimo_opencode.py`` applies it after
install); the adapter now applies it in ``setup`` and fails closed. These
tests exercise the real helper and the real ``setup`` override against stub
environments — the only stubbed seam is the Harbor import and the container
``exec`` boundary, per the deterministic-test rule.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab.execution_contracts import (
    TERMINUS_PROXY_URL_ENV,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _ExecResult:
    def __init__(self, return_code: int, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _StubEnvironment:
    """Records ``exec`` calls; replays one canned result."""

    def __init__(self, result: _ExecResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.default_user: str | None = "root"

    async def exec(self, command: str, **kwargs: Any) -> _ExecResult:
        self.calls.append({"command": command, **kwargs})
        return self.result


class _StubLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class _FakeTerminus2:
    """Upstream stand-in: real setup/run ordering, stubbed session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.logger = _StubLogger()
        self.setup_calls = 0

    async def setup(self, environment: Any) -> None:
        self.setup_calls += 1
        self.setup_environment = environment


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
def terminus_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.agents.terminus_2",
        "harbor.llms",
    ):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.base",
        _module(
            "harbor.agents.installed.base",
            NonZeroAgentExitCodeError=type("NonZeroAgentExitCodeError", (RuntimeError,), {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "harbor.llms.lite_llm", _module("harbor.llms.lite_llm", LiteLLM=type("LiteLLM", (), {}))
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.terminus_2.terminus_2",
        _module("harbor.agents.terminus_2.terminus_2", Terminus2=_FakeTerminus2),
    )
    sys.modules.pop("evallab.harbor_terminus", None)
    try:
        return importlib.import_module("evallab.harbor_terminus")
    finally:
        sys.modules.pop("evallab.harbor_terminus", None)


@pytest.fixture
def bound_module(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(ZAI_OPENAPI_PROXY_CAPABILITY_ENV, "test-capability-token")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    return terminus_module


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def test_blocklist_applied_as_root_when_staged(bound_module: Any) -> None:
    env = _StubEnvironment(_ExecResult(0, stdout="33\n"))
    message = _run(bound_module.apply_mimo_blocklist(env))
    assert message == "33 hosts blocked in /etc/hosts"
    assert len(env.calls) == 1
    call = env.calls[0]
    assert call["user"] == "root"
    assert "cat /var/lib/mimo/blocklist >> /etc/hosts" in call["command"]
    assert "[ -f /var/lib/mimo/blocklist ]" in call["command"]


def test_blocklist_absent_is_noop(bound_module: Any) -> None:
    env = _StubEnvironment(_ExecResult(0, stdout="none\n"))
    message = _run(bound_module.apply_mimo_blocklist(env))
    assert message == "none for this task"
    assert len(env.calls) == 1
    assert env.calls[0]["user"] == "root"
    assert ">> /etc/hosts" in env.calls[0]["command"]


def test_blocklist_failure_fails_closed(bound_module: Any) -> None:
    env = _StubEnvironment(_ExecResult(1, stderr="Read-only file system"))
    with pytest.raises(RuntimeError, match="answer-leak blocklist"):
        _run(bound_module.apply_mimo_blocklist(env))


def test_setup_applies_blocklist_after_parent_setup(
    bound_module: Any, tmp_path: Path
) -> None:
    agent = bound_module.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
    )
    env = _StubEnvironment(_ExecResult(0, stdout="33\n"))
    _run(agent.setup(env))
    assert agent.setup_calls == 1
    assert len(env.calls) == 1
    assert env.calls[0]["user"] == "root"
    assert any("33 hosts blocked" in message for message in agent.logger.messages)


def test_setup_failure_blocks_agent_start(bound_module: Any, tmp_path: Path) -> None:
    agent = bound_module.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
    )
    env = _StubEnvironment(_ExecResult(1, stderr="Read-only file system"))
    with pytest.raises(RuntimeError, match="answer-leak blocklist"):
        _run(agent.setup(env))
    assert agent.setup_calls == 1
