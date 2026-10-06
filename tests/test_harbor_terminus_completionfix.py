"""The HAR-116 completion fix as the adapter actually applies it (HAR-96).

Once Terminus-2's confirm prompt is pending, a native ``task_complete``
tool call — or ``"task_complete": true`` inside a native call — counts as
the confirmation and ends the episode. These drive the real
``SecretSafeTerminus2._handle_llm_interaction`` against a stubbed upstream
parse, so they cover the part only the adapter knows: the pending-only
override fires with the knob, and behaviour is unchanged without it.
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

#: A verification command beside a native completion call: a parse error
#: without the fix, the confirmation with it.
MIXED_COMPLETE = (
    "<tool_call><function=bash><parameter=command>git diff --stat</parameter></function></tool_call>"
    "<tool_call><function=task_complete></function></tool_call>"
)

#: ``task_complete: true`` inside a native exec call's JSON arguments.
EMBEDDED_COMPLETE = (
    '<tool_call><function=exec_command>{"command": "pytest -q", '
    '"task_complete": true}</function></tool_call>'
)

#: A plain verification command with no completion signal at all.
VERIFY_ONLY = "<tool_call><function=bash><parameter=command>git diff --stat</parameter></function></tool_call>"


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


class _FakeTerminus2Options:
    """Upstream Terminus2Options stand-in (Harbor 0.24 options_model base)."""


class _FakeTerminus2:
    """Upstream stand-in returning one canned parse outcome."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._trajectory_steps: list[Any] = []
        self._pending_completion = False
        self._save_raw_content_in_trajectory = False
        self._canned_outcome: Any = None

    async def _handle_llm_interaction(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return self._canned_outcome

    def _dump_trajectory(self) -> None:
        return None


@pytest.fixture
def completionfix_module(monkeypatch: pytest.MonkeyPatch) -> Any:
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
        _module(
            "harbor.agents.terminus_2.terminus_2",
            Terminus2=_FakeTerminus2,
            Terminus2Options=_FakeTerminus2Options,
        ),
    )
    sys.modules.pop("evallab.harbor_terminus", None)
    try:
        return importlib.import_module("evallab.harbor_terminus")
    finally:
        sys.modules.pop("evallab.harbor_terminus", None)


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch, completionfix_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(ZAI_OPENAPI_PROXY_CAPABILITY_ENV, "test-capability-token")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    return completionfix_module


def _agent(module: Any, tmp_path: Path, **kwargs: Any) -> Any:
    return module.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash", **kwargs)


def _drive(
    bound: Any,
    tmp_path: Path,
    *,
    knob: bool,
    pending: bool,
    content: str,
    live_complete: bool = False,
) -> tuple[Any, Any]:
    """Run one parsed turn through the adapter; return (agent, outcome)."""
    agent = _agent(bound, tmp_path, completion_fix=knob)
    agent._parser = SimpleNamespace()
    agent._pending_completion = pending
    agent._canned_outcome = (
        [],
        live_complete,
        "",
        "analysis",
        "plan",
        SimpleNamespace(content=content, reasoning_content=None),
    )
    outcome = asyncio.run(agent._handle_llm_interaction())
    return agent, outcome


def test_pending_prompt_plus_native_task_complete_call_confirms(bound: Any, tmp_path: Path) -> None:
    agent, outcome = _drive(bound, tmp_path, knob=True, pending=True, content=MIXED_COMPLETE)
    assert outcome[1] is True
    assert agent._pending_layer is not None
    assert agent._pending_layer["task_complete"] is True


def test_pending_prompt_plus_embedded_task_complete_true_confirms(
    bound: Any, tmp_path: Path
) -> None:
    agent, outcome = _drive(bound, tmp_path, knob=True, pending=True, content=EMBEDDED_COMPLETE)
    assert outcome[1] is True
    assert agent._pending_layer is not None
    assert agent._pending_layer["task_complete"] is True


def test_without_the_knob_a_native_completion_turn_does_not_confirm(
    bound: Any, tmp_path: Path
) -> None:
    for content in (MIXED_COMPLETE, EMBEDDED_COMPLETE):
        agent, outcome = _drive(bound, tmp_path, knob=False, pending=True, content=content)
        assert outcome[1] is False
        assert agent._pending_layer is not None
        assert agent._pending_layer["task_complete"] is False


def test_verification_command_without_a_signal_never_confirms(bound: Any, tmp_path: Path) -> None:
    agent, outcome = _drive(bound, tmp_path, knob=True, pending=True, content=VERIFY_ONLY)
    assert outcome[1] is False


def test_native_completion_with_no_pending_prompt_does_not_confirm_early(
    bound: Any, tmp_path: Path
) -> None:
    # The fix is pending-only: a native-shaped turn before any prompt is not
    # a confirmation, so the first-claim prompt path is unchanged.
    for content in (MIXED_COMPLETE, EMBEDDED_COMPLETE):
        _agent_check, outcome = _drive(bound, tmp_path, knob=True, pending=False, content=content)
        assert outcome[1] is False


def test_claim_with_no_pending_prompt_still_reaches_the_prompt(bound: Any, tmp_path: Path) -> None:
    # A clean claim the parser already accepts passes through with or
    # without the knob; upstream then sends the confirm prompt.
    claim = '{"analysis": "done", "plan": "", "commands": [], "task_complete": true}'
    for knob in (False, True):
        _agent_check, outcome = _drive(
            bound, tmp_path, knob=knob, pending=False, content=claim, live_complete=True
        )
        assert outcome[1] is True


def test_non_boolean_completion_fix_refuses(bound: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="completion_fix must be a boolean"):
        _agent(bound, tmp_path, completion_fix="yes")
