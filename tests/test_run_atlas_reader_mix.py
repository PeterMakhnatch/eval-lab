"""``evallab atlas coverage`` in a fresh process against a synthetic trace DB and store."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

HAIKU = "anthropic/claude-haiku-5-5"
LUNA = "openai-codex/gpt-6.1-luna"
SONNET = "anthropic/claude-sonnet-5-5"
V2 = "run-atlas-prompt/v2"

SCHEMA = """
CREATE TABLE corpus_tasks (
    task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, split TEXT NOT NULL,
    instruction_text TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE agent_traces (
    trace_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES corpus_tasks(task_id),
    step_count INTEGER NOT NULL CHECK(step_count >= 0)
);
CREATE TABLE trace_steps (
    trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id), step_index INTEGER NOT NULL,
    role TEXT NOT NULL, content_text TEXT, flags_json TEXT NOT NULL,
    PRIMARY KEY(trace_id, step_index)
);
"""

# trace_id -> (task_id, declared step_count, stored step rows)
TRACES = {
    "read-a": ("dev-1", 2, 2),
    "read-b": ("dev-1", 1, 1),
    "superseded": ("dev-2", 3, 3),
    "partial-read": ("dev-2", 3, 1),
    "empty": ("dev-2", 0, 0),
    "waiting": ("dev-1", 2, 2),
    "sealed-run": ("test-1", 2, 2),
}


def _database(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO corpus_tasks VALUES (?, ?, ?, ?, ?)",
            [
                ("dev-1", "fixture-a", "DEV", "Implement a counter.", "{}"),
                ("dev-2", "fixture-a", "dev", "Parse a date.", "{}"),
                ("test-1", "fixture-a", "TEST", "Sort a list.", "{}"),
            ],
        )
        for trace_id, (task_id, declared, stored) in TRACES.items():
            connection.execute("INSERT INTO agent_traces VALUES (?, ?, ?)", (trace_id, task_id, declared))
            connection.executemany(
                "INSERT INTO trace_steps VALUES (?, ?, 'assistant', 'Checked the output.', '{}')",
                [(trace_id, index) for index in range(stored)],
            )
    return path


def _facet(run_id: str, model: str, source: str = "fixture-a") -> dict[str, Any]:
    return {"run_id": run_id, "source": source, "split": "DEV", "facet_model": model, "suspicion": 0}


def _receipt(run_id: str, model: str, version: str | None = V2) -> dict[str, Any]:
    row = {"run_id": run_id, "purpose": "form", "requested_model": model, "status": "ok"}
    if version is not None:
        row["prompt_version"] = version
    return row


def _summary(run_id: str, status: str = "ok") -> dict[str, Any]:
    return {"run_id": run_id, "purpose": "run", "status": status, "error": None}


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _store(tmp_path: Path) -> list[str]:
    facets = _jsonl(tmp_path / "facets.jsonl", [
        _facet("read-a", HAIKU),
        _facet("read-b", LUNA, source="fixture-b"),
        _facet("superseded", LUNA),
        _facet("partial-read", LUNA),
        _facet("superseded", HAIKU),  # a later line for the same run supersedes the first
        _facet("cheat-item", SONNET, source="cheatbench-actions-v1"),
    ])
    calls = _jsonl(tmp_path / "facet-calls.jsonl", [
        _receipt("read-a", HAIKU), _summary("read-a"),
        _receipt("read-b", LUNA, version=None), _summary("read-b"),  # predates prompt versions
        _receipt("superseded", LUNA, version="run-atlas-prompt/v1"), _summary("superseded"),
        _receipt("waiting", HAIKU), _summary("waiting", status="failed"),
        _receipt("superseded", HAIKU), _summary("superseded"),
        _receipt("partial-read", HAIKU), _summary("partial-read"),  # not the stored reader
    ])
    watch_calls = _jsonl(tmp_path / "watch-calls.jsonl", [
        _receipt("cheat-item", SONNET), _summary("cheat-item"),
    ])
    return ["--facets", str(facets), "--calls", str(calls), "--calls", str(watch_calls)]


def _coverage(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(tmp_path), "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    return subprocess.run(
        [sys.executable, "-m", "evallab.cli", "atlas", "coverage", *argv],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=300,
    )


def test_coverage_counts_stored_records_by_reader_version_and_source(tmp_path: Path) -> None:
    database = _database(tmp_path / "traces.sqlite")
    result = _coverage(tmp_path, "--database", str(database), *_store(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema"] == "evallab.atlas_store_coverage/v1"
    assert report["stored"] == {
        "records_n": 5,
        "mixed_readers": True,
        "by_facet_model": {HAIKU: 2, LUNA: 2, SONNET: 1},
        "by_prompt_version": {V2: 3, "unrecorded": 2},
        "by_source": {"fixture-a": 3, "fixture-b": 1, "cheatbench-actions-v1": 1},
        "not_dev_trace_n": 1,
    }


def test_coverage_splits_deferred_dev_runs(tmp_path: Path) -> None:
    database = _database(tmp_path / "traces.sqlite")
    result = _coverage(tmp_path, "--database", str(database), *_store(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    # TEST-split runs are outside the population; an incomplete run counts as incomplete
    # even when it already holds a facet.
    assert json.loads(result.stdout)["deferred"] == {
        "dev_runs_n": 6, "read_n": 3, "incomplete_steps_n": 2, "not_yet_read_n": 1,
    }


def test_coverage_renders_the_operator_table(tmp_path: Path) -> None:
    database = _database(tmp_path / "traces.sqlite")
    table = _coverage(tmp_path, "--database", str(database), *_store(tmp_path))
    assert table.returncode == 0, table.stderr
    lines = table.stdout.splitlines()
    assert lines[0] == "Facet store: 5 records from 3 reader(s) (mixed readers)"
    assert "DEV runs in the trace DB: 6" in lines
    for label, count in (("read", 3), ("deferred: incomplete steps", 2), ("deferred: not yet read", 1)):
        assert any(line.split() == [*label.split(), str(count)] for line in lines), label
    assert any(line.split() == [V2, "3"] for line in lines)


def test_coverage_refuses_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite"
    result = _coverage(tmp_path, "--database", str(missing), *_store(tmp_path))
    assert result.returncode != 0
    assert result.stdout == ""
    assert f"cloud trace database not found: {missing}" in result.stderr
    assert not missing.exists()
