"""G5 position-wave gate: canonical infra semantics and stale-result refusal."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/ovn-sft-v0/g5_wave_outcome.py"
_spec = importlib.util.spec_from_file_location("g5_wave_outcome", SCRIPT)
assert _spec and _spec.loader
g5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g5)

STARTED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _result(
    runs: Path, name: str, exception: str | None, reward: float | None, at: datetime
) -> None:
    path = runs / name / "trial__x" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "exception_info": {"exception_type": exception} if exception else None,
                "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
            }
        )
    )
    os.utime(path, (at.timestamp(), at.timestamp()))


def _wave(tmp_path: Path, *names: str) -> Path:
    wave = tmp_path / "wave-1.txt"
    wave.write_text("".join(f"{name} 01ID\n" for name in names))
    return wave


def test_unscored_non_stop_exception_is_infra_but_scored_agent_stops_are_not(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    later = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    _result(runs, "tmux", "RuntimeError", None, later)
    _result(runs, "timeout-pass", "AgentTimeoutError", 1.0, later)
    _result(runs, "loop-stop-fail", "LoopBreakStop", 0.0, later)
    _result(runs, "budget-unscored", "TrialBudgetExhaustedError", None, later)
    _result(runs, "scored-runtime", "RuntimeError", 0.0, later)
    outcome = g5.classify(
        _wave(
            tmp_path, "tmux", "timeout-pass", "loop-stop-fail", "budget-unscored", "scored-runtime"
        ),
        STARTED,
        runs,
        tmp_path / "events.jsonl",
    )
    assert outcome["infra_failures"] == ["tmux:RuntimeError"]
    assert outcome["not_terminal"] == []


def test_result_older_than_the_wave_is_not_terminal(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _result(runs, "stale", None, 1.0, datetime(2026, 10, 1, 11, 0, tzinfo=UTC))
    outcome = g5.classify(_wave(tmp_path, "stale"), STARTED, runs, tmp_path / "events.jsonl")
    assert outcome["not_terminal"] == ["stale"]
    assert outcome["terminal"] == []


def test_refusal_since_the_wave_started_is_reported(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event": "dispatch_refused",
                "job_name": "a",
                "reason_code": "x",
                "occurred_at": "2026-10-01T12:01:00+00:00",
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "dispatch_refused",
                "job_name": "a",
                "reason_code": "old",
                "occurred_at": "2026-10-01T11:00:00+00:00",
            }
        )
        + "\n"
    )
    outcome = g5.classify(_wave(tmp_path, "a"), STARTED, tmp_path / "runs", events)
    assert outcome["refused"] == ["a:x"]
