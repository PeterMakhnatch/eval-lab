"""DeepSeek credential transport for Harbor's generic mini-swe-agent adapter.

Harbor 0.21's installed MiniSweAgent forwards provider keys as per-exec
environment values. Docker serializes those values into ``docker compose exec``
argv, and BaseInstalledAgent attaches the same mapping to a DEBUG record. This
narrow wrapper keeps the generic install/run/LiteLLM implementation while
loading the provider key from a Compose secret inside the agent shell instead.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from harbor.agents.installed.mini_swe_agent import (  # ty: ignore[unresolved-import]
    MiniSweAgent,
)
from harbor.agents.model_connection import (  # ty: ignore[unresolved-import]
    ResolvedModelConnection,
)
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]

DEEPSEEK_SECRET_PATH = "/run/secrets/evallab_deepseek_api_key"


class SecretSafeDeepSeekMiniSweAgent(MiniSweAgent):
    """MiniSweAgent with a file-backed DeepSeek key and no secret-bearing exec env."""

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        if connection.provider != "deepseek":
            raise ValueError("SecretSafeDeepSeekMiniSweAgent requires a deepseek/* model")
        if connection.api_key is None:
            return connection
        return replace(
            connection,
            api_key="<mounted-compose-secret>",
            env={
                name: value
                for name, value in connection.env.items()
                if name not in {"DEEPSEEK_API_KEY", "MSWEA_API_KEY"}
            },
        )

    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        runtime_env = env or {}
        if runtime_env.get("MSWEA_CONFIGURED") == "true":
            command = (
                f"test -r {DEEPSEEK_SECRET_PATH} && "
                f'export DEEPSEEK_API_KEY="$(cat {DEEPSEEK_SECRET_PATH})" && '
                f"{command}"
            )
        return await super().exec_as_agent(
            environment,
            command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )
