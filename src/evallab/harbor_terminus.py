"""Secret-safe Harbor Terminus2 adapter for pinned host-side model routes.

Upstream Terminus2 runs its model client host-side (inside the Harbor
controller process) through LiteLLM, including the main terminal loop, the
context-summarization subcalls, and the LiteLLM retry path. This subclass
binds that client to the trial-owned ``127.0.0.1`` metered proxy or an
explicit local Ollama service, refusing caller transport/credential overrides.

Credential posture:

- The real provider key lives only in the existing owner-only secret file
- The capability reaches litellm's provider key lookup (``ZAI_API_KEY`` for
  Z.ai routes, ``OPENAI_API_KEY`` for Tinker routes) through the controller
  process environment set here at construction time. It is never placed in
  ``llm_kwargs``/``llm_call_kwargs``: upstream Terminus2 persists
  ``llm_kwargs`` verbatim into the ATIF trajectory
  (``agent.extra.llm_kwargs``), so any secret there would land in evidence.
- ``extra_env`` is exported into the task's tmux session, i.e. inside the
  task container. It must never carry provider secrets or the trial
  capability; construction fails closed when it does.
- The local route requires an already installed, digest-identified GGUF model.
  It never pulls weights and records zero provider API charge, not imputed usage.

Episode endings:

- A trial ceiling trip (the proxy's 429 "trial budget exhausted") ends the
  agent phase as :class:`TrialBudgetExhaustedError`. Harbor records it like an
  agent timeout and still runs the verifier; the agent metadata records
  ``stop_reason: trial_budget_exhausted``.
- On the self-hosted MiMo route, a prose-only turn that finished with
  ``finish_reason: stop`` counts as ``task_complete`` (see
  :mod:`evallab.mimo_tool_calls`). Each mapped agent step carries
  ``extra.prose_completion: true``; each trajectory file's
  ``final_metrics.extra.prose_completions`` and the agent metadata's
  ``prose_completions`` count them, including on the agent-timeout path.

Step layers (HAR-92, recording only):

- Every agent step records ``extra.step_layers`` with what the model
  proposed, what the parser accepted, what executed, and what came back
  (see :mod:`evallab.step_layers`). Raw messages, observations, and rollout
  details are untouched; parser decisions are identical.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import NonZeroAgentExitCodeError  # ty: ignore[unresolved-import]
from harbor.agents.terminus_2.terminus_2 import Terminus2  # ty: ignore[unresolved-import]
from harbor.llms.lite_llm import LiteLLM  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    MIMO_SELFHOSTED_CONTEXT_TOKENS,
    MIMO_SELFHOSTED_MODEL_PREFIX,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    MIMO_SELFHOSTED_PROXY_TOKEN,
    MIMO_SELFHOSTED_TEMPERATURE,
    TERMINUS_LOCAL_MODEL_SELECTOR,
    TERMINUS_PROXY_URL_ENV,
    TINKER_CONTEXT_TOKENS,
    TINKER_MODEL_PREFIX,
    TINKER_PROXY_CAPABILITY_ENV,
    TINKER_PROXY_TOKEN,
    ZAI_OPENAPI_ALLOWED_MODELS,
    ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
    ZAI_OPENAPI_PROXY_TOKEN,
    TinkerModelSpec,
    collected_secret_values,
    parse_mimo_selfhosted_model,
    parse_tinker_model,
)
from evallab.harbor_common import sanitize_native_trajectory
from evallab.mimo_tool_calls import MimoToolCallParser
from evallab.step_layers import (
    STEP_LAYERS_KEY,
    attach_layers,
    build_recorded_layers,
    copied_layers,
)
from evallab.terminus_local import OllamaBinding, resolve_ollama_binding

__all__ = [
    "SecretSafeTerminus2",
    "TrialBudgetExhaustedError",
    "apply_mimo_blocklist",
    "mimo_replay_parser",
]

#: The metered proxy's 429 body when a trial ceiling (requests, tokens or
#: cost) is spent (``containers/zai_openapi_secret_proxy.py``).
TRIAL_BUDGET_EXHAUSTED_MESSAGE = "trial budget exhausted"
#: The agent-metadata stop reason recorded for a ceiling trip.
TRIAL_BUDGET_EXHAUSTED_STOP_REASON = "trial_budget_exhausted"
#: The ``Step.extra`` flag marking a prose-only turn mapped to task_complete.
PROSE_COMPLETION_STEP_FLAG = "prose_completion"


class TrialBudgetExhaustedError(NonZeroAgentExitCodeError):
    """The trial proxy refused a model call because a ceiling is spent.

    Harbor's single-step trial records only an agent timeout or an agent exit
    error and then runs the verifier; any other agent exception skips it and
    loses the reward. Subclassing the exit error routes a ceiling trip there,
    and the distinct class name keeps it apart in ``exception_info``.
    """


def _is_trial_budget_exhausted(exc: BaseException) -> bool:
    """Whether ``exc`` or an exception it wraps is the proxy's ceiling 429."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if TRIAL_BUDGET_EXHAUSTED_MESSAGE in str(current):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class _FinishReasonLiteLLM(LiteLLM):
    """LiteLLM that remembers the finish reason of its latest completion.

    Upstream's ``LLMResponse`` drops ``finish_reason``; the MiMo prose rule
    needs it. Upstream reads usage before it raises on ``length``, so every
    completion that returned a body sets the value, and a call that failed
    before any body leaves ``None``.
    """

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
    """Write the file's count of flagged steps into its final metrics."""
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


