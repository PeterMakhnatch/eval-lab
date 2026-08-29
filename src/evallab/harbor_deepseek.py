"""DeepSeek credential isolation for Harbor's generic mini-swe-agent adapter.

Harbor 0.21 MiniSweAgent copies ``model_connection.env`` into ``exec_as_agent``,
and ``BaseInstalledAgent._exec`` DEBUG-logs that mapping. Docker serializes the
same mapping into compose exec argv. This wrapper never places the provider key
in that environment. Model transport authenticates through an internal proxy
that alone mounts the file-backed secret.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from harbor.agents.installed.mini_swe_agent import (  # ty: ignore[unresolved-import]
    MiniSweAgent,
)
from harbor.agents.model_connection import (  # ty: ignore[unresolved-import]
    ResolvedModelConnection,
)
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS,
    DEEPSEEK_PROXY_TOKEN,
    DEEPSEEK_PROXY_URL,
    REDACTED_SECRET_VALUE,
    collected_secret_values,
    persist_private_bytes,
)

SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "api_key",
        "x-api-key",
        "access_token",
    }
)


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
        if name not in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS
        and name not in {"DEEPSEEK_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"}
    }
    env["DEEPSEEK_API_KEY"] = DEEPSEEK_PROXY_TOKEN
    env["DEEPSEEK_BASE_URL"] = DEEPSEEK_PROXY_URL
    env["OPENAI_BASE_URL"] = DEEPSEEK_PROXY_URL
    env["OPENAI_API_BASE"] = DEEPSEEK_PROXY_URL
    return env


class SecretSafeDeepSeekMiniSweAgent(MiniSweAgent):
    """MiniSweAgent that talks only to the internal credential broker."""

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        if connection.provider != "deepseek":
            raise ValueError("SecretSafeDeepSeekMiniSweAgent requires a deepseek/* model")
        return replace(
            connection,
            api_key=DEEPSEEK_PROXY_TOKEN,
            base_url=DEEPSEEK_PROXY_URL,
            configured_base_url=DEEPSEEK_PROXY_URL,
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
        host_secrets = collected_secret_values()
        for name in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS:
            value = runtime_env.get(name)
            if value and value != DEEPSEEK_PROXY_TOKEN:
                raise ValueError(
                    "DeepSeek provider credential cannot enter the task exec environment"
                )
        if any(
            value and value in host_secrets and value != DEEPSEEK_PROXY_TOKEN
            for value in runtime_env.values()
        ):
            raise ValueError(
                "DeepSeek provider credential cannot enter the task exec environment"
            )
        if "cat /run/secrets/" in command or "DEEPSEEK_API_KEY=\"$(cat" in command:
            raise ValueError("DeepSeek provider credential cannot enter the task exec command")
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
        sanitize_native_trajectory(logs / "mini-swe-agent.trajectory.json", secrets)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
        super().populate_context_post_run(context)
        sanitize_native_trajectory(logs / "mini-swe-agent.trajectory.json", secrets)
        sanitize_native_trajectory(logs / "trajectory.json", secrets)
