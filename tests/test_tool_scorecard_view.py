"""The trace-tool scorecard page reads retained producer files and invents nothing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evallab.production.tool_scorecard_view import ToolScorecardCache, build_tool_scorecard

GOODFIRE = {"positive": 10, "negative": 8, "unscored": 2}
CHEAT = {"positive": 6, "negative": 6, "unscored": 0}


def _row(tool: str, detector: str, population: dict[str, int], **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tool": tool,
        "detector": detector,
        "n_pos": population["positive"],
        "n_neg": population["negative"],
        "tp": 0,
        "fp": 0,
        "abstain_pos": 0,
        "abstain_neg": 0,
        "recall": 0.0,
        "far": 0.0,
        "recall_ci95": [0.0, 0.3],
        "far_ci95": [0.0, 0.3],
        "overlap": {"both": 0, "tool_only": 0, "cascade_only": 0, "neither": 0},
    }
    return {**row, **overrides}


def _label_set(population: dict[str, int], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "title": "cohort",
        "cascade_field": "alarm",
        "label_counts": {**population, "total": sum(population.values())},
        "rows": rows,
    }


def _detector(tp: int, fp: int, positives: int, negatives: int) -> dict[str, Any]:
    return {
        "tp": tp,
        "fp": fp,
        "positives": positives,
        "negatives": negatives,
        "recall": round(100 * tp / positives, 1),
        "far": round(100 * fp / negatives, 1),
        "recall_ci": [0.0, 100.0],
        "far_ci": [0.0, 100.0],
    }


def _atlas_scorecard(cheat: tuple[int, int] = (3, 1)) -> dict[str, Any]:
    def population(shape: dict[str, int], hits: dict[str, tuple[int, int]]) -> dict[str, Any]:
        return {
            "paired_detectors": {
                name: _detector(tp, fp, shape["positive"], shape["negative"])
                for name, (tp, fp) in hits.items()
            },
            "strata": {"positive": shape["positive"]},
        }

    goodfire = {"cascade": (2, 1), "suspicion>=2": (1, 0), "union": (3, 1)}
    return {
        "schema": "run-atlas-scorecard/v1",
        "headline": "suspicion>=2",
        "populations": {
            "goodfire_cm6": population(GOODFIRE, goodfire),
            "cheatbench_dev": population(CHEAT, {"cascade": (1, 1), "suspicion>=2": cheat}),
        },
    }


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")


def _lines(path: Path, rows: list[dict[str, Any]]) -> None:
    _write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _tree(root: Path) -> Path:
    tools = root / "trace-tools"
    docent = _row(
        "docent",
        "any_flag",
        GOODFIRE,
        tp=3,
        fp=1,
        abstain_pos=4,
        abstain_neg=5,
        recall=0.5,
        far=1 / 3,
        recall_ci95=[0.1, 0.9],
        far_ci95=[0.01, 0.8],
        overlap={"both": 1, "tool_only": 2, "cascade_only": 1, "neither": 1},
    )
    scout = _row(
        "scout",
        "any_flag",
        CHEAT,
        tp=3,
        fp=3,
        recall=0.5,
        far=0.5,
        recall_ci95=[0.1, 0.9],
        far_ci95=[0.1, 0.9],
    )
    _write(
        tools / "tt1-scorecard.json",
        {
            "schema": "evallab.trace_tool_scorecard/v1",
            "label_sets": {
                "goodfire_cm9_alarm": _label_set(GOODFIRE, [docent]),
                "cheatbench_dev_alarm": _label_set(CHEAT, [scout]),
            },
        },
    )
    _write(root / "run-atlas/scorecards/ra3-now.json", _atlas_scorecard(cheat=(3, 1)))
    _lines(
        tools / "spend.jsonl",
        [
            {
                "at": "2026-10-27T22:00:00+00:00",
                "tool": "docent",
                "route": "hosted",
                "runs": 100,
                "input_tokens": 2_000_000,
                "output_tokens": 10_000,
                "usd": 1.5,
            },
            {
                "at": "2026-10-27T23:00:00+00:00",
                "tool": "docent",
                "route": "deferred",
                "runs": 900,
                "input_tokens": 0,
                "output_tokens": 0,
                "usd": 0,
            },
        ],
    )
    _lines(
        tools / "results-v1.jsonl",
        [
            {"tool": "docent", "at": "2026-10-27T21:00:00+00:00"},
            {"tool": "docent", "at": "2026-10-28T01:00:00+00:00"},
            {"tool": "scout", "at": "2026-10-27T20:00:00+00:00"},
        ],
    )
    return root


def _tool(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in view["tools"] if item["tool"] == name)


def test_abstained_runs_are_not_misses_and_the_view_carries_counts_not_rates(
    tmp_path: Path,
) -> None:
    view = build_tool_scorecard(_tree(tmp_path))
    docent = _tool(view, "docent")["populations"]["goodfire_cm9"]
    assert (docent["tp"], docent["positives"], docent["positives_decided"]) == (3, 10, 6)
    assert (docent["fp"], docent["negatives"], docent["negatives_decided"]) == (1, 8, 3)
    assert docent["caught_beyond_cascade"] == 2 and docent["cascade_only_caught"] == 1
    assert "recall" not in docent and "far" not in docent
    assert _tool(view, "docent")["populations"]["cheatbench_dev"] is None
    assert view["populations"][0]["labels"] == {**GOODFIRE, "total": 20}


def test_facets_and_cascade_share_the_atlas_scorecard_and_facets_list_their_union(
    tmp_path: Path,
) -> None:
    view = build_tool_scorecard(_tree(tmp_path))
    facets = _tool(view, "facets")["populations"]["goodfire_cm9"]
    assert (facets["detector"], facets["tp"], facets["fp"]) == ("suspicion>=2", 1, 0)
    assert [row["detector"] for row in facets["details"]] == ["suspicion>=2", "union"]
    cascade = _tool(view, "cascade")["populations"]["cheatbench_dev"]
    assert (cascade["tp"], cascade["fp"], cascade["positives_decided"]) == (1, 1, 6)
    assert facets["recall_ci"] == [0.0, 1.0]


def test_cost_denominators_count_only_entries_that_recorded_usage(tmp_path: Path) -> None:
    usage = _tool(build_tool_scorecard(_tree(tmp_path)), "docent")["usage"]
    assert usage["entries"] == 2 and usage["usage_entries"] == 1
    # The 900-run deferral recorded nothing, so it must not dilute the 100 measured runs.
    assert usage["usage_runs"] == 100 and usage["usd"] == 1.5
    assert usage["routes"] == ["deferred", "hosted"]
    assert _tool(build_tool_scorecard(_tree(tmp_path)), "scout")["usage"] is None


def test_last_run_is_the_latest_record_and_names_its_source(tmp_path: Path) -> None:
    view = build_tool_scorecard(_tree(tmp_path))
    docent = _tool(view, "docent")
    assert (docent["last_run_at"], docent["last_run_source"]) == (
        "2026-10-28T01:00:00+00:00",
        "scan results",
    )
    _lines(
        tmp_path / "trace-tools/spend.jsonl",
        [
            {
                "at": "2026-10-29T00:00:00+00:00",
                "tool": "docent",
                "route": "r",
                "runs": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "usd": 0,
            }
        ],
    )
    assert _tool(build_tool_scorecard(tmp_path), "docent")["last_run_source"] == "spend ledger"


def test_an_inconsistent_row_is_rejected_and_its_neighbours_survive(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    document = json.loads((root / "trace-tools/tt1-scorecard.json").read_text())
    rows = document["label_sets"]["goodfire_cm9_alarm"]["rows"]
    rows.append(_row("laminar", "any_flag", GOODFIRE, tp=9, abstain_pos=5))  # 9 hits of 5 decided
    rows.append(_row("omp", "any_flag", CHEAT))  # counts disagree with this cohort
    _write(root / "trace-tools/tt1-scorecard.json", document)
    view = build_tool_scorecard(root)
    assert _tool(view, "laminar")["populations"]["goodfire_cm9"] is None
    assert _tool(view, "omp")["populations"]["goodfire_cm9"] is None
    assert _tool(view, "docent")["populations"]["goodfire_cm9"]["tp"] == 3
    source = next(item for item in view["sources"] if item["file"] == "tt1-scorecard.json")
    assert source["rejected_n"] == 2 and source["error"] is None
    assert any("hits exceed" in note for note in source["notes"])


def test_the_newest_valid_atlas_scorecard_is_used_and_a_broken_newer_one_is_reported(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    older = root / "run-atlas/scorecards/ra3-now.json"
    os.utime(older, (1_000_000, 1_000_000))
    _write(root / "run-atlas/scorecards/ra3-newest.json", _atlas_scorecard(cheat=(5, 0)))
    assert _tool(build_tool_scorecard(root), "facets")["populations"]["cheatbench_dev"]["tp"] == 5
    _write(root / "run-atlas/scorecards/ra3-newest.json", '{"schema": "run-atlas-scorecard/v1"')
    view = build_tool_scorecard(root)
    assert _tool(view, "facets")["populations"]["cheatbench_dev"]["tp"] == 3
    source = next(item for item in view["sources"] if item["file"] == "ra3-now.json")
    assert any("ra3-newest.json" in note for note in source["notes"])


def test_missing_sources_degrade_one_by_one_and_unmeasured_tools_stay_empty(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "trace-tools/results-v1.jsonl").unlink()
    (root / "run-atlas/scorecards/ra3-now.json").unlink()
    view = build_tool_scorecard(root)
    assert _tool(view, "docent")["populations"]["goodfire_cm9"]["tp"] == 3
    assert _tool(view, "docent")["last_run_source"] == "spend ledger"
    assert _tool(view, "facets")["populations"] == {"goodfire_cm9": None, "cheatbench_dev": None}
    harbor = _tool(view, "harbor_analyze")
    assert harbor["populations"] == {"goodfire_cm9": None, "cheatbench_dev": None}
    assert harbor["usage"] is None and harbor["last_run_at"] is None
    states = {item["file"]: item["error"] for item in view["sources"]}
    assert states["results-v1.jsonl"] == "not retained"
    assert states["tt1-scorecard.json"] is None


def test_a_symlinked_scorecard_is_refused_not_followed(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    real = tmp_path / "elsewhere.json"
    real.write_text((root / "trace-tools/tt1-scorecard.json").read_text())
    (root / "trace-tools/tt1-scorecard.json").unlink()
    (root / "trace-tools/tt1-scorecard.json").symlink_to(real)
    view = build_tool_scorecard(root)
    assert _tool(view, "docent")["populations"]["goodfire_cm9"] is None
    assert next(i for i in view["sources"] if i["file"] == "tt1-scorecard.json")["error"]


def test_pending_blinding_refresh_counts_runs_without_a_re_extracted_record(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root / "run-atlas/blinding-refresh-v1.json", {"run_ids": ["a", "b", "c"]})
    _lines(root / "run-atlas/reblinded-facets-v1.jsonl", [{"run_id": "a"}, {"run_id": "z"}])
    assert _tool(build_tool_scorecard(root), "facets")["pending_blinding_refresh"] == {
        "listed": 3,
        "pending": 2,
    }


def test_the_cache_rebuilds_only_when_a_retained_file_changes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    cache = ToolScorecardCache(root)
    first = cache.get()
    assert cache.get() is first
    _lines(
        root / "trace-tools/results-v1.jsonl",
        [{"tool": "scout", "at": "2026-10-30T00:00:00+00:00"}],
    )
    second = cache.get()
    assert second is not first
    assert _tool(second, "scout")["last_run_at"] == "2026-10-30T00:00:00+00:00"


def _scope_document(hits: int = 1) -> dict[str, Any]:
    return {
        "schema": "evallab.detector_scope/v1",
        "cells": [{
            "detector": "cascade", "cohort": "hackbench_natural", "measure": "exact_command",
            "hits": hits, "positives": 41, "route": "judge/route", "recorded_at": "2026-10-28T00:54:07+00:00",
            "date_basis": "freeze", "source": "receipt/union-command.json",
            "version": "window-cascade/v7", "note": "v7-to-v8 compatibility checked in the receipt",
        }],
    }


def _cascade_hackbench(view: dict[str, Any]) -> dict[str, Any]:
    row = next(item for item in view["where_it_works"]["rows"] if item["tool"] == "cascade")
    return row["cohorts"]["hackbench_natural"]


def test_the_where_each_detector_works_table_reads_the_scope_file_at_the_configured_path(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    absent = build_tool_scorecard(root)
    assert absent["where_it_works"]["present"] is False
    assert _cascade_hackbench(absent)["state"] == "absent"
    # The scorecard rows are still served from their own files.
    scout = next(row for row in absent["where_it_works"]["rows"] if row["tool"] == "scout")
    assert scout["cohorts"]["cheatbench_dev"]["hits"] == 3

    _write(root / "trace-tools/detector-scope-v1.json", _scope_document(1))
    default = build_tool_scorecard(root)
    assert default["where_it_works"]["present"] is True and _cascade_hackbench(default)["hits"] == 1
    assert next(item for item in default["sources"] if item["file"] == "detector-scope-v1.json")["error"] is None

    elsewhere = tmp_path / "elsewhere" / "receipt.json"
    _write(elsewhere, _scope_document(2))
    assert _cascade_hackbench(build_tool_scorecard(root, elsewhere))["hits"] == 2


def test_an_invalid_scope_file_blocks_only_its_own_cells(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    secret = "private-trace-identifier"
    _write(root / "trace-tools/detector-scope-v1.json", {**_scope_document(), secret: ["x"]})
    view = build_tool_scorecard(root)
    assert view["where_it_works"]["present"] is False and view["where_it_works"]["error"]
    assert _cascade_hackbench(view)["state"] == "absent"
    assert _tool(view, "docent")["populations"]["goodfire_cm9"]["tp"] == 3
    assert secret not in json.dumps(view)  # validation errors must not echo private field names


def test_the_cache_follows_the_scope_file(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    scope = root / "trace-tools/detector-scope-v1.json"
    _write(scope, _scope_document(1))
    cache = ToolScorecardCache(root)
    first = cache.get()
    assert cache.get() is first and _cascade_hackbench(first)["hits"] == 1
    _write(scope, _scope_document(3))
    os.utime(scope, ns=(1, 1))
    assert _cascade_hackbench(cache.get())["hits"] == 3
