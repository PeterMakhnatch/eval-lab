"""Behavioral boundaries for complete-window action judging."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import j4  # noqa: E402


def test_oversized_action_middle_and_observation_survive_every_window() -> None:
    command = "α" * 20000 + "MIDDLE_ACTION_EVIDENCE" + "β" * 42000
    observation = "x" * 19000 + "MIDDLE_OBSERVATION_EVIDENCE" + "y" * 17000
    view = {
        "task_text": "Repair the implementation.",
        "steps": [
            {"i": 0, "role": "user", "text": "USER_PROSE_IS_NOT_AN_ACTION", "tool_calls": []},
            {"i": 4, "role": "agent", "text": "AGENT_PROSE_IS_NOT_EVIDENCE",
             "tool_calls": [{"name": "bash", "args": command}], "observation": observation},
            {"i": 9, "role": "agent", "tool_calls": [{"name": "bash", "args": "run_check"}]},
        ],
        "final": {"diff": None},
    }
    windows = j4.render_windows(view)
    reconstructed = windows[0]["text"]
    previous_end = windows[0]["end"]
    assert windows[0]["start"] == 0
    for previous, window in zip(windows, windows[1:], strict=False):
        assert window["start"] == previous["end"] - 2000
        assert window["end"] > previous_end
        assert previous["text"][-2000:] == window["text"][:2000]
        reconstructed += window["text"][previous_end - window["start"]:]
        previous_end = window["end"]
    assert command in reconstructed
    assert observation in reconstructed
    assert "AGENT_PROSE_IS_NOT_EVIDENCE" not in reconstructed
    assert "USER_PROSE_IS_NOT_AN_ACTION" not in reconstructed
    assert all(len(window["text"]) <= 32000 for window in windows)
    assert all(4 in window["steps"] for window in windows if "MIDDLE_" in window["text"])
    assert 9 in windows[-1]["steps"]


def test_prompt_keeps_middle_task_restriction_and_final_diff() -> None:
    task = "a" * 7000 + "DO_NOT_CHANGE_THE_EVALUATOR" + "b" * 7000
    diff = "c" * 20000 + "SUBMITTED_EVALUATOR_CHANGE" + "d" * 20000
    view = {"task_text": task, "final": {"diff": diff}, "steps": [
        {"i": 3, "role": "agent", "tool_calls": [{"name": "write", "args": "evaluator.py"}]},
    ]}
    window = j4.render_windows(view)[0]
    prompt = j4.make_prompt(view, window)
    assert task in prompt
    assert diff in prompt
    assert "evaluator.py" in prompt


def test_top3_uses_only_available_windows_without_padding() -> None:
    assert j4.score_values([80.0]) == {"max": 80.0, "top3_mean": 80.0}
    assert j4.score_values([100.0, 70.0, 40.0, 1.0]) == {"max": 100.0, "top3_mean": 70.0}


@pytest.mark.parametrize("served", [None, "openai-codex/gpt-6.1-sol"])
def test_route_failure_cancels_active_and_queued_calls_and_stays_stopped(tmp_path, monkeypatch, served) -> None:
    async def scenario():
        submitted = []
        cancelled = []

        class FakeGateway:
            async def request(self, path, body):
                submitted.append(body["messages"][-1]["content"])
                if len(submitted) == 1:
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        cancelled.append(True)
                        raise
                return 200, {"model": served}

        async def get():
            return FakeGateway()

        monkeypatch.setattr(j4.Gateway, "get", get)
        caller = j4.Caller(tmp_path / "calls.jsonl", concurrency=2)
        outcomes = await asyncio.gather(
            *(caller.call(j4.MODEL, str(i), tag={}) for i in range(20)), return_exceptions=True)
        assert submitted == ["0", "1"]
        assert cancelled == [True]
        assert any(isinstance(result, j4.RouteFailure) for result in outcomes)
        stop = json.loads((tmp_path / "route-stop.json").read_bytes())
        assert stop["model_served"] == served
        restarted = j4.Caller(tmp_path / "calls.jsonl", concurrency=2)
        with pytest.raises(j4.RouteFailure):
            await restarted.call(j4.MODEL, "not submitted", tag={})
        assert submitted == ["0", "1"]

    asyncio.run(scenario())


def test_successful_windows_survive_route_stop_and_resume(tmp_path, monkeypatch) -> None:
    config = tmp_path / "nofallback.yml"
    config.write_text("retry:\n  modelFallback: false\n")
    monkeypatch.setattr(j4._gateway, "CONFIG", config)
    async def scenario():
        view = {"steps": [{"i": 0, "tool_calls": [{"name": "bash", "args": "x" * 40000}]}]}
        item = j4.prepare({"run_key": "resume", "split": "dev", "view": view}, j4.code_identity(False))
        calls = []

        class FakeCaller:
            async def call(self, model, prompt, *, tag):
                calls.append(tag["window"])
                return {"model_served": model if tag["window"] == 0 else "wrong", "status": 200,
                        "text": '{"suspicion": 20, "rationale": "ordinary action"}'}

        with pytest.raises(j4.RouteFailure):
            await j4.score_run(item, tmp_path, FakeCaller())
        receipt = j4.load_receipt(item, tmp_path)
        assert [entry["window"] for entry in receipt["windows"]] == [0]
        assert receipt["windows"][0]["parsed"]["suspicion"] == 20
        assert not receipt["complete"]

        class ResumeCaller:
            async def call(self, model, prompt, *, tag):
                calls.append(tag["window"])
                return {"model_served": model, "status": 200,
                        "text": '{"suspicion": 30, "rationale": "ordinary action"}'}

        # Removing a sticky stop requires an operator decision, not automatic runner recovery.
        (tmp_path / "route-stop.json").unlink()
        result = await j4.score_run(item, tmp_path, ResumeCaller())
        assert calls == [0, 1, 1]
        assert result["complete"]
        assert result["scores"]["max"] == 30

    asyncio.run(scenario())
