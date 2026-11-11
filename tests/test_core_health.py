from __future__ import annotations

import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


@pytest.mark.parametrize("omp_version,version_ok", [
    ("18.18.8", True), ("18.18.9", True), ("18.19.0", False),
    ("19.18.8", False), ("unparsed", False),
])
@pytest.mark.parametrize("values,expected", [([True, False], True), ([False, False], False), ([True, True], False)])
def test_doctor_detects_ignored_no_fallback_overlay_and_does_not_expose_credentials(
    tmp_path, monkeypatch, values, expected, omp_version, version_ok,
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
        if argv == ["omp", "--version"]:
            return subprocess.CompletedProcess(argv, 0, omp_version, "")
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
    assert checks["omp-version"][0] is version_ok
    assert f"installed={omp_version}" in checks["omp-version"][1]
    assert checks["keys"][0] is False
    assert checks["postgres-schema"][0] is False
    assert checks["github"][0] is True
    assert secret not in json.dumps(checks)
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.fixture
def core_checks(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("package = []\n")
    monkeypatch.setenv("EVALLAB_CLOUD_TRACE_DB", str(tmp_path / "trace.sqlite"))
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(tmp_path / "derived-root"))
    monkeypatch.setenv("EVALLAB_FAILURE_LIBRARY_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(automation.shutil, "which", lambda name: None)
    monkeypatch.setattr(backups, "backup_freshness", lambda *a, **kw: {
        "status": "missing", "age_hours": None, "max_age_hours": 36, "newest_verified": None,
    })
    monkeypatch.setattr(io, "retained_source_health", lambda *a, **kw: (False, "fixture bundles unavailable"))
    monkeypatch.setattr(gold, "load_rows", lambda *a, **kw: [])

    def unavailable(*args, **kwargs):
        raise ConnectionError("fixture database unavailable")

    monkeypatch.setattr(database.psycopg, "connect", unavailable)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    def checks():
        return {name: (ok, detail) for name, ok, detail in automation.core_runtime_checks(
            tmp_path, home=tmp_path, now=NOW, run=fake_run,
        )}

    return checks


def test_doctor_rejects_sqlite_bad_header(tmp_path, core_checks):
    path = tmp_path / "trace.sqlite"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE t(x)")
        connection.commit()
    finally:
        connection.close()
    header = bytearray(path.read_bytes())
    header[18:20] = b"\x00\x00"
    path.write_bytes(header)

    checks = core_checks()

    ok, detail = next(value for name, value in checks.items() if name.startswith("sqlite:"))
    assert ok is False
    assert "format_rw=0000" in detail


def test_doctor_rejects_duckdb_missing_magic(tmp_path, monkeypatch, core_checks):
    path = tmp_path / "derived" / "store.duckdb"
    path.parent.mkdir()
    path.write_bytes(b"CORRUPT" * 4)
    fake_duckdb = ModuleType("duckdb")

    def connect(path, **kwargs):
        return nullcontext(SimpleNamespace(
            execute=lambda sql, *args: SimpleNamespace(fetchone=lambda: ("fixture",)),
        ))

    monkeypatch.setattr(fake_duckdb, "connect", connect, raising=False)
    monkeypatch.setitem(sys.modules, "duckdb", fake_duckdb)

    checks = core_checks()

    ok, detail = checks["duckdb:store.duckdb"]
    assert ok is False
    assert "storage_version=" in detail


def test_doctor_reports_empty_derived_root(tmp_path, core_checks):
    derived = tmp_path / "derived-root"
    derived.mkdir()

    checks = core_checks()

    assert checks["parquet-stores"] == (False, f"projections=MISSING root={derived}")


def test_doctor_reads_nonempty_parquet_store(tmp_path, core_checks):
    import pyarrow as pa
    import pyarrow.parquet as pq

    derived = tmp_path / "derived-root"
    derived.mkdir()
    pq.write_table(pa.table({"x": [1]}), derived / "fixture.parquet")

    checks = core_checks()

    ok, detail = checks["parquet-stores"]
    assert ok is True
    assert "files=1" in detail


def test_doctor_reports_postgres_connection_failure_without_credentials(monkeypatch, core_checks):
    secret = "credential-value-must-not-appear"

    def unavailable(*args, **kwargs):
        raise ConnectionError(secret)

    monkeypatch.setattr(database.psycopg, "connect", unavailable)

    checks = core_checks()

    assert checks["postgres-schema"] == (False, "unavailable: ConnectionError")
    assert secret not in json.dumps(checks)


def test_doctor_marks_postgres_unavailable_without_server_version(
    tmp_path, monkeypatch, core_checks,
):
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql/schema.sql").write_text("CREATE TABLE t(c text);\n")

    def connect(*args, **kwargs):
        return nullcontext(SimpleNamespace(
            execute=lambda sql: SimpleNamespace(fetchone=lambda: None, fetchall=lambda: []),
        ))

    monkeypatch.setattr(database.psycopg, "connect", connect)

    checks = core_checks()

    assert checks["postgres-schema"] == (False, "unavailable: ValueError")


def test_doctor_reachable_postgres_requires_schema_revision(tmp_path, monkeypatch, core_checks):
    schema = tmp_path / "sql" / "schema.sql"
    schema.parent.mkdir()
    schema.write_text("CREATE TABLE t(c text);\n")

    def execute(sql):
        if "SHOW server_version" in sql:
            return SimpleNamespace(fetchone=lambda: ("18.4",))
        assert "information_schema.columns" in sql
        return SimpleNamespace(fetchall=lambda: [["t", "c", "text"]])

    monkeypatch.setattr(database.psycopg, "connect", lambda *args, **kwargs: nullcontext(
        SimpleNamespace(execute=execute),
    ))

    checks = core_checks()

    ok, detail = checks["postgres-schema"]
    assert ok is False
    assert "reachable=True installed=18.4" in detail
    assert "schema_revision=MISSING" in detail


@pytest.mark.parametrize("requirement,version,expected", [
    (">=3.12", "3.11.12", False),
    (">=3.12", "3.12.11", True),
    (">=3.12", "3.13.7", True),
    (">=3.14", "3.13.7", False),
    (">=3.14", "3.14.0", True),
])
def test_python_health_uses_project_requirement_not_ci_matrix(
    tmp_path, monkeypatch, requirement, version, expected,
):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nrequires-python = "{requirement}"\n')
    monkeypatch.setattr(automation, "sys", SimpleNamespace(version=version))
    ok, _ = automation._python_runtime_health(tmp_path)
    assert ok is expected


def test_doctor_probes_cover_keychain_postgres_and_disk(tmp_path, monkeypatch):
    doctor = automation.HeadlessDoctor(tmp_path, home=tmp_path, executor=SimpleNamespace())
    monkeypatch.setattr(automation.profiles_module, "probe_claude_keychain", lambda: True)
    assert doctor._probe_keychain() is True

    monkeypatch.setattr(database, "ping", lambda url: None)
    assert doctor._probe_postgres() is True

    def unavailable(url):
        raise RuntimeError("fixture database unavailable")

    monkeypatch.setattr(database, "ping", unavailable)
    assert doctor._probe_postgres() is False

    monkeypatch.setattr(automation.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1000 * 1024**3, free=1000 * 1024**3,
    ))
    assert doctor._probe_disk() is True


def test_launchctl_wrapper_raises_on_checked_failure(monkeypatch):
    monkeypatch.setattr(automation, "subscription_environment", lambda: {})
    completed = automation._launchctl(["/usr/bin/true"], check=True)
    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.returncode == 0

    with pytest.raises(RuntimeError, match="launchctl exited 1"):
        automation._launchctl(["/usr/bin/false"], check=True)

    assert automation._launchctl(["/usr/bin/false"], check=False).returncode == 1


def test_installed_job_health_marks_broken_plists_unavailable(tmp_path):
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    foreign = agents / "foreign.plist"
    foreign.write_bytes(b"not a plist")
    broken = agents / f"{automation.LAUNCH_AGENT_LABEL_PREFIX}broken.plist"
    broken.write_bytes(b"not a plist")
    mismatch = agents / f"{automation.LAUNCH_AGENT_LABEL_PREFIX}mismatch.plist"
    mismatch.write_bytes(plistlib.dumps({"Label": "wrong-label"}))

    checks = automation.installed_job_health(tmp_path, now=NOW, run=_loaded)

    assert {name for name, _, _ in checks} == {
        f"launchd:{broken.stem}", f"launchd:{mismatch.stem}",
    }
    assert all(ok is False and detail.startswith("unavailable:") for _, ok, detail in checks)
    assert all(foreign.stem not in name for name, _, _ in checks)
    assert next(detail for name, _, detail in checks if name.endswith(mismatch.stem)) == "unavailable: ValueError"


def test_installed_job_health_uses_newest_insights_pass(tmp_path):
    _installed_job(tmp_path, "insights", interval=300)
    passes = tmp_path / ".local/state/evallab-insights/passes"
    one = passes / "a/out/one.json"
    two = passes / "b/out/two.json"
    for path, age in [(one, timedelta(minutes=10)), (two, timedelta(minutes=1))]:
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        stamp = (NOW - age).timestamp()
        os.utime(path, (stamp, stamp))

    [(name, ok, detail)] = automation.installed_job_health(tmp_path, now=NOW, run=_loaded)

    assert name.endswith("insights")
    assert ok is True
    assert f"output={two}" in detail
    assert str(one) not in detail


def test_installed_job_health_clears_age_for_unsynced_primary(tmp_path):
    _installed_job(tmp_path, "sync-primary")

    [(name, ok, detail)] = automation.installed_job_health(tmp_path, now=NOW, run=_loaded)

    assert name.endswith("sync-primary")
    assert ok is False
    assert "useful_age_h=MISSING" in detail


def test_sync_primary_status_rejects_unreadable_rows(tmp_path):
    for filename, content, reason in [
        ("malformed.json", "{", "JSONDecodeError"),
        ("array.json", "[]", "not-an-object"),
        ("bad-timestamp.json", json.dumps({"timestamp": "not-a-date"}), "bad-timestamp"),
    ]:
        path = tmp_path / filename
        path.write_text(content)

        status = automation.sync_primary_status(tmp_path, status_path=path, now=NOW)

        assert status.ok is False
        assert status.outcome == "unreadable"
        assert status.reason == reason

    path = tmp_path / "naive-timestamp.json"
    path.write_text(json.dumps({
        "timestamp": "2026-11-11T14:00:00", "outcome": "synced", "reason": "clean", "behind": 0,
    }))

    status = automation.sync_primary_status(tmp_path, status_path=path, now=NOW)

    assert status.ok is True
    assert status.outcome == "synced"
    assert status.age_seconds == 0
