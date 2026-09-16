"""Policy-driven dspy.RLM harness shared by the synthetic bench and the Harbor agent.

``LabRlm`` is a thin subclass of ``dspy.RLM`` that applies an ``RlmPolicy``:

- orchestration guidance is prepended through the signature instructions (dspy
  already places ``signature.instructions`` ahead of its action template);
- ``history_window`` masks older REPL outputs in the *view* given to the action
  predictor while the real, unmasked history is what ends up in the trajectory;
- ``iteration_reminder`` replaces the bare ``k/N`` iteration field with an
  explicit remaining-budget sentence;
- an API-equivalent cost ceiling stops the loop (falling back to dspy's extract
  step) instead of letting a runaway trajectory spend without bound.

Language-model construction is centralised here so the bench and the Harbor
agent build byte-identical requests for the same policy.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dspy  # ty: ignore[unresolved-import]
from dspy.adapters.chat_adapter import ChatAdapter  # ty: ignore[unresolved-import]
from dspy.primitives.code_interpreter import CodeInterpreter  # ty: ignore[unresolved-import]
from dspy.primitives.prediction import Prediction  # ty: ignore[unresolved-import]
from dspy.primitives.python_interpreter import PythonInterpreter  # ty: ignore[unresolved-import]
from dspy.primitives.repl_types import (  # ty: ignore[unresolved-import]
    REPLEntry,
    REPLHistory,
    REPLVariable,
)
from dspy.utils.exceptions import (  # ty: ignore[unresolved-import]
    AdapterParseError,
    format_error_for_lm,
)

from evallab.rlm.policies import RlmPolicy

logger = logging.getLogger(__name__)

#: The action step never falls back to JSONAdapter: GLM-5.3-Flash answers
#: dspy's JSON-mode retry with a bare ``{}`` and the whole trial dies. Parse
#: failures are handled inside the loop instead (see ``LabRlm``).
_ACTION_ADAPTER = ChatAdapter(use_json_adapter_fallback=False)

#: litellm retries with exponential backoff; the Z.ai coding plan enforces a
#: per-account concurrency window that other lanes share, so 429s are routine.
LM_RETRIES = 8

_FENCED_CODE = re.compile(r"```(?:python|py|python3|py3)?[ \t]*\n(.*?)```", re.S)
_FIELD_MARKER = re.compile(r"\[\[\s*##\s*\w+\s*##\s*\]\]")
_LABEL_PREFIX = re.compile(r"^\s*(?:reasoning|code)\s*:\s*", re.I | re.M)


def salvage_action(raw: str) -> tuple[str, str] | None:
    """Recover ``(reasoning, code)`` from a response that drifted off dspy's markers.

    Returns ``None`` when no non-empty fenced Python block exists, in which case
    the caller must treat the turn as unparseable.
    """
    match = _FENCED_CODE.search(raw)
    if match is None:
        return None
    code = match.group(1).strip()
    if not code:
        return None
    head = _FIELD_MARKER.sub("", raw[: match.start()])
    head = _LABEL_PREFIX.sub("", head)
    return head.strip(), code


#: Z.ai coding-plan OpenAI-compatible endpoint; the same upstream path the
#: container-side broker (``containers/zai_secret_proxy.py``) forwards to.
ZAI_CODING_API_BASE = "https://api.z.ai/api/coding/paas/v4"

#: API-list-price equivalents mirrored from ``evallab.execution_contracts``
#: (``ZAI_INPUT_COST_MICROS_PER_MILLION`` / ``ZAI_OUTPUT_COST_MICROS_PER_MILLION``).
#: Kept literal here so the harbor-side python (which imports this module with
#: only harbor's dependencies) never needs the full evallab import graph.
ZAI_INPUT_USD_PER_MILLION = 1.40
ZAI_OUTPUT_USD_PER_MILLION = 4.40

MASKED_OUTPUT_MARKER = (
    "[earlier output masked by harness policy: {chars:,} chars hidden; variables and "
    "state persist, re-print from them if you need this again]"
)


def zai_model_id(model_selector: str) -> str:
    """Translate a lab model selector (``zai-coding-plan/glm-5.3-flash``) to the API id."""
    if "/" in model_selector:
        return model_selector.rsplit("/", 1)[1]
    return model_selector


def build_lm(
    *,
    model_id: str,
    api_key: str,
    api_base: str = ZAI_CODING_API_BASE,
    max_tokens: int,
    thinking: bool,
    temperature: float | None,
) -> dspy.LM:
    """One uncached litellm-backed LM for the Z.ai coding endpoint."""
    kwargs: dict[str, Any] = {}
    if not thinking:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return dspy.LM(
        f"openai/{model_id}",
        api_base=api_base,
        api_key=api_key,
        max_tokens=max_tokens,
        cache=False,
        num_retries=LM_RETRIES,
        **kwargs,
    )


def build_lms(
    policy: RlmPolicy, *, model_id: str, api_key: str, api_base: str = ZAI_CODING_API_BASE
) -> tuple[dspy.LM, dspy.LM | None]:
    root = build_lm(
        model_id=model_id,
        api_key=api_key,
        api_base=api_base,
        max_tokens=policy.root_max_tokens,
        thinking=policy.root_thinking,
        temperature=policy.temperature,
    )
    sub = None
    if policy.separate_sub_lm:
        sub = build_lm(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            max_tokens=policy.sub_max_tokens,
            thinking=policy.sub_thinking,
            temperature=policy.temperature,
        )
    return root, sub


@dataclass(frozen=True)
class LmUsage:
    calls: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * ZAI_INPUT_USD_PER_MILLION
            + self.output_tokens * ZAI_OUTPUT_USD_PER_MILLION
        ) / 1_000_000

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd_api_equivalent": round(self.cost_usd, 6),
        }


def _usage_field(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    value = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
    return int(value or 0)


def lm_usage(lm: dspy.LM | None) -> LmUsage:
    """Aggregate usage from an LM's request history (never the prompt text)."""
    if lm is None:
        return LmUsage(0, 0, 0, 0)
    calls = input_tokens = output_tokens = reasoning_tokens = 0
    for entry in lm.history:
        if not isinstance(entry, dict):
            continue
        calls += 1
        usage = entry.get("usage")
        input_tokens += _usage_field(usage, "prompt_tokens")
        output_tokens += _usage_field(usage, "completion_tokens")
        details = (
            usage.get("completion_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens_details", None)
        )
        reasoning_tokens += _usage_field(details, "reasoning_tokens")
    return LmUsage(calls, input_tokens, output_tokens, reasoning_tokens)


