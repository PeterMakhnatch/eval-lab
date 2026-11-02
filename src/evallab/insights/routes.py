"""Ordered route selection, with a plain explanation of every skipped route."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from evallab.insights.config import RouteSpec, model_family, provider_of


@dataclass(frozen=True)
class RouteChoice:
    route: str | None
    reason: str
    skipped: tuple[tuple[str, str], ...]


def choose_route(
    spec: RouteSpec,
    *,
    usage: Mapping[str, float | None],
    parked: Mapping[str, datetime],
    avoid_families: set[str],
    max_used_percent: float,
    now: datetime,
) -> RouteChoice:
    """Use full route keys so shared providers can expose separate quota buckets."""
    skipped: list[tuple[str, str]] = []
    for route in spec.routes:
        provider = provider_of(route)
        used = usage.get(route)
        if used is not None and used >= max_used_percent:
            skipped.append((route, f"Usage is {used:g}%, at or above {max_used_percent:g}%."))
            continue
        until = next(
            (parked[key] for key in (route, provider) if key in parked and parked[key] > now),
            None,
        )
        if until is not None:
            skipped.append((route, f"Parked until {until.isoformat()}."))
            continue
        if model_family(route) in avoid_families:
            skipped.append((route, "This model family must not judge its own inputs."))
            continue
        reason = "First available route."
        if used is None:
            reason += " Provider usage is unknown; this is allowed."
        return RouteChoice(route, reason, tuple(skipped))
    return RouteChoice(None, "All configured routes are blocked.", tuple(skipped))
