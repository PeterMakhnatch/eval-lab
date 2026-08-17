"""Tests for discovery verdict persistence, validation, and views (§2.1, §2.2).

Verdicts are append-only human judgements on discovery findings.
CI has no PostgreSQL or derived corpus — tests use tmp_path and in-memory DuckDB.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from evallab import cli
from evallab.runner import database_url_from_environment
from evallab.schemas import Verdict
from evallab.verdicts import (
    DEFAULT_DISCOVERIES_PATH,
    SQL_VERDICTS_PATH,
    execute_verdicts_views,
    format_verdict_history_table,
    format_verdicts_table,
    get_verdict_history_from_catalog,
    get_verdict_history_from_duckdb,
    list_current_verdicts_from_catalog,
    list_current_verdicts_from_duckdb,
    record_verdict,
    resolve_discovery_ids,
    validate_discovery_id,
    validate_human_actor,
    validate_status,
)

SAMPLE_DISCOVERY_HEADER_ID = "D-20260815-KTXJSHGZ"
SAMPLE_DISCOVERY_HEADER_ID_2 = "D-20260816-7CQRVDQ6"
SAMPLE_CITATION_ULID = "01KZZCK33HJM4R8HW3V0Y25DXE"

def _create_sample_discoveries_journal(root: Path) -> Path:
    journal_path = root / DEFAULT_DISCOVERIES_PATH
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        f"""# Eval lab discovery journal

## {SAMPLE_DISCOVERY_HEADER_ID} — draft

- Claim: Across this small control-only cohort, verifiers showed expected pattern.
- Builds on: new thread
- Evidence:
  - [queue/researchers/passes/2026-08-15/{SAMPLE_CITATION_ULID}/evidence.json](../evidence.json)
- Proposed spec: [queue/proposed/spec-01KZZCN7X9PA643W1QCKQNNNY5.json](../spec.json)

## {SAMPLE_DISCOVERY_HEADER_ID_2} — draft

