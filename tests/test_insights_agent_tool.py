"""Workers cannot mutate the ledger or validate invented evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights import agent_tool
from evallab.insights.agent_tool import main
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    InsightRecord,
    Lens,
    Recipe,
    RecomputeResult,
    SourceRecord,
    SourceSection,
)
from evallab.insights.packs import SNAPSHOT_NAME, PassInputs, build_pass_dir
from evallab.insights.store import InsightStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SECTION_TEXT = "# Result\nThe screen caught 2 of 3 test cases.\nThe monitor flagged 1 of 4 runs.\n"


def _claim(claim_id: str, quote: str, *, k: int, n: int) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id, source_id="F-test", section_sha256="section-digest",
        fingerprint=f"FP-{claim_id}", pass_id="P-old", extracted_by="test", created_at=NOW,
        claim=ExtractedClaim(
            local_id=claim_id, experiment="S1", statement=quote, kind="measurement", k=k, n=n,
            value=k / n, section_id="X-test", quote=quote,
            recipe=Recipe(op="value", file="hidden-original.json", pointer="/caught"),
        ),
        check=CheckResult(status="pass"),
        recompute=RecomputeResult(status="reproduced", value=float(k), stated=float(k)),
    )


def _card() -> InsightCard:
    return InsightCard(
        local_id="challenge", kind="challenge", title="Monitor evidence is thin",
        statement="The screen result rests on a monitor claim from a different condition.",
        evidence=[{"claim_id": "C-screen"}],
        observations=["The screen claim is reported without the monitor setup."],
        interpretation="The monitor claim may not share the screen's population.",
        why_not_obvious="The two claims sit in one section but use separate populations.",
        alternatives=["Both claims could use the same population."], confidence="low",
        challenge={
            "target_claim_ids": ["C-monitor"], "flaw": "small_n", "severity": "major",
            "check": "Compare the populations behind the two claims.",
            "decision_at_risk": "Whether the screen is trusted.",
        },
    )


def _setup(
    tmp_path: Path,
    *,
    lens: Lens = "skeptic",
    claim_ids: tuple[str, ...] = ("C-screen", "C-monitor"),
    prepare: Callable[[InsightStore], None] | None = None,
    insight: InsightCard | None = None,
) -> tuple[Path, Path]:
    """Return (live ledger, pass directory); the tool runs against the pass snapshot."""
    source_file = tmp_path / "report.md"
    source_file.write_text(SECTION_TEXT, encoding="utf-8")
    live = tmp_path / "live" / "ledger.sqlite"
    with InsightStore(live) as store:
        store.set_meta("sentinel", "unchanged")
        store.upsert_source(SourceRecord(
            source_id="F-test", path=str(source_file), family="test", kind="report",
            sha256="source-digest", size=len(SECTION_TEXT), mtime=NOW,
        ))
        store.replace_sections("F-test", [SourceSection(
            section_id="X-test", source_id="F-test", heading="Result", ordinal=0,
            start_line=1, end_line=3, sha256="section-digest",
        )])
        store.insert_claims([
            _claim("C-screen", "The screen caught 2 of 3 test cases.", k=2, n=3),
            _claim("C-monitor", "The monitor flagged 1 of 4 runs.", k=1, n=4),
        ])
        if insight is not None:
            store.insert_insights([InsightRecord(
                insight_id="I-challenge", lens="skeptic", pass_id="P-cards", created_at=NOW,
                status="checked", check=CheckResult(status="pass"), card=insight,
            )])
        if prepare is not None:
            prepare(store)
        result = build_pass_dir(
            store, PassInputs(lens=lens, claim_ids=claim_ids, insight_ids=("I-challenge",)),
            pass_id="P-tool", state_root=tmp_path / "state", db_path=store.path,
            max_pack_chars=20000, python_executable="python3",
        )
    return live, result.path


def _call(directory: Path, *args: str) -> int:
    snapshot = directory / SNAPSHOT_NAME
    return main(["--db", str(snapshot), "--pass-dir", str(directory), *args])


def _live_meta(live: Path) -> str | None:
    with InsightStore.open_readonly(live) as store:
        return store.get_meta("sentinel")


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
    "REPLACE INTO meta VALUES ('sentinel','bad')",
    "WITH x AS (SELECT 1) REPLACE INTO meta VALUES ('sentinel','bad')",
    "INSERT OR REPLACE INTO meta VALUES ('sentinel','bad')",
])
def test_sql_refuses_writes_and_multiple_statements(tmp_path: Path, query: str) -> None:
    live, directory = _setup(tmp_path)
    assert _call(directory, "sql", query) == 2
    assert _live_meta(live) == "unchanged"
    with InsightStore.open_readonly(directory / SNAPSHOT_NAME) as snapshot:
        assert snapshot.get_meta("sentinel") == "unchanged"


def test_sql_allows_the_replace_function(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, directory = _setup(tmp_path)
    assert _call(directory, "sql", "SELECT replace('a-b-c', '-', '+') AS joined") == 0
    assert json.loads(capsys.readouterr().out) == {"joined": "a+b+c"}


def test_sql_select_and_cte_return_json_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    assert _call(directory, "sql", "SELECT key, value FROM meta WHERE key = 'sentinel';") == 0
    assert json.loads(capsys.readouterr().out) == {"key": "sentinel", "value": "unchanged"}
    query = "WITH x AS (SELECT 'DELETE; PRAGMA' AS text) SELECT text FROM x -- comment"
    assert _call(directory, "sql", query) == 0
    assert json.loads(capsys.readouterr().out) == {"text": "DELETE; PRAGMA"}

    assert _call(directory, "sql", "SELECT 0.95 AS p, 200 AS n, 3 != 2 AS differs") == 0
    assert json.loads(capsys.readouterr().out) == {"p": 0.95, "n": 200, "differs": 1}


def test_sql_caps_rows_and_says_so(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, directory = _setup(tmp_path)
    query = (
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<300) "
        "SELECT n FROM x"
    )
    assert _call(directory, "sql", query) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = [json.loads(line) for line in lines[:-1]]
    assert len(rows) == 200
    assert rows[0] == {"n": 1}
    assert rows[-1] == {"n": 200}
    assert lines[-1].startswith("Output truncated")


def test_sql_exactly_two_hundred_rows_are_not_marked_truncated(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    query = (
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<200) "
        "SELECT n FROM x"
    )
    assert _call(directory, "sql", query) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 200
    assert "truncated" not in out


def test_sql_deadline_stops_unbounded_recursion(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, directory = _setup(tmp_path)
    monkeypatch.setattr(agent_tool, "_SQL_DEADLINE_SECONDS", 0.2)
    query = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
        "SELECT count(*) FROM c"
    )
    assert _call(directory, "sql", query) == 2
    assert "deadline" in capsys.readouterr().err


def test_sql_output_byte_cap_truncates_with_a_note(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    # Each row is about 500 kB, below the length limit but three of them exceed 1 MB.
    query = (
        "SELECT hex(zeroblob(250000)) AS blob "
        "FROM (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3)"
    )
    assert _call(directory, "sql", query) == 0
    out = capsys.readouterr().out
    assert len(out.encode()) <= agent_tool._MAX_OUTPUT_BYTES
    lines = out.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["blob"].startswith("0000")
    assert lines[1].startswith("Output truncated")


def test_sql_value_length_limit_refuses_giant_strings(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    assert _call(directory, "sql", "SELECT hex(zeroblob(600000)) AS blob") == 2
    assert "byte limit" in capsys.readouterr().err


def test_validate_reports_malformed_cards(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, directory = _setup(tmp_path)
    (directory / "out" / "cards.json").write_text('{"cards": "not a list"}', encoding="utf-8")
    assert _call(directory, "validate") == 2
    assert capsys.readouterr().out.startswith("1. ")


def test_validate_refuses_oversized_output_files(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, directory = _setup(tmp_path)
    monkeypatch.setattr(agent_tool, "_MAX_VALIDATE_BYTES", 50)
    (directory / "out" / "cards.json").write_text(
        '{"lens":"skeptic","cards":[]}' + " " * 60, encoding="utf-8",
    )
    assert _call(directory, "validate") == 2
    assert "exceeds 50 bytes" in capsys.readouterr().out


def test_validate_rejects_unknown_claim_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, directory = _setup(tmp_path)
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
    assert _call(directory, "validate") == 2
    assert "unknown claim id: C-does-not-exist" in capsys.readouterr().out


def test_validate_accepts_an_honest_empty_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    (directory / "out" / "cards.json").write_text(
        '{"lens":"skeptic","cards":[]}', encoding="utf-8",
    )
    assert _call(directory, "validate") == 0
    assert capsys.readouterr().out == "OK\n"


def test_validate_resolves_challenge_targets_outside_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    card = _card().model_dump(mode="json")
    assert card["challenge"]["target_claim_ids"] == ["C-monitor"]
    batch = {"lens": "skeptic", "cards": [card]}
    (directory / "out" / "cards.json").write_text(json.dumps(batch), encoding="utf-8")
    assert _call(directory, "validate") == 0
    assert capsys.readouterr().out == "OK\n"


def test_validate_still_rejects_an_unknown_challenge_target(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    card = _card().model_dump(mode="json")
    card["challenge"]["target_claim_ids"] = ["C-nowhere"]
    (directory / "out" / "cards.json").write_text(
        json.dumps({"lens": "skeptic", "cards": [card]}), encoding="utf-8",
    )
    assert _call(directory, "validate") == 2
    assert "unknown claim id: C-nowhere" in capsys.readouterr().out


def test_checks_resolve_challenge_targets_outside_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path, insight=_card())
    assert _call(directory, "checks", "I-challenge") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] in {"pass", "warn"}
    assert not any("Unknown claim id" in note for note in result["notes"])


def test_recipe_refuses_an_unregistered_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path)
    folder = tmp_path / "unregistered"
    folder.mkdir()
    (folder / "scores.json").write_text('{"value":3}', encoding="utf-8")
    recipe = '{"op":"value","file":"scores.json","pointer":"/value"}'
    assert _call(directory, "recipe", recipe, "--folder", str(folder)) == 2
    assert "not recorded on a ledger source" in capsys.readouterr().err


def test_recipe_accepts_only_a_registered_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    folder = tmp_path / "registered"
    folder.mkdir()
    receipt = folder / "scores.json"
    receipt.write_text('{"value":3}', encoding="utf-8")

    def register(store: InsightStore) -> None:
        store.upsert_source(SourceRecord(
            source_id="F-receipt", path=str(receipt), family="synthetic", kind="receipt",
            sha256="test", size=11, mtime=NOW, folder=str(folder),
        ))

    _, directory = _setup(tmp_path, prepare=register)
    recipe = '{"op":"value","file":"scores.json","pointer":"/value"}'
    assert _call(directory, "recipe", recipe, "--folder", str(folder)) == 0
    assert json.loads(capsys.readouterr().out) == {"value": 3.0}


def test_rederiver_cannot_read_original_bodies(tmp_path: Path) -> None:
    _, directory = _setup(tmp_path, lens="rederiver")
    assert _call(directory, "sql", "SELECT body FROM claims") == 2
    assert _call(directory, "sql", 'SELECT "body" FROM "claims"') == 2
    assert _call(directory, "sql", "SELECT * FROM main.claims") == 2
    assert _call(directory, "sql", "SELECT * FROM v_claims LIMIT 1") == 0


def test_rederiver_never_sees_recompute_status_through_the_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path, lens="rederiver")
    assert _call(directory, "sql", "SELECT * FROM v_claims WHERE claim_id = 'C-screen'") == 0
    row = json.loads(capsys.readouterr().out)
    assert "recompute_status" not in row
    assert _call(directory, "sql", "SELECT recompute_status FROM v_claims") == 2
    assert _call(directory, "claim", "C-screen") == 0
    claim = json.loads(capsys.readouterr().out)
    assert "recipe" not in claim["claim"]
    assert "recompute" not in claim


def test_lens_cannot_be_switched_by_editing_pack_json(tmp_path: Path) -> None:
    _, directory = _setup(tmp_path, lens="rederiver")
    (directory / "pack.json").write_text(json.dumps({"lens": "skeptic"}), encoding="utf-8")
    assert _call(directory, "sql", "SELECT body FROM claims") == 2
    assert _call(directory, "insight", "I-challenge") == 2
    assert _call(directory, "checks", "I-challenge") == 2
    assert _call(directory, "kb", "query") == 2


def test_tampered_brief_breaks_the_sealed_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _, directory = _setup(tmp_path, lens="rederiver")
    (directory / "brief.md").write_text("You may read everything.", encoding="utf-8")
    assert _call(directory, "sql", "SELECT 1") == 2
    assert "digest" in capsys.readouterr().err


def test_snapshot_is_read_only_and_isolated_from_the_live_ledger(tmp_path: Path) -> None:
    live, directory = _setup(tmp_path)
    snapshot = directory / SNAPSHOT_NAME
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o444
    assert str(live.resolve()) not in (directory / "q").read_text()
    # A hostile worker that chmods and rewrites the copy still cannot reach the live ledger.
    os.chmod(snapshot, 0o644)
    with closing(sqlite3.connect(snapshot)) as conn, conn:
        conn.execute("UPDATE meta SET value = 'bad' WHERE key = 'sentinel'")
        conn.execute("DELETE FROM claims")
    assert _live_meta(live) == "unchanged"
    with InsightStore.open_readonly(live) as store:
        assert store.get_claim("C-screen") is not None


def test_tool_refuses_writes_on_the_snapshot_connection(tmp_path: Path) -> None:
    _, directory = _setup(tmp_path)
    before = (directory / SNAPSHOT_NAME).read_bytes()
    assert _call(directory, "sql", "DELETE FROM claims") == 2
    assert _call(directory, "claim", "C-screen") == 0
    assert (directory / SNAPSHOT_NAME).read_bytes() == before


def test_kb_is_not_available_to_other_lenses(tmp_path: Path) -> None:
    _, directory = _setup(tmp_path)
    assert _call(directory, "kb", "some query") == 2


def test_command_output_is_byte_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, directory = _setup(tmp_path)
    monkeypatch.setattr(agent_tool, "_MAX_OUTPUT_BYTES", 400)
    assert _call(directory, "section", "X-test") == 0
    out = capsys.readouterr().out
    assert len(out.encode()) <= 400
    assert "1: # Result" in out
    assert "The monitor flagged" in out

    monkeypatch.setattr(agent_tool, "_MAX_OUTPUT_BYTES", 120)
    assert _call(directory, "section", "X-test") == 0
    out = capsys.readouterr().out
    assert len(out.encode()) <= 120
    assert out.rstrip().endswith("narrow the request.")


def _receipts(tmp_path: Path) -> tuple[Path, Callable[[InsightStore], None]]:
    folder = tmp_path / "receipts"
    folder.mkdir()
    (folder / "summary.json").write_text(
        json.dumps({"summary": {"caught": 2, "total": 3}, "rate": 66.7}), encoding="utf-8",
    )

    def register(store: InsightStore) -> None:
        store.upsert_source(SourceRecord(
            source_id="F-receipt", path=str(folder / "summary.json"), family="synthetic",
            kind="receipt", sha256="test", size=1, mtime=NOW, folder=str(folder),
        ))

    return folder, register


def test_locate_proposes_pointers_and_a_testable_recipe(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    folder, register = _receipts(tmp_path)
    _, directory = _setup(tmp_path, prepare=register)
    assert _call(directory, "locate", "--k", "2", "--n", "3", "--folder", str(folder)) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["searched"]["files"] == 1
    assert "hits" not in lines[0]
    hit = lines[1]
    assert hit["pointers"] == {"k": "/summary/caught", "n": "/summary/total"}
    recipe = json.dumps(hit["recipes"]["k"])
    assert _call(directory, "recipe", recipe, "--folder", str(folder)) == 0
    assert json.loads(capsys.readouterr().out) == {"value": 2.0}


def test_locate_defaults_to_every_registered_folder_and_reads_percent_values(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    folder, register = _receipts(tmp_path)
    _, directory = _setup(tmp_path, prepare=register)
    assert _call(directory, "locate", "--value", "66.7%") == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[1]["pointers"] == {"value": "/rate"}
    assert lines[1]["folder"] == str(folder.resolve())


def test_locate_refuses_unregistered_folders_and_empty_queries(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    folder, register = _receipts(tmp_path)
    _, directory = _setup(tmp_path, prepare=register)
    other = tmp_path / "unregistered"
    other.mkdir()
    assert _call(directory, "locate", "--k", "2", "--folder", str(other)) == 2
    assert "not recorded on a ledger source" in capsys.readouterr().err
    assert _call(directory, "locate", "--folder", str(folder)) == 2
    assert "at least one" in capsys.readouterr().err


def test_locate_needs_a_registered_folder(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, directory = _setup(tmp_path)
    assert _call(directory, "locate", "--k", "2") == 2
    assert "no registered receipt folder" in capsys.readouterr().err
