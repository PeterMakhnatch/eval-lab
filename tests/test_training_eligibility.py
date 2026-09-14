from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.tracing import TraceError
from evallab.training_eligibility import evaluate_job, evaluate_trial, main


def _trial(path: Path, steps: list[dict[str, Any]]) -> Path:
    agent = path / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "synthetic-eligibility",
                "agent": {"name": "fixture", "model_name": "must-not-fill-missing-step"},
                "steps": steps,
            }
        )
    )
    return path


def test_single_actor_text_and_tool_calls_are_sft_not_online_rl(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path / "trial",
        [
            {"source": "user", "message": "Solve", "metrics": {"prompt_tokens": 999}},
            {
                "source": "agent",
                "message": "",
                "model_name": "deepseek-fixture",
                "reasoning_effort": "high",
                "tool_calls": [
                    {"tool_call_id": "call-1", "function_name": "exec", "arguments": {}}
                ],
                "metrics": {"prompt_tokens": 10, "completion_tokens": 2},
            },
            {
                "source": "agent",
                "message": "Solved",
                "model_name": "deepseek-fixture",
                "metrics": {"prompt_tokens": 14},
            },
        ],
    )
    verdict = evaluate_trial(trial)
    assert verdict["sft"] == {"eligible": True, "reasons": []}
    assert verdict["online_rl"] == {
        "eligible": False,
        "capture_present": False,
        "reasons": ["missing_behavior_policy_probabilities"],
    }
    assert verdict["identity"] == {
        "model_name": "deepseek-fixture",
        "reasoning_effort": "high",
        "tool_protocol": "function_calls",
    }
    assert verdict["usage"] == {"prompt_tokens": 24, "completion_tokens": 2}


@pytest.mark.parametrize(
    ("second_model", "reason"),
    [("other-actor", "mixed_actor_identity"), (None, "missing_model_name")],
)
def test_every_agent_step_needs_the_same_identity(
    tmp_path: Path, second_model: str | None, reason: str
) -> None:
    trial = _trial(
        tmp_path / "trial",
        [
            {"source": "agent", "message": "Answer", "model_name": "actor"},
            {"source": "agent", "message": "", "model_name": second_model},
        ],
    )
    verdict = evaluate_trial(trial)
    assert verdict["sft"] == {"eligible": False, "reasons": [reason]}
    assert verdict["identity"]["model_name"] is None


@pytest.mark.parametrize("marker", ["oracle_file", "nop_config", "oracle_result"])
@pytest.mark.parametrize("trajectory_present", [False, True])
def test_controls_are_not_model_training_evidence(
    tmp_path: Path, marker: str, trajectory_present: bool
) -> None:
    (tmp_path / "agent").mkdir()
    if marker == "oracle_file":
        (tmp_path / "agent" / "oracle.txt").write_text("control solution")
    elif marker == "oracle_result":
        (tmp_path / "result.json").write_text(json.dumps({"agent_info": {"name": "oracle"}}))
    else:
        (tmp_path / "config.json").write_text(json.dumps({"agent": {"name": "nop"}}))
    if trajectory_present:
        (tmp_path / "agent" / "trajectory.json").write_text(
            json.dumps({"steps": [{"source": "agent", "message": "Copied", "model_name": "m"}]})
        )
    verdict = evaluate_trial(tmp_path)
    assert verdict["control_trial"] is True
    assert verdict["trajectory_present"] is trajectory_present
    assert verdict["sft"]["eligible"] is False
    assert "control_trial_no_model" in verdict["sft"]["reasons"]
    assert verdict["online_rl"]["eligible"] is False
    if not trajectory_present:
        assert verdict["identity"] == {
            "model_name": None,
            "reasoning_effort": None,
            "tool_protocol": None,
        }


