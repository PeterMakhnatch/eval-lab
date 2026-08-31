from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from pydantic import BaseModel

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
            "task": "gaia-level1",
            "model": "openai/gpt-4o",
            "run_id": "run-gaia-001",
            "created_at": "2026-08-31T00:00:00Z",
            "completed_at": "2026-08-31T00:05:00Z",
            "solver": "agent",
        },
        "samples": [
            {
                "id": "sample-1",
                "epoch": 1,
                "uuid": "00000000-0000-0000-0000-000000000001",
                "started_at": "2026-08-31T00:00:01Z",
                "completed_at": "2026-08-31T00:01:00Z",
                "total_time": 59.0,
                "working_time": 45.0,
                "error_retries": [{"error": "Rate limit exceeded, retry 1"}],
                "model_usage": {
                    "openai/gpt-4o": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "cache_read_tokens": 40,
                        "cost": 0.01,
                    }
                },
                "scores": {
                    "accuracy": {"value": 1.0, "answer": "Paris", "explanation": "Correct capital"},
                    "format": {"value": "valid", "metadata": {"schema": "v1"}},
                },
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What is the capital of France?"},
                    {
                        "role": "assistant",
                        "content": "Checking database.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "name": "lookup",
                                "arguments": {"query": "capital of France"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": "Paris is the capital.",
                    },
                    {"role": "assistant", "content": "Paris"},
                ],
                "events": [
                    {"event": "sample_init", "timestamp": "2026-08-31T00:00:01Z"},
                    {"event": "model", "timestamp": "2026-08-31T00:00:05Z"},
                ],
            },
            {
                "id": "sample-1",
                "epoch": 2,
                "started_at": "2026-08-31T00:01:01Z",
                "completed_at": "2026-08-31T00:02:00Z",
                "error": {"type": "TimeoutError", "message": "Timed out after 60s"},
                "limit": {"type": "time"},
                "scores": {
                    "quality": {"value": 0.0, "explanation": "Timed out"},
                },
                "messages": [
                    {"role": "user", "content": "Retry the question."},
                ],
                "events": [],
            },
        ],
    }