def mimo_replay_parser() -> Any:
    """Stock Terminus JSON parser wrapped as a HAR-92 replay callback.

    Offline layer reconstruction replays the raw (or normalized) response
    text through this — the same stock parser the live
    :class:`evallab.mimo_tool_calls.MimoToolCallParser` delegates to — so
    replayed decisions are identical by construction. The MiMo prose rule is
    deliberately NOT applied here: reconstruction consults the recorded
    ``prose_completion`` step flag instead of assuming a finish reason.
    Durations mirror upstream's ``min(duration, 60)`` cap
    (``terminus_2.py`` ``_handle_llm_interaction``); the Enter keystrokes the
    live parser appends are applied by the caller
    (:func:`evallab.step_layers.executed_keystrokes` path), so commands here
    carry the parsed text verbatim.
    """
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


#: Marks "no prose completion awaiting its trajectory step".
_NO_PENDING_PROSE_STEP = object()

#: Where MiMo task setup stages FineEnvs' answer-leak blocklist. Task setup
#: leaves the list here; the agent appends it to /etc/hosts right after it
#: installs (the install itself needs github.com). Mirrors
#: ``agents/mimo_opencode.py`` ``MimoOpenCode.install`` in the FineEnvs
#: Harbor ports (``agents/mimo_opencode.py:63-71``). Terminal and music tasks
#: never stage this file (their ``setup.sh`` defines ``write_blocklist`` but
#: has no ``files/blocklist`` to copy and never calls it), so the apply step
#: is a no-op there by design.
MIMO_BLOCKLIST_PATH = "/var/lib/mimo/blocklist"


async def apply_mimo_blocklist(environment: Any) -> str:
    """Append the FineEnvs answer-leak blocklist to /etc/hosts, failing closed.

    Runs only when ``/var/lib/mimo/blocklist`` exists (code/cyber/general
    tasks stage it in setup); otherwise returns ``"none for this task"``
    without touching ``/etc/hosts``. A nonzero ``exec`` fails the trial
    setup loudly instead of running the agent unleaked. HAR-83: flag for
    HAR-81 — our Terminus-2 path never applied this until now.
    """
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

#: litellm resolves each provider's key from this process-environment name.
#: The capability token (never the provider key) is what lands here.
_PROVIDER_KEY_ENVS = {"zai": "ZAI_API_KEY", "tinker": "OPENAI_API_KEY", "mimo_selfhosted": "OPENAI_API_KEY"}

#: The loopback interface the runner binds the trial proxy to. Hostnames that
#: merely resolve to loopback (``localhost``) are rejected: the binding must be
#: the literal runner-owned endpoint.
_LOOPBACK_HOST = "127.0.0.1"

