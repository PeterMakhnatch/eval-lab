"""Run-final records for the Terminus2 adapter (HAR-307).

``SecretSafeTerminus2.run`` seals each trial's evidence: the prose-completion
count and the loop-break record land in ``context.metadata`` and in every
trajectory file's ``final_metrics.extra``, and per-run state resets before the
next episode. These tests drive those ``finally`` paths, the finish-reason
LLM swap on the self-hosted MiMo route, the replay parser wrapper, and the
local-Ollama and breaker-selfhosted constructor bindings with the same fake
Harbor surface as ``tests/test_terminus2_runtime.py``: no network, no model
calls.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_BASE_MODEL,
    BREAKER_SELFHOSTED_CONTEXT_TOKENS,
    BREAKER_SELFHOSTED_MAX_COMPLETION_TOKENS,
    BREAKER_SELFHOSTED_MODEL_PREFIX,
    BREAKER_SELFHOSTED_PROXY_CAPABILITY_ENV,
    MIMO_SELFHOSTED_CONTEXT_TOKENS,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    MIMO_SELFHOSTED_TEMPERATURE,
    TERMINUS_PROXY_URL_ENV,
)
from evallab.terminus_local import OllamaBinding

CAPABILITY_SENTINEL = "test-runrecords-capability-token"


class _FakeTerminus2Options:
    """Upstream Terminus2Options stand-in (Harbor 0.24 options_model base)."""


class _FakeLiteLLM:
    """Upstream LiteLLM stand-in with the call and usage seams."""

    def __init__(self) -> None:
        self.usage_calls = 0

    async def call(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "upstream-response"

    def _extract_usage_info(self, response: Any) -> str:
        del response
        self.usage_calls += 1
        return "upstream-usage"


class _FakeTerminus2:
    """Upstream Terminus2 stand-in covering the adapter's super() seams."""

    run_error: BaseException | None = None
    llm_seam: Any = None

    def __init__(
        self,
        logs_dir: Path | str | None = None,
        model_name: str | None = None,
        *args: Any,
        api_base: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.logs_dir = Path(logs_dir) if logs_dir is not None else Path(".")
        self.model_name = model_name
        self.api_base = api_base
        self._llm_kwargs = llm_kwargs
        self._llm_call_kwargs = dict(llm_call_kwargs) if llm_call_kwargs else {}
        self._extra_env = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs
        self._trajectory_steps: list[Any] = []
        self._parser_name = "json"
        self._llm = _FakeLiteLLM()
        self.llm_seam = _FakeLiteLLM()

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction, environment, context
        if self.run_error is not None:
            raise self.run_error

    def _init_llm(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return self.llm_seam

    def _get_parser(self) -> Any:
        return {"stock": "json-parser"}

    def _reset_per_run_state(self) -> None:
        self.upstream_reset = True


class _FakeJSONPlainParser:
    """Stock Terminus JSON parser stand-in for the replay wrapper."""

    result: Any = None
    seen_text: str | None = None

    def parse_response(self, text: str) -> Any:
        type(self).seen_text = text
        return type(self).result


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
        sys.modules,
        "harbor.llms.lite_llm",
        _module("harbor.llms.lite_llm", LiteLLM=_FakeLiteLLM),
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
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.terminus_2.terminus_json_plain_parser",
        _module(
            "harbor.agents.terminus_2.terminus_json_plain_parser",
            TerminusJSONPlainParser=_FakeJSONPlainParser,
        ),
    )
    sys.modules.pop("evallab.harbor_terminus", None)
    try:
        return importlib.import_module("evallab.harbor_terminus")
    finally:
        sys.modules.pop("evallab.harbor_terminus", None)


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    """Bind the trial proxy endpoint and every route's capability."""
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    monkeypatch.setenv(BREAKER_SELFHOSTED_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", CAPABILITY_SENTINEL)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-original-openai-key")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    return terminus_module


def _mimo_agent(module: Any, tmp_path: Path, **kwargs: Any) -> Any:
    return module.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=MIMO_SELFHOSTED_MODEL_SELECTOR, **kwargs
    )


