"""GLM self-hosted and fine-tuned credential isolation for Harbor's mini-swe-agent adapter.

Harbor 0.22 MiniSweAgent copies ``model_connection.env`` into ``exec_as_agent``,
and ``BaseInstalledAgent._exec`` logs that mapping. Docker serializes the
same mapping into compose exec argv. This wrapper never places the GLM self-hosted
provider key in that environment. Model transport connects to the OpenAI-compatible
self-hosted endpoint validated from GLM_SELFHOSTED_BASE_URL.
"""

from __future__ import annotations

import os
import urllib.parse
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
    GLM_SELFHOSTED_ALLOWED_PROVIDERS,
    GLM_SELFHOSTED_BASE_URL_ENV,
    GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS,
    GLM_SELFHOSTED_PROXY_TOKEN,
    collected_secret_values,
)
from evallab.harbor_common import sanitize_native_trajectory

__all__ = [
    "GLM_SELFHOSTED_ALLOWED_PROVIDERS",
    "GLM_SELFHOSTED_BASE_URL_ENV",
    "SecretSafeGlmSelfhostedMiniSweAgent",
    "SecretSafeOpenAICompatMiniSweAgent",
]


def _validate_model_provider(model: str | None) -> str | None:
    if model is None:
        return None
    if "/" not in model:
        raise ValueError(
            f"SecretSafeGlmSelfhostedMiniSweAgent requires provider in "
            f"{sorted(GLM_SELFHOSTED_ALLOWED_PROVIDERS)}, got model without provider: {model!r}"
        )
    provider, _ = model.split("/", 1)
    if provider not in GLM_SELFHOSTED_ALLOWED_PROVIDERS:
        raise ValueError(
            f"SecretSafeGlmSelfhostedMiniSweAgent requires provider in "
            f"{sorted(GLM_SELFHOSTED_ALLOWED_PROVIDERS)}, got {provider!r} (model: {model!r})"
        )
    return model


def _validated_base_url() -> str:
    base_url = os.environ.get(GLM_SELFHOSTED_BASE_URL_ENV)
    if not base_url:
        raise ValueError(f"{GLM_SELFHOSTED_BASE_URL_ENV} environment variable is not set")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{GLM_SELFHOSTED_BASE_URL_ENV} must be a valid http or https URL, got {base_url!r}"
        )
    return base_url


def _scrubbed_connection_env(
    connection: ResolvedModelConnection, base_url: str
) -> dict[str, str]:
    env = {
        name: value
        for name, value in dict(connection.env).items()
        if name not in GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS
        and name not in {"OPENAI_BASE_URL", "OPENAI_API_BASE", GLM_SELFHOSTED_BASE_URL_ENV}
    }
    token = os.environ.get("EVALLAB_GLM_PROXY_CAPABILITY") or GLM_SELFHOSTED_PROXY_TOKEN
    env["MSWEA_API_KEY"] = token
    env["OPENAI_BASE_URL"] = base_url
    env["OPENAI_API_BASE"] = base_url
    return env


class SecretSafeGlmSelfhostedMiniSweAgent(MiniSweAgent):
    """MiniSweAgent that talks to a self-hosted or fine-tuned GLM endpoint without exposing secrets."""

    def __init__(self, *args: Any, model_name: str | None = None, **kwargs: Any) -> None:
        if model_name is not None:
            _validate_model_provider(model_name)
            kwargs["model_name"] = model_name
        super().__init__(*args, **kwargs)
        model = getattr(self, "model_name", None) or getattr(self, "model", None)
        if model is not None:
            _validate_model_provider(model)

    @property
    def model_connection(self) -> ResolvedModelConnection:
        connection = super().model_connection
        if connection.provider not in GLM_SELFHOSTED_ALLOWED_PROVIDERS:
            raise ValueError(
                f"SecretSafeGlmSelfhostedMiniSweAgent requires provider in "
                f"{sorted(GLM_SELFHOSTED_ALLOWED_PROVIDERS)}, got provider {connection.provider!r}"
            )
        model = (
            getattr(connection, "model", None)
            or getattr(connection, "model_name", None)
            or getattr(self, "model_name", None)
            or getattr(self, "model", None)
        )
        if model is not None:
            _validate_model_provider(model)

        base_url = _validated_base_url()
        token = os.environ.get("EVALLAB_GLM_PROXY_CAPABILITY") or GLM_SELFHOSTED_PROXY_TOKEN
        return replace(
            connection,
            api_key=token,
            base_url=base_url,
            configured_base_url=base_url,
            env=_scrubbed_connection_env(connection, base_url),
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
        capability = os.environ.get("EVALLAB_GLM_PROXY_CAPABILITY") or GLM_SELFHOSTED_PROXY_TOKEN
        allowed_tokens = {GLM_SELFHOSTED_PROXY_TOKEN, capability}
        host_secrets = collected_secret_values() - allowed_tokens
        for name in GLM_SELFHOSTED_CREDENTIAL_ENVIRONMENT_KEYS:
            value = runtime_env.get(name)
            if value and value not in allowed_tokens:
                raise ValueError(
                    "GLM self-hosted provider credential cannot enter the task exec environment"
                )
        if any(value and value in host_secrets for value in runtime_env.values()):
            raise ValueError(
                "GLM self-hosted provider credential cannot enter the task exec environment"
            )
        if "cat /run/secrets/" in command or 'GLM_SELFHOSTED_API_KEY="$(cat' in command:
            raise ValueError(
                "GLM self-hosted provider credential cannot enter the task exec command"
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


SecretSafeOpenAICompatMiniSweAgent = SecretSafeGlmSelfhostedMiniSweAgent
