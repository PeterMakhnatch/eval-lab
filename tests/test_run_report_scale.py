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

from evallab.interpretation.run_report import (
    MAX_CONTEXT_EVENT_LINES,
    build_run_report,
    render_run_report_markdown,
)
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


def test_time_windows_bucket_by_wall_clock_not_step_count(tmp_path: Path) -> None:
    # First 30 agent steps run 1s apart, the last 30 run 100s apart: equal-duration
    # windows must put the fast early steps together and spread the late ones out.
    steps = []
    for i in range(1, 61):
        second = i if i <= 30 else 30 + (i - 30) * 100
        steps.append(_bash_step(i, second, f"cmd-{i % 7}", f"output for step {i}, long enough"))
    report = build_run_report(synthetic_trial(tmp_path, "time-windows", steps))
    section = report["timeline"]["time_windows"]

    assert section["status"] == "available"
    windows = section["windows"]
    assert len(windows) == 10
    assert [w["steps"][0] for w in windows] == sorted(w["steps"][0] for w in windows)
    # Every window covers the same duration and they tile the span exactly.
    span = section["span_seconds"]
    assert windows[0]["starts_at_offset_seconds"] == 0.0
    assert windows[-1]["ends_at_offset_seconds"] == span
    for left, right in zip(windows, windows[1:], strict=False):
        assert left["ends_at_offset_seconds"] == right["starts_at_offset_seconds"]
    # The fast first half lands in the first window; late steps spread out.
    assert windows[0]["steps"][1] >= 25
    assert windows[-1]["steps"][0] > 30
    # No step is lost or double counted across windows.
    assert sum(w["tool_calls"] for w in windows) == 60


def test_time_windows_unavailable_without_timestamps(tmp_path: Path) -> None:
    steps = synthetic_steps(60, stamps=False)
    report = build_run_report(synthetic_trial(tmp_path, "no-stamps", steps, user_stamp=False))
    section = report["timeline"]["time_windows"]

    assert section["status"] == "unavailable"
    assert section["reason"] == "steps carry no timestamps"
    # Step windows still work, and the Markdown says why time windows are missing.
    assert len(report["timeline"]["windows"]) == 10
    markdown = render_run_report_markdown(report)
    assert "Time windows unavailable: steps carry no timestamps." in markdown


def test_time_windows_unavailable_when_all_timestamps_identical(tmp_path: Path) -> None:
    steps = synthetic_steps(60, stamps=False)
    for step in steps:
        step["timestamp"] = "2026-09-01T00:02:00Z"
    section = build_run_report(synthetic_trial(tmp_path, "same-stamp", steps))[
        "timeline"
    ]["time_windows"]

    assert section["status"] == "unavailable"
    assert section["reason"] == "all timestamps are identical"


def test_revisit_onset_is_first_window_above_run_median(tmp_path: Path) -> None:
    # 24 unique commands, then the same two commands alternate for 35 steps.
    # Total 60 steps = clean tenths (6 steps per window): rates are
    # [0, 0, 0, 0, 0.5, 1, 1, 1, 1, 1], median 0.75, onset = window 5 (steps 31-36).
    steps = [
        _bash_step(i, i, f"unique-cmd-{i}", f"output {i} with enough text to matter")
        for i in range(1, 25)
    ]
    for i in range(25, 60):
        command = "cat app.py" if i % 2 else "sed -n '1,50p' bottle.py"
        steps.append(_bash_step(i, i, command, f"stable output {i % 2}, long enough to be counted"))
    onset = build_run_report(synthetic_trial(tmp_path, "late-repeats", steps))["revisits"][
        "revisits_started"
    ]

    assert onset["status"] == "onset"
    assert onset["window"] == 5
    assert onset["steps"] == [31, 36]
    assert onset["median_repeat_rate"] == 0.75
    assert onset["repeat_rate"] == 1.0
    assert onset["repeat_rate_by_window"][:4] == [0.0, 0.0, 0.0, 0.0]
    assert onset["repeat_rate_by_window"][4] == 0.5
    assert onset["repeat_rate_by_window"][5] == 1.0


