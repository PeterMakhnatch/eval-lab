"""Secret-safe Harbor Terminus2 adapter for pinned host-side model routes.

Upstream Terminus2 runs its model client host-side (inside the Harbor
controller process) through LiteLLM, including the main terminal loop, the
context-summarization subcalls, and the LiteLLM retry path. This subclass
binds that client to the trial-owned ``127.0.0.1`` metered proxy or an
explicit local Ollama service, refusing caller transport/credential overrides.

Credential posture:

- The real provider key lives only in the existing owner-only secret file
  read by the trusted proxy. The harness holds a per-trial capability.
- The capability reaches litellm's ``zai`` provider lookup through the
  controller process environment (``ZAI_API_KEY``) set here at construction
  time. It is never placed in ``llm_kwargs``/``llm_call_kwargs``: upstream
  Terminus2 persists ``llm_kwargs`` verbatim into the ATIF trajectory
  (``agent.extra.llm_kwargs``), so any secret there would land in evidence.
- ``extra_env`` is exported into the task's tmux session, i.e. inside the
  task container. It must never carry provider secrets or the trial
  capability; construction fails closed when it does.
- The local route requires an already installed, digest-identified GGUF model.
  It never pulls weights and records zero provider API charge, not imputed usage.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

from harbor.agents.terminus_2.terminus_2 import Terminus2  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    REEF_SCENARIO_ENV,
    TERMINUS_LOCAL_MODEL_SELECTOR,
    TERMINUS_PROXY_URL_ENV,
    ZAI_OPENAPI_ALLOWED_MODELS,
    ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
    ZAI_OPENAPI_PROXY_TOKEN,
    collected_secret_values,
)
from evallab.harbor_common import sanitize_native_trajectory
from evallab.terminus_local import OllamaBinding, resolve_ollama_binding

__all__ = ["SecretSafeTerminus2"]

#: litellm resolves the ``zai`` provider key from this process-environment name.
_PROVIDER_KEY_ENV = "ZAI_API_KEY"

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
_FORBIDDEN_EXTRA_ENV_PREFIXES = ("evallab_zai_openapi", "evallab_terminus", "evallab_zai_")


def _require_exact_model(model_name: str | None) -> str:
    """Return ``model_name`` only when it is the qualified exact GLM route."""
    if model_name != ZAI_OPENAPI_MODEL_SELECTOR or model_name not in ZAI_OPENAPI_ALLOWED_MODELS:
        raise ValueError(
            "SecretSafeTerminus2 requires the qualified exact model "
            f"{ZAI_OPENAPI_MODEL_SELECTOR!r}, got {model_name!r}"
        )
    return model_name


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


def _require_capability() -> str:
    """Return the trial capability, failing closed on absence or placeholder."""
    capability = os.environ.get(ZAI_OPENAPI_PROXY_CAPABILITY_ENV, "")
    if not capability or capability == ZAI_OPENAPI_PROXY_TOKEN:
        raise ValueError(
            "SecretSafeTerminus2 requires a bound trial capability in "
            f"{ZAI_OPENAPI_PROXY_CAPABILITY_ENV}"
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
        self._reef_scenario: str | None = None
        if model_name == TERMINUS_LOCAL_MODEL_SELECTOR:
            self._local_binding = resolve_ollama_binding(model_name)
            model = model_name
        else:
            model = _require_exact_model(model_name)
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
            reef_scenario = os.environ.get(REEF_SCENARIO_ENV)
            if reef_scenario:
                # HAR-74: the same HAR-70 loopback proxy slot, forwarding
                # through Reef's capture proxy. The bearer token lives in the
                # runner process; this child holds only the proxy URL.
                self._reef_scenario = reef_scenario
                proxy_url = _require_loopback_proxy_url()
            else:
                proxy_url = self._local_binding.endpoint
            capability = None
            kwargs["model_info"] = {
                "max_input_tokens": self._local_binding.context_budget_tokens,
                "max_output_tokens": self._local_binding.context_budget_tokens,
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "litellm_provider": "ollama_chat",
            }
        else:
            proxy_url = _require_loopback_proxy_url()
            capability = _require_capability()
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
        # Bind the capability for litellm's zai provider key lookup. This
        # stays in the controller process environment only: it is never added
        # to llm_kwargs (trajectory-persisted), llm_call_kwargs, extra_env
        # (task container), or any task exec call.
        if capability is not None:
            os.environ[_PROVIDER_KEY_ENV] = capability

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        try:
            await super().run(instruction, environment, context)
        finally:
            if self._local_binding is not None:
                if self._reef_scenario is None:
                    # Installed local inference has no provider API charge. This is
                    # a billing fact, not invented missing token or call telemetry.
                    context.cost_usd = 0.0
                    context.metadata = {
                        **(context.metadata or {}),
                        "local_ollama": self._local_binding.to_dict(),
                    }
                else:
                    # HAR-74: Reef owns metering server-side; Eval Lab claims no
                    # cost here and records the scenario instead. Weight
                    # provenance still comes from the qualified local binding.
                    context.metadata = {
                        **(context.metadata or {}),
                        "local_ollama": self._local_binding.to_dict(),
                        "reef_traffic": {"scenario": self._reef_scenario},
                    }

    def populate_context_post_run(self, context: Any) -> None:
        secrets = collected_secret_values()
        logs = Path(self.logs_dir)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        for continuation in sorted(logs.glob("trajectory.cont-*.json")):
            sanitize_native_trajectory(continuation, secrets)
        super().populate_context_post_run(context)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        for continuation in sorted(logs.glob("trajectory.cont-*.json")):
            sanitize_native_trajectory(continuation, secrets)
