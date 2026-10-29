"""Every Harbor child process is started with HARBOR_TELEMETRY=off."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab import cheat, hub_trials, task_stability
from evallab.execution_contracts import subscription_environment
from evallab.harbor_telemetry import harbor_child_environment


def _capture_run(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout=b"harbor 0.24.0\n", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_child_environment_forces_off_without_mutating_input() -> None:
    source = {"PATH": "/bin", "HARBOR_TELEMETRY": "on"}
    child = harbor_child_environment(source)
    assert child == {"PATH": "/bin", "HARBOR_TELEMETRY": "off"}
    assert source["HARBOR_TELEMETRY"] == "on"


def test_child_environment_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_TELEMETRY_PROBE", "kept")
    monkeypatch.delenv("HARBOR_TELEMETRY", raising=False)
    child = harbor_child_environment()
    assert child["EVALLAB_TELEMETRY_PROBE"] == "kept"
    assert child["HARBOR_TELEMETRY"] == "off"


def test_subscription_environment_overrides_ambient_opt_in() -> None:
    assert subscription_environment({"HARBOR_TELEMETRY": "on"})["HARBOR_TELEMETRY"] == "off"
    assert subscription_environment({})["HARBOR_TELEMETRY"] == "off"


def test_native_hub_harbor_child_env_has_telemetry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(hub_trials, monkeypatch)
    hub_trials.NativeHarbor("harbor")._run(["hub", "trial", "list"])
    assert calls[0]["env"]["HARBOR_TELEMETRY"] == "off"


def test_cheat_harbor_version_child_env_has_telemetry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_run(cheat, monkeypatch)
    monkeypatch.setattr(cheat.shutil, "which", lambda name: f"/bin/{name}")
    assert cheat.harbor_version() is not None
    assert calls[0]["env"]["HARBOR_TELEMETRY"] == "off"


def test_stability_job_env_has_telemetry_off(tmp_path: Path) -> None:
    _, env = task_stability.stability_run_command(
        task_path=tmp_path / "task",
        agent="nop",
        job_name="stability-probe",
        jobs_dir=tmp_path / "jobs",
        repo_src=tmp_path / "src",
    )
    assert env["HARBOR_TELEMETRY"] == "off"
    assert env["PYTHONPATH"] == str(tmp_path / "src")
