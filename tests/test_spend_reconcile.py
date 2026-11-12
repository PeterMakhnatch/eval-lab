"""Behavioural tests for evallab spend reconcile and OMP session scanning.

Covers:
- Window boundary arithmetic (half-open [window_start, window_end));
- Provider endpoint failure and timeout handling (graceful fetch_error);
- Discrepancy threshold gating (exit 0 below/at threshold, exit 1 above);
- OMP session log discovery and idempotent sync to spend.jsonl;
- CLI dispatch and JSON output contracts.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evallab.spend_reconcile import (
    DEEPSEEK_OCT30_BASELINE_TS,
    fetch_deepseek_balance,
    fetch_meta_stats,
    fetch_openrouter_usage,
    read_spend_ledger,
    reconcile_spend,
    scan_omp_sessions,
    sync_omp_sessions_to_ledger,
)


def _make_ledger_line(
    route: str,
    cost_usd: float,
    ts: datetime,
    *,
    purpose: str = "test",
    provider: str | None = None,
) -> str:
    payload = {
        "route": route,
        "cost_usd": cost_usd,
        "usd": cost_usd,
        "ts": ts.isoformat(),
        "purpose": purpose,
    }
    if provider:
        payload["provider"] = provider
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# 1. Window boundary arithmetic
# ---------------------------------------------------------------------------


def test_window_boundary_arithmetic(tmp_path: Path) -> None:
    """Half-open [start, end) includes start, includes end-1us, excludes before and at/after end."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 10, 31, 14, 0, 0, tzinfo=UTC)

    lines = [
        # Exactly 1 second before window -> EXCLUDED
        _make_ledger_line("deepseek/flash", 1.0, t_start - timedelta(seconds=1)),
        # Exactly at window start -> INCLUDED
        _make_ledger_line("deepseek/flash", 2.0, t_start),
        # In middle of window -> INCLUDED
        _make_ledger_line("deepseek/flash", 3.0, t_start + timedelta(hours=1)),
        # 1 microsecond before window end -> INCLUDED
        _make_ledger_line("deepseek/flash", 4.0, t_end - timedelta(microseconds=1)),
        # Exactly at window end -> EXCLUDED
        _make_ledger_line("deepseek/flash", 5.0, t_end),
        # 1 second after window end -> EXCLUDED
        _make_ledger_line("deepseek/flash", 6.0, t_end + timedelta(seconds=1)),
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    totals = read_spend_ledger(ledger, t_start, t_end)
    ds_data = totals.get("deepseek")
    assert ds_data is not None
    # Included: 2.0 + 3.0 + 4.0 = 9.0
    assert ds_data["usd"] == pytest.approx(9.0)
    assert ds_data["records"] == 3


def test_window_boundary_across_multiple_providers(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)

    lines = [
        _make_ledger_line("deepseek/v4", 2.50, t_start + timedelta(hours=2)),
        _make_ledger_line("openrouter/sonnet", 1.25, t_start + timedelta(hours=4)),
        _make_ledger_line("modal", 5.00, t_start + timedelta(hours=6)),
        # Out of window
        _make_ledger_line("deepseek/v4", 10.00, t_start - timedelta(hours=1)),
        _make_ledger_line("openrouter/sonnet", 20.00, t_end + timedelta(hours=1)),
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    totals = read_spend_ledger(ledger, t_start, t_end)
    assert totals["deepseek"]["usd"] == pytest.approx(2.50)
    assert totals["openrouter"]["usd"] == pytest.approx(1.25)
    assert totals["modal"]["usd"] == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# 2. Provider endpoint failure and timeout handling
# ---------------------------------------------------------------------------


def test_deepseek_balance_network_failure() -> None:
    """URLError or HTTPError does not crash, returns None gracefully."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        bal = fetch_deepseek_balance(api_key="mock_key", allow_omp_fallback=False)
        assert bal is None


def test_deepseek_balance_malformed_json() -> None:
    """Non-JSON response handled cleanly without raising."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>Bad Gateway 502</html>"
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        bal = fetch_deepseek_balance(api_key="mock_key", allow_omp_fallback=False)
        assert bal is None


def test_openrouter_usage_network_failure() -> None:
    """OpenRouter error returns None gracefully."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError("https://openrouter.ai", 500, "Server Error", {}, None),  # type: ignore[arg-type]
    ):
        usage = fetch_openrouter_usage(api_key="mock_key")
        assert usage is None


def test_openrouter_usage_missing_key() -> None:
    """When no key is configured anywhere, returns None gracefully."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("evallab.spend_reconcile._get_provider_key_from_omp", return_value=None),
    ):
        usage = fetch_openrouter_usage(api_key=None)
        assert usage is None


def test_reconciliation_handles_fetch_error(tmp_path: Path) -> None:
    """Report surfaces fetch_error status, unverified=True, and verdict=UNVERIFIED (B3)."""
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text("", encoding="utf-8")

    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    with (
        patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=None),
        patch("evallab.spend_reconcile.fetch_openrouter_usage", return_value=None),
    ):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            threshold_usd=1.0,
            providers=["deepseek", "openrouter"],
        )

        statuses = {p.provider: p.status for p in report.providers}
        assert statuses["deepseek"] == "fetch_error"
        assert statuses["openrouter"] == "fetch_error"
        assert report.unverified is True
        assert report.verdict == "UNVERIFIED"
        assert report.exceeded_threshold is False

