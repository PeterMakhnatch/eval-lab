"""Ported Traces probe-03 rules behave on real-shaped Terminus trials."""

from __future__ import annotations

import json
from pathlib import Path

from evallab import probe03

MESSAGE = json.dumps(
    {"analysis": "", "plan": "", "commands": [{"keystrokes": "echo stuck"}]}
)


def _step(step_id: int, message: str = MESSAGE) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": message,
        "observation": "output\n",
        "metrics": {"prompt_tokens": 1000 + step_id, "completion_tokens": 10},
    }


def _write_trial(
    root: Path, name: str, *, n_loop: int, reward: float, ceiling: bool
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    steps = [_step(i) for i in range(1, n_loop + 1)]
    head: dict = {"steps": steps}
    result: dict = {
        "task_name": "task",
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    if ceiling:
        prompt = sum(step["metrics"]["prompt_tokens"] for step in steps)
        result["exception_info"] = {"exception_type": "TrialBudgetExhaustedError"}
        result["agent_result"] = {"n_input_tokens": prompt, "n_output_tokens": 120}
    (agent / "trajectory.json").write_text(json.dumps(head), encoding="utf-8")
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial


def test_ceiling_loop_trial_stop_runs_and_reward(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {"trial_budget": {"max_input_tokens": 12078, "max_output_tokens": 200000}}
        ),
        encoding="utf-8",
    )
    trial = _write_trial(job, "trial-loop", n_loop=12, reward=0.0, ceiling=True)

    reward, scored, _ = probe03.read_reward(trial)
    assert (reward, scored) == (0.0, True)
    analysis = probe03.analyze_trial_core(trial, job)
    assert analysis["stop_reason"] == "ceiling:input_tokens"
    assert [(r["start"], r["end"], r["length"]) for r in analysis["runs"]] == [
        (1, 12, 12)
    ]
    # A mechanically clean identical loop with no claim is not a failure.
    assert analysis["first_failure"] is None


def test_distinct_steps_are_no_run(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text("{}", encoding="utf-8")
    trial = tmp_path / "job" / "trial-ok"
    trial.mkdir(parents=True)
    agent = trial / "agent"
    agent.mkdir()
    steps = [
        _step(i, json.dumps({"commands": [{"keystrokes": f"echo {i}"}]}))
        for i in range(1, 4)
    ]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "task",
                "trial_name": "trial-ok",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    analysis = probe03.analyze_trial_core(trial, job)
    assert analysis["runs"] == []
    assert analysis["stop_reason"] == "unknown"