class RlmBudgetExceeded(RuntimeError):
    """Raised when the API-equivalent cost ceiling is crossed mid-trajectory."""


class MarkerHistory(REPLHistory):
    """REPL history rendered with dspy's own field markers.

    dspy's stock rendering ("Reasoning: ...\\nCode:\\n```python") is what
    GLM-5.3-Flash mirrors when it drifts off the ``[[ ## field ## ]]`` markers
    the ChatAdapter parses. Rendering past turns the way dspy renders few-shot
    demos makes mirroring produce a parseable response.
    """

    def format(self) -> str:
        if not self.entries:
            return "You have not interacted with the REPL environment yet."
        blocks = []
        for index, entry in enumerate(self.entries):
            reasoning = f"[[ ## reasoning ## ]]\n{entry.reasoning}\n" if entry.reasoning else ""
            blocks.append(
                f"=== Step {index + 1} ===\n{reasoning}[[ ## code ## ]]\n```python\n{entry.code}\n```\n"
                f"[[ ## output ## ]]\n{REPLEntry.format_output(entry.output, self.max_output_chars)}"
            )
        return "\n".join(blocks)


class LabRlm(dspy.RLM):
    """``dspy.RLM`` with an ``RlmPolicy`` applied."""

    def __init__(
        self,
        signature: str,
        policy: RlmPolicy,
        *,
        tools: list[Callable[..., Any]] | None = None,
        sub_lm: dspy.LM | None = None,
        root_lm: dspy.LM | None = None,
        cost_limit_usd: float | None = None,
        interpreter_factory: Callable[[], CodeInterpreter] = PythonInterpreter,
        verbose: bool = False,
    ) -> None:
        parts = [] if policy.action_instructions_override else [policy.instruction_addendum]
        if tools and policy.environment_addendum:
            parts.append(policy.environment_addendum)
        instructions = "\n\n".join(part for part in parts if part) or None
        sig = dspy.Signature(signature, instructions)
        super().__init__(
            sig,
            max_iters=policy.max_iters,
            max_llm_calls=policy.max_llm_calls,
            max_output_chars=policy.max_output_chars,
            verbose=verbose,
            tools=tools,
            sub_lm=sub_lm,
            interpreter_factory=interpreter_factory,
        )
        if policy.action_instructions_override:
            override = policy.action_instructions_override
            if tools and policy.environment_addendum:
                override = override + "\n\n" + policy.environment_addendum
            self.generate_action.signature = self.generate_action.signature.with_instructions(
                override
            )
        self.policy = policy
        self._root_lm = root_lm
        self._cost_limit_usd = cost_limit_usd
        self.budget_stopped = False
        self.iteration_wall_seconds: list[float] = []
        self.parse_failures = 0
        self.salvaged_actions = 0

    # -- policy mechanics -------------------------------------------------

    def _history_view(self, history: REPLHistory) -> REPLHistory:
        window = self.policy.history_window
        entries: list[REPLEntry]
        if window is None or len(history) <= window:
            entries = list(history.entries)
        else:
            cutoff = len(history) - window
            entries = []
            for index, entry in enumerate(history.entries):
                if index < cutoff:
                    entries.append(
                        REPLEntry(
                            reasoning=entry.reasoning,
                            code=entry.code,
                            output=MASKED_OUTPUT_MARKER.format(chars=len(entry.output)),
                        )
                    )
                else:
                    entries.append(entry)
        if self.policy.history_style == "markers":
            return MarkerHistory(entries=entries, max_output_chars=history.max_output_chars)
        if window is None or len(history) <= window:
            return history
        return REPLHistory(entries=entries, max_output_chars=history.max_output_chars)

    def _iteration_label(self, iteration: int) -> str:
        label = f"{iteration + 1}/{self.max_iters}"
        if not self.policy.iteration_reminder:
            return label
        remaining = self.max_iters - iteration - 1
        if remaining == 0:
            return f"{label} (this is your LAST iteration: call SUBMIT now with your best answer)"
        return f"{label} ({remaining} iterations remain after this one; SUBMIT before they run out)"

    def _spent_usd(self) -> float:
        total = lm_usage(self._root_lm).cost_usd
        if self.sub_lm is not None and self.sub_lm is not self._root_lm:
            total += lm_usage(self.sub_lm).cost_usd
        return total

    # -- dspy.RLM overrides -------------------------------------------------

    def _execute_iteration(
        self,
        repl: CodeInterpreter,
        variables: list[REPLVariable],
        history: REPLHistory,
        iteration: int,
        input_args: dict[str, Any],
        output_field_names: list[str],
    ) -> Prediction | REPLHistory:
        if (
            self._cost_limit_usd is not None
            and self._root_lm is not None
            and self._spent_usd() >= self._cost_limit_usd
        ):
            self.budget_stopped = True
            logger.warning(
                "RLM policy %s stopped at iteration %d: API-equivalent spend %.4f >= limit %.4f",
                self.policy.policy_id,
                iteration + 1,
                self._spent_usd(),
                self._cost_limit_usd,
            )
            return self._extract_fallback(variables, history, output_field_names)

        started = time.monotonic()
        variables_info = [variable.format() for variable in variables]
        try:
            with dspy.context(adapter=_ACTION_ADAPTER):
                action = self.generate_action(
                    variables_info=variables_info,
                    repl_history=self._history_view(history),
                    iteration=self._iteration_label(iteration),
                )
        except AdapterParseError as exc:
            # GLM-5.3-Flash frequently mirrors the rendered REPL history
            # ("Reasoning: ... Code: ```python ...```") instead of dspy's
            # "[[ ## field ## ]]" markers. Under ``lenient_parse`` the fenced code
            # and its preamble are salvaged as the action (no extra LM call);
            # otherwise the turn is recorded as a recoverable observation so the
            # model can retry on the next iteration instead of the trial dying
            # (dspy's JSON fallback returned "{}" and killed the run).
            raw_full = str(getattr(exc, "lm_response", "") or "")
            salvaged = salvage_action(raw_full) if self.policy.lenient_parse else None
            if salvaged is not None:
                self.salvaged_actions += 1
                action = Prediction(reasoning=salvaged[0], code=salvaged[1])
            else:
                self.parse_failures += 1
                raw = raw_full[:1500]
                self.iteration_wall_seconds.append(time.monotonic() - started)
                return history.append(
                    reasoning="",
                    code="# (no code executed: previous response was not parseable)",
                    output=(
                        "[Error] Your previous response could not be parsed into the required "
                        "`reasoning` and `code` fields. Reply again using exactly the field "
                        "markers `[[ ## reasoning ## ]]` and `[[ ## code ## ]]`, then "
                        "`[[ ## completed ## ]]`. Unparsed response head: " + repr(raw)
                    ),
                )
        if self.verbose:
            logger.info(
                "RLM iteration %d/%d\nReasoning: %s\nCode:\n%s",
                iteration + 1,
                self.max_iters,
                action.reasoning,
                action.code,
            )
        try:
            code = _strip_code_fences(action.code)
        except SyntaxError as exc:
            code = action.code
            result: Any = f"[Error] {format_error_for_lm(exc)}"
            outcome = self._process_execution_result(
                action, code, result, history, output_field_names
            )
        else:
            result = self._execute_code(repl, code, input_args)
            outcome = self._process_execution_result(
                action, code, result, history, output_field_names
            )
        self.iteration_wall_seconds.append(time.monotonic() - started)
        return outcome


