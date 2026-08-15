from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.atif import PARQUET_SCHEMAS
from evallab.facts import TRIAL_FACT_SCHEMA

QUERY_FILE = Path(__file__).parents[1] / "research/analysis/queries.sql"
BEGIN = "-- BEGIN TRAJECTORY_INTELLIGENCE_DUCKDB"
END = "-- END TRAJECTORY_INTELLIGENCE_DUCKDB"


def _queries() -> dict[str, str]:
    block = QUERY_FILE.read_text().split(BEGIN, 1)[1].split(END, 1)[0]
    queries: dict[str, str] = {}
    for section in block.split("-- name: ")[1:]:
        name, body = section.split("\n", 1)
        query = body.strip()
        assert query.endswith(";"), name
        queries[name.strip()] = query
    return queries


def _default(field: pa.Field) -> object:
    if pa.types.is_string(field.type):
        return "x"
    if pa.types.is_integer(field.type):
        return 0
    if pa.types.is_floating(field.type):
        return 0.0
    if pa.types.is_boolean(field.type):
        return False
    raise AssertionError(f"fixture needs a default for {field.type}")


def _row(schema: pa.Schema, **overrides: object) -> dict[str, object]:
    row = {field.name: _default(field) for field in schema}
    row.update(overrides)
    return row


def _write_fixture(root: Path) -> None:
    partition = root / "derived/parquet/job_id=j1/trial_id=t1"
    partition.mkdir(parents=True)
    digest = "sha256:" + "a" * 64
    trajectory = _row(
        PARQUET_SCHEMAS["trajectories"],
        job_id="j1",
        trial_id="t1",
        document_id="d1",
        source_path="agent/trajectory.json",
        source_sha256=digest,
        model_name="model-a",
        prompt_tokens=220,
        completion_tokens=30,
        cost_usd=0.2,
    )
    steps = [
        _row(
            PARQUET_SCHEMAS["steps"],
            job_id="j1",
            trial_id="t1",
            document_id="d1",
            source_path="agent/trajectory.json",
            source_sha256=digest,
            step_id=step_id,
            prompt_tokens=prompt_tokens,
        )
        for step_id, prompt_tokens in ((1, 100), (2, 150), (3, 220))
    ]
    tool_calls = [
        _row(
            PARQUET_SCHEMAS["tool_calls"],
            job_id="j1",
            trial_id="t1",
            document_id="d1",
            source_path="agent/trajectory.json",
            source_sha256=digest,
            step_id=index,
            tool_call_id=f"call-{index}",
            function_name="shell" if index < 3 else f"tool-{index}",
            arguments_sha256=digest if index < 3 else f"sha256:{index:064x}",
        )
        for index in range(1, 5)
    ]
    observations = [
        _row(
            PARQUET_SCHEMAS["observations"],
            job_id="j1",
            trial_id="t1",
            document_id="d1",
            source_path="agent/trajectory.json",
            source_sha256=digest,
            step_id=index,
            observation_index=0,
            source_call_id=f"call-{index}",
            command_exit_code=1 if index < 3 else 0,
        )
        for index in range(1, 4)
    ]
    trials = [
        _row(
            TRIAL_FACT_SCHEMA,
            job_id="j1",
            trial_id="t1",
            task_name="task-a",
            task_digest=digest,
            verifier_digest=digest,
            agent_config_digest=digest,
            primary_reward=1.0,
            exception_class=None,
            exception_phase=None,
        ),
        _row(
            TRIAL_FACT_SCHEMA,
            job_id="j2",
            trial_id="t2",
            task_name="task-a",
            task_digest=digest,
            verifier_digest=digest,
            agent_config_digest=digest,
            primary_reward=0.0,
            exception_class=None,
            exception_phase=None,
            step_count=2,
            tool_call_count=0,
        ),
    ]
    tables = {
        "trajectories": (PARQUET_SCHEMAS["trajectories"], [trajectory]),
        "steps": (PARQUET_SCHEMAS["steps"], steps),
        "tool_calls": (PARQUET_SCHEMAS["tool_calls"], tool_calls),
        "observations": (PARQUET_SCHEMAS["observations"], observations),
    }
    for name, (schema, rows) in tables.items():
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), partition / f"{name}.parquet")
    pq.write_table(
        pa.Table.from_pylist([trials[0]], schema=TRIAL_FACT_SCHEMA),
        partition / "trial_facts.parquet",
    )
    second_partition = root / "derived/parquet/job_id=j2/trial_id=t2"
    second_partition.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([trials[1]], schema=TRIAL_FACT_SCHEMA),
        second_partition / "trial_facts.parquet",
    )


def test_all_trajectory_intelligence_queries_execute(tmp_path: Path, monkeypatch) -> None:
    queries = _queries()
    assert len(queries) >= 8
    _write_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    connection = duckdb.connect()
    results = {name: connection.execute(query).fetchall() for name, query in queries.items()}
    assert results["loop-index"][0][-1] == 0.25
    assert results["tool-efficiency-ratio"][0][-1] == 0.25
    assert results["context-bloat-velocity"][0][-1] == pytest.approx(60.0)
    assert results["flaky-verifier-candidates"]
    assert results["tool-hallucination-candidates"][0][4] == "call-4"
    assert results["surrender-candidates"][0][1] == "t2"
    assert results["repeated-failed-commands"][0][4] == 2
