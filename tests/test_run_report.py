"""Run report: revisit semantics, status channels, cost provenance, subagents, rollups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.cli import run_cli
from evallab.interpretation.run_report import build_job_report, build_run_report, write_reports

FIXTURES = Path(__file__).parent / "fixtures" / "terminus2" / "job-terminus2"


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "trial-id",
        "trial_name": "trial",
        "task_name": "lab/task",
        "config": {"agent": {"name": "mini-swe-agent", "model_name": "zai/glm"}},
        "agent_info": {"name": "mini-swe-agent", "version": "2.4.6"},
        "agent_result": {
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": 0.5,
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-09-01T00:00:00Z",
        "finished_at": "2026-09-01T00:10:00Z",
        "environment_setup": {
            "started_at": "2026-09-01T00:00:00Z",
            "finished_at": "2026-09-01T00:01:00Z",
        },
        "agent_setup": {
            "started_at": "2026-09-01T00:01:00Z",
            "finished_at": "2026-09-01T00:02:00Z",
        },
        "agent_execution": {
            "started_at": "2026-09-01T00:02:00Z",
            "finished_at": "2026-09-01T00:09:00Z",
        },
        "verifier": {
            "started_at": "2026-09-01T00:09:00Z",
            "finished_at": "2026-09-01T00:09:30Z",
        },
    }
    result.update(overrides)
    return result


def _bash(step_id: int, second: int, command: str, output: str, code: int = 0, **metrics: Any) -> dict[str, Any]:
    call_id = f"call-{step_id}"
    return {
        "step_id": step_id,
        "timestamp": f"2026-09-01T00:02:{second:02d}Z",
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": call_id, "function_name": "bash", "arguments": {"command": command}}
        ],
        "observation": {
            "results": [
                {
                    "source_call_id": call_id,
                    "content": json.dumps({"returncode": code, "output": output}),
                }
            ]
        },
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 100, **metrics},
    }


def _trial(
    root: Path,
    steps: list[dict[str, Any]],
    *,
    result: dict[str, Any] | None = None,
    name: str = "trial",
    final_metrics: dict[str, Any] | None = None,
    extra_docs: dict[str, Any] | None = None,
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    body = result if result is not None else _result()
    body = {**body, "trial_name": name}
    (trial / "result.json").write_text(json.dumps(body), encoding="utf-8")
    doc: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "mini-swe-agent", "version": "2.4.6", "model_name": "zai/glm"},
        "steps": [
            {"step_id": 0, "source": "user", "message": "Fix the bug", "timestamp": "2026-09-01T00:02:00Z"},
            *steps,
        ],
    }
    if final_metrics is not None:
        doc["final_metrics"] = final_metrics
    (agent / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    for relative, payload in (extra_docs or {}).items():
        (agent / relative).write_text(json.dumps(payload), encoding="utf-8")
    return trial


def test_revisits_separate_returns_immediate_repeats_and_exact_spots(tmp_path: Path) -> None:
    steps = [
        _bash(1, 5, "cat app.py", "print('v1')  # the original file contents"),
        _bash(2, 10, "ls", "app.py tests/ README.md and other files here"),
        _bash(3, 15, "cat app.py", "print('v1')  # the original file contents"),
        _bash(4, 20, "sed -i s/v1/v2/ app.py", ""),
        _bash(5, 25, "cat app.py", "print('v2')  # the edited file contents now"),
        _bash(6, 30, "cat app.py", "print('v2')  # the edited file contents now"),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    revisits = report["revisits"]

    assert revisits["actions_considered"] == 6
    assert revisits["repeated_actions"] == 3
    # step 3 and step 5 came back to `cat app.py` after doing something else;
    # step 6 repeated step 5 immediately.
    assert revisits["returns_to_earlier_action"] == 2
    assert revisits["consecutive_repeats"] == 1
    # Exact revisits need an identical result: step 3 (v1 again) and step 6 (v2 again),
    # but not step 5, whose result changed after the edit.
    assert revisits["exact_revisits"] == 2
    assert revisits["most_repeated"][0]["count"] == 4
    assert revisits["most_repeated"][0]["steps"] == [2, 4, 6, 7]
    flagged = [e["step"] for e in report["timeline"]["entries"] if "revisit" in e.get("flags", [])]
    assert flagged == [4, 6, 7]


def test_terminal_polls_are_not_revisits(tmp_path: Path) -> None:
    def keystrokes(step_id: int, second: int, keys: str) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "timestamp": f"2026-09-01T00:02:{second:02d}Z",
            "source": "agent",
            "message": "",
            "tool_calls": [
                {"tool_call_id": f"c{step_id}", "function_name": "bash_command",
                 "arguments": {"keystrokes": keys, "duration": 5}}
            ],
            "observation": {"results": [{"content": "root@box:/app# still running..."}]},
        }

    steps = [keystrokes(1, 1, "make\n"), keystrokes(2, 7, ""), keystrokes(3, 13, ""), keystrokes(4, 19, "")]
    report = build_run_report(_trial(tmp_path, steps))

    assert report["tools"]["polls"] == 3
    assert report["revisits"]["repeated_actions"] == 0
    assert report["errors"]["status_unknown_calls"] == 4


def test_codex_code_mode_is_unwrapped_and_script_status_drives_errors(tmp_path: Path) -> None:
    def code_mode(step_id: int, cmd: str, status: str, body: str) -> dict[str, Any]:
        source = f"const r = await tools.exec_command({json.dumps({'cmd': cmd})});\ntext(r.output);"
        content = str(
            [
                {"type": "input_text", "text": f"Script {status}\nWall time 0.2 seconds\nOutput:\n"},
                {"type": "input_text", "text": body},
            ]
        )
        return {
            "step_id": step_id,
            "timestamp": f"2026-09-01T00:02:{step_id:02d}Z",
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": f"c{step_id}", "function_name": "exec", "arguments": {"input": source}}],
            "observation": {"results": [{"source_call_id": f"c{step_id}", "content": content}]},
        }

    listing = "def load(body):\n    pass\n" * 40 + "    raise HTTPError(400, 'Invalid JSON')\n"
    steps = [
        code_mode(1, "sed -n '1,200p' bottle.py", "completed", listing),
        code_mode(2, "pytest -q", "failed", "Script error:\nexec_command failed for `pytest -q`"),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    tools = {row["tool"]: row for row in report["tools"]["by_tool"]}

    assert tools["exec_command"]["calls"] == 2
    assert report["tools"]["wrapped_calls"] == {"exec": 2}
    assert [p["program"] for p in report["tools"]["top_programs"]] == ["sed", "pytest"]
    assert report["errors"]["tool_errors"] == 1
    assert report["errors"]["examples"][0]["step"] == 3
    assert report["errors"]["examples"][0]["evidence"] == "script_status"
    assert report["errors"]["ended_in_error"] is True


def test_trailing_harness_notice_keeps_every_call_and_is_surfaced(tmp_path: Path) -> None:
    step = _bash(1, 5, "ls", "a")
    step["tool_calls"].append(
        {"tool_call_id": "second", "function_name": "bash", "arguments": {"command": "pwd"}}
    )
    step["observation"]["results"] = [
        {"content": json.dumps({"returncode": 0, "output": "a b c"})},
        {"content": json.dumps({"returncode": 2, "output": "pwd: bad option"})},
        {"content": "Your previous response reached the output token limit (finish_reason=length)."},
    ]
    report = build_run_report(_trial(tmp_path, [step]))

    assert report["tools"]["total_calls"] == 2
    assert report["errors"]["tool_errors"] == 1
    assert [e["kind"] for e in report["context"]["events"]] == ["harness_notice"]


def test_payload_status_wrapping_an_error_value_is_an_error(tmp_path: Path) -> None:
    def mcp(step_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "timestamp": f"2026-09-01T00:02:{step_id:02d}Z",
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": f"m{step_id}", "function_name": "memory_get",
                            "arguments": {"chunk_id": f"c{step_id}"}}],
            "observation": {"results": [{"source_call_id": f"m{step_id}", "content": json.dumps(payload)}]},
        }

    steps = [
        mcp(1, {"status": "ok", "value": {"text": "chunk body"}}),
        mcp(2, {"status": "ok", "value": {"error": "not_found"}}),
    ]
    row = build_run_report(_trial(tmp_path, steps))["tools"]["by_tool"][0]

    assert (row["ok"], row["errors"], row["unknown"]) == (1, 1, 0)


def test_expected_probe_miss_is_ok_but_counted(tmp_path: Path) -> None:
    steps = [_bash(1, 5, "grep -r TODO src", "", code=1)]
    errors = build_run_report(_trial(tmp_path, steps))["errors"]

    assert errors["tool_errors"] == 0
    assert errors["expected_probe_misses"] == 1


def test_usage_not_attributed_to_steps_is_flagged(tmp_path: Path) -> None:
    # result.json declares 300 output tokens; the two steps only carry 200.
    report = build_run_report(_trial(tmp_path, [_bash(1, 5, "ls", "x"), _bash(2, 9, "pwd", "/app")]))

    assert any("100 output tokens" in q for q in report["data_quality"])


def test_intercepted_submit_action_is_not_an_error(tmp_path: Path) -> None:
    submit = _bash(2, 10, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "")
    submit["observation"]["results"][0]["content"] = json.dumps(
        {"returncode": -1, "output": "", "exception_info": "action was not executed"}
    )
    report = build_run_report(_trial(tmp_path, [_bash(1, 5, "pytest", "3 passed"), submit]))

    assert report["errors"]["tool_errors"] == 0
    assert report["errors"]["ended_in_error"] is False
    assert report["errors"]["status_unknown_calls"] == 1


def test_cost_prefers_result_then_final_metrics_and_never_invents_zero(tmp_path: Path) -> None:
    steps = [_bash(1, 5, "ls", "a b c", cost_usd=0.1), _bash(2, 10, "pwd", "/app")]
    final = {"total_prompt_tokens": 2000, "total_completion_tokens": 200, "total_cost_usd": 0.25}

    from_result = build_run_report(_trial(tmp_path, steps, name="a", final_metrics=final))
    assert (from_result["cost"]["total_usd"], from_result["cost"]["source"]) == (0.5, "result_json")

    no_result_usage = _result(agent_result={})
    from_final = build_run_report(
        _trial(tmp_path, steps, name="b", result=no_result_usage, final_metrics=final)
    )
    assert (from_final["cost"]["total_usd"], from_final["cost"]["source"]) == (
        0.25,
        "trajectory_final_metrics",
    )
    assert from_final["tokens"]["input"] == 2000

    from_steps = build_run_report(_trial(tmp_path, steps, name="c", result=no_result_usage))
    assert from_steps["cost"]["source"] == "step_sum"
    assert from_steps["cost"]["is_lower_bound"] is True

    no_cost_steps = [_bash(1, 5, "ls", "a b c"), _bash(2, 10, "pwd", "/app")]
    unknown = build_run_report(_trial(tmp_path, no_cost_steps, name="d", result=no_result_usage))
    assert unknown["cost"]["total_usd"] is None
    assert any("cost unavailable" in q for q in unknown["data_quality"])


def test_token_disagreement_between_sources_is_reported(tmp_path: Path) -> None:
    final = {"total_prompt_tokens": 9000, "total_completion_tokens": 300}
    report = build_run_report(_trial(tmp_path, [_bash(1, 5, "ls", "x")], final_metrics=final))

    assert report["tokens"]["input"] == 3000
    assert any("input tokens disagree" in q for q in report["data_quality"])


def test_phase_timing_and_gaps_between_agent_steps(tmp_path: Path) -> None:
    steps = [
        _bash(1, 5, "ls", "x"),
        {"step_id": 2, "source": "user", "message": "keep going", "timestamp": "2026-09-01T00:02:40Z"},
        _bash(3, 50, "pwd", "/app"),
        _bash(4, 55, "id", "root"),
    ]
    timing = build_run_report(_trial(tmp_path, steps))["timing"]

    assert timing["total_seconds"] == 600
    assert timing["phases"]["agent_execution"]["seconds"] == 420
    assert timing["phases"]["verifier"]["starts_at_offset_seconds"] == 540
    assert timing["unaccounted_seconds"] == 30
    assert timing["offset_origin"] == "agent_execution.started_at"
    assert timing["first_agent_step_offset_seconds"] == 5
    # The user step in the middle does not split the agent's 45s gap into 35s + 10s.
    assert timing["step_gap_seconds"]["count"] == 2
    assert timing["step_gap_seconds"]["max"] == 45


def test_overlapping_phases_are_flagged_not_negative(tmp_path: Path) -> None:
    skewed = _result(
        environment_setup={
            "started_at": "2026-09-01T00:00:00Z",
            "finished_at": "2026-09-01T00:06:00Z",
        },
    )
    report = build_run_report(_trial(tmp_path, [_bash(1, 5, "ls", "x")], result=skewed))

    assert report["timing"]["unaccounted_seconds"] == 0
    assert any("overlap" in q for q in report["data_quality"])


def test_summarization_subagents_are_timelined(tmp_path: Path) -> None:
    report = build_run_report(FIXTURES / "trial-summarized")
    subagents = report["subagents"]

    assert subagents["observability"] == "captured"
    assert subagents["summarization_count"] == 3
    assert {item["spawned_at_step"] for item in subagents["items"]} == {5}
    assert all(item["steps"] and item["input_tokens"] for item in subagents["items"])
    assert any(e["kind"] == "summarization_subagent" for e in report["context"]["events"])


def test_sidechains_and_bare_delegations(tmp_path: Path) -> None:
    def side(step_id: int, agent_id: str) -> dict[str, Any]:
        step = _bash(step_id, step_id * 5, f"grep -r todo src/{step_id}", "src/a.py: TODO")
        step["extra"] = {"is_sidechain": True, "agent_id": agent_id}
        return step

    delegate = _bash(1, 2, "unused", "")
    delegate["tool_calls"] = [
        {"tool_call_id": "t1", "function_name": "Task", "arguments": {"prompt": "find todos"}}
    ]
    delegate["observation"] = {"results": [{"source_call_id": "t1", "content": "done"}]}
    steps = [delegate, side(2, "agent-a"), side(3, "agent-a"), side(4, "agent-b")]
    subagents = build_run_report(_trial(tmp_path, steps, name="claude"))["subagents"]

    assert subagents["observability"] == "captured"
    assert [(i["id"], i["steps"]) for i in subagents["items"]] == [("agent-a", 2), ("agent-b", 1)]
    assert [d["tool"] for d in subagents["delegation_calls"]] == ["Task"]

    only_calls = build_run_report(_trial(tmp_path, [delegate], name="codex"))["subagents"]
    assert only_calls["observability"] == "delegations_only"
    assert only_calls["count"] == 0


def test_control_agent_without_trajectory_is_accounted_not_zeroed(tmp_path: Path) -> None:
    trial = tmp_path / "oracle-trial"
    trial.mkdir()
    oracle = _result(
        agent_info={"name": "oracle"}, config={"agent": {"name": "oracle"}}, agent_result={}
    )
    (trial / "result.json").write_text(json.dumps({**oracle, "trial_name": "oracle-trial"}))
    report = build_run_report(trial)

    assert report["availability"]["trajectory"] == "absent"
    assert "control agent" in report["availability"]["reason"]
    assert report["cost"]["total_usd"] is None
    assert report["tokens"]["total"] is None
    assert not any("cost unavailable" in q for q in report["data_quality"])


def test_long_runs_keep_notable_steps_and_mark_omissions(tmp_path: Path) -> None:
    steps = [_bash(i, i % 60, f"echo step-{i}", f"step {i} output text long enough") for i in range(1, 121)]
    steps[69] = _bash(70, 10, "false", "boom", code=1)
    timeline = build_run_report(_trial(tmp_path, steps), timeline_limit=30)["timeline"]

    shown = [e["step"] for e in timeline["entries"] if "step" in e]
    assert timeline["total_steps"] == 121
    assert len(shown) <= 30
    assert 71 in shown  # the error step (chain ordinal 71) survives the cut
    assert any("omitted_steps" in e for e in timeline["entries"])
    assert len(timeline["windows"]) == 10


def test_job_rollup_and_cli_outputs(tmp_path: Path, capsys: Any) -> None:
    job = tmp_path / "job"
    _trial(job, [_bash(1, 5, "ls", "x")], name="passed-trial")
    failed = _result(verifier_result={"rewards": {"reward": 0.0}}, agent_result={})
    _trial(job, [_bash(1, 5, "ls", "x")], name="failed-trial", result=failed)
    multi = _result(verifier_result={"rewards": {"tests": 1.0, "style": 0.0}}, agent_result={})
    _trial(job, [_bash(1, 5, "ls", "x")], name="multi-metric-trial", result=multi)
    (job / "result.json").write_text(json.dumps({"n_total_trials": 3}))

    rollup, reports = build_job_report(job)
    assert len(reports) == 3
    # Several metrics without a primary `reward` still score the trial (here: partial).
    assert (rollup["passed"], rollup["scored"]) == (1, 3)
    assert rollup["verdicts"] == {"passed": 1, "failed": 1, "partial": 1}
    assert rollup["total_cost_usd"] == 0.5
    assert rollup["trials_without_cost"] == 2
    assert rollup["cost_per_pass_usd"] == 0.5

    out_dir = tmp_path / "out"
    code = run_cli(["report", "run", str(job), "--output-dir", str(out_dir)], workspace=tmp_path)
    assert code == 0
    capsys.readouterr()
    written = json.loads((out_dir / "job.run_report.json").read_text())
    assert written["schema"] == "evallab.job_report/v1"
    assert (out_dir / "passed-trial.run_report.md").is_file()

    code = run_cli(["report", "run", str(job / "passed-trial"), "--json"], workspace=tmp_path)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "evallab.run_report/v1"
    assert payload["outcome"]["verdict"] == "passed"


def test_report_files_cannot_escape_the_output_directory(tmp_path: Path) -> None:
    report = build_run_report(_trial(tmp_path / "runs", [_bash(1, 5, "ls", "x")]))
    report["identity"]["trial_dir"] = str(tmp_path / "runs" / "..")  # hostile directory name
    report["identity"]["trial_name"] = "../../evil"

    out_dir = tmp_path / "out"
    written = write_reports([report], out_dir)

    assert all(path.resolve().parent == out_dir.resolve() for path in written)


def test_subagent_reference_outside_the_trial_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "x"}]}))
    step = _bash(1, 5, "summarize", "done")
    step["observation"]["results"][0]["subagent_trajectory_ref"] = [
        {"trajectory_path": "../../secret.json"}
    ]
    item = build_run_report(_trial(tmp_path / "runs", [step]))["subagents"]["items"][0]

    assert item["location"] == "escaping"
    assert item["captured"] is False


def test_status_channels_beyond_exit_codes(tmp_path: Path) -> None:
    def raw(step_id: int, name: str, args: dict[str, Any], content: str, **call: Any) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "timestamp": f"2026-09-01T00:02:{step_id:02d}Z",
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": f"c{step_id}", "function_name": name, "arguments": args, **call}],
            "observation": {"results": [{"source_call_id": f"c{step_id}", "content": content}]},
        }

    wait_timeout = str(
        [
            {"type": "input_text", "text": "Script completed\nWall time 300.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({"isError": True, "content": "timed out"})},
        ]
    )
    steps = [
        raw(1, "execute", {"code": "print(1)"}, "exit 0\n1"),
        raw(2, "execute", {"code": "boom()"}, "exit 1\nNameError: boom"),
        raw(3, "exec", {"input": 'await tools.wait({"ms": 1})'}, wait_timeout),
        raw(4, "Read", {"file_path": "a.py"}, "contents", extra={"tool_result_is_error": True}),
    ]
    report = build_run_report(_trial(tmp_path, steps))
    evidence = {(e["step"], e["evidence"]) for e in report["errors"]["examples"]}

    assert evidence == {(3, "exit_prefix"), (4, "result_payload"), (5, "error_flag")}
    assert report["tools"]["by_tool"][0] == {**report["tools"]["by_tool"][0], "ok": 1, "errors": 1}
