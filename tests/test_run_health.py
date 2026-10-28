"""Run Health detector behaviour on synthetic ATIF-shaped runs (no I/O beyond tmp_path)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.production import run_health as rh

T0 = 1_700_000_000  # epoch seconds
RECORD_KEYS = {
    "run_id", "detector", "detector_version", "reader_model", "verdict", "score", "kind",
    "evidence_steps", "summary", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "produced_at",
}


def _step(step_id: int, command: str | None = "ls", *, observation: str | None = None,
          timestamp: Any = None, source: str = "agent", message: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "source": source, "message": message or ""}
    if command is not None:
        step["tool_calls"] = [{"function_name": "bash_command", "arguments": {"keystrokes": command + "\n"}}]
    if observation is not None:
        step["observation"] = {"results": [{"content": observation}]}
    if timestamp is not None:
        step["timestamp"] = timestamp
    return step


def _run(steps: list[dict[str, Any]], **meta: Any) -> rh.HealthRun:
    return rh.HealthRun(run_id="run-1", source="synthetic", steps=steps, meta=meta)


def _by_kind(run: rh.HealthRun) -> dict[str, dict[str, Any]]:
    return {record["kind"]: record for record in rh.detect(run)}


def _words(n: int) -> list[str]:
    """n distinct digit-free commands (digits are normalized away, so spell them in letters)."""
    out = []
    for i in range(n):
        letters, k = "", i
        for _ in range(3):
            letters += chr(ord("a") + k % 26)
            k //= 26
        out.append(f"echo {letters}")
    return out


def test_no_actor_transcript_is_unknown_never_clear() -> None:
    run = _run([_step(1, None, source="user", message="fix the bug"), _step(2, None)])
    records = rh.detect(run)
    assert [r["kind"] for r in records] == ["loop", "runaway", "latency", "infra"]
    assert {r["verdict"] for r in records} == {"unknown"}


def test_loop_identical_command_run_at_threshold_alarms_with_first_step() -> None:
    steps = [_step(7 + i, "pytest -x tests") for i in range(rh.LOOP_COMMAND_RUN)]
    loop = _by_kind(_run(steps))["loop"]
    assert loop["verdict"] == "alarm"
    assert loop["evidence_steps"] == [7]
    assert "identical_command_run" in loop["summary"]


def test_loop_identical_commands_with_edit_inside_is_not_that_component() -> None:
    steps = [_step(i + 1, "sed -i 's/a/b/' src/app.py") for i in range(rh.LOOP_COMMAND_RUN)]
    loop = _by_kind(_run(steps))["loop"]
    assert loop["verdict"] == "clear"


def test_loop_one_short_of_threshold_is_clear() -> None:
    steps = [_step(i + 1, "pytest -x tests") for i in range(rh.LOOP_COMMAND_RUN - 1)]
    assert _by_kind(_run(steps))["loop"]["verdict"] == "clear"


def test_loop_period_two_cycle_alarms_via_command_cycle() -> None:
    steps = [_step(i + 1, "ls" if i % 2 == 0 else "pwd") for i in range(12)]
    loop = _by_kind(_run(steps))["loop"]
    assert loop["verdict"] == "alarm"
    assert "command_cycle" in loop["summary"]
    assert "identical_command_run" not in loop["summary"]


def test_runaway_agent_timeout_exception_alarms_on_short_run() -> None:
    run = _run([_step(1, "ls")], exception_type="AgentTimeoutError")
    assert _by_kind(run)["runaway"]["verdict"] == "alarm"


def test_runaway_step_count_threshold() -> None:
    n = rh.RUNAWAY_AGENT_STEPS
    commands = _words(n)
    long_run = _run([_step(i + 1, commands[i]) for i in range(n)])
    short_run = _run([_step(i + 1, commands[i]) for i in range(n - 1)])
    assert _by_kind(long_run)["runaway"]["verdict"] == "alarm"
    assert _by_kind(short_run)["runaway"]["verdict"] == "clear"


def test_latency_abstains_without_enough_timestamps_or_phase_timings() -> None:
    steps = [_step(1, "ls", timestamp=T0), _step(2, "pwd", timestamp=T0 + 5), _step(3, "whoami")]
    latency = _by_kind(_run(steps))["latency"]
    assert latency["verdict"] == "abstain"
    assert latency["evidence_steps"] == []


@pytest.mark.parametrize("stamp", [
    lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    lambda t: t * 1000,
], ids=["iso", "epoch_ms"])
def test_latency_long_gap_alarms_at_later_step(stamp: Any) -> None:
    offsets = [0, 10, 20, 20 + rh.LATENCY_MAX_GAP_SECONDS + 60]
    steps = [_step(i + 1, cmd, timestamp=stamp(T0 + off))
             for i, (cmd, off) in enumerate(zip(_words(4), offsets, strict=True))]
    latency = _by_kind(_run(steps))["latency"]
    assert latency["verdict"] == "alarm"
    assert latency["evidence_steps"] == [4]


def test_infra_exception_type_alarms() -> None:
    run = _run([_step(1, "ls")], exception_type="DaytonaNotFoundError")
    assert _by_kind(run)["infra"]["verdict"] == "alarm"


def test_infra_signature_on_last_agent_step_alarms() -> None:
    steps = [_step(1, "ls"), _step(2, "make build", observation="write failed: No space left on device")]
    infra = _by_kind(_run(steps))["infra"]
    assert infra["verdict"] == "alarm"
    assert infra["evidence_steps"] == [2]


def test_infra_signature_followed_by_more_agent_work_is_clear_but_counted() -> None:
    commands = _words(6)
    steps = [_step(1, commands[0], observation="No space left on device")]
    steps += [_step(i + 2, commands[i + 1]) for i in range(5)]
    run = _run(steps)
    assert _by_kind(run)["infra"]["verdict"] == "clear"
    assert rh.features(run)["infra_signatures"] == {"disk": 1}


def test_no_actor_run_alarms_on_run_level_infra_exception_and_stays_unknown_otherwise() -> None:
    run = _run([_step(1, None, source="system", message="journal")], exception_type="ImageBuildError")
    verdicts = {k: r["verdict"] for k, r in _by_kind(run).items()}
    assert verdicts == {"loop": "unknown", "runaway": "unknown", "latency": "unknown", "infra": "alarm"}


def test_git_stash_baseline_check_is_not_an_edit_undo() -> None:
    steps = []
    for i in range(4):
        steps += [_step(3 * i + 1, "sed -i 's/a/b/' src/app.py"), _step(3 * i + 2, "git stash"),
                  _step(3 * i + 3, "git checkout -- src/app.py")]
    feats = rh.features(_run(steps))
    assert feats["edit_undo_cycles"] == 4
    assert feats["edit_undo_steps"][0] == 3


@pytest.mark.parametrize("run", [
    _run([_step(1, None, source="user", message="hi")]),
    _run([_step(i, "pytest -x tests") for i in range(1, 9)], exception_type="AgentTimeoutError"),
    _run([_step(i, "ls" if i % 2 else "pwd", timestamp=T0 + 2000 * i) for i in range(1, 15)]),
    _run([_step(1, "ls"), _step(2, "x", observation="No space left on device")],
         exception_type="DaytonaNotFoundError"),
], ids=["unknown", "loop_runaway", "cycle_latency", "infra"])
def test_every_record_matches_the_findings_contract(run: rh.HealthRun) -> None:
    ids = {s["step_id"] for s in run.steps}
    records = rh.detect(run)
    assert len(records) == 4
    for record in records:
        assert set(record) == RECORD_KEYS
        assert record["run_id"] == "run-1"
        assert record["detector"] == f"run_health.{record['kind']}"
        assert record["detector_version"] == rh.DETECTOR_VERSION
        assert record["reader_model"] is None
        assert record["verdict"] in {"alarm", "clear", "unknown", "abstain"}
        assert isinstance(record["score"], float | int)
        assert len(record["summary"].split()) <= 40
        assert (record["input_tokens"], record["output_tokens"], record["cost_usd"]) == (0, 0, 0.0)
        assert record["latency_ms"] >= 0
        assert record["produced_at"].endswith("Z")
        evidence = record["evidence_steps"]
        assert len(evidence) <= 5
        assert all(isinstance(s, int) and s in ids for s in evidence)


def test_score_findings_arithmetic_and_non_scored_verdicts() -> None:
    def finding(run_id: str, verdict: str, detector: str = "run_health.loop") -> dict[str, Any]:
        return {"run_id": run_id, "detector": detector, "verdict": verdict}

    labels = {"tp1": True, "tp2": True, "fn": True, "unk": True, "missing": True,
              "fp1": False, "fp2": False, "tn": False, "abs": False, "other": False}
    findings = [
        finding("tp1", "alarm"), finding("tp2", "alarm"), finding("fn", "clear"),
        finding("unk", "unknown"), finding("fp1", "alarm"), finding("fp2", "alarm"),
        finding("tn", "clear"), finding("abs", "abstain"),
        finding("other", "alarm", detector="run_health.infra"),  # another detector: not this one's finding
    ]
    score = rh.score_findings(findings, labels, detector="run_health.loop")
    assert (score["tp"], score["fp"], score["fn"], score["tn"]) == (2, 2, 1, 1)
    assert (score["unknown"], score["abstain"], score["not_run"]) == (1, 1, 2)
    assert score["recall"] == pytest.approx(2 / 3, abs=1e-4)
    assert score["precision"] == 0.5
    assert score["false_alarm_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_trial_run_reads_trajectory_and_result_json(tmp_path: Path) -> None:
    trial = tmp_path / "trial-1"
    (trial / "agent").mkdir(parents=True)
    steps = [
        {"step_id": i, "timestamp": f"2026-01-01T00:00:{i * 10:02d}Z", "source": "agent", "message": f"step {i}",
         "tool_calls": [{"tool_call_id": f"c{i}", "function_name": "bash_command",
                         "arguments": {"keystrokes": "ls\n"}}],
         "metrics": {"prompt_tokens": 100, "completion_tokens": 10}}
        for i in (1, 2, 3)
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps(
        {"schema_version": "ATIF-v1.5", "session_id": "s", "agent": {"name": "a", "version": "1"}, "steps": steps}))
    (trial / "result.json").write_text(json.dumps({
        "agent_execution": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:16:40Z"},
        "exception_info": {"exception_type": "AgentTimeoutError", "exception_message": "late"},
        "agent_result": {"n_input_tokens": 12345, "n_output_tokens": 678},
    }))
    run = rh.trial_run(trial)
    feats = rh.features(run)
    assert run.run_id == "trial-1"
    assert feats["agent_steps"] == 3
    assert feats["wall_seconds"] == 1000.0
    assert feats["prompt_tokens"] == 12345
    assert feats["completion_tokens"] == 678
    assert feats["exception_kind"] == "runaway"
    assert _by_kind(run)["runaway"]["verdict"] == "alarm"
