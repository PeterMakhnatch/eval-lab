from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights.board import plain_count, render_board
from evallab.insights.models import (
    CheckResult,
    EvidenceRef,
    HumanReview,
    InsightCard,
    InsightRecord,
    PassRecord,
    PredictionRecord,
    PredictionSpec,
    ProposedExperiment,
    RefereeVerdict,
    SignalRecord,
)
from evallab.insights.store import InsightStore

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


def _insight(index: int) -> InsightRecord:
    identity = f"I-{index}"
    return InsightRecord(
        insight_id=identity, lens="connector", pass_id="P-card", model_served="served-author",
        created_at=NOW - timedelta(minutes=index), check=CheckResult(status="pass"),
        status="refereed", rank=index + 1, referee_model="independent-referee",
        card=InsightCard(
            local_id=str(index), kind="connection", title=f"Card {index}",
            statement="A shared pattern links the two experiments & deserves a closer look.",
            evidence=[EvidenceRef(claim_id="C-evidence")], observations=["The receipts agree."],
            interpretation="This may reflect a shared mechanism.",
            why_not_obvious="The reports use different names for the same component.",
            alternatives=["The matching results could be chance."], confidence="medium",
        ),
        referee=RefereeVerdict(
            insight_id=identity, supported="yes", novel=True, falsifiable=True, actionable=True,
            importance=4, non_obviousness=3, verdict="accept", notes="Worth checking carefully.",
        ),
    )


def _render(store: InsightStore, tmp_path: Path, top_n: int = 15) -> tuple[str, str, dict]:
    paths = render_board(store, tmp_path / "board", now=NOW, top_n=top_n)
    assert [path.name for path in paths] == ["board.md", "board.html", "status.json"]
    return paths[0].read_text("utf-8"), paths[1].read_text("utf-8"), json.loads(paths[2].read_text("utf-8"))


def _review(store: InsightStore, index: int, status: str) -> None:
    store.add_review(HumanReview(
        review_id=f"R-{index}", insight_id=f"I-{index}", status=status,
        by="Peter Makhnatch", note="Receipt review recorded.", at=NOW,
    ))


def test_board_separates_all_lanes_and_quarantines_reasons(tmp_path: Path) -> None:
    records = [_insight(index) for index in range(10)]
    records[1] = records[1].model_copy(update={"referee": None, "rank": None, "status": "checked"})
    records[2] = records[2].model_copy(update={
        "referee": records[2].referee.model_copy(update={"verdict": "revise"}),
        "status": "needs_revision", "check": CheckResult(status="warn", notes=["Check sample size."]),
    })
    records[3] = records[3].model_copy(update={
        "referee": records[3].referee.model_copy(update={"verdict": "reject"}), "status": "rejected",
    })
    records[4] = records[4].model_copy(update={
        "status": "check_failed", "check": CheckResult(status="fail", notes=["Unknown cited claim."]),
    })
    records[8] = records[8].model_copy(update={"duplicate_of": "I-0", "status": "duplicate"})
    # Human acceptance does not remove a failed code check from quarantine.
    records[9] = records[9].model_copy(update={
        "check": CheckResult(status="fail", notes=["Stale evidence."]), "status": "check_failed",
    })
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights(records)
        for index, status in ((5, "accepted"), (6, "rejected"), (7, "needs_evidence"), (9, "accepted")):
            _review(store, index, status)
        # Pending is a valid human-review state, not a nonexistent human_pending lane.
        _review(store, 1, "pending")
        markdown, document, status = _render(store, tmp_path)
    assert status["lane_counts"] == {
        "refereed_accepted": 1, "awaiting_referee": 1, "needs_revision": 1,
        "referee_rejected": 1, "check_failed": 2, "human_accepted": 1,
        "human_rejected": 1, "human_needs_evidence": 1, "archived": 1,
    }
    assert status["quarantine_count"] == 2
    assert status["top_insight_ids"] == ["I-0", "I-5"]
    assert "I-6" not in status["newest_accepted_ids"]
    assert status["lane_insight_ids"]["human_rejected"] == ["I-6"]
    assert "Unknown cited claim." in markdown and "Stale evidence." in document
    assert "Check sample size." in document
    assert "8 pass / 1 warn / 1 fail" not in markdown
    assert "7 pass / 1 warn / 2 fail" in markdown
    assert "unranked" in document
    assert "Human review: accepted; by Peter Makhnatch" in markdown


