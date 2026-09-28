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
"""

from __future__ import annotations

import os
import shlex
import urllib.parse
from pathlib import Path
from typing import Any

from harbor.agents.terminus_2.terminus_2 import Terminus2  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
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
    parse_tinker_model,
)
from evallab.harbor_common import sanitize_native_trajectory
from evallab.terminus_local import OllamaBinding, resolve_ollama_binding

__all__ = ["SecretSafeTerminus2", "apply_mimo_blocklist"]

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
_PROVIDER_KEY_ENVS = {"zai": "ZAI_API_KEY", "tinker": "OPENAI_API_KEY"}

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
    "evallab_terminus",
    "evallab_zai_",
)


class HostsBlocklistError(RuntimeError):
    """The pinned hosts blocklist could not be applied as an infrastructure step."""


def _resolve_metered_model(model_name: str | None) -> tuple[str, TinkerModelSpec | None]:
    """Return the validated model string plus its Tinker spec, if any.

    Z.ai routes must be one of the exact admitted selectors. Tinker routes
    are parsed strictly (fail-closed on unknown bases and malformed
    checkpoints) so the selector string fully identifies the sampled weights.
    """
    if isinstance(model_name, str) and model_name.startswith(TINKER_MODEL_PREFIX):
        spec = parse_tinker_model(model_name)
        return model_name, spec
    if model_name in ZAI_OPENAPI_ALLOWED_MODELS:
        return model_name, None
    raise ValueError(
        "SecretSafeTerminus2 requires an exact metered model: one of "
        f"{sorted(ZAI_OPENAPI_ALLOWED_MODELS)} or a Tinker route "
        "'tinker/<base>[@tinker://<run>:train:<i>/sampler_weights/<step>]'; "
        f"got {model_name!r}. Coding Plan credentials are not admitted."
    )


def _validated_hosts_blocklist_path(value: str | None) -> str | None:
    """Accept only a plain absolute container path for the hosts blocklist."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value != value.strip()
        or any(ch.isspace() for ch in value)
        or ".." in value.split("/")
    ):
        raise ValueError(
            "hosts_blocklist_path must be a plain absolute container path "
            f"without whitespace or traversal, got {value!r}"
        )
    return value

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
        hosts_blocklist_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        if len(args) > 1:
            raise ValueError(
                "SecretSafeTerminus2 accepts at most logs_dir positionally; "
                "pass model_name as a keyword argument"
            )
        self._local_binding: OllamaBinding | None = None
        self._tinker_spec: TinkerModelSpec | None = None
        self._hosts_blocklist_path = _validated_hosts_blocklist_path(hosts_blocklist_path)
        self._hosts_blocklist_result: dict[str, Any] | None = None
        if model_name == TERMINUS_LOCAL_MODEL_SELECTOR:
            self._local_binding = resolve_ollama_binding(model_name)
            model = model_name
        else:
            model, self._tinker_spec = _resolve_metered_model(model_name)
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
            if self._tinker_spec is not None:
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

    async def _apply_hosts_blocklist(self, environment: Any) -> None:
        """Append the pinned answer-leak blocklist to /etc/hosts as root.

        Runs after the agent's own setup/install (network still required for
        the install) and before the first model turn, mirroring the reference
        MiMo harness protocol. Any failure — missing file, failed append, a
        line that did not land — is an infrastructure error that fails the
        trial; it is never a task score.
        """
        assert self._hosts_blocklist_path is not None
        quoted = shlex.quote(self._hosts_blocklist_path)
        script = (
            "set -e; "
            f"B={quoted}; "
            '[ -f "$B" ] || { echo "hosts blocklist file missing: $B" >&2; exit 3; }; '
            'cat "$B" >> /etc/hosts || { echo "hosts append failed" >&2; exit 4; }; '
            "applied=0; "
            'while IFS= read -r line; do '
            '  [ -n "$line" ] || continue; '
            '  grep -qF -- "$line" /etc/hosts || '
            '    { echo "hosts blocklist line not applied: $line" >&2; exit 5; }; '
            '  applied=$((applied+1)); '
            'done < "$B"; '
            '[ "$applied" -gt 0 ] || { echo "hosts blocklist is empty: $B" >&2; exit 6; }; '
            'echo "$applied"'
        )
        result = await environment.exec(script, timeout_sec=30, user="root")
        output = (result.stdout or "").strip()
        if result.return_code != 0 or not output.isdigit() or int(output) < 1:
            raise HostsBlocklistError(
                "applying the pinned hosts blocklist "
                f"{self._hosts_blocklist_path!r} failed as an infrastructure "
                f"step (exit={result.return_code}, stdout={result.stdout!r}, "
                f"stderr={result.stderr!r}); the trial must fail, not score"
            )
        self._hosts_blocklist_result = {
            "path": self._hosts_blocklist_path,
            "applied_lines": int(output),
        }

    async def setup(self, environment: Any) -> None:
        await super().setup(environment)
        # HAR-83: FineEnvs' answer-leak blocklist is a task file the agent
        # must apply after install; stock Terminus2 never does. Fail closed
        # here (inside Harbor's agent-setup phase, before the agent runs) so
        # a trial can never run unleaked on a task that staged the list.
        self.logger.info("answer-leak blocklist: " + await apply_mimo_blocklist(environment))

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        if self._hosts_blocklist_path is not None and self._hosts_blocklist_result is None:
            await self._apply_hosts_blocklist(environment)
        try:
            await super().run(instruction, environment, context)
        finally:
            if self._local_binding is not None:
                # Installed local inference has no provider API charge. This is
                # a billing fact, not invented missing token or call telemetry.
                context.cost_usd = 0.0
                context.metadata = {
                    **(context.metadata or {}),
                    "local_ollama": self._local_binding.to_dict(),
                }
            if self._hosts_blocklist_result is not None:
                context.metadata = {
                    **(context.metadata or {}),
                    "hosts_blocklist": self._hosts_blocklist_result,
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
