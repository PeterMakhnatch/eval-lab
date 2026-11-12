"""Deterministic coverage for the laptop-local Docker opt-out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab import cheat
from evallab.execution_backend import LocalDockerDisabledError


def _disable_local_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail("local Docker opt-out must precede runtime discovery or execution")


def test_cheat_refuses_before_executor_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.queue import Executor

    _disable_local_docker(monkeypatch)
    monkeypatch.setattr(Executor, "from_repo", _forbidden)

    with pytest.raises(LocalDockerDisabledError):
        cheat.run_cheat_trial(
            tmp_path / "task", (), repo_root=tmp_path, jobs_dir=tmp_path / "runs", name="disabled"
        )