def _trajectory(path: Path, steps: list[dict[str, Any]], **payload: Any) -> None:
    path.write_text(json.dumps({"steps": steps, **payload}), encoding="utf-8")


def _prose_flagged_step(**extra: Any) -> dict[str, Any]:
    return {"extra": {"prose_completion": True, **extra}}


# ---------------------------------------------------------------------------
# run() finally: prose-completion and loop-break records reach the evidence
# ---------------------------------------------------------------------------


def test_run_seals_prose_completions_into_metadata_and_trajectories(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    _trajectory(
        tmp_path / "trajectory.json",
        [
            _prose_flagged_step(),
            _prose_flagged_step(),
            {"extra": {"prose_completion": True}, "is_copied_context": True},
            {"extra": {"other": True}},
            {"no_extra": None},
            "not-a-dict",
        ],
        final_metrics={"extra": {"keep": 1}},
    )
    _trajectory(
        tmp_path / "trajectory.cont-1.json",
        [_prose_flagged_step()],
    )
    # The live counter the episode's flag calls advanced; the file holds the
    # same two flagged agent steps plus steps that must not count.
    agent._prose_completions = 2
    context = SimpleNamespace(metadata={"n_episodes": 1}, cost_usd=1.25)

    asyncio.run(agent.run("task", None, context))

    assert context.metadata == {"n_episodes": 1, "prose_completions": 2}
    sealed = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert sealed["final_metrics"]["extra"] == {"keep": 1, "prose_completions": 2}
    sealed_cont = json.loads(
        (tmp_path / "trajectory.cont-1.json").read_text(encoding="utf-8")
    )
    assert sealed_cont["final_metrics"]["extra"] == {"prose_completions": 1}


def test_run_seals_loop_break_into_metadata_and_trajectories(
    bound: Any, tmp_path: Path
) -> None:
    agent = bound.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash", loop_break=True
    )
    agent._loop_nudged_call = 3
    agent._loop_detector = "identical_command_run"
    agent._loop_stop_call = 8
    agent._loop_model_next = "pytest -q"
    _trajectory(tmp_path / "trajectory.json", [], final_metrics={"n_steps": 8})
    _trajectory(tmp_path / "trajectory.cont-2.json", [])
    context = SimpleNamespace(metadata={"n_episodes": 1}, cost_usd=0.5)

    asyncio.run(agent.run("task", None, context))

    record = {
        "fired": True,
        "nudge_call": 3,
        "detector": "identical_command_run",
        "stop_call": 8,
        "model_next": "pytest -q",
    }
    assert context.metadata["loop_break"] == record
    assert context.metadata["stop_reason"] == "loop_break"
    assert context.metadata["n_episodes"] == 1
    for name in ("trajectory.json", "trajectory.cont-2.json"):
        payload = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert payload["final_metrics"]["extra"]["loop_break"] == record
    # The fired run keeps the trajectory's own metrics too.
    payload = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert payload["final_metrics"]["n_steps"] == 8


def test_run_records_an_unfired_loop_break_without_a_stop_reason(
    bound: Any, tmp_path: Path
) -> None:
    agent = bound.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash", loop_break=True
    )
    _trajectory(tmp_path / "trajectory.json", [])
    context = SimpleNamespace(metadata=None, cost_usd=0.0)

    asyncio.run(agent.run("task", None, context))

    assert context.metadata == {
        "loop_break": {
            "fired": False,
            "nudge_call": None,
            "detector": None,
            "stop_call": None,
            "model_next": None,
        }
    }
    payload = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert payload["final_metrics"]["extra"]["loop_break"]["fired"] is False


