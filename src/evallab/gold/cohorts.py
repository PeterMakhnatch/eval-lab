"""Prospective study registrations, including cohorts with no admitted rows."""
from __future__ import annotations

import json
from pathlib import Path

FRESH_SOURCE = "goodfire-dev-fresh-labels-v1"
LABELLED_ONLY = "labelled-only, unscored"
REGISTRATIONS = (Path("research/evidence/ev1110-gold-fresh/cohort.json"),)


def registrations(repo_root: Path) -> dict[str, dict]:
    result = {}
    for relative in REGISTRATIONS:
        path = repo_root / relative
        if path.is_file():
            value = json.loads(path.read_text())
            result[value["source"]] = value
    return result


def check_scoring(registry: dict[str, dict], selected: set[str], unlocked: set[str]) -> None:
    """Refuse locked cohorts before any prediction or trajectory access."""
    for source, value in registry.items():
        panel = f"action-v2/{source}"
        if ((source in selected or panel in selected)
                and value.get("scoring") == LABELLED_ONLY and source not in unlocked):
            raise ValueError(f"Cohort {source} is {LABELLED_ONLY}; requires --unlock-cohort {source}")
