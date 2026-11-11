"""Deterministic coverage for the laptop-local Docker opt-out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab import calibrate, cheat
from evallab.execution_backend import ExecutionBackendConfig, LocalDockerDisabledError


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


def test_calibration_readiness_refuses_before_runtime_or_host_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_local_docker(monkeypatch)
    executor = type("ForbiddenExecutor", (), {"local_runtime_checks": _forbidden})()
    monkeypatch.setattr(calibrate.database, "ping", _forbidden)
    monkeypatch.setattr(calibrate.shutil, "disk_usage", _forbidden)

    with pytest.raises(LocalDockerDisabledError):
        calibrate.codex_calibration_readiness(tmp_path, executor=executor)


def test_calibration_dispatch_refuses_before_executor_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.queue import Executor

    _disable_local_docker(monkeypatch)
    monkeypatch.setattr(Executor, "from_repo", _forbidden)

    with pytest.raises(LocalDockerDisabledError):
        calibrate.dispatch_approved_codex_calibration(tmp_path, "retry-storm-backlog", "unused")


def test_auto_resolves_modal_when_opt_out_follows_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ExecutionBackendConfig(backend="auto", allow_local_docker=True)
    _disable_local_docker(monkeypatch)

    assert policy.selected_backend() == "modal"


def test_auto_resolves_modal_when_constructed_with_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_local_docker(monkeypatch)
    policy = ExecutionBackendConfig(backend="auto", allow_local_docker=True)

    assert policy.allow_local_docker is False
    assert policy.selected_backend() == "modal"
