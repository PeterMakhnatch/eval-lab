"""Explicit execution routing and the hard opt-out for laptop Docker workloads."""

from __future__ import annotations

import os


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


