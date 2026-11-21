from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from evallab.execution_contracts import is_mimo_selfhosted_model

MODAL_APP_NAME = "evallab-mimo-v26-9b"

ModalCommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def default_runner(repo_root: Path) -> ModalCommandRunner:

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "uv",
                "run",
                "--project",
                "tools/modal-mimo-serve",
                "--locked",
                "modal",
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            cwd=repo_root,
        )

    return run


def is_selfhosted_model(model: str | None) -> bool:
    return is_mimo_selfhosted_model(model)