# ---------------------------------------------------------------------------
# 3. Threshold gating and gap calculation
# ---------------------------------------------------------------------------


def test_gap_below_threshold_passes(tmp_path: Path) -> None:
    """When discrepancy is within threshold, status is OK and exceeded_threshold is False."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    # Ledger has $10.00 spent
    ledger.write_text(_make_ledger_line("deepseek/flash", 10.00, t_start + timedelta(hours=1)) + "\n")

    # Start $15.00, current $4.50 -> real spend $10.50 -> gap = $0.50
    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=4.50):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            threshold_usd=1.00,  # Gap $0.50 <= $1.00
            providers=["deepseek"],
            deepseek_start_balance=15.00,
        )

        assert report.exceeded_threshold is False
        p = report.providers[0]
        assert p.status == "ok"
        assert p.gap_usd == pytest.approx(0.50)


def test_gap_exceeds_threshold_fails(tmp_path: Path) -> None:
    """When discrepancy exceeds threshold, status is gap_exceeded and exceeded_threshold is True."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    # Ledger has $4.00 spent
    ledger.write_text(_make_ledger_line("deepseek/flash", 4.00, t_start + timedelta(hours=1)) + "\n")

    # Start $16.00, current $0.88 -> real spend $15.12 -> gap = $11.12
    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=0.88):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            threshold_usd=1.00,  # Gap $11.12 > $1.00
            providers=["deepseek"],
            deepseek_start_balance=16.00,
        )

        assert report.exceeded_threshold is True
        p = report.providers[0]
        assert p.status == "gap_exceeded"
        assert p.gap_usd == pytest.approx(11.12)


