"""Data models for quota-aware route selector."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricReading:
    """Reading of a specific quota metric from `omp usage`."""

    metric_key: str
    name: str
    percent_used: float
    resets_in_str: str | None = None
    resets_in_seconds: float | None = None


@dataclass
class UsageSnapshot:
    """Parsed state of all quota metrics from `omp usage`."""

    metrics: dict[str, MetricReading] = field(default_factory=dict)
    balances_usd: dict[str, float] = field(default_factory=dict)
    raw_text: str = ""

    def get_percent(self, key: str) -> float | None:
        """Get percent used for a metric key if present."""
        m = self.metrics.get(key)
        return m.percent_used if m else None

    def get_resets_in_seconds(self, key: str) -> float | None:
        """Get reset countdown in seconds for a metric key if present."""
        m = self.metrics.get(key)
        return m.resets_in_seconds if m else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to simple dict (compatible with on_watch usage_snapshot)."""
        return {
            k: v.percent_used for k, v in self.metrics.items()
        }


@dataclass(frozen=True)
class MetricGate:
    """Gate and burn parameters for a metric."""

    metric_key: str
    label: str
    gate_percent: float
    measured_burn_pct_per_hour: float
    sample_size_hours: float = 0.0
    sample_size_items: int = 0
    confidence: str = "low"
    reset_period_hours: float = 24.0
    notes: str = ""


@dataclass(frozen=True)
class PoolConfig:
    """Quota pool configuration."""

    pool_id: str
    display_name: str
    gate_percent: float
    metrics: dict[str, MetricGate] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteConfig:
    """Route configuration mapping route identifier to pool and model."""

    route_id: str
    pool_id: str
    model: str
    provider: str


@dataclass
class CandidateEvaluation:
    """Evaluation result for a candidate route."""

    route: str
    pool: str
    eligible: bool
    refusal_reason: str | None = None
    current_max_pct: float = 0.0
    projected_max_pct: float = 0.0
    limiting_metric: str | None = None
    resets_in_seconds: float | None = None
    headroom_pct: float = 0.0


class PickResult(str):
    """Result of route selection.

    Acts as a string containing the selected route (or 'REFUSE'/'REFUSE: <reason>'),
    while also providing structured attributes and tuple unpacking:
        route, reason = pick(...)
    """

    def __new__(cls, content: str, route: str | None, refused: bool, reason: str, evaluations: dict[str, CandidateEvaluation] | None = None):
        obj = super().__new__(cls, content)
        obj.route = route
        obj.refused = refused
        obj.reason = reason
        obj.evaluations = evaluations or {}
        return obj

    route: str | None
    refused: bool
    reason: str
    evaluations: dict[str, CandidateEvaluation]

    @property
    def ok(self) -> bool:
        return not self.refused

    def __iter__(self):
        """Allow unpacking: `route_or_refuse, reason = pick(...)`."""
        yield str(self)
        yield self.reason
