"""Run Atlas coverage: what is stored versus what could be, and what is not yet ready.

A read-only projection of the files the atlas already validates plus the blinding-refresh
files (``blinding-refresh-v1.json`` lists runs whose first extraction used an incomplete
source-name redaction; ``reblinded-facets-v1.jsonl`` holds their re-extraction). Nothing
here opens a model, writes, or joins a label. Per population (dataset and trust tier of the
*admitted DEV* runs in the cloud database) it reports admitted runs, runs with a stored
facet, and the facet version, facet model and blinding state of every stored record.

Blinding states of a stored facet:

* ``refreshed`` -- listed for refresh and the stored record is the re-extraction;
* ``pending`` -- listed for refresh and the stored record is still the earlier extraction;
* ``unlisted`` -- not on the refresh list (this is not a claim that it was never leaky);
* ``unknown`` -- no readable refresh list, so no state can be stated.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.production.atlas_view import (
    AtlasStore,
    json_reason,
    read_regular,
    strict_loads,
)

SCHEMA = "evallab.atlas_coverage/v1"
REFRESH_FILE = "blinding-refresh-v1.json"
REBLINDED_FILE = "reblinded-facets-v1.jsonl"
MAX_REFRESH_BYTES = 8 * 1024 * 1024
MAX_REBLINDED_BYTES = 128 * 1024 * 1024
# The fields that decide whether a stored record *is* the re-extraction.
_COMPARED = ("facet_model", "goal", "approach", "shortcut", "suspicion", "evidence_steps")
STATES = ("refreshed", "pending", "unlisted", "unknown")


@dataclass(frozen=True)
class RefreshFiles:
    """The refresh list and the re-extracted records, or why they are unreadable."""

    listed: frozenset[str] | None = None
    reblinded: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    rejected_n: int = 0
    error: str | None = None


def _signature(directory: Path) -> tuple[Any, ...]:
    found: list[tuple[int, int, int] | None] = []
    for name in (REFRESH_FILE, REBLINDED_FILE):
        try:
            seen = (directory / name).lstat()
        except OSError:
            found.append(None)
        else:
            found.append((seen.st_mtime_ns, seen.st_size, seen.st_ino))
    return tuple(found)


def load_refresh_files(directory: Path) -> RefreshFiles:
    """Validate the refresh list and the re-extracted records; never raises on bad data."""
    try:
        raw = read_regular(directory, REFRESH_FILE, MAX_REFRESH_BYTES)
        if raw is None:
            return RefreshFiles(error="no blinding refresh list retained")
        document = strict_loads(raw.decode("utf-8"))
        if document.get("schema") != "run-atlas-blinding-refresh/v1":
            return RefreshFiles(error="blinding refresh list has an unknown schema")
        listed = frozenset(str(item) for item in document["run_ids"])
        reblinded: dict[str, tuple[Any, ...]] = {}
        rejected = 0
        body = read_regular(directory, REBLINDED_FILE, MAX_REBLINDED_BYTES)
        for line in (body or b"").decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = strict_loads(line)
                reblinded[str(record["run_id"])] = tuple(
                    tuple(value) if isinstance(value, list) else value
                    for value in (record[name] for name in _COMPARED)
                )
            except (ValueError, KeyError, TypeError):
                rejected += 1
        return RefreshFiles(listed, reblinded, rejected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as error:
        return RefreshFiles(error=json_reason(error))


class RefreshCache:
    """Re-reads the refresh files only when either changes on disk."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._value = RefreshFiles()

    def get(self) -> RefreshFiles:
        with self._lock:
            key = _signature(self._directory)
            if key != self._key:
                self._key, self._value = key, load_refresh_files(self._directory)
            return self._value


def blinding_states(
    refresh: RefreshFiles, facets: Mapping[str, Mapping[str, Any]]
) -> dict[str, str]:
    """The blinding state of every stored facet in ``facets``."""
    if refresh.listed is None:
        return dict.fromkeys(facets, "unknown")
    states: dict[str, str] = {}
    for run_id, facet in facets.items():
        if run_id not in refresh.listed:
            states[run_id] = "unlisted"
            continue
        stored = tuple(
            tuple(value) if isinstance(value, (list, tuple, frozenset)) else value
            for value in (facet.get(name) for name in _COMPARED)
        )
        states[run_id] = "refreshed" if refresh.reblinded.get(run_id) == stored else "pending"
    return states


def build_coverage(
    store: AtlasStore,
    traces: Mapping[str, Mapping[str, Any]],
    shown: Mapping[str, Mapping[str, Any]],
    withheld: Mapping[str, int],
    refresh: RefreshFiles,
) -> dict[str, Any]:
    """Per-population stored versus admitted runs, with version and blinding of each record."""
    states = blinding_states(refresh, shown)
    populations: dict[tuple[str, str], dict[str, Any]] = {}
    for run_id, identity in traces.items():
        key = (identity["dataset_id"], identity["trust_tier"])
        row = populations.setdefault(
            key,
            {
                "dataset_id": key[0],
                "trust_tier": key[1],
                "admitted_n": 0,
                "stored_n": 0,
                "facet_versions": Counter(),
                "facet_models": Counter(),
                "blinding": Counter(),
            },
        )
        row["admitted_n"] += 1
        facet = shown.get(run_id)
        if facet is not None:
            row["stored_n"] += 1
            row["facet_versions"][facet["facet_version"]] += 1
            row["facet_models"][facet["facet_model"]] += 1
            row["blinding"][states[run_id]] += 1
    rows = [
        {
            **row,
            "facet_versions": dict(sorted(row["facet_versions"].items())),
            "facet_models": dict(sorted(row["facet_models"].items())),
            "blinding": {state: row["blinding"].get(state, 0) for state in STATES},
        }
        for _, row in sorted(populations.items())
    ]
    pending = sum(1 for state in states.values() if state == "pending")
    return {
        "schema": SCHEMA,
        "populations": rows,
        "totals": {
            "admitted_n": len(traces),
            "stored_n": len(shown),
            "not_stored_n": len(traces) - len(shown),
            "pending_blinding_n": pending,
            "refreshed_n": sum(1 for state in states.values() if state == "refreshed"),
            "facets_read_n": len(store.facets),
            "unadmitted_n": withheld.get("unadmitted_n", 0),
            "evidence_outside_run_n": withheld.get("evidence_outside_run_n", 0),
            "rejected_n": store.rejected_n,
            "superseded_n": store.superseded_n,
            "refresh_listed_n": None if refresh.listed is None else len(refresh.listed),
            "refresh_rejected_n": refresh.rejected_n,
        },
        "refresh_error": refresh.error,
        "notice": (
            "Admitted runs are the DEV runs in the cloud database. A run without a stored facet "
            "is not ready: it has no leads here yet, which is not the same as being clean. "
            "Versions and blinding states describe the stored records; nothing was recomputed."
        ),
    }
