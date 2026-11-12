"""What a command *tells the operator* is part of its contract (M009 F-02/08/09/11/12).

Every test here fixes a line an operator reads and then acts on: the id the
next command wants, the database a green check inspected, whether a write
reached the catalog. A command that prints something the next step cannot
consume is a defect even when its exit code is 0.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab import cli, database
from evallab.cli import run_cli
from evallab.evidence.facts import AnalyzerCallResult, run_trial_analysis
from evallab.results import load_job

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/operability"
PROMPT = ROOT / "research/analysis/stage5-prompt.md"
RUBRIC = ROOT / "research/analysis/stage5-rubric.json"
STUB = ROOT / "research/analysis/stub-oracle-analysis.json"
FIXED = datetime(2026, 8, 14, tzinfo=UTC)
CATALOG_URL = "postgresql://evallab:local-development-only@catalog.test:54329/evallab"




class RecordingConnection:
    """A psycopg stand-in that records every statement the command issues."""

    def __init__(self, statements: list[tuple[str, Any]]) -> None:
        self._statements = statements

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: Any = None) -> RecordingConnection:
        self._statements.append((query, parameters))
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


@pytest.fixture
def catalog_statements(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    statements: list[tuple[str, Any]] = []
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(
        database.psycopg,
        "connect",
        lambda *_args, **_kwargs: RecordingConnection(statements),
    )
    return statements


def _scratch_repo(tmp_path: Path) -> Path:
    """A scratch workspace with one completed job and the committed stage-5 inputs."""
    scratch = tmp_path / "complete"
    shutil.copytree(FIXTURES / "complete", scratch)
    analysis_inputs = scratch / "research/analysis"
    analysis_inputs.mkdir(parents=True)
    for source in (PROMPT, RUBRIC, STUB):
        shutil.copy2(source, analysis_inputs / source.name)
    return scratch


def _scratch_with_sidecar(tmp_path: Path) -> Path:
    """A scratch repo holding one durable, valid analysis sidecar."""
    scratch = _scratch_repo(tmp_path)
    job = load_job(scratch / "jobs/operability-join")

    def analyzer(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        return AnalyzerCallResult(raw_output=STUB.read_text(), cost_usd=0.0)

    sidecar_path, sidecar = run_trial_analysis(
        job,
        job.trials[0],
        analyzer=analyzer,
        repo_root=scratch,
        destination_root=scratch / "derived/analyses",
        prompt_path=PROMPT,
        rubric_path=RUBRIC,
        agent="stub",
        agent_version="1",
        model="saved-response",
        created_at=FIXED,
    )
    assert sidecar.validation_status == "valid"
    return sidecar_path


def _inserted(statements: list[tuple[str, Any]], table: str) -> list[Any]:
    return [parameters for query, parameters in statements if f"INSERT INTO {table}" in query]


# ---- F-02: `analyze review` indexes its own output --------------------------


# ---- F-11: doctor names the database it inspected, never the password -------


def test_doctor_names_the_catalog_it_inspected_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.evidence.atif import ProjectionInvariant

    task = tmp_path / "library/tasks/event-summary/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text("version = 1\n")
    monkeypatch.setenv("DATABASE_URL", CATALOG_URL)
    monkeypatch.setattr(
        cli.Executor,
        "from_repo",
        lambda root: type("R", (), {"local_runtime_checks": lambda self: []})(),
    )
    monkeypatch.setattr(cli.database, "ping", lambda url: "PostgreSQL 16")
    monkeypatch.setattr(
        cli,
        "check_projection_invariant",
        lambda url, output, events: ProjectionInvariant(
            catalog_job_ids=frozenset({"job"}),
            projected_job_ids=frozenset({"job"}),
            excepted_job_ids=frozenset(),
            missing_job_ids=frozenset(),
            extra_job_ids=frozenset(),
        ),
    )

    assert cli._doctor(tmp_path, home=tmp_path / "home") == 0

    out = capsys.readouterr().out
    catalog_line = next(line for line in out.splitlines() if "catalog-parquet" in line)
    assert "catalog=1 projected=1" in catalog_line  # the counts stay first
    assert "db=catalog.test:54329/evallab" in catalog_line
    assert "local-development-only" not in out
    assert "local-development-only" not in capsys.readouterr().err


def test_doctor_names_the_catalog_even_when_postgres_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = tmp_path / "library/tasks/event-summary/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text("version = 1\n")
    monkeypatch.setenv("DATABASE_URL", CATALOG_URL)
    monkeypatch.setattr(
        cli.Executor,
        "from_repo",
        lambda root: type("R", (), {"local_runtime_checks": lambda self: []})(),
    )

    def refuse(url: str) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(cli.database, "ping", refuse)

    assert cli._doctor(tmp_path, home=tmp_path / "home") == 1

    out = capsys.readouterr().out
    catalog_line = next(line for line in out.splitlines() if "catalog-parquet" in line)
    assert "db=catalog.test:54329/evallab" in catalog_line
    assert "local-development-only" not in out


def test_database_identity_never_returns_a_password() -> None:
    assert database.identity(CATALOG_URL) == "catalog.test:54329/evallab"
    assert database.identity("host=db port=6000 dbname=c password=hunter2") == "db:6000/c"
    assert "hunter2" not in database.identity("host=db port=6000 dbname=c password=hunter2")
    assert database.identity("=== not a connection string") == "unparsable connection string"


# ---- HAR-246: doctor surfaces the primary checkout's sync timer ---------------

SYNC_STATUS_RELATIVE = Path("Library/Application Support/evallab/sync-status.json")


@pytest.fixture
def sync_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No test may inherit the developer's sync status path or interval."""
    monkeypatch.delenv("EVALLAB_SYNC_STATUS", raising=False)
    monkeypatch.delenv("EVALLAB_SYNC_INTERVAL_SECONDS", raising=False)
    return monkeypatch


