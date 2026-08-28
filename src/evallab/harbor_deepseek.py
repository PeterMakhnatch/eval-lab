"""DeepSeek credential transport for Harbor's generic mini-swe-agent adapter.

Harbor 0.21's installed MiniSweAgent forwards provider keys as per-exec
environment values. Docker serializes those values into ``docker compose exec``
argv, and BaseInstalledAgent attaches the same mapping to a DEBUG record. This
narrow wrapper keeps the generic install/run/LiteLLM implementation while
loading the provider key from a Compose secret inside the agent shell instead.
"""

from __future__ import annotations

import json
import os
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

DEEPSEEK_SECRET_PATH = "/run/secrets/evallab_deepseek_api_key"
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
                "<redacted>"
                if str(key).casefold() in SENSITIVE_CONFIG_KEYS
                else _redact_sensitive_values(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item, secrets) for item in value]
    if isinstance(value, str) and value in secrets:
        return "<redacted>"
    return value


def _sanitize_native_trajectory(path: Path) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        path.write_text(
            json.dumps({"redacted": "unparseable native trajectory removed"}) + "\n",
            encoding="utf-8",
        )
        return
    secrets = frozenset(
        value
        for name in ("DEEPSEEK_API_KEY", "MSWEA_API_KEY")
        if (value := os.environ.get(name))
    )
    sanitized = _redact_sensitive_values(payload, secrets)
    path.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


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

    def populate_context_post_run(self, context: Any) -> None:
        _sanitize_native_trajectory(
            self.logs_dir / "mini-swe-agent.trajectory.json"
        )
        super().populate_context_post_run(context)