def test_gap_exact_threshold_boundary(tmp_path: Path) -> None:
    """A gap exactly equal to the threshold is allowed (not strictly exceeded)."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    # Ledger has $10.00 spent
    ledger.write_text(_make_ledger_line("deepseek/flash", 10.00, t_start + timedelta(hours=1)) + "\n")

    # Start $15.00, current $4.00 -> spend $11.00 -> gap exactly $1.00
    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=4.00):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            threshold_usd=1.00,
            providers=["deepseek"],
            deepseek_start_balance=15.00,
        )

        assert report.exceeded_threshold is False
        assert report.providers[0].status == "ok"


def test_gap_negative_exceeds_threshold_flags_overstatement(tmp_path: Path) -> None:
    """Negative gap (ledger overstatement) strictly exceeding threshold flags gap_exceeded (S3)."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    # Ledger says $10.00 spent, but provider spend is only $1.00 -> gap = -$9.00
    ledger.write_text(_make_ledger_line("deepseek/deepseek-flash", 10.00, t_start + timedelta(hours=1)) + "\n")

    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=15.00):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            threshold_usd=1.00,
            providers=["deepseek"],
            deepseek_start_balance=16.00,
        )

        assert report.exceeded_threshold is True
        p = report.providers[0]
        assert p.status == "gap_exceeded"
        assert p.gap_usd == pytest.approx(-9.00)
        assert "over-reported in ledger" in p.note


def test_modal_reported_as_not_measured(tmp_path: Path) -> None:
    """Modal is recorded as unsupported/NOT MEASURED and excluded from totals (S1)."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    ledger.write_text(_make_ledger_line("modal", 2.50, t_start + timedelta(hours=1)) + "\n")

    report = reconcile_spend(
        t_start,
        t_end,
        ledger_path=ledger,
        providers=["modal"],
    )
    p = report.providers[0]
    assert p.status == "unsupported"
    assert p.reported_usd is None
    assert p.gap_usd is None
    assert "NOT MEASURED" in p.note
    assert report.total_reported_usd == 0.0
    assert report.total_gap_usd == 0.0
    # Since modal had no numeric gap, overall report is unverified
    assert report.unverified is True
    assert report.verdict == "UNVERIFIED"


def test_meta_unpriced_calls_reported_as_unverifiable(tmp_path: Path) -> None:
    """Meta unpriced calls in stats.db yield unverifiable status, not ok (S2)."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    ledger.write_text("", encoding="utf-8")

    mock_stats = {"calls": 10, "cost_total": 0.0, "unpriced_calls": 10}
    with patch("evallab.spend_reconcile.fetch_meta_stats", return_value=mock_stats):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            providers=["meta"],
        )
        p = report.providers[0]
        assert p.status == "unverifiable"
        assert p.gap_usd is None
        assert "unpriced calls present" in p.note


def test_openrouter_start_usage_required_for_numeric_gap(tmp_path: Path) -> None:
    """When openrouter_start_usage is omitted, reported_usd and gap are None (B4)."""
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    ledger.write_text("", encoding="utf-8")

    mock_usage = {"usage": 45.50, "limit": 100.0, "remaining": 54.50, "is_free_tier": False}
    with patch("evallab.spend_reconcile.fetch_openrouter_usage", return_value=mock_usage):
        report = reconcile_spend(
            t_start,
            t_end,
            ledger_path=ledger,
            providers=["openrouter"],
            openrouter_start_usage=None,
        )
        p = report.providers[0]
        assert p.reported_usd is None
        assert p.gap_usd is None
        assert "start usage unknown" in p.note


def test_deepseek_baseline_requires_exact_oct30_timestamp(tmp_path: Path) -> None:
    """DeepSeek baseline of $16.00 is only assumed if window_start matches Oct 30 baseline (B4)."""
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text("", encoding="utf-8")

    # Case A: Window start is Oct 30 21:00 UTC -> baseline 16.00 used
    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=14.00):
        rep_a = reconcile_spend(
            DEEPSEEK_OCT30_BASELINE_TS,
            DEEPSEEK_OCT30_BASELINE_TS + timedelta(days=1),
            ledger_path=ledger,
            providers=["deepseek"],
            deepseek_start_balance=None,
        )
        assert rep_a.providers[0].reported_usd == pytest.approx(2.00)
        assert rep_a.providers[0].gap_usd == pytest.approx(2.00)

    # Case B: Window start is weeks earlier (e.g. Oct 1) -> baseline NOT assumed, start unknown
    earlier = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=14.00):
        rep_b = reconcile_spend(
            earlier,
            earlier + timedelta(days=1),
            ledger_path=ledger,
            providers=["deepseek"],
            deepseek_start_balance=None,
        )
        assert rep_b.providers[0].reported_usd is None
        assert rep_b.providers[0].gap_usd is None
        assert "start balance unknown" in rep_b.providers[0].note
