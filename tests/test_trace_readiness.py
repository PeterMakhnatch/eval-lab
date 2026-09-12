"""Behavioral tests for the trace readiness gate over Harbor trial directories.

All fixtures are synthetic ATIF v1.7 payloads built in tmp_path; the real
TB4 root is never touched and no model, docker, or network calls occur.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation import trace_readiness
from evallab.interpretation.trace_readiness import check_trace_readiness


def _write_atif(
    trial: Path,
    *,
    agent_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    step_prompt: int | None = None,
    step_completion: int | None = None,
) -> Path:
    agent_dir = trial / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    first_metrics = {
        "prompt_tokens": prompt_tokens if step_prompt is None else step_prompt,
        "completion_tokens": completion_tokens if step_completion is None else step_completion,
    }
    payload = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": agent_name, "version": "0.1.0"},
        "stop_reason": "completed",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "Listing files.",
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "function_name": "exec",
                        "arguments": {"command": "ls"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": "a.txt",
                            "type": "output",
                            "status": "success",
                            "extra": {"exit_code": 0},
                        }
                    ]
                },
                "metrics": first_metrics,
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "Done.",
                "tool_calls": [],
                "metrics": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": prompt_tokens,
            "total_completion_tokens": completion_tokens,
        },
    }
    path = agent_dir / "trajectory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _snapshot(trial: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(trial)): path.read_bytes()
        for path in sorted(trial.rglob("*"))
        if path.is_file()
    }


def test_interpretable_dsh_shaped_fixture(tmp_path: Path) -> None:
    trial = tmp_path / "trial-dsh"
    trial.mkdir()
    _write_atif(
        trial, agent_name="deepseek-harness", prompt_tokens=300, completion_tokens=150
    )
    before = _snapshot(trial)

    report = check_trace_readiness(trial)

    assert report["verdict"] == "interpretable"
    assert report["atif_present"] is True
    assert report["atif_schema_version"] == "ATIF-v1.7"
    assert report["agent_name"] == "deepseek-harness"
    assert report["steps"] == 2
    assert report["tool_calls"] == 1
    assert report["observations"] == 1
    assert report["final_metrics_present"] is True
    assert report["prompt_tokens"] == 300
    assert report["completion_tokens"] == 150
    assert report["stop_reason_present"] is True
    assert report["ir_built"] is True
    assert report["ir_error"] is None
    assert _snapshot(trial) == before


def test_interpretable_mini_swe_shaped_fixture(tmp_path: Path) -> None:
    trial = tmp_path / "trial-miniswe"
    trial.mkdir()
    _write_atif(
        trial, agent_name="mini-swe-agent", prompt_tokens=120, completion_tokens=60
    )

    report = check_trace_readiness(trial)

    assert report["verdict"] == "interpretable"
    assert report["agent_name"] == "mini-swe-agent"
    assert report["ir_built"] is True
    assert report["ir_error"] is None


def test_missing_trajectory_is_uninterpretable(tmp_path: Path) -> None:
    trial = tmp_path / "trial-empty"
    trial.mkdir()

    report = check_trace_readiness(trial)

    assert report["verdict"] == "uninterpretable"
    assert report["atif_present"] is False
    assert report["steps"] == 0
    assert "missing_trajectory_file" in report["reasons"]


def test_zero_tokens_records_reason(tmp_path: Path) -> None:
    trial = tmp_path / "trial-zerotok"
    trial.mkdir()
    _write_atif(
        trial, agent_name="deepseek-harness", prompt_tokens=0, completion_tokens=0
    )

    report = check_trace_readiness(trial)

    assert report["steps"] > 0
    assert report["prompt_tokens"] == 0
    assert report["completion_tokens"] == 0
    assert "zero_token_metrics" in report["reasons"]


def test_corrupt_json_never_raises(tmp_path: Path) -> None:
    trial = tmp_path / "trial-corrupt"
    agent_dir = trial / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "trajectory.json").write_text("{not valid json", encoding="utf-8")
    before = _snapshot(trial)

    report = check_trace_readiness(trial)

    assert report["verdict"] in ("degraded", "uninterpretable")
    assert report["atif_present"] is True
    assert len(report["reasons"]) > 0
    assert _snapshot(trial) == before


def test_main_prints_json_for_trial_dir(
    tmp_path: Path, capsys: Any
) -> None:
    trial = tmp_path / "trial-cli"
    trial.mkdir()
    _write_atif(
        trial, agent_name="deepseek-harness", prompt_tokens=300, completion_tokens=150
    )

    assert trace_readiness.main([str(trial)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["verdict"] == "interpretable"
    assert payload["trial_dir"] == str(trial)