def test_nested_capture_is_never_qualified_from_its_presence(tmp_path: Path) -> None:
    trial = _trial(tmp_path / "trial", [{"source": "agent", "message": "A", "model_name": "m"}])
    capture = trial / "agent" / "session" / "proxy_capture.json"
    capture.parent.mkdir()
    capture.write_text("not inspected by this verdict")
    assert evaluate_trial(trial)["online_rl"] == {
        "eligible": False,
        "capture_present": True,
        "reasons": ["capture_present_unqualified"],
    }


def test_unknown_usage_stays_null_but_recorded_zero_stays_zero(tmp_path: Path) -> None:
    trial = _trial(tmp_path / "unknown", [{"source": "agent", "message": "A", "model_name": "m"}])
    verdict = evaluate_trial(trial)
    assert verdict["usage"] == {"prompt_tokens": None, "completion_tokens": None}
    assert verdict["identity"]["reasoning_effort"] is None
    assert verdict["identity"]["tool_protocol"] == "text_only"
    zero = _trial(
        tmp_path / "zero",
        [{"source": "agent", "message": "A", "model_name": "m", "metrics": {"prompt_tokens": 0}}],
    )
    assert evaluate_trial(zero)["usage"] == {"prompt_tokens": 0, "completion_tokens": None}


@pytest.mark.parametrize("metrics", [None, {}])
def test_null_or_empty_metrics_leave_usage_unknown(tmp_path: Path, metrics: Any) -> None:
    trial = _trial(
        tmp_path / "trial",
        [{"source": "agent", "message": "A", "model_name": "m", "metrics": metrics}],
    )
    verdict = evaluate_trial(trial)
    assert verdict["usage"] == {"prompt_tokens": None, "completion_tokens": None}
    assert verdict["sft"]["eligible"] is True
    assert verdict["online_rl"]["eligible"] is False