def _strip_code_fences(code: str) -> str:
    from dspy.predict.rlm import _strip_code_fences as strip  # ty: ignore[unresolved-import]

    return strip(code)


@dataclass(frozen=True)
class RlmRunResult:
    """Everything one RLM execution produced, in a JSON-friendly shape."""

    outputs: dict[str, Any]
    trajectory: list[dict[str, Any]]
    final_reasoning: str
    root_usage: LmUsage
    sub_usage: LmUsage
    wall_seconds: float
    iterations: int
    budget_stopped: bool
    parse_failures: int
    salvaged_actions: int
    error: str | None

    @property
    def cost_usd(self) -> float:
        return self.root_usage.cost_usd + self.sub_usage.cost_usd

    def to_json(self) -> dict[str, Any]:
        return {
            "outputs": self.outputs,
            "final_reasoning": self.final_reasoning,
            "root_usage": self.root_usage.to_json(),
            "sub_usage": self.sub_usage.to_json(),
            "cost_usd_api_equivalent": round(self.cost_usd, 6),
            "wall_seconds": round(self.wall_seconds, 3),
            "iterations": self.iterations,
            "budget_stopped": self.budget_stopped,
            "parse_failures": self.parse_failures,
            "salvaged_actions": self.salvaged_actions,
            "error": self.error,
        }


