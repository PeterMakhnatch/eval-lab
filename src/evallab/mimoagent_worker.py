"""Pinned Xiaomi controller subprocess; its SDK must not share Harbor's interpreter.

Tool transport and append-only evidence are adapted. DefaultAgent, its native
tools, prompts, parallelism and step limits are unchanged. Two adapters wrap
the pinned SDK (never inside site-packages):

* Known-cold OpenAI failures retry within a ~300s recovery window. The initial
  ordinary query retains the SDK's 3600s read timeout; connect/pool/write are
  bounded. An explicit transient failure starts the recovery clock, and
  retry preheaders/backoff are bounded by its remainder. HTTP200 restores
  native body-read timeout and is never retried, even if empty/malformed.
  Non-streaming generation and a hung cold endpoint are indistinguishable
  before the first headers: this is not a 300s cap on ordinary generation.
  Permanent 4xx/auth errors and proxy budget refusals fail fast; failed
  attempts reuse the exact prefix. Dispatch readiness is handled separately.
* Terminal ModelQueryError/InfraError diagnostics become structured finished
  metadata, never a synthetic user turn or a traceback in native history. A
  400 context-length refusal instead ends as ContextExhausted with stop
  reason context_exhausted (no truncation or summary turn), so Harbor grades
  the final state.
  Agent-tool log_file metadata uses a logical childlog://<stem> identity;
  internal on-disk log locations remain untouched.

The script runs in tools/mimoagent-harbor/.venv/bin/python; SDK imports stay
inside main so Harbor can import the pin and sampling constants separately.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any

NATIVE_REVISION = "467f0a19016f0ac4d63b8d17a1f0da9ba07f232c"
SWE_SHA256 = "a03457e611f7f531105ba23e8c9f9d4360e91341e305470c90845860f3315985"
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
COLD_START_BUDGET_S = 300.0
WRAPPER_ADDITIONS = {
    "cold_start_retry_window_seconds": COLD_START_BUDGET_S,
    "infrastructure_errors": "stop_metadata",
    "agent_log_paths": "logical_childlog",
    "fineenv_issue_header": "strip_one_leading_copy",
}


class ColdStartDeadlineExceeded(Exception):
    """A failed transport exhausted its retry window; no answer was received."""

    def __init__(self, *, attempts: int, elapsed_seconds: float):
        super().__init__("native model transport readiness window exhausted")
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds


def _retry_delay(attempts: int) -> float:
    # Keep the pinned 4/4/4/8 backoff initially, then cap at 20s so a three-
    # minute startup does not sit through an unnecessary minute-long sleep.
    return min(20.0, max(4.0, 2.0 ** min(attempts - 1, 5)))


def _root_error(error: BaseException) -> BaseException:
    """Follow structured causes, never inspect arbitrary exception/user text."""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        cause = error.__cause__
        if cause is None:
            break
        error = cause
    return error


def _error_status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _error_budget_reason(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    return proxy_budget_reason(_error_status(error), getattr(response, "text", None))


def _safe_budget_reason(reason: str | None) -> str | None:
    """Export the known budget dimension, not the provider's arbitrary body."""
    if reason is None:
        return None
    marker = "ceiling:"
    if marker in reason:
        suffix = reason.partition(marker)[2].split()
        dimension = suffix[0] if suffix else ""
        if dimension and all(char.isalnum() or char == "_" for char in dimension):
            return marker + dimension
    return "trial budget exhausted"


#: Terminal status when the served context fills. The rollout ends and the
#: final sandbox state is graded, mirroring Xiaomi's RL runner (only
#: ``InfraError`` skips grading; every other status is graded with
#: ``termination_kind=truncated``). Distinct from ``ModelQueryError`` and
#: ``InfraError`` so Harbor runs the verifier and records
#: ``context_exhausted``, never ``infra_error``.
CONTEXT_EXHAUSTED_STATUS = "ContextExhausted"
CONTEXT_EXHAUSTED_STOP = "context_exhausted"

