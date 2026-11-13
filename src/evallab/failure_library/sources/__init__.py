"""Shared split normalization for failure-library sources."""

from __future__ import annotations

from evallab.failure_library.records import Split


def normalize_split(value: str) -> Split:
    """Preserve an existing key assignment, changing spelling rather than policy."""
    splits: dict[str, Split] = {
        "dev": "DEV",
        "test": "TEST",
        "heldout": "heldout",
        "cross_test": "cross_test",
        "unassigned": "unassigned",
    }
    try:
        return splits[value.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown source split: {value}") from exc
