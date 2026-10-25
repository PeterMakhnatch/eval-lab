"""Explicit execution routing and the hard opt-out for laptop Docker workloads."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ExecutionBackend = Literal["auto", "docker", "modal"]


class LocalDockerDisabledError(RuntimeError):
    """Local container execution was forbidden by the operator or worker policy."""


def require_local_docker(*, allow_local_docker: bool | None = None) -> None:
    """Refuse before Docker admission, image preparation, or subprocess execution.

    The environment switch is checked on every invocation, so setting it after a
    worker was constructed still protects the laptop. Explicit True cannot
    override the operator's environment switch.
    """
    if allow_local_docker is False or os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") == "1":
        raise LocalDockerDisabledError(
            "Local Docker execution is disabled (EVALLAB_DISABLE_LOCAL_DOCKER=1 "
            "or allow_local_docker=False); configure backend='modal' with cloud approval"
        )


@dataclass(frozen=True)
class ExecutionBackendConfig:
    """Worker-owned execution policy; queued task payloads cannot override it.

    Auto preserves the native local route when permitted, but resolves to Modal
    when local Docker is disabled. That does not authorize cloud spending: the
    selected executor still requires the explicit approval, cap and ledger.
    """

    backend: ExecutionBackend = "docker"
    modal_app_name: str | None = None
    spend_ledger: Path | str | None = None
    approval: str | None = None
    max_usd: float | None = None
    billing_tag: str | None = None
    allow_local_docker: bool = field(
        default_factory=lambda: os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") != "1"
    )

    def __post_init__(self) -> None:
        if self.backend not in {"auto", "docker", "modal"}:
            raise ValueError(f"unsupported execution backend: {self.backend!r}")
        if not isinstance(self.allow_local_docker, bool):
            raise ValueError("allow_local_docker must be a boolean")
        if os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") == "1":
            object.__setattr__(self, "allow_local_docker", False)

    def selected_backend(self) -> ExecutionBackend:
        """Resolve auto without probing the laptop's Docker daemon."""
        if self.backend == "auto" and (
            not self.allow_local_docker or os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") == "1"
        ):
            return "modal"
        return self.backend

    def require_backend(self) -> ExecutionBackend:
        backend = self.selected_backend()
        if backend != "modal":
            require_local_docker(allow_local_docker=self.allow_local_docker)
        return backend

    def candidate_kwargs(self) -> dict[str, Any]:
        """Forward the entire policy unchanged to the canonical artifact runner."""
        return {
            "backend": self.require_backend(),
            "modal_app_name": self.modal_app_name,
            "spend_ledger": self.spend_ledger,
            "approval": self.approval,
            "max_usd": self.max_usd,
            "billing_tag": self.billing_tag,
            "allow_local_docker": self.allow_local_docker
            and os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") != "1",
        }