#: Provider-side wordings of an HTTP 400 context-length refusal. SGLang's two
#: exact messages come from ``tokenizer_manager._validate_one_request``; the
#: rest cover the equivalent vLLM/OpenAI phrasings and OpenAI's
#: ``string_above_max_length`` code. Matched only against the provider's
#: error body, never agent/task content, so other 400s (schema, auth,
#: params) stay ``ModelQueryError``.
_CONTEXT_LENGTH_PATTERNS = (
    re.compile(r"context[_\s-]?length", re.IGNORECASE),
    re.compile(r"context[_\s-]?window", re.IGNORECASE),
    re.compile(r"max(?:imum)?[_\s-]?context", re.IGNORECASE),
    re.compile(r"max_model_len"),
    re.compile(r"requested token count exceeds", re.IGNORECASE),
    re.compile(r"string_above_max_length"),
    re.compile(r"tokens?.{0,40}exceed|exceed.{0,40}tokens?", re.IGNORECASE),
    re.compile(r"prompt.{0,40}too long|too long.{0,40}prompt", re.IGNORECASE),
    re.compile(r"input.{0,40}too large|too large.{0,40}input", re.IGNORECASE),
)


def _provider_error_text(error: BaseException) -> str:
    """Provider-side error body for a failed model call, never agent content.

    Only the HTTP response body (when present), the SDK's structured body,
    and its message are read. The worker records no part of this text —
    details carry fixed vocabulary plus status/token integers — it only
    decides whether the served context filled.
    """
    parts: list[str] = []
    response = getattr(error, "response", None)
    for candidate in (getattr(response, "text", None), getattr(error, "body", None)):
        if isinstance(candidate, str) and candidate:
            parts.append(candidate)
        elif isinstance(candidate, dict):
            with suppress(Exception):
                parts.append(json.dumps(candidate))
    message = getattr(error, "message", None)
    if isinstance(message, str) and message and message not in parts:
        parts.append(message)
    if not parts:
        parts.append(str(error))
    return "\n".join(parts)


def _is_context_length_error(
    error: BaseException,
    *,
    last_prompt_tokens: int | None = None,
    served_context_tokens: int | None = None,
) -> bool:
    """Whether a failed model call means the served context filled.

    Only an HTTP 400 whose provider body names the context limit, or a 400
    when the previous successful call already sat at/above the served limit
    (history only grows, so the next prefix necessarily overflows). Budget
    refusals and every other failure — other 400s, 5xx, transport errors —
    are not context exhaustion.
    """
    if _error_status(error) != 400:
        return False
    if _error_budget_reason(error) is not None:
        return False
    text = _provider_error_text(error)
    if any(pattern.search(text) is not None for pattern in _CONTEXT_LENGTH_PATTERNS):
        return True
    return (
        isinstance(last_prompt_tokens, int)
        and isinstance(served_context_tokens, int)
        and served_context_tokens > 0
        and last_prompt_tokens >= served_context_tokens
    )


class SandboxRpc:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self._lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._sequence = 0
        threading.Thread(target=self._receive, daemon=True).start()

    def emit(self, event: dict) -> None:
        with self._lock:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _receive(self) -> None:
        try:
            for line in sys.stdin:
                response = json.loads(line)
                with self._lock:
                    future = self._pending.pop(response["id"])
                if "error" in response:
                    from mimoagent.environments import (  # ty: ignore[unresolved-import]
                        TransportError,
                    )

                    future.set_exception(TransportError(response["error"]))
                else:
                    future.set_result(response["result"])
        finally:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                future.set_exception(ConnectionError("Harbor sandbox transport closed"))

    def _request(self, method: str, **kwargs) -> Any:
        future = Future()
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            self._pending[request_id] = future
        self.emit({"event": "tool", "id": request_id, "method": method, **kwargs})
        return future.result()

    def execute(self, command: str, cwd: str = "", timeout: int | None = None) -> dict:
        result = self._request("exec", command=command, cwd=cwd or self.cwd, timeout=timeout or 300)
        # Observe-only: report the actual transport returncode to an active
        # traced-tool recorder; the returned result is never altered.
        _record_exec_result(result)
        return result

    def execute_detached(
        self, command: str, cwd: str = "", timeout: int | None = None, **_kwargs
    ) -> dict:
        return self.execute(command, cwd=cwd, timeout=timeout)

    def copy_to(
        self, src_path: str, dest_path: str, *, timeout: int = 300, max_retries: int = 10
    ) -> None:
        # Native write creates this controller-side tempfile. No task tool may
        # access the host filesystem; only the trusted transport uploads it.
        self._request("upload", source=src_path, target=dest_path, timeout=timeout)

    def get_template_vars(self) -> dict:
        return {"cwd": self.cwd, "timeout": 300}


_EXEC_RECORDERS = threading.local()


def _push_exec_recorder() -> list:
    """Begin collecting observed exec returncodes in this thread; nestable."""
    recorder: list = []
    stack = getattr(_EXEC_RECORDERS, "stack", None)
    if stack is None:
        stack = []
        _EXEC_RECORDERS.stack = stack
    stack.append(recorder)
    return recorder


