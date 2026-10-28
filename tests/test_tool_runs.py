"""Per-tool run lists split admitted runs by what the tool recorded, never by a label."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production.task_evidence import ToolFindingIndex
from evallab.production.tool_runs import MAX_PAGE, RUN_LIST_TOOLS, tool_run_page


def _row(run: str, tool: str, detector: str, flag: bool | None, steps: list[int] | None = None) -> dict:
    return {
        "run_id": run,
        "tool": tool,
        "detector": detector,
        "flag": flag,
        "at": "2026-10-27T20:00:00+00:00",
        "evidence_steps": steps or [],
    }


def _index(tmp_path: Path, rows: list[dict[str, Any]]) -> ToolFindingIndex:
    path = tmp_path / "results-v1.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return ToolFindingIndex(path)


def _traces(*runs: str) -> dict[str, dict[str, Any]]:
    return {run: {"task_id": f"task-of-{run}"} for run in runs}


def _facet(suspicion: int) -> dict[str, Any]:
    return {"suspicion": suspicion, "evidence_steps": [4, 5]}


def _page(index: ToolFindingIndex, tool: str, kind: str, **extra: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "traces": _traces("a", "b", "c", "d", "e"),
        "shown": {},
        "facets_n": 0,
        "findings": index,
        "limit": 50,
        "offset": 0,
    } | extra
    return tool_run_page(tool, kind, **arguments)


def test_a_run_is_hit_read_clean_or_not_read_and_the_three_groups_cover_every_admitted_run(
    tmp_path: Path,
) -> None:
    index = _index(
        tmp_path,
        [
            _row("a", "scout", "d1", True, [2, 9]),
            _row("a", "scout", "d2", False),
            _row("b", "scout", "d1", False),
            _row("c", "scout", "d1", None),  # abstained: not read
            _row("d", "docent", "x", True),  # another tool read d, scout did not
            _row("elsewhere", "scout", "d1", True),  # not an admitted run
        ],
    )
    hit = _page(index, "scout", "hit")
    assert [(r["run_id"], r["task_id"]) for r in hit["runs"]] == [("a", "task-of-a")]
    assert hit["runs"][0]["detectors"] == [{"detector": "d1", "evidence_steps": [2, 9]}]
    counts = hit["counts"]
    assert (counts["hit_n"], counts["clean_n"], counts["unread_n"], counts["outside_n"]) == (1, 1, 3, 1)
    assert counts["hit_n"] + counts["clean_n"] + counts["unread_n"] == counts["admitted_n"] == 5
    assert [r["run_id"] for r in _page(index, "scout", "clean")["runs"]] == ["b"]
    unread = _page(index, "scout", "unread")
    assert [r["run_id"] for r in unread["runs"]] == ["c", "d", "e"]
    assert all(r["detectors"] == [] for r in unread["runs"])  # nothing was recorded for them


def test_hits_list_the_most_flagged_runs_first_and_pages_are_exact(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            _row("a", "scout", "d1", True),
            _row("b", "scout", "d1", True),
            _row("b", "scout", "d2", True),
            _row("c", "scout", "d1", True),
        ],
    )
    first = _page(index, "scout", "hit", limit=2)
    assert [r["run_id"] for r in first["runs"]] == ["b", "a"] and first["total"] == 3
    second = _page(index, "scout", "hit", limit=2, offset=2)
    assert [r["run_id"] for r in second["runs"]] == ["c"]
    assert _page(index, "scout", "hit", limit=2, offset=9)["runs"] == []


def test_the_facet_reader_is_split_on_stored_suspicion_and_a_missing_facet_is_not_read(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path, [])
    shown = {"a": _facet(3), "b": _facet(2), "c": _facet(1), "d": _facet(0)}
    hit = _page(index, "facets", "hit", shown=shown, facets_n=6)
    assert [r["run_id"] for r in hit["runs"]] == ["a", "b"]
    assert hit["runs"][0]["detectors"] == [{"detector": "suspicion>=2", "evidence_steps": [4, 5]}]
    assert all(r["faceted"] for r in hit["runs"])
    counts = hit["counts"]
    assert (counts["hit_n"], counts["clean_n"], counts["unread_n"], counts["outside_n"]) == (2, 2, 1, 2)
    unread = _page(index, "facets", "unread", shown=shown, facets_n=6)
    assert [(r["run_id"], r["faceted"]) for r in unread["runs"]] == [("e", False)]
    assert hit["results_error"] is None  # the facet reader does not depend on the results file


def test_rows_carry_ids_only_never_a_label_reward_or_outcome(tmp_path: Path) -> None:
    index = _index(tmp_path, [_row("a", "scout", "d1", True)])
    traces = {"a": {"task_id": "t", "outcome": "pass", "reward": 1.0, "step_count": 9}}
    page = tool_run_page(
        "scout", "hit", traces=traces, shown={}, facets_n=0, findings=index, limit=5, offset=0
    )
    assert set(page["runs"][0]) == {"run_id", "task_id", "faceted", "detectors"}
    blob = json.dumps(page)
    assert "outcome" not in blob and "reward" not in blob and '"pass"' not in blob


def test_a_missing_results_file_lists_everything_as_not_read_and_says_why(tmp_path: Path) -> None:
    index = ToolFindingIndex(tmp_path / "absent.jsonl")
    page = _page(index, "scout", "unread")
    assert page["counts"]["unread_n"] == 5 and page["counts"]["hit_n"] == 0
    assert page["results_error"] == "not retained"


def test_unsupported_tools_kinds_and_pages_are_refused(tmp_path: Path) -> None:
    index = _index(tmp_path, [])
    for tool in ("cascade", "harbor_analyze", "nope"):
        with pytest.raises(ValueError, match="no per-run list"):
            _page(index, tool, "hit")
    with pytest.raises(ValueError, match="kind"):
        _page(index, "scout", "missed")
    for bad in ({"limit": 0}, {"limit": MAX_PAGE + 1}, {"offset": -1}):
        with pytest.raises(ValueError, match="limit"):
            _page(index, "scout", "hit", **bad)
    assert set(RUN_LIST_TOOLS) == {"facets", "laminar", "docent", "scout", "scout_metr", "omp"}