# ---------------------------------------------------------------------------
# 4. OMP session scanner and sync
# ---------------------------------------------------------------------------


def test_omp_session_scanner_and_idempotent_sync(tmp_path: Path) -> None:
    """Scans session JSONL traces, extracts paid calls, and appends to ledger idempotently."""
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    sess_file = sess_dir / "test_session.jsonl"

    t1 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 1, 10, 5, tzinfo=UTC)

    records = [
        # Non-message event (title) -> ignored
        {"type": "title", "title": "my session"},
        # Paid call 1: DeepSeek
        {
            "type": "message",
            "id": "msg-001",
            "timestamp": t1.isoformat(),
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "usage": {
                    "input": 100,
                    "output": 50,
                    "cacheRead": 200,
                    "cost": {"total": 0.0025},
                },
            },
        },
        # Paid call 2: OpenRouter
        {
            "type": "message",
            "id": "msg-002",
            "timestamp": t2.isoformat(),
            "message": {
                "role": "assistant",
                "provider": "openrouter",
                "model": "openai/gpt-oss-120b",
                "usage": {
                    "input": 500,
                    "output": 100,
                    "cost": {"total": 0.0150},
                },
            },
        },
    ]
    sess_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # 1. Scan sessions
    discovered = scan_omp_sessions(session_dirs=[sess_dir])
    assert len(discovered) == 2
    assert discovered[0].provider == "deepseek"
    assert discovered[0].cost_usd == pytest.approx(0.0025)
    assert discovered[1].provider == "openrouter"
    assert discovered[1].cost_usd == pytest.approx(0.0150)

    # 2. Sync to empty ledger
    ledger = tmp_path / "spend.jsonl"
    added, usd = sync_omp_sessions_to_ledger(discovered, ledger)
    assert added == 2
    assert usd == pytest.approx(0.0175)
    assert ledger.is_file()

    # 3. Second sync with same calls -> IDEMPOTENT (0 added)
    added_second, usd_second = sync_omp_sessions_to_ledger(discovered, ledger)
    assert added_second == 0
    assert usd_second == pytest.approx(0.0)


def test_subscription_routes_excluded_from_paid_calls(tmp_path: Path) -> None:
    """Subscription routes (anthropic, openai-codex, xai-oauth) excluded by default from paid spend (B1)."""
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    sess_file = sess_dir / "review_smoke.jsonl"

    t = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    records = [
        # Anthropic (subscription)
        {
            "type": "message",
            "id": "a1",
            "timestamp": t.isoformat(),
            "message": {
                "role": "assistant",
                "provider": "anthropic",
                "model": "claude-opus-5-5",
                "usage": {"input": 10, "output": 5, "cost": {"total": 3.0}},
            },
        },
        # DeepSeek (paid)
        {
            "type": "message",
            "id": "d1",
            "timestamp": (t + timedelta(seconds=60)).isoformat(),
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "usage": {"input": 10, "output": 5, "cost": {"total": 0.5}},
            },
        },
        # OpenAI Codex (subscription)
        {
            "type": "message",
            "id": "c1",
            "timestamp": (t + timedelta(seconds=120)).isoformat(),
            "message": {
                "role": "assistant",
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "usage": {"input": 10, "output": 5, "cost": {"total": 1.0}},
            },
        },
    ]
    sess_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    scan_stats: dict[str, int] = {}
    paid_calls = scan_omp_sessions(session_dirs=[sess_dir], stats=scan_stats)

    # Only deepseek is returned as paid call
    assert len(paid_calls) == 1
    assert paid_calls[0].provider == "deepseek"
    assert paid_calls[0].cost_usd == pytest.approx(0.5)
    assert scan_stats.get("subscription_calls") == 2

    # Syncing to ledger only writes the paid call
    ledger = tmp_path / "spend.jsonl"
    added, added_usd = sync_omp_sessions_to_ledger(paid_calls, ledger)
    assert added == 1
    assert added_usd == pytest.approx(0.5)

    # Verify ledger route format has provider prefix (NIT 1)
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert rows[0]["route"] == "deepseek/deepseek-flash"


