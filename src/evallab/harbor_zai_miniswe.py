"""Z.ai OpenAPI credential isolation for Harbor's generic mini-swe-agent adapter.

Harbor 0.22 MiniSweAgent copies ``model_connection.env`` into ``exec_as_agent``,
and ``BaseInstalledAgent._exec`` logs that mapping. Docker serializes the
same mapping into compose exec argv. This wrapper never places the Z.ai Open Platform
provider key in that environment. Model transport authenticates through an internal proxy
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
    ZAI_OPENAPI_ALLOWED_MODELS,
    ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENAPI_PROXY_CAPABILITY_ENV,
    ZAI_OPENAPI_PROXY_TOKEN,
    ZAI_OPENAPI_PROXY_URL,
    collected_secret_values,
)
from evallab.harbor_common import sanitize_native_trajectory

__all__ = [
    "SecretSafeZaiMiniSweAgent",
    "ZAI_OPENAPI_ALLOWED_MODELS",
    "ZAI_OPENAPI_MODEL_SELECTOR",
]


def _scrubbed_connection_env(connection: ResolvedModelConnection) -> dict[str, str]:
    env = {
        name: value
        for name, value in dict(connection.env).items()
        if name not in ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS
        and name not in {"OPENAI_BASE_URL", "OPENAI_API_BASE", "ZAI_BASE_URL"}
    }
    token = os.environ.get(ZAI_OPENAPI_PROXY_CAPABILITY_ENV) or ZAI_OPENAPI_PROXY_TOKEN
    env["MSWEA_API_KEY"] = token
    env["OPENAI_BASE_URL"] = ZAI_OPENAPI_PROXY_URL
    env["OPENAI_API_BASE"] = ZAI_OPENAPI_PROXY_URL
    return env


class SecretSafeZaiMiniSweAgent(MiniSweAgent):
    """MiniSweAgent that talks only to the internal Z.ai OpenAPI credential proxy."""

    def __init__(self, *args: Any, model_name: str | None = None, **kwargs: Any) -> None:
        if model_name is not None:
            normalized = model_name if "/" in model_name else f"zai/{model_name}"
            if normalized not in ZAI_OPENAPI_ALLOWED_MODELS:
                raise ValueError(
                    f"SecretSafeZaiMiniSweAgent requires model in {sorted(ZAI_OPENAPI_ALLOWED_MODELS)}, "
                    f"got {model_name!r}"
                )
            kwargs["model_name"] = model_name
        super().__init__(*args, **kwargs)
        model = getattr(self, "model_name", None) or getattr(self, "model", None)
        if model is not None:
            normalized = model if "/" in model else f"zai/{model}"
            if normalized not in ZAI_OPENAPI_ALLOWED_MODELS:
                raise ValueError(
                    f"SecretSafeZaiMiniSweAgent requires model in {sorted(ZAI_OPENAPI_ALLOWED_MODELS)}, "
                    f"got {model!r}"
                )

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        if connection.provider != "zai":
            raise ValueError(
                f"SecretSafeZaiMiniSweAgent requires a zai/* model, got provider {connection.provider!r}"
            )
        model = (
            getattr(connection, "model", None)
            or getattr(connection, "model_name", None)
            or getattr(self, "model_name", None)
            or getattr(self, "model", None)
        )
        if model is not None:
            normalized = model if "/" in model else f"zai/{model}"
            if normalized not in ZAI_OPENAPI_ALLOWED_MODELS:
                raise ValueError(
                    f"SecretSafeZaiMiniSweAgent requires model in {sorted(ZAI_OPENAPI_ALLOWED_MODELS)}, "
                    f"got {model!r}"
                )
        token = os.environ.get(ZAI_OPENAPI_PROXY_CAPABILITY_ENV) or ZAI_OPENAPI_PROXY_TOKEN
        return replace(
            connection,
            api_key=token,
            base_url=ZAI_OPENAPI_PROXY_URL,
            configured_base_url=ZAI_OPENAPI_PROXY_URL,
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
        capability = os.environ.get(ZAI_OPENAPI_PROXY_CAPABILITY_ENV) or ZAI_OPENAPI_PROXY_TOKEN
        allowed_tokens = {ZAI_OPENAPI_PROXY_TOKEN, capability}
        host_secrets = collected_secret_values() - allowed_tokens
        for name in ZAI_OPENAPI_CREDENTIAL_ENVIRONMENT_KEYS:
            value = runtime_env.get(name)
            if value and value not in allowed_tokens:
                raise ValueError(
                    "Z.ai OpenAPI provider credential cannot enter the task exec environment"
                )
        if any(value and value in host_secrets for value in runtime_env.values()):
            raise ValueError(
                "Z.ai OpenAPI provider credential cannot enter the task exec environment"
            )
        if "cat /run/secrets/" in command or 'ZAI_OPENAPI_API_KEY="$(cat' in command:
            raise ValueError(
                "Z.ai OpenAPI provider credential cannot enter the task exec command"
            )
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
