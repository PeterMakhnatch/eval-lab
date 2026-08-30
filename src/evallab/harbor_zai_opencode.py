"""Harbor adapter for the Z.ai Coding Plan via OpenCode.

Provides two execution lanes:
1. ``ZaiOpenCodeAgent``: Trusted-task-only mount-based adapter (links auth.json).
2. ``SecretSafeZaiOpenCodeAgent``: Proxy-grade credential isolation adapter.
   Untrusted task/agent containers talk only to the internal credential broker
   (``zai-secret-proxy``) and receive a placeholder capability token, never
   the real provider secret.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from harbor.agents.installed.opencode import OpenCode  # ty: ignore[unresolved-import]
from harbor.agents.model_connection import (  # ty: ignore[unresolved-import]
    ResolvedModelConnection,
)
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    REDACTED_SECRET_VALUE,
    collected_secret_values,
    persist_private_bytes,
)

ADAPTER_VERSION = "1.0.0"

#: OpenCode release every install uses unless an explicit override is passed.
PINNED_OPENCODE_VERSION = "1.18.25"

#: The only accepted provider prefix; the Z.ai credential is scoped to it.
REQUIRED_MODEL_PREFIX = "zai-coding-plan/"

#: Read-only mount of the filtered, Z.ai-only OpenCode auth document (trusted lane).
AUTH_SECRET_MOUNT = "/run/secrets/evallab_zai_opencode_auth.json"

#: OpenCode reads ``$XDG_DATA_HOME/opencode/auth.json``; Harbor's pinned
#: OpenCode ``run()`` exports ``XDG_DATA_HOME=/logs/agent/opencode/xdg-data``.
AUTH_LINK_DIR = "/logs/agent/opencode/xdg-data/opencode"
AUTH_LINK_PATH = f"{AUTH_LINK_DIR}/auth.json"

#: The only commands the trusted-lane adapter executes for auth mounting.
CREATE_AUTH_LINK_COMMAND = f"mkdir -p {AUTH_LINK_DIR} && ln -sfn {AUTH_SECRET_MOUNT} {AUTH_LINK_PATH}"
REMOVE_AUTH_LINK_COMMAND = f"rm -f {AUTH_LINK_PATH}"

# --------------------------------------------------------------------------
# Proxy lane constants
# --------------------------------------------------------------------------

ZAI_PROXY_HOST = "zai-secret-proxy"
ZAI_PROXY_URL = "http://zai-secret-proxy:8080"
ZAI_PROXY_TOKEN = "evallab-proxy-placeholder"
ZAI_PROXY_CAPABILITY_ENV = "EVALLAB_ZAI_PROXY_CAPABILITY"
ZAI_CREDENTIAL_ENVIRONMENT_KEYS = frozenset(
    {
        "ZAI_CODING_PLAN_API_KEY",
        "ZAI_API_KEY",
        "ZAI_CODING_PLAN_KEY",
        "ZAI_KEY",
    }
)
ZAI_SECRET_COMPOSE = Path("containers/zai-secret.compose.yaml")

SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "api_key",
        "x-api-key",
        "access_token",
        "apiKey",
    }
)


def validate_model_name(model_name: str | None) -> str:
    """Return ``model_name`` when it selects a Z.ai Coding Plan model.

    Raises ``ValueError`` otherwise. ``model_name`` is a provider/model
    selector, not a secret, so the offending value may appear in the message.
    """
    if not model_name or "/" not in model_name:
        raise ValueError(
            "ZaiOpenCodeAgent requires a provider/model selector, got "
            f"{model_name!r}"
        )
    provider, _, model = model_name.partition("/")
    if f"{provider}/" != REQUIRED_MODEL_PREFIX:
        raise ValueError(
            "ZaiOpenCodeAgent only accepts models under "
            f"{REQUIRED_MODEL_PREFIX!r} (the Z.ai Coding Plan credential "
            f"lane); got {model_name!r}"
        )
    if not model:
        raise ValueError(
            "ZaiOpenCodeAgent requires a non-empty model under "
            f"{REQUIRED_MODEL_PREFIX!r}; got {model_name!r}"
        )
    return model_name


def _redact_sensitive_values(value: Any, secrets: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED_SECRET_VALUE
                if str(key).casefold() in SENSITIVE_CONFIG_KEYS
                else _redact_sensitive_values(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item, secrets) for item in value]
    if isinstance(value, str) and value in secrets:
        return REDACTED_SECRET_VALUE
    return value


def sanitize_native_trajectory(path: Path, secrets: frozenset[str] | None = None) -> None:
    """Rewrite a native trajectory on disk only after in-memory redaction."""
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        persist_private_bytes(
            path,
            (json.dumps({"redacted": "unparseable native trajectory removed"}) + "\n").encode(),
            secrets=(),
        )
        return
    known = secrets if secrets is not None else collected_secret_values()
    sanitized = _redact_sensitive_values(payload, known)
    persist_private_bytes(
        path,
        (json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        secrets=tuple(secret.encode() for secret in known),
    )


def _scrubbed_connection_env(connection: ResolvedModelConnection) -> dict[str, str]:
    env = {
        name: value
        for name, value in dict(connection.env).items()
        if name not in ZAI_CREDENTIAL_ENVIRONMENT_KEYS
        and name not in {"ZAI_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"}
    }
    token = os.environ.get(ZAI_PROXY_CAPABILITY_ENV) or ZAI_PROXY_TOKEN
    env["ZAI_CODING_PLAN_API_KEY"] = token
    env["ZAI_API_KEY"] = token
    env["ZAI_BASE_URL"] = ZAI_PROXY_URL
    env["OPENAI_BASE_URL"] = ZAI_PROXY_URL
    env["OPENAI_API_BASE"] = ZAI_PROXY_URL
    return env


class ZaiOpenCodeAgent(OpenCode):
    """Pinned, model-guarded OpenCode agent for trusted Z.ai Coding Plan tasks.

    Trusted-task-only: see the module docstring for the exact security
    boundary (readable read-only credential mount, no proxy isolation).
    """

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, version=version or PINNED_OPENCODE_VERSION, **kwargs)
        # Harbor may pass the model selector at construction; when it does,
        # refuse a non-Z.ai provider before any install or exec happens.
        model_name = getattr(self, "model_name", None)
        if model_name is not None:
            validate_model_name(model_name)

    async def run(self, instruction, environment, context) -> None:  # type: ignore[no-untyped-def]
        # Re-check at run time: Harbor can also assign the model after
        # construction, and ``run`` must fail closed before the auth link is
        # created rather than after.
        validate_model_name(self.model_name)
        await self.exec_as_agent(environment, command=CREATE_AUTH_LINK_COMMAND)
        try:
            await super().run(instruction, environment, context)
        finally:
            # Remove the link even when the agent run raises. Cleanup errors
            # are suppressed so the original agent error is never masked.
            with contextlib.suppress(Exception):
                await self.exec_as_agent(environment, command=REMOVE_AUTH_LINK_COMMAND)


class SecretSafeZaiOpenCodeAgent(OpenCode):
    """OpenCode agent that talks only to the internal Z.ai credential broker.

    Proxy-grade credential isolation: the container receives a per-trial
    capability token and an internal endpoint, never the real Z.ai secret.
    """

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        opencode_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        model_name = kwargs.get("model_name")
        if model_name is not None:
            validate_model_name(model_name)

        config = copy.deepcopy(opencode_config or {})
        # Wire the zai-coding-plan provider baseURL to the internal proxy
        provider_cfg = config.setdefault("provider", {}).setdefault("zai-coding-plan", {})
        provider_cfg.setdefault("options", {})["baseURL"] = ZAI_PROXY_URL

        super().__init__(
            *args,
            version=version or PINNED_OPENCODE_VERSION,
            opencode_config=config,
            **kwargs,
        )

        model_name = getattr(self, "model_name", None)
        if model_name is not None:
            validate_model_name(model_name)

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        if connection.provider != "zai-coding-plan" and connection.provider != "zai":
            # Also check if provider is part of model name
            model = getattr(self, "model_name", None)
            if model:
                validate_model_name(model)
            elif connection.provider:
                validate_model_name(f"{connection.provider}/placeholder")
        token = os.environ.get(ZAI_PROXY_CAPABILITY_ENV) or ZAI_PROXY_TOKEN
        return replace(
            connection,
            api_key=token,
            base_url=ZAI_PROXY_URL,
            configured_base_url=ZAI_PROXY_URL,
            env=_scrubbed_connection_env(connection),
        )

    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        runtime_env = dict(env or {})
        capability = os.environ.get(ZAI_PROXY_CAPABILITY_ENV) or ZAI_PROXY_TOKEN
        allowed_tokens = {ZAI_PROXY_TOKEN, capability}
        host_secrets = collected_secret_values() - allowed_tokens
        for name in ZAI_CREDENTIAL_ENVIRONMENT_KEYS:
            value = runtime_env.get(name)
            if value and value not in allowed_tokens:
                raise ValueError(
                    "Z.ai provider credential cannot enter the task exec environment"
                )
        if any(
            value and value in host_secrets
            for value in runtime_env.values()
        ):
            raise ValueError(
                "Z.ai provider credential cannot enter the task exec environment"
            )
        if "cat /run/secrets/" in command or any(
            f'{key}="$(cat' in command for key in ZAI_CREDENTIAL_ENVIRONMENT_KEYS
        ):
            raise ValueError("Z.ai provider credential cannot enter the task exec command")
        return await super().exec_as_agent(
            environment,
            command,
            env=runtime_env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

    def populate_context_post_run(self, context: Any) -> None:
        secrets = collected_secret_values()
        logs = self.logs_dir
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        super().populate_context_post_run(context)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
