"""Tests for QuotaRouter, route selection, burn projection, and hysteresis."""

from __future__ import annotations

from pathlib import Path

from quota_router.models import PickResult
from quota_router.parser import parse_usage
from quota_router.router import DEFAULT_ROUTES_PATH, QuotaRouter, pick

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_refusal_above_gate() -> None:
    """Routes at or above gate percentage must be refused immediately."""
    usage_text = (FIXTURES_DIR / "omp_usage_above_gate.txt").read_text(encoding="utf-8")
    router = QuotaRouter(DEFAULT_ROUTES_PATH)
    usage = parse_usage(usage_text)

    # 1. Anthropic route: Claude 7 Day is 35.0% (gate is 30.0%) -> Refused
    ev_claude = router.evaluate_route("anthropic/claude-sonnet-5-5", need_worker_hours=0.1, usage=usage)
    assert not ev_claude.eligible
    assert "current usage 35.0% >= gate 30.0%" in (ev_claude.refusal_reason or "")

    # 2. Devin route: Daily Quota is 92.0% (gate is 80.0%) -> Refused
    ev_devin = router.evaluate_route("devin/claude-sonnet-5-5", need_worker_hours=0.1, usage=usage)
    assert not ev_devin.eligible
    assert "current usage 92.0% >= gate 80.0%" in (ev_devin.refusal_reason or "")

    # 3. Antigravity Claude shared: 88.1% (gate is 85.0%) -> Refused
    ev_shared = router.evaluate_route("google-antigravity/claude-sonnet-5-5", need_worker_hours=0.1, usage=usage)
    assert not ev_shared.eligible
    assert "current usage 88.1% >= gate 85.0%" in (ev_shared.refusal_reason or "")

    # 4. Gemini Flash is only 15.0% (gate is 60.0%) -> Eligible!
    ev_gemini = router.evaluate_route("google-antigravity/gemini-3.8-flash", need_worker_hours=1.0, usage=usage)
    assert ev_gemini.eligible

    # Pick with only above-gate candidates results in REFUSE
    res = router.pick(
        candidates=["anthropic/claude-sonnet-5-5", "devin/claude-sonnet-5-5"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert res.refused
    assert res.startswith("REFUSE")
    assert "current usage 35.0%" in res.reason
    assert "Claude 7 Day" in res.reason


def test_burn_based_refusal() -> None:
    """Route with enough current headroom but insufficient for planned burn must be refused."""
    usage_text = (FIXTURES_DIR / "omp_usage_burn_refusal.txt").read_text(encoding="utf-8")
    router = QuotaRouter(DEFAULT_ROUTES_PATH)
    usage = parse_usage(usage_text)

    # In fixture: Devin Daily Quota is 50.0% used. Gate is 80.0%. Headroom = 30.0%.
    # Measured burn rate = 60.0% / worker-hour.

    # Case A: Short duration (0.2h = 12m).
    # Burn = 60 * 0.2 = 12%. Projected = 50 + 12 = 62% <= 80%.
    ev_short = router.evaluate_route("devin/claude-sonnet-5-5", need_worker_hours=0.2, usage=usage)
    assert ev_short.eligible
    assert ev_short.projected_max_pct == 62.0

    # Case B: Standard duration (1.0h).
    # Burn = 60 * 1.0 = 60%. Projected = 50 + 60 = 110% > 80% (and > 100%).
    ev_long = router.evaluate_route("devin/claude-sonnet-5-5", need_worker_hours=1.0, usage=usage)
    assert not ev_long.eligible
    assert "projected usage 110.0% >= gate 80.0%" in (ev_long.refusal_reason or "")
    assert "60.0%/h * 1.00h" in (ev_long.refusal_reason or "")

    # Pick rejects Devin for 1.0h and falls back to eligible Gemini
    res = router.pick(
        candidates=["devin/claude-sonnet-5-5", "google-antigravity/gemini-3.8-flash"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert not res.refused
    assert res.route == "google-antigravity/gemini-3.8-flash"


def test_reset_time_handling() -> None:
    """Reset duration is parsed, checked, and reflected in refusal / evaluation metadata."""
    usage_text = (FIXTURES_DIR / "omp_usage_near_reset.txt").read_text(encoding="utf-8")
    router = QuotaRouter(DEFAULT_ROUTES_PATH)
    usage = parse_usage(usage_text)

    # In fixture: Devin Daily is 92.0% used, resets in 4m (240s)
    ev_devin = router.evaluate_route("devin/claude-sonnet-5-5", need_worker_hours=1.0, usage=usage)
    assert not ev_devin.eligible
    assert ev_devin.resets_in_seconds == 240.0
    assert "resets in 4m" in (ev_devin.refusal_reason or "")

    # In fixture: Gemini 5h is 95.0% used, resets in 3m (180s)
    ev_gemini = router.evaluate_route("google-antigravity/gemini-3.8-flash", need_worker_hours=1.0, usage=usage)
    assert not ev_gemini.eligible
    assert ev_gemini.resets_in_seconds == 180.0
    assert "resets in 3m" in (ev_gemini.refusal_reason or "")

    # Pick with only near-reset exhausted routes refuses with reset notice
    res = router.pick(
        candidates=["devin/claude-sonnet-5-5", "google-antigravity/gemini-3.8-flash"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert res.refused
    assert "resets in 4m" in res.reason


def test_hysteresis_threshold_schmitt_trigger(tmp_path: Path) -> None:
    """Once a route trips the gate, hysteresis lowers its gate by the hysteresis band."""
    state_file = tmp_path / "router_state.json"
    router = QuotaRouter(DEFAULT_ROUTES_PATH, state_file=state_file, hysteresis_band_pct=5.0)

    # Synthetic usage: Claude at 32.0% (gate is 30.0%) -> trips gate
    synth_usage_tripped = (
        "Anthropic — 1 account\n"
        "  ● Claude 7 Day  32.0% used · resets in 5d\n"
    )
    res1 = router.pick(
        candidates=["anthropic/claude-sonnet-5-5"],
        need_worker_hours=0.1,
        usage_text=synth_usage_tripped,
    )
    assert res1.refused

    # Reload router from saved state: route is marked tripped
    router2 = QuotaRouter(DEFAULT_ROUTES_PATH, state_file=state_file, hysteresis_band_pct=5.0)

    # Now Claude usage cools to 28.0% (below original 30% gate, but ABOVE 30% - 5% = 25% hysteresis gate)
    synth_usage_borderline = (
        "Anthropic — 1 account\n"
        "  ● Claude 7 Day  28.0% used · resets in 5d\n"
    )
    res2 = router2.pick(
        candidates=["anthropic/claude-sonnet-5-5"],
        need_worker_hours=0.1,
        usage_text=synth_usage_borderline,
    )
    # Must STILL be refused because it has not fallen below (gate - 5.0%) = 25.0%
    assert res2.refused
    assert "hysteresis threshold 25.0% applied" in res2.reason

    # Once Claude usage cools to 22.0% (< 25.0%), it is re-admitted
    synth_usage_cleared = (
        "Anthropic — 1 account\n"
        "  ● Claude 7 Day  22.0% used · resets in 5d\n"
    )
    res3 = router2.pick(
        candidates=["anthropic/claude-sonnet-5-5"],
        need_worker_hours=0.1,
        usage_text=synth_usage_cleared,
    )
    assert not res3.refused
    assert res3.route == "anthropic/claude-sonnet-5-5"


def test_hysteresis_route_stickiness(tmp_path: Path) -> None:
    """Active route is retained unless alternative has headroom advantage exceeding affinity margin."""
    state_file = tmp_path / "router_state.json"
    router = QuotaRouter(DEFAULT_ROUTES_PATH, state_file=state_file)

    # Both Anthropic and Gemini are eligible, but Anthropic was last_route
    usage_text = (FIXTURES_DIR / "omp_usage_real_scrubbed.txt").read_text(encoding="utf-8")

    # Pick without last_route chooses first candidate in preference order
    res1 = router.pick(
        candidates=["anthropic/claude-sonnet-5-5", "google-antigravity/gemini-3.8-flash"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert not res1.refused
    assert res1.route == "anthropic/claude-sonnet-5-5"

    # Now pass Gemini as last_route with affinity
    res2 = router.pick(
        candidates=["anthropic/claude-sonnet-5-5", "google-antigravity/gemini-3.8-flash"],
        need_worker_hours=1.0,
        usage_text=usage_text,
        last_route="google-antigravity/gemini-3.8-flash",
        hysteresis_affinity_pct=50.0,  # High switching hurdle
    )
    # Retains Gemini despite Anthropic being first in candidates
    assert res2.route == "google-antigravity/gemini-3.8-flash"
    assert "retained active route" in res2.reason


def test_pick_result_string_and_unpacking() -> None:
    """PickResult acts as string and unrolls as (route, reason)."""
    usage_text = (FIXTURES_DIR / "omp_usage_real_scrubbed.txt").read_text(encoding="utf-8")
    res = pick(
        candidates=["anthropic/claude-sonnet-5-5"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert isinstance(res, PickResult)
    assert res == "anthropic/claude-sonnet-5-5"
    assert res.ok

    # Tuple unpacking
    route, reason = res
    assert route == "anthropic/claude-sonnet-5-5"
    assert "selected candidate" in reason

    # Refusal unpacking
    bad_res = pick(
        candidates=["invalid-route"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert bad_res.refused
    refuse_str, refuse_reason = bad_res
    assert refuse_str.startswith("REFUSE")
    assert "not registered" in refuse_reason


def test_malformed_usage_refusal() -> None:
    """Malformed usage output fails safe and returns REFUSE."""
    usage_text = (FIXTURES_DIR / "omp_usage_malformed.txt").read_text(encoding="utf-8")
    res = pick(
        candidates=["anthropic/claude-sonnet-5-5"],
        need_worker_hours=1.0,
        usage_text=usage_text,
    )
    assert res.refused
    assert "telemetry down or malformed" in res.reason
