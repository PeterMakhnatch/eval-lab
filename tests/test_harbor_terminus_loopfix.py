"""The HAR-116 loop fix as the adapter actually applies it.

The detector and the cap have their own tests. These drive the real
``SecretSafeTerminus2`` methods against a stub session, so they cover the
part only the adapter knows: the nudge is appended to the fed-back output
exactly once, the agent phase ends five calls later when the loop holds and
does not end when it breaks, and the full output is written to the sandbox
file the marker names.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from evallab.execution_contracts import (
    TERMINUS_PROXY_URL_ENV,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
)
from evallab.loopfix import LOOP_GRACE_CALLS, LOOP_NUDGE_MESSAGE, OUTPUT_CAP_CHARS
from evallab.token_flow import COMMAND_RUN_MIN


class _ExecResult:
    def __init__(self, return_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _StubEnvironment:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exec(self, command: str, **kwargs: Any) -> _ExecResult:
        del kwargs
        self.calls.append(command)
        return _ExecResult()


class _StubSession:
    def __init__(self, output: str) -> None:
        self.environment = _StubEnvironment()
        self._user = "agent"
        self.output = output

    async def get_incremental_output(self) -> str:
        return self.output


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


class _FakeTerminus2:
    """Upstream stand-in whose command execution returns the session's output."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._trajectory_steps: list[Any] = []
        self._pending_completion = False
        self._n_episodes = 1
        self._save_raw_content_in_trajectory = False

    async def _execute_commands(self, commands: Any, session: Any) -> Any:
        del commands
        return False, await session.get_incremental_output()

    def _dump_trajectory(self) -> None:
        return None


@pytest.fixture
def loopfix_module(monkeypatch: pytest.MonkeyPatch) -> Any:
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
        sys.modules,
        "harbor.llms.lite_llm",
        _module("harbor.llms.lite_llm", LiteLLM=type("LiteLLM", (), {})),
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
def bound(monkeypatch: pytest.MonkeyPatch, loopfix_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(ZAI_OPENAPI_PROXY_CAPABILITY_ENV, "test-capability-token")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    return loopfix_module


def _agent(module: Any, tmp_path: Path, **kwargs: Any) -> Any:
    return module.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash", **kwargs)


def _command(text: str) -> Any:
    return SimpleNamespace(keystrokes=text, duration_sec=0.1)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_output_cap_spills_the_full_output_and_feeds_back_both_ends(
    bound: Any, tmp_path: Path
) -> None:
    agent = _agent(bound, tmp_path, output_cap_chars=OUTPUT_CAP_CHARS)
    agent._n_episodes = 7
    full = "H" * 1500 + "M" * 9000 + "T" * 1500
    session = _StubSession(full)
    _timeout, fed_back = _run(agent._execute_commands([_command("ls\n")], session))
    assert fed_back.startswith("H" * (OUTPUT_CAP_CHARS // 2))
    assert fed_back.endswith("T" * (OUTPUT_CAP_CHARS - OUTPUT_CAP_CHARS // 2))
    spill = "/logs/agent/evallab-output/step-0007.txt"
    assert spill in fed_back
    # The whole output was written, not the trimmed copy: the spill commands
    # carry every character, base64-encoded, and the file is made readable.
    spilled = "".join(session.environment.calls)
    assert spill in spilled
    assert "mkdir -p /logs/agent/evallab-output" in spilled
    assert "chmod a+r" in spilled
    import base64

    payload = base64.b64encode(full.encode()).decode()
    assert payload[:48] in spilled and payload[-48:] in spilled


def test_short_output_is_fed_back_whole(bound: Any, tmp_path: Path) -> None:
    agent = _agent(bound, tmp_path, output_cap_chars=OUTPUT_CAP_CHARS)
    session = _StubSession("short output\n")
    _timeout, fed_back = _run(agent._execute_commands([_command("ls\n")], session))
    assert fed_back == "short output\n"


def test_loop_nudges_once_then_ends_the_agent_phase(bound: Any, tmp_path: Path) -> None:
    agent = _agent(bound, tmp_path, loop_break=True)
    session = _StubSession("no change\n")
    command = _command("pytest -q\n")
    nudged_at = None
    for call in range(1, COMMAND_RUN_MIN + LOOP_GRACE_CALLS + 1):
        _timeout, fed_back = _run(agent._execute_commands([command], session))
        agent._trajectory_steps.append(
            SimpleNamespace(
                source="agent",
                message="working",
                tool_calls=[
                    SimpleNamespace(
                        function_name="bash_command", arguments={"keystrokes": "pytest -q\n"}
                    )
                ],
                is_copied_context=False,
            )
        )
        if LOOP_NUDGE_MESSAGE in fed_back:
            assert nudged_at is None  # one nudge, never repeated
            nudged_at = call
        if call < COMMAND_RUN_MIN + LOOP_GRACE_CALLS:
            agent._dump_trajectory()  # the loop holding does not end the phase yet
            continue
        with pytest.raises(bound.LoopBreakStop):
            agent._dump_trajectory()
    assert nudged_at == COMMAND_RUN_MIN
    assert agent._loop_stop_call == COMMAND_RUN_MIN + LOOP_GRACE_CALLS
    assert agent._loop_model_next == "pytest -q"


def test_a_broken_loop_never_ends_the_agent_phase(bound: Any, tmp_path: Path) -> None:
    agent = _agent(bound, tmp_path, loop_break=True)
    session = _StubSession("no change\n")
    for _call in range(COMMAND_RUN_MIN):
        _run(agent._execute_commands([_command("pytest -q\n")], session))
        agent._trajectory_steps.append(
            SimpleNamespace(
                source="agent",
                message="working",
                tool_calls=[
                    SimpleNamespace(
                        function_name="bash_command", arguments={"keystrokes": "pytest -q\n"}
                    )
                ],
                is_copied_context=False,
            )
        )
    # The model changes approach on the next call: no stop, ever.
    _timeout, fed_back = _run(agent._execute_commands([_command("ls /app\n")], session))
    assert LOOP_NUDGE_MESSAGE not in fed_back
    for _call in range(LOOP_GRACE_CALLS + 2):
        agent._dump_trajectory()
    assert agent._loop_stop_call is None
    assert agent._loop_model_next == "ls /app"


def test_disabled_loop_fix_changes_nothing(bound: Any, tmp_path: Path) -> None:
    agent = _agent(bound, tmp_path)
    session = _StubSession("no change\n")
    for _call in range(COMMAND_RUN_MIN + LOOP_GRACE_CALLS):
        _timeout, fed_back = _run(agent._execute_commands([_command("pytest -q\n")], session))
        assert fed_back == "no change\n"
        assert LOOP_NUDGE_MESSAGE not in fed_back
    agent._dump_trajectory()
    assert agent._loop_nudged_call is None
