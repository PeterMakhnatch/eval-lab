"""The Run Atlas first screen: controls reach the run picker and say what a detector recorded.

The page's own JavaScript (which option a link carries, which cell text a row shows) has no
runtime harness here: the dashboard ships as one classic script and the suite has no browser
or JS engine. These checks exercise ranking, filter counts and retained-result boundaries,
alongside the nesting and document order of the served page. They do not claim to execute
client-side link generation or cell rendering.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from test_modal_cloud import (
    AUTH,
    PROVISIONAL,
    VERIFIED,
    _atlas_facet,
    _harness_finding,
    _task,
    _trace,
    _write_atlas,
)
from test_modal_cloud import client as client
from test_modal_cloud import isolated_environment as isolated_environment
from test_modal_cloud import modal_cloud as modal_cloud
from test_modal_cloud import records as records
from test_modal_cloud import repo_root as repo_root
from test_modal_cloud import runtime as runtime
from test_production_dashboard_browser import atlas_outline

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from evallab.production.modal_cloud import CloudRuntime

URL = "/api/cloud-db/atlas"


def _listing(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get(URL, headers=AUTH, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _rows(listing: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {run["run_id"]: run for run in listing["runs"]}


def _write_tool_results(runtime: CloudRuntime, *rows: dict[str, Any]) -> None:
    directory = runtime.data_root / "trace-tools"
    directory.mkdir(exist_ok=True)
    (directory / "results-v1.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _result(run_id: str, tool: str = "scout", flag: bool | None = True) -> dict[str, Any]:
    return {
        "run_id": run_id, "tool": tool, "detector": "repeated", "flag": flag,
        "at": "2026-10-27T22:00:00+00:00", "evidence_steps": [2] if flag else [],
    }


def test_each_page_order_changes_the_ranking_and_matches_the_links_unsorted_default(
    client: TestClient, runtime: CloudRuntime, records: dict[str, Any]
) -> None:
    outline = atlas_outline()
    orders = [
        attrs["value"]
        for index, (tag, attrs, _) in enumerate(outline.elements)
        if tag == "option" and outline.within(index, "atlas-sort") is not None
    ]
    _write_atlas(runtime, [_atlas_facet(VERIFIED, suspicion=1), _atlas_facet(PROVISIONAL, suspicion=3)])
    _write_tool_results(runtime, _result(VERIFIED))

    def ranked(**params: str) -> list[str]:
        return [run["run_id"] for run in _listing(client, **params)["runs"]]

    by_order = {value: ranked(sort=value) for value in orders}
    assert by_order[orders[0]] == ranked()  # a link that omits sort lands on the page's first choice
    assert len({tuple(runs) for runs in by_order.values()}) == len(orders) > 1


def test_detector_alarm_options_count_exactly_the_runs_the_filter_returns(
    client: TestClient, runtime: CloudRuntime, records: dict[str, Any]
) -> None:
    _write_atlas(
        runtime,
        [
            _atlas_facet(VERIFIED, suspicion=1),
            _atlas_facet(PROVISIONAL, suspicion=3),
            _atlas_facet("trace-never-read", suspicion=2),
        ],
    )
    _write_tool_results(runtime, _result(VERIFIED), _result(PROVISIONAL, flag=False))
    findings = runtime.data_root / "trace-harness"
    findings.mkdir()
    (findings / "findings-v1.jsonl").write_text(
        json.dumps(_harness_finding(VERIFIED, detector="run_health.loop", detector_version="rh/v1",
                                    reader_model=None, kind="loop")) + "\n",
        encoding="utf-8",
    )
    counts = _listing(client)["options"]["alarmed_by"]
    assert counts
    for name, count in counts.items():
        picked = _listing(client, alarmed_by=name)
        assert picked["total"] == count
        assert all(name in run["alarms"] for run in picked["runs"])


def test_unread_unknown_and_read_clean_runs_are_three_distinct_states(
    client: TestClient, runtime: CloudRuntime, records: dict[str, Any]
) -> None:
    _write_atlas(
        runtime,
        [_atlas_facet(VERIFIED, suspicion=1), _atlas_facet(PROVISIONAL, suspicion=3)],
    )
    nothing = _listing(client)
    assert nothing["alarms_available"] is False  # nothing retained: every row is unknown, not clean
    assert not any(run["alarms"] for run in nothing["runs"])

    task = _task("first-screen-task", "Synthetic task.")
    runtime.database.upsert_task(task)
    runtime.database.upsert_trace(
        {**_trace(VERIFIED, task["task_id"], None), "step_count": 1},
        steps=[{"step_index": 0, "role": "assistant", "content_text": "Synthetic evidence."}],
    )
    _write_tool_results(runtime, _result(VERIFIED, flag=False))  # read clean
    rows = _rows(_listing(client))
    assert rows[VERIFIED]["alarms"] == [] and rows[VERIFIED]["detector_results_n"] > 0  # read, no alarm
    assert rows[VERIFIED]["actor_inputs_available"] is True
    assert rows[PROVISIONAL]["alarms"] == [] and rows[PROVISIONAL]["detector_results_n"] == 0  # not read
    assert rows[PROVISIONAL]["actor_inputs_available"] is False  # nothing to read: unknown, not clean

    _write_tool_results(runtime, _result(VERIFIED, flag=None))  # abstained, not a clean read
    rows = _rows(_listing(client))
    assert rows[VERIFIED]["alarms"] == [] and rows[VERIFIED]["detector_results_n"] > 0


def test_only_harness_findings_still_say_detectors_were_retained(
    client: TestClient, runtime: CloudRuntime, records: dict[str, Any]
) -> None:
    _write_atlas(runtime, [_atlas_facet(VERIFIED, suspicion=1), _atlas_facet(PROVISIONAL, suspicion=3)])
    findings = runtime.data_root / "trace-harness"
    findings.mkdir()
    (findings / "findings-v1.jsonl").write_text(
        json.dumps(_harness_finding(PROVISIONAL, verdict="clear")) + "\n", encoding="utf-8"
    )
    listing = _listing(client)
    assert listing["alarms_available"] is True
    assert _rows(listing)[VERIFIED]["alarms"] == [] and _rows(listing)[VERIFIED]["detector_results_n"] == 0
    assert _rows(listing)[PROVISIONAL]["detector_results_n"] > 0  # recorded, not proof that an actor transcript was read


def test_an_unreadable_alarm_source_degrades_instead_of_failing_the_atlas(
    client: TestClient, runtime: CloudRuntime, records: dict[str, Any]
) -> None:
    _write_atlas(runtime, [_atlas_facet(VERIFIED, suspicion=1)])
    (runtime.data_root / "trace-tools").mkdir(parents=True, exist_ok=True)
    (runtime.data_root / "trace-tools" / "results-v1.jsonl").mkdir()  # unreadable, not absent
    listing = _listing(client)
    assert listing["alarms_available"] is False
    assert _rows(listing)[VERIFIED]["alarms"] == []
