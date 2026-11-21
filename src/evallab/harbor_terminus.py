from __future__ import annotations

import base64
import json
import os
import shlex
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harbor.agents.installed.base import NonZeroAgentExitCodeError  # ty: ignore[unresolved-import]
from harbor.agents.terminus_2.terminus_2 import (  # ty: ignore[unresolved-import]
    Terminus2,
    Terminus2Options,
)
from harbor.llms.lite_llm import LiteLLM  # ty: ignore[unresolved-import]
from pydantic import Field

from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_CONTEXT_TOKENS,
    BREAKER_SELFHOSTED_KEY_ENV,
    BREAKER_SELFHOSTED_MAX_COMPLETION_TOKENS,
    BREAKER_SELFHOSTED_PROXY_CAPABILITY_ENV,
    MIMO_SELFHOSTED_CONTEXT_TOKENS,
    MIMO_SELFHOSTED_MODEL_PREFIX,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    MIMO_SELFHOSTED_PROXY_TOKEN,
    MIMO_SELFHOSTED_TEMPERATURE,
    OPENROUTER_MODEL_PREFIX,
    OPENROUTER_MODEL_SELECTORS,
    OPENROUTER_PROXY_CAPABILITY_ENV,
    OPENROUTER_PROXY_TOKEN,
    TERMINUS_PROXY_URL_ENV,
    ZAI_OPENAPI_ALLOWED_MODELS,
    ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
    ZAI_OPENAPI_PROXY_TOKEN,
    collected_secret_values,
    is_breaker_selfhosted_model,
    is_openrouter_model,
    openrouter_route,
    parse_breaker_selfhosted_model,
    parse_mimo_selfhosted_model,
)
from evallab.harbor_common import sanitize_native_trajectory
from evallab.loopfix import (
    LOOP_BREAK_KEY,
    LOOP_GRACE_CALLS,
    LOOP_NUDGE_MESSAGE,
    LOOP_STOP_REASON,
    OUTPUT_SPILL_PATH,
    cap_output,
    live_loop_action,
    loop_break_record,
    loop_decision,
)
from evallab.mimo_tool_calls import MimoToolCallParser, has_native_completion
from evallab.probe03 import LOOP_MIN_RUN
from evallab.step_layers import (
    STEP_LAYERS_KEY,
    attach_layers,
    build_recorded_layers,
    copied_layers,
)
from evallab.token_flow import COMMAND_RUN_MIN

__all__ = [
    "LoopBreakStop",
    "SecretSafeTerminus2",
    "SecretSafeTerminus2Options",
    "TrialBudgetExhaustedError",
    "apply_mimo_blocklist",
    "mimo_replay_parser",
]

TRIAL_BUDGET_EXHAUSTED_MESSAGE = "trial budget exhausted"
TRIAL_BUDGET_EXHAUSTED_STOP_REASON = "trial_budget_exhausted"
PROSE_COMPLETION_STEP_FLAG = "prose_completion"

_SPILL_CHUNK_BYTES = 48 * 1024


class TrialBudgetExhaustedError(NonZeroAgentExitCodeError):
    pass


class LoopBreakStop(NonZeroAgentExitCodeError):
    pass


