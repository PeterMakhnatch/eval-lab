"""Atlas coverage states what is stored and what is not ready, from the retained files only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evallab.production.atlas_coverage import (
    REBLINDED_FILE,
    REFRESH_FILE,
    RefreshCache,
    RefreshFiles,
    blinding_states,
    build_coverage,
    load_refresh_files,
)
from evallab.production.atlas_view import AtlasStore


def _facet(run_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "facet_version": "run-facets/v1",
        "facet_model": "p/m",
        "goal": "g",
        "approach": "a",
        "shortcut": "none",
        "suspicion": 0,
        "evidence_steps": [0, 3],
    } | fields


def _write(directory: Path, listed: list[str], reblinded: list[dict[str, Any]], extra: str = "") -> None:
    directory.mkdir(exist_ok=True)
    (directory / REFRESH_FILE).write_text(
        json.dumps({"schema": "run-atlas-blinding-refresh/v1", "run_ids": listed}), encoding="utf-8"
    )
    (directory / REBLINDED_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in reblinded) + extra, encoding="utf-8"
    )


def test_a_listed_run_is_refreshed_only_when_the_stored_record_is_the_reextraction(
    tmp_path: Path,
) -> None:
    redone = _facet("redone", goal="blinded goal")
    _write(
        tmp_path,
        ["redone", "stale", "gone"],
        [redone, _facet("stale", goal="blinded goal"), _facet("gone")],
    )
    stored = {
        "redone": _facet("redone", goal="blinded goal"),
        "stale": _facet("stale", goal="leaky original goal"),  # still the earlier extraction
        "other": _facet("other"),
    }
    states = blinding_states(load_refresh_files(tmp_path), stored)
    assert states == {"redone": "refreshed", "stale": "pending", "other": "unlisted"}
    # tuples, lists and frozensets of evidence steps compare equal
    assert blinding_states(
        load_refresh_files(tmp_path), {"redone": _facet("redone", goal="blinded goal", evidence_steps=(0, 3))}
    ) == {"redone": "refreshed"}


def test_without_a_readable_refresh_list_no_state_is_claimed(tmp_path: Path) -> None:
    missing = load_refresh_files(tmp_path)
    assert missing.listed is None and "no blinding refresh list" in (missing.error or "")
    assert blinding_states(missing, {"r": _facet("r")}) == {"r": "unknown"}
    (tmp_path / REFRESH_FILE).write_text(json.dumps({"schema": "other", "run_ids": []}), encoding="utf-8")
    assert "unknown schema" in (load_refresh_files(tmp_path).error or "")
    (tmp_path / REFRESH_FILE).write_text("{not json", encoding="utf-8")
    assert load_refresh_files(tmp_path).error


def test_bad_reextraction_lines_are_counted_and_a_symlinked_list_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, ["a"], [_facet("a")], extra='{"run_id": "b"}\nnot json\n')
    files = load_refresh_files(tmp_path)
    assert files.error is None and files.rejected_n == 2 and set(files.reblinded) == {"a"}
    real = tmp_path / "real.json"
    real.write_text((tmp_path / REFRESH_FILE).read_text(), encoding="utf-8")
    (tmp_path / REFRESH_FILE).unlink()
    os.symlink(real, tmp_path / REFRESH_FILE)
    assert load_refresh_files(tmp_path).error


def test_the_refresh_files_are_reread_only_when_they_change(tmp_path: Path) -> None:
    cache = RefreshCache(tmp_path)
    assert cache.get().listed is None
    _write(tmp_path, ["a"], [])
    first = cache.get()
    assert first.listed == {"a"} and cache.get() is first
    _write(tmp_path, ["a", "b"], [])
    assert cache.get().listed == {"a", "b"}


def _identity(dataset: str, tier: str) -> dict[str, Any]:
    return {"dataset_id": dataset, "trust_tier": tier}


def test_coverage_counts_stored_against_admitted_per_population_and_names_what_is_not_ready() -> None:
    traces = {
        "g1": _identity("goodfire", "tier2"),
        "g2": _identity("goodfire", "tier2"),
        "g3": _identity("goodfire", "tier2"),
        "m1": _identity("mimo", "tier1"),
        "t1": _identity("tb4", "tier1"),
    }
    shown = {
        "g1": _facet("g1"),
        "g2": _facet("g2", facet_model="other/m"),
        "m1": _facet("m1", facet_version="run-facets/v2"),
    }
    store = AtlasStore(facets={**shown, "ghost": _facet("ghost")}, rejected_n=4, superseded_n=2)
    refresh = RefreshFiles(frozenset({"g1", "g2"}), {"g1": ("p/m", "g", "a", "none", 0, (0, 3))})
    coverage = build_coverage(
        store, traces, shown, {"unadmitted_n": 1, "evidence_outside_run_n": 0}, refresh
    )
    rows = {(row["dataset_id"], row["trust_tier"]): row for row in coverage["populations"]}
    goodfire = rows[("goodfire", "tier2")]
    assert (goodfire["admitted_n"], goodfire["stored_n"]) == (3, 2)
    assert goodfire["facet_models"] == {"other/m": 1, "p/m": 1}
    assert goodfire["blinding"] == {"refreshed": 1, "pending": 1, "unlisted": 0, "unknown": 0}
    assert goodfire["mixed_readers"] is True
    mimo = rows[("mimo", "tier1")]
    assert (mimo["admitted_n"], mimo["stored_n"]) == (1, 1) and mimo["facet_versions"] == {"run-facets/v2": 1}
    assert mimo["mixed_readers"] is False
    tb4 = rows[("tb4", "tier1")]  # admitted, nothing stored: no reader, not a mix
    assert (tb4["stored_n"], tb4["facet_models"], tb4["mixed_readers"]) == (0, {}, False)
    assert mimo["blinding"]["unlisted"] == 1
    totals = coverage["totals"]
    assert (totals["admitted_n"], totals["stored_n"], totals["not_stored_n"]) == (5, 3, 2)
    assert (totals["pending_blinding_n"], totals["refreshed_n"]) == (1, 1)
    assert (totals["facets_read_n"], totals["unadmitted_n"], totals["rejected_n"]) == (4, 1, 4)
    assert totals["refresh_listed_n"] == 2


def test_an_unreadable_refresh_list_is_reported_not_counted_as_zero_pending() -> None:
    store = AtlasStore(facets={"r": _facet("r")})
    coverage = build_coverage(
        store, {"r": _identity("d", "t")}, {"r": _facet("r")}, {}, RefreshFiles(error="no list")
    )
    assert coverage["refresh_error"] == "no list"
    assert coverage["totals"]["refresh_listed_n"] is None
    assert coverage["populations"][0]["blinding"]["unknown"] == 1
