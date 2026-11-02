"""Behavioural tests for evallab spend reconcile and OMP session scanning.

Covers:
- Window boundary arithmetic (half-open [window_start, window_end));
- Provider endpoint failure and timeout handling (graceful fetch_error);
- Discrepancy threshold gating (exit 0 below/at threshold, exit 1 above);
- OMP session log discovery and idempotent sync to spend.jsonl;
- CLI dispatch and JSON output contracts.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evallab import cli
from evallab.spend_reconcile import (
    fetch_deepseek_balance,
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
    """Report surfaces fetch_error status without raising exception."""
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
        # Fetch errors alone do not trigger gap threshold
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


# ---------------------------------------------------------------------------
# 5. CLI commands integration
# ---------------------------------------------------------------------------


def test_cli_spend_reconcile_exit_code_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    ledger.write_text(_make_ledger_line("deepseek/flash", 10.00, t_start) + "\n")

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    spend = sub.add_parser("spend")
    s_sub = spend.add_subparsers(dest="spend_command")
    r_cmd = s_sub.add_parser("reconcile")
    r_cmd.add_argument("--since")
    r_cmd.add_argument("--until")
    r_cmd.add_argument("--threshold-usd", type=float, default=1.0)
    r_cmd.add_argument("--providers", default="deepseek")
    r_cmd.add_argument("--deepseek-start-balance", type=float)
    r_cmd.add_argument("--openrouter-start-usage", type=float)
    r_cmd.add_argument("--spend-log", type=Path)
    r_cmd.add_argument("--sync-omp-sessions", action="store_true")
    r_cmd.add_argument("--json", action="store_true")
    r_cmd.set_defaults(func=cli._spend_reconcile_command)

    args = parser.parse_args([
        "spend", "reconcile",
        "--since", "2026-11-01T00:00:00Z",
        "--until", "2026-11-02T00:00:00Z",
        "--threshold-usd", "2.0",
        "--providers", "deepseek",
        "--deepseek-start-balance", "10.50",
        "--spend-log", str(ledger),
    ])

    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=0.0):
        # spend reported = 10.50 - 0.0 = 10.50; ledger = 10.00; gap = 0.50 <= 2.0 -> exit 0
        ret = args.func(args, tmp_path)
        assert ret == 0

    out = capsys.readouterr().out
    assert "Spend Reconciliation" in out
    assert "PASS" in out


def test_cli_spend_reconcile_exit_code_one_on_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    ledger.write_text(_make_ledger_line("deepseek/flash", 1.00, t_start) + "\n")

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    spend = sub.add_parser("spend")
    s_sub = spend.add_subparsers(dest="spend_command")
    r_cmd = s_sub.add_parser("reconcile")
    r_cmd.add_argument("--since")
    r_cmd.add_argument("--until")
    r_cmd.add_argument("--threshold-usd", type=float, default=1.0)
    r_cmd.add_argument("--providers", default="deepseek")
    r_cmd.add_argument("--deepseek-start-balance", type=float)
    r_cmd.add_argument("--openrouter-start-usage", type=float)
    r_cmd.add_argument("--spend-log", type=Path)
    r_cmd.add_argument("--sync-omp-sessions", action="store_true")
    r_cmd.add_argument("--json", action="store_true")
    r_cmd.set_defaults(func=cli._spend_reconcile_command)

    args = parser.parse_args([
        "spend", "reconcile",
        "--since", "2026-11-01T00:00:00Z",
        "--until", "2026-11-02T00:00:00Z",
        "--threshold-usd", "1.0",
        "--providers", "deepseek",
        "--deepseek-start-balance", "16.00",
        "--spend-log", str(ledger),
    ])

    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=1.00):
        # spend reported = 16.00 - 1.00 = 15.00; ledger = 1.00; gap = 14.00 > 1.00 -> exit 1
        ret = args.func(args, tmp_path)
        assert ret == 1

    out = capsys.readouterr().out
    assert "EXCEEDED" in out
    assert "FAIL" in out


def test_cli_spend_reconcile_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "spend.jsonl"
    t_start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    ledger.write_text(_make_ledger_line("deepseek/flash", 5.00, t_start) + "\n")

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    spend = sub.add_parser("spend")
    s_sub = spend.add_subparsers(dest="spend_command")
    r_cmd = s_sub.add_parser("reconcile")
    r_cmd.add_argument("--since")
    r_cmd.add_argument("--until")
    r_cmd.add_argument("--threshold-usd", type=float, default=1.0)
    r_cmd.add_argument("--providers", default="deepseek")
    r_cmd.add_argument("--deepseek-start-balance", type=float)
    r_cmd.add_argument("--openrouter-start-usage", type=float)
    r_cmd.add_argument("--spend-log", type=Path)
    r_cmd.add_argument("--sync-omp-sessions", action="store_true")
    r_cmd.add_argument("--json", action="store_true")
    r_cmd.set_defaults(func=cli._spend_reconcile_command)

    args = parser.parse_args([
        "spend", "reconcile",
        "--since", "2026-11-01T00:00:00Z",
        "--until", "2026-11-02T00:00:00Z",
        "--threshold-usd", "10.0",
        "--providers", "deepseek",
        "--deepseek-start-balance", "10.00",
        "--spend-log", str(ledger),
        "--json",
    ])

    with patch("evallab.spend_reconcile.fetch_deepseek_balance", return_value=5.00):
        ret = args.func(args, tmp_path)
        assert ret == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["exceeded_threshold"] is False
    assert data["totals"]["ledger_usd"] == 5.0
    assert len(data["providers"]) == 1
    assert data["providers"][0]["provider"] == "deepseek"