def _is_trial_budget_exhausted(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if TRIAL_BUDGET_EXHAUSTED_MESSAGE in str(current):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class _FinishReasonLiteLLM(LiteLLM):

    last_finish_reason: str | None = None

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        self.last_finish_reason = None
        return await super().call(*args, **kwargs)

    def _extract_usage_info(self, response: Any) -> Any:
        try:
            reason = response["choices"][0].get("finish_reason")
        except (AttributeError, IndexError, KeyError, TypeError):
            reason = None
        self.last_finish_reason = reason if isinstance(reason, str) else None
        return super()._extract_usage_info(response)


def _record_prose_completions(path: Path) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list):
        return
    count = sum(
        1
        for step in steps
        if isinstance(step, dict)
        and not step.get("is_copied_context")
        and isinstance(step.get("extra"), dict)
        and step["extra"].get(PROSE_COMPLETION_STEP_FLAG) is True
    )
    metrics = payload.get("final_metrics")
    metrics = dict(metrics) if isinstance(metrics, dict) else {}
    extra = metrics.get("extra")
    metrics["extra"] = {**(extra if isinstance(extra, dict) else {}), "prose_completions": count}
    payload["final_metrics"] = metrics
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _record_loop_break(path: Path, record: dict[str, Any]) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    metrics = payload.get("final_metrics")
    metrics = dict(metrics) if isinstance(metrics, dict) else {}
    extra = metrics.get("extra")
    metrics["extra"] = {**(extra if isinstance(extra, dict) else {}), LOOP_BREAK_KEY: record}
    payload["final_metrics"] = metrics
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mimo_replay_parser() -> Any:
    from harbor.agents.terminus_2.terminus_json_plain_parser import (  # ty: ignore[unresolved-import]
        TerminusJSONPlainParser,
    )

    from evallab.step_layers import ReplayedCommand, ReplayedParse

    inner = TerminusJSONPlainParser()

    def parse(text: str) -> ReplayedParse:
        result = inner.parse_response(text)
        commands = tuple(
            ReplayedCommand(
                keystrokes=command.keystrokes,
                duration_sec=min(command.duration, 60)
                if isinstance(getattr(command, "duration", None), (int, float))
                else None,
            )
            for command in result.commands
        )
        return ReplayedParse(
            error=getattr(result, "error", None),
            commands=commands,
            task_complete=bool(getattr(result, "is_task_complete", False)),
        )

    return parse


_NO_PENDING_PROSE_STEP = object()

MIMO_BLOCKLIST_PATH = "/var/lib/mimo/blocklist"


async def apply_mimo_blocklist(environment: Any) -> str:
    res = await environment.exec(
        f"if [ -f {MIMO_BLOCKLIST_PATH} ]; then "
        f"cat {MIMO_BLOCKLIST_PATH} >> /etc/hosts && grep -c '^0.0.0.0' /etc/hosts; "
        "else echo none; fi",
        user="root",
    )
    if res.return_code != 0:
        raise RuntimeError(
            "could not install the answer-leak blocklist in /etc/hosts: "
            + (res.stderr or res.stdout or "")[-300:]
        )
    out = (res.stdout or "").strip()
    return "none for this task" if out == "none" else f"{out} hosts blocked in /etc/hosts"


_PROVIDER_KEY_ENVS = {
    "zai": "ZAI_API_KEY",
    "mimo_selfhosted": "OPENAI_API_KEY",
    "breaker_selfhosted": "OPENAI_API_KEY",
    "openrouter": "OPENAI_API_KEY",
}

_LOOPBACK_HOST = "127.0.0.1"

_FORBIDDEN_KWARG_SUBSTRINGS = ("api_key", "api_base", "base_url")

_FORBIDDEN_EXTRA_ENV_KEYS = frozenset(
    {
        BREAKER_SELFHOSTED_KEY_ENV.casefold(),
        "mswea_api_key",
        "zai_api_key",
        "zai_api_base",
        "openai_api_key",
        "openai_base_url",
        "openai_api_base",
        "anthropic_api_key",
        "anthropic_base_url",
        *(str(key).casefold() for key in ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS),
    }
)
_FORBIDDEN_EXTRA_ENV_PREFIXES = (
    "evallab_zai_openapi",
    "evallab_tinker",
    "evallab_mimo_selfhosted",
    "evallab_breaker_selfhosted",
    "evallab_openrouter",
    "evallab_terminus",
    "evallab_zai_",
)


def _resolve_metered_model(model_name: str | None) -> tuple[str, bool]:
    if isinstance(model_name, str) and model_name.startswith(OPENROUTER_MODEL_PREFIX):
        openrouter_route(model_name)
        return model_name, False
    if is_breaker_selfhosted_model(model_name):
        parse_breaker_selfhosted_model(model_name)
        assert isinstance(model_name, str)
        return model_name, False
    if isinstance(model_name, str) and model_name.startswith(MIMO_SELFHOSTED_MODEL_PREFIX):
        parse_mimo_selfhosted_model(model_name)
        return model_name, True
    if model_name in ZAI_OPENAPI_ALLOWED_MODELS:
        return model_name, False
    raise ValueError(
        "SecretSafeTerminus2 requires an exact metered model: one of "
        f"{sorted(ZAI_OPENAPI_ALLOWED_MODELS)}, "
        f"the self-hosted route {MIMO_SELFHOSTED_MODEL_SELECTOR!r}, "
        f"the matched breaker-selfhosted base/L1 routes, or OpenRouter {OPENROUTER_MODEL_SELECTORS!r}; "
        f"got {model_name!r}. Coding Plan credentials are not admitted."
    )


