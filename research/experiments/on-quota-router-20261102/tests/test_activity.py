"""Tests for Temporal-compatible activity route_quota_ok."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from quota_router.activity import route_quota_ok, route_quota_ok_async
from quota_router.exceptions import RouteExhausted

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_route_quota_ok_standard_signature() -> None:
    """Test primary brief interface: route_quota_ok(route, worker_hours) -> (bool, reason)."""
    usage_text = (FIXTURES_DIR / "omp_usage_real_scrubbed.txt").read_text(encoding="utf-8")

    # Gemini Flash has plenty of quota: 17.0% used, gate 60.0%
    ok, reason = route_quota_ok(
        route="google-antigravity/gemini-3.8-flash",
        worker_hours=2.0,
        usage_text=usage_text,
    )
    assert ok is True
    assert "OK:" in reason
    assert "projected usage" in reason

    # Devin has high burn: 60%/h, 2.0h -> 120% burn + 9% = 129% > 80% gate
    ok_devin, reason_devin = route_quota_ok(
        route="devin/claude-sonnet-5-5",
        worker_hours=2.0,
        usage_text=usage_text,
    )
    assert ok_devin is False
    assert "REFUSE:" in reason_devin
    assert "projected usage 129.0% >= gate 80.0%" in reason_devin


def test_route_quota_ok_f3_signature() -> None:
    """Test backwards-compatibility with F3's positional signature: (quota_file, route)."""
    usage_text = (FIXTURES_DIR / "omp_usage_real_scrubbed.txt").read_text(encoding="utf-8")

    # Called as route_quota_ok(None, route)
    ok, reason = route_quota_ok(
        route=None,
        worker_hours="google-antigravity/gemini-3.8-flash",
        usage_text=usage_text,
    )
    assert ok is True
    assert "OK:" in reason


def test_route_quota_ok_raise_on_refusal() -> None:
    """When raise_on_refusal=True, raises RouteExhausted exception."""
    usage_text = (FIXTURES_DIR / "omp_usage_above_gate.txt").read_text(encoding="utf-8")

    with pytest.raises(RouteExhausted) as exc_info:
        route_quota_ok(
            route="anthropic/claude-sonnet-5-5",
            worker_hours=1.0,
            usage_text=usage_text,
            raise_on_refusal=True,
        )
    assert "quota exhausted" in str(exc_info.value)


def test_route_quota_ok_async() -> None:
    """Test async activity wrapper."""
    usage_text = (FIXTURES_DIR / "omp_usage_real_scrubbed.txt").read_text(encoding="utf-8")

    ok, reason = asyncio.run(
        route_quota_ok_async(
            route="google-antigravity/gemini-3.8-flash",
            worker_hours=1.0,
            usage_text=usage_text,
        )
    )
    assert ok is True
    assert "OK:" in reason
