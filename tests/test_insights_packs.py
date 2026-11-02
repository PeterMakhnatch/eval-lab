"""Pass packs preserve provenance, budgets, and independent re-derivation."""

from __future__ import annotations

import hashlib
import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    InsightRecord,
    InsightStatus,
    Lens,
    Recipe,
    SignalRecord,
    SourceRecord,
    SourceSection,
)
from evallab.insights.packs import OUTPUT_FILES, PassInputs, build_pass_dir
from evallab.insights.store import InsightStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ledger(tmp_path: Path) -> InsightStore:
    source_file = tmp_path / "report.md"
    text = "# Result\nThe screen caught 2 of 3 test cases.\n"
    source_file.write_text(text, encoding="utf-8")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "hidden-original.json").write_text('{"caught": 2}', encoding="utf-8")
    store = InsightStore(tmp_path / "ledger.sqlite")
    source = SourceRecord(
        source_id="F-test", path=str(source_file), family="test", kind="report",
        sha256="source-digest", size=len(text), mtime=NOW, folder=str(receipts),
    )
    store.upsert_source(source)
    store.replace_sections("F-test", [SourceSection(
        section_id="X-test", source_id="F-test", heading="Result", ordinal=0,
        start_line=1, end_line=2, sha256="section-digest",
    )])
    claim = ExtractedClaim(
        local_id="one", experiment="S1", statement="The screen caught 2 of 3 test cases.",
        kind="measurement", k=2, n=3, value=2 / 3, section_id="X-test",
        quote="The screen caught 2 of 3 test cases.",
        recipe=Recipe(op="value", file="hidden-original.json", pointer="/caught"),
    )
    store.insert_claims([ClaimRecord(
        claim_id="C-test", source_id="F-test", section_sha256="section-digest",
        fingerprint="FP-test", pass_id="P-old", extracted_by="test", created_at=NOW,
        claim=claim, check=CheckResult(status="pass"),
    )])
    return store


@pytest.mark.parametrize("lens", list(OUTPUT_FILES))
def test_builds_complete_pass_directory(tmp_path: Path, lens: Lens) -> None:
    with _ledger(tmp_path) as store:
        inputs = PassInputs(
            lens=lens, section_ids=("X-test",) if lens == "extractor" else (),
            claim_ids=("C-test",),
        )
        result = build_pass_dir(
            store, inputs, pass_id="P-test", state_root=tmp_path / "state",
            db_path=store.path, max_pack_chars=20000, python_executable="/usr/bin/python3",
        )
    assert result.output_file == result.path / "out" / OUTPUT_FILES[lens]
    assert result.output_file.parent.is_dir()
    assert not result.output_file.exists()
    for name in ("brief.md", "pack.md", "pack.json", "schema.json", "q", "prompt.md"):
        assert (result.path / name).is_file()
    manifest = json.loads((result.path / "pack.json").read_text())
    assert manifest["lens"] == lens
    assert manifest["claim_ids"] == ["C-test"]
    digest = hashlib.sha256(
        ((result.path / "pack.json").read_text() + (result.path / "brief.md").read_text())
        .encode()
    ).hexdigest()
    assert result.inputs_digest == digest
    schema = json.loads((result.path / "schema.json").read_text())
    expected = {
        "extractor": "ClaimExtraction", "referee": "RefereeBatch",
        "synthesizer": "BoardUpdate", "rederiver": "RederiveBatch",
    }.get(lens, "InsightBatch")
    assert schema["title"] == expected
    assert f"out/{OUTPUT_FILES[lens]}" in result.prompt_file.read_text()


