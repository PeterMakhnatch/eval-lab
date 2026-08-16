"""What a command *tells the operator* is part of its contract (M009 F-02/08/09/11/12).

Every test here fixes a line an operator reads and then acts on: the id the
next command wants, the database a green check inspected, whether a write
reached the catalog. A command that prints something the next step cannot
consume is a defect even when its exit code is 0.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab import cli, database
from evallab.cli import run_cli
from evallab.facts import AnalyzerCallResult, run_trial_analysis
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
    return [
        parameters
        for query, parameters in statements
        if f"INSERT INTO {table}" in query
    ]


# ---- F-02: `analyze review` indexes its own output --------------------------


def test_analyze_review_index_populates_analysis_reviews(
    tmp_path: Path, catalog_statements: list[tuple[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    sidecar_path = _scratch_with_sidecar(tmp_path)
    scratch = sidecar_path.parents[2]

    assert (
        run_cli(
            [
                "analyze", "review", str(sidecar_path),
                "--disposition", "accepted",
                "--rationale", "the control did no work, as designed",
                "--reviewer", "operator-fixes",
                "--index",
                "--database-url", CATALOG_URL,
            ],
            workspace=scratch,
        )
        == 0
    )

    review_path = next((sidecar_path.parent / "reviews").glob("*.json"))
    (row,) = _inserted(catalog_statements, "analysis_reviews")
    review_id, _analysis_id, disposition, _rationale, reviewer = row[:5]
    assert review_id == review_path.stem
    assert (disposition, reviewer) == ("accepted", "operator-fixes")

    out = capsys.readouterr().out
    assert f"indexed review: {review_id}" in out
    assert "catalog: catalog.test:54329/evallab" in out


def test_analyze_review_without_index_names_the_command_that_indexes(
    tmp_path: Path, catalog_statements: list[tuple[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    sidecar_path = _scratch_with_sidecar(tmp_path)
    scratch = sidecar_path.parents[2]

    assert (
        run_cli(
            [
                "analyze", "review", str(sidecar_path),
                "--disposition", "accepted",
                "--rationale", "the control did no work, as designed",
                "--reviewer", "operator-fixes",
            ],
            workspace=scratch,
        )
        == 0
    )

    assert catalog_statements == []  # no --index means no catalog write
    out = capsys.readouterr().out
    assert "indexed: no" in out
    assert f"next: uv run evallab analyze ingest-sidecar {sidecar_path}" in out


def test_analyze_review_on_a_missing_sidecar_says_what_to_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)

    assert (
        run_cli(
            [
                "analyze", "review", "derived/analyses/nope",
                "--disposition", "accepted",
                "--rationale", "r",
                "--reviewer", "operator-fixes",
            ],
            workspace=tmp_path,
        )
        == 2
    )

    err = capsys.readouterr().err
    assert "no analysis sidecar at" in err
    assert "derived/analyses/<analysis_id>/analysis.json" in err


# ---- F-11: doctor names the database it inspected, never the password -------


def test_doctor_names_the_catalog_it_inspected_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.atif import ProjectionInvariant

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

    assert cli._doctor(tmp_path) == 0

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

    assert cli._doctor(tmp_path) == 1

    out = capsys.readouterr().out
    catalog_line = next(line for line in out.splitlines() if "catalog-parquet" in line)
    assert "db=catalog.test:54329/evallab" in catalog_line
    assert "local-development-only" not in out


def test_database_identity_never_returns_a_password() -> None:
    assert database.identity(CATALOG_URL) == "catalog.test:54329/evallab"
    assert database.identity("host=db port=6000 dbname=c password=hunter2") == "db:6000/c"
    assert "hunter2" not in database.identity("host=db port=6000 dbname=c password=hunter2")
    assert database.identity("=== not a connection string") == "unparsable connection string"




# ---- F-09: `submit` prints the id the next command wants --------------------


def test_submit_prints_the_bare_spec_id_approve_wants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    spec = tmp_path / "spec.json"
    spec.write_text(
        '{"name": "operator-fixes-control", "hypothesis": "the control runs",'
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
    from evallab.queue import DirectoryQueue

    located = DirectoryQueue(tmp_path / "queue").locate(spec_id)
    assert located.is_file()
    assert spec_id == located.stem.rsplit("-", 1)[-1]
    assert "state: " in out
    assert "path: " in out


# ---- F-12: `analyze stub --index` says what it indexed, and where -----------


def test_analyze_stub_index_reports_what_it_indexed(
    tmp_path: Path, catalog_statements: list[tuple[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = _scratch_repo(tmp_path)
    response = scratch / "saved-response.json"
    response.write_text(STUB.read_text())

    assert (
        run_cli(
            [
                "analyze", "stub", "jobs/operability-join/join-trial",
                "--response", str(response),
                "--index",
                "--database-url", CATALOG_URL,
            ],
            workspace=scratch,
        )
        == 0
    )

    out = capsys.readouterr().out
    analysis_id = next(
        line.split("analysis: ")[1] for line in out.splitlines() if line.startswith("analysis: ")
    )
    analysis_id = Path(analysis_id).parent.name
    assert f"indexed analysis: {analysis_id}" in out
    assert "catalog: catalog.test:54329/evallab" in out


def test_analyze_stub_without_index_says_it_did_not_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    scratch = _scratch_repo(tmp_path)
    response = scratch / "saved-response.json"
    response.write_text(STUB.read_text())

    assert (
        run_cli(
            [
                "analyze", "stub", "jobs/operability-join/join-trial",
                "--response", str(response),
            ],
            workspace=scratch,
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "indexed: no" in out
    assert "next: uv run evallab analyze ingest-sidecar " in out


def test_ingest_sidecar_reports_the_reviews_it_swept_in(
    tmp_path: Path, catalog_statements: list[tuple[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.facts import write_analysis_review

    sidecar_path = _scratch_with_sidecar(tmp_path)
    scratch = sidecar_path.parents[2]
    write_analysis_review(
        sidecar_path,
        disposition="accepted",
        rationale="the control did no work, as designed",
        reviewer="operator-fixes",
    )

    assert (
        run_cli(
            [
                "analyze", "ingest-sidecar", str(sidecar_path),
                "--database-url", CATALOG_URL,
            ],
            workspace=scratch,
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "indexed reviews: 1" in out
    assert "catalog: catalog.test:54329/evallab" in out
    assert len(_inserted(catalog_statements, "analysis_reviews")) == 1