- Claim: A verified second finding for test cohorts.
- Builds on: {SAMPLE_DISCOVERY_HEADER_ID}
""",
        encoding="utf-8",
    )
    return journal_path


def test_standalone_sql_script_with_fallbacks() -> None:
    """Test that sql/verdicts.sql resolves in clean DuckDB with zero pre-created tables."""
    sql = SQL_VERDICTS_PATH.read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in ["v_verdicts_history", "v_current_verdicts"]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def test_verdict_roundtrip_duckdb(tmp_path: Path) -> None:
    """A verdict round-trips: written, then returned with right status and actor."""
    _create_sample_discoveries_journal(tmp_path)

    with duckdb.connect(":memory:") as con:
        execute_verdicts_views(con)

        now = datetime.now(UTC)
        verdict = record_verdict(
            SAMPLE_DISCOVERY_HEADER_ID,
            "accepted",
            by="Peter Makhnatch",
            note="Verified against run evidence",
            at=now,
            repo_root=tmp_path,
            duckdb_conn=con,
        )

        assert verdict.discovery_id == SAMPLE_DISCOVERY_HEADER_ID
        assert verdict.status == "accepted"
        assert verdict.by == "Peter Makhnatch"
        assert verdict.note == "Verified against run evidence"

        current = list_current_verdicts_from_duckdb(con)
        assert len(current) == 1
        assert current[0].discovery_id == SAMPLE_DISCOVERY_HEADER_ID
        assert current[0].status == "accepted"
        assert current[0].by == "Peter Makhnatch"
        assert current[0].note == "Verified against run evidence"
        assert current[0].at == now


def test_verdict_append_only_history(tmp_path: Path) -> None:
    """Two verdicts persist; current reports later, history reports both oldest-first."""
    _create_sample_discoveries_journal(tmp_path)

    with duckdb.connect(":memory:") as con:
        execute_verdicts_views(con)

        t1 = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

        v1 = record_verdict(
            SAMPLE_DISCOVERY_HEADER_ID,
            "pending",
            by="Peter Makhnatch",
            note="Initial triage",
            at=t1,
            repo_root=tmp_path,
            duckdb_conn=con,
        )

        record_verdict(
            SAMPLE_DISCOVERY_HEADER_ID,
            "accepted",
            by="Peter Makhnatch",
            note="Promoted after evidence review",
            at=t2,
            repo_root=tmp_path,
            duckdb_conn=con,
        )

        # Current view returns only the latest verdict (v2)
        current = list_current_verdicts_from_duckdb(con)
        assert len(current) == 1
        assert current[0].status == "accepted"
        assert current[0].note == "Promoted after evidence review"
        assert current[0].at == t2

        # History returns all rows oldest-first
        history = get_verdict_history_from_duckdb(con, SAMPLE_DISCOVERY_HEADER_ID)
        assert len(history) == 2
        assert history[0].status == "pending"
        assert history[0].note == "Initial triage"
        assert history[0].at == t1
        assert history[1].status == "accepted"
        assert history[1].note == "Promoted after evidence review"
        assert history[1].at == t2

        # Invariant: the first row in history is strictly unchanged
        assert history[0].status == v1.status
        assert history[0].by == v1.by
        assert history[0].at == v1.at
        assert history[0].note == v1.note


def test_refuse_empty_or_whitespace_actor() -> None:
    """An empty or whitespace-only actor is refused."""
    for bad_actor in ["", "   ", "\t\n"]:
        with pytest.raises(ValueError, match="Actor \\(--by\\) is required and cannot be empty"):
            validate_human_actor(bad_actor)


def test_refuse_automated_actor() -> None:
    """An obviously-automated actor name is refused with a clear guidance message."""
    automated_names = [
        "autopilot",
        "autopilot-researcher",
        "bot",
        "agent",
        "harbor",
        "automated",
        "ci",
        "github-actions",
        "codex",
        "oracle",
        "nop",
        "system",
        "agent-worker",
        "bot-auto",
        "ai-assistant",
        "automated-pipeline",
        "harbor-evaluator",
        "codex-judge",
    ]
    for bad_actor in automated_names:
        with pytest.raises(
            ValueError, match="Automated actor .* refused: verdicts require human judgment"
        ):
            validate_human_actor(bad_actor)


def test_refuse_unknown_discovery_id(tmp_path: Path) -> None:
    """A verdict on an unknown discovery_id is refused, naming what was not found."""
    _create_sample_discoveries_journal(tmp_path)
    unknown_id = "D-20260815-NONEXIST"

    with pytest.raises(ValueError, match=f"Discovery '{unknown_id}' not found"):
        validate_discovery_id(unknown_id, repo_root=tmp_path)


def test_refuse_malformed_discovery_id(tmp_path: Path) -> None:
    """Malformed discovery IDs are refused at validation time."""
    _create_sample_discoveries_journal(tmp_path)
    for bad_id in [
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "not-a-discovery-id",
        "D-2026081-SHORT",
        "20260815-KTXJSHGZ",
    ]:
        with pytest.raises(ValueError):
            record_verdict(
                bad_id,
                "accepted",
                by="Peter Makhnatch",
                repo_root=tmp_path,
            )


def test_record_verdict_on_real_committed_journal() -> None:
    """Record a verdict on a real ID from the committed journal (D-20260815-KTXJSHGZ)."""
    with duckdb.connect(":memory:") as con:
        execute_verdicts_views(con)
        now = datetime.now(UTC)
        verdict = record_verdict(
            "D-20260815-KTXJSHGZ",
            "accepted",
            by="Peter Makhnatch",
            note="Verified against real committed journal",
            at=now,
            duckdb_conn=con,
        )
        assert verdict.discovery_id == "D-20260815-KTXJSHGZ"
        assert verdict.status == "accepted"
        assert verdict.by == "Peter Makhnatch"

        current = list_current_verdicts_from_duckdb(con)
        assert len(current) == 1
        assert current[0].discovery_id == "D-20260815-KTXJSHGZ"
        assert current[0].status == "accepted"

        history = get_verdict_history_from_duckdb(con, "D-20260815-KTXJSHGZ")
        assert len(history) == 1
        assert history[0].discovery_id == "D-20260815-KTXJSHGZ"


def test_refuse_invalid_status() -> None:
    """A status outside the four §2.1 literals is refused."""
    for bad_status in ["maybe", "approved", "failed", "unreviewed", ""]:
        with pytest.raises(ValueError, match="Invalid status .* must be one of:"):
            validate_status(bad_status)


def test_resolve_discovery_ids_parsing(tmp_path: Path) -> None:
    """Discovery IDs are correctly extracted from headers and embedded citations."""
    _create_sample_discoveries_journal(tmp_path)
    ids = resolve_discovery_ids(repo_root=tmp_path)

    assert SAMPLE_DISCOVERY_HEADER_ID in ids
    assert SAMPLE_DISCOVERY_HEADER_ID_2 in ids
    assert SAMPLE_CITATION_ULID in ids
    assert "01KZZCN7X9PA643W1QCKQNNNY5" in ids


def test_cli_verdict_record_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI records a verdict and lists current verdicts, proving the round trip."""
    _create_sample_discoveries_journal(tmp_path)

    code = cli.run_cli(
        [
            "verdict",
            SAMPLE_DISCOVERY_HEADER_ID,
            "accepted",
            "--by",
            "Peter Makhnatch",
            "--note",
            "Verified via CLI",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    out, _ = capsys.readouterr()
    assert f"Recorded verdict for {SAMPLE_DISCOVERY_HEADER_ID}: accepted by Peter Makhnatch" in out

    # CLI list returns the recorded verdict
    code_list = cli.run_cli(["verdict", "list"], workspace=tmp_path)
    assert code_list == 0
    out_list, _ = capsys.readouterr()
    assert SAMPLE_DISCOVERY_HEADER_ID in out_list
    assert "accepted" in out_list
    assert "Peter Makhnatch" in out_list

def test_cli_verdict_refusal_unknown_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI refuses unknown discovery ID and outputs error."""
    _create_sample_discoveries_journal(tmp_path)
    unknown_id = "D-20260815-NONEXIST"

    code = cli.run_cli(
        ["verdict", unknown_id, "accepted", "--by", "Peter Makhnatch"],
        workspace=tmp_path,
    )
    assert code == 2
    _, err = capsys.readouterr()
    assert f"Discovery '{unknown_id}' not found" in err


def test_cli_verdict_refusal_automated_by(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI refuses automated --by actor."""
    _create_sample_discoveries_journal(tmp_path)

    code = cli.run_cli(
        ["verdict", SAMPLE_DISCOVERY_HEADER_ID, "accepted", "--by", "autopilot"],
        workspace=tmp_path,
    )
    assert code == 2
    _, err = capsys.readouterr()
    assert "Automated actor 'autopilot' refused" in err


def test_format_verdicts_table() -> None:
    """Formatting renders headers and rows or empty message."""
    assert format_verdicts_table([]) == "No verdicts recorded."

    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    v = [
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="accepted",
            by="Peter Makhnatch",
            at=now,
            note="Note here",
        )
    ]
    rendered = format_verdicts_table(v)
    assert "DISCOVERY ID" in rendered
    assert "STATUS" in rendered
    assert SAMPLE_DISCOVERY_HEADER_ID in rendered
    assert "accepted" in rendered
    assert "Peter Makhnatch" in rendered


def test_format_verdict_history_table() -> None:
    """Formatting history table renders chronological entries."""
    assert (
        format_verdict_history_table(SAMPLE_DISCOVERY_HEADER_ID, [])
        == f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}."
    )

    t1 = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    history = [
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="pending",
            by="Peter Makhnatch",
            at=t1,
            note="First",
        ),
        Verdict(
            discovery_id=SAMPLE_DISCOVERY_HEADER_ID,
            status="accepted",
            by="Peter Makhnatch",
            at=t2,
            note="Second",
        ),
    ]
    rendered = format_verdict_history_table(SAMPLE_DISCOVERY_HEADER_ID, history)
    assert f"History for {SAMPLE_DISCOVERY_HEADER_ID}:" in rendered
    assert "pending" in rendered
    assert "accepted" in rendered