#: Substrings that mark a caller-supplied LLM kwarg as a transport/credential
#: override (covers ``api_key``, ``OPENAI_API_KEY``, ``api_base``,
#: ``OPENAI_BASE_URL``, ``base_url``, ``AZURE_API_BASE``, ...).
_FORBIDDEN_KWARG_SUBSTRINGS = ("api_key", "api_base", "base_url")

#: Extra task-container environment names that are never admitted. ``extra_env``
#: is exported into the task's tmux session, so anything credential-shaped or
#: proxy-shaped fails closed here.
_FORBIDDEN_EXTRA_ENV_KEYS = frozenset(
    {
        "mswea_api_key",
        "zai_api_key",
        "zai_api_base",
        "openai_api_key",
        "openai_base_url",
        "openai_api_base",
        "anthropic_api_key",
        "anthropic_base_url",
        *(
            str(key).casefold()
            for key in ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS
        ),
    }
)
_FORBIDDEN_EXTRA_ENV_PREFIXES = (
    "evallab_zai_openapi",
    "evallab_tinker",
    "evallab_mimo_selfhosted",
    "evallab_terminus",
    "evallab_zai_",
)


def _resolve_metered_model(model_name: str | None) -> tuple[str, TinkerModelSpec | None, bool]:
    """Return the validated model string, its Tinker spec, and the MiMo flag.

    Z.ai routes must be one of the exact admitted selectors. Tinker routes
    are parsed strictly (fail-closed on unknown bases and malformed
    checkpoints) so the selector string fully identifies the sampled weights.
    The self-hosted MiMo route admits exactly one selector; anything else
    under ``selfhosted/`` fails closed.
    """
    if isinstance(model_name, str) and model_name.startswith(MIMO_SELFHOSTED_MODEL_PREFIX):
        parse_mimo_selfhosted_model(model_name)
        return model_name, None, True
    if isinstance(model_name, str) and model_name.startswith(TINKER_MODEL_PREFIX):
        spec = parse_tinker_model(model_name)
        return model_name, spec, False
    if model_name in ZAI_OPENAPI_ALLOWED_MODELS:
        return model_name, None, False
    raise ValueError(
        "SecretSafeTerminus2 requires an exact metered model: one of "
        f"{sorted(ZAI_OPENAPI_ALLOWED_MODELS)}, a Tinker route "
        "'tinker/<base>[@tinker://<run>:train:<i>/sampler_weights/<step>]', "
        f"or the self-hosted route {MIMO_SELFHOSTED_MODEL_SELECTOR!r}; "
        f"got {model_name!r}. Coding Plan credentials are not admitted."
    )


def _require_loopback_proxy_url() -> str:
    """Return the runner-bound loopback proxy URL, failing closed otherwise."""
    raw = os.environ.get(TERMINUS_PROXY_URL_ENV, "")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            f"{TERMINUS_PROXY_URL_ENV} is not a valid proxy endpoint"
        ) from exc
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
    """Return the trial capability, failing closed on absence or placeholder."""
    capability = os.environ.get(env_name, "")
    if not capability or capability == placeholder:
        raise ValueError(
            "SecretSafeTerminus2 requires a bound trial capability in "
            f"{env_name}"
        )
    return capability


def _reject_kwarg_overrides(
    values: dict[str, Any] | None, *, source: str
) -> dict[str, Any]:
    """Reject transport/credential overrides inside one caller-supplied mapping."""
    items = dict(values or {})
    for key in items:
        folded = str(key).casefold()
        if any(fragment in folded for fragment in _FORBIDDEN_KWARG_SUBSTRINGS):
            raise ValueError(
                f"SecretSafeTerminus2 rejects {source} transport override {key!r}: "
                "model transport is bound by the trial proxy"
            )
    return items


def _scrubbed_extra_env(extra_env: dict[str, str] | None, *, capability: str | None) -> dict[str, str]:
    """Reject secret-bearing task-container environment; pass through the rest."""
    env = dict(extra_env or {})
    secrets = collected_secret_values()
    if capability is not None:
        secrets = secrets | {capability}
    for key, value in env.items():
        folded = str(key).casefold()
        if folded in _FORBIDDEN_EXTRA_ENV_KEYS or folded.startswith(
            _FORBIDDEN_EXTRA_ENV_PREFIXES
        ):
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


