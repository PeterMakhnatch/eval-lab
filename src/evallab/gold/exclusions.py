"""Gold source exclusions; historical run and certify evidence remains readable."""
from __future__ import annotations

import csv
from functools import cache
from pathlib import Path


def _name(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


@cache
def _sources() -> dict[str, dict[str, str]]:
    with Path(__file__).with_name("exclusions.tsv").open(encoding="utf-8", newline="") as handle:
        entries: dict[str, dict[str, str]] = {}
        for row in csv.DictReader(handle, delimiter="\t"):
            for name in (row["name"], *row["aliases"].split(";")):
                if name.strip():
                    entries[_name(name)] = row
        return entries


def source_exclusion(source: str) -> dict[str, str] | None:
    """Return the reviewed exclusion, including its reason and raw custody path."""
    for prefix in ("staged:", "cross-test:", "failure-library:runs/"):
        if source.startswith(prefix):
            source = source.removeprefix(prefix)
            break
    return _sources().get(_name(source))
