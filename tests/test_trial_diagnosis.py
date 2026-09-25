"""Fixture-based tests for deterministic trial diagnosis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.trial_diagnosis import (
    DEFAULT_MAX_CHARS,
    TRUNCATION_MARKER,
    diagnose_job,
    diagnose_trial,
    render_diagnosis_text,
    sanitize_excerpt,
)


def _write_trial(
    path: Path,
    steps: list[dict[str, Any]],
    *,
    reward: float | None = 0.0,
    exception: str | None = None,
    trial_name: str = "fixture-trial",
) -> Path:
    agent = path / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "synthetic-diagnosis",
                "agent": {"name": "fixture", "model_name": "fixture-model"},
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "id": "fixture-id",
        "trial_name": trial_name,
        "task_name": "fixture-task",
        "config": {"agent": {"name": "fixture"}},
    }
    if reward is not None:
        result["verifier_result"] = {"rewards": {"reward": reward}}
    if exception is not None:
        result["exception_info"] = {"exception_type": exception}
    (path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return path


def _agent_step(
    message: str = "",
    *,
    command: str | None = None,
    output: str | None = None,
    returncode: int = 0,
) -> dict[str, Any]:
    step: dict[str, Any] = {"source": "agent", "message": message}
    if command is not None:
        step["tool_calls"] = [{"function_name": "bash", "arguments": {"command": command}}]
        content = (
            json.dumps({"returncode": returncode, "output": output or ""})
            if output is not None or returncode != 0
            else json.dumps({"returncode": 0, "output": "ok"})
        )
        step["observation"] = {"results": [{"content": content}]}
    return step


def _modes(trial: Path) -> list[str]:
    return [mode.mode for mode in diagnose_trial(trial).modes]


def test_infra_failed_trials_carry_no_modes(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [_agent_step("working", command="pytest -q", output="boom", returncode=1)],
        reward=0.0,
        exception="HarborTimeout",
    )
    diagnosis = diagnose_trial(trial)
    assert diagnosis.outcome == "infra_failed"
    assert diagnosis.exception_class == "HarborTimeout"
    assert diagnosis.modes == ()


def test_unscored_trials_carry_no_modes(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [_agent_step("working", command="pytest -q", output="boom", returncode=1)],
        reward=None,
    )
    diagnosis = diagnose_trial(trial)
    assert diagnosis.outcome == "unscored"
    assert diagnosis.reward is None
    assert diagnosis.modes == ()


def test_scored_pass_carries_no_modes(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [_agent_step("done", command="pytest -q", output="3 passed")],
        reward=1.0,
    )
    diagnosis = diagnose_trial(trial)
    assert diagnosis.outcome == "scored"
    assert diagnosis.modes == ()
    assert any("pass" in notice for notice in diagnosis.notices)


def test_no_tool_use_points_at_terminal_agent_step(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            {"source": "user", "message": "Fix it"},
            {"source": "agent", "message": "I will think about this at length."},
        ],
    )
    diagnosis = diagnose_trial(trial)
    assert [mode.mode for mode in diagnosis.modes] == ["no_tool_use"]
    assert diagnosis.modes[0].step_ids == (2,)


def test_tool_use_loop_cites_repeated_command(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("try", command="pytest -q cases", output="1 failed"),
            _agent_step("retry", command="pytest -q cases", output="1 failed"),
            _agent_step("retry again", command="pytest -q cases", output="1 failed"),
        ],
    )
    diagnosis = diagnose_trial(trial)
    assert "tool_use_loop" in [mode.mode for mode in diagnosis.modes]
    loop = next(mode for mode in diagnosis.modes if mode.mode == "tool_use_loop")
    assert loop.step_ids == (1, 2, 3)
    assert "pytest" in loop.excerpt


def test_empty_terminal_reply(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            {"source": "user", "message": "Solve"},
            _agent_step("", command="pytest -q", output="exit 0"),
            {"source": "agent", "message": "   "},
        ],
    )
    assert "empty_terminal_reply" in _modes(trial)


def test_any_nonempty_reply_suppresses_empty_reply(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("trying", command="pytest -q", output="1 failed"),
            {"source": "agent", "message": "   "},
        ],
    )
    assert "empty_terminal_reply" not in _modes(trial)


def test_repl_exit_zero_counts_as_silence(tmp_path: Path) -> None:
    def repl_step(code: str) -> dict[str, Any]:
        return {
            "source": "agent",
            "message": "",
            "tool_calls": [{"function_name": "execute", "arguments": {"code": code}}],
            "observation": {"results": [{"content": "exit 0\n"}]},
        }

    trial = _write_trial(
        tmp_path / "trial",
        [repl_step("print('hi')"), repl_step("print('again')")],
    )
    assert "silent_tool_output" in _modes(trial)


def test_planning_skipped_for_repl_tools(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            {
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {
                        "function_name": "execute",
                        "arguments": {"code": "open('/tmp/x.csv').read()"},
                    }
                ],
                "observation": {"results": [{"content": "exit 0\n"}]},
            },
        ],
    )
    assert "planning_no_edit" not in _modes(trial)


def test_multi_output_exit_zero_step_is_silent(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            {
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {"function_name": "execute", "arguments": {"code": "f()"}},
                    {"function_name": "execute", "arguments": {"code": "g()"}},
                ],
                "observation": {
                    "results": [
                        {"content": "wrote 321 characters to f.py"},
                        {"content": "exit 0\n"},
                    ]
                },
            },
        ],
    )
    assert "silent_tool_output" in _modes(trial)


def test_run_bash_redirect_counts_as_edit(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            {
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {
                        "function_name": "run_bash",
                        "arguments": {"command": "echo 'a,b' > /tmp/data.csv"},
                    }
                ],
                "observation": {"results": [{"content": "exit 0\n"}]},
            },
        ],
    )
    assert "planning_no_edit" not in _modes(trial)


def test_malformed_json_read_back_is_a_mode(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "write", command="echo -n '{' > errors.json", output="ok"
            ),
            _agent_step(
                "read",
                command="cat errors.json",
                output="New Terminal Output: root@x:/w# cat errors.json "
                "{auth: 3, payments: 4}root@x:/w#",
            ),
        ],
    )
    modes = _modes(trial)
    assert "malformed_artifact" in modes


def test_write_with_embedded_cat_is_not_a_read_back(tmp_path: Path) -> None:
    # Mirrors the har71-local-baseline false positive: an awk program span in
    # the output of an echo-write (with an embedded $(cat)) is not a
    # malformed JSON artifact.
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "write",
                command="echo 'v $(cat /tmp/a.txt)' >> /tmp/summary.json",
                output="root@x:/w# echo 'v 1' >> /tmp/summary.json "
                "awk '{for(i=1;i<=NR;i++) print}' root@x:/w#",
            ),
        ],
    )
    assert "malformed_artifact" not in _modes(trial)


def test_valid_json_read_back_is_not_a_mode(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "read",
                command="cat package.json",
                output='{"name": "x", "version": 1}',
            ),
            _agent_step(
                "fail",
                command="pytest -q",
                output="1 failed",
                returncode=1,
            ),
        ],
    )
    assert "malformed_artifact" not in _modes(trial)


def test_expected_silence_is_not_a_mode(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("write", command="cat <<'EOF' > fix.py\npass\nEOF", output=""),
            _agent_step("edit", command="sed -i 's/a/b/' fix.py", output=""),
            _agent_step("run", command="python fix.py", output="done"),
        ],
    )
    assert "silent_tool_output" not in _modes(trial)


def test_unexpected_silence_is_a_mode(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("check", command="python verify_fix.py", output=""),
            _agent_step("check again", command="python verify_fix.py", output=""),
        ],
    )
    assert "silent_tool_output" in _modes(trial)


def test_wrong_tool_arguments(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "run",
                command="pytest --unknown-flag",
                output="error: unrecognized argument --unknown-flag",
                returncode=2,
            ),
        ],
    )
    assert "wrong_tool_arguments" in _modes(trial)


def test_state_persistence_nameerror(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "prepare",
                command="cat <<'EOF' > prep.py\ndf = load_data()\nEOF",
                output="",
            ),
            _agent_step(
                "compute",
                command="python -c 'print(df.describe())'",
                output="NameError: name 'df' is not defined",
                returncode=1,
            ),
        ],
    )
    assert "state_persistence_assumption" in _modes(trial)


def test_nameerror_without_prior_definition_is_not_state_loss(
    tmp_path: Path,
) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "compute",
                command="python analyze.py",
                output="NameError: name 'df' is not defined",
                returncode=1,
            ),
        ],
    )
    assert "state_persistence_assumption" not in _modes(trial)


def test_state_persistence_bare_cd_then_relative_failure(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("move", command="cd /tmp/workdir", output="ok"),
            _agent_step(
                "read",
                command="cat results.json",
                output="cat: results.json: No such file or directory",
                returncode=1,
            ),
        ],
    )
    assert "state_persistence_assumption" in _modes(trial)


def test_planning_no_edit_without_file_writes(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("look", command="ls -la /tmp/work", output="total 0"),
            _agent_step("check", command="python --version", output="3.12"),
        ],
    )
    assert "planning_no_edit" in _modes(trial)


def test_planning_no_edit_skipped_without_file_task(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("look", command="ls -la", output="total 0"),
            _agent_step("check", command="python --version", output="3.12"),
        ],
    )
    assert "planning_no_edit" not in _modes(trial)


def test_unrecovered_terminal_error(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("run", command="cat <<'EOF' > fix.py\npass\nEOF", output=""),
            _agent_step(
                "test",
                command="pytest -q",
                output="1 failed",
                returncode=1,
            ),
        ],
    )
    assert "unrecovered_error" in _modes(trial)


def test_unclassified_fallback_stays_honest(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("write", command="cat <<'EOF' > fix.py\npass\nEOF", output=""),
            _agent_step("done", command="echo FINISHED", output="FINISHED"),
            {"source": "agent", "message": "Submitted the fix."},
        ],
    )
    assert _modes(trial) == ["unclassified_failure"]


def test_hidden_paths_never_leak_into_feedback(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "run",
                command="pytest --bad-flag",
                output=(
                    "error: unrecognized argument; see tests/test_hidden.py and solution/patch.diff"
                ),
                returncode=2,
            ),
        ],
    )
    diagnosis = diagnose_trial(trial)
    text = render_diagnosis_text(diagnosis)
    assert "tests/test_hidden.py" not in text
    assert "solution/patch.diff" not in text
    assert "[hidden-path]" in text
    payload = json.dumps(diagnosis.to_dict())
    assert "tests/test_hidden.py" not in payload
    assert "solution/patch.diff" not in payload


def test_sanitize_excerpt_redacts_secrets_and_bounds() -> None:
    long_text = "token sk-abcdefgh12345678 plus " + ("word " * 100)
    excerpt = sanitize_excerpt(long_text, 60)
    assert "sk-abcdefgh12345678" not in excerpt
    assert "[redacted]" in excerpt
    assert len(excerpt) <= 63


def test_rendering_is_bounded_and_marked(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "x" * 5000,
                command="pytest --bad-flag",
                output="error: unrecognized argument " + "y" * 5000,
                returncode=2,
            )
        ],
    )
    diagnosis = diagnose_trial(trial)
    full = render_diagnosis_text(diagnosis)
    assert len(full) <= DEFAULT_MAX_CHARS
    short = render_diagnosis_text(diagnosis, 120)
    assert len(short) <= 120
    assert TRUNCATION_MARKER in short
    with pytest.raises(ValueError):
        render_diagnosis_text(diagnosis, 0)


def test_diagnosis_is_deterministic_and_json_serializable(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step("try", command="pytest -q cases", output="1 failed"),
            _agent_step("retry", command="pytest -q cases", output="1 failed"),
            _agent_step("retry again", command="pytest -q cases", output="1 failed"),
            {"source": "agent", "message": ""},
        ],
    )
    first = diagnose_trial(trial).to_dict()
    second = diagnose_trial(trial).to_dict()
    assert first == second
    json.dumps(first)
    order = [mode["mode"] for mode in first["modes"]]
    assert order == sorted(order, key=_mode_rank)


def _mode_rank(name: str) -> int:
    from evallab.trial_diagnosis import MODE_ORDER

    return MODE_ORDER.index(name)


def test_missing_trial_dir_is_infra_failed(tmp_path: Path) -> None:
    diagnosis = diagnose_trial(tmp_path / "absent")
    assert diagnosis.outcome == "infra_failed"
    assert diagnosis.modes == ()


def test_diagnose_job_covers_sorted_trials(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text("{}")
    _write_trial(
        job / "b-trial",
        [_agent_step("done", command="pytest -q", output="ok")],
        reward=1.0,
        trial_name="b-trial",
    )
    _write_trial(
        job / "a-trial",
        [{"source": "agent", "message": "thinking"}],
        reward=0.0,
        trial_name="a-trial",
    )
    diagnoses = diagnose_job(job)
    assert [item.trial_name for item in diagnoses] == ["a-trial", "b-trial"]
    assert diagnoses[0].outcome == "scored"


def test_trial_without_trajectory_file_is_scored_without_modes(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "no-traj",
                "trial_name": "no-traj",
                "task_name": "fixture-task",
                "config": {"agent": {"name": "oracle"}},
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    diagnosis = diagnose_trial(trial)
    assert diagnosis.outcome == "scored"
    assert diagnosis.modes == ()
    assert any("trajectory" in notice for notice in diagnosis.notices)


def test_later_tool_call_in_step_counts_as_edit(tmp_path: Path) -> None:
    step = _agent_step("inspect", command="ls -la /tmp/work", output="total 0")
    step["tool_calls"].append(
        {
            "function_name": "bash",
            "arguments": {"command": "sqlite3 db 'UPDATE t SET x = 1'"},
        }
    )
    trial = _write_trial(tmp_path / "trial", [step])
    assert "planning_no_edit" not in [mode.mode for mode in diagnose_trial(trial).modes]


def test_traceback_outputs_are_not_wrong_tool_arguments(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path / "trial",
        [
            _agent_step(
                "debug",
                command="python -c 'import subprocess'",
                output=(
                    "Traceback (most recent call last):\n"
                    '  File "<string>", line 1, in <module>\n'
                    "TypeError: __init__() got an unexpected keyword argument"
                ),
                returncode=1,
            ),
        ],
    )
    assert "wrong_tool_arguments" not in [mode.mode for mode in diagnose_trial(trial).modes]


def test_diagnose_atif_matches_trial_dir(tmp_path: Path) -> None:
    from evallab.trial_diagnosis import diagnose_atif, diagnose_trial

    steps = [
        _agent_step(
            "run",
            command="pytest --bad-flag",
            output="error: unrecognized argument --bad-flag",
            returncode=2,
        ),
    ]
    trial = _write_trial(tmp_path / "trial", steps)
    from_dir = diagnose_trial(trial)
    trajectory = json.loads((trial / "agent" / "trajectory.json").read_text())
    from_doc = diagnose_atif(
        trajectory,
        trial_id="fixture-id",
        trial_name="fixture-trial",
        task_name="fixture-task",
        agent_name="fixture",
        model_name="fixture-model",
        reward=0.0,
    )
    assert from_doc.outcome == "scored"
    assert [mode.mode for mode in from_doc.modes] == [
        mode.mode for mode in from_dir.modes
    ]
    assert json.dumps(from_doc.to_dict())


def test_diagnose_atif_outcome_branches() -> None:
    from evallab.trial_diagnosis import diagnose_atif

    trajectory: dict[str, Any] = {"steps": []}
    assert (
        diagnose_atif(trajectory, trial_id="t", trial_name="t", reward=1.0).modes
        == ()
    )
    assert (
        diagnose_atif(trajectory, trial_id="t", trial_name="t").outcome == "unscored"
    )
    assert (
        diagnose_atif(
            trajectory, trial_id="t", trial_name="t", reward=0.0,
            exception_class="Boom",
        ).outcome
        == "infra_failed"
    )


def test_cli_text_and_json_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from evallab.trial_diagnosis import main

    trial = _write_trial(
        tmp_path / "trial",
        [_agent_step("done", command="pytest -q", output="3 passed")],
        reward=1.0,
    )
    assert main([str(trial)]) == 0
    text_out = capsys.readouterr().out
    assert "outcome=scored" in text_out
    assert main([str(trial), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["outcome"] == "scored"
    assert payload[0]["modes"] == []


def test_cli_rejects_unknown_path(tmp_path: Path) -> None:
    from evallab.trial_diagnosis import main

    with pytest.raises(SystemExit):
        main([str(tmp_path / "absent")])
