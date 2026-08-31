from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from evallab.cli import run_cli
from evallab.inspect_adapter import (
    load_inspect_eval_fixture_json,
    load_inspect_eval_log,
    project_inspect_eval_log,
)


def _inspect_fixture_dict() -> dict:
    return {
        "version": 2,
        "status": "success",
        "eval": {
            "eval_id": "eval_gaia_001",
            "task": "gaia-level1",
            "model": "openai/gpt-4o",
            "run_id": "run-gaia-001",
            "created": "2026-08-31T00:00:00Z",
            "revision": "git-commit-abc1234",
            "solver": "agent",
        },
        "stats": {
            "started_at": "2026-08-31T00:00:00Z",
            "completed_at": "2026-08-31T00:05:00Z",
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
                "error_retries": [
                    {
                        "type": "RateLimitError",
                        "message": "Rate limit exceeded, retry 1",
                        "traceback": "Traceback ... 429",
                    }
                ],
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
            "eval_id": "eval_condensed_042",
            "task": "multimodal-research",
            "model": "anthropic/claude-3-7-sonnet",
            "run_id": "run-condensed-042",
            "created": "2026-08-31T02:00:00Z",
            "revision": {"origin": "https://github.com/test/repo", "commit": "fedcba987654"},
            "solver": "research_agent",
        },
        "stats": {
            "started_at": "2026-08-31T02:00:00Z",
            "completed_at": "2026-08-31T02:10:00Z",
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
                        "type": "ConnectionError",
                        "message": "Connection error to LLM backend",
                        "traceback": "Traceback ... ConnectionResetError",
                    },
                    {
                        "type": "GatewayTimeout",
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


def test_official_eval_log_reading_and_projection(tmp_path: Path) -> None:
    pytest.importorskip("inspect_ai")
    import inspect_ai.log as inspect_log
    from inspect_ai.model import ChatMessageAssistant, ChatMessageUser
    from inspect_ai.scorer import Score

    eval_spec = inspect_log.EvalSpec(
        eval_id="eval_official_100",
        task="official-eval-task",
        model="openai/gpt-4o",
        created="2026-08-31T00:00:00Z",
        dataset=inspect_log.EvalDataset(name="official_data", location="loc", samples=1),
        config=inspect_log.EvalConfig(),
        revision=inspect_log.EvalRevision(
            type="git", origin="https://github.com/repo", commit="112233445566"
        ),
        solver="official_solver",
    )
    sample = inspect_log.EvalSample(
        id="sample_off_1",
        epoch=1,
        uuid="99999999-9999-9999-9999-999999999999",
        input="Official input prompt",
        target="Official target",
        messages=[
            ChatMessageUser(content="User message"),
            ChatMessageAssistant(content="Assistant response"),
        ],
        scores={"accuracy": Score(value=1.0, answer="Assistant response", explanation="Correct")},
    )
    eval_log_obj = inspect_log.EvalLog(
        version=3,
        status="success",
        eval=eval_spec,
        samples=[sample],
        stats=inspect_log.EvalStats(
            started_at="2026-08-31T00:00:00Z", completed_at="2026-08-31T00:01:00Z"
        ),
    )

    eval_file = tmp_path / "official_run.eval"
    inspect_log.write_eval_log(eval_log_obj, eval_file)

    # 1. Read through official loader
    loaded_log = load_inspect_eval_log(eval_file)
    assert loaded_log["version"] in (2, 3)
    assert loaded_log["status"] == "success"

    # 2. Project
    projection = project_inspect_eval_log(
        loaded_log,
        source_path=eval_file.name,
        source_bytes=eval_file.read_bytes(),
    )

    assert projection.run.identity_source == "eval_id"
    assert projection.run.eval_id == "eval_official_100"
    assert projection.run.source_revision == "112233445566"
    assert projection.run.validator == "inspect_ai.log.read_eval_log"
    assert projection.run.evidence_only is True
    assert projection.run.sample_count == 1
    assert projection.run.created_at.startswith("2026-08-31T00:00:00")
    assert projection.run.completed_at.startswith("2026-08-31T00:01:00")

    # 3. Check attempts and scores
    assert len(projection.attempts) == 1
    attempt = projection.attempts[0]
    assert attempt.ordinal == 1
    assert attempt.terminal is True
    assert attempt.retry_of is None
    assert attempt.sample_uuid == "99999999-9999-9999-9999-999999999999"

    assert len(projection.scores) == 1
    score = projection.scores[0]
    assert score.score_name == "accuracy"
    assert score.authority == "non_decision"
    assert score.is_deterministic is False


def test_production_loader_refuses_json_files(tmp_path: Path) -> None:
    json_path = tmp_path / "fixture.json"
    json_path.write_text(json.dumps(_inspect_fixture_dict()), encoding="utf-8")

    with pytest.raises(
        ValueError, match="Production Inspect ingest accepts official .eval files only"
    ):
        load_inspect_eval_log(json_path)

    # Segregated JSON fixture loader succeeds and attributes fixture validator
    fixture_data = load_inspect_eval_fixture_json(json_path)
    projection = project_inspect_eval_log(fixture_data, source_path=json_path.name)
    assert projection.run.validator == "evallab.inspect_adapter.fixture_loader"
    assert projection.run.evidence_only is True


def test_domain_separated_run_identity() -> None:
    # 1. Official eval_id (domain key excludes revision)
    payload_eval_id = {
        "version": 2,
        "status": "success",
        "eval": {
            "eval_id": "eval_alpha",
            "task": "task_1",
            "revision": "rev_commit_1",
        },
        "samples": [],
    }
    proj1 = project_inspect_eval_log(payload_eval_id, source_path="run1.eval")
    assert proj1.run.identity_source == "eval_id"

    # Same eval_id with different revision preserves same stable job_id
    payload_eval_id_rev2 = {
        "version": 2,
        "status": "success",
        "eval": {
            "eval_id": "eval_alpha",
            "task": "task_1",
            "revision": "rev_commit_2",
        },
        "samples": [],
    }
    proj2 = project_inspect_eval_log(payload_eval_id_rev2, source_path="run2.eval")
    assert proj2.run.job_id == proj1.run.job_id
    assert proj2.run.source_revision == "rev_commit_2"

    # 2. Run_id fallback
    payload_run_id = {
        "version": 2,
        "status": "success",
        "eval": {
            "run_id": "run_beta",
            "task": "task_1",
        },
        "samples": [],
    }
    proj_run = project_inspect_eval_log(payload_run_id, source_path="run_id.eval")
    assert proj_run.run.identity_source == "run_id"

    # 3. Content digest fallback
    payload_anon = {
        "version": 2,
        "status": "success",
        "eval": {
            "task": "task_1",
        },
        "samples": [],
    }
    proj_anon = project_inspect_eval_log(payload_anon, source_path="anon.eval")
    assert proj_anon.run.identity_source == "content_digest_fallback"


def test_ordered_retry_attempts_and_terminal_outcome() -> None:
    fixture = _condensed_attachment_fixture()
    projection = project_inspect_eval_log(fixture, source_path="condensed.eval")

    # Sample alpha has 2 error retries -> 3 attempts total (ordinal 1, 2, 3)
    assert len(projection.attempts) == 3

    att1, att2, att3 = projection.attempts
    assert att1.ordinal == 1
    assert att1.terminal is False
    assert att1.retry_of is None
    assert att1.status == "error"
    assert att1.error_type == "ConnectionError"

    assert att2.ordinal == 2
    assert att2.terminal is False
    assert att2.retry_of == att1.attempt_id
    assert att2.status == "error"
    assert att2.error_type == "GatewayTimeout"

    assert att3.ordinal == 3
    assert att3.terminal is True
    assert att3.retry_of == att2.attempt_id
    assert att3.status == "success"
    assert att3.total_time == 239.0
    assert att3.working_time == 200.0


def test_scores_authority_defaults_to_non_decision() -> None:
    fixture = _condensed_attachment_fixture()
    projection = project_inspect_eval_log(fixture, source_path="condensed.eval")

    assert len(projection.scores) == 5
    for score in projection.scores:
        assert score.outcome_namespace == "inspect"
        assert score.authority == "non_decision"
        assert score.is_deterministic is False


def test_cli_ingest_requires_cas_with_eval_file(tmp_path: Path, capsys) -> None:
    import inspect_ai.log as inspect_log
    from inspect_ai.model import ChatMessageUser
    from inspect_ai.scorer import Score

    eval_spec = inspect_log.EvalSpec(
        eval_id="cli_eval_01",
        task="cli-task",
        model="openai/gpt-4o",
        created="2026-08-31T00:00:00Z",
        dataset=inspect_log.EvalDataset(name="cli_data", location="loc", samples=1),
        config=inspect_log.EvalConfig(),
        solver="cli_solver",
    )
    sample = inspect_log.EvalSample(
        id="sample_cli_1",
        epoch=1,
        uuid="77777777-7777-7777-7777-777777777777",
        input="CLI question",
        target="CLI answer",
        messages=[ChatMessageUser(content="CLI question")],
        scores={"accuracy": Score(value=1.0, answer="CLI answer")},
    )
    eval_log_obj = inspect_log.EvalLog(
        version=3,
        status="success",
        eval=eval_spec,
        samples=[sample],
    )

    eval_path = tmp_path / "inspect-run.eval"
    inspect_log.write_eval_log(eval_log_obj, eval_path)

    derived_dir = tmp_path / "derived"
    store_dir = tmp_path / "evidence-cas"

    code = run_cli(
        [
            "inspect-ingest",
            str(eval_path),
            "--derived-dir",
            str(derived_dir),
            "--store",
            str(store_dir),
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["raw_cas_uri"].startswith("cas://sha256/")
    assert output["attempts"] == 1


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

    # 8. Duplicate sample uuid collision
    with pytest.raises(ValueError, match="Duplicate sample uuid collision"):
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
    assert projection.scores[0].authority == "non_decision"