def _write_sync_status(home: Path, **fields: Any) -> Path:
    path = home / SYNC_STATUS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "timestamp": "2026-08-13T23:59:30Z",
        "outcome": "up-to-date",
        "reason": "up-to-date",
        "dirty_files": 0,
        "ahead": 0,
        "behind": 0,
        "fetch_ok": True,
    }
    payload.update(fields)
    path.write_text(json.dumps(payload))
    return path


def test_sync_primary_row_reports_an_up_to_date_timer(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"
    _write_sync_status(home)

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is True
    assert (status.outcome, status.reason, status.behind, status.age_seconds) == (
        "up-to-date",
        "up-to-date",
        0,
        30,
    )
    assert status.detail == "up-to-date (up-to-date) behind 0 age 30s"


def test_sync_primary_row_fails_when_the_timer_skipped_a_checkout_behind(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"
    _write_sync_status(
        home,
        timestamp="2026-08-13T23:55:00Z",
        outcome="skipped",
        reason="dirty",
        dirty_files=2,
        behind=848,
    )

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is False
    assert (status.outcome, status.reason, status.behind, status.age_seconds) == (
        "skipped",
        "dirty",
        848,
        300,
    )
    assert status.detail == (
        "skipped (dirty) behind 848 age 5m; skipped while 848 commits behind"
    )


def test_sync_primary_row_fails_when_the_last_run_is_stale(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"
    _write_sync_status(home, timestamp="2026-08-13T22:00:00Z")  # two hours before FIXED

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is False
    # The timer's own verdict is still reported; only doctor's is FAIL.
    assert status.outcome == "up-to-date"
    assert status.age_seconds == 7200
    assert "stale: last run 2h0m ago (limit 900s)" in status.detail


def test_sync_primary_row_fails_when_the_status_file_is_missing(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is False
    assert status.outcome == "missing"
    assert status.age_seconds is None
    assert str(home / SYNC_STATUS_RELATIVE) in status.detail


def test_sync_primary_row_fails_when_the_last_fetch_failed(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"
    _write_sync_status(
        home,
        outcome="skipped",
        reason="fetch-failed",
        fetch_ok=False,
        fetch_error="Could not resolve host: github.com",
    )

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is False
    assert status.detail.endswith("fetch failed")
    assert "fetch-failed" in status.detail


def test_sync_primary_row_takes_the_status_path_and_interval_from_the_environment(
    tmp_path: Path, sync_environment: pytest.MonkeyPatch
) -> None:
    from evallab.automation import sync_primary_status

    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "state/sync-status.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-13T22:00:00Z",
                "outcome": "synced",
                "reason": "synced",
                "behind": 0,
                "fetch_ok": True,
            }
        )
    )
    sync_environment.setenv("EVALLAB_SYNC_STATUS", str(elsewhere))
    sync_environment.setenv("EVALLAB_SYNC_INTERVAL_SECONDS", "3600")

    status = sync_primary_status(home, now=FIXED)

    assert status.ok is True  # two hours is inside 3 x 3600s
    assert status.outcome == "synced"


def _stub_doctor_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from evallab.evidence.atif import ProjectionInvariant

    monkeypatch.setattr(cli.database, "ping", lambda url: "PostgreSQL 16")
    monkeypatch.setattr(
        cli,
        "check_projection_invariant",
        lambda url, output, events: ProjectionInvariant(
            catalog_job_ids=frozenset({"job"}),
            projected_job_ids=frozenset({"job"}),
            excepted_job_ids=frozenset(),
            missing_job_ids=frozenset(),
            extra_job_ids=frozenset(),
        ),
    )


def _stub_local_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli.Executor,
        "from_repo",
        lambda root: type("R", (), {"local_runtime_checks": lambda self: []})(),
    )
    _stub_doctor_database(monkeypatch)


def test_doctor_prints_the_sync_primary_row_and_keeps_it_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sync_environment: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "library/tasks/event-summary/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text("version = 1\n")
    _stub_local_doctor(monkeypatch)
    home = tmp_path / "home"
    _write_sync_status(
        home,
        timestamp="2026-08-13T23:55:00Z",
        outcome="skipped",
        reason="dirty",
        dirty_files=2,
        behind=848,
    )

    # FAIL is reported, but sync-primary is not in doctor's required set.
    assert cli._doctor(tmp_path, home=home, clock=lambda: FIXED) == 0
    line = next(
        row for row in capsys.readouterr().out.splitlines() if "sync-primary" in row
    )
    assert line.startswith("FAIL  sync-primary   ")
    assert "skipped (dirty) behind 848" in line

    _write_sync_status(home)

    assert cli._doctor(tmp_path, home=home, clock=lambda: FIXED) == 0
    line = next(
        row for row in capsys.readouterr().out.splitlines() if "sync-primary" in row
    )
    assert line.startswith("ok    sync-primary   up-to-date (up-to-date) behind 0 age 30s")


# ---- F-09: `submit` prints the id the next command wants --------------------




def test_submit_prints_the_bare_spec_id_approve_wants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.queue import DirectoryQueue
    from evallab.queue import database as queue_database

    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    # `Executor.submit` refuses to admit anything it cannot cost-check, so it
    # probes the catalog for today's spend and the recent harness-failure run.
    # Neither is what this test is about, and a runner without Postgres would
    # otherwise see `cannot enforce cost policy because the catalog is
    # unavailable` and exit 2. Pin both to a clean, admitting state.
    monkeypatch.setattr(queue_database, "daily_cost_usd", lambda url, day: 0.0)
    monkeypatch.setattr(queue_database, "consecutive_harness_failures", lambda url: 0)
    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    shutil.copytree(ROOT / "library/tasks/event-summary", tmp_path / "library/tasks/event-summary")
    spec = tmp_path / "spec.json"
    spec.write_text(
        '{"name": "operator-fixes-control", "hypothesis": "the control runs",'
        ' "purpose": "practice",'
        ' "task": "library/tasks/event-summary", "agent": "oracle",'
        ' "submitted_by": "operator-fixes", "est_cost_usd": 0}'
    )

    assert run_cli(["submit", str(spec)], workspace=tmp_path) == 0

    out = capsys.readouterr().out
    spec_id = next(
        line.split("spec_id: ")[1] for line in out.splitlines() if line.startswith("spec_id: ")
    )
    # The printed id is exactly what `approve`, `reject`, and the catalog's
    # `experiment_id` column consume — no prefix, no extension, no directory.
    located = DirectoryQueue(tmp_path / "queue").locate(spec_id)
    assert located.is_file()
    assert spec_id == located.stem.rsplit("-", 1)[-1]
    assert "state: " in out
    assert "path: " in out

