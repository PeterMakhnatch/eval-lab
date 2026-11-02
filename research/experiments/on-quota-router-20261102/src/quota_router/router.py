"""Quota-aware route selector with burn rate projection and hysteresis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    CandidateEvaluation,
    MetricGate,
    PickResult,
    PoolConfig,
    RouteConfig,
    UsageSnapshot,
)
from .parser import fetch_live_usage, parse_usage

DEFAULT_ROUTES_PATH = Path(__file__).resolve().parent.parent.parent / "routes.json"


class QuotaRouter:
    """Evaluates candidate routes against live quota usage and measured burn rates."""

    def __init__(
        self,
        routes_config: dict[str, Any] | Path | str | None = None,
        state_file: Path | str | None = None,
        hysteresis_band_pct: float | None = None,
    ):
        self.pools: dict[str, PoolConfig] = {}
        self.routes: dict[str, RouteConfig] = {}
        self.default_hysteresis_band_pct: float = 5.0
        self.state_file: Path | None = Path(state_file) if state_file else None
        self._state: dict[str, Any] = self._load_state()

        self._load_config(routes_config or DEFAULT_ROUTES_PATH)
        if hysteresis_band_pct is not None:
            self.default_hysteresis_band_pct = hysteresis_band_pct

    def _load_state(self) -> dict[str, Any]:
        """Load persistent router state (e.g. tripped routes, last selected route)."""
        if self.state_file and self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"tripped_routes": {}, "last_route": None}

    def _save_state(self) -> None:
        """Persist router state to disk."""
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:
            pass

    def _load_config(self, config_source: dict[str, Any] | Path | str) -> None:
        """Parse configuration mapping routes to pools and metrics."""
        data: dict[str, Any]
        if isinstance(config_source, dict):
            data = config_source
        else:
            p = Path(config_source)
            if not p.exists():
                raise FileNotFoundError(f"Routes config file not found: {p}")
            data = json.loads(p.read_text(encoding="utf-8"))

        self.default_hysteresis_band_pct = float(data.get("default_hysteresis_band_pct", 5.0))

        # Parse pools
        raw_pools = data.get("pools", {})
        for pool_id, pool_data in raw_pools.items():
            metrics: dict[str, MetricGate] = {}
            for m_key, m_data in pool_data.get("metrics", {}).items():
                metrics[m_key] = MetricGate(
                    metric_key=m_key,
                    label=m_data.get("label", m_key),
                    gate_percent=float(m_data.get("gate_percent", 80.0)),
                    measured_burn_pct_per_hour=float(m_data.get("measured_burn_pct_per_hour", 0.0)),
                    sample_size_hours=float(m_data.get("sample_size_hours", 0.0)),
                    sample_size_items=int(m_data.get("sample_size_items", 0)),
                    confidence=str(m_data.get("confidence", "low")),
                    reset_period_hours=float(m_data.get("reset_period_hours", 24.0)),
                    notes=str(m_data.get("notes", "")),
                )
            self.pools[pool_id] = PoolConfig(
                pool_id=pool_id,
                display_name=str(pool_data.get("display_name", pool_id)),
                gate_percent=float(pool_data.get("gate_percent", 80.0)),
                metrics=metrics,
            )

        # Parse routes
        raw_routes = data.get("routes", {})
        for route_id, route_data in raw_routes.items():
            self.routes[route_id] = RouteConfig(
                route_id=route_id,
                pool_id=str(route_data.get("pool", "")),
                model=str(route_data.get("model", "")),
                provider=str(route_data.get("provider", "")),
            )

    def evaluate_route(
        self,
        route_id: str,
        need_worker_hours: float,
        usage: UsageSnapshot,
        hysteresis_band_pct: float | None = None,
        imminent_reset_seconds: float = 300.0,
    ) -> CandidateEvaluation:
        """Evaluate a single route against current quota usage and projected burn.

        Checks:
        1. Whether route is registered in config.
        2. Whether telemetry exists for pool metrics.
        3. Current usage vs gate (including hysteresis if previously tripped).
        4. Projected usage = current + (burn_rate * hours) vs gate and 100% capacity.
        5. Reset timing: notes if quota reset is imminent.
        """
        band = hysteresis_band_pct if hysteresis_band_pct is not None else self.default_hysteresis_band_pct

        if route_id not in self.routes:
            return CandidateEvaluation(
                route=route_id,
                pool="unknown",
                eligible=False,
                refusal_reason=f"route '{route_id}' not registered in routes.json",
            )

        route_cfg = self.routes[route_id]
        pool_id = route_cfg.pool_id
        if pool_id not in self.pools:
            return CandidateEvaluation(
                route=route_id,
                pool=pool_id,
                eligible=False,
                refusal_reason=f"pool '{pool_id}' for route '{route_id}' not configured",
            )

        pool_cfg = self.pools[pool_id]
        if not pool_cfg.metrics:
            # Pool has no explicit metrics; check pool-level gate
            return CandidateEvaluation(
                route=route_id,
                pool=pool_id,
                eligible=True,
                headroom_pct=100.0 - pool_cfg.gate_percent,
            )

        # Check all metrics for this pool
        max_current_pct = 0.0
        max_projected_pct = 0.0
        limiting_metric: str | None = None
        min_headroom = 100.0
        min_reset_sec: float | None = None

        previously_tripped = self._state.get("tripped_routes", {}).get(route_id, False)

        for m_key, m_gate in pool_cfg.metrics.items():
            reading = usage.metrics.get(m_key)
            if reading is None:
                # Telemetry missing for this metric
                return CandidateEvaluation(
                    route=route_id,
                    pool=pool_id,
                    eligible=False,
                    refusal_reason=f"missing telemetry for metric '{m_key}' in usage output",
                )

            current_pct = reading.percent_used
            burn_pct = m_gate.measured_burn_pct_per_hour * need_worker_hours
            projected_pct = current_pct + burn_pct

            # Track maximums across metrics
            if current_pct > max_current_pct:
                max_current_pct = current_pct
            if projected_pct > max_projected_pct:
                max_projected_pct = projected_pct
                limiting_metric = m_key

            reset_sec = reading.resets_in_seconds
            if reset_sec is not None and (min_reset_sec is None or reset_sec < min_reset_sec):
                min_reset_sec = reset_sec

            # Calculate effective gate considering hysteresis
            # If route previously tripped, gate is lowered by hysteresis band (Schmitt trigger)
            effective_gate = m_gate.gate_percent
            if previously_tripped:
                effective_gate = max(0.0, m_gate.gate_percent - band)

            headroom = effective_gate - projected_pct
            if headroom < min_headroom:
                min_headroom = headroom

            reset_note = f" (resets in {reading.resets_in_str})" if reading.resets_in_str else ""

            # Check 1: Current usage exceeds gate
            if current_pct >= effective_gate:
                h_note = f" [hysteresis threshold {effective_gate:.1f}% applied]" if previously_tripped else ""
                self._state.setdefault("tripped_routes", {})[route_id] = True
                self._save_state()
                return CandidateEvaluation(
                    route=route_id,
                    pool=pool_id,
                    eligible=False,
                    refusal_reason=(
                        f"current usage {current_pct:.1f}% >= gate {effective_gate:.1f}% "
                        f"for {m_gate.label}{h_note}{reset_note}"
                    ),
                    current_max_pct=current_pct,
                    projected_max_pct=projected_pct,
                    limiting_metric=m_key,
                    resets_in_seconds=reset_sec,
                    headroom_pct=headroom,
                )

            # Check 2: Projected usage exceeds gate
            if projected_pct >= effective_gate:
                self._state.setdefault("tripped_routes", {})[route_id] = True
                self._save_state()
                return CandidateEvaluation(
                    route=route_id,
                    pool=pool_id,
                    eligible=False,
                    refusal_reason=(
                        f"projected usage {projected_pct:.1f}% >= gate {effective_gate:.1f}% "
                        f"({current_pct:.1f}% + {m_gate.measured_burn_pct_per_hour:.1f}%/h * {need_worker_hours:.2f}h) "
                        f"for {m_gate.label}{reset_note}"
                    ),
                    current_max_pct=current_pct,
                    projected_max_pct=projected_pct,
                    limiting_metric=m_key,
                    resets_in_seconds=reset_sec,
                    headroom_pct=headroom,
                )

            # Check 3: Projected usage exceeds absolute capacity (100%)
            if projected_pct > 100.0:
                self._state.setdefault("tripped_routes", {})[route_id] = True
                self._save_state()
                return CandidateEvaluation(
                    route=route_id,
                    pool=pool_id,
                    eligible=False,
                    refusal_reason=(
                        f"projected usage {projected_pct:.1f}% exceeds 100% capacity "
                        f"({current_pct:.1f}% + {m_gate.measured_burn_pct_per_hour:.1f}%/h * {need_worker_hours:.2f}h) "
                        f"for {m_gate.label}{reset_note}"
                    ),
                    current_max_pct=current_pct,
                    projected_max_pct=projected_pct,
                    limiting_metric=m_key,
                    resets_in_seconds=reset_sec,
                    headroom_pct=headroom,
                )

        # Route passed all checks; clear tripped state if below (gate - band)
        if previously_tripped and max_current_pct < (effective_gate - band):
            self._state.setdefault("tripped_routes", {})[route_id] = False
            self._save_state()

        return CandidateEvaluation(
            route=route_id,
            pool=pool_id,
            eligible=True,
            current_max_pct=max_current_pct,
            projected_max_pct=max_projected_pct,
            limiting_metric=limiting_metric,
            resets_in_seconds=min_reset_sec,
            headroom_pct=min_headroom,
        )

    def pick(
        self,
        candidates: list[str],
        need_worker_hours: float,
        usage_text: str | None = None,
        last_route: str | None = None,
        hysteresis_affinity_pct: float = 10.0,
    ) -> PickResult:
        """Select the best eligible candidate route or return REFUSE with reason.

        Args:
            candidates: Ordered list of candidate routes (in priority order).
            need_worker_hours: Planned duration in hours.
            usage_text: Raw `omp usage` text; if None, fetches live output.
            last_route: Previously active route for route stickiness (hysteresis).
            hysteresis_affinity_pct: Headroom advantage required to switch away from last_route.

        Returns:
            PickResult containing route string or 'REFUSE', unpackable as (route, reason).
        """
        if not candidates:
            return PickResult(
                "REFUSE",
                route=None,
                refused=True,
                reason="no candidate routes provided",
            )

        # Fetch / parse usage
        raw_text = usage_text if usage_text is not None else fetch_live_usage()
        usage = parse_usage(raw_text)

        # If usage text is completely empty or missing any recognized metrics
        if not usage.metrics and not usage.balances_usd:
            return PickResult(
                "REFUSE",
                route=None,
                refused=True,
                reason="unable to parse any quota metrics from usage telemetry (telemetry down or malformed)",
            )

        evaluations: dict[str, CandidateEvaluation] = {}
        eligible_candidates: list[CandidateEvaluation] = []
        refusal_reasons: list[str] = []

        for cand in candidates:
            ev = self.evaluate_route(cand, need_worker_hours, usage)
            evaluations[cand] = ev
            if ev.eligible:
                eligible_candidates.append(ev)
            else:
                refusal_reasons.append(f"{cand}: {ev.refusal_reason}")

        if not eligible_candidates:
            combined_reason = "; ".join(refusal_reasons)
            return PickResult(
                f"REFUSE: {combined_reason}",
                route=None,
                refused=True,
                reason=combined_reason,
                evaluations=evaluations,
            )

        # Determine active last_route for hysteresis affinity
        effective_last_route = last_route or self._state.get("last_route")

        # Check if last_route is among eligible candidates
        last_route_ev = next((ev for ev in eligible_candidates if ev.route == effective_last_route), None)

        chosen: CandidateEvaluation
        if last_route_ev is not None:
            # Hysteresis affinity: keep last_route unless another route has significantly higher headroom
            best_other = max(eligible_candidates, key=lambda ev: ev.headroom_pct)
            if best_other.headroom_pct - last_route_ev.headroom_pct > hysteresis_affinity_pct:
                chosen = best_other
                reason = (
                    f"switched from {effective_last_route} to {chosen.route}: "
                    f"headroom advantage {best_other.headroom_pct:.1f}% vs {last_route_ev.headroom_pct:.1f}% "
                    f"exceeds hysteresis affinity {hysteresis_affinity_pct:.1f}%"
                )
            else:
                chosen = last_route_ev
                reason = (
                    f"retained active route {chosen.route} via hysteresis affinity "
                    f"(headroom {chosen.headroom_pct:.1f}%)"
                )
        else:
            # Follow candidate priority order
            chosen = eligible_candidates[0]
            reason = (
                f"selected candidate {chosen.route} (projected {chosen.projected_max_pct:.1f}%, "
                f"headroom {chosen.headroom_pct:.1f}%)"
            )

        # Update persistent state
        self._state["last_route"] = chosen.route
        self._save_state()

        return PickResult(
            chosen.route,
            route=chosen.route,
            refused=False,
            reason=reason,
            evaluations=evaluations,
        )


def pick(
    candidates: list[str],
    need_worker_hours: float,
    usage_text: str | None = None,
    routes_config: dict[str, Any] | Path | str | None = None,
    last_route: str | None = None,
    state_file: Path | str | None = None,
) -> PickResult:
    """Functional convenience wrapper for `QuotaRouter.pick`."""
    router = QuotaRouter(routes_config=routes_config, state_file=state_file)
    return router.pick(
        candidates=candidates,
        need_worker_hours=need_worker_hours,
        usage_text=usage_text,
        last_route=last_route,
    )
