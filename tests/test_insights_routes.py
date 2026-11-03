"""Route admission stays deterministic across quota and independence gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evallab.insights.config import RouteSpec
from evallab.insights.routes import choose_route

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
CLAUDE = "anthropic/claude-sonnet-5-5"
GEMINI = "google-antigravity/gemini-3.8-flash"
GPT = "openai-codex/gpt-6.1-terra"
KIMI = "kimi-code/k3.1-pro"


def test_skips_usage_parked_and_family_in_order() -> None:
    choice = choose_route(
        RouteSpec(routes=[CLAUDE, GEMINI, GPT, KIMI]),
        usage={CLAUDE: 85, GEMINI: 10, GPT: 30, KIMI: 50},
        parked={GEMINI: NOW + timedelta(minutes=10)},
        avoid_families={"openai"}, max_used_percent=85, now=NOW,
    )
    assert choice.route == KIMI
    assert [route for route, _ in choice.skipped] == [CLAUDE, GEMINI, GPT]
    assert "85%" in choice.skipped[0][1]
    assert "Parked" in choice.skipped[1][1]
    assert "family" in choice.skipped[2][1]


def test_all_routes_blocked() -> None:
    choice = choose_route(
        RouteSpec(routes=[CLAUDE, GEMINI]), usage={CLAUDE: 99},
        parked={"google-antigravity": NOW + timedelta(hours=1)},
        avoid_families=set(), max_used_percent=85, now=NOW,
    )
    assert choice.route is None
    assert len(choice.skipped) == 2
    assert "All" in choice.reason


def test_unknown_usage_allowed_and_expired_parking_ignored() -> None:
    choice = choose_route(
        RouteSpec(routes=[CLAUDE, GEMINI]), usage={CLAUDE: None},
        parked={CLAUDE: NOW}, avoid_families=set(), max_used_percent=85, now=NOW,
    )
    assert choice.route == CLAUDE
    assert "unknown" in choice.reason
    assert not choice.skipped


def test_antigravity_buckets_are_full_route_keys() -> None:
    shared = "google-antigravity/claude-sonnet-5-5"
    choice = choose_route(
        RouteSpec(routes=[shared, GEMINI]), usage={shared: 95, GEMINI: 17},
        parked={}, avoid_families=set(), max_used_percent=85, now=NOW,
    )
    assert choice.route == GEMINI
    assert len(choice.skipped) == 1


def test_provider_park_remains_active_after_route_park_expires() -> None:
    choice = choose_route(
        RouteSpec(routes=[CLAUDE]), usage={CLAUDE: 10},
        parked={CLAUDE: NOW, "anthropic": NOW + timedelta(minutes=1)},
        avoid_families=set(), max_used_percent=85, now=NOW,
    )
    assert choice.route is None