def test_pack_budget_drops_whole_groups_with_retrieval_note(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        result = build_pass_dir(
            store, PassInputs(lens="extractor", section_ids=("X-test",)), pass_id="P-small",
            state_root=tmp_path, db_path=store.path, max_pack_chars=220,
            python_executable="python3",
        )
    pack = (result.path / "pack.md").read_text()
    assert len(pack) <= 220
    assert "1 more omitted; query with ./q sql" in pack
    assert "The screen caught" not in pack
    assert "X-test" in (result.path / "pack.json").read_text()


def test_rederiver_never_gets_original_recipe(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        # Receipt names are allowed. A recipe's pointer/filter/aggregation must not leak.
        record = store.get_claim("C-test")
        assert record is not None
        claim = record.claim.model_copy(update={
            "recipe": Recipe(op="value", file="unlisted-secret.json", pointer="/secret"),
        })
        record = record.model_copy(update={"claim": claim})
        store.update_claim(record)
        result = build_pass_dir(
            store, PassInputs(lens="rederiver", claim_ids=("C-test",)), pass_id="P-blind",
            state_root=tmp_path, db_path=store.path, max_pack_chars=10000,
            python_executable="python3",
        )
    pack = (result.path / "pack.md").read_text()
    assert '"recipe"' not in pack
    assert "unlisted-secret.json" not in pack
    assert "/secret" not in pack
    assert "The screen caught 2 of 3 test cases." in pack
    assert "receipts" in pack


def test_wrapper_quotes_every_path_and_forwards_arguments(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        python = "/some path/it's python"
        result = build_pass_dir(
            store, PassInputs(lens="skeptic"), pass_id="P-quotes",
            state_root=tmp_path / "state's space", db_path=store.path,
            max_pack_chars=10000, python_executable=python,
        )
    lines = (result.path / "q").read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    assert shlex.split(lines[1]) == [
        "exec", python, "-m", "evallab.insights.agent_tool", "--db", str(store.path.resolve()),
        "--pass-dir", str(result.path), "$@",
    ]
    assert (result.path / "q").stat().st_mode & 0o100


def test_pack_is_deterministic_and_does_not_overwrite_pass(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        kwargs = {
            "state_root": tmp_path, "db_path": store.path, "max_pack_chars": 20000,
            "python_executable": "python3",
        }
        first = build_pass_dir(store, PassInputs(lens="skeptic", claim_ids=("C-test",)),
                               pass_id="P-one", **kwargs)
        second = build_pass_dir(store, PassInputs(lens="skeptic", claim_ids=("C-test",)),
                                pass_id="P-two", **kwargs)
        assert first.inputs_digest == second.inputs_digest
        assert (first.path / "pack.md").read_text() == (second.path / "pack.md").read_text()
        with pytest.raises(FileExistsError):
            build_pass_dir(store, PassInputs(lens="skeptic"), pass_id="P-one", **kwargs)


def _insight(insight_id: str, status: InsightStatus, *, day: int = 0) -> InsightRecord:
    return InsightRecord(
        insight_id=insight_id, lens="connector", pass_id="P-cards",
        created_at=NOW + timedelta(days=day), status=status, check=CheckResult(status="pass"),
        card=InsightCard(
            local_id=insight_id, kind="gap", title="A concrete evidence gap",
            statement="The existing evidence leaves a distinct comparison unresolved.",
            evidence=[{"claim_id": "C-test"}], observations=["A held-out comparison is missing."],
            interpretation="The conclusion may not transfer to the held-out condition.",
            why_not_obvious="The neighboring experiments use different evaluation conditions.",
            alternatives=["The condition may not affect the outcome."], confidence="low",
        ),
    )


def test_recent_cards_exclude_dead_statuses_and_are_newest_first(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        store.insert_insights([
            _insight("I-old", "checked"),
            _insight("I-new", "needs_revision", day=1),
            _insight("I-rejected", "rejected", day=2),
            _insight("I-failed", "check_failed", day=3),
            _insight("I-duplicate", "duplicate", day=4),
            _insight("I-superseded", "superseded", day=5),
        ])
        result = build_pass_dir(
            store, PassInputs(lens="skeptic"), pass_id="P-recent", state_root=tmp_path,
            db_path=store.path, max_pack_chars=20000, python_executable="python3",
        )
    pack = (result.path / "pack.md").read_text()
    assert pack.index("I-new") < pack.index("I-old")
    for omitted in ("I-rejected", "I-failed", "I-duplicate", "I-superseded"):
        assert omitted not in pack


@pytest.mark.parametrize("lens", ["theorist", "ideator", "anomaly"])
def test_priority_evidence_precedes_bulk_claims(tmp_path: Path, lens: Lens) -> None:
    with _ledger(tmp_path) as store:
        store.upsert_signals([
            SignalRecord(
                signal_id="S-selected", kind="extreme_small_n", score=1, title="Selected lead",
                claim_ids=["C-test"], created_at=NOW,
            ),
            SignalRecord(
                signal_id="S-other", kind="extreme_small_n", score=9, title="Unselected lead",
                claim_ids=["C-test"], created_at=NOW,
            ),
        ])
        board = _insight("I-board", "refereed").model_copy(update={"rank": 1})
        store.insert_insights([board])
        result = build_pass_dir(
            store, PassInputs(lens=lens, claim_ids=("C-test",), signal_ids=("S-selected",)),
            pass_id="P-priority", state_root=tmp_path, db_path=store.path,
            max_pack_chars=20000, python_executable="python3",
        )
    pack = (result.path / "pack.md").read_text()
    claim_position = pack.index("## Input claim")
    assert pack.index("S-selected") < claim_position
    if lens == "anomaly":
        assert claim_position < pack.index("S-other")
    else:
        assert pack.index("## Board insight") < claim_position
        assert pack.index("S-other") < claim_position
