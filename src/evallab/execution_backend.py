from __future__ import annotations

import os


class LocalDockerDisabledError(RuntimeError):
    pass


def require_local_docker(*, allow_local_docker: bool | None = None) -> None:
    if allow_local_docker is False or os.environ.get("EVALLAB_DISABLE_LOCAL_DOCKER") == "1":
        raise LocalDockerDisabledError(
            "Local Docker execution is disabled (EVALLAB_DISABLE_LOCAL_DOCKER=1 "
            "or allow_local_docker=False); configure backend='modal' with cloud approval"
        )


