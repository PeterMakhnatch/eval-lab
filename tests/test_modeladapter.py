"""Tests for model adapter subsystem, CLI transports, and injection points."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.analysis_worker import (
    AnalysisWorker,
    _no_adapter,
    default_worker,
)
from evallab.analyst import (
    ModelAnalyzer,
    ModelProviderRefusedError,
    run_analysis,
)
from evallab.evidence.facts import AnalyzerCallResult
from evallab.modeladapter import (
    ModelAdapter,
    ModelAdapterExecutionError,
    ModelAdapterRefusalError,
    ModelAdapterResult,
    ModelAdapterSchemaError,
    ModelAdapterTimeoutError,
    SchemaRepairingAdapter,
    agy_adapter,
    cursor_adapter,
    validate_pinned_model,
)


def _make_fake_runner(
    stdout: str = "Fake model completion response",
    stderr: str = "",
    returncode: int = 0,
    raise_timeout: bool = False,
    timeout_val: float = 30.0,
):
    calls: list[dict[str, Any]] = []

    def runner(
        argv: list[str],
        *,
        timeout: float | None = None,
        env: Any = None,
        cwd: Any = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, "timeout": timeout, "env": env, "cwd": cwd})
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout or timeout_val)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return runner, calls


def test_cursor_adapter_argv_and_success() -> None:
    fake_runner, calls = _make_fake_runner(stdout="cursor completion output")
    adapter = cursor_adapter(
        model="cursor-grok-4.6-high",
        timeout_seconds=45.0,
        runner=fake_runner,
        output_format="text",
    )

    result = adapter("Analyze trial 123")
    assert isinstance(result, ModelAdapterResult)
    assert isinstance(result, AnalyzerCallResult)
    assert result.raw_output == "cursor completion output"
    assert result.model == "cursor-grok-4.6-high"
    assert result.transport == "cursor-agent"

    assert len(calls) == 1
    assert calls[0]["timeout"] == 45.0
    argv = calls[0]["argv"]
    assert argv[0] == "cursor-agent"
    assert "-f" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "cursor-grok-4.6-high"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "Analyze trial 123"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "text"


def test_agy_adapter_argv_and_success() -> None:
    fake_runner, calls = _make_fake_runner(stdout="agy completion output")
    adapter = agy_adapter(
        model="gemini-3.7-flash-high",
        timeout_seconds=60.0,
        runner=fake_runner,
        effort="medium",
        output_format="text",
    )

    result = adapter.complete("Analyze trial 456")
    assert result.raw_output == "agy completion output"
    assert result.model == "gemini-3.7-flash-high"
    assert result.transport == "agy"

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-high"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "Analyze trial 456"
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "medium"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "text"


def test_schema_repairing_adapter_extracts_nested_transport_response() -> None:
    schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["memory_failure"]},
            "summary": {"type": "string"},
        },
        "required": ["category", "summary"],
    }

    def adapter(_prompt: str, _schema: dict[str, Any] | None) -> ModelAdapterResult:
        nested = json.dumps(
            {
                "conversation_id": "c1",
                "status": "SUCCESS",
                "response": '```json\\n{"category":"memory_failure","summary":"grounded"}\\n```',
            }
        )
        return ModelAdapterResult(
            raw_output=nested,
            model="judge-v1",
            argv=["agy"],
            transport="agy",
        )

    result = SchemaRepairingAdapter(adapter)(prompt="judge", schema=schema)
    assert json.loads(result.raw_output) == {
        "category": "memory_failure",
        "summary": "grounded",
    }


def test_schema_repairing_adapter_retries_then_refuses() -> None:
    schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["memory_failure"]},
            "summary": {"type": "string"},
        },
        "required": ["category", "summary"],
    }
    outputs = [
        "Unstructured response",
        '{"category":"memory_failure","summary":"repaired"}',
    ]
    prompts: list[str] = []

    def adapter(prompt: str, _schema: dict[str, Any] | None) -> ModelAdapterResult:
        prompts.append(prompt)
        return ModelAdapterResult(
            raw_output=outputs[len(prompts) - 1],
            model="judge-v1",
            argv=["agy"],
            transport="agy",
        )

    result = SchemaRepairingAdapter(adapter)(prompt="judge", schema=schema)
    assert json.loads(result.raw_output)["summary"] == "repaired"
    assert len(prompts) == 2
    assert "Source response" in prompts[1]

    def invalid_adapter(_prompt: str, _schema: dict[str, Any] | None) -> ModelAdapterResult:
        return ModelAdapterResult(
            raw_output="still invalid",
            model="judge-v1",
            argv=["agy"],
            transport="agy",
        )

    with pytest.raises(ModelAdapterSchemaError, match="did not satisfy"):
        SchemaRepairingAdapter(invalid_adapter)(prompt="judge", schema=schema)


def test_nonzero_exit_raises_execution_error() -> None:
    fake_runner, _ = _make_fake_runner(
        returncode=127,
        stderr="command not found: cursor-agent",
    )
    adapter = ModelAdapter(
        model="cursor-grok-4.6-high",
        transport="cursor-agent",
        runner=fake_runner,
    )

    with pytest.raises(ModelAdapterExecutionError) as exc_info:
        adapter("Some prompt")

    err = exc_info.value
    assert err.returncode == 127
    assert "command not found" in err.stderr
    assert "cursor-agent" in err.argv[0]


def test_timeout_raises_timeout_error() -> None:
    fake_runner, _ = _make_fake_runner(raise_timeout=True, timeout_val=10.0)
    adapter = ModelAdapter(
        model="gemini-3.7-flash-high",
        transport="agy",
        timeout_seconds=10.0,
        runner=fake_runner,
    )

    with pytest.raises(ModelAdapterTimeoutError) as exc_info:
        adapter("Some long running prompt")

    err = exc_info.value
    assert err.timeout == 10.0
    assert "--model" in err.argv


@pytest.mark.parametrize(
    "invalid_model",
    [
        "",
        "   ",
        "auto",
        "AUTO",
        "default",
        "DEFAULT",
        "none",
        "latest",
        "unpinned",
        "null",
        "auto:latest",
        "default:model",
    ],
)
def test_unpinned_or_empty_model_refused_before_process_starts(
    invalid_model: str,
) -> None:
    fake_runner, calls = _make_fake_runner()

    with pytest.raises(ModelAdapterRefusalError):
        validate_pinned_model(invalid_model)

    with pytest.raises(ModelAdapterRefusalError):
        ModelAdapter(
            model=invalid_model,
            transport="cursor-agent",
            runner=fake_runner,
        )

    assert len(calls) == 0, "No subprocess must start when model is unpinned"


def test_none_model_refused_before_process_starts() -> None:
    fake_runner, calls = _make_fake_runner()

    with pytest.raises(ModelAdapterRefusalError):
        validate_pinned_model(None)

    with pytest.raises(ModelAdapterRefusalError):
        ModelAdapter(
            model=None,  # type: ignore[arg-type]
            transport="cursor-agent",
            runner=fake_runner,
        )

    assert len(calls) == 0, "No subprocess must start when model is None"


def test_analyst_refuses_without_adapter() -> None:
    # 1. ModelAnalyzer without model raises ModelProviderRefusedError
    with pytest.raises(
        ModelProviderRefusedError, match="Model analyzer requires an explicit model selector"
    ):
        ModelAnalyzer(model=None)

    # 2. ModelAnalyzer with model selector but no adapter refuses when analyze() is called
    analyzer = ModelAnalyzer(model="cursor-grok-4.6-high")
    with pytest.raises(ModelProviderRefusedError, match="spends tokens"):
        analyzer.analyze("prompt", "context")


def test_analyst_succeeds_with_injected_adapter(tmp_path: Path) -> None:
    # Create fake structured response
    model_json = json.dumps(
        {
            "category": "task_execution_failure",
            "summary": "Agent failed task due to missing file.",
            "evidence": [{"path": "agent/trajectory.json", "step": 1}],
            "confidence": "high",
        }
    )
    fake_runner, _ = _make_fake_runner(stdout=model_json)
    adapter = cursor_adapter(
        model="cursor-grok-4.6-high",
        runner=fake_runner,
    )

    analyzer = ModelAnalyzer(model="cursor-grok-4.6-high", adapter=adapter)
    result = analyzer.analyze("rubric prompt", "trial context")

    assert result.category == "task_execution_failure"
    assert result.summary == "Agent failed task due to missing file."
    assert len(result.evidence) == 1
    assert result.evidence[0].path == "agent/trajectory.json"
    assert result.evidence[0].step == 1
    assert result.confidence.level == "high"
    assert len(result.steps) >= 1
    assert result.steps[0]["model"] == "cursor-grok-4.6-high"


def test_run_analysis_with_injected_adapter(tmp_path: Path) -> None:
    # Set up synthetic trial directory
    trial_dir = tmp_path / "runs" / "job_01" / "trial_01"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial_01",
                "task_name": "event-summary",
                "agent_name": "codex",
                "model_name": "codex-gpt-5.6-terra",
                "primary_reward": 0.0,
            }
        )
    )
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"step_id": 0, "source": "agent", "message": "Initial plan"},
                    {"step_id": 1, "source": "agent", "message": "Failed action"},
                ]
            }
        )
    )

    # 1. run_analysis with model and no adapter refuses
    derived = tmp_path / "derived" / "parquet"
    with pytest.raises(ModelProviderRefusedError):
        run_analysis(
            "trial_01",
            model="cursor-grok-4.6-high",
            adapter=None,
            repo_root=tmp_path,
            derived_root=derived,
            runs_root=tmp_path / "runs",
        )

    # 2. run_analysis with model and injected adapter succeeds
    model_json = json.dumps(
        {
            "category": "task_execution_failure",
            "summary": "Agent encountered an error.",
            "evidence": [{"path": "agent/trajectory.json", "step": 1}],
            "confidence": "high",
        }
    )
    fake_runner, _ = _make_fake_runner(stdout=model_json)
    adapter = cursor_adapter(model="cursor-grok-4.6-high", runner=fake_runner)

    record, traj, conclusion_file, _ = run_analysis(
        "trial_01",
        model="cursor-grok-4.6-high",
        adapter=adapter,
        repo_root=tmp_path,
        derived_root=derived,
        runs_root=tmp_path / "runs",
    )
    assert record.model == "cursor-grok-4.6-high"
    assert record.category == "task_execution_failure"
    assert conclusion_file.is_file()


def test_analysis_worker_defers_with_no_adapter(tmp_path: Path) -> None:
    # Verify _no_adapter raises RuntimeError
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "standing-approvals.yaml").write_text(
        """version: 1