def test_board_links_evidence_and_escapes_every_card_surface(tmp_path: Path) -> None:
    malicious = '<script>alert("unsafe")</script>'
    record = _insight(0)
    record = record.model_copy(update={
        "card": record.card.model_copy(update={
            "title": malicious,
            "evidence": [EvidenceRef(claim_id="C-evidence", note=malicious)],
        }),
        "check": CheckResult(status="warn", notes=[malicious]),
        "model_served": malicious,
        "referee_model": malicious,
    })
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights([record])
        store.set_meta("board_headline", malicious)
        store.set_meta("board_summary", malicious)
        markdown, document, status = _render(store, tmp_path)
    assert "<script>" not in document
    assert "&lt;script&gt;" in document and "&amp;" in document
    assert "<details>" in document and "<summary>" in document
    assert "<details>" not in markdown
    assert "<a href='#item-" in document
    assert "evallab insights show C-evidence" in document
    for decision in ("accepted", "rejected", "needs_evidence"):
        command = f'evallab insights review I-0 {decision} --by "Peter Makhnatch"'
        assert command in markdown
        assert f"--state-dir {tmp_path}" in markdown
    assert status["cards"][0]["model_served"] == malicious
    assert "Lens: connector. Author (attested):" in markdown


def test_board_markdown_neutralizes_newline_and_control_injection(tmp_path: Path) -> None:
    injected = "# forged heading\n## injected\n> quote\n- bullet\n| table\n```python"
    record = _insight(0).model_copy(update={
        "model_served": "author\n# forged model heading",
        "referee_model": "referee\n> forged quote",
        "card": _insight(0).card.model_copy(update={
            "title": injected,
            "statement": "```text\nforged fence",
            "observations": ["line one\n> forged observation"],
            "interpretation": "line one\n- forged interpretation",
        }),
    })
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights([record])
        store.set_meta("board_headline", "> forged headline\nnew line")
        markdown, _, status = _render(store, tmp_path)
    card_lines = markdown.split("## Awaiting referee", 1)[0]
    assert "### \\# forged heading ## injected > quote - bullet | table ```python" in card_lines
    assert "\n# forged" not in markdown
    assert "\n## injected" not in markdown
    assert "\n> forged" not in markdown
    assert "\n- forged" not in markdown
    assert "\n```" not in markdown
    assert "author # forged model heading" in markdown
    assert "\\```text forged fence" in markdown
    assert "\\> forged headline new line" in markdown
    # JSON remains the lossless record; Markdown is deliberately presentation-safe.
    assert status["cards"][0]["model_served"] == "author\n# forged model heading"
    assert json.loads(json.dumps(status)) == status


def test_board_reads_serialized_tick_contract_and_displays_age_errors(tmp_path: Path) -> None:
    tick = {
        "started_at": (NOW - timedelta(minutes=3)).isoformat(),
        "finished_at": (NOW - timedelta(minutes=2)).isoformat(),
        "errors": ["mirror refresh failed <source>"], "waiting": {"skeptic": "cooldown"},
    }
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.set_meta("last_tick", json.dumps(tick))
        # Metadata is authoritative, not an unrelated event matching %tick%.
        store.log_event("tick_finished", {}, at=NOW - timedelta(days=1))
        markdown, document, status = _render(store, tmp_path)
    assert status["last_tick"] == tick["finished_at"]
    assert status["last_tick_report"] == tick
    assert status["last_tick_age_seconds"] == 120
    assert "2026-11-02 12:58:00 PM ET" in markdown
    assert "2.0 minutes old" in markdown
    assert "mirror refresh failed &lt;source&gt;" in document
    assert "skeptic: cooldown" in document
    assert "Last tick: not recorded" not in markdown