def test_missing_timestamp_skipped_and_counted(tmp_path: Path) -> None:
    """Calls without a timestamp are skipped and counted, never defaulted to now() (S4)."""
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    sess_file = sess_dir / "no_ts.jsonl"

    records = [
        {
            "type": "message",
            "id": "d2",
            # No timestamp
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "usage": {"input": 10, "output": 5, "cost": {"total": 0.25}},
            },
        }
    ]
    sess_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    stats: dict[str, int] = {}
    calls = scan_omp_sessions(session_dirs=[sess_dir], stats=stats)
    assert len(calls) == 0
    assert stats.get("missing_timestamp") == 1


def test_lab_logged_row_deduplication(tmp_path: Path) -> None:
    """Session call matching an already-logged lab row is skipped to prevent double counting (B2)."""
    ledger = tmp_path / "spend.jsonl"
    t = datetime(2026, 11, 1, 10, 1, 5, tzinfo=UTC)
    # Existing lab row
    lab_row = {
        "ts": t.isoformat(),
        "route": "deepseek/deepseek-flash",
        "cost_usd": 0.5,
        "job": "labrun",
        "note": "lab-logged",
    }
    ledger.write_text(json.dumps(lab_row) + "\n", encoding="utf-8")

    # Session trace containing the same call 5 seconds earlier
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    sess_file = sess_dir / "overlap.jsonl"
    sess_record = {
        "type": "message",
        "id": "d1",
        "timestamp": (t - timedelta(seconds=5)).isoformat(),
        "message": {
            "role": "assistant",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "usage": {"input": 10, "output": 5, "cost": {"total": 0.5}},
        },
    }
    sess_file.write_text(json.dumps(sess_record) + "\n", encoding="utf-8")

    calls = scan_omp_sessions(session_dirs=[sess_dir])
    assert len(calls) == 1

    # Sync should recognize overlap with existing lab row and NOT append
    added, added_usd = sync_omp_sessions_to_ledger(calls, ledger)
    assert added == 0
    assert added_usd == pytest.approx(0.0)


def test_fetch_meta_stats(tmp_path: Path) -> None:
    """fetch_meta_stats queries stats.db messages correctly over window (S5)."""
    db_file = tmp_path / "stats.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE messages (provider TEXT, timestamp INTEGER, cost_total REAL)"
    )
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    t_in = t_start + timedelta(hours=2)
    t_out = t_start + timedelta(days=5)

    ms_in = int(t_in.timestamp() * 1000)
    ms_out = int(t_out.timestamp() * 1000)

    conn.execute("INSERT INTO messages VALUES ('meta', ?, 0.0)", (ms_in,))
    conn.execute("INSERT INTO messages VALUES ('meta', ?, 1.25)", (ms_in,))
    conn.execute("INSERT INTO messages VALUES ('anthropic', ?, 5.0)", (ms_in,))
    conn.execute("INSERT INTO messages VALUES ('meta', ?, 2.0)", (ms_out,))
    conn.commit()
    conn.close()

    res = fetch_meta_stats(t_start, t_start + timedelta(days=1), stats_db_path=db_file)
    assert res["calls"] == 2
    assert res["cost_total"] == pytest.approx(1.25)
    assert res["unpriced_calls"] == 1

# ---------------------------------------------------------------------------
# 5. CLI commands integration
# ---------------------------------------------------------------------------