daily_cost_ceiling_usd: 20
per_job_cost_ceiling_usd: 3
quiet_failure_rule: 3
refuse_billable_at_used_percent: null
auto_run:
  - name: local-controls
    agents: [oracle, nop]
escalate_to_human:
  - any_billable_agent
"""
    )
    (tmp_path / "derived" / "analyses" / "worker").mkdir(parents=True, exist_ok=True)

    worker = default_worker(tmp_path)
    assert isinstance(worker, AnalysisWorker)
    assert worker.adapter is _no_adapter


def test_analysis_worker_accepts_injected_adapter(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "standing-approvals.yaml").write_text(
        """version: 1
daily_cost_ceiling_usd: 20
per_job_cost_ceiling_usd: 3
quiet_failure_rule: 3
refuse_billable_at_used_percent: null
auto_run:
  - name: local-controls
    agents: [oracle, nop]
escalate_to_human:
  - any_billable_agent
    """
    )
    (tmp_path / "derived" / "analyses" / "worker").mkdir(parents=True, exist_ok=True)
    fake_runner, _ = _make_fake_runner(stdout="{}")
    adapter = cursor_adapter(model="cursor-grok-4.6-high", runner=fake_runner)
    worker = default_worker(tmp_path, adapter=adapter)
    assert worker.adapter is adapter