def test_cli_verdict_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI supports --json for machine-readable output."""
    _create_sample_discoveries_journal(tmp_path)

    code = cli.run_cli(
        [
            "verdict",
            SAMPLE_DISCOVERY_HEADER_ID,
            "needs_evidence",
            "--by",
            "Peter Makhnatch",
            "--note",
            "Needs more samples",
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert data["discovery_id"] == SAMPLE_DISCOVERY_HEADER_ID
    assert data["status"] == "needs_evidence"
    assert data["by"] == "Peter Makhnatch"
    assert data["note"] == "Needs more samples"


def test_cli_verdict_history_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI history subcommand handles history inspection."""
    _create_sample_discoveries_journal(tmp_path)

    # With no discovery ID provided
    code_missing = cli.run_cli(["verdict", "history"], workspace=tmp_path)
    assert code_missing == 2
    _, err = capsys.readouterr()
    assert "discovery_id is required" in err

    # With discovery ID provided
    code = cli.run_cli(["verdict", "history", SAMPLE_DISCOVERY_HEADER_ID], workspace=tmp_path)
    assert code == 0


def test_cli_verdict_missing_status_or_by(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI reports errors when required arguments are missing."""
    _create_sample_discoveries_journal(tmp_path)

    # Missing status
    code1 = cli.run_cli(["verdict", SAMPLE_DISCOVERY_HEADER_ID], workspace=tmp_path)
    assert code1 == 2
    _, err1 = capsys.readouterr()
    assert "status is required" in err1

    # Missing --by
    code2 = cli.run_cli(["verdict", SAMPLE_DISCOVERY_HEADER_ID, "accepted"], workspace=tmp_path)
    assert code2 == 2
    _, err2 = capsys.readouterr()
    assert "--by <who> is required" in err2


def test_cli_verdict_empty_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Bare verdict invocation displays help."""
    code = cli.run_cli(["verdict"], workspace=tmp_path)
    assert code == 0
    out, _ = capsys.readouterr()
    assert "usage:" in out


def test_unreachable_catalog_fails_distinctly(capsys: pytest.CaptureFixture[str]) -> None:
    """An unreachable catalog produces a distinguishable failure with a reason, not empty."""
    unreachable_url = "postgresql://127.0.0.1:59999/evallab_unreachable"

    # 1. list_current_verdicts_from_catalog fails with non-zero exit and informative stderr
    with pytest.raises(SystemExit) as exc_list:
        list_current_verdicts_from_catalog(database_url=unreachable_url)
    assert exc_list.value.code == 2
    out_list, err_list = capsys.readouterr()
    assert "No verdicts recorded." not in out_list
    assert "No verdicts recorded." not in err_list
    assert "catalog read failed [unavailable]" in err_list
    assert "127.0.0.1:59999" in err_list
    assert "v_current_verdicts" in err_list

    # 2. get_verdict_history_from_catalog fails with non-zero exit and informative stderr
    with pytest.raises(SystemExit) as exc_hist:
        get_verdict_history_from_catalog(
            SAMPLE_DISCOVERY_HEADER_ID, database_url=unreachable_url
        )
    assert exc_hist.value.code == 2
    out_hist, err_hist = capsys.readouterr()
    assert f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}." not in out_hist
    assert f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}." not in err_hist
    assert "catalog read failed [unavailable]" in err_hist
    assert "127.0.0.1:59999" in err_hist
    assert "v_verdicts_history" in err_hist