@pytest.mark.parametrize("metrics", [False, 0, "", [], True, 1, "invalid", [1]])
def test_non_object_metrics_fail_without_publishing_a_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], metrics: Any
) -> None:
    trial = _trial(
        tmp_path / "trial",
        [{"source": "agent", "message": "A", "model_name": "m", "metrics": metrics}],
    )
    output = tmp_path / "verdicts.json"
    with pytest.raises(TraceError, match="metrics must be an object"):
        evaluate_trial(trial)
    with pytest.raises(SystemExit) as exc:
        main([str(trial), "--json-out", str(output)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "metrics must be an object" in captured.err
    assert not output.exists()


@pytest.mark.parametrize(
    "steps",
    [
        [{"source": "user", "message": "A"}],
        [{"source": "agent", "message": "", "model_name": "m"}],
        [{"source": "agent", "message": [{"type": "text", "text": " "}], "model_name": "m"}],
    ],
)
def test_only_nonempty_actor_output_supports_sft(
    tmp_path: Path, steps: list[dict[str, Any]]
) -> None:
    trial = _trial(tmp_path / "trial", steps)
    assert evaluate_trial(trial)["sft"] == {"eligible": False, "reasons": ["no_agent_steps"]}


@pytest.mark.parametrize(
    "output",
    [
        {"message": 42},
        {"message": [{"type": "text", "text": 42}]},
        {"message": [{"type": "image", "source": {"path": "unknown.png"}}]},
        {"tool_calls": "not-a-call-list"},
        {"tool_calls": ["not-a-call"]},
        {"tool_calls": [{"tool_call_id": "c", "function_name": "exec"}]},
        {"tool_calls": [{"tool_call_id": "", "function_name": "exec", "arguments": {}}]},
        {"tool_calls": [{"tool_call_id": "c", "function_name": "exec", "arguments": "{}"}]},
    ],
)
def test_malformed_output_cannot_be_hidden_by_valid_actor_text(
    tmp_path: Path, output: dict[str, Any]
) -> None:
    trial = _trial(
        tmp_path / "trial",
        [
            {"source": "agent", "model_name": "m", **output},
            {"source": "agent", "model_name": "m", "message": "Valid sibling"},
        ],
    )
    verdict = evaluate_trial(trial)
    assert verdict["sft"] == {"eligible": False, "reasons": ["unsupported_agent_output"]}
    assert verdict["identity"]["tool_protocol"] == "text_only"
    assert verdict["online_rl"]["eligible"] is False


def test_atif_text_parts_are_supported_without_inventing_provenance(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path / "trial",
        [{"source": "agent", "model_name": "m", "message": [{"type": "text", "text": "Answer"}]}],
    )
    verdict = evaluate_trial(trial)
    assert verdict["sft"]["eligible"] is True
    assert verdict["identity"]["tool_protocol"] == "text_only"
    assert verdict["identity"]["reasoning_effort"] is None
    assert verdict["usage"] == {"prompt_tokens": None, "completion_tokens": None}


def test_job_output_is_sorted_and_cli_writes_the_same_verdicts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "a-missing"
    missing.mkdir()
    (missing / "trial.log").touch()
    valid = _trial(tmp_path / "z-valid", [{"source": "agent", "message": "A", "model_name": "m"}])
    malformed = _trial(
        tmp_path / "b-malformed", [{"source": "agent", "message": 42, "model_name": "m"}]
    )
    (tmp_path / "job-artifacts").mkdir()
    verdicts = evaluate_job(tmp_path)
    assert [row["trial_dir"] for row in verdicts] == [str(missing), str(malformed), str(valid)]
    assert verdicts[0]["sft"] == {"eligible": False, "reasons": ["no_trajectory"]}
    assert verdicts[0]["control_trial"] is False
    assert verdicts[1]["sft"] == {
        "eligible": False,
        "reasons": ["no_agent_steps", "unsupported_agent_output"],
    }
    assert verdicts[1]["identity"]["tool_protocol"] is None
    output = tmp_path / "verdicts.json"
    assert main([str(tmp_path), "--json-out", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_text()) == verdicts


@pytest.mark.parametrize(
    "destination", ["report", "trajectory", "symlink", "hardlink", "parent_symlink"]
)
def test_cli_refuses_existing_outputs_and_evidence_aliases_without_changing_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], destination: str
) -> None:
    trial = _trial(tmp_path / "trial", [{"source": "agent", "message": "A", "model_name": "m"}])
    trajectory = trial / "agent" / "trajectory.json"
    source_bytes = trajectory.read_bytes()
    output = tmp_path / "verdicts.json"
    if destination == "report":
        output.write_bytes(b"previous report\n")
    elif destination == "trajectory":
        output = trajectory
    elif destination == "symlink":
        output.symlink_to(trajectory)
    elif destination == "hardlink":
        output.hardlink_to(trajectory)
    else:
        alias = tmp_path / "agent-alias"
        alias.symlink_to(trajectory.parent, target_is_directory=True)
        output = alias / trajectory.name
    output_bytes = output.read_bytes()

    with pytest.raises(SystemExit) as exc:
        main([str(trial), "--json-out", str(output)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(output) in captured.err
    assert output.read_bytes() == output_bytes
    assert trajectory.read_bytes() == source_bytes
    if destination == "symlink":
        assert output.is_symlink()


def test_cli_refuses_dangling_output_symlink_without_creating_its_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trial = _trial(tmp_path / "trial", [{"source": "agent", "message": "A", "model_name": "m"}])
    output = tmp_path / "verdicts.json"
    target = tmp_path / "not-created.json"
    output.symlink_to(target)

    with pytest.raises(SystemExit) as exc:
        main([str(trial), "--json-out", str(output)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(output) in captured.err
    assert output.is_symlink()
    assert not target.exists()


def test_invalid_json_is_not_reported_as_missing_evidence(tmp_path: Path) -> None:
    trial = _trial(tmp_path / "trial", [])
    (trial / "agent" / "trajectory.json").write_text("{broken")
    with pytest.raises(TraceError, match="not valid JSON"):
        evaluate_trial(trial)
