"""Temp-only locator resolution; synthetic rows, no retained corpus or model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.resolve import locator, project_runs, select_field
from evallab.failure_library.records import RunRecord, TaskRef
from evallab.failure_library.runs.io import reference


def _run(ref: str, run_id: str = "hacktrace:one") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        collection="hacktrace",
        outcome="unknown",
        transcript_ref=ref,
        task=TaskRef(source_id="synthetic", task_id="task", upstream_repo="synthetic"),
    )


def _messages(text: str) -> list[dict]:
    return [{"role": "user", "content": "task"}, {"role": "assistant", "content": text}]


def test_jsonl_line_locator_reads_the_exact_one_based_row_once(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(
        "".join(
            json.dumps({"messages": _messages(f"answer {i}"), "label": "excluded"}) + "\n"
            for i in range(3)
        )
    )
    runs = [
        _run(reference(path, index, jsonl=True, field="messages"), f"hacktrace:{index}")
        for index in (2, 0)
    ]
    results = dict((run.run_id, result) for run, result in project_runs(runs))
    assert results["hacktrace:2"].doc["steps"][1]["message"] == "answer 2"
    assert results["hacktrace:0"].doc["steps"][1]["message"] == "answer 0"
    assert "excluded" not in json.dumps(results["hacktrace:2"].doc)


def test_missing_remote_or_absent_rows_are_unavailable_not_empty(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(json.dumps({"messages": _messages("x")}) + "\n")
    runs = [
        _run("https://example.invalid/data.parquet#row=0&field=messages", "hacktrace:remote"),
        _run(reference(path, 5, jsonl=True, field="messages"), "hacktrace:absent"),
        _run(reference(path, 0, jsonl=True, field="nodes"), "hacktrace:field"),
        _run((tmp_path / "gone.jsonl").as_uri() + "#line=1", "hacktrace:file"),
    ]
    results = dict((run.run_id, result) for run, result in project_runs(runs))
    assert all(isinstance(result, ActorUnavailable) for result in results.values())
    assert "local" in str(results["hacktrace:remote"])


def test_field_paths_traverse_indices_and_decode_serialized_messages() -> None:
    row = {"traces": [{"nodes": [1]}], "messages": json.dumps(_messages("y"))}
    assert select_field(row, "traces[0]") == {"nodes": [1]}
    assert select_field(row, "messages")[1]["content"] == "y"
    with pytest.raises(ActorUnavailable):
        select_field(row, "traces[1]")
    with pytest.raises(ActorUnavailable):
        locator("file:///x.jsonl#line=1&row=2")
