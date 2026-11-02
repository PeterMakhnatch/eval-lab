"""Tests to verify fixture integrity, schema compliance, and absence of PII."""

from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
ROUTES_JSON = Path(__file__).resolve().parent.parent / "routes.json"


def test_routes_json_validity() -> None:
    assert ROUTES_JSON.exists(), f"routes.json missing: {ROUTES_JSON}"
    data = json.loads(ROUTES_JSON.read_text(encoding="utf-8"))

    assert "pools" in data
    assert "routes" in data
    assert "devin" in data["pools"]
    assert "anthropic" in data["pools"]
    assert "google-antigravity-gemini" in data["pools"]

    for pool_id, pool in data["pools"].items():
        assert "gate_percent" in pool, f"Pool {pool_id} missing gate_percent"
        assert "metrics" in pool, f"Pool {pool_id} missing metrics"
        for _m_key, m_val in pool["metrics"].items():
            assert "measured_burn_pct_per_hour" in m_val
            assert "sample_size_hours" in m_val
            assert "confidence" in m_val
            assert m_val["confidence"] in ("low", "medium", "medium-high", "high")

    for route_id, route in data["routes"].items():
        assert "pool" in route
        assert route["pool"] in data["pools"], f"Route {route_id} points to non-existent pool {route['pool']}"


def test_fixtures_no_pii() -> None:
    """Every fixture must be free of personal email addresses, real names, or secret tokens."""
    # Pattern detecting actual personal emails (excluding sanitized placeholders)
    email_pattern = re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net))[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

    forbidden_strings = [
        "p.makhnatch",
        "Makhnatch",
        "nevermorezxc",
    ]

    for fix_file in FIXTURES_DIR.glob("*.txt"):
        text = fix_file.read_text(encoding="utf-8")
        assert not email_pattern.search(text), f"PII email found in {fix_file.name}"
        for forb in forbidden_strings:
            assert forb not in text, f"PII string '{forb}' found in {fix_file.name}"
