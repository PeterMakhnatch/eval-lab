"""Harbor BaseAgent for a depth-1 authors-RLM root loop.

This is not the DSPy ``dspy-rlm`` adapter. The candidate arm borrows the
Harbor trial ``BaseEnvironment`` and talks to PostTraining's
``ManagedReplBackend`` once HAR-10 accepts ``environment=``. Nested Docker
construction is refused.

Baseline arm (unchanged): ``evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent``
with mini-swe-agent 2.4.6.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import re
import shlex
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.environments.base import (  # ty: ignore[unresolved-import]
    BaseEnvironment,
    ExecResult,
)
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.harbor_rlm_prompts import (
    PARSING_SOURCE_SHA256,
    PROMPT_SOURCE_SHA256,
    RLM_SYSTEM_PROMPT,
    QueryMetadata,
    build_rlm_system_prompt,
    build_user_prompt,
)

AGENT_VERSION = "0.1.0-har12"
MINI_SWE_AGENT_VERSION = "2.4.6"
MINI_SWE_AGENT_IMPORT = "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
AUTHORS_RLM_IMPORT = "evallab.harbor_rlm:AuthorsRlmAgent"
MANAGED_BACKEND_IMPORT = "evallab.rlm_runtime:ManagedReplBackend"
REPL_CODE_PATTERN = re.compile(r"```repl\s*\n(.*?)\n```", re.DOTALL)
_MAX_REPL_OUTPUT_CHARS = 20_000


class HarnessBackendContractError(RuntimeError):
    """HAR-10 backend is missing or still owns its own environment."""


@dataclass
class RootCompletion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class RootChatClient(Protocol):
    def complete(self, messages: Sequence[dict[str, str]]) -> RootCompletion: ...


class ReplBackend(Protocol):
    async def start(self, proxy_url: str, rollout_id: str, depth: int = 1) -> None: ...

    async def load_context(self, payload: Any, index: int | None = None) -> int: ...

    async def execute(self, code: str) -> Any: ...

    async def stop(self) -> None: ...

    async def bootstrap(self, code: str) -> None: ...


@dataclass
class ProtocolExecResult:
    stdout: str = ""
    stderr: str = ""
    final_answer: str | None = None
    execution_time: float = 0.0
    locals_keys: list[str] = field(default_factory=list)


class AnswerDict(dict):
    def __init__(self, on_ready: Callable[[str], None] | None = None) -> None:
        super().__init__()
        super().__setitem__("content", "")
        super().__setitem__("ready", False)
        self._on_ready = on_ready

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self._emit_if_ready()

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        self._emit_if_ready()

    def _emit_if_ready(self) -> None:
        if self.get("ready") and self._on_ready is not None:
            self._on_ready(str(self.get("content", "")))


def find_repl_blocks(text: str) -> list[str]:
    """Authors parser: only ```repl fences, not generic markdown."""
    return [match.group(1).strip() for match in REPL_CODE_PATTERN.finditer(text)]


def baseline_import_path() -> str:
    return MINI_SWE_AGENT_IMPORT


def candidate_import_path() -> str:
    return AUTHORS_RLM_IMPORT


def _require_environment_backend(cls: type) -> None:
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError) as exc:
        raise HarnessBackendContractError(
            f"{MANAGED_BACKEND_IMPORT} constructor is uninspectable: {exc}"
        ) from exc
    if "environment" not in params:
        names = ", ".join(name for name in params if name != "self")
        raise HarnessBackendContractError(
            "HAR-10 ManagedReplBackend must borrow the trial BaseEnvironment "
            f"(parameter 'environment'). Current parameters: {names or '(none)'}. "
            "This agent refuses nested Docker construction."
        )


def managed_backend_factory(
    environment: BaseEnvironment,
    *,
    session_id: str,
    worker_src: Path | None = None,
    aiohttp_wheels: list[Path] | None = None,
    work_root: str = "/opt/rlm-managed",
) -> ReplBackend:
    """Construct HAR-10's published backend. Fails closed if it is absent or incomplete."""
    try:
        from evallab.rlm_runtime import ManagedReplBackend
    except ImportError as exc:
        raise HarnessBackendContractError(
            "HAR-10 ManagedReplBackend is not importable. Publish "
            f"{MANAGED_BACKEND_IMPORT} (PR #392). Import error: {exc}"
        ) from exc
    _require_environment_backend(ManagedReplBackend)
    if worker_src is None or aiohttp_wheels is None:
        raise HarnessBackendContractError(
            "HAR-10 ManagedReplBackend requires pinned worker_src and "
            "aiohttp_wheels from the published backend packet"
        )
    return ManagedReplBackend(
        environment,
        worker_src=Path(worker_src),
        aiohttp_wheels=[Path(wheel) for wheel in aiohttp_wheels],
        session_id=session_id,
        work_root=work_root,
    )


class ProvidedEnvironmentReplBackend:
    """CPU/protocol REPL that executes in-process and writes through the trial env.

    This is not a substitute for HAR-10's managed worker. It exists so the root
    loop, file effects, subcall hook, and cancellation can be exercised without
    creating a nested Docker environment.
    """

    def __init__(
        self,
        environment: BaseEnvironment,
        *,
        sub_llm: Callable[[str], str],
        working_dir: str = "/app",
    ) -> None:
        self._environment = environment
        self._sub_llm = sub_llm
        self._working_dir = working_dir
        self._namespace: dict[str, Any] = {}
        self._pending_env_calls: list[tuple[str, str, asyncio.Future[ExecResult]]] = []
        self._replay_cache: dict[tuple[str, ...], Any] = {}
        self._final: str | None = None
        self._started = False
        self.worker_prompts: list[str] = []
        self.stopped = False
        self.environment_stopped = False

    async def start(self, proxy_url: str, rollout_id: str, depth: int = 1) -> None:
        del proxy_url, rollout_id
        if depth != 1:
            raise ValueError("Harbor authors-RLM comparison is depth-1 only")
        self._started = True
        self._namespace = {
            "llm_query": self._llm_query,
            "llm_query_batched": self._llm_query_batched,
            "rlm_query": self._llm_query,
            "rlm_query_batched": self._llm_query_batched,
            "SHOW_VARS": lambda: ", ".join(sorted(self._namespace)),
            "answer": AnswerDict(self._mark_ready),
            "write_file": self._write_file,
            "read_file": self._read_file,
        }

    async def load_context(self, payload: Any, index: int | None = None) -> int:
        del index
        self._namespace["context"] = payload
        return 1

    async def bootstrap(self, code: str) -> None:
        if code:
            await self.execute(code)

    async def execute(self, code: str) -> ProtocolExecResult:
        if not self._started:
            raise RuntimeError("REPL backend has not started")
        loop = asyncio.get_running_loop()
        self._replay_cache = {}
        self._final = None

        def env_run(command: str, cwd: str) -> ExecResult:
            key = ("env", command, cwd)
            cacheable = command.startswith("mkdir -p")
            if cacheable and key in self._replay_cache:
                cached = self._replay_cache[key]
                if isinstance(cached, BaseException):
                    raise cached
                return cached
            future: asyncio.Future[ExecResult] = loop.create_future()
            self._pending_env_calls.append((command, cwd, future))
            return _pump_future(future)

        stdout_text = ""
        stderr = ""
        namespace: dict[str, Any] = {}
        while True:
            namespace = dict(self._namespace)
            namespace["write_file"] = lambda path, content: _protocol_write_file(
                env_run, path, content, self._working_dir
            )
            namespace["read_file"] = lambda path: _protocol_read_file(
                env_run, path, self._working_dir
            )
            chunk = io.StringIO()
            try:
                with redirect_stdout(chunk):
                    exec(code, namespace, namespace)  # noqa: S102
            except _SuspendExecution:
                await self._flush_pending_file_ops()
                continue
            except Exception as exc:  # noqa: BLE001 - REPL errors are returned to the root LM
                stderr = f"{type(exc).__name__}: {exc}"
            stdout_text = chunk.getvalue()
            break
        await self._flush_pending_file_ops()
        self._capture_answer(namespace)
        self._namespace = namespace
        if len(stdout_text) > _MAX_REPL_OUTPUT_CHARS:
            stdout_text = stdout_text[:_MAX_REPL_OUTPUT_CHARS] + "\n[truncated]"
        return ProtocolExecResult(
            stdout=stdout_text,
            stderr=stderr,
            final_answer=self._final,
            locals_keys=sorted(self._namespace),
        )

    async def _flush_pending_file_ops(self) -> None:
        pending, self._pending_env_calls = self._pending_env_calls, []
        for command, cwd, future in pending:
            try:
                result = await self._environment.exec(command=command, cwd=cwd, timeout_sec=30)
            except Exception as exc:  # noqa: BLE001 - file-op failures reach the REPL as errors
                if command.startswith("mkdir -p"):
                    self._replay_cache[("env", command, cwd)] = exc
                if not future.done():
                    future.set_exception(exc)
            else:
                if command.startswith("mkdir -p"):
                    self._replay_cache[("env", command, cwd)] = result
                if not future.done():
                    future.set_result(result)

    async def stop(self) -> None:
        self.stopped = True
        # Never stop the trial environment; verification still needs it.

    def _mark_ready(self, content: str) -> None:
        self._final = content

    def _capture_answer(self, namespace: dict[str, Any]) -> None:
        answer = namespace.get("answer")
        if isinstance(answer, dict) and answer.get("ready"):
            self._final = str(answer.get("content", ""))
        elif isinstance(answer, dict):
            self._final = None

    def _llm_query(self, prompt: str, model: str | None = None) -> str:
        del model
        key = ("llm", prompt)
        if key in self._replay_cache:
            return self._replay_cache[key]
        self.worker_prompts.append(prompt)
        result = self._sub_llm(prompt)
        self._replay_cache[key] = result
        return result

    def _llm_query_batched(self, prompts: list[str], model: str | None = None) -> list[str]:
        return [self._llm_query(prompt, model) for prompt in prompts]

    def _write_file(self, path: str, content: str) -> str:
        return self._namespace["write_file"](path, content)

    def _read_file(self, path: str) -> str:
        return self._namespace["read_file"](path)


def _pump_future(future: asyncio.Future[ExecResult]) -> ExecResult:
    """Resolve an in-flight environment call pump-friendly."""
    while not future.done():
        try:
            future.result()
        except asyncio.InvalidStateError:
            raise _SuspendExecution from None
    return future.result()


class _SuspendExecution(BaseException):
    """Internal control flow: the REPL yields so execute() can pump env calls."""


def _protocol_write_file(
    env_run: Callable[[str, str], ExecResult], path: str, content: str, working_dir: str
) -> str:
    quoted = shlex.quote(path)
    payload = content.replace("'", "'\\''")
    command = f"mkdir -p \"$(dirname {quoted})\" && printf '%s' '{payload}' > {quoted}"
    result = env_run(command, working_dir)
    if result.return_code != 0:
        raise RuntimeError(result.stderr or "write_file failed")
    return "ok"


def _protocol_read_file(
    env_run: Callable[[str, str], ExecResult], path: str, working_dir: str
) -> str:
    result = env_run(f"cat {shlex.quote(path)}", working_dir)
    if result.return_code != 0:
        raise RuntimeError(result.stderr or "read_file failed")
    return result.stdout or ""


class ScriptedRootClient:
    """Deterministic root LM for CPU protocol checks. Not a model."""

    def __init__(self, completions: Sequence[str]) -> None:
        self._completions = list(completions)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: Sequence[dict[str, str]]) -> RootCompletion:
        self.calls.append(list(messages))
        if not self._completions:
            raise RuntimeError("scripted root client exhausted")
        return RootCompletion(text=self._completions.pop(0), input_tokens=3, output_tokens=5)


class AuthorsRlmAgent(BaseAgent):
    """Depth-1 authors-RLM root agent. Import path: evallab.harbor_rlm:AuthorsRlmAgent."""

    SUPPORTS_ATIF = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        worker_model: str | None = None,
        max_iterations: int = 30,
        max_tokens: int = 8192,
        temperature: float | None = None,
        extra_instruction: str | None = None,
        extra_instruction_path: str | Path | None = None,
        working_dir: str = "/app",
        backend_factory: Callable[..., ReplBackend] | None = None,
        root_client: RootChatClient | None = None,
        sub_llm: Callable[[str], str] | None = None,
        timeout_sec: float | None = None,
        worker_src: str | Path | None = None,
        aiohttp_wheels: list[str | Path] | None = None,
        worker_proxy_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._worker_model = worker_model or model_name
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._extra_instruction = extra_instruction
        if extra_instruction_path is not None:
            path = Path(extra_instruction_path)
            extra = path.read_text(encoding="utf-8")
            self._extra_instruction = (
                extra
                if self._extra_instruction is None
                else f"{self._extra_instruction}\n\n{extra}"
            )
        self._working_dir = working_dir
        self._backend_factory = backend_factory
        self._root_client = root_client
        self._sub_llm = sub_llm
        self._timeout_sec = timeout_sec
        self._worker_src = Path(worker_src) if worker_src is not None else None
        self._aiohttp_wheels = (
            [Path(wheel) for wheel in aiohttp_wheels] if aiohttp_wheels is not None else None
        )
        self._worker_proxy_url = worker_proxy_url
        self._backend: ReplBackend | None = None
        self._root_calls = 0
        self._worker_calls = 0
        self._root_input_tokens = 0
        self._root_output_tokens = 0
        self._exhausted = False

    @staticmethod
    def name() -> str:
        return "authors-rlm"

    def version(self) -> str | None:
        return AGENT_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        del environment

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        backend = await self._open_backend(environment)
        self._backend = backend
        try:
            if self._timeout_sec is None:
                await self._loop(instruction, environment, context, backend)
            else:
                async with asyncio.timeout(self._timeout_sec):
                    await self._loop(instruction, environment, context, backend)
        finally:
            await backend.stop()
            self._publish_context(context)

    async def _open_backend(self, environment: BaseEnvironment) -> ReplBackend:
        factory = self._backend_factory
        if factory is None:
            return managed_backend_factory(
                environment,
                session_id=self.session_id or "authors-rlm",
                worker_src=self._worker_src,
                aiohttp_wheels=self._aiohttp_wheels,
            )
        try:
            return factory(environment)
        except TypeError:
            return factory(
                environment,
                sub_llm=self._require_sub_llm(),
                working_dir=self._working_dir,
            )

    def _require_sub_llm(self) -> Callable[[str], str]:
        if self._sub_llm is None:
            raise HarnessBackendContractError(
                "CPU protocol backend requires an injected sub_llm hook; "
                "production HAR-10 routing uses the caller-provided worker client"
            )
        return self._sub_llm

    def _worker_proxy(self) -> str:
        if self._backend_factory is not None:
            return self._worker_proxy_url or "hook://trial-worker"
        if not self._worker_proxy_url:
            raise HarnessBackendContractError(
                "HAR-10 ManagedReplBackend requires worker_proxy_url for sub-LLM "
                "transport; this packet does not open a model route"
            )
        return self._worker_proxy_url

    async def _loop(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
        backend: ReplBackend,
    ) -> None:
        context_payload = await self._load_workspace_context(environment)
        await backend.start(
            proxy_url=self._worker_proxy(),
            rollout_id=self.session_id or "authors-rlm",
            depth=1,
        )
        await backend.load_context(context_payload)
        history = build_rlm_system_prompt(
            RLM_SYSTEM_PROMPT,
            QueryMetadata(context_payload),
            root_prompt=instruction,
            extra_instruction=self._extra_instruction,
        )
        client = self._root_client
        if client is None:
            raise HarnessBackendContractError(
                "Root OpenAI-compatible client is not bound. Integration must inject "
                "the approved provider client; this packet does not open a model route."
            )
        for iteration in range(self._max_iterations):
            history.append(build_user_prompt(iteration, self._max_iterations))
            completion = client.complete(history)
            self._root_calls += 1
            if completion.input_tokens is not None:
                self._root_input_tokens += completion.input_tokens
            if completion.output_tokens is not None:
                self._root_output_tokens += completion.output_tokens
            blocks = find_repl_blocks(completion.text)
            outputs: list[str] = []
            final_answer: str | None = None
            for code in blocks:
                result = await backend.execute(code)
                self._worker_calls = _worker_call_count(backend, self._worker_calls)
                outputs.append(_format_exec(result))
                candidate = getattr(result, "final_answer", None)
                if candidate is not None:
                    final_answer = candidate
            history.append({"role": "assistant", "content": completion.text})
            if outputs:
                history.append({"role": "user", "content": "\n\n".join(outputs)})
            self._write_native_log(history)
            self._publish_context(context)
            if final_answer is not None:
                return
        self._exhausted = True
        self._write_native_log(history)
        self._publish_context(context)

    async def _load_workspace_context(self, environment: BaseEnvironment) -> str:
        listing = await environment.exec(
            command="find . -maxdepth 4 -type f | head -200",
            cwd=self._working_dir,
            timeout_sec=15,
        )
        return listing.stdout or "(empty workspace)"

    def _write_native_log(self, history: list[dict[str, str]]) -> None:
        directory = self.logs_dir / "rlm"
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_format": "authors-rlm-root-messages",
            "schema_version": None,
            "prompt_source_sha256": PROMPT_SOURCE_SHA256,
            "parsing_source_sha256": PARSING_SOURCE_SHA256,
            "messages": history,
            "root_calls": self._root_calls,
            "worker_calls": self._worker_calls,
            "exhausted_iterations": self._exhausted,
        }
        (directory / "root-messages.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _publish_context(self, context: AgentContext) -> None:
        context.n_input_tokens = self._root_input_tokens or None
        context.n_output_tokens = self._root_output_tokens or None
        context.metadata = {
            "agent": self.name(),
            "agent_version": self.version(),
            "root_model": self.model_name,
            "worker_model": self._worker_model,
            "root_calls": self._root_calls,
            "worker_calls": self._worker_calls,
            "root_input_tokens": self._root_input_tokens or None,
            "root_output_tokens": self._root_output_tokens or None,
            "worker_usage": None,
            "atif": False,
            "backend": MANAGED_BACKEND_IMPORT,
            "exhausted_iterations": self._exhausted,
        }


def _worker_call_count(backend: ReplBackend, fallback: int) -> int:
    prompts = getattr(backend, "worker_prompts", None)
    if isinstance(prompts, list):
        return len(prompts)
    forwarded = getattr(backend, "forwarded_subcalls", None)
    if isinstance(forwarded, int):
        return forwarded
    return fallback


def _format_exec(result: Any) -> str:
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n".join(parts) if parts else "(no output)"
