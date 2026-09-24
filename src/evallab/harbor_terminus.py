"""Secret-safe Harbor Terminus2 adapter for the Z.ai OpenAPI standard-API lane.

Upstream Terminus2 runs its model client host-side (inside the Harbor
controller process) through LiteLLM, including the main terminal loop, the
context-summarization subcalls, and the LiteLLM retry path. This subclass
binds that client to the trial-owned ``127.0.0.1`` metered proxy and refuses
any caller-supplied transport or credential override.

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
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

from harbor.agents.terminus_2.terminus_2 import Terminus2  # ty: ignore[unresolved-import]

from evallab import execution_contracts as _contracts
from evallab.harbor_common import sanitize_native_trajectory

__all__ = [
    "REQUIRED_MODEL_SELECTOR",
    "SecretSafeTerminus2",
    "TERMINUS_AGENT",
    "TERMINUS_AGENT_IMPORT_PATH",
    "TERMINUS_PROXY_URL_ENV",
]

#: Canonical agent selector. Read from the shared contracts when the parent
#: integration has landed; the literal fallback matches it exactly.
TERMINUS_AGENT: str = getattr(_contracts, "TERMINUS_AGENT", "terminus-2")

#: Import path the Harbor CLI uses to construct this adapter.
TERMINUS_AGENT_IMPORT_PATH: str = getattr(
    _contracts,
    "TERMINUS_AGENT_IMPORT_PATH",
    "evallab.harbor_terminus:SecretSafeTerminus2",
)

#: Runner-bound loopback proxy endpoint consumed by this adapter.
TERMINUS_PROXY_URL_ENV: str = getattr(
    _contracts, "TERMINUS_PROXY_URL_ENV", "EVALLAB_TERMINUS_PROXY_URL"
)

#: The only admitted model route: qualified exact GLM standard-API selector.
REQUIRED_MODEL_SELECTOR: str = _contracts.ZAI_OPENAPI_MODEL_SELECTOR

_ALLOWED_MODELS: frozenset[str] = _contracts.ZAI_OPENAPI_ALLOWED_MODELS
_CAPABILITY_ENV: str = _contracts.ZAI_OPENAPI_PROXY_CAPABILITY_ENV
_PROXY_TOKEN_PLACEHOLDER: str = _contracts.ZAI_OPENAPI_PROXY_TOKEN
_CREDENTIAL_ENV_KEYS: frozenset[str] = _contracts.ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS

#: litellm resolves the ``zai`` provider key from this process-environment name.
_PROVIDER_KEY_ENV = "ZAI_API_KEY"

#: The loopback interface the runner binds the trial proxy to. Hostnames that
#: merely resolve to loopback (``localhost``) are rejected: the binding must be
#: the literal runner-owned endpoint.
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
        *(str(key).casefold() for key in _CREDENTIAL_ENV_KEYS),
    }
)
_FORBIDDEN_EXTRA_ENV_PREFIXES = ("evallab_zai_openapi", "evallab_terminus", "evallab_zai_")


def _require_exact_model(model_name: str | None) -> str:
    """Return ``model_name`` only when it is the qualified exact GLM route."""
    if model_name != REQUIRED_MODEL_SELECTOR or model_name not in _ALLOWED_MODELS:
        raise ValueError(
            "SecretSafeTerminus2 requires the qualified exact model "
            f"{REQUIRED_MODEL_SELECTOR!r}, got {model_name!r}"
        )
    return model_name


def _require_loopback_proxy_url() -> str:
    """Return the runner-bound loopback proxy URL, failing closed otherwise."""
    raw = os.environ.get(TERMINUS_PROXY_URL_ENV, "")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ValueError(
            f"{TERMINUS_PROXY_URL_ENV} is not a valid proxy endpoint"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != _LOOPBACK_HOST
        or parsed.port is None
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
    return f"http://{_LOOPBACK_HOST}:{parsed.port}"


def _require_capability() -> str:
    """Return the trial capability, failing closed on absence or placeholder."""
    capability = os.environ.get(_CAPABILITY_ENV, "")
    if not capability or capability == _PROXY_TOKEN_PLACEHOLDER:
        raise ValueError(
            f"SecretSafeTerminus2 requires a bound trial capability in {_CAPABILITY_ENV}"
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


def _scrubbed_extra_env(extra_env: dict[str, str] | None, *, capability: str) -> dict[str, str]:
    """Reject secret-bearing task-container environment; pass through the rest."""
    env = dict(extra_env or {})
    secrets = _contracts.collected_secret_values() | {capability}
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
    """Terminus2 bound to the trial-owned Z.ai OpenAPI metered proxy.

    The native terminal loop, summarization subcalls, raw-response/ATIF
    capture, and model identity are inherited unchanged from upstream. Only
    the model transport is pinned: ``api_base`` points at the runner-owned
    loopback proxy and the trial capability is exposed to litellm's ``zai``
    provider lookup via the controller process environment. No provider
    secret ever enters ``llm_kwargs`` (persisted into the trajectory),
    ``llm_call_kwargs``, ``extra_env`` (exported into the task container),
    or the task exec environment.
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
        if len(args) > 2:
            raise ValueError(
                "SecretSafeTerminus2 accepts at most (logs_dir, model_name) "
                "positionally: transport must be bound by keyword-free defaults"
            )
        effective_model = model_name if model_name is not None else kwargs.get("model_name")
        model = _require_exact_model(effective_model)
        if api_base is not None:
            raise ValueError(
                "SecretSafeTerminus2 rejects api_base overrides: "
                "model transport is bound by the trial proxy"
            )
        backend = kwargs.get("llm_backend", None)
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
        os.environ[_PROVIDER_KEY_ENV] = capability

    def populate_context_post_run(self, context: Any) -> None:
        secrets = _contracts.collected_secret_values()
        logs = Path(self.logs_dir)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        for continuation in sorted(logs.glob("trajectory.cont-*.json")):
            sanitize_native_trajectory(continuation, secrets)
        super().populate_context_post_run(context)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        for continuation in sorted(logs.glob("trajectory.cont-*.json")):
            sanitize_native_trajectory(continuation, secrets)
