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
  metadata, never a synthetic user turn or a traceback in native history.
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
import sys
import threading
import time
from concurrent.futures import Future
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
    return proxy_budget_reason(
        _error_status(error), getattr(response, "text", None)
    )


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
        return self._request("exec", command=command, cwd=cwd or self.cwd, timeout=timeout or 300)

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


def main() -> None:
    initial = json.loads(sys.stdin.readline())
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

    def name_of(agent=None, messages=None) -> str:
        with logs._lock:
            for name, candidate in logs.trajectories.items():
                if candidate is agent or (messages is not None and candidate.messages is messages):
                    return name
        raise RuntimeError("native agent was not registered for trajectory capture")

    original_add_message = BaseAgent.add_message

    def observe_message(agent, role: str, content: str, **kwargs) -> None:
        original_add_message(agent, role, content, **kwargs)
        name = name_of(agent=agent)
        with evidence_lock:
            if name not in names_started:
                names_started.add(name)
                rpc.emit({"event": "agent_start", "name": name, **agent.get_model_query_kwargs()})
            rpc.emit({"event": "message", "name": name, "message": agent.messages[-1]})

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
            try:
                if agent.step() is None:
                    return agent.IDLE_STATUS, agent._last_assistant_text()
                agent.after_step()
            except NonTerminatingException as error:
                agent.add_message("user", str(error))
                agent.after_step()
            except (ModelQueryError, InfraError) as error:
                name = name_of(agent=agent)
                root = _root_error(error)
                state = query_states.get(name, {}) if isinstance(error, ModelQueryError) else {}
                details = {
                    "error_type": type(error).__name__,
                    "root_error_type": type(root).__name__,
                    "last_response_status": state.get("last_response_status", _error_status(root)),
                    "retry_window_seconds": COLD_START_BUDGET_S,
                    "retry_rounds": max(0, state.get("attempts", 1) - 1),
                    "retry_window_exhausted": isinstance(error.__cause__, ColdStartDeadlineExceeded),
                    "budget_refusal": _safe_budget_reason(_error_budget_reason(root)),
                }
                with evidence_lock:
                    infrastructure_stops[name] = details
                # AgentTool returns a child ModelQueryError as a tool result;
                # it must receive a safe summary rather than SDK traceback.
                summary = f"native infrastructure failure: {details['error_type']}"
                if details["budget_refusal"]:
                    summary += f"; {details['budget_refusal']}"
                return type(error).__name__, summary
            except TerminatingException as error:
                agent.add_message("user", str(error))
                return type(error).__name__, str(error)

    BaseAgent.run = run_without_infrastructure_turn

    original_execute_tool = DefaultAgent._execute_tool

    def safe_execute_tool(agent, action: dict) -> dict:
        try:
            return original_execute_tool(agent, action)
        except InfraError as error:
            # _emit_outcome appends this as a tool response before run catches
            # it. Keep its call ID/terminal semantics but not host diagnostics.
            raise InfraError("native tool transport failed") from error

    DefaultAgent._execute_tool = safe_execute_tool
    original_format_observation = DefaultAgent._format_observation

    def logical_child_observation(agent, output: dict) -> str:
        metadata = output.get("metadata")
        if (
            isinstance(metadata, dict)
            and isinstance(metadata.get("subagent_type"), str)
            and isinstance(metadata.get("log_file"), str)
        ):
            output = output | {
                "metadata": metadata | {
                    "log_file": "childlog://" + Path(metadata["log_file"]).stem
                }
            }
        return original_format_observation(agent, output)

    DefaultAgent._format_observation = logical_child_observation

    class RecordedModel(OpenAIChatModel):
        def _query(self, messages: list[dict], **kwargs):
            local.name = name_of(messages=messages)
            local.assistant_index = sum(message["role"] == "assistant" for message in messages)
            state = {"attempts": 0, "last_response_status": None}
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
                    COLD_START_BUDGET_S if local.cold_deadline is None
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
                        native_timeout.read if local.cold_deadline is None
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
                        or status is not None and (500 <= status <= 599 or status == 429)
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
            for name, details in infrastructure_stops.items() if name != "main"
        }
        if child_stops:
            stop_metadata["agent_stops"] = child_stops
        if status in {"ModelQueryError", "InfraError"}:
            stop_metadata |= {
                "stop_reason": "infra_error",
                "infra_error": infrastructure_stops["main"],
            }
    finally:
        # Official native serialization is retained for parity comparisons; it
        # does not serialize model_kwargs, so no proxy capability enters it.
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
        model.client.close()
        logs.close()
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