def test_revisit_onset_flat_when_no_window_exceeds_median(tmp_path: Path) -> None:
    # The same command for every step: after the first occurrence every window
    # sits at rate 1.0, so none exceeds the run median and no onset is reported.
    steps = [_bash_step(i, i, "cat app.py", "print('v1')  # stable file contents here") for i in range(1, 61)]
    onset = build_run_report(synthetic_trial(tmp_path, "flat", steps))["revisits"]["revisits_started"]

    assert onset["status"] == "flat"
    assert onset["window"] is None
    assert onset["repeat_rate"] is None


def test_revisit_onset_no_repeats_and_short_runs(tmp_path: Path) -> None:
    unique = [
        _bash_step(i, i, f"unique-cmd-{i}", f"output {i} long enough to be counted")
        for i in range(1, 61)
    ]
    report = build_run_report(synthetic_trial(tmp_path, "no-repeats", unique))
    assert report["revisits"]["revisits_started"]["status"] == "no_repeats"

    short = build_run_report(synthetic_trial(tmp_path, "short", unique[:10]))
    assert "revisits_started" not in short["revisits"]
    assert short["timeline"]["time_windows"]["status"] == "unavailable"
    assert short["timeline"]["time_windows"]["reason"] == "fewer than 20 steps"


def test_context_growth_curve_peaks_and_inferred_compactions(tmp_path: Path) -> None:
    # Prompt tokens climb 9k -> ~20k then reset every 20 steps: each reset is one
    # inferred compaction (agent steps 20/40/60 = ordinals 21/41/61 of 61).
    steps = synthetic_steps(
        60, seed=7, repeat_probability=0.0, compaction_every=20, base_prompt=9_000, growth=600
    )
    report = build_run_report(synthetic_trial(tmp_path, "growth", steps))
    windows = report["timeline"]["windows"]

    assert [w["compactions"] for w in windows] == [0, 0, 0, 1, 0, 0, 1, 0, 0, 1]
    assert max(w["peak_prompt_tokens"] for w in windows) >= 19_800
    drops = [e for e in report["context"]["events"] if e["kind"] == "inferred_context_drop"]
    assert len(drops) == 3
    assert sum(w["compactions"] for w in windows) == len(drops)
    # Gradual growth without a cliff invents no compaction.
    flat = synthetic_steps(60, seed=7, repeat_probability=0.0, compaction_every=0, growth=50)
    flat_report = build_run_report(synthetic_trial(tmp_path, "flat-growth", flat))
    assert sum(w["compactions"] for w in flat_report["timeline"]["windows"]) == 0


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

    report = build_run_report(trial)["revisits"]["loop_suspicion"]
    outline = outline_trajectory(trial, repo_root=trial, explicit_runs_root=trial)

    assert outline.status == "featured"
    assert report is not None
    assert report["score"] == outline.loop_suspicion.score
    assert report["detected"] == outline.loop_suspicion.detected
    assert report["reasons"] == list(outline.loop_suspicion.reasons)


def test_two_thousand_step_report_stays_bounded_and_flat(tmp_path: Path) -> None:
    steps = synthetic_steps(2000, compaction_every=25)
    report = build_run_report(synthetic_trial(tmp_path, "synthetic-2000", steps))
    markdown = render_run_report_markdown(report)

    assert report["timeline"]["total_steps"] == 2001  # 2000 generated + leading user step
    assert report["timeline"]["shown_steps"] <= 60
    assert len(report["timeline"]["windows"]) == 10
    assert report["timeline"]["time_windows"]["status"] == "available"
    assert len(report["timeline"]["time_windows"]["windows"]) == 10
    # The Markdown stays bounded: timeline rows are capped, context events capped.
    assert markdown.count("\n") < 400
    assert "more events (see the JSON report)" in markdown
    assert len(report["context"]["events"]) > MAX_CONTEXT_EVENT_LINES
    # Windows carry the context growth curve.
    assert sum(w["compactions"] for w in report["timeline"]["windows"]) == 80
    assert all("peak_prompt_tokens" in w for w in report["timeline"]["windows"])
