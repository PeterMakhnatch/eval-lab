"""Run-report scale: synthetic long trials, time windows, revisit onset, context growth.

The synthetic generator is deterministic (fixed seed, no wall clock, no network):
the same arguments always produce the same trial, so window boundaries, onset
detection, and compaction counts are exact assertions, not tolerances.

Step ordinals are 1-based over the stitched trajectory: the leading user step is
ordinal 1 and generated agent steps are ordinals 2..n+1 (mirrors real trials).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evallab.traj import outline_trajectory

_COMMANDS = (
    ("cat app.py", "print('v1')  # original contents, second line, third line"),
    ("ls", "app.py tests/ README.md and more files listed here"),
    ("grep -r TODO src", ""),
    ("pytest -q tests/", "3 passed in 0.42s"),
    ("sed -n '1,50p' bottle.py", "def load(body):\n    pass\n" * 6),
    ("pwd", "/app"),
    ("git status --short", " M app.py"),
    ("curl -s localhost:8080/health", '{"status": "ok"}'),
)
_STAMP_BASE = datetime(2026, 9, 1, 0, 2, tzinfo=UTC)


def _bash_step(
    step_id: int,
    second: int | None,
    command: str,
    output: str,
    code: int = 0,
    prompt: int | None = None,
) -> dict[str, Any]:
    call_id = f"call-{step_id}"
    step: dict[str, Any] = {
        "step_id": step_id,
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
    }
    if second is not None:
        stamp = _STAMP_BASE + timedelta(seconds=second)
        step["timestamp"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if prompt is not None:
        step["metrics"] = {"prompt_tokens": prompt, "completion_tokens": 100, "cost_usd": 0.001}
    return step


def synthetic_steps(
    n: int,
    *,
    seed: int = 42,
    stamps: bool = True,
    metrics: bool = True,
    repeat_probability: float = 0.12,
    compaction_every: int = 0,
    base_prompt: int = 9_000,
    growth: int = 1_200,
) -> list[dict[str, Any]]:
    """Deterministic realistic steps: commands, outputs, timestamps, token metrics.

    ``repeat_probability`` controls verbatim command reuse (loop-ish runs),
    ``compaction_every`` resets prompt tokens to ``base_prompt`` every N steps
    (a hard context drop), and prompt tokens otherwise grow by ``growth`` per
    step. ``stamps=False`` omits timestamps entirely (harnesses that drop them).
    """
    rng = random.Random(seed)
    steps: list[dict[str, Any]] = []
    clock = 0
    for index in range(1, n + 1):
        if steps and rng.random() < repeat_probability:
            previous = steps[-1]
            command = previous["tool_calls"][0]["arguments"]["command"]
            payload = json.loads(previous["observation"]["results"][0]["content"])
            output, code = payload["output"], payload["returncode"]
        else:
            command, output = _COMMANDS[rng.randrange(len(_COMMANDS))]
            code = 1 if rng.random() < 0.07 else 0
        clock += rng.choice((2, 3, 5, 8, 13))
        if compaction_every and index % compaction_every == 0:
            prompt = base_prompt
        else:
            prompt = base_prompt + (index % (compaction_every or n)) * growth
        steps.append(
            _bash_step(
                index,
                clock if stamps else None,
                command,
                output,
                code=code,
                prompt=prompt if metrics else None,
            )
        )
    return steps


def _result(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "trial_name": name,
        "task_name": "lab/synthetic",
        "config": {"agent": {"name": "mini-swe-agent", "model_name": "zai/glm"}},
        "agent_info": {"name": "mini-swe-agent", "version": "2.4.6"},
        "agent_result": {"n_input_tokens": 1_000_000, "n_output_tokens": 200_000, "cost_usd": 3.5},
        "verifier_result": {"rewards": {"reward": 0.0}},
        "started_at": "2026-09-01T00:00:00Z",
        "finished_at": "2026-09-01T04:00:00Z",
        "agent_execution": {"started_at": "2026-09-01T00:02:00Z", "finished_at": "2026-09-01T03:58:00Z"},
    }


def synthetic_trial(
    root: Path, name: str, steps: list[dict[str, Any]], *, user_stamp: bool = True
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (trial / "result.json").write_text(json.dumps(_result(name)), encoding="utf-8")
    user_step: dict[str, Any] = {"step_id": 0, "source": "user", "message": "Do the task"}
    if user_stamp:
        user_step["timestamp"] = "2026-09-01T00:02:00Z"
    doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": name,
        "agent": {"name": "mini-swe-agent", "version": "2.4.6", "model_name": "zai/glm"},
        "steps": [user_step, *steps],
    }
    (agent / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    return trial


def test_loop_suspicion_matches_the_trajectory_outline(tmp_path: Path) -> None:
    # A run with every loop shape: 3+ consecutive identical commands, a failing
    # command repeated 3+ times, and an alternating tool cycle. The report
    # computes loop suspicion in its single parse; the outline is the reference.
    steps = [
        _bash_step(1, 1, "pytest -q tests/", "FAILED", code=1),
        _bash_step(2, 2, "pytest -q tests/", "FAILED", code=1),
        _bash_step(3, 3, "pytest -q tests/", "FAILED", code=1),
        _bash_step(4, 4, "cat app.py", "contents"),
        _bash_step(5, 5, "cat app.py", "contents"),
        _bash_step(6, 6, "cat app.py", "contents"),
    ]
    for i in range(7, 19):
        steps.append(_bash_step(i, i, "ls -la" if i % 2 else "pwd", f"out {i}"))
    trial = synthetic_trial(tmp_path, "loopy", steps)

    outline = outline_trajectory(trial, repo_root=trial, explicit_runs_root=trial)

    assert outline.status == "featured"