def test_run_seals_records_even_when_the_episode_raises(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    agent.run_error = RuntimeError("episode aborted")
    _trajectory(tmp_path / "trajectory.json", [_prose_flagged_step()])
    agent._prose_completions = 1
    context = SimpleNamespace(metadata={}, cost_usd=0.0)

    with pytest.raises(RuntimeError, match="episode aborted"):
        asyncio.run(agent.run("task", None, context))

    assert context.metadata["prose_completions"] == 1
    payload = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert payload["final_metrics"]["extra"]["prose_completions"] == 1


def test_missing_and_malformed_trajectory_files_are_skipped(
    bound: Any, tmp_path: Path
) -> None:
    (tmp_path / "trajectory.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "trajectory.cont-1.json").write_text("[]", encoding="utf-8")
    # A non-dict payload skips both writers; cont-2 is absent entirely.
    before = (tmp_path / "trajectory.json").read_text(encoding="utf-8")
    before_cont = (tmp_path / "trajectory.cont-1.json").read_text(encoding="utf-8")

    agent = _mimo_agent(bound, tmp_path, loop_break=True)
    context = SimpleNamespace(metadata=None, cost_usd=0.0)
    asyncio.run(agent.run("task", None, context))

    assert context.metadata["prose_completions"] == 0
    assert context.metadata["loop_break"]["fired"] is False
    assert (tmp_path / "trajectory.json").read_text(encoding="utf-8") == before
    assert (tmp_path / "trajectory.cont-1.json").read_text(encoding="utf-8") == before_cont


def test_a_failing_record_write_leaves_metadata_and_the_run_intact(
    bound: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _trajectory(tmp_path / "trajectory.json", [_prose_flagged_step()])
    monkeypatch.setattr(
        bound, "_record_prose_completions", _raises(RuntimeError("disk full"))
    )
    monkeypatch.setattr(
        bound, "_record_loop_break", _raises(RuntimeError("disk full"))
    )

    agent = _mimo_agent(bound, tmp_path, loop_break=True)
    agent._prose_completions = 1
    context = SimpleNamespace(metadata=None, cost_usd=0.0)
    asyncio.run(agent.run("task", None, context))

    assert context.metadata["prose_completions"] == 1
    assert context.metadata["loop_break"]["fired"] is False


def _raises(exc: BaseException) -> Any:
    def _fail(*args: Any, **kwargs: Any) -> None:
        raise exc

    return _fail


def test_run_without_any_trajectory_files_still_seals_metadata(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path, loop_break=True)
    agent._prose_completions = 1
    context = SimpleNamespace(metadata=None, cost_usd=0.0)

    asyncio.run(agent.run("task", None, context))

    assert context.metadata["prose_completions"] == 1
    assert context.metadata["loop_break"]["fired"] is False
    assert not (tmp_path / "trajectory.json").exists()


def test_per_run_state_resets_for_the_next_episode(bound: Any, tmp_path: Path) -> None:
    agent = _mimo_agent(bound, tmp_path, loop_break=True)
    agent._prose_completions = 4
    agent._pending_prose_step = object()
    agent._pending_layer = {"message": "m"}
    agent._layer_queue = [{"message": "m"}]
    agent._layer_steps_id = 9
    agent._layer_annotated = 2
    agent._loop_nudged_call = 3
    agent._loop_detector = "identical_command_run"
    agent._loop_stop_call = 8
    agent._loop_model_next = "ls"
    agent._loop_stop_pending = True
    agent._pending_composed_message = "leftover"

    agent._reset_per_run_state()

    assert agent.upstream_reset is True
    assert agent._prose_completions == 0
    assert agent._pending_prose_step is bound._NO_PENDING_PROSE_STEP
    assert agent._pending_layer is None
    assert agent._layer_queue == []
    assert agent._layer_steps_id is None
    assert agent._layer_annotated == 0
    assert agent._loop_nudged_call is None
    assert agent._loop_detector is None
    assert agent._loop_stop_call is None
    assert agent._loop_model_next is None
    assert agent._loop_stop_pending is False
    assert agent._pending_composed_message == ""


# ---------------------------------------------------------------------------
# Prose-completion step flagging (the anchor the sealed count reads)
# ---------------------------------------------------------------------------


def _step(source: str = "agent", **attrs: Any) -> Any:
    step = SimpleNamespace(source=source, is_copied_context=False, extra=None)
    for key, value in attrs.items():
        setattr(step, key, value)
    return step


def test_flag_prose_completion_marks_the_last_agent_step_once(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    flagged = _step()
    agent._trajectory_steps = [_step(), flagged]
    agent._pending_prose_step = object()

    agent._flag_prose_completion_step()

    assert flagged.extra == {"prose_completion": True}
    assert agent._prose_completions == 1
    assert agent._pending_prose_step is bound._NO_PENDING_PROSE_STEP


def test_flag_prose_completion_never_marks_non_agent_or_anchor_steps(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)

    # No anchor armed: the flag call is a no-op.
    agent._trajectory_steps = [_step()]
    agent._flag_prose_completion_step()
    assert agent._trajectory_steps[-1].extra is None
    assert agent._prose_completions == 0

    # The anchored step itself is last (the append never happened).
    anchor = _step()
    agent._trajectory_steps = [anchor]
    agent._pending_prose_step = anchor
    agent._flag_prose_completion_step()
    assert anchor.extra is None
    assert agent._prose_completions == 0

    # A system/user step last: that turn has no step to flag.
    agent._trajectory_steps = [_step(source="user")]
    agent._pending_prose_step = object()
    agent._flag_prose_completion_step()
    assert agent._trajectory_steps[-1].extra is None
    assert agent._prose_completions == 0

    # A copied-context agent step is never flagged.
    agent._trajectory_steps = [_step(is_copied_context=True)]
    agent._pending_prose_step = object()
    agent._flag_prose_completion_step()
    assert agent._trajectory_steps[-1].extra is None
    assert agent._prose_completions == 0


# ---------------------------------------------------------------------------
# Finish-reason LLM, parser binding, replay parser
# ---------------------------------------------------------------------------


def test_mimo_route_reclasses_the_llm_to_record_finish_reason(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    assert agent.extra_kwargs["model_info"] == {
        "max_input_tokens": MIMO_SELFHOSTED_CONTEXT_TOKENS,
        "max_output_tokens": MIMO_SELFHOSTED_CONTEXT_TOKENS,
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "openai",
    }
    assert agent.extra_kwargs["temperature"] == MIMO_SELFHOSTED_TEMPERATURE
    llm = agent._init_llm()

    assert type(llm).__name__ == "_FinishReasonLiteLLM"
    assert llm.last_finish_reason is None
    llm._extract_usage_info({"choices": [{"finish_reason": "stop"}]})
    assert llm.last_finish_reason == "stop"
    llm._extract_usage_info({"choices": [{"finish_reason": 7}]})
    assert llm.last_finish_reason is None
    llm._extract_usage_info({"no-choices": []})
    assert llm.last_finish_reason is None
    llm.last_finish_reason = "length"
    assert asyncio.run(llm.call()) == "upstream-response"
    assert llm.last_finish_reason is None

def test_mimo_route_requires_the_upstream_litellm_client(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    agent.llm_seam = object()
    with pytest.raises(ValueError, match="upstream's LiteLLM client"):
        agent._init_llm()


def test_other_routes_keep_the_upstream_llm(bound: Any, tmp_path: Path) -> None:
    agent = bound.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
    )
    llm = agent._init_llm()
    assert type(llm) is _FakeLiteLLM


def test_mimo_route_wraps_the_stock_parser_with_finish_reason_lookup(
    bound: Any, tmp_path: Path
) -> None:
    agent = _mimo_agent(bound, tmp_path)
    agent._llm.last_finish_reason = "stop"

    parser = agent._get_parser()

    assert type(parser).__name__ == "MimoToolCallParser"
    assert parser._inner == {"stock": "json-parser"}
    assert parser._finish_reason() == "stop"

    agent._parser_name = "plain"
    assert agent._get_parser() == {"stock": "json-parser"}

    other = bound.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
    )
    other._parser_name = "json"
    assert other._get_parser() == {"stock": "json-parser"}


def test_mimo_replay_parser_replays_the_stock_parse_with_clamped_durations(
    bound: Any,
) -> None:
    _FakeJSONPlainParser.result = SimpleNamespace(
        commands=[
            SimpleNamespace(keystrokes="ls -la", duration=90.0),
            SimpleNamespace(keystrokes="pwd", duration=5),
            SimpleNamespace(keystrokes="sleep", duration="long"),
        ],
        error=None,
        is_task_complete=True,
    )

    parse = bound.mimo_replay_parser()
    replayed = parse("raw model text")

    assert _FakeJSONPlainParser.seen_text == "raw model text"
    assert replayed.error is None
    assert replayed.task_complete is True
    assert [(c.keystrokes, c.duration_sec) for c in replayed.commands] == [
        ("ls -la", 60.0),
        ("pwd", 5),
        ("sleep", None),
    ]

    _FakeJSONPlainParser.result = SimpleNamespace(
        commands=[], error="unparseable", is_task_complete=False
    )
    failed = parse("garbage")
    assert failed.error == "unparseable"
    assert failed.commands == ()
    assert failed.task_complete is False


# ---------------------------------------------------------------------------
# Constructor bindings: local Ollama and breaker self-hosted (binding only)
# ---------------------------------------------------------------------------


def test_local_ollama_binding_binds_model_info_and_zero_cost_run(
    bound: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = OllamaBinding(
        selector="ollama_chat/qwen2.5:7b",
        model="qwen2.5:7b",
        model_digest="sha256:" + "ab" * 32,
        endpoint="http://127.0.0.1:11434",
        context_budget_tokens=8192,
    )
    monkeypatch.setattr(bound, "resolve_ollama_binding", lambda model: binding)

    agent = bound.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="ollama_chat/qwen2.5:7b"
    )

    assert agent.api_base == "http://127.0.0.1:11434"
    assert agent.extra_kwargs["model_info"] == {
        "max_input_tokens": 8192,
        "max_output_tokens": 8192,
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "ollama_chat",
    }
    with pytest.raises(ValueError, match="runtime-bound"):
        bound.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="ollama_chat/qwen2.5:7b",
            model_info={"max_input_tokens": 1},
        )

    context = SimpleNamespace(metadata={"n_episodes": 1}, cost_usd=9.99)
    asyncio.run(agent.run("task", None, context))
    assert context.cost_usd == 0.0
    assert context.metadata["local_ollama"] == binding.to_dict()


def test_breaker_selfhosted_binding_binds_model_info_and_capability(
    bound: Any, tmp_path: Path
) -> None:
    selector = f"{BREAKER_SELFHOSTED_MODEL_PREFIX}{BREAKER_SELFHOSTED_BASE_MODEL}"
    agent = bound.SecretSafeTerminus2(logs_dir=tmp_path, model_name=selector)

    assert agent.model_name == selector
    assert agent.api_base == "http://127.0.0.1:9"
    assert agent.extra_kwargs["model_info"] == {
        "max_input_tokens": BREAKER_SELFHOSTED_CONTEXT_TOKENS,
        "max_output_tokens": BREAKER_SELFHOSTED_MAX_COMPLETION_TOKENS,
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "openai",
    }
    assert agent.extra_kwargs["temperature"] == 1.0
    assert os.environ["OPENAI_API_KEY"] == CAPABILITY_SENTINEL
    with pytest.raises(ValueError, match="runtime-bound"):
        bound.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=selector,
            model_info={"max_input_tokens": 1},
        )

