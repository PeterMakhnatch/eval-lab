"""Bounded trajectory reads must not turn partial history into partial-run accounting."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evallab import cli
from evallab.traj import outline_payload, outline_trajectory, render_outline


@pytest.fixture
def outline():
    root = Path(__file__).resolve().parents[1]
    trial = root / (
        "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
    )
    return outline_trajectory(trial, repo_root=root)


def test_summary_omits_step_bodies_without_losing_full_run_accounting(outline):
    outline = replace(
        outline,
        total_prompt_tokens=987654,
        total_cost_usd=None,
        final_metrics_present=True,
        steps=(replace(outline.steps[0], reasoning_content="private-step-body"),),
    )
    payload = outline_payload(outline, summary=True)
    assert payload["total_prompt_tokens"] == 987654
    assert payload["total_cost_usd"] is None
    assert payload["final_metrics_present"] is True
    assert payload["steps"] == ()
    assert payload["step_selection"]["omitted_steps"] == 1
    assert payload["step_selection"]["metrics_scope"] == "full_trajectory"
    assert "private-step-body" not in json.dumps(payload)
    assert payload["source_sha256"] == outline.source_sha256
    assert {c["path"] for c in payload["citations"]} == {c.path for c in outline.citations}


def test_step_range_uses_logical_ids_preserving_original_source_coordinates(outline):
    first = replace(outline.steps[0], step_id=4, source_step_id=1, source_path="head.json")
    later = replace(
        outline.steps[-1],
        step_id=9,
        source_step_id=2,
        source_path="trajectory.cont-1.json",
        source_sha256="a" * 64,
    )
    outline = replace(outline, steps=(first, later), total_prompt_tokens=123456)
    payload = outline_payload(outline, step_range=(8, 9))
    assert [s["step_id"] for s in payload["steps"]] == [9]
    assert payload["steps"][0]["source_step_id"] == 2
    assert payload["steps"][0]["source_path"] == "trajectory.cont-1.json"
    assert payload["steps"][0]["source_sha256"] == "a" * 64
    assert payload["total_prompt_tokens"] == 123456
    assert payload["step_selection"]["omitted_steps"] == 1
    assert payload["step_selection"]["available_last_step_id"] == 9
    rendered = render_outline(outline, step_range=(8, 9))
    assert "trajectory.cont-1.json#2" in rendered
    assert "Step   4" not in rendered
    assert "prompt=123,456" in rendered


def test_first_error_selects_one_logical_step_with_original_continuation_coordinates(outline):
    steps = (
        replace(outline.steps[0], step_id=4, is_error=False),
        replace(
            outline.steps[1],
            step_id=9,
            is_error=True,
            source_step_id=2,
            source_path="trajectory.cont-1.json",
            source_sha256="a" * 64,
        ),
        replace(outline.steps[-1], step_id=12, is_error=True),
    )
    outline = replace(
        outline,
        steps=steps,
        total_errors=2,
        total_prompt_tokens=123456,
        total_cost_usd=None,
    )
    payload = outline_payload(outline, first_error=True)
    assert [step["step_id"] for step in payload["steps"]] == [9]
    assert payload["steps"][0]["source_step_id"] == 2
    assert payload["steps"][0]["source_path"] == "trajectory.cont-1.json"
    assert payload["steps"][0]["source_sha256"] == "a" * 64
    assert payload["total_errors"] == 2
    assert payload["total_prompt_tokens"] == 123456
    assert payload["total_cost_usd"] is None
    assert payload["step_selection"]["omitted_steps"] == 2
    assert payload["step_selection"]["metrics_scope"] == "full_trajectory"
    assert "trajectory.cont-1.json#2" in render_outline(outline, first_error=True)


def test_no_error_selection_is_recorded_absence_not_a_rewritten_outcome(outline):
    outline = replace(
        outline,
        primary_reward=0.0,
        total_errors=0,
        steps=tuple(replace(step, is_error=False) for step in outline.steps),
    )
    payload = outline_payload(outline, first_error=True)
    assert payload["steps"] == ()
    assert payload["primary_reward"] == 0.0
    assert payload["step_selection"]["returned_steps"] == 0
    assert payload["step_selection"]["omitted_steps"] == len(outline.steps)


@pytest.mark.parametrize(
    "selection",
    [
        {"summary": True, "first_error": True},
        {"step_range": (1, 2), "first_error": True},
    ],
)
def test_first_error_cannot_be_combined_with_another_selection(outline, selection):
    with pytest.raises(ValueError):
        outline_payload(outline, **selection)
    with pytest.raises(ValueError):
        render_outline(outline, **selection)


@pytest.mark.parametrize("step_range", [(0, 3), (3, 2), (10000, 10001)])
def test_invalid_or_missing_step_ranges_do_not_look_like_empty_success(outline, step_range):
    with pytest.raises(ValueError):
        outline_payload(outline, step_range=step_range)
    with pytest.raises(ValueError):
        render_outline(outline, step_range=step_range)


def test_cli_readout_preserves_unknown_cost_and_reports_range_failure(
    tmp_path, capsys, monkeypatch
):
    trial = tmp_path / "trial"
    monkeypatch.setenv("EVALLAB_RUNS_ROOT", str(tmp_path))
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "readout",
                "agent": {"name": "fixture"},
                "steps": [{"step_id": 1, "source": "agent", "message": "working"}],
            }
        )
    )
    assert cli.run_cli(["traj", "outline", str(trial), "--summary", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cost_usd"] is None
    assert payload["steps"] == []
    assert payload["step_selection"]["omitted_steps"] == 1
    assert cli.run_cli(["traj", "outline", str(trial), "--first-error", "--json"]) == 0
    first_error_payload = json.loads(capsys.readouterr().out)
    assert first_error_payload["steps"] == []
    assert first_error_payload["total_cost_usd"] is None
    assert first_error_payload["step_selection"]["mode"] == "first_error"
    assert cli.run_cli(["traj", "outline", str(trial), "--steps", "99:100"]) == 1
    assert "no trajectory steps" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--steps", "0:1"],
        ["--steps", "2:1"],
        ["--steps", "1"],
        ["--steps", "1:2:3"],
        ["--summary", "--verbose"],
        ["--summary", "--steps", "1:2"],
        ["--first-error", "--verbose"],
        ["--first-error", "--summary"],
        ["--first-error", "--steps", "1:2"],
    ],
)
def test_cli_rejects_ambiguous_or_invalid_selection_before_reading(args):
    with pytest.raises(SystemExit) as exc:
        cli.run_cli(["traj", "outline", "not-a-trial", *args])
    assert exc.value.code == 2