def _pop_exec_recorder(recorder: list) -> None:
    """End this thread's collection, restoring any enclosing recorder."""
    try:
        stack = getattr(_EXEC_RECORDERS, "stack", None)
        if stack and stack[-1] is recorder:
            stack.pop()
    except Exception:
        pass


def _record_exec_result(result: Any) -> None:
    """Append an actually observed exec returncode to the innermost recorder.

    Records exactly what the sandbox transport returned (including -1 for a
    client-side timeout); missing/non-integer codes stay unknown (None).
    Never raises; never alters the result.
    """
    try:
        stack = getattr(_EXEC_RECORDERS, "stack", None)
        if not stack:
            return
        code = result.get("returncode") if isinstance(result, dict) else None
        stack[-1].append(code if isinstance(code, int) else None)
    except Exception:
        pass


def _tool_exit_metadata(tool_name: Any, codes: list, output: Any) -> dict:
    """Tracing-only exit telemetry; the original tool output is untouched.

    Process-backed tools report every exec returncode honestly observed during
    the span (multiple subcommands included) with the most recent as primary;
    no exec observed means unknown (None), never an invented 0. The agent tool
    has no process exit code: explicit null code plus the real
    metadata.exit_status when the tool reported one.
    """
    status = None
    if isinstance(output, dict):
        inner = output.get("metadata")
        if isinstance(inner, dict):
            candidate = inner.get("exit_status")
            status = candidate if isinstance(candidate, str) else None
    if tool_name == "agent":
        metadata: dict = {"exit_code": None}
        if status is not None:
            metadata["exit_status"] = status
        return metadata
    metadata = {"exit_codes": list(codes), "exit_code": codes[-1] if codes else None}
    if status is not None:
        metadata["exit_status"] = status
    return metadata