@pytest.mark.parametrize("raw", [NOW.isoformat(), json.dumps({"started_at": NOW.isoformat()})])
def test_board_supports_started_tick_and_legacy_timestamp(tmp_path: Path, raw: str) -> None:
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.set_meta("last_tick", raw)
        _, _, status = _render(store, tmp_path)
    assert status["last_tick"] == NOW.isoformat()


@pytest.mark.parametrize("needs_approval", [True, False])
def test_proposals_are_never_execution_authorization(tmp_path: Path, needs_approval: bool) -> None:
    record = _insight(0)
    proposal = ProposedExperiment(
        hypothesis="Existing receipts reveal the mechanism.", primary_variable="detector mode",
        fixed_variables=["dataset"], arms=["baseline", "variant"], est_cost_usd=0,
        cost_basis="Existing data only.", needs_approval=needs_approval, reuses_existing_data=True,
        expected_if_true="The effect remains.", expected_if_false="The effect disappears.",
    )
    record = record.model_copy(update={"card": record.card.model_copy(update={"proposed_experiment": proposal})})
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights([record])
        _review(store, 0, "accepted")
        markdown, document, status = _render(store, tmp_path)
    for rendered in (markdown, document):
        assert "Proposal only; execution not authorized." in rendered
        assert "execution_authorized: false" in rendered
        assert "Needs Peter's approval: no" not in rendered
    assert status["cards"][0]["execution_authorized"] is False


def test_truncation_is_explicit_with_complete_lane_totals(tmp_path: Path) -> None:
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights(_insight(index) for index in range(4))
        store.upsert_signals(SignalRecord(
            signal_id=f"S-{index}", kind="underpowered_null", score=index,
            title=f"Signal {index}", claim_ids=["C-evidence"], created_at=NOW,
        ) for index in range(5))
        markdown, document, status = _render(store, tmp_path, top_n=2)
    assert status["open_signals_total"] == 5
    assert len(status["displayed_open_signal_ids"]) == 2
    assert status["signals_truncated"] is True
    assert "open_signal_ids" not in status  # A truncated list must not imply completeness.
    assert status["lane_counts"]["refereed_accepted"] == 4
    assert len(status["lane_insight_ids"]["refereed_accepted"]) == 4
    assert len(status["displayed_lane_insight_ids"]["refereed_accepted"]) == 2
    assert status["lanes_truncated"]["refereed_accepted"] is True
    assert status["lanes_truncated"]["check_failed"] is False
    assert "Open QC signals — showing 2 of 5" in markdown
    assert "List truncated." in document


def test_scoreboards_preserve_predictions_pass_activity_and_empty_states(tmp_path: Path) -> None:
    with InsightStore(tmp_path / "ledger.sqlite") as store:
        store.insert_insights([_insight(0)])
        store.insert_predictions([PredictionRecord(
            prediction_id="I-0.p1", insight_id="I-0", status="hit", created_at=NOW,
            spec=PredictionSpec(
                statement="The pattern will recur in the next run.", direction="at_least", low=1,
                resolves_when="The next frozen experiment report is available.",
            ),
        )])
        store.save_pass(PassRecord(
            pass_id="P-failure", lens="skeptic", route="requested", trigger="tick",
            status="failed", pass_dir="/unused", created_at=NOW - timedelta(hours=30),
            started_at=NOW - timedelta(minutes=4), finished_at=NOW, served={"actual-model": 1},
        ))
        markdown, _, status = _render(store, tmp_path)
    assert "actual-model; 4.0 minutes" in markdown
    assert status["failures_last_24h"] == 1
    assert status["prediction_scoreboard"]["connector"]["hit"] == 1
    assert status["lens_scoreboard"][0]["checks_passed"] == 1
    with InsightStore(tmp_path / "empty.sqlite") as store:
        markdown, document, status = _render(store, tmp_path)
    for empty in ("No accepted findings yet", "No tracked predictions yet.", "No cards yet.", "No recent passes."):
        assert empty in document
    assert "not recorded" in markdown
    assert status["signals_truncated"] is False
    assert status["open_signals_total"] == 0
    assert plain_count(0, 0) == "0 of 0 (not yet measured)"
