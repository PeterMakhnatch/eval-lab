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


def _scratch_with_sidecar(tmp_path: Path) -> Path:
    """A scratch repo holding one durable, valid analysis sidecar."""
    scratch = tmp_path / "complete"
    shutil.copytree(FIXTURES / "complete", scratch)
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
