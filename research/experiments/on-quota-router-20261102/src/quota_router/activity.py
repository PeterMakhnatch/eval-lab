"""Temporal-compatible activity for route quota checking."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .exceptions import RouteExhausted
from .models import UsageSnapshot
from .parser import fetch_live_usage, parse_usage
from .router import QuotaRouter


def route_quota_ok(
    route: str | None,
    worker_hours: float | str = 1.0,
    quota_file: str | Path | None = None,
    usage_text: str | None = None,
    routes_config: dict[str, Any] | Path | str | None = None,
    raise_on_refusal: bool = False,
) -> tuple[bool, str]:
    """Check if route has sufficient quota for planned worker hours.

    Compatible with both:
    1. Primary brief interface: `route_quota_ok(route, worker_hours)` -> `(bool, reason)`
    2. F3 activity stub interface: `route_quota_ok(quota_file, route)` -> `bool` (or raises `RouteExhausted`)

    Args:
        route: Target route name (e.g. 'devin/claude-sonnet-5-5') or quota_file in F3 style.
        worker_hours: Planned duration in hours (default 1.0), or route in F3 style.
        quota_file: Optional path to simulated quota JSON or routes config.
        usage_text: Optional raw `omp usage` text; if None, fetches live output.
        routes_config: Optional routes config dict or path.
        raise_on_refusal: If True, raises RouteExhausted instead of returning False.

    Returns:
        tuple[bool, str]: (is_ok, reason_string)
    """
    # Detect F3 calling convention: route_quota_ok(quota_file, route)
    actual_route: str
    actual_hours: float = 1.0
    actual_config: Any = routes_config

    if isinstance(worker_hours, str):
        # Called as route_quota_ok(quota_file, route)
        actual_route = worker_hours
        if route is not None:
            actual_config = route
    else:
        if route is None:
            if raise_on_refusal:
                raise RouteExhausted("No route specified")
            return False, "REFUSE: No route specified"
        actual_route = route
        actual_hours = float(worker_hours)

    if quota_file and not actual_config:
        actual_config = quota_file

    router = QuotaRouter(routes_config=actual_config)

    # Parse usage
    raw_usage = usage_text if usage_text is not None else fetch_live_usage()
    usage: UsageSnapshot = parse_usage(raw_usage)

    if not usage.metrics and not usage.balances_usd:
        reason = "REFUSE: unable to parse usage telemetry (telemetry down or malformed)"
        if raise_on_refusal:
            raise RouteExhausted(reason)
        return False, reason

    evaluation = router.evaluate_route(actual_route, actual_hours, usage)

    if evaluation.eligible:
        reason = (
            f"OK: route '{actual_route}' has sufficient quota for {actual_hours:.2f}h "
            f"(projected usage {evaluation.projected_max_pct:.1f}%, headroom {evaluation.headroom_pct:.1f}%)"
        )
        return True, reason

    reason = f"REFUSE: route '{actual_route}' quota exhausted: {evaluation.refusal_reason}"
    if raise_on_refusal:
        raise RouteExhausted(reason)
    return False, reason


async def route_quota_ok_async(
    route: str | None,
    worker_hours: float | str = 1.0,
    quota_file: str | Path | None = None,
    usage_text: str | None = None,
    routes_config: dict[str, Any] | Path | str | None = None,
    raise_on_refusal: bool = False,
) -> tuple[bool, str]:
    """Asynchronous wrapper for route_quota_ok."""
    return route_quota_ok(
        route=route,
        worker_hours=worker_hours,
        quota_file=quota_file,
        usage_text=usage_text,
        routes_config=routes_config,
        raise_on_refusal=raise_on_refusal,
    )


# Register as Temporal activity if temporalio is installed
try:
    from temporalio import activity

    route_quota_ok = activity.defn(name="route_quota_ok")(route_quota_ok)  # type: ignore[assignment]
    route_quota_ok_async = activity.defn(name="route_quota_ok_async")(route_quota_ok_async)  # type: ignore[assignment]
except ImportError:
    pass
