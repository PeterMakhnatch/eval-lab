from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.insights.board import plain_count, render_board
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    EvidenceRef,
    ExtractedClaim,
    HumanReview,
    InsightCard,
    InsightRecord,
    PassRecord,
    PredictionRecord,
    PredictionSpec,
    RefereeVerdict,
)
from evallab.insights.store import InsightStore

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


def _insight(index: int) -> InsightRecord:
    identity = f"I-{index}"
    return InsightRecord(
        insight_id=identity, lens="connector", pass_id="P-card", model_served="served-model",
        created_at=NOW, check=CheckResult(status="pass" if index < 21 else "fail"),
        status="checked" if index < 21 else "check_failed", rank=21 - index,
        card=InsightCard(
            local_id=str(index), kind="connection", title=f"Card {index} <script>alert(1)</script>",
            statement="A shared pattern links the two experiments & deserves a closer look.",
            evidence=[EvidenceRef(claim_id="C-evidence")], observations=["The receipts agree."],
            interpretation="This may reflect a shared mechanism.",
            why_not_obvious="The reports use different names for the same component.",
            alternatives=["The matching results could be chance."], confidence="medium",
        ),
        referee=RefereeVerdict(
            insight_id=identity, supported="yes", novel=True, falsifiable=True, actionable=True,
            importance=4, non_obviousness=3, verdict="accept", notes="Worth checking carefully.",
        ) if index < 21 else None,
    )


def test_board_ranks_escapes_and_shows_plain_counts(tmp_path: Path) -> None:
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_claims([ClaimRecord(
            claim_id="C-evidence", source_id="F-source", section_sha256="digest",
            fingerprint="fp", pass_id="P-extract", extracted_by="extractor", created_at=NOW,
            check=CheckResult(status="pass"), claim=ExtractedClaim(
                local_id="one", experiment="experiment", statement="Receipts agree on five cases.",
                kind="measurement", quote="Receipts agree on five cases.", section_id="X-source",
            ),
        )])
        store.insert_insights(_insight(index) for index in range(28))
        store.add_review(HumanReview(
            review_id="R-one", insight_id="I-20", status="accepted", by="Peter", at=NOW,
        ))
        store.insert_predictions([PredictionRecord(
            prediction_id="I-20.p1", insight_id="I-20", status="hit", created_at=NOW,
            spec=PredictionSpec(
                statement="The pattern will recur in the next run.", direction="at_least", low=1,
                resolves_when="The next frozen experiment report is available.",
            ),
        )])
        store.log_event("tick_finished", {}, at=NOW)
        store.save_pass(PassRecord(
            pass_id="P-failure", lens="skeptic", route="requested", trigger="tick",
            status="failed", pass_dir="/unused", created_at=NOW - timedelta(hours=30),
            started_at=NOW - timedelta(minutes=4), finished_at=NOW,
            served={"actual-model": 1},
        ))
        paths = render_board(store, tmp_path / "board", now=NOW, top_n=2)
    assert [path.name for path in paths] == ["board.md", "board.html", "status.json"]
    markdown = paths[0].read_text("utf-8")
    document = paths[1].read_text("utf-8")
    status = json.loads(paths[2].read_text("utf-8"))
    assert markdown.index("### 1. Card 20") < markdown.index("### 2. Card 19")
    assert "21 of 28 (75%)" in markdown
    assert "C-evidence: Receipts agree on five cases." in markdown
    assert "Human review: accepted" in markdown
    assert "Referee: accept; importance 4 of 5; non-obviousness 3 of 5" in markdown
    assert " ET" in markdown
    assert "actual-model; 4.0 minutes" in markdown
    assert "<script>" not in document
    assert "&lt;script&gt;" in document and "&amp;" in document
    assert "src=" not in document and "stylesheet" not in document
    assert document.count("<table>") == 3
    assert document.count("<article class='insight'>") == 2
    assert "<h1>Insight board</h1>" in document
    assert "<pre>" not in document
    assert status["top_insight_ids"] == ["I-20", "I-19"]
    assert status["failures_last_24h"] == 1
    assert status["prediction_scoreboard"]["connector"]["hit"] == 1
    assert status["lens_scoreboard"][0]["checks_passed"] == 21


def test_empty_board_does_not_claim_a_success_rate(tmp_path: Path) -> None:
    with InsightStore(tmp_path / "empty.sqlite") as store:
        paths = render_board(store, tmp_path / "board", now=NOW, top_n=5)
    text = paths[0].read_text("utf-8")
    assert "No checked insights yet." in text
    assert "not recorded" in text
    assert plain_count(0, 0) == "0 of 0 (not yet measured)"
