"""Tests for omp usage text parser and duration helpers."""

from __future__ import annotations

from pathlib import Path

from quota_router.models import UsageSnapshot
from quota_router.parser import (
    parse_duration_to_seconds,
    parse_usage,
    scrub_usage_text,
    strip_ansi,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_parse_duration_to_seconds() -> None:
    assert parse_duration_to_seconds("5m") == 300.0
    assert parse_duration_to_seconds("3h25m") == 12300.0
    assert parse_duration_to_seconds("6d18h") == 583200.0
    assert parse_duration_to_seconds("1d22h") == 165600.0
    assert parse_duration_to_seconds("45s") == 45.0
    assert parse_duration_to_seconds("") is None
    assert parse_duration_to_seconds("invalid") is None
    assert parse_duration_to_seconds(None) is None


def test_strip_ansi() -> None:
    colored = "\x1b[32m7.0%\x1b[0m used"
    assert strip_ansi(colored) == "7.0% used"


def test_parse_real_scrubbed_fixture() -> None:
    path = FIXTURES_DIR / "omp_usage_real_scrubbed.txt"
    assert path.exists(), f"Fixture missing: {path}"
    raw = path.read_text(encoding="utf-8")

    snapshot = parse_usage(raw)
    assert isinstance(snapshot, UsageSnapshot)

    # Check key metrics
    claude = snapshot.metrics.get("claude_7d")
    assert claude is not None
    assert claude.percent_used == 7.0
    assert claude.resets_in_str == "6d18h"
    assert claude.resets_in_seconds == 583200.0

    devin_d = snapshot.metrics.get("devin_daily")
    assert devin_d is not None
    assert devin_d.percent_used == 9.0
    assert devin_d.resets_in_str == "5m"
    assert devin_d.resets_in_seconds == 300.0

    devin_w = snapshot.metrics.get("devin_wk")
    assert devin_w is not None
    assert devin_w.percent_used == 4.0
    assert devin_w.resets_in_str == "6d"
    assert devin_w.resets_in_seconds == 518400.0

    gemini_5h = snapshot.metrics.get("gemini_5h")
    assert gemini_5h is not None
    assert gemini_5h.percent_used == 17.0
    assert gemini_5h.resets_in_str == "3h25m"
    assert gemini_5h.resets_in_seconds == 12300.0

    gemini_wk = snapshot.metrics.get("gemini_wk")
    assert gemini_wk is not None
    assert gemini_wk.percent_used == 9.2

    ag_shared = snapshot.metrics.get("ag_shared_wk")
    assert ag_shared is not None
    assert ag_shared.percent_used == 88.1

    # Check balances
    assert snapshot.balances_usd.get("deepseek_balance_usd") == 0.85
    assert snapshot.balances_usd.get("overage_usd") == -2.43


def test_parse_malformed_usage_text() -> None:
    path = FIXTURES_DIR / "omp_usage_malformed.txt"
    raw = path.read_text(encoding="utf-8")

    snapshot = parse_usage(raw)
    assert isinstance(snapshot, UsageSnapshot)
    # Malformed text should parse without throwing and return empty metrics
    assert len(snapshot.metrics) == 0
    assert len(snapshot.balances_usd) == 0

    # None and empty string
    empty_snap = parse_usage("")
    assert len(empty_snap.metrics) == 0
    none_snap = parse_usage(None)
    assert len(none_snap.metrics) == 0


def test_scrub_usage_text() -> None:
    sample = (
        "Anthropic — 1 account\n"
        "  ● alice@secretcorp.com · Peter Makhnatch · ✦ 1 reset\n"
        "      ● Claude 7 Day  7.0% used\n"
        "Devin — 1 account\n"
        "  ● bob@example.org · devin-team$account-8df4af68970442359098c94c4b156bd8\n"
        "Kimi Code — 1 account\n"
        "  ● d9givrtqip61ts4n824g · fetched 3m ago\n"
    )
    scrubbed = scrub_usage_text(sample)
    assert "alice@secretcorp.com" not in scrubbed
    assert "bob@example.org" not in scrubbed
    assert "Peter Makhnatch" not in scrubbed
    assert "8df4af68970442359098c94c4b156bd8" not in scrubbed
    assert "d9givrtqip61ts4n824g" not in scrubbed
    assert "user@example.com" in scrubbed
    assert "User" in scrubbed
    assert "devin-team$account-scrubbed" in scrubbed
    assert "scrubbed-token" in scrubbed
