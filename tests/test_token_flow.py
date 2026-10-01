"""Regression tests for evallab.token_flow (HAR-114 items 1-2)."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.process_job import process_job
from evallab.token_flow import analyze_token_flow


def _call(keystrokes: str) -> dict:
    return {
        "tool_call_id": "call_0",
        "function_name": "bash_command",
        "arguments": {"keystrokes": keystrokes, "duration": 1.0},
    }


def _step(
    step_id: int,
    keystrokes: str,
    *,
    prompt: int | None = 1000,
    completion: int = 10,
    message: str = "",
    source: str = "agent",
    output: str = "ok\n",
) -> dict:
    step: dict = {
        "step_id": step_id,
        "source": source,
        "message": message or f"do step {step_id}",
        "tool_calls": [_call(keystrokes)],
        "observation": {"results": [{"content": output}]},
    }
    if prompt is not None:
        step["metrics"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
    return step


def _write_trial(
    root: Path,
    name: str,
    steps: list[dict],
    *,
    diff: str | None = None,
    metadata: dict | None = None,
    exception_type: str | None = None,
    n_in: int | None = None,
    n_out: int | None = None,
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    result: dict = {
        "task_name": "task",
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": 0.0}},
    }
    agent_result: dict = {
        "metadata": metadata or {},
        "n_input_tokens": n_in,
        "n_output_tokens": n_out,
    }
    if n_in is None:
        del agent_result["n_input_tokens"]
    if n_out is None:
        del agent_result["n_output_tokens"]
    result["agent_result"] = agent_result
    if exception_type is not None:
        result["exception_info"] = {"exception_type": exception_type}
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if diff is not None:
        verifier = trial / "verifier"
        verifier.mkdir(exist_ok=True)
        (verifier / "agent.diff").write_text(diff, encoding="utf-8")
    return trial


def _prompts(n: int, start: int = 1000, step: int = 100) -> list[int]:
    return [start + i * step for i in range(n)]


def test_read_only_probe_is_not_an_edit(tmp_path: Path) -> None:
    steps = [
        _step(
            1,
            "cd /workspace/repo && python - <<'EOF'\nprint(open('a.py').read())\nEOF\n",
            prompt=1000,
        ),
        _step(2, "pytest -q", prompt=1100),
    ]
    trial = _write_trial(tmp_path, "trial", steps)
    flow = analyze_token_flow(trial)

    assert flow["last_useful_edit"]["step_id"] is None


def test_heredoc_write_is_an_edit(tmp_path: Path) -> None:
    body = (
        "cd /workspace/repo && python - <<'EOF'\n"
        "path = 'a.py'\n"
        "src = open(path).read()\n"
        "open(path, 'w').write(src.replace('x', 'y'))\n"
        "EOF\n"
    )
    steps = [_step(1, body, prompt=1000), _step(2, "pytest -q", prompt=1100)]
    trial = _write_trial(tmp_path, "trial", steps, diff="a.py\n")
    flow = analyze_token_flow(trial)

    edit = flow["last_useful_edit"]
    assert edit["step_id"] == 1
    assert edit["kind"] == "write-call"
    assert edit["persists_in_final_diff"] is True


def test_scratch_redirect_is_not_a_repo_edit(tmp_path: Path) -> None:
    steps = [
        _step(1, "cat > a.py <<'EOF'\nx\nEOF\n", prompt=1000),
        _step(2, "pytest -q > /tmp/out.txt 2>&1; tail /tmp/out.txt", prompt=1100),
    ]
    trial = _write_trial(tmp_path, "trial", steps, diff="a.py\n")
    flow = analyze_token_flow(trial)

    edit = flow["last_useful_edit"]
    assert edit["step_id"] == 1


def test_multiline_sed_edit_extracts_target(tmp_path: Path) -> None:
    keys = (
        "sed -i '345a\\\n          if((x) > n)\\\n            break;' /home/agent/src/lib/proto.c\n"
    )
    steps = [_step(1, keys, prompt=1000), _step(2, "make", prompt=1100)]
    trial = _write_trial(tmp_path, "trial", steps, diff="proto.c\n")
    flow = analyze_token_flow(trial)

    edit = flow["last_useful_edit"]
    assert edit["step_id"] == 1
    assert edit["touched_paths"] == ["/home/agent/src/lib/proto.c"]
    assert edit["persists_in_final_diff"] is True


def test_clean_finish_stop_has_reason(tmp_path: Path) -> None:
    steps = [_step(1, "ls", prompt=1000)]
    trial = _write_trial(tmp_path, "trial", steps)
    flow = analyze_token_flow(trial)

    stop = flow["stop"]
    assert stop["stop_reason"] is None
    assert stop["exception_type"] is None
    assert "clean finish" in stop["reason"]


def test_no_edits_marks_whole_run_post_edit(tmp_path: Path) -> None:
    steps = [_step(i, "ls /workspace/repo", prompt=p) for i, p in enumerate(_prompts(6), 1)]
    trial = _write_trial(tmp_path, "trial", steps, diff="")
    flow = analyze_token_flow(trial)

    assert flow["last_useful_edit"]["step_id"] is None
    after = flow["tokens_after_last_edit"]
    assert after["share_input"] == 1.0
    assert after["share_output"] == 1.0
    assert after["note"].startswith("no useful edit")


def test_last_edit_splits_tokens_after(tmp_path: Path) -> None:
    prompts = _prompts(8)
    steps = []
    for i, prompt in enumerate(prompts, 1):
        if i == 3:
            keys = "cat > /workspace/repo/fix.py <<'EOF'\nprint(1)\nEOF\n"
        else:
            keys = f"pytest -q test_{i}.py"
        steps.append(_step(i, keys, prompt=prompt))
    diff = "diff --git a/fix.py b/fix.py\n+print(1)\n"
    trial = _write_trial(tmp_path, "trial", steps, diff=diff, n_in=sum(prompts), n_out=80)
    flow = analyze_token_flow(trial)

    edit = flow["last_useful_edit"]
    assert edit["step_id"] == 3
    assert edit["call_index"] == 3
    assert edit["persists_in_final_diff"] is True
    after = flow["tokens_after_last_edit"]
    assert after["input_tokens"] == sum(prompts[3:])
    assert after["share_input"] == round(sum(prompts[3:]) / sum(prompts), 4)
    assert after["totals_source"] == "agent_result"


def test_identical_message_run_triggers_onset(tmp_path: Path) -> None:
    claim = "the task is complete and verified"
    # Structurally distinct commands (digits alone would normalize away),
    # so only the identical-message rule can fire.
    shapes = [
        "ls",
        "pwd",
        "whoami",
        "id",
        "date",
        "uptime",
        "df -h",
        "free -m",
        "env | sort",
        "ls /tmp",
        "echo done",
    ]
    steps = [_step(i, shapes[i - 1], prompt=1000 + i, message=claim) for i in range(1, 12)]
    trial = _write_trial(tmp_path, "trial", steps)
    flow = analyze_token_flow(trial)

    onset = flow["loop_onset"]
    assert onset["step_id"] == 1
    assert onset["detector"] == "identical_message_run"


def test_loop_at_start_detected_at_first_step(tmp_path: Path) -> None:
    steps = [
        _step(1, "ls /workspace/repo", prompt=1000),
        *[
            _step(i, "grep -rn 'TODO' /workspace/repo --include='*.py' | head", prompt=1000 + i)
            for i in range(2, 8)
        ],
    ]
    trial = _write_trial(tmp_path, "trial", steps)
    flow = analyze_token_flow(trial)

    onset = flow["loop_onset"]
    assert onset["step_id"] == 2
    assert onset["call_index"] == 2
    assert onset["detector"] == "normalized_command_run"


def test_summarisation_that_fails_to_shrink(tmp_path: Path) -> None:
    steps = [
        _step(1, "ls", prompt=50000),
        {
            "step_id": 2,
            "source": "system",
            "message": "Performed context summarization and handoff to continue task.",
        },
        {
            "step_id": 3,
            "source": "user",
            "message": "summary handoff",
        },
        _step(4, "ls", prompt=52000),
    ]
    trial = _write_trial(tmp_path, "trial", steps, metadata={"summarization_count": 1})
    flow = analyze_token_flow(trial)

    summ = flow["summarisations"]
    assert summ["metadata_count"] == 1
    assert len(summ["events"]) == 1
    event = summ["events"][0]
    assert event["prompt_before"]["prompt_tokens"] == 50000
    assert event["prompt_after"]["prompt_tokens"] == 52000
    assert event["success"] is False
    assert event["success_reason"] == "prompt did not shrink across the handoff"
    assert summ["discrepancy"] is None


def test_failed_summarisation_counter_gap(tmp_path: Path) -> None:
    steps = [_step(1, "ls", prompt=1000)]
    trial = _write_trial(tmp_path, "trial", steps, metadata={"summarization_count": 2})
    flow = analyze_token_flow(trial)

    summ = flow["summarisations"]
    assert summ["metadata_count"] == 2
    assert summ["events"] == []
    assert summ["discrepancy"] is not None
    assert "failed" in summ["discrepancy"]


def test_missing_trajectory_is_none_with_reason(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(json.dumps({"task_name": "t"}))
    flow = analyze_token_flow(trial)

    assert flow["trajectory"] is None
    assert flow["trajectory_reason"]
    assert flow["last_useful_edit"]["step_id"] is None
    assert flow["last_useful_edit"]["reason"]
    assert flow["tokens_after_last_edit"] is None
    assert flow["tokens_after_reason"]
    assert flow["loop_onset"]["step_id"] is None
    assert flow["prompt_summary"]["reason"]
    assert flow["top_terminal_outputs"] == []


def test_missing_trial_dir_is_none_with_reason(tmp_path: Path) -> None:
    flow = analyze_token_flow(tmp_path / "absent")
    assert flow["trajectory"] is None
    assert flow["trajectory_reason"] == "trial directory missing"
    assert flow["stop"]["reason"] == "trial directory missing"


def test_top_outputs_ranked_with_step_refs(tmp_path: Path) -> None:
    steps = [
        _step(1, "ls", prompt=1000, output="small\n"),
        _step(2, "cat big.log", prompt=2000, output="x" * 5000),
        _step(3, "cat mid.log", prompt=3000, output="y" * 500),
    ]
    trial = _write_trial(tmp_path, "trial", steps)
    flow = analyze_token_flow(trial)

    tops = flow["top_terminal_outputs"]
    assert tops[0]["step_id"] == 2
    assert tops[0]["chars"] == 5000
    assert tops[0]["est_tokens"] == 5000 // 4
    assert tops[0]["next_prompt_tokens"] == 3000
    assert [top["step_id"] for top in tops] == [2, 3, 1]


def test_process_job_carries_token_flow(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(json.dumps({}), encoding="utf-8")
    steps = [
        _step(1, "cat > a.py <<'EOF'\nx\nEOF\n", prompt=1000),
        *[_step(i, "pytest -q", prompt=1000 + i) for i in range(2, 7)],
    ]
    _write_trial(job, "trial-a", steps, diff="a.py\n")
    out = tmp_path / "out"
    report = process_job(job, output_dir=out, ingest=False, publish=False)

    saved = json.loads((out / "trial-trial-a.json").read_text(encoding="utf-8"))
    flow = saved["token_flow"]
    assert flow["schema"] == "token_flow/v1"
    assert flow["last_useful_edit"]["step_id"] == 1
    assert flow["loop_onset"]["step_id"] == 2
    assert "token flow (HAR-114)" in (out / "trial-trial-a.md").read_text(encoding="utf-8")
    assert report["trials"][0]["trial_name"] == "trial-a"
