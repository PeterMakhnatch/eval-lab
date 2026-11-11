"""Scoring locks for labelled-only cohorts; empty studies do not register Gold sources."""
from __future__ import annotations

LABELLED_ONLY = "labelled-only, unscored"


def check_scoring(registry: dict[str, dict], selected: set[str], unlocked: set[str]) -> None:
    """Refuse locked cohorts before any prediction or trajectory access."""
    for source, value in registry.items():
        panel = f"action-v2/{source}"
        if ((source in selected or panel in selected)
                and value.get("scoring") == LABELLED_ONLY and source not in unlocked):
            raise ValueError(f"Cohort {source} is {LABELLED_ONLY}; requires --unlock-cohort {source}")