def _condensed_attachment_fixture() -> dict:
    return {
        "version": 3,
        "status": "success",
        "eval": {
            "task": "multimodal-research",
            "model": "anthropic/claude-3-7-sonnet",
            "run_id": "run-condensed-042",
            "created_at": "2026-08-31T02:00:00Z",
            "completed_at": "2026-08-31T02:10:00Z",
            "solver": "research_agent",
        },
        "attachments": {
            "sys_prompt": "You are an expert multi-eval researcher.",
        },
        "samples": [
            {
                "id": "sample-alpha",
                "epoch": 1,
                "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "started_at": "2026-08-31T02:00:01Z",
                "completed_at": "2026-08-31T02:04:00Z",
                "total_time": 239.0,
                "working_time": 200.0,
                "attachments": {
                    "doc_research_spec": "Research spec: examine trajectory compression and entropy.",
                    "chart_image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                },
                "error_retries": [
                    {
                        "message": "Connection error to LLM backend",
                        "traceback": "Traceback ... ConnectionResetError",
                    },
                    {
                        "message": "Gateway timeout",
                        "traceback": "Traceback ... 504 Gateway Timeout",
                    },
                ],
                "model_usage": {
                    "anthropic/claude-3-7-sonnet": {
                        "input_tokens": 1500,
                        "output_tokens": 400,
                        "cache_read_tokens": 800,
                        "cache_write_tokens": 200,
                        "cost": 0.045,
                    }
                },
                "scores": {
                    "accuracy": {"value": 0.96, "answer": "PASS", "explanation": "Target verified"},
                    "is_factual": {
                        "value": True,
                        "answer": "true",
                        "metadata": {"confidence": 0.99},
                    },
                    "metrics_breakdown": {
                        "value": {"precision": 0.95, "recall": 0.98, "f1": 0.965},
                        "metadata": {"curve": "pr_v1"},
                    },
                    "tags": {"value": ["fast", "accurate", "clean"]},
                    "verdict": "pass",
                },
                "messages": [
                    {
                        "role": "system",
                        "content": "tc://sys_prompt",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "attachment://doc_research_spec"},
                            {"type": "image", "image": "attachment://chart_image"},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning", "reasoning": "Need to execute analysis script."},
                            {"type": "text", "text": "Running the evaluation tool now."},
                        ],
                        "tool_calls": [
                            {
                                "id": "call_exec_01",
                                "function": "run_python",
                                "arguments": {"script": "print('compression ratio: 4.2')"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_exec_01",
                        "content": "compression ratio: 4.2",
                        "metadata": {"exit_code": 0},
                    },
                    {
                        "role": "assistant",
                        "content": "The analysis indicates a 4.2x compression ratio.",
                    },
                ],
                "events": [
                    {
                        "event": "sample_init",
                        "timestamp": "2026-08-31T02:00:01Z",
                        "span_id": "span_0",
                    },
                    {"event": "model", "timestamp": "2026-08-31T02:01:00Z", "span_id": "span_1"},
                    {"event": "tool", "timestamp": "2026-08-31T02:02:00Z", "span_id": "span_2"},
                ],
            }
        ],
    }


def test_inspect_projection_preserves_epochs_scores_events_and_canonical_spine() -> None:
    payload = _inspect_fixture()
    projection = project_inspect_eval_log(
        payload,
        source_path="fixture.eval",
        source_bytes=b"fixture-bytes",
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
        "accuracy",
        "format",
        "quality",
    }
    assert all(score.outcome_namespace == "inspect" for score in projection.scores)
    assert all(score.authority == "inspect_scorer" for score in projection.scores)

    assert len(projection.events) == 2
    assert len(projection.trajectories.trajectories) == 2
    assert len(projection.trajectories.steps) == 6
    assert len(projection.trajectories.tool_calls) == 1
    assert len(projection.trajectories.observations) == 1
    trajectory = projection.trajectories.trajectories[0]
    assert trajectory.prompt_tokens == 100
    assert trajectory.completion_tokens == 25
    assert trajectory.cached_tokens == 40
    assert trajectory.cost_usd == 0.01

    assert projection.rebuild_digest.startswith("sha256:")
    assert projection.run.rebuild_digest == projection.rebuild_digest


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
        "inspect_attachments",
        "trajectories",
        "steps",
        "tool_calls",
        "observations",
    }
    for path in paths.values():
        assert path.is_file()

    conn = duckdb.connect()
    assert (
        conn.execute(
            f"SELECT count(*) FROM read_parquet('{paths['inspect_attempts']}')"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(f"SELECT count(*) FROM read_parquet('{paths['inspect_scores']}')").fetchone()[
            0
        ]
        == 3
    )
    assert (
        conn.execute(f"SELECT count(*) FROM read_parquet('{paths['trajectories']}')").fetchone()[0]
        == 2
    )


def test_inspect_json_ingest_archives_raw_log_and_projects(tmp_path: Path) -> None:
    log_path = tmp_path / "inspect-log.json"
    log_path.write_text(json.dumps(_inspect_fixture()), encoding="utf-8")
    result = ingest_inspect_eval_log(
        log_path,
        output_root=tmp_path / "derived",
        store_root=tmp_path / "cas",
    )
    assert result.raw_cas_uri is not None
    assert result.raw_cas_uri.startswith("cas://")
    assert result.projection.run.source_path == "inspect-log.json"
    assert result.source_manifest is not None
    assert result.source_manifest.schema_version == "inspect-source-manifest/v1"
    assert result.source_manifest.job_id == result.projection.run.job_id


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
    assert output["status"] == "success"
    assert output["samples"] == 2
    assert output["attempts"] == 2
    assert output["scores"] == 3
    assert output["raw_cas_uri"] is None


def test_condensed_eval_log_resolves_attachments_and_preserves_complex_scores() -> None:
    fixture = _condensed_attachment_fixture()
    projection = project_inspect_eval_log(
        fixture,
        source_path="condensed.eval",
    )
    assert len(projection.attempts) == 1
    attempt = projection.attempts[0]
    assert attempt.sample_uuid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert attempt.trial_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert attempt.retry_error_count == 2
    assert attempt.message_count == 5
    assert attempt.event_count == 3

    # Check attachments resolution and tracking
    assert len(projection.attachments) == 3
    att_by_id = {att.attachment_id: att for att in projection.attachments}
    assert "sys_prompt" in att_by_id
    assert "doc_research_spec" in att_by_id
    assert "chart_image" in att_by_id
    assert att_by_id["sys_prompt"].resolved_count == 1
    assert att_by_id["doc_research_spec"].resolved_count == 1
    assert att_by_id["chart_image"].resolved_count == 1

    # Check complex multi-score parsing
    scores_by_name = {score.score_name: score for score in projection.scores}
    assert len(scores_by_name) == 5

    # Float score
    assert scores_by_name["accuracy"].value_type == "float"
    assert json.loads(scores_by_name["accuracy"].value_json) == 0.96
    assert scores_by_name["accuracy"].answer == "PASS"

    # Bool score
    assert scores_by_name["is_factual"].value_type == "bool"
    assert json.loads(scores_by_name["is_factual"].value_json) is True

    # Dict score
    assert scores_by_name["metrics_breakdown"].value_type == "dict"
    metrics_val = json.loads(scores_by_name["metrics_breakdown"].value_json)
    assert metrics_val == {"f1": 0.965, "precision": 0.95, "recall": 0.98}

    # List score
    assert scores_by_name["tags"].value_type == "list"
    assert json.loads(scores_by_name["tags"].value_json) == ["fast", "accurate", "clean"]

    # String score
    assert scores_by_name["verdict"].value_type == "str"
    assert json.loads(scores_by_name["verdict"].value_json) == "pass"

    # Canonical Trajectories
    assert len(projection.trajectories.steps) == 5
    assert len(projection.trajectories.tool_calls) == 1
    tool_call = projection.trajectories.tool_calls[0]
    assert tool_call.function_name == "run_python"
    assert tool_call.tool_call_id == "call_exec_01"

    assert len(projection.trajectories.observations) == 1
    obs = projection.trajectories.observations[0]
    assert obs.source_call_id == "call_exec_01"
    assert obs.command_exit_code == 0


def test_malformed_input_refusals() -> None:
    # 1. Empty payload
    with pytest.raises(ValueError, match="empty or not a dictionary"):
        project_inspect_eval_log({}, source_path="bad.json")

    # 2. Missing version
    with pytest.raises(ValueError, match="version is missing"):
        project_inspect_eval_log({"status": "success", "eval": {}}, source_path="bad.json")

    # 3. Invalid version type
    with pytest.raises(ValueError, match="version has invalid type"):
        project_inspect_eval_log({"version": True, "status": "success"}, source_path="bad.json")

    # 4. Non-positive version
    with pytest.raises(ValueError, match="version must be positive"):
        project_inspect_eval_log({"version": 0, "status": "success"}, source_path="bad.json")

    # 5. Missing status
    with pytest.raises(ValueError, match="status is missing"):
        project_inspect_eval_log({"version": 2}, source_path="bad.json")

    # 6. Invalid epoch
    with pytest.raises(ValueError, match="invalid non-positive epoch"):
        project_inspect_eval_log(
            {
                "version": 2,
                "status": "success",
                "samples": [{"id": "s1", "epoch": 0}],
            },
            source_path="bad.json",
        )

    # 7. Duplicate (sample_id, epoch) collision
    with pytest.raises(ValueError, match="Duplicate sample identity collision"):
        project_inspect_eval_log(
            {
                "version": 2,
                "status": "success",
                "samples": [
                    {"id": "sample-1", "epoch": 1},
                    {"id": "sample-1", "epoch": 1},
                ],
            },
            source_path="bad.json",
        )

    # 8. Duplicate trial_id collision
    with pytest.raises(ValueError, match="Duplicate trial_id collision"):
        project_inspect_eval_log(
            {
                "version": 2,
                "status": "success",
                "samples": [
                    {"id": "sample-1", "epoch": 1, "uuid": "same-uuid"},
                    {"id": "sample-2", "epoch": 1, "uuid": "same-uuid"},
                ],
            },
            source_path="bad.json",
        )


def test_rebuild_digest_determinism() -> None:
    fixture = _condensed_attachment_fixture()
    proj1 = project_inspect_eval_log(fixture, source_path="test.eval")
    proj2 = project_inspect_eval_log(fixture, source_path="test.eval")
    assert proj1.rebuild_digest == proj2.rebuild_digest

    # Changing any field alters the rebuild digest
    fixture_modified = _condensed_attachment_fixture()
    fixture_modified["samples"][0]["scores"]["accuracy"]["value"] = 0.99
    proj_mod = project_inspect_eval_log(fixture_modified, source_path="test.eval")
    assert proj_mod.rebuild_digest != proj1.rebuild_digest


def test_pydantic_model_dump_object_projection() -> None:
    class MockEvalSpec(BaseModel):
        task: str
        model: str
        run_id: str

    class MockSample(BaseModel):
        id: str
        epoch: int
        scores: dict[str, float]
        messages: list[dict[str, str]]

    class MockEvalLog(BaseModel):
        version: int
        status: str
        eval: MockEvalSpec
        samples: list[MockSample]

    mock_log = MockEvalLog(
        version=2,
        status="success",
        eval=MockEvalSpec(task="pydantic-test", model="gpt-4o", run_id="pydantic-001"),
        samples=[
            MockSample(
                id="sample-p1",
                epoch=1,
                scores={"accuracy": 1.0},
                messages=[
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer"},
                ],
            )
        ],
    )

    projection = project_inspect_eval_log(mock_log, source_path="pydantic.eval")
    assert projection.run.task_name == "pydantic-test"
    assert projection.run.model_name == "gpt-4o"
    assert len(projection.attempts) == 1
    assert len(projection.scores) == 1
    assert projection.scores[0].score_name == "accuracy"
    assert len(projection.trajectories.steps) == 2
