"""Pass packs preserve provenance, budgets, and independent re-derivation."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights import packs as packs_module
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    InsightRecord,
    InsightStatus,
    Lens,
    Recipe,
    RecomputeResult,
    SignalRecord,
    SourceRecord,
    SourceSection,
)
from evallab.insights.packs import (
    OUTPUT_FILES,
    SNAPSHOT_NAME,
    PassInputs,
    build_pass_dir,
    pass_inputs_digest,
    receipt_listing,
)
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
    for name in ("brief.md", "pack.md", "pack.json", "schema.json", "q", "prompt.md",
                 SNAPSHOT_NAME):
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
    assert lines[1] == "unset EVALLAB_INSIGHTS_STATE"
    assert shlex.split(lines[2]) == [
        "exec", python, "-m", "evallab.insights.agent_tool", "--db",
        str(result.path / SNAPSHOT_NAME), "--pass-dir", str(result.path), "$@",
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


def _build(store: InsightStore, tmp_path: Path, lens: Lens, **inputs: object) -> Path:
    return build_pass_dir(
        store, PassInputs(lens=lens, **inputs),  # type: ignore[arg-type]
        pass_id=f"P-{lens}", state_root=tmp_path / "state", db_path=store.path,
        max_pack_chars=40000, python_executable="python3",
    ).path


def test_snapshot_is_a_read_only_copy_and_the_live_path_stays_out_of_the_pass(
    tmp_path: Path,
) -> None:
    with _ledger(tmp_path) as store:
        store.set_meta("sentinel", "unchanged")
        live = str(store.path.resolve())
        path = _build(store, tmp_path, "skeptic", claim_ids=("C-test",))
        snapshot = path / SNAPSHOT_NAME
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o444
        with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as copy:
            assert copy.execute("SELECT value FROM meta WHERE key = 'sentinel'").fetchone() == (
                "unchanged",
            )
            with pytest.raises(sqlite3.OperationalError):
                copy.execute("DELETE FROM claims")
        # Even a worker that chmods the copy and writes to it cannot reach the live ledger.
        os.chmod(snapshot, 0o644)
        with closing(sqlite3.connect(snapshot)) as copy, copy:
            copy.execute("DELETE FROM claims")
            copy.execute("UPDATE meta SET value = 'bad' WHERE key = 'sentinel'")
        assert store.get_meta("sentinel") == "unchanged"
        assert store.get_claim("C-test") is not None
    for item in path.rglob("*"):
        if item.is_file() and item.name != SNAPSHOT_NAME:
            assert live not in item.read_text(errors="ignore"), item.name
    assert str(tmp_path / "ledger.sqlite") not in (path / "q").read_text()


def test_snapshot_seals_the_manifest_that_the_digest_covers(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        result = build_pass_dir(
            store, PassInputs(lens="skeptic", claim_ids=("C-test",)), pass_id="P-seal",
            state_root=tmp_path / "state", db_path=store.path, max_pack_chars=20000,
            python_executable="python3",
        )
    with closing(sqlite3.connect(f"file:{result.path / SNAPSHOT_NAME}?mode=ro", uri=True)) as copy:
        sealed = dict(copy.execute("SELECT key, value FROM meta").fetchall())
    manifest = sealed["evallab.insights.pass_manifest"]
    assert manifest == (result.path / "pack.json").read_text()
    assert sealed["evallab.insights.pass_inputs_digest"] == result.inputs_digest
    assert pass_inputs_digest(result.path) == result.inputs_digest
    (result.path / "pack.json").write_text('{"lens":"skeptic","claim_ids":[]}')
    assert pass_inputs_digest(result.path) != result.inputs_digest


def test_rederiver_snapshot_has_no_recipe_or_recompute_state(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        record = store.get_claim("C-test")
        assert record is not None
        store.update_claim(record.model_copy(update={
            "recompute": RecomputeResult(status="differs", value=1.0, stated=2.0),
        }))
        blind = _build(store, tmp_path, "rederiver", claim_ids=("C-test",))
        control = _build(store, tmp_path, "skeptic", claim_ids=("C-test",))
    with closing(sqlite3.connect(f"file:{blind / SNAPSHOT_NAME}?mode=ro", uri=True)) as copy:
        body, status = copy.execute("SELECT body, recompute_status FROM claims").fetchone()
        columns = [row[1] for row in copy.execute("PRAGMA table_info(v_claims)").fetchall()]
        assert "hidden-original.json" not in body
        assert '"recipe"' not in body
        assert '"recompute"' not in body
        assert status is None
        assert "recompute_status" not in columns
        assert "statement" in columns
    with closing(sqlite3.connect(f"file:{control / SNAPSHOT_NAME}?mode=ro", uri=True)) as copy:
        body, status = copy.execute("SELECT body, recompute_status FROM claims").fetchone()
        assert "hidden-original.json" in body
        assert status == "differs"
    # The live ledger keeps the original recipe and result.
    kept = store_claim = None
    with InsightStore.open_readonly(tmp_path / "ledger.sqlite") as live:
        store_claim = live.get_claim("C-test")
    assert store_claim is not None
    kept = store_claim.claim.recipe
    assert kept is not None
    assert store_claim.recompute is not None


def _challenge(insight_id: str, target: str) -> InsightRecord:
    card = InsightCard.model_validate({
        **_insight(insight_id, "checked").card.model_dump(mode="json"),
        "kind": "challenge", "local_id": insight_id,
        "challenge": {
            "target_claim_ids": [target], "flaw": "small_n", "severity": "major",
            "check": "Compare the populations behind both claims.",
            "decision_at_risk": "Whether the screen is trusted.",
        },
    })
    return InsightRecord(
        insight_id=insight_id, lens="skeptic", pass_id="P-cards", created_at=NOW,
        model_served="vendor/secret-author-model", status="checked",
        check=CheckResult(status="pass"), card=card,
    )


def test_referee_pack_includes_challenge_targets_and_hides_the_author(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        section_text = "# Result\nThe screen caught 2 of 3 test cases.\n"
        target_quote = "The target quote that only the challenge names."
        target = ClaimRecord(
            claim_id="C-target", source_id="F-test", section_sha256="section-digest",
            fingerprint="FP-target", pass_id="P-old", extracted_by="test", created_at=NOW,
            claim=ExtractedClaim(
                local_id="target", experiment="S1", statement=target_quote, kind="measurement",
                k=1, n=2, value=0.5, section_id="X-test", quote=target_quote,
            ),
            check=CheckResult(status="pass"),
        )
        store.insert_claims([target])
        store.insert_insights([_challenge("I-challenge", "C-target")])
        path = _build(store, tmp_path, "referee", insight_ids=("I-challenge",))
    pack = (path / "pack.md").read_text()
    assert "C-target" in pack
    assert target_quote in pack
    assert "C-test" in pack
    # Source section excerpt with line numbers, once for each cited claim.
    assert section_text.splitlines()[1] in pack
    assert "1: # Result" in pack
    assert "secret-author-model" not in pack
    assert "model_served" not in pack
    assert "I-challenge" in pack


def test_referee_pack_fails_closed_on_an_unknown_challenge_target(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        store.insert_insights([_challenge("I-ghost", "C-ghost")])
        with pytest.raises(ValueError, match="unknown cited claim: C-ghost"):
            _build(store, tmp_path, "referee", insight_ids=("I-ghost",))


def _experiment_folder(root: Path) -> None:
    (root / "cases").mkdir(parents=True)
    for index in range(120):
        (root / "cases" / f"case-{index:03d}.json").write_text("{}", encoding="utf-8")
    for name in ("rollouts.jsonl", "summary.json", "metrics_summary.json", "report.json"):
        (root / name).write_text("{}", encoding="utf-8")
    for name in ("prepare_labels.py", "score.py", "helpers.py"):
        (root / name).write_text("print('x')\n", encoding="utf-8")
    (root / "README.md").write_text("notes\n", encoding="utf-8")


def test_receipt_listing_shows_aggregates_and_code_before_per_case_files(tmp_path: Path) -> None:
    root = tmp_path / "exp"
    _experiment_folder(root)
    listing = receipt_listing(str(root))
    lines = listing.splitlines()
    for name in ("rollouts.jsonl", "summary.json", "metrics_summary.json", "report.json"):
        assert any(line.startswith(f"{name} | ") for line in lines), name
    assert any(line.startswith("prepare_labels.py | ") for line in lines)
    assert any(line.startswith("score.py | ") for line in lines)
    # Preparation and scoring code precede other code; code follows the aggregates.
    assert listing.index("rollouts.jsonl") < listing.index("prepare_labels.py")
    assert listing.index("prepare_labels.py") < listing.index("helpers.py")
    # Per-case files collapse to one line with a count, a glob and examples.
    collapsed = [line for line in lines if line.startswith("cases/case-*.json")]
    assert collapsed == [
        "cases/case-*.json | 120 files | 240 bytes | e.g. case-000.json, case-001.json"
    ]
    assert not any(line.startswith("cases/case-050") for line in lines)
    assert len(lines) < 25
    assert listing == receipt_listing(str(root))


def test_receipt_listing_collapses_large_directories_of_named_files(tmp_path: Path) -> None:
    root = tmp_path / "exp"
    (root / "runs").mkdir(parents=True)
    for index in range(40):
        (root / "runs" / f"task-{chr(97 + index % 26)}{chr(97 + index // 26)}.json").write_text(
            "{}", encoding="utf-8",
        )
    (root / "summary.json").write_text("{}", encoding="utf-8")
    listing = receipt_listing(str(root))
    assert "runs/*.json | 40 files" in listing
    assert listing.count("\n") < 12


def test_receipt_listing_states_the_bundle_layout(tmp_path: Path) -> None:
    bundle = tmp_path / "work" / ".bundle"
    for member in ("main-exp", "earlier-exp"):
        (bundle / member).mkdir(parents=True)
        (bundle / member / "summary.json").write_text("{}", encoding="utf-8")
        (bundle / member / "label.py").write_text("x = 1\n", encoding="utf-8")
    listing = receipt_listing(str(bundle))
    assert "Layout: analysis bundle" in listing
    assert "Members: earlier-exp, main-exp" in listing
    assert "main-exp/summary.json | " in listing
    assert "main-exp/label.py | " in listing
    assert "Layout:" not in receipt_listing(str(tmp_path / "work"))


def test_receipt_listing_reports_a_bounded_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "exp"
    root.mkdir()
    for index in range(10):
        (root / f"file-{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(packs_module, "SCAN_LIMIT", 4)
    assert "Folder scan stopped after 4 files" in receipt_listing(str(root))


def test_receipt_listing_skips_symlinks_and_missing_folders(tmp_path: Path) -> None:
    root = tmp_path / "exp"
    root.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    (root / "link.json").symlink_to(outside)
    assert "link.json" not in receipt_listing(str(root))
    assert receipt_listing(str(tmp_path / "missing")) == "Receipts: recorded folder unavailable."
    assert receipt_listing(None) == "Receipts: no folder recorded."


def test_extractor_pack_lists_aggregates_despite_many_case_files(tmp_path: Path) -> None:
    with _ledger(tmp_path) as store:
        folder = tmp_path / "wide"
        _experiment_folder(folder)
        source = store.get_source("F-test")
        assert source is not None
        store.upsert_source(source.model_copy(update={"folder": str(folder)}))
        path = _build(store, tmp_path, "extractor", section_ids=("X-test",))
    pack = (path / "pack.md").read_text()
    assert "summary.json | " in pack
    assert "prepare_labels.py | " in pack
    assert "cases/case-*.json | 120 files" in pack


@pytest.mark.parametrize("lens", ["skeptic", "anomaly"])
def test_skeptic_and_anomaly_packs_show_the_experiment_receipt_layout(
    tmp_path: Path, lens: Lens,
) -> None:
    with _ledger(tmp_path) as store:
        folder = tmp_path / "wide"
        _experiment_folder(folder)
        source = store.get_source("F-test")
        assert source is not None
        store.upsert_source(source.model_copy(update={"folder": str(folder)}))
        path = _build(store, tmp_path, lens, claim_ids=("C-test",))
    pack = (path / "pack.md").read_text()
    assert f"## Receipt layout\nfolder={folder}" in pack
    assert "prepare_labels.py | " in pack
    assert pack.index("## Input claim") < pack.index("## Receipt layout")