def proxy_budget_reason(status_code: Any, body_text: Any) -> str | None:
    """Preserve a proxy budget refusal, never mistake provider throttling for it."""
    if status_code != 429 or not isinstance(body_text, str):
        return None
    try:
        payload = json.loads(body_text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            reasons = (error.get("reason"), error.get("message"), payload.get("reason"))
        else:
            reasons = (payload.get("reason"), error, payload.get("message"))
    else:
        reasons = (body_text.strip(),)
    for reason in reasons:
        if isinstance(reason, str) and (
            "trial budget exhausted" in reason or reason.startswith("ceiling:")
        ):
            return reason
    return None


def _load_trace_runtime(tracing_config: dict, initial: dict):
    """Initialize standalone Laminar tracing; fail-open to None on any error.

    The core module is loaded by explicit sibling file path so the trusted
    ``-I`` worker keeps its interpreter isolation (no sys.path broadening, no
    package import). A missing module, missing key, or any initialization
    failure leaves the worker exactly untraced; callers check for None.
    """
    if not tracing_config:
        return None
    try:
        import importlib.util

        tracing_path = Path(__file__).with_name("laminar_tracing.py")
        spec = importlib.util.spec_from_file_location(
            "evallab_laminar_tracing_standalone", tracing_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host_paths = tuple(
            path
            for path in (
                initial.get("native_logs_dir"),
                initial.get("global_config_dir"),
                initial.get("config_path"),
            )
            if isinstance(path, str) and path
        )
        # Both host credentials that reach this worker are redaction needles
        # before the encoded sink: the proxy capability and the LMNR key.
        secrets = tuple(
            secret
            for secret in (
                initial.get("capability"),
                os.environ.get("LMNR_PROJECT_API_KEY"),
            )
            if isinstance(secret, str) and secret
        )
        bearer = tracing_config.get("bearer_pattern")
        runtime = module.initialize_tracing(
            automatic_openai=True,
            secrets=secrets,
            host_paths=host_paths,
            native_sink=tracing_config.get("native_sink"),
            bearer_pattern=bearer if isinstance(bearer, str) else None,
        )
        return runtime if runtime is not None and runtime.enabled else None
    except Exception:
        return None


def main() -> None:
    initial = json.loads(sys.stdin.readline())
    tracing_config = initial.get("tracing")
    if not isinstance(tracing_config, dict):
        tracing_config = {}
    # Actual automatic OpenAI instrumentation is initialized here, before the
    # model is constructed or called. Fail-open: None means run untraced.
    trace_runtime = _load_trace_runtime(tracing_config, initial)
    trace_session = tracing_config.get("session_id")
    if not isinstance(trace_session, str):
        trace_session = None
    trace_parent = tracing_config.get("parent_context")
    if not isinstance(trace_parent, str):
        trace_parent = None
    trace_metadata = tracing_config.get("metadata")
    if not isinstance(trace_metadata, dict):
        trace_metadata = {}
    # Prevent mimoagent's global .env loader from importing an unrelated host
    # configuration. This directory is created and owned by the Harbor agent.
    os.environ["MIMOAGENT_GLOBAL_CONFIG_DIR"] = initial["global_config_dir"]
    import httpx2  # ty: ignore[unresolved-import]
    import openai  # ty: ignore[unresolved-import]
    import yaml
    from mimoagent.agents.base import (  # ty: ignore[unresolved-import]
        BaseAgent,
        InfraError,
        ModelQueryError,
        NonTerminatingException,
        TerminatingException,
    )
    from mimoagent.agents.default import DefaultAgent  # ty: ignore[unresolved-import]
    from mimoagent.models.openai_chat import OpenAIChatModel  # ty: ignore[unresolved-import]
    from mimoagent.models.utils.content import content_text  # ty: ignore[unresolved-import]
    from mimoagent.run.utils.save import save_traj  # ty: ignore[unresolved-import]
    from mimoagent.utils.log import (  # ty: ignore[unresolved-import]
        AgentLogContext,
        use_log_context,
    )

    distribution = importlib.metadata.distribution("mimoagent")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct_url.get("vcs_info", {}).get("commit_id") != NATIVE_REVISION:
        raise RuntimeError("mimoagent runtime does not match the pinned Xiaomi commit")
    config_path = Path(initial["config_path"])
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != SWE_SHA256:
        raise RuntimeError("native swe.yaml changed; refusing a non-parity harness")
    config = yaml.safe_load(config_bytes)
    rpc = SandboxRpc(initial["cwd"])
    logs = AgentLogContext.create(Path(initial["native_logs_dir"]))
    names_started: set[str] = set()
    evidence_lock = threading.Lock()
    local = threading.local()
    query_states: dict[str, dict] = {}
    infrastructure_stops: dict[str, dict] = {}
    context_stops: dict[str, dict] = {}
    # Served context window in tokens, when the host adapter knows it. Only
    # corroborates an HTTP 400 (the previous prefix already filled the
    # window, so the next one necessarily overflows); never ends a rollout
    # on its own, so observing never changes what the model sees.
    served_context_tokens = initial.get("served_context_tokens")
    if (
        isinstance(served_context_tokens, bool)
        or not isinstance(served_context_tokens, int)
        or served_context_tokens <= 0
    ):
        served_context_tokens = None

    # Explicit tracing parentage for reused pool threads: agent-run span
    # contexts by live agent identity, plus the active tool span in each
    # executing thread so a child run nests under its invoking agent tool.
    agent_span_contexts: dict[int, str] = {}
    agent_span_lock = threading.Lock()
    tool_span_state = threading.local()

    def _quietly(call, *args, **kwargs):
        """Run a telemetry call fail-open; tracing never changes the trial."""
        try:
            return call(*args, **kwargs)
        except Exception:
            return None

    def name_of(agent=None, messages=None) -> str:
        with logs._lock:
            for name, candidate in logs.trajectories.items():
                if candidate is agent or (messages is not None and candidate.messages is messages):
                    return name
        raise RuntimeError("native agent was not registered for trajectory capture")

    def observer_failure(category: str, error: Exception) -> None:
        with suppress(Exception):
            sys.stderr.write(f"native observer failure: {category} {type(error).__name__}\n")
            sys.stderr.flush()

    original_append_msg_to_file = BaseAgent._append_msg_to_file

    def safe_append_msg_to_file(agent, message: dict) -> None:
        # Native add_message has already appended the original history. Only
        # its ancillary formatted file dump is fail-open, not initialization.
        try:
            original_append_msg_to_file(agent, message)
        except Exception as error:
            observer_failure("message_file", error)

    BaseAgent._append_msg_to_file = safe_append_msg_to_file

    def notify_step_complete(agent, previous_step: int) -> None:
        # Failed queries/step-limit checks have no new assistant boundary.
        try:
            if agent._steps_taken <= previous_step:
                return
            name = name_of(agent=agent)
            with evidence_lock:
                rpc.emit(
                    {
                        "event": "step_complete",
                        "name": name,
                        "native_step": agent._steps_taken,
                        "message_count": len(agent.messages),
                    }
                )
        except Exception as error:
            observer_failure("step_complete", error)

    original_add_message = BaseAgent.add_message

    def observe_message(agent, role: str, content: str, **kwargs) -> None:
        original_add_message(agent, role, content, **kwargs)
        try:
            name = name_of(agent=agent)
            with evidence_lock:
                if name not in names_started:
                    rpc.emit(
                        {"event": "agent_start", "name": name, **agent.get_model_query_kwargs()}
                    )
                    names_started.add(name)
                rpc.emit({"event": "message", "name": name, "message": agent.messages[-1]})
        except Exception as error:
            observer_failure("message_capture", error)

    BaseAgent.add_message = observe_message

    def run_without_infrastructure_turn(agent, task: str | dict, **kwargs) -> tuple[str, str]:
        # The pinned BaseAgent.run loop, with only its infrastructure terminal
        # branch separated from conversation. This seam covers lazy children
        # without classifying or dropping arbitrary user/task content.
        first_turn = not agent.messages
        task_text = content_text(task.get("content")) if isinstance(task, dict) else task
        agent.extra_template_vars |= {"task": task_text, **kwargs}
        if first_turn:
            agent.add_message("system", agent.render_template(agent.config.system_template))
        if isinstance(task, dict):
            agent.add_message(**{"role": "user", **task})
        elif first_turn:
            agent.add_message("user", agent.render_template(agent.config.instance_template))
        else:
            agent.add_message("user", task)
        while True:
            previous_step = agent._steps_taken
            try:
                if agent.step() is None:
                    notify_step_complete(agent, previous_step)
                    return agent.IDLE_STATUS, agent._last_assistant_text()
                agent.after_step()
                notify_step_complete(agent, previous_step)
            except NonTerminatingException as error:
                agent.add_message("user", str(error))
                agent.after_step()
                notify_step_complete(agent, previous_step)
            except (ModelQueryError, InfraError) as error:
                name = name_of(agent=agent)
                root = _root_error(error)
                state = query_states.get(name, {}) if isinstance(error, ModelQueryError) else {}
                if isinstance(error, ModelQueryError) and _is_context_length_error(
                    root,
                    last_prompt_tokens=state.get("last_prompt_tokens"),
                    served_context_tokens=served_context_tokens,
                ):
                    # The served context filled: end the rollout like every
                    # other terminal model failure, but record a context stop
                    # (never infra_error) so Harbor grades the final state.
                    # History is untouched: no truncation, no summary turn.
                    details = {
                        "error_type": type(error).__name__,
                        "root_error_type": type(root).__name__,
                        "last_response_status": state.get(
                            "last_response_status", _error_status(root)
                        ),
                        "last_prompt_tokens": state.get("last_prompt_tokens"),
                        "served_context_tokens": served_context_tokens,
                    }
                    with evidence_lock:
                        context_stops[name] = details
                    # A child ContextExhausted becomes a normal tool result
                    # (only InfraError propagates); fixed vocabulary, never
                    # provider bytes or traceback.
                    summary = f"native context exhausted: {details['error_type']}"
                    notify_step_complete(agent, previous_step)
                    return CONTEXT_EXHAUSTED_STATUS, summary
                details = {
                    "error_type": type(error).__name__,
                    "root_error_type": type(root).__name__,
                    "last_response_status": state.get("last_response_status", _error_status(root)),
                    "retry_window_seconds": COLD_START_BUDGET_S,
                    "retry_rounds": max(0, state.get("attempts", 1) - 1),
                    "retry_window_exhausted": isinstance(
                        error.__cause__, ColdStartDeadlineExceeded
                    ),
                    "budget_refusal": _safe_budget_reason(_error_budget_reason(root)),
                }
                with evidence_lock:
                    infrastructure_stops[name] = details
                # AgentTool returns a child ModelQueryError as a tool result;
                # it must receive a safe summary rather than SDK traceback.
                summary = f"native infrastructure failure: {details['error_type']}"
                if details["budget_refusal"]:
                    summary += f"; {details['budget_refusal']}"
                notify_step_complete(agent, previous_step)
                return type(error).__name__, summary
            except TerminatingException as error:
                agent.add_message("user", str(error))
                notify_step_complete(agent, previous_step)
                return type(error).__name__, str(error)

    BaseAgent.run = run_without_infrastructure_turn
    if trace_runtime is not None:

        def traced_agent_run(agent, task: str | dict, **kwargs) -> tuple[str, str]:
            # Observe actual execution only. The infrastructure-safe loop runs
            # exactly once below with unchanged requests, tools, and stops.
            try:
                agent_name = name_of(agent=agent)
            except Exception:
                agent_name = None
            parent_context = getattr(tool_span_state, "current", None)
            if not isinstance(parent_context, str):
                # The root agent joins the host trial span. A child agent always
                # runs inside its invoking agent-tool span, bound above.
                parent_context = trace_parent
            span = _quietly(
                trace_runtime.start_span,
                f"agent.run {agent_name or 'unknown'}",
                span_type="DEFAULT",
                parent_context=parent_context,
                session_id=trace_session,
                metadata={**trace_metadata, "agent": agent_name or "unknown"},
                input=task,
                tags=["mimoagent-native"],
            )
            if span is None:
                return run_without_infrastructure_turn(agent, task, **kwargs)
            span_context = _quietly(span.serialized_context)
            if isinstance(span_context, str):
                with agent_span_lock:
                    agent_span_contexts[id(agent)] = span_context
            try:
                with span:
                    try:
                        status, result = run_without_infrastructure_turn(agent, task, **kwargs)
                    except BaseException as error:
                        _quietly(span.end, error_type=type(error).__name__)
                        raise
                    _quietly(span.set_output, {"status": status, "result": result})
                    return status, result
            finally:
                if isinstance(span_context, str):
                    with agent_span_lock:
                        agent_span_contexts.pop(id(agent), None)

        BaseAgent.run = traced_agent_run

    original_execute_tool = DefaultAgent._execute_tool

    def safe_execute_tool(agent, action: dict) -> dict:
        try:
            return original_execute_tool(agent, action)
        except InfraError as error:
            # _emit_outcome appends this as a tool response before run catches
            # it. Keep its call ID/terminal semantics but not host diagnostics.
            raise InfraError("native tool transport failed") from error

    DefaultAgent._execute_tool = safe_execute_tool
    if trace_runtime is not None:

        def traced_execute_tool(agent, action: dict) -> dict:
            # Observe the actual tool call. Transport and error semantics stay
            # in safe_execute_tool, which runs exactly once below.
            tool_name = action.get("tool") if isinstance(action, dict) else None
            params = action.get("params") if isinstance(action, dict) else None
            try:
                invoking_name = name_of(agent=agent)
            except Exception:
                invoking_name = None
            with agent_span_lock:
                parent_context = agent_span_contexts.get(id(agent))
            span = _quietly(
                trace_runtime.start_span,
                f"tool {tool_name or 'unknown'}",
                span_type="TOOL",
                parent_context=parent_context,
                session_id=trace_session,
                metadata={**trace_metadata, "agent": invoking_name or "unknown"},
                input={"tool": tool_name, "params": params},
                tags=["mimoagent-native"],
            )
            if span is None:
                return safe_execute_tool(agent, action)
            with span:
                span_context = _quietly(span.serialized_context)
                previous = getattr(tool_span_state, "current", None)
                if isinstance(span_context, str):
                    # A child agent.run executes synchronously in this thread.
                    # Explicit per-thread binding keeps reused pool threads
                    # isolated; the finally below always restores the thread.
                    tool_span_state.current = span_context
                try:
                    # Collect exec returncodes actually observed during this
                    # tool in this thread. Nested child tool spans push their
                    # own recorder, so each span attributes only its window;
                    # the finally below restores the enclosing recorder.
                    recorder = _push_exec_recorder()
                    try:
                        output = safe_execute_tool(agent, action)
                    except BaseException as error:
                        _quietly(
                            span.set_metadata,
                            _tool_exit_metadata(tool_name, recorder, None),
                        )
                        _quietly(span.end, error_type=type(error).__name__)
                        raise
                    finally:
                        _pop_exec_recorder(recorder)
                    # Original ToolOutput dict only: tracing attributes carry
                    # the observed exit codes, never a fabricated code, and
                    # the output itself is never altered.
                    _quietly(
                        span.set_output,
                        output if isinstance(output, dict) else {"output": output},
                    )
                    _quietly(
                        span.set_metadata,
                        _tool_exit_metadata(tool_name, recorder, output),
                    )
                    return output
                finally:
                    if isinstance(span_context, str):
                        if previous is None:
                            with suppress(AttributeError):
                                del tool_span_state.current
                        else:
                            tool_span_state.current = previous

        DefaultAgent._execute_tool = traced_execute_tool
    original_format_observation = DefaultAgent._format_observation

    def logical_child_observation(agent, output: dict) -> str:
        metadata = output.get("metadata")
        if (
            isinstance(metadata, dict)
            and isinstance(metadata.get("subagent_type"), str)
            and isinstance(metadata.get("log_file"), str)
        ):
            output = output | {
                "metadata": metadata | {"log_file": "childlog://" + Path(metadata["log_file"]).stem}
            }
        return original_format_observation(agent, output)

    DefaultAgent._format_observation = logical_child_observation

    class RecordedModel(OpenAIChatModel):
        def _query(self, messages: list[dict], **kwargs):
            local.name = name_of(messages=messages)
            local.assistant_index = sum(message["role"] == "assistant" for message in messages)
            with evidence_lock:
                previous_prompt_tokens = query_states.get(local.name, {}).get("last_prompt_tokens")
            state = {
                "attempts": 0,
                "last_response_status": None,
                "last_prompt_tokens": previous_prompt_tokens,
            }
            with evidence_lock:
                query_states[local.name] = state
            local.query_state = state
            started = time.monotonic()
            local.cold_deadline = None
            native_timeout = httpx2.Timeout(self.client.timeout)
            local.native_read_timeout = native_timeout.read
            attempt_model = copy.copy(self)
            while True:
                remaining = (
                    COLD_START_BUDGET_S
                    if local.cold_deadline is None
                    else local.cold_deadline - time.monotonic()
                )
                if remaining <= 0:
                    raise ColdStartDeadlineExceeded(
                        attempts=state["attempts"],
                        elapsed_seconds=time.monotonic() - started,
                    )
                state["attempts"] += 1
                local.received_answer = False
                local.response_recorded = False
                # Client copies share transport/hooks but not timeout settings;
                # parallel native children must never overwrite shared options.
                timeout = httpx2.Timeout(
                    connect=min(native_timeout.connect or remaining, remaining),
                    pool=min(native_timeout.pool or remaining, remaining),
                    write=min(native_timeout.write or remaining, remaining),
                    read=(
                        native_timeout.read
                        if local.cold_deadline is None
                        else min(native_timeout.read or remaining, remaining)
                    ),
                )
                attempt_model.client = self.client.with_options(timeout=timeout, max_retries=0)
                try:
                    # Bypass only tenacity's five-attempt decorator. Native
                    # request construction, response parsing and usage remain
                    # in the exact pinned implementation.
                    return OpenAIChatModel._query.__wrapped__(attempt_model, messages, **kwargs)
                except Exception as error:
                    status = _error_status(error)
                    if isinstance(error, openai.APIConnectionError) and not local.response_recorded:
                        # An attempted HTTP request with no response is still
                        # evidence. Missing status/usage must remain missing.
                        rpc.emit(
                            {
                                "event": "model_call",
                                "name": local.name,
                                "assistant_index": local.assistant_index,
                                "sampling": SAMPLING,
                                "response_status": None,
                                "usage": None,
                                "finish_reason": None,
                                "proxy_budget_reason": None,
                                "transport_error_type": type(error).__name__,
                            }
                        )
                    transient = (
                        isinstance(error, openai.APIConnectionError)
                        or status is not None
                        and (500 <= status <= 599 or status == 429)
                    )
                    if (
                        local.received_answer
                        or not transient
                        or _error_budget_reason(error) is not None
                    ):
                        raise
                    if local.cold_deadline is None:
                        local.cold_deadline = time.monotonic() + COLD_START_BUDGET_S
                    remaining = local.cold_deadline - time.monotonic()
                    delay = _retry_delay(state["attempts"])
                    if remaining <= delay:
                        raise ColdStartDeadlineExceeded(
                            attempts=state["attempts"],
                            elapsed_seconds=time.monotonic() - started,
                        ) from error
                    # Raw logs contain types/statuses, never provider bodies or
                    # traceback-bearing host paths.
                    sys.stderr.write(
                        f"native transport retry: attempt={state['attempts']} "
                        f"status={status} error_type={type(error).__name__} delay={delay:g}s\n"
                    )
                    sys.stderr.flush()
                    time.sleep(delay)

    model_config = config["model"] | {"model_name": initial["model_name"]}
    model_config["model_kwargs"] = config["model"]["model_kwargs"] | {
        "base_url": initial["proxy_url"],
        "api_key": initial["capability"],
        **SAMPLING,
    }
    model = RecordedModel(**model_config)

    def record_response(response) -> None:
        # A real HTTP200 is never resent, even if its body is empty, malformed
        # or fails while reading. Capture status before reading/parsing it.
        local.query_state["last_response_status"] = response.status_code
        local.response_recorded = True
        local.received_answer = response.status_code == 200
        timeouts = response.request.extensions.get("timeout", {})
        if local.received_answer:
            # The pinned httpx2/httpcore2 HTTP/1.1 transport shares this dict
            # with its lazy body stream; restore native generation read time
            # before response.read(), without altering other concurrent calls.
            timeouts["read"] = local.native_read_timeout
        elif response.status_code == 429 or 500 <= response.status_code <= 599:
            if local.cold_deadline is None:
                local.cold_deadline = time.monotonic() + COLD_START_BUDGET_S
            timeouts["read"] = max(0.001, local.cold_deadline - time.monotonic())
        request = json.loads(response.request.content)
        payload = {}
        try:
            response.read()
            try:
                value = response.json()
                if isinstance(value, dict):
                    payload = value
            except ValueError:
                pass
        finally:
            choices = payload.get("choices") or []
            first_choice = (
                choices[0]
                if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                else {}
            )
            body = getattr(response, "_content", b"")
            refusal = proxy_budget_reason(
                response.status_code, body.decode("utf-8", errors="replace")
            )
            # Server-reported prefix size for the 400 corroboration: history
            # only grows, so a prefix already at the served limit proves the
            # next call overflows. Usage integers only, never message text.
            usage_block = payload.get("usage")
            if isinstance(usage_block, dict) and isinstance(usage_block.get("prompt_tokens"), int):
                local.query_state["last_prompt_tokens"] = usage_block["prompt_tokens"]

            rpc.emit(
                {
                    "event": "model_call",
                    "name": local.name,
                    "assistant_index": local.assistant_index,
                    "sampling": {key: request.get(key) for key in SAMPLING},
                    "response_status": response.status_code,
                    "usage": payload.get("usage"),
                    "finish_reason": first_choice.get("finish_reason"),
                    "proxy_budget_reason": _safe_budget_reason(refusal),
                }
            )

    model.client._client.event_hooks["response"].append(record_response)
    agent = DefaultAgent(
        model=model, env=rpc, msg_path=logs.agent_msg_path("main"), **config["agent"]
    )
    logs.register_agent("main", agent)
    status, result = None, None
    stop_metadata: dict = {}
    try:
        with use_log_context(logs):
            status, result = agent.run(initial["instruction"])
        child_stops = {
            name: {
                "exit_status": details["error_type"],
                "stop_reason": "infra_error",
                "infra_error": details,
            }
            for name, details in infrastructure_stops.items()
            if name != "main"
        }
        child_context_stops = {
            name: {
                "exit_status": CONTEXT_EXHAUSTED_STATUS,
                "stop_reason": CONTEXT_EXHAUSTED_STOP,
                "context_exhaustion": details,
            }
            for name, details in context_stops.items()
            if name != "main"
        }
        if child_stops or child_context_stops:
            stop_metadata["agent_stops"] = {**child_stops, **child_context_stops}
        if status == CONTEXT_EXHAUSTED_STATUS:
            stop_metadata |= {
                "stop_reason": CONTEXT_EXHAUSTED_STOP,
                "context_exhaustion": context_stops["main"],
            }
        elif status in {"ModelQueryError", "InfraError"}:
            stop_metadata |= {
                "stop_reason": "infra_error",
                "infra_error": infrastructure_stops["main"],
            }
    finally:
        # Official native serialization is retained for parity comparisons; it
        # does not serialize model_kwargs, so no proxy capability enters it.
        try:
            save_traj(
                agent,
                Path(initial["native_trajectory_path"]),
                print_path=False,
                exit_status=status,
                result=result,
                log_context=logs,
                extra_info={
                    "native_revision": NATIVE_REVISION,
                    "swe_sha256": SWE_SHA256,
                    "sampling": SAMPLING,
                    "antihack": False,
                    **stop_metadata,
                },
            )
        except Exception as error:
            observer_failure("native_trajectory", error)
        model.client.close()
        logs.close()
        if trace_runtime is not None:
            # Bounded local-only drain of SDK spans to the native sink before
            # the finished event; never a Cloud request or wait. The host
            # forwards the file. Failures leave completion unchanged.
            _quietly(trace_runtime.flush_native)
    rpc.emit(
        {
            "event": "finished",
            "exit_status": status,
            "result": result,
            "model_stats": {"api_calls": model.n_calls, **vars(model.token_stats)},
            **stop_metadata,
        }
    )


if __name__ == "__main__":
    main()
