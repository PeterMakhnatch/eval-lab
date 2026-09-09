"""Focused behavioral coverage for the authors-RLM Harbor root agent."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def _install_harbor_stubs() -> dict[str, Any]:
    harbor = types.ModuleType("harbor")
    agents = types.ModuleType("harbor.agents")
    agents_base = types.ModuleType("harbor.agents.base")
    environments = types.ModuleType("harbor.environments")
    environments_base = types.ModuleType("harbor.environments.base")
    models = types.ModuleType("harbor.models")
    models_agent = types.ModuleType("harbor.models.agent")
    models_context = types.ModuleType("harbor.models.agent.context")

    class ExecResult:
        def __init__(
            self, stdout: str | None = None, stderr: str | None = None, return_code: int = 0
        ) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.return_code = return_code

    class AgentContext:
        def __init__(self) -> None:
            self.n_input_tokens: int | None = None
            self.n_output_tokens: int | None = None
            self.metadata: dict[str, Any] | None = None

    class BaseAgent:
        SUPPORTS_ATIF = False

        def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any) -> None:
            self.logs_dir = Path(logs_dir)
            self.model_name = model_name
            self.session_id = kwargs.get("session_id")
            self._extra = kwargs

    class BaseEnvironment:
        pass

    agents_base.BaseAgent = BaseAgent
    environments_base.BaseEnvironment = BaseEnvironment
    environments_base.ExecResult = ExecResult
    models_context.AgentContext = AgentContext
    agents.base = agents_base
    environments.base = environments_base
    models.agent = models_agent

    created = {
        "BaseAgent": BaseAgent,
        "BaseEnvironment": BaseEnvironment,
        "ExecResult": ExecResult,
        "AgentContext": AgentContext,
    }
    for name, module in {
        "harbor": harbor,
        "harbor.agents": agents,
        "harbor.agents.base": agents_base,
        "harbor.environments": environments,
        "harbor.environments.base": environments_base,
        "harbor.models": models,
        "harbor.models.agent": models_agent,
        "harbor.models.agent.context": models_context,
    }.items():
        sys.modules[name] = module
    return created


_STUBS = _install_harbor_stubs()
sys.path.insert(0, str(_REPO_SRC))

from evallab.harbor_rlm import (  # noqa: E402
    AGENT_VERSION,
    AUTHORS_RLM_IMPORT,
    MANAGED_BACKEND_IMPORT,
    MINI_SWE_AGENT_IMPORT,
    MINI_SWE_AGENT_VERSION,
    AuthorsRlmAgent,
    HarnessBackendContractError,
    ProvidedEnvironmentReplBackend,
    ScriptedRootClient,
    baseline_import_path,
    candidate_import_path,
    find_repl_blocks,
    managed_backend_factory,
)


class FakeEnvironment(_STUBS["BaseEnvironment"]):
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.exec_calls: list[str] = []
        self.stopped = False

    async def exec(
        self, command: str, cwd: str | None = None, timeout_sec: int | None = None
    ) -> Any:
        self.exec_calls.append(command)
        if command.startswith("find "):
            return _STUBS["ExecResult"](stdout="./input/events.jsonl", return_code=0)
        if command.startswith("mkdir -p"):
            try:
                path = command.rsplit(">", 1)[1].strip()
                content = command.split("printf '%s' '", 1)[1].rsplit("' >", 1)[0]
            except IndexError:
                return _STUBS["ExecResult"](stdout="", stderr="parse", return_code=1)
            self.files[path] = content
            return _STUBS["ExecResult"](stdout="", return_code=0)
        if command.startswith("cat "):
            path = command.split(" ", 1)[1]
            if path in self.files:
                return _STUBS["ExecResult"](stdout=self.files[path], return_code=0)
            return _STUBS["ExecResult"](stdout="", stderr="missing", return_code=1)
        return _STUBS["ExecResult"](stdout="", return_code=0)

    async def stop(self) -> None:
        self.stopped = True


PROBE_BLOCK = '```repl\nprint("visible")\n```'
GENERIC_BLOCK = '```python\nprint("not repl")\n```'


def test_repl_parser_accepts_only_repl_fences() -> None:
    assert find_repl_blocks(PROBE_BLOCK + "\n" + GENERIC_BLOCK) == ['print("visible")']


def test_import_paths_and_versions_declared() -> None:
    from evallab.execution_contracts import HARBOR_AGENT_IMPORT_PATHS, resolve_harbor_agent

    assert baseline_import_path() == MINI_SWE_AGENT_IMPORT
    assert candidate_import_path() == AUTHORS_RLM_IMPORT
    assert MINI_SWE_AGENT_VERSION == "2.4.6"
    assert AGENT_VERSION.startswith("0.1.0")
    assert MANAGED_BACKEND_IMPORT == "evallab.rlm_runtime:ManagedReplBackend"
    assert HARBOR_AGENT_IMPORT_PATHS["authors-rlm"] == AUTHORS_RLM_IMPORT
    assert resolve_harbor_agent("authors-rlm") == AUTHORS_RLM_IMPORT


def _script_root() -> ScriptedRootClient:
    inspect = "```repl\nprint(len(context))\n```"
    solve = '```repl\nwrite_file("/app/output/summary.json", llm_query("summarize"))\nanswer["content"] = "done"\nanswer["ready"] = True\n```'
    return ScriptedRootClient([inspect, solve])


def test_full_loop_writes_artifact_and_consumes_worker_hook(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeEnvironment, ProvidedEnvironmentReplBackend]:
        environment = FakeEnvironment()
        worker_texts: list[str] = []

        def hook(prompt: str) -> str:
            worker_texts.append(prompt)
            assert prompt == "summarize"
            return '{"events": 2}'

        backend = ProvidedEnvironmentReplBackend(environment, sub_llm=hook)
        client = _script_root()
        agent = AuthorsRlmAgent(
            tmp_path,
            model_name="deepseek/deepseek-v4-flash",
            worker_model="deepseek/deepseek-v4-flash",
            backend_factory=lambda env: backend,
            root_client=client,
            working_dir="/app",
        )
        context = _STUBS["AgentContext"]()
        await agent.run("Summarize the events", environment, context)
        assert backend.stopped
        assert not environment.stopped
        assert backend.worker_prompts == ["summarize"]
        assert worker_texts == ["summarize"]
        assert context.metadata is not None
        assert context.metadata["root_model"] == "deepseek/deepseek-v4-flash"
        assert context.metadata["worker_calls"] == 1
        assert context.metadata["root_calls"] == 2
        assert context.n_input_tokens == 3 * 2
        logs = (tmp_path / "rlm" / "root-messages.json").read_text()
        assert '"ready": true' in logs.replace("True", "true") or "ready" in logs
        return environment, backend

    environment, _ = asyncio.run(scenario())
    assert environment.files["/app/output/summary.json"] == '{"events": 2}'


def test_worker_single_and_batched_calls_reach_hook() -> None:
    async def scenario() -> None:
        environment = FakeEnvironment()
        seen: list[str] = []
        backend = ProvidedEnvironmentReplBackend(
            environment, sub_llm=lambda prompt: seen.append(prompt) or f"r:{prompt}"
        )
        await backend.start(proxy_url="hook://trial-worker", rollout_id="r", depth=1)
        await backend.load_context("payload")
        first = await backend.execute(
            '```repl\nprint(llm_query("one"))\n```' if False else 'print(llm_query("one"))'
        )
        assert first.stdout.strip() == "r:one"
        second = await backend.execute('print(llm_query_batched(["a", "b"]))')
        assert "['r:a', 'r:b']" in second.stdout
        assert backend.worker_prompts == ["one", "a", "b"]
        assert seen == ["one", "a", "b"]
        await backend.stop()

    asyncio.run(scenario())


def test_cancellation_stops_backend_but_keeps_trial(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment = FakeEnvironment()
        backend = ProvidedEnvironmentReplBackend(environment, sub_llm=lambda prompt: prompt)
        client = ScriptedRootClient(['```repl\nprint("never finishes")\n```'] * 30)
        agent = AuthorsRlmAgent(
            tmp_path,
            model_name="deepseek/deepseek-v4-flash",
            backend_factory=lambda env: backend,
            root_client=client,
        )

        async def slow_execute(code: str) -> Any:
            await asyncio.sleep(60.0)
            return await ProvidedEnvironmentReplBackend.execute(backend, code)

        backend.execute = slow_execute  # type: ignore[method-assign]
        task = asyncio.create_task(agent.run("Slow", environment, _STUBS["AgentContext"]()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.stopped
        assert not environment.stopped
        assert not (tmp_path / "rlm" / "solution.txt").exists()

    asyncio.run(scenario())


def test_iteration_exhaustion_returns_without_raising(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment = FakeEnvironment()
        backend = ProvidedEnvironmentReplBackend(environment, sub_llm=lambda prompt: prompt)
        client = ScriptedRootClient(['```repl\nprint("no answer")\n```'] * 2)
        agent = AuthorsRlmAgent(
            tmp_path,
            model_name="deepseek/deepseek-v4-flash",
            backend_factory=lambda env: backend,
            root_client=client,
            max_iterations=2,
        )
        context = _STUBS["AgentContext"]()
        await agent.run("No ready", environment, context)
        assert backend.stopped
        assert not environment.stopped
        assert context.metadata is not None
        assert context.metadata["exhausted_iterations"] is True
        assert context.metadata["root_calls"] == 2

    asyncio.run(scenario())


def test_answer_rebind_update_and_ready_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment = FakeEnvironment()
        backend = ProvidedEnvironmentReplBackend(environment, sub_llm=lambda prompt: prompt)
        rebind = '```repl\nanswer = {"content": "rebound", "ready": True}\n```'
        update = '```repl\nanswer.update({"content": "updated", "ready": True})\n```'
        ordered = '```repl\nanswer["ready"] = True\nanswer["content"] = "ordered"\n```'
        for code, expected in (
            (rebind, "rebound"),
            (update, "updated"),
            (ordered, "ordered"),
        ):
            client = ScriptedRootClient([code])
            agent = AuthorsRlmAgent(
                tmp_path,
                model_name="deepseek/deepseek-v4-flash",
                backend_factory=lambda env, b=backend: b,
                root_client=client,
                max_iterations=1,
            )
            context = _STUBS["AgentContext"]()
            await agent.run("Complete", environment, context)
            assert context.metadata is not None
            assert context.metadata["exhausted_iterations"] is False
            assert backend._final == expected

    asyncio.run(scenario())


def test_gepa_extra_instruction_path_reaches_root_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        instruction_file = tmp_path / "extra.md"
        instruction_file.write_text("GEPA-SKILL: chunk-first\n", encoding="utf-8")
        environment = FakeEnvironment()
        backend = ProvidedEnvironmentReplBackend(environment, sub_llm=lambda prompt: prompt)
        client = ScriptedRootClient(
            ['```repl\nanswer["content"] = "x"\nanswer["ready"] = True\n```']
        )
        agent = AuthorsRlmAgent(
            tmp_path,
            model_name="deepseek/deepseek-v4-flash",
            backend_factory=lambda env: backend,
            root_client=client,
            extra_instruction_path=instruction_file,
        )
        await agent.run("Task", environment, _STUBS["AgentContext"]())
        system_text = client.calls[0][0]["content"]
        assert "GEPA-SKILL: chunk-first" in system_text
        assert "rlm_query" in system_text

    asyncio.run(scenario())


def test_har10_without_environment_parameter_fails_closed() -> None:
    class DebugOwnedBackend:
        def __init__(self, *, image: str = "python:3.13-slim") -> None:
            self.image = image

    with pytest.raises(HarnessBackendContractError, match="must borrow the trial BaseEnvironment"):
        from evallab.harbor_rlm import _require_environment_backend

        _require_environment_backend(DebugOwnedBackend)


def test_uninspectable_backend_contract_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect as inspect_module

    from evallab.harbor_rlm import _require_environment_backend

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("no signature available")

    monkeypatch.setattr(inspect_module, "signature", boom)
    with pytest.raises(HarnessBackendContractError, match="uninspectable"):
        _require_environment_backend(int)


def test_managed_factory_without_published_module_fails_closed() -> None:
    with pytest.raises(HarnessBackendContractError, match="not importable"):
        managed_backend_factory(object(), session_id="s")  # type: ignore[arg-type]


def test_managed_factory_requires_worker_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBackend:
        def __init__(
            self,
            environment: Any,
            *,
            worker_src: Path,
            aiohttp_wheels: list[Path],
            session_id: str = "rlm-managed",
            work_root: str = "/opt/rlm-managed",
        ) -> None:
            del environment, worker_src, aiohttp_wheels, session_id, work_root

    module = types.ModuleType("evallab.rlm_runtime")
    module.ManagedReplBackend = FakeBackend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "evallab.rlm_runtime", module)
    with pytest.raises(HarnessBackendContractError, match="worker_src"):
        managed_backend_factory(object(), session_id="s")  # type: ignore[arg-type]


def test_default_backend_path_fails_closed_without_har10(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = AuthorsRlmAgent(
            tmp_path,
            model_name="deepseek/deepseek-v4-flash",
            root_client=_script_root(),
        )
        with pytest.raises(HarnessBackendContractError, match="not importable"):
            await agent.run("Task", FakeEnvironment(), _STUBS["AgentContext"]())

    asyncio.run(scenario())


def test_second_markdown_fence_style_rejected() -> None:
    assert find_repl_blocks("``` REPL\nprint(1)\n```") == []
