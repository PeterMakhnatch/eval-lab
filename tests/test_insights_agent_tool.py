"""Workers cannot mutate the ledger or validate invented evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights.agent_tool import main
from evallab.insights.models import SourceRecord
from evallab.insights.store import InsightStore


def _setup(tmp_path: Path, *, lens: str = "skeptic") -> tuple[Path, Path]:
    db = tmp_path / "ledger.sqlite"
    with InsightStore(db) as store:
        store.set_meta("sentinel", "unchanged")
    directory = tmp_path / "pass"
    (directory / "out").mkdir(parents=True)
    (directory / "pack.json").write_text(json.dumps({"lens": lens}), encoding="utf-8")
    return db, directory


def _call(db: Path, directory: Path, *args: str) -> int:
    return main(["--db", str(db), "--pass-dir", str(directory), *args])


@pytest.mark.parametrize("query", [
    "INSERT INTO meta VALUES ('bad','bad')",
    "UPDATE meta SET value='bad'",
    "DELETE FROM meta",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA user_version",
    "SELECT 1; DELETE FROM meta",
    "SELECT 1; SELECT 2",
    "WITH x AS (SELECT 1) DELETE FROM meta",
    "SELECT load_extension('anything')",
])
def test_sql_refuses_writes_and_multiple_statements(tmp_path: Path, query: str) -> None:
    db, directory = _setup(tmp_path)
    assert _call(db, directory, "sql", query) == 2
    with InsightStore.open_readonly(db) as store:
        assert store.get_meta("sentinel") == "unchanged"


def test_sql_select_and_cte_return_json_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db, directory = _setup(tmp_path)
    assert _call(db, directory, "sql", "SELECT key, value FROM meta;") == 0
    assert json.loads(capsys.readouterr().out) == {"key": "sentinel", "value": "unchanged"}
    query = "WITH x AS (SELECT 'DELETE; PRAGMA' AS text) SELECT text FROM x -- comment"
    assert _call(db, directory, "sql", query) == 0
    assert json.loads(capsys.readouterr().out) == {"text": "DELETE; PRAGMA"}

    assert _call(db, directory, "sql", "SELECT 0.95 AS p, 200 AS n, 3 != 2 AS differs") == 0
    assert json.loads(capsys.readouterr().out) == {"p": 0.95, "n": 200, "differs": 1}


def test_sql_limits_recursive_cte_to_two_hundred_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    db, directory = _setup(tmp_path)
    query = (
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<300) "
        "SELECT n FROM x"
    )
    assert _call(db, directory, "sql", query) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 200
    assert rows[0] == {"n": 1}
    assert rows[-1] == {"n": 200}


def test_validate_reports_malformed_cards(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db, directory = _setup(tmp_path)
    (directory / "out" / "cards.json").write_text('{"cards": "not a list"}', encoding="utf-8")
    assert _call(db, directory, "validate") == 2
    assert capsys.readouterr().out.startswith("1. ")


def test_validate_rejects_unknown_claim_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db, directory = _setup(tmp_path)
    batch = {
        "lens": "skeptic",
        "cards": [{
            "local_id": "gap", "kind": "gap", "title": "A missing evidence source",
            "statement": "The stated conclusion has no corresponding ledger evidence.",
            "evidence": [{"claim_id": "C-does-not-exist"}],
            "observations": ["The cited claim is unavailable."],
            "interpretation": "The conclusion should remain provisional.",
            "why_not_obvious": "The supporting receipt was not indexed with the report.",
            "alternatives": ["The evidence may exist in an unscanned source."],
            "confidence": "low",
        }],
    }
    (directory / "out" / "cards.json").write_text(json.dumps(batch), encoding="utf-8")
    assert _call(db, directory, "validate") == 2
    assert "unknown claim id: C-does-not-exist" in capsys.readouterr().out


def test_validate_accepts_an_honest_empty_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    db, directory = _setup(tmp_path)
    (directory / "out" / "cards.json").write_text(
        '{"lens":"skeptic","cards":[]}', encoding="utf-8",
    )
    assert _call(db, directory, "validate") == 0
    assert capsys.readouterr().out == "OK\n"


def test_recipe_refuses_an_unregistered_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    db, directory = _setup(tmp_path)
    folder = tmp_path / "unregistered"
    folder.mkdir()
    (folder / "scores.json").write_text('{"value":3}', encoding="utf-8")
    recipe = '{"op":"value","file":"scores.json","pointer":"/value"}'
    assert _call(db, directory, "recipe", recipe, "--folder", str(folder)) == 2
    assert "not recorded on a ledger source" in capsys.readouterr().err


def test_recipe_accepts_only_a_registered_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    db, directory = _setup(tmp_path)
    folder = tmp_path / "registered"
    folder.mkdir()
    receipt = folder / "scores.json"
    receipt.write_text('{"value":3}', encoding="utf-8")
    with InsightStore(db) as store:
        store.upsert_source(SourceRecord(
            source_id="F-test", path=str(receipt), family="synthetic", kind="receipt",
            sha256="test", size=11, mtime=datetime(2026, 1, 1, tzinfo=UTC), folder=str(folder),
        ))
    recipe = '{"op":"value","file":"scores.json","pointer":"/value"}'
    assert _call(db, directory, "recipe", recipe, "--folder", str(folder)) == 0
    assert json.loads(capsys.readouterr().out) == {"value": 3.0}


def test_rederiver_cannot_read_original_bodies(tmp_path: Path) -> None:
    db, directory = _setup(tmp_path, lens="rederiver")
    assert _call(db, directory, "sql", "SELECT body FROM claims") == 2
    assert _call(db, directory, "sql", 'SELECT "body" FROM "claims"') == 2
    assert _call(db, directory, "sql", "SELECT * FROM main.claims") == 2
    assert _call(db, directory, "sql", "SELECT * FROM v_claims LIMIT 1") == 0


def test_kb_is_not_available_to_other_lenses(tmp_path: Path) -> None:
    db, directory = _setup(tmp_path)
    assert _call(db, directory, "kb", "some query") == 2
