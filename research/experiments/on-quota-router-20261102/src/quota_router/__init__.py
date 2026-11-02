"""Quota-aware route selector for overnight evaluation squads."""

from __future__ import annotations

from .activity import route_quota_ok, route_quota_ok_async
from .exceptions import (
    MalformedUsageError,
    QuotaRouterError,
    RouteExhausted,
    UnknownRouteError,
)
from .models import (
    CandidateEvaluation,
    MetricGate,
    MetricReading,
    PickResult,
    PoolConfig,
    RouteConfig,
    UsageSnapshot,
)
from .parser import (
    fetch_live_usage,
    parse_duration_to_seconds,
    parse_usage,
    scrub_usage_text,
    strip_ansi,
)
from .router import (
    DEFAULT_ROUTES_PATH,
    QuotaRouter,
    pick,
)

__all__ = [
    "DEFAULT_ROUTES_PATH",
    "CandidateEvaluation",
    "MalformedUsageError",
    "MetricGate",
    "MetricReading",
    "PickResult",
    "PoolConfig",
    "QuotaRouter",
    "QuotaRouterError",
    "RouteConfig",
    "RouteExhausted",
    "UnknownRouteError",
    "UsageSnapshot",
    "fetch_live_usage",
    "parse_duration_to_seconds",
    "parse_usage",
    "pick",
    "route_quota_ok",
    "route_quota_ok_async",
    "scrub_usage_text",
    "strip_ansi",
]
