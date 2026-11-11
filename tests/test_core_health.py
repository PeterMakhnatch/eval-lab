from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab import automation, backups, database, gold
from evallab.failure_library.runs import io

NOW = datetime(2026, 11, 11, 14, tzinfo=UTC)


def _installed_job(home: Path, name: str, *, interval: int | None = None) -> Path:
    label = automation.LAUNCH_AGENT_LABEL_PREFIX + name
    cwd = home / ("a" * 40)
    cwd.mkdir(exist_ok=True)
    definition = {"Label": label, "WorkingDirectory": str(cwd), "ProgramArguments": ["/usr/bin/true"]}
    if interval is not None:
        definition["StartInterval"] = interval
    plist = home / "Library/LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(definition))
    return plist


def _loaded(argv, **kwargs):
    assert argv[:2] == ["launchctl", "print"]
    return subprocess.CompletedProcess(argv, 0, "state = waiting\n last exit code = 0\n", "")


def test_loaded_job_with_recent_stdout_but_stale_useful_output_fails(tmp_path):
    _installed_job(tmp_path, "nightly")
    state = tmp_path / ".local/state/evallab-nightly"
    state.mkdir(parents=True)
    output = state / "trials.parquet"
    output.write_bytes(b"retained projection")
    stale = (NOW - timedelta(hours=37)).timestamp()
    os.utime(output, (stale, stale))
    (state / "stdout.log").write_text("alive\n")
    [(name, ok, detail)] = automation.installed_job_health(tmp_path, now=NOW, run=_loaded)
    assert name.endswith("nightly")
    assert not ok
    assert "useful_age_h=37.0" in detail


@pytest.mark.parametrize("exit_code,loaded,running,expected", [
    (0, True, False, True), (9, True, True, False), (0, False, False, False),
    (None, True, True, True), (None, True, False, False),
])
def test_useful_output_does_not_hide_unloaded_or_failed_job(tmp_path, exit_code, loaded, running, expected):
    _installed_job(tmp_path, "nightly")
    output = tmp_path / ".local/state/evallab-nightly/trials.parquet"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"retained projection")
    stamp = (NOW - timedelta(hours=1)).timestamp()
    os.utime(output, (stamp, stamp))

    def launchctl(argv, **kwargs):
        assert argv[:2] == ["launchctl", "print"]
        output = f"state = {'running' if running else 'waiting'}\n last exit code = {exit_code}\n"
        return subprocess.CompletedProcess(argv, 0 if loaded else 113, output, "")

    [(_, ok, _)] = automation.installed_job_health(tmp_path, now=NOW, run=launchctl)
    assert ok is expected


def test_factory_idle_refresh_does_not_reset_last_useful_output(tmp_path):
    _installed_job(tmp_path, "prod-factory", interval=300)
    cycles = tmp_path / ".local/state/evallab-prod-live/factory/factory-cycles.jsonl"
    cycles.parent.mkdir(parents=True)
    cycles.write_text("\n".join(json.dumps(cycle) for cycle in [
        {"ts": (NOW - timedelta(hours=3)).isoformat(), "emitted": 1},
        {"ts": (NOW - timedelta(minutes=1)).isoformat(), "emitted": 0, "consumed_meaningful": 0},
    ]) + "\n")
    [(_, ok, detail)] = automation.installed_job_health(tmp_path, now=NOW, run=_loaded)
    assert not ok
    assert "useful_age_h=3.0" in detail


@pytest.mark.parametrize("values,expected", [([True, False], True), ([False, False], False), ([True, True], False)])
def test_doctor_detects_ignored_no_fallback_overlay_and_does_not_expose_credentials(
    tmp_path, monkeypatch, values, expected,
):
    (tmp_path / "uv.lock").write_text("package = []\n")
    executable = tmp_path / "keys"
    executable.write_bytes(b"fixture CLI code, not a credential\n")
    monkeypatch.setattr(automation.shutil, "which", lambda name: str(executable) if name == "keys" else None)
    monkeypatch.setitem(automation.CORE_TOOL_PINS, "keys-code-sha256", hashlib.sha256(executable.read_bytes()).hexdigest())
    monkeypatch.setenv("EVALLAB_FAILURE_LIBRARY_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("EVALLAB_CLOUD_TRACE_DB", str(tmp_path / "missing.sqlite"))
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(tmp_path / "parquet"))
    monkeypatch.setattr(backups, "backup_freshness", lambda *a, **kw: {"status": "missing", "age_hours": None, "max_age_hours": 36, "newest_verified": None})
    monkeypatch.setattr(io, "retained_source_health", lambda *a, **kw: (False, "fixture bundles unavailable"))
    monkeypatch.setattr(gold, "load_rows", lambda *a, **kw: [])
    secret = "credential-value-must-not-appear"

    def unavailable(*args, **kwargs):
        raise ConnectionError(secret)

    monkeypatch.setattr(database.psycopg, "connect", unavailable)
    config_values = iter(values)

    def command(argv, **kwargs):
        assert "-p" not in argv and "download" not in argv
        if argv[:3] == ["omp", "config", "get"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"value": next(config_values)}), "")
        if argv[1:] == ["has", "HF_TOKEN"] or argv[:2] == ["gh", "auth"]:
            assert kwargs["stdout"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.DEVNULL
            return subprocess.CompletedProcess(argv, 0 if argv[0] == "gh" else 1, secret, secret)
        if argv[:2] == ["gh", "api"]:
            value = {"contexts": ["quality-required", "typecheck-required"]} if "/protection/" in argv[2] else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if argv[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(argv, 0, '{"OSType":"linux"}', "")
        return subprocess.CompletedProcess(argv, 0, "0.0.0", "")

    checks = {name: (ok, detail) for name, ok, detail in automation.core_runtime_checks(
        tmp_path, home=tmp_path, now=NOW, run=command,
    )}
    assert checks["omp-no-fallback"][0] is expected
    assert checks["keys"][0] is False
    assert checks["postgres-schema"][0] is False
    assert checks["github"][0] is True
    assert secret not in json.dumps(checks)
    assert not (tmp_path / "missing.sqlite").exists()
