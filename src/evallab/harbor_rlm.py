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
import os
import re
import shlex
import urllib.error
import urllib.request
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

AGENT_VERSION = "0.1.1-har12"
MINI_SWE_AGENT_VERSION = "2.4.6"
MINI_SWE_AGENT_IMPORT = "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
AUTHORS_RLM_IMPORT = "evallab.harbor_rlm:AuthorsRlmAgent"
MANAGED_BACKEND_IMPORT = "evallab.rlm_runtime:ManagedReplBackend"
HAR10_BACKEND_PR = 392
HAR10_BACKEND_GIT_REF = "00cf4af7496894ac87c64f16117c52666108afc4"
ADMITTED_SECRET_ENV_NAMES = frozenset({"DEEPSEEK_API_KEY", "MSWEA_API_KEY"})
DEFAULT_SECRET_SOURCE = "env:DEEPSEEK_API_KEY"
DEFAULT_API_BASE = "https://api.deepseek.com"
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


class OpenAICompatibleRootClient:
    """Host-side OpenAI-compatible client from serializable Harbor kwargs."""

    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str,
        timeout_sec: float,
        max_tokens: int = 8192,
        temperature: float | None = None,
        secret_source: str = DEFAULT_SECRET_SOURCE,
    ) -> None:
        self.model = model
        self.api_base = api_base.rstrip("/")
        self._api_key = api_key
        self.timeout_sec = float(timeout_sec)
        self.max_tokens = int(max_tokens)
        self.temperature = temperature
        self.secret_source = secret_source

    def __repr__(self) -> str:
        return (
            "OpenAICompatibleRootClient("
            f"model={self.model!r}, api_base={self.api_base!r}, "
            f"secret_source={self.secret_source!r})"
        )

    def complete(self, messages: Sequence[dict[str, str]]) -> RootCompletion:
        url = f"{self.api_base}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "max_tokens": self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                body = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise TimeoutError(f"root client timed out after {self.timeout_sec:g}s") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise TimeoutError(f"root client timed out after {self.timeout_sec:g}s") from exc
            raise
        choices = body.get("choices") or []
        message = choices[0].get("message") if choices else {}
        text = (message or {}).get("content") or ""
        usage = body.get("usage")
        input_tokens = None
        output_tokens = None
        if isinstance(usage, dict):
            if "prompt_tokens" in usage and usage["prompt_tokens"] is not None:
                input_tokens = int(usage["prompt_tokens"])
            if "completion_tokens" in usage and usage["completion_tokens"] is not None:
                output_tokens = int(usage["completion_tokens"])
        return RootCompletion(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    def complete_text(self, prompt: str) -> str:
        return self.complete(({"role": "user", "content": prompt},)).text


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


def parse_secret_source(secret_source: str) -> str:
    if not secret_source.startswith("env:"):
        raise HarnessBackendContractError(
            f"secret_source must be an env: identifier, not {secret_source!r}"
        )
    name = secret_source.split(":", 1)[1]
    if name not in ADMITTED_SECRET_ENV_NAMES:
        raise HarnessBackendContractError(
            f"secret_source {secret_source!r} is not an admitted host identifier"
        )
    return name


def serializable_runtime_config() -> dict[str, Any]:
    """Harbor-kwargs Integration can serialize. No Python callables."""
    return {
        "agent": "authors-rlm",
        "import_path": AUTHORS_RLM_IMPORT,
        "agent_version": AGENT_VERSION,
        "baseline_import": MINI_SWE_AGENT_IMPORT,
        "baseline_version": MINI_SWE_AGENT_VERSION,
        "backend_import": MANAGED_BACKEND_IMPORT,
        "backend_pr": HAR10_BACKEND_PR,
        "backend_git_ref": HAR10_BACKEND_GIT_REF,
        "kwargs": {
            "worker_src": "<pinned released worker.py from HAR-10>",
            "worker_model": "<defaults to --model>",
            "worker_proxy_url": "<optional host-reachable worker URL>",
            "secret_source": DEFAULT_SECRET_SOURCE,
            "api_base": DEFAULT_API_BASE,
            "extra_instruction_path": "<optional GEPA candidate>",
            "timeout_sec": 120,
            "max_tokens": 8192,
        },
        "forbidden_kwargs": (
            "aiohttp_wheels",
            "root_client",
            "sub_llm",
            "backend_factory",
        ),
    }


def accounting_contract() -> dict[str, Any]:
    """Source-native file/field contract for HAR-13. Unknowns stay null."""
    return {
        "file": "logs_dir/rlm/root-messages.json",
        "source_format": "authors-rlm-root-messages",
        "fields": {
            "schema_version": None,
            "prompt_source_sha256": PROMPT_SOURCE_SHA256,
            "parsing_source_sha256": PARSING_SOURCE_SHA256,
            "root_model": "string or null",
            "worker_model": "string or null",
            "root_model_revision": None,
            "worker_model_revision": None,
            "root_calls": "int",
            "worker_calls": "int",
            "root_input_tokens": "int or null",
            "root_output_tokens": "int or null",
            "worker_input_tokens": None,
            "worker_output_tokens": None,
            "worker_usage": None,
            "cost_usd": None,
            "atif": False,
            "agent_result_totals_include_workers": False,
            "exhausted_iterations": "bool",
            "secret_source": "env identifier only",
            "messages": "root conversation, no provider secrets",
        },
        "lego_capture": (
            "not emitted unless tokenizer ids/logprobs/mask are actually observed; "
            "this agent does not invent them from text"
        ),
    }


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
    work_root: str = "/opt/rlm-managed",
) -> ReplBackend:
    """Construct HAR-10's published backend without obsolete aiohttp_wheels."""
    try:
        from evallab.rlm_runtime import ManagedReplBackend
    except ImportError as exc:
        raise HarnessBackendContractError(
            "HAR-10 ManagedReplBackend is not importable. Consume "
            f"{MANAGED_BACKEND_IMPORT} from PR #{HAR10_BACKEND_PR} @"
            f"{HAR10_BACKEND_GIT_REF} (corrected constructor: no aiohttp_wheels). "
            f"Import error: {exc}"
        ) from exc
    _require_environment_backend(ManagedReplBackend)
    parameters = inspect.signature(ManagedReplBackend.__init__).parameters
    if (
        "aiohttp_wheels" in parameters
        and parameters["aiohttp_wheels"].default is inspect.Parameter.empty
    ):
        raise HarnessBackendContractError(
            "HAR-10 constructor still requires obsolete aiohttp_wheels; "
            f"need the corrected pin after {HAR10_BACKEND_GIT_REF}. "
            "This agent will not pass a dummy empty list."
        )
    if worker_src is None:
        raise HarnessBackendContractError(
            "HAR-10 ManagedReplBackend requires pinned worker_src from the published backend packet"
        )
    kwargs: dict[str, Any] = {
        "worker_src": Path(worker_src),
        "session_id": session_id,
        "work_root": work_root,
    }
    accepted = {
        name
        for name, param in parameters.items()
        if name != "self" and param.kind is not inspect.Parameter.VAR_KEYWORD
    }
    return ManagedReplBackend(
        environment,
        **{key: value for key, value in kwargs.items() if key in accepted},
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
        max_iterations: int | str = 30,
        max_tokens: int | str = 8192,
        temperature: float | str | None = None,
        extra_instruction: str | None = None,
        extra_instruction_path: str | Path | None = None,
        working_dir: str = "/app",
        backend_factory: Callable[..., ReplBackend] | None = None,
        root_client: RootChatClient | None = None,
        sub_llm: Callable[[str], str] | None = None,
        timeout_sec: float | str | None = None,
        worker_src: str | Path | None = None,
        worker_proxy_url: str | None = None,
        secret_source: str = DEFAULT_SECRET_SOURCE,
        api_base: str = DEFAULT_API_BASE,
        **kwargs: Any,
    ) -> None:
        if "aiohttp_wheels" in kwargs:
            raise HarnessBackendContractError(
                "obsolete aiohttp_wheels is not accepted; HAR-10 corrected "
                "constructor has no dummy empty-list shim"
            )
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._worker_model = worker_model or model_name
        self._max_iterations = int(max_iterations)
        self._max_tokens = int(max_tokens)
        self._temperature = None if temperature is None else float(temperature)
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
        self._injected_root_client = root_client
        self._root_client = root_client
        self._sub_llm = sub_llm
        self._timeout_sec = None if timeout_sec is None else float(timeout_sec)
        self._worker_src = Path(worker_src) if worker_src is not None else None
        self._worker_proxy_url = worker_proxy_url
        self._secret_source = secret_source
        self._api_base = api_base
        self._backend: ReplBackend | None = None
        self._root_calls = 0
        self._worker_calls = 0
        self._root_input_tokens: int | None = None
        self._root_output_tokens: int | None = None
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
        if self._sub_llm is not None:
            return self._sub_llm
        worker = self._bind_production_root_client(model=self._worker_model)
        return worker.complete_text

    def _worker_proxy(self) -> str:
        if self._backend_factory is not None:
            return self._worker_proxy_url or "hook://trial-worker"
        if self._worker_proxy_url:
            return self._worker_proxy_url
        raise HarnessBackendContractError(
            "serialized config requires worker_proxy_url for HAR-10 sub-LLM transport"
        )

    def _secret_value(self, name: str) -> str | None:
        getter = getattr(self, "_get_env", None)
        if callable(getter):
            value = getter(name)
            if value:
                return value
        extra = getattr(self, "_extra_env", None) or getattr(self, "extra_env", None) or {}
        if isinstance(extra, dict) and name in extra:
            return str(extra[name])
        return os.environ.get(name)

    def _bind_production_root_client(
        self, *, model: str | None = None
    ) -> OpenAICompatibleRootClient:
        chosen = model or self.model_name
        if not chosen:
            raise HarnessBackendContractError(
                "serialized config requires model_name for the OpenAI-compatible root client"
            )
        env_name = parse_secret_source(self._secret_source)
        api_key = self._secret_value(env_name)
        if not api_key:
            raise HarnessBackendContractError(
                f"secret source {self._secret_source} is unset on the host"
            )
        timeout = self._timeout_sec if self._timeout_sec is not None else 120.0
        return OpenAICompatibleRootClient(
            model=chosen,
            api_base=self._api_base,
            api_key=api_key,
            timeout_sec=timeout,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            secret_source=self._secret_source,
        )

    def _resolve_root_client(self) -> RootChatClient:
        if self._injected_root_client is not None:
            return self._injected_root_client
        if self._root_client is None:
            self._root_client = self._bind_production_root_client()
        return self._root_client

    async def _complete_root(
        self, client: RootChatClient, messages: Sequence[dict[str, str]]
    ) -> RootCompletion:
        acomplete = getattr(client, "acomplete", None)
        if callable(acomplete):
            result = await acomplete(messages)
            return result
        return await asyncio.to_thread(client.complete, list(messages))

    def _record_root_usage(self, completion: RootCompletion) -> None:
        if completion.input_tokens is not None:
            current = 0 if self._root_input_tokens is None else self._root_input_tokens
            self._root_input_tokens = current + completion.input_tokens
        if completion.output_tokens is not None:
            current = 0 if self._root_output_tokens is None else self._root_output_tokens
            self._root_output_tokens = current + completion.output_tokens

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
        client = self._resolve_root_client()
        for iteration in range(self._max_iterations):
            history.append(build_user_prompt(iteration, self._max_iterations))
            completion = await self._complete_root(client, history)
            self._root_calls += 1
            self._record_root_usage(completion)
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
            "root_model": self.model_name,
            "worker_model": self._worker_model,
            "root_model_revision": None,
            "worker_model_revision": None,
            "messages": history,
            "root_calls": self._root_calls,
            "worker_calls": self._worker_calls,
            "root_input_tokens": self._root_input_tokens,
            "root_output_tokens": self._root_output_tokens,
            "worker_input_tokens": None,
            "worker_output_tokens": None,
            "worker_usage": None,
            "cost_usd": None,
            "atif": False,
            "agent_result_totals_include_workers": False,
            "exhausted_iterations": self._exhausted,
            "secret_source": self._secret_source,
            "backend": MANAGED_BACKEND_IMPORT,
            "backend_git_ref": HAR10_BACKEND_GIT_REF,
        }
        (directory / "root-messages.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _publish_context(self, context: AgentContext) -> None:
        context.n_input_tokens = self._root_input_tokens
        context.n_output_tokens = self._root_output_tokens
        context.metadata = {
            "agent": self.name(),
            "agent_version": self.version(),
            "root_model": self.model_name,
            "worker_model": self._worker_model,
            "root_model_revision": None,
            "worker_model_revision": None,
            "root_calls": self._root_calls,
            "worker_calls": self._worker_calls,
            "root_input_tokens": self._root_input_tokens,
            "root_output_tokens": self._root_output_tokens,
            "worker_input_tokens": None,
            "worker_output_tokens": None,
            "worker_usage": None,
            "cost_usd": None,
            "atif": False,
            "agent_result_totals_include_workers": False,
            "backend": MANAGED_BACKEND_IMPORT,
            "backend_git_ref": HAR10_BACKEND_GIT_REF,
            "exhausted_iterations": self._exhausted,
            "secret_source": self._secret_source,
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