def _require_loopback_proxy_url() -> str:
    raw = os.environ.get(TERMINUS_PROXY_URL_ENV, "")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{TERMINUS_PROXY_URL_ENV} is not a valid proxy endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != _LOOPBACK_HOST
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{TERMINUS_PROXY_URL_ENV} must be an http://127.0.0.1:<port> "
            f"trial endpoint, got {raw!r}"
        )
    return f"http://{_LOOPBACK_HOST}:{port}"


def _require_capability(env_name: str, placeholder: str) -> str:
    capability = os.environ.get(env_name, "")
    if not capability or capability == placeholder:
        raise ValueError(f"SecretSafeTerminus2 requires a bound trial capability in {env_name}")
    return capability


def _reject_kwarg_overrides(values: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    items = dict(values or {})
    for key in items:
        folded = str(key).casefold()
        if any(fragment in folded for fragment in _FORBIDDEN_KWARG_SUBSTRINGS):
            raise ValueError(
                f"SecretSafeTerminus2 rejects {source} transport override {key!r}: "
                "model transport is bound by the trial proxy"
            )
    return items


def _scrubbed_extra_env(
    extra_env: dict[str, str] | None, *, capability: str | None
) -> dict[str, str]:
    env = dict(extra_env or {})
    secrets = collected_secret_values()
    if capability is not None:
        secrets = secrets | {capability}
    for key, value in env.items():
        folded = str(key).casefold()
        if folded in _FORBIDDEN_EXTRA_ENV_KEYS or folded.startswith(_FORBIDDEN_EXTRA_ENV_PREFIXES):
            raise ValueError(
                f"SecretSafeTerminus2 rejects task-container env override {key!r}: "
                "provider credentials cannot enter the task environment"
            )
        if value and value in secrets:
            raise ValueError(
                f"SecretSafeTerminus2 rejects task-container env value for {key!r}: "
                "provider credential cannot enter the task environment"
            )
    return env


class SecretSafeTerminus2Options(Terminus2Options):

    loop_break: bool = Field(default=False, description="Enable the HAR-116 loop nudge-and-stop.")
    output_cap_chars: int | None = Field(
        default=None, description="Cap fed-back terminal output; spill the full text to a file."
    )
    completion_fix: bool = Field(
        default=False, description="End the episode on a native completion signal."
    )
    loop_command_run_min: int | None = Field(
        default=None, description="Minimum run-command evidence before a loop verdict."
    )
    loop_message_run_min: int | None = Field(
        default=None, description="Minimum message evidence before a loop verdict."
    )
    loop_grace_calls: int | None = Field(
        default=None, description="Calls after the nudge before the loop stop fires."
    )


class SecretSafeTerminus2(Terminus2):

    options_model = SecretSafeTerminus2Options

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        model_name: str | None = None,
        api_base: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        loop_break: bool = False,
        output_cap_chars: int | None = None,
        completion_fix: bool = False,
        loop_command_run_min: int | None = None,
        loop_message_run_min: int | None = None,
        loop_grace_calls: int | None = None,
        **kwargs: Any,
    ) -> None:
        if args:
            raise ValueError(
                "SecretSafeTerminus2 accepts at most logs_dir positionally; "
                "pass model_name as a keyword argument"
            )
        if not isinstance(loop_break, bool):
            raise ValueError("loop_break must be a boolean")
        if not isinstance(completion_fix, bool):
            raise ValueError("completion_fix must be a boolean")
        if output_cap_chars is not None and (
            isinstance(output_cap_chars, bool)
            or not isinstance(output_cap_chars, int)
            or output_cap_chars < 2
        ):
            raise ValueError("output_cap_chars must be an integer of at least 2, or null")
        for knob_name, knob_value in (
            ("loop_command_run_min", loop_command_run_min),
            ("loop_message_run_min", loop_message_run_min),
            ("loop_grace_calls", loop_grace_calls),
        ):
            if knob_value is not None and (
                isinstance(knob_value, bool) or not isinstance(knob_value, int) or knob_value < 2
            ):
                raise ValueError(f"{knob_name} must be an integer of at least 2, or null")
        self._loop_break_enabled = loop_break
        self._completion_fix_enabled = completion_fix
        self._output_cap_chars = output_cap_chars
        self._loop_command_run_min = loop_command_run_min or COMMAND_RUN_MIN
        self._loop_message_run_min = loop_message_run_min or LOOP_MIN_RUN
        self._loop_grace_calls = loop_grace_calls or LOOP_GRACE_CALLS
        self._loop_nudged_call: int | None = None
        self._loop_detector: str | None = None
        self._loop_stop_call: int | None = None
        self._loop_model_next: str | None = None
        self._loop_stop_pending = False
        self._pending_composed_message = ""
        self._mimo_selfhosted = False
        self._prose_completions = 0
        self._pending_prose_step: Any = _NO_PENDING_PROSE_STEP
        self._pending_layer: dict[str, Any] | None = None
        self._layer_queue: list[dict[str, Any]] = []
        self._layer_steps_id: int | None = None
        self._layer_annotated: int = 0
        model, self._mimo_selfhosted = _resolve_metered_model(model_name)
        if api_base is not None:
            raise ValueError(
                "SecretSafeTerminus2 rejects api_base overrides: "
                "model transport is bound by the trial proxy"
            )
        backend = kwargs.get("llm_backend")
        if backend is not None and getattr(backend, "value", backend) != "litellm":
            raise ValueError(f"SecretSafeTerminus2 requires the litellm backend, got {backend!r}")
        for key in kwargs:
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_KWARG_SUBSTRINGS):
                raise ValueError(
                    f"SecretSafeTerminus2 rejects transport override {key!r}: "
                    "model transport is bound by the trial proxy"
                )
        clean_llm_kwargs = _reject_kwarg_overrides(llm_kwargs, source="llm_kwargs")
        clean_llm_call_kwargs = _reject_kwarg_overrides(llm_call_kwargs, source="llm_call_kwargs")
        proxy_url = _require_loopback_proxy_url()
        if is_breaker_selfhosted_model(model):
            if kwargs.get("model_info") is not None:
                raise ValueError("breaker model context/pricing is runtime-bound")
            kwargs["model_info"] = {
                "max_input_tokens": BREAKER_SELFHOSTED_CONTEXT_TOKENS,
                "max_output_tokens": BREAKER_SELFHOSTED_MAX_COMPLETION_TOKENS,
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "litellm_provider": "openai",
            }
            kwargs["temperature"] = 1.0
            capability = _require_capability(
                BREAKER_SELFHOSTED_PROXY_CAPABILITY_ENV, ZAI_OPENAPI_PROXY_TOKEN
            )
            provider = "breaker_selfhosted"
        elif self._mimo_selfhosted:
            if kwargs.get("model_info") is not None:
                raise ValueError(
                    "self-hosted MiMo model context/pricing is runtime-bound, not a harness override"
                )
            kwargs["model_info"] = {
                "max_input_tokens": MIMO_SELFHOSTED_CONTEXT_TOKENS,
                "max_output_tokens": MIMO_SELFHOSTED_CONTEXT_TOKENS,
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "litellm_provider": "openai",
            }
            kwargs["temperature"] = MIMO_SELFHOSTED_TEMPERATURE
            capability = _require_capability(
                MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, MIMO_SELFHOSTED_PROXY_TOKEN
            )
            provider = "mimo_selfhosted"
        elif is_openrouter_model(model):
            if kwargs.get("model_info") is not None:
                raise ValueError(
                    "OpenRouter model context/pricing is runtime-bound, not a harness override"
                )
            route = openrouter_route(model)
            kwargs["model_info"] = {
                "max_input_tokens": route.context_input_tokens,
                "max_output_tokens": route.max_completion_tokens,
                "input_cost_per_token": route.input_cost_micros_per_million / 1e12,
                "output_cost_per_token": route.output_cost_micros_per_million / 1e12,
                "litellm_provider": "openai",
            }
            capability = _require_capability(
                OPENROUTER_PROXY_CAPABILITY_ENV, OPENROUTER_PROXY_TOKEN
            )
            provider = "openrouter"
        else:
            capability = _require_capability(
                ZAI_OPENAPI_PROXY_CAPABILITY_ENV, ZAI_OPENAPI_PROXY_TOKEN
            )
            provider = "zai"
        clean_extra_env = _scrubbed_extra_env(extra_env, capability=capability)
        super().__init__(
            logs_dir=logs_dir,
            model_name=model,
            api_base=proxy_url,
            llm_kwargs=clean_llm_kwargs,
            llm_call_kwargs=clean_llm_call_kwargs,
            extra_env=clean_extra_env,
            **kwargs,
        )
        if capability is not None and provider is not None:
            os.environ[_PROVIDER_KEY_ENVS[provider]] = capability

    def _init_llm(self, *args: Any, **kwargs: Any) -> Any:
        llm = super()._init_llm(*args, **kwargs)
        if self._mimo_selfhosted:
            if type(llm) is not LiteLLM:
                raise ValueError(
                    "the MiMo prose rule needs upstream's LiteLLM client, got " + type(llm).__name__
                )
            llm.__class__ = _FinishReasonLiteLLM
        return llm

    def _get_parser(self) -> Any:
        parser = super()._get_parser()
        if self._mimo_selfhosted and self.options.parser_name == "json":
            llm = cast(_FinishReasonLiteLLM, self._llm)
            return MimoToolCallParser(parser, finish_reason=lambda: llm.last_finish_reason)
        return parser

    def _reset_per_run_state(self) -> None:
        super()._reset_per_run_state()
        self._prose_completions = 0
        self._pending_prose_step = _NO_PENDING_PROSE_STEP
        self._pending_layer = None
        self._layer_queue = []
        self._layer_steps_id = None
        self._layer_annotated = 0
        self._loop_nudged_call = None
        self._loop_detector = None
        self._loop_stop_call = None
        self._loop_model_next = None
        self._loop_stop_pending = False
        self._pending_composed_message = ""

    async def _handle_llm_interaction(self, *args: Any, **kwargs: Any) -> Any:
        self._flush_pending_layer("parse_error: nothing executed")
        outcome = await super()._handle_llm_interaction(*args, **kwargs)
        commands, is_task_complete, feedback, analysis, plan, llm_response = outcome
        if (
            self._completion_fix_enabled
            and not is_task_complete
            and bool(getattr(self, "_pending_completion", False))
            and has_native_completion(getattr(llm_response, "content", None))
        ):
            is_task_complete = True
            outcome = (commands, True, feedback, analysis, plan, llm_response)
        self._pending_composed_message = self._recorded_message(
            analysis, plan, llm_response, feedback
        )
        parser = self._parser
        if isinstance(parser, MimoToolCallParser) and parser.last_prose_completion:
            self._pending_prose_step = (
                self._trajectory_steps[-1] if self._trajectory_steps else None
            )
        if isinstance(parser, MimoToolCallParser):
            prose_mapped = parser.last_prose_completion
            parse_error = parser.last_error or None
        else:
            prose_mapped = False
            parse_error = (
                feedback[len("ERROR:") :].strip()
                if isinstance(feedback, str) and feedback.startswith("ERROR:")
                else None
            )
        content = getattr(llm_response, "content", None)
        self._pending_layer = {
            "message": content,
            "reasoning": getattr(llm_response, "reasoning_content", None),
            "prose_mapped": prose_mapped,
            "commands": [(command.keystrokes, command.duration_sec) for command in commands],
            "task_complete": bool(is_task_complete),
            "parse_error": parse_error,
            "exec": None,
        }
        if self._is_parse_error_turn(feedback):
            feedback = self._apply_loop_fix_to_parse_error(feedback, llm_response)
            outcome = (commands, is_task_complete, feedback, analysis, plan, llm_response)
        return outcome

    async def _execute_commands(self, commands: Any, session: Any) -> Any:
        timeout, output = await self._execute_with_full_output(commands, session)
        pending = self._pending_layer
        if pending is not None:
            pending["exec"] = {
                "keystrokes_sent": [command.keystrokes for command in commands],
                "durations_sec": [command.duration_sec for command in commands],
                "sent_at": datetime.now(UTC).isoformat(),
                "timeout": bool(timeout),
                "output": output,
            }
            self._layer_queue.append(pending)
            self._pending_layer = None
        output = await self._apply_loop_fix(output, commands, session)
        return timeout, output

    async def _execute_with_full_output(self, commands: Any, session: Any) -> Any:
        if self._output_cap_chars is None or not hasattr(session, "get_incremental_output"):
            return await super()._execute_commands(commands, session)
        captured: dict[str, str] = {}
        original = session.get_incremental_output

        async def _capturing() -> str:
            raw = await original()
            captured["raw"] = raw
            return raw

        session.get_incremental_output = _capturing
        try:
            timeout, output = await super()._execute_commands(commands, session)
        finally:
            session.get_incremental_output = original
        if "raw" in captured:
            self._pending_full_output = captured["raw"]
        return timeout, output

    def _composed_message(self, analysis: Any, plan: Any, llm_response: Any) -> str:
        if getattr(self, "_save_raw_content_in_trajectory", False):
            content = getattr(llm_response, "content", None)
            return content if isinstance(content, str) else ""
        parts = []
        if isinstance(analysis, str) and analysis:
            parts.append(f"Analysis: {analysis}")
        if isinstance(plan, str) and plan:
            parts.append(f"Plan: {plan}")
        return "\n".join(parts)

    @staticmethod
    def _is_parse_error_turn(feedback: Any) -> bool:
        return isinstance(feedback, str) and "ERROR:" in feedback

    def _recorded_message(self, analysis: Any, plan: Any, llm_response: Any, feedback: Any) -> str:
        if self._is_parse_error_turn(feedback) and not getattr(
            self, "_save_raw_content_in_trajectory", False
        ):
            content = getattr(llm_response, "content", None)
            return content if isinstance(content, str) else ""
        return self._composed_message(analysis, plan, llm_response)

    def _loop_thresholds(self) -> dict[str, int]:
        return {
            "command_run_min": self._loop_command_run_min,
            "message_run_min": self._loop_message_run_min,
            "grace_calls": self._loop_grace_calls,
        }

    def _current_step(self, commands: Any) -> dict[str, Any]:
        return {
            "source": "agent",
            "message": self._pending_composed_message,
            "tool_calls": [
                {
                    "function_name": "bash_command",
                    "arguments": {"keystrokes": getattr(command, "keystrokes", "")},
                }
                for command in commands
            ],
        }

    def _taken_steps(self) -> list[dict[str, Any]]:
        taken = []
        for step in getattr(self, "_trajectory_steps", []) or []:
            if getattr(step, "source", None) != "agent" or getattr(
                step, "is_copied_context", False
            ):
                continue
            calls = []
            for call in getattr(step, "tool_calls", None) or []:
                arguments = getattr(call, "arguments", None)
                calls.append(
                    {
                        "function_name": getattr(call, "function_name", None),
                        "arguments": arguments if isinstance(arguments, dict) else {},
                    }
                )
            taken.append(
                {
                    "source": "agent",
                    "message": getattr(step, "message", "") or "",
                    "tool_calls": calls,
                }
            )
        return taken

    async def _apply_loop_fix(self, output: str, commands: Any, session: Any) -> str:
        if self._output_cap_chars is not None:
            output = await self._cap_feedback(output, session)
        if not self._loop_break_enabled or self._loop_stop_call is not None:
            return output
        steps = [*self._taken_steps(), self._current_step(commands)]
        action = live_loop_action(steps, **self._loop_thresholds())
        if self._loop_nudged_call is not None and self._loop_model_next is None:
            self._loop_model_next = self._describe_next(commands)
        if action == "nudge":
            self._loop_nudged_call = len(steps)
            self._loop_detector = loop_decision(steps, **self._loop_thresholds())["detector"]
            return output + "\n\n" + LOOP_NUDGE_MESSAGE
        if action == "stop" and not self._finishing():
            self._loop_stop_call = len(steps)
            self._loop_stop_pending = True
        return output

    def _apply_loop_fix_to_parse_error(self, feedback: Any, llm_response: Any) -> Any:
        if not self._loop_break_enabled or self._loop_stop_call is not None:
            return feedback
        content = getattr(llm_response, "content", None)
        current = {
            "source": "agent",
            "message": content if isinstance(content, str) else "",
            "tool_calls": [],
        }
        steps = [*self._taken_steps(), current]
        action = live_loop_action(steps, **self._loop_thresholds())
        if action == "nudge":
            self._loop_nudged_call = len(steps)
            self._loop_detector = loop_decision(steps, **self._loop_thresholds())["detector"]
            if isinstance(feedback, str):
                return feedback + "\n\n" + LOOP_NUDGE_MESSAGE
            return feedback
        if action == "stop" and not self._finishing():
            self._loop_stop_call = len(steps)
            self._loop_stop_pending = True
        return feedback

    def _finishing(self) -> bool:
        pending = self._pending_layer or {}
        return bool(pending.get("task_complete")) and bool(
            getattr(self, "_pending_completion", False)
        )

    @staticmethod
    def _describe_next(commands: Any) -> str:
        texts = [
            getattr(command, "keystrokes", "")
            for command in commands
            if getattr(command, "keystrokes", "").strip()
        ]
        if not texts:
            return "no command"
        return " | ".join(text.strip() for text in texts)[:200]

    async def _cap_feedback(self, output: str, session: Any) -> str:
        full = getattr(self, "_pending_full_output", None)
        self._pending_full_output = None
        text = full if isinstance(full, str) else output
        episode = getattr(self, "_n_episodes", 0) or 0
        spill = OUTPUT_SPILL_PATH.format(episode=episode)
        problem = await self._spill_output(session, spill, text)
        capped = cap_output(text, spill, limit=self._output_cap_chars or 0)
        if problem is not None:
            capped += f"\n[full output was not saved: {problem}]"
        return capped

    async def _spill_output(self, session: Any, path: str, content: str) -> str | None:
        environment = getattr(session, "environment", None)
        if environment is None or not hasattr(environment, "exec"):
            return "the session has no executable environment"
        user = getattr(session, "_user", None)
        directory, _, _ = path.rpartition("/")
        made = await environment.exec(f"mkdir -p {shlex.quote(directory)}", user=user)
        if getattr(made, "return_code", 1) != 0:
            return "could not create the output directory"
        payload = base64.b64encode(content.encode("utf-8", errors="replace")).decode("ascii")
        if not payload:
            written = await environment.exec(f": > {shlex.quote(path)}", user=user)
            if getattr(written, "return_code", 1) != 0:
                return "could not write the output file"
            return None
        for offset in range(0, len(payload), _SPILL_CHUNK_BYTES):
            chunk = payload[offset : offset + _SPILL_CHUNK_BYTES]
            redirect = ">" if offset == 0 else ">>"
            written = await environment.exec(
                f"printf %s {chunk} | base64 -d {redirect} {shlex.quote(path)}",
                user=user,
            )
            if getattr(written, "return_code", 1) != 0:
                return "could not write the output file"
        await environment.exec(f"chmod a+r {shlex.quote(path)}", user=user)
        return None

    def _flush_pending_layer(self, reason: str) -> None:
        pending = self._pending_layer
        if pending is None:
            return
        pending["exec"] = None
        pending["not_executed_reason"] = reason
        self._layer_queue.append(pending)
        self._pending_layer = None

    def _annotate_step_layers(self) -> None:
        step_list = getattr(self, "_trajectory_steps", None)
        steps = step_list if step_list else []
        if step_list is not None and id(step_list) != self._layer_steps_id:
            self._layer_steps_id = id(step_list)
            self._layer_annotated = 0
        raw_content = bool(getattr(self, "_save_raw_content_in_trajectory", False))
        for step in steps[self._layer_annotated :]:
            if getattr(step, "source", None) != "agent":
                self._layer_annotated += 1
                continue
            if STEP_LAYERS_KEY in (getattr(step, "extra", None) or {}):
                self._layer_annotated += 1
                continue
            if getattr(step, "is_copied_context", False):
                step.extra = attach_layers(getattr(step, "extra", None), copied_layers())
                self._layer_annotated += 1
                continue
            if raw_content:
                step_message = getattr(step, "message", None)
                match_index: int | None = None
                for index, record in enumerate(self._layer_queue):
                    if record.get("message") == step_message:
                        match_index = index
                        break
                if match_index is None:
                    self._layer_annotated += 1
                    continue
                del self._layer_queue[:match_index]
            if self._layer_queue:
                record = self._layer_queue.pop(0)
                exec_info = record.get("exec") or {}
                if record.get("exec") is None:
                    layers = build_recorded_layers(
                        message=record.get("message"),
                        reasoning=record.get("reasoning"),
                        prose_mapped=record.get("prose_mapped", False),
                        commands=record.get("commands") or [],
                        task_complete=record.get("task_complete", False),
                        parse_error=record.get("parse_error"),
                        keystrokes_sent=None,
                        durations_sec=None,
                        sent_at=None,
                        timeout=None,
                        output=None,
                        not_executed_reason=record.get("not_executed_reason")
                        or "no execution recorded",
                    )
                else:
                    layers = build_recorded_layers(
                        message=record.get("message"),
                        reasoning=record.get("reasoning"),
                        prose_mapped=record.get("prose_mapped", False),
                        commands=record.get("commands") or [],
                        task_complete=record.get("task_complete", False),
                        parse_error=record.get("parse_error"),
                        keystrokes_sent=exec_info.get("keystrokes_sent"),
                        durations_sec=exec_info.get("durations_sec"),
                        sent_at=exec_info.get("sent_at"),
                        timeout=exec_info.get("timeout"),
                        output=exec_info.get("output"),
                    )
                step.extra = attach_layers(getattr(step, "extra", None), layers)
            self._layer_annotated += 1

    def _dump_trajectory_with_continuation_index(self, continuation_index: int) -> None:
        self._flag_prose_completion_step()
        self._annotate_step_layers()
        super()._dump_trajectory_with_continuation_index(continuation_index)

    def _dump_trajectory(self) -> None:
        pending = self._pending_layer
        if pending is not None:
            self._flush_pending_layer(
                "parse_error: nothing executed"
                if pending.get("parse_error")
                else "episode interrupted before execution"
            )
        super()._dump_trajectory()
        if self._loop_stop_pending:
            self._loop_stop_pending = False
            raise LoopBreakStop(
                "loop break: the repetition was still going "
                f"{self._loop_stop_call} calls in, {self._loop_grace_calls} after the nudge at "
                f"call {self._loop_nudged_call}"
            )

    def _flag_prose_completion_step(self) -> None:
        anchor = self._pending_prose_step
        if anchor is _NO_PENDING_PROSE_STEP or not self._trajectory_steps:
            return
        step = self._trajectory_steps[-1]
        if step is anchor or step.source != "agent" or step.is_copied_context:
            return
        step.extra = {**(step.extra or {}), PROSE_COMPLETION_STEP_FLAG: True}
        self._prose_completions += 1
        self._pending_prose_step = _NO_PENDING_PROSE_STEP

    async def setup(self, environment: Any) -> None:
        await super().setup(environment)
        self.logger.info("answer-leak blocklist: " + await apply_mimo_blocklist(environment))

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        stop_reason: str | None = None
        try:
            await super().run(instruction, environment, context)
        except Exception as exc:
            if not _is_trial_budget_exhausted(exc):
                raise
            stop_reason = TRIAL_BUDGET_EXHAUSTED_STOP_REASON
            raise TrialBudgetExhaustedError(
                "the trial proxy refused a model call: " + TRIAL_BUDGET_EXHAUSTED_MESSAGE
            ) from exc
        finally:
            metadata: dict[str, Any] = {}
            if stop_reason is not None:
                metadata["stop_reason"] = stop_reason
            if self._mimo_selfhosted:
                metadata["prose_completions"] = self._prose_completions
                for path in self._trajectory_files():
                    try:
                        _record_prose_completions(path)
                    except Exception:
                        continue
            if self._loop_break_enabled:
                record = loop_break_record(
                    fired=self._loop_nudged_call is not None,
                    nudge_call=self._loop_nudged_call,
                    detector=self._loop_detector,
                    stop_call=self._loop_stop_call,
                    model_next=self._loop_model_next,
                )
                metadata[LOOP_BREAK_KEY] = record
                if self._loop_stop_call is not None:
                    metadata["stop_reason"] = LOOP_STOP_REASON
                for path in self._trajectory_files():
                    try:
                        _record_loop_break(path, record)
                    except Exception:
                        continue
            if metadata:
                context.metadata = {**(context.metadata or {}), **metadata}

    def _trajectory_files(self) -> list[Path]:
        logs = Path(self.logs_dir)
        return [logs / "trajectory.json", *sorted(logs.glob("trajectory.cont-*.json"))]

    def populate_context_post_run(self, context: Any) -> None:
        secrets = collected_secret_values()
        for path in self._trajectory_files():
            sanitize_native_trajectory(path, secrets)
        super().populate_context_post_run(context)
        for path in self._trajectory_files():
            if self._mimo_selfhosted:
                _record_prose_completions(path)
            sanitize_native_trajectory(path, secrets)