class SecretSafeTerminus2(Terminus2):
    """Terminus2 with either a metered proxy or a qualified local Ollama client.

    The terminal loop, parser, summarization, retries and artifact capture are
    upstream behavior. Model transport is controller-bound; provider secrets
    never enter trajectory-persisted kwargs or the task environment.

    ``model_name`` is keyword-only: upstream binds it positionally, but this
    adapter must validate the exact route before delegating, so a second
    positional can never smuggle an unvalidated model past the guard.
    """

    def __init__(
        self,
        *args: Any,
        model_name: str | None = None,
        api_base: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if len(args) > 1:
            raise ValueError(
                "SecretSafeTerminus2 accepts at most logs_dir positionally; "
                "pass model_name as a keyword argument"
            )
        self._local_binding: OllamaBinding | None = None
        self._tinker_spec: TinkerModelSpec | None = None
        self._mimo_selfhosted = False
        self._prose_completions = 0
        self._pending_prose_step: Any = _NO_PENDING_PROSE_STEP
        # HAR-92 step layers: one episode record per LLM turn, queued in
        # episode order until the appended agent step is annotated at dump.
        self._pending_layer: dict[str, Any] | None = None
        self._layer_queue: list[dict[str, Any]] = []
        self._layer_steps_id: int | None = None
        self._layer_annotated: int = 0
        if model_name == TERMINUS_LOCAL_MODEL_SELECTOR:
            self._local_binding = resolve_ollama_binding(model_name)
            model = model_name
        else:
            model, self._tinker_spec, self._mimo_selfhosted = _resolve_metered_model(model_name)
        if api_base is not None:
            raise ValueError(
                "SecretSafeTerminus2 rejects api_base overrides: "
                "model transport is bound by the trial proxy"
            )
        backend = kwargs.get("llm_backend")
        if backend is not None and getattr(backend, "value", backend) != "litellm":
            raise ValueError(
                f"SecretSafeTerminus2 requires the litellm backend, got {backend!r}"
            )
        for key in kwargs:
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_KWARG_SUBSTRINGS):
                raise ValueError(
                    f"SecretSafeTerminus2 rejects transport override {key!r}: "
                    "model transport is bound by the trial proxy"
                )
        clean_llm_kwargs = _reject_kwarg_overrides(llm_kwargs, source="llm_kwargs")
        clean_llm_call_kwargs = _reject_kwarg_overrides(
            llm_call_kwargs, source="llm_call_kwargs"
        )
        if self._local_binding is not None:
            if kwargs.get("model_info") is not None:
                raise ValueError("local model context/pricing is runtime-bound, not a harness override")
            proxy_url = self._local_binding.endpoint
            capability = None
            provider = None
            kwargs["model_info"] = {
                "max_input_tokens": self._local_binding.context_budget_tokens,
                "max_output_tokens": self._local_binding.context_budget_tokens,
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "litellm_provider": "ollama_chat",
            }
        else:
            proxy_url = _require_loopback_proxy_url()
            if self._mimo_selfhosted:
                # Context/pricing of the served MiMo weights is runtime-bound:
                # the 64K window drives native context summarization before
                # overflow, and zero per-token prices mark the time-billed
                # route (GPU hours, not tokens). A caller-supplied model_info
                # can never override it.
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
                # The trajectory must record the real sampling the proxy
                # enforces; the proxy still overrides any caller value.
                kwargs["temperature"] = MIMO_SELFHOSTED_TEMPERATURE
                capability = _require_capability(
                    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, MIMO_SELFHOSTED_PROXY_TOKEN
                )
                provider = "mimo_selfhosted"
            elif self._tinker_spec is not None:
                # Context/pricing of the pinned Tinker base is runtime-bound:
                # the 64K window drives native context summarization before
                # overflow, and the table price keeps native cost estimates
                # honest. A caller-supplied model_info can never override it.
                if kwargs.get("model_info") is not None:
                    raise ValueError(
                        "Tinker model context/pricing is runtime-bound, not a harness override"
                    )
                spec = self._tinker_spec
                kwargs["model_info"] = {
                    "max_input_tokens": TINKER_CONTEXT_TOKENS,
                    "max_output_tokens": TINKER_CONTEXT_TOKENS,
                    "input_cost_per_token": spec.input_cost_micros_per_million / 1e6,
                    "output_cost_per_token": spec.output_cost_micros_per_million / 1e6,
                    "litellm_provider": "openai",
                }
                capability = _require_capability(
                    TINKER_PROXY_CAPABILITY_ENV, TINKER_PROXY_TOKEN
                )
                provider = "tinker"
            else:
                capability = _require_capability(
                    ZAI_OPENAPI_PROXY_CAPABILITY_ENV, ZAI_OPENAPI_PROXY_TOKEN
                )
                provider = "zai"
        clean_extra_env = _scrubbed_extra_env(extra_env, capability=capability)
        super().__init__(
            *args,
            model_name=model,
            api_base=proxy_url,
            llm_kwargs=clean_llm_kwargs,
            llm_call_kwargs=clean_llm_call_kwargs,
            extra_env=clean_extra_env,
            **kwargs,
        )
        # Bind the capability for litellm's provider key lookup. This stays in
        # the controller process environment only: it is never added to
        # llm_kwargs (trajectory-persisted), llm_call_kwargs, extra_env (task
        # container), or any task exec call.
        if capability is not None and provider is not None:
            os.environ[_PROVIDER_KEY_ENVS[provider]] = capability

    def _init_llm(self, *args: Any, **kwargs: Any) -> Any:
        llm = super()._init_llm(*args, **kwargs)
        if self._mimo_selfhosted:
            if type(llm) is not LiteLLM:
                raise ValueError(
                    "the MiMo prose rule needs upstream's LiteLLM client, got "
                    + type(llm).__name__
                )
            # The subclass only adds last_finish_reason. Re-classing the
            # client upstream built keeps its constructor semantics exactly.
            llm.__class__ = _FinishReasonLiteLLM
        return llm

    def _get_parser(self) -> Any:
        # MiMo's native exec_command calls become Terminus commands, and a
        # prose-only turn that stopped naturally counts as task_complete
        # (HAR-90). The parser sees normalized text; the chat, trajectory and
        # rollout details keep the raw model output.
        parser = super()._get_parser()
        if self._mimo_selfhosted and self._parser_name == "json":
            return MimoToolCallParser(parser, finish_reason=lambda: self._llm.last_finish_reason)
        return parser

    def _reset_per_run_state(self) -> None:
        super()._reset_per_run_state()
        self._prose_completions = 0
        self._pending_prose_step = _NO_PENDING_PROSE_STEP
        self._pending_layer = None
        self._layer_queue = []
        self._layer_steps_id = None
        self._layer_annotated = 0

    async def _handle_llm_interaction(self, *args: Any, **kwargs: Any) -> Any:
        # A previous turn that never executed (a parse error appends its step
        # without touching the terminal) still deserves its layers: flush it
        # as not-executed so episode records stay aligned with appended steps.
        self._flush_pending_layer("parse_error: nothing executed")
        outcome = await super()._handle_llm_interaction(*args, **kwargs)
        commands, is_task_complete, feedback, _analysis, _plan, llm_response = outcome
        parser = self._parser
        if isinstance(parser, MimoToolCallParser) and parser.last_prose_completion:
            # Upstream appends this turn's agent step later in the episode.
            # Remembering the current last step lets the flag land only on a
            # step appended after this parse.
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
            "commands": [
                (command.keystrokes, command.duration_sec) for command in commands
            ],
            "task_complete": bool(is_task_complete),
            "parse_error": parse_error,
            "exec": None,
        }
        return outcome

    async def _execute_commands(self, commands: Any, session: Any) -> Any:
        timeout, output = await super()._execute_commands(commands, session)
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
        return timeout, output

    def _flush_pending_layer(self, reason: str) -> None:
        """Queue a turn that never reached execution (parse error, interrupt)."""
        pending = self._pending_layer
        if pending is None:
            return
        pending["exec"] = None
        pending["not_executed_reason"] = reason
        self._layer_queue.append(pending)
        self._pending_layer = None

    def _annotate_step_layers(self) -> None:
        """Attach queued episode records to newly appended agent steps, in order.

        In ``raw_content`` mode the step message is the raw model text, so a
        record is consumed only when its message matches the step's: a turn
        lost between parse and step append drops its orphan record instead of
        misattaching it to the next turn. On other parsers steps are paired
        by order. Steps left without records stay layer-free here; consumers
        reconstruct them offline and label the provenance.

        Progress is a prefix count over the step list object: upstream appends
        in place, and a summarization split swaps in a fresh list, which
        restarts the scan. The queue is never cleared on a swap: a record
        queued at the split's own dump belongs to a step that lands in the
        new list, and already-layered steps carry their layers with them, so
        they are recognized and never consume a second record. A non-matching
        step (sanitized/redacted message, Harbor fallback) likewise leaves
        later records intact unless a later step provably matches them.
        Step object ids are never used for progress (a freed step's address
        can be reused by a later step); only the step list identity restarts
        the scan, and the layers themselves mark finished steps.
        """
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
                # Annotated before a list swap; never consume a second record.
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
                    # Sanitized/redacted/fallback step: no record is provably
                    # this turn's, so leave the queue intact for later steps
                    # and leave this step layer-free for offline rebuild.
                    self._layer_annotated += 1
                    continue
                # Records before the match belong to turns lost between parse
                # and step append; only those provably stale orphans drop.
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
        # Never flush the pending turn here: Harbor's summarization split
        # dumps through this path *between* a turn's parse and its execution
        # (``_split_trajectory_on_summarization`` runs before
        # ``_execute_commands`` for the same turn), so the turn is still live
        # and its executed layer must survive. Truly abandoned turns are
        # flushed in ``_dump_trajectory`` (cancel/timeout end of run) or at
        # the next ``_handle_llm_interaction`` (parse errors).
        self._annotate_step_layers()
        super()._dump_trajectory_with_continuation_index(continuation_index)

    def _dump_trajectory(self) -> None:
        # End-of-run (and per-episode) dump: a pending turn that never reached
        # execution is truly abandoned here — cancel/timeout ended the run, or
        # a parse-error turn never executes — so keep its proposed/accepted
        # layers with a not-executed executed layer. The per-episode call runs
        # after ``_execute_commands``, so a live pending never exists there.
        pending = self._pending_layer
        if pending is not None:
            self._flush_pending_layer(
                "parse_error: nothing executed"
                if pending.get("parse_error")
                else "episode interrupted before execution"
            )
        super()._dump_trajectory()

    def _flag_prose_completion_step(self) -> None:
        anchor = self._pending_prose_step
        if anchor is _NO_PENDING_PROSE_STEP or not self._trajectory_steps:
            return
        step = self._trajectory_steps[-1]
        # An error between the parse and the step append leaves the anchor
        # (or a system/user step) last; that turn has no step to flag.
        if step is anchor or step.source != "agent" or step.is_copied_context:
            return
        step.extra = {**(step.extra or {}), PROSE_COMPLETION_STEP_FLAG: True}
        self._prose_completions += 1
        self._pending_prose_step = _NO_PENDING_PROSE_STEP

    async def setup(self, environment: Any) -> None:
        await super().setup(environment)
        # HAR-83: FineEnvs' answer-leak blocklist is a task file the agent
        # must apply after install; stock Terminus2 never does. Fail closed
        # here (inside Harbor's agent-setup phase, before the agent runs) so
        # a trial can never run unleaked on a task that staged the list.
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
                # HAR-92: the trial-level prose count must reach consumers on
                # every path, including the agent-timeout path that never
                # reaches populate_context_post_run. This runs after Harbor's
                # final dump, so the counts land on the sealed files.
                for path in self._trajectory_files():
                    try:
                        _record_prose_completions(path)
                    except Exception:
                        continue
            if self._local_binding is not None:
                # Installed local inference has no provider API charge. This is
                # a billing fact, not invented missing token or call telemetry.
                context.cost_usd = 0.0
                metadata["local_ollama"] = self._local_binding.to_dict()
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
