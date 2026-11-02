"""Ledger behaviour the engine depends on: migrations, revival of stale claims, recheck flags."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    EvidenceRef,
    ExtractedClaim,
    InsightCard,
    InsightRecord,
)
from evallab.insights.store import DB_NAME, SCHEMA_USER_VERSION, InsightStore

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _claim(claim_id: str = "C-one", *, experiment: str = "B3", metric: str = "flip_rate",
           n: int = 50, fingerprint: str = "FP-old") -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id, source_id="S-1", section_sha256="sha-old", fingerprint=fingerprint,
        pass_id="P-1", extracted_by="anthropic/claude-sonnet-5-5", created_at=NOW,
        claim=ExtractedClaim(
            local_id="c1", experiment=experiment, statement=f"{experiment} flips {n} packages",
            kind="measurement", metric=metric, n=n, quote="quoted text here", section_id="sec-1",
        ),
        check=CheckResult(status="pass"),
    )


def _card_record(claim_id: str, status: str = "checked") -> InsightRecord:
    return InsightRecord(
        insight_id="I-one", lens="skeptic", pass_id="P-1", created_at=NOW, status=status,  # type: ignore[arg-type]
        check=CheckResult(status="pass"),
        card=InsightCard(
            local_id="k1", kind="gap", title="A gap in coverage",
            statement="Nobody measured this cell of the grid yet, so it is open.",
            evidence=[EvidenceRef(claim_id=claim_id)], observations=["One claim exists."],
            interpretation="The grid is incomplete here.",
            why_not_obvious="The headline table hides the empty cell.",
            alternatives=["It was measured elsewhere."], confidence="low",
        ),
    )


def test_reviving_a_stale_claim_updates_the_indexed_columns(tmp_path: Path) -> None:
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_claim()])
        store.mark_section_claims_stale(["sec-1"])
        assert store.list_claims(experiment="B3") == []
        revived = _claim(experiment="B4", metric="defense_rate", n=100, fingerprint="FP-new")
        assert store.insert_claims([revived]) == 1
        assert [c.claim_id for c in store.list_claims(experiment="B4")] == ["C-one"]
        assert store.list_claims(experiment="B3") == []
        row = store.query(
            "SELECT experiment, metric, fingerprint, kind FROM claims WHERE claim_id = 'C-one'"
        )[0]
        assert (row["experiment"], row["metric"], row["fingerprint"]) == ("B4", "defense_rate", "FP-new")
        view = store.query("SELECT experiment, metric, n FROM v_claims WHERE claim_id = 'C-one'")[0]
        assert (view["experiment"], view["metric"], view["n"]) == ("B4", "defense_rate", 100)


def test_cards_citing_stale_claims_are_flagged_for_recheck(tmp_path: Path) -> None:
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_claim()])
        store.insert_insights([_card_record("C-one")])
        assert store.insight_recheck_count() == 0
        store.mark_section_claims_stale(["sec-1"])
        assert [r.insight_id for r in store.insights_needing_recheck()] == ["I-one"]
        store.clear_recheck(["I-one"])
        assert store.insight_recheck_count() == 0


def test_graded_at_is_stamped_once_and_filterable(tmp_path: Path) -> None:
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_claim()])
        record = _card_record("C-one")
        store.insert_insights([record])
        assert store.list_insights(graded_since=datetime(2026, 1, 1, tzinfo=UTC)) == []
        graded = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
        store.save_insight(record.model_copy(update={"status": "refereed"}), graded_at=graded)
        store.save_insight(record.model_copy(update={"status": "refereed", "rank": 1}),
                           graded_at=datetime(2026, 11, 3, tzinfo=UTC))
        found = store.list_insights(statuses=["refereed"], graded_since=datetime(2026, 11, 2, 9, 30, tzinfo=UTC))
        assert [r.insight_id for r in found] == ["I-one"]
        assert store.list_insights(graded_since=datetime(2026, 11, 2, 10, 30, tzinfo=UTC)) == []


def test_migrate_upgrades_an_older_ledger_in_place_and_refuses_a_newer_one(tmp_path: Path) -> None:
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_claim()])
        store.insert_insights([_card_record("C-one", status="refereed")])
    raw = sqlite3.connect(tmp_path / DB_NAME)
    raw.executescript(
        "DROP VIEW v_insights; DROP TABLE lens_coverage; DROP TABLE pass_inputs;"
        "DROP TABLE referee_calibration;"
        "ALTER TABLE insights DROP COLUMN graded_at; ALTER TABLE insights DROP COLUMN needs_recheck;"
        "ALTER TABLE insights DROP COLUMN hidden;"
        "DROP VIEW v_claims; CREATE VIEW v_claims AS SELECT claim_id FROM claims;"
        "PRAGMA user_version=1;"
    )
    raw.commit()
    raw.close()
    with InsightStore.open(tmp_path) as store:
        assert store.query("PRAGMA user_version")[0]["user_version"] == SCHEMA_USER_VERSION
        assert store.coverage_count("skeptic") == 0
        assert store.pass_inputs("none") is None
        assert store.query("SELECT experiment FROM v_claims")  # the full view is back
        assert store.query("SELECT graded_at FROM insights")[0]["graded_at"] is not None
    newer = sqlite3.connect(tmp_path / DB_NAME)
    newer.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION + 1}")
    newer.commit()
    newer.close()
    with pytest.raises(RuntimeError, match="newer than supported"):
        InsightStore.open(tmp_path)


def test_coverage_digest_changes_only_for_the_experiment_that_changed(tmp_path: Path) -> None:
    with InsightStore.open(tmp_path) as store:
        store.insert_claims([_claim("C-a", experiment="E1"), _claim("C-b", experiment="E2")])
        before = {c["exp_id"]: c["claims_digest"] for c in store.experiment_review_candidates()}
        store.insert_claims([_claim("C-c", experiment="E2")])
        after = {c["exp_id"]: c["claims_digest"] for c in store.experiment_review_candidates()}
        assert before["E1"] == after["E1"] and before["E2"] != after["E2"]
        store.record_coverage("skeptic", "P-1", before, reviewed_at=NOW)
        assert store.coverage_digests("skeptic") == before