def test_genuinely_empty_table_prints_friendly_message() -> None:
    """A genuinely empty table still prints the friendly empty message and exits zero."""
    with duckdb.connect(":memory:") as con:
        execute_verdicts_views(con)

        # Current view on empty table
        current = list_current_verdicts_from_duckdb(con)
        assert current == []
        assert format_verdicts_table(current) == "No verdicts recorded."

        # History view on empty table
        history = get_verdict_history_from_duckdb(con, SAMPLE_DISCOVERY_HEADER_ID)
        assert history == []
        assert (
            format_verdict_history_table(SAMPLE_DISCOVERY_HEADER_ID, history)
            == f"No verdict history for {SAMPLE_DISCOVERY_HEADER_ID}."
        )


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason=f"requires live PostgreSQL {database_url_from_environment()}",
)
def test_postgres_catalog_roundtrip_if_available(tmp_path: Path) -> None:
    """Full PostgreSQL catalog roundtrip when DATABASE_URL is provided."""
    db_url = os.environ["DATABASE_URL"]
    _create_sample_discoveries_journal(tmp_path)

    now = datetime.now(UTC)
    verdict = record_verdict(
        SAMPLE_DISCOVERY_HEADER_ID,
        "accepted",
        by="Peter Makhnatch",
        note="Catalog roundtrip test",
        at=now,
        repo_root=tmp_path,
        database_url=db_url,
    )
    assert verdict.status == "accepted"

    current = list_current_verdicts_from_catalog(database_url=db_url)
    matching = [v for v in current if v.discovery_id == SAMPLE_DISCOVERY_HEADER_ID]
    assert len(matching) >= 1
    assert matching[0].status == "accepted"
    assert matching[0].by == "Peter Makhnatch"
    assert matching[0].note == "Catalog roundtrip test"

    history = get_verdict_history_from_catalog(SAMPLE_DISCOVERY_HEADER_ID, database_url=db_url)
    assert len(history) >= 1
    assert history[-1].status == "accepted"
    assert history[-1].note == "Catalog roundtrip test"
