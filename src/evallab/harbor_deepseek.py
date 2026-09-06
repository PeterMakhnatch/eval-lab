"""DeepSeek credential isolation for Harbor's generic mini-swe-agent adapter.

Harbor 0.21 MiniSweAgent copies ``model_connection.env`` into ``exec_as_agent``,
and ``BaseInstalledAgent._exec`` DEBUG-logs that mapping. Docker serializes the
same mapping into compose exec argv. This wrapper never places the provider key
in that environment. Model transport authenticates through an internal proxy
that alone mounts the file-backed secret.
"""

from __future__ import annotations

import os
from dataclasses import replace
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
    DEEPSEEK_PROXY_CAPABILITY_ENV,
    DEEPSEEK_PROXY_TOKEN,
    DEEPSEEK_PROXY_URL,
    collected_secret_values,
)
from evallab.harbor_common import (  # noqa: F401 - re-exported adapter API
    SENSITIVE_CONFIG_KEYS,
    _redact_sensitive_values,
    sanitize_native_trajectory,
)


def _scrubbed_connection_env(connection: ResolvedModelConnection) -> dict[str, str]:
    env = {
        name: value
        for name, value in dict(connection.env).items()
        if name not in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS
        and name not in {"DEEPSEEK_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"}
    }
    token = os.environ.get(DEEPSEEK_PROXY_CAPABILITY_ENV) or DEEPSEEK_PROXY_TOKEN
    env["DEEPSEEK_API_KEY"] = token
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
        token = os.environ.get(DEEPSEEK_PROXY_CAPABILITY_ENV) or DEEPSEEK_PROXY_TOKEN
        return replace(
            connection,
            api_key=token,
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
        capability = os.environ.get(DEEPSEEK_PROXY_CAPABILITY_ENV) or DEEPSEEK_PROXY_TOKEN
        allowed_tokens = {DEEPSEEK_PROXY_TOKEN, capability}
        host_secrets = collected_secret_values() - allowed_tokens
        for name in DEEPSEEK_CREDENTIAL_ENVIRONMENT_KEYS:
            value = runtime_env.get(name)
            if value and value not in allowed_tokens:
                raise ValueError(
                    "DeepSeek provider credential cannot enter the task exec environment"
                )
        if any(value and value in host_secrets for value in runtime_env.values()):
            raise ValueError("DeepSeek provider credential cannot enter the task exec environment")
        if "cat /run/secrets/" in command or 'DEEPSEEK_API_KEY="$(cat' in command:
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