def run_rlm(
    rlm: LabRlm,
    root_lm: dspy.LM,
    sub_lm: dspy.LM | None,
    **inputs: Any,
) -> RlmRunResult:
    """Execute ``rlm`` under ``root_lm`` and return a serialisable result.

    Exceptions from the LM, interpreter, or budget are captured into ``error``
    so a batch of tasks never aborts on one failure; usage is still reported.
    """
    started = time.monotonic()
    outputs: dict[str, Any] = {}
    trajectory: list[dict[str, Any]] = []
    final_reasoning = ""
    error: str | None = None
    try:
        with dspy.context(lm=root_lm, track_usage=True):
            prediction = rlm(**inputs)
        for name in rlm.signature.output_fields:
            outputs[name] = prediction.get(name)
        trajectory = list(getattr(prediction, "trajectory", None) or [])
        final_reasoning = str(getattr(prediction, "final_reasoning", "") or "")
    except Exception as exc:  # noqa: BLE001 - captured into the result row on purpose
        error = f"{type(exc).__name__}: {exc}"[:2000]
    return RlmRunResult(
        outputs=outputs,
        trajectory=trajectory,
        final_reasoning=final_reasoning,
        root_usage=lm_usage(root_lm),
        sub_usage=lm_usage(sub_lm) if sub_lm is not None else LmUsage(0, 0, 0, 0),
        wall_seconds=time.monotonic() - started,
        iterations=len(trajectory),
        budget_stopped=rlm.budget_stopped,
        parse_failures=rlm.parse_failures,
        salvaged_actions=rlm.salvaged_actions,
        error=error,
    )
