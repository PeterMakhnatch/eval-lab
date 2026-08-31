from __future__ import annotations

import json
from pathlib import Path

import duckdb

from evallab.cli import run_cli
from evallab.inspect_adapter import (
    ingest_inspect_eval_log,
    project_inspect_eval_log,
    write_inspect_projection,
)


def _inspect_fixture() -> dict:
    return {
        "version": 2,
        "status": "success",
        "eval": {
            "run_id": "inspect-run-1",
            "task": "gaia-level1",
            "model": "openai/gpt-test",
            "solver": "react",
            "created_at": "2026-08-31T00:00:00Z",
            "completed_at": "2026-08-31T00:01:00Z",
        },
        "samples": [
            {
                "id": "sample-a",
                "uuid": "sample-a-epoch-1",
                "epoch": 1,
                "started_at": "2026-08-31T00:00:01Z",
                "completed_at": "2026-08-31T00:00:30Z",
                "total_time": 29.0,
                "working_time": 20.0,
                "messages": [
                    {"role": "user", "content": "Find the requested fact."},
                    {
                        "role": "assistant",
                        "content": "I will inspect the source.",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "web_search",
                                    "arguments": {"query": "grounded fact"},
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": "source result",
                    },
                    {"role": "assistant", "content": "Final grounded answer."},
                ],
                "events": [
                    {
                        "event": "model",
                        "timestamp": "2026-08-31T00:00:05Z",
                        "model": "openai/gpt-test",
                    },
                    {
                        "event": "tool",
                        "timestamp": "2026-08-31T00:00:10Z",
                        "function": "web_search",
                    },
                ],
                "model_usage": {
                    "openai/gpt-test": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "cache_read_tokens": 40,
                        "cost": 0.01,
                    }
                },
                "scores": {
                    "task_reward": {
                        "value": 1.0,
                        "answer": "PASS",
                        "explanation": "Verifier passed.",
                    },
                    "policy": {"value": {"violations": 0}},
                },
                "error_retries": [{"error": {"type": "RateLimitError"}, "attempt": 1}],
            },
            {
                "id": "sample-a",
                "uuid": "sample-a-epoch-2",
                "epoch": 2,
                "messages": [{"role": "assistant", "content": "Second epoch."}],
                "events": [],
                "model_usage": {},
                "scores": {"task_reward": {"value": 0.0, "answer": "FAIL"}},
                "error": {
                    "type": "TimeoutError",
                    "message": "sample exceeded limit",
                },
                "limit": {"type": "time"},
            },
        ],
    }


def test_inspect_projection_preserves_epochs_scores_events_and_canonical_spine() -> None:
    payload = _inspect_fixture()
    projection = project_inspect_eval_log(
        payload,
        source_path="fixture.eval",
        source_bytes=b"inspect-eval-bytes",
    )
    assert projection.run.task_name == "gaia-level1"
    assert projection.run.sample_count == 2
    assert len(projection.attempts) == 2
    assert projection.attempts[0].epoch == 1
    assert projection.attempts[0].retry_error_count == 1
    assert projection.attempts[1].epoch == 2
    assert projection.attempts[1].status == "error"
    assert projection.attempts[1].limit_type == "time"
    assert projection.attempts[0].trial_id != projection.attempts[1].trial_id

    assert len(projection.scores) == 3
    assert {score.score_name for score in projection.scores} == {
        "task_reward",
        "policy",
    }
    assert all(score.outcome_namespace == "inspect" for score in projection.scores)
    assert all(score.authority == "inspect_scorer" for score in projection.scores)

    assert len(projection.events) == 2
    assert len(projection.trajectories.trajectories) == 2
    assert len(projection.trajectories.steps) == 5
    assert len(projection.trajectories.tool_calls) == 1
    assert len(projection.trajectories.observations) == 1
    trajectory = projection.trajectories.trajectories[0]
    assert trajectory.prompt_tokens == 100
    assert trajectory.completion_tokens == 25
    assert trajectory.cached_tokens == 40
    assert trajectory.cost_usd == 0.01


def test_inspect_projection_writes_shared_parquet_tables(tmp_path: Path) -> None:
    projection = project_inspect_eval_log(
        _inspect_fixture(),
        source_path="fixture.eval",
    )
    paths = write_inspect_projection(projection, tmp_path / "parquet")
    assert set(paths) == {
        "inspect_runs",
        "inspect_attempts",
        "inspect_scores",
        "inspect_events",
        "trajectories",
        "steps",
        "tool_calls",
        "observations",
    }
    with duckdb.connect(":memory:") as connection:
        attempts = connection.execute(
            "SELECT epoch, status FROM read_parquet(?) ORDER BY epoch",
            [str(paths["inspect_attempts"])],
        ).fetchall()
        scores = connection.execute(
            "SELECT score_name, outcome_namespace FROM read_parquet(?) ORDER BY score_name",
            [str(paths["inspect_scores"])],
        ).fetchall()
    assert attempts == [(1, "success"), (2, "error")]
    assert scores == [
        ("policy", "inspect"),
        ("task_reward", "inspect"),
        ("task_reward", "inspect"),
    ]


def test_inspect_json_ingest_archives_raw_log_and_projects(tmp_path: Path) -> None:
    log_path = tmp_path / "inspect-log.json"
    log_path.write_text(json.dumps(_inspect_fixture()), encoding="utf-8")
    result = ingest_inspect_eval_log(
        log_path,
        output_root=tmp_path / "derived",
        store_root=tmp_path / "cas",
    )
    assert result.raw_cas_uri is not None
    assert result.raw_cas_uri.startswith("cas://sha256/")
    assert result.table_paths["inspect_runs"].is_file()
    assert result.projection.run.source_path == "inspect-log.json"


def test_inspect_ingest_cli_projects_json_log(tmp_path: Path, capsys) -> None:
    log_path = tmp_path / "inspect-log.json"
    log_path.write_text(json.dumps(_inspect_fixture()), encoding="utf-8")
    code = run_cli(
        [
            "inspect-ingest",
            "inspect-log.json",
            "--derived-dir",
            "derived",
            "--no-cas",
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["samples"] == 2
    assert output["scores"] == 3
    assert output["raw_cas_uri"] is None
