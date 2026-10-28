"""Run Health adapters: four views of one shared computation, store facts vs steps-only."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from evallab.production import run_health as rh
from evallab.production import trace_harness as th
from evallab.production import trace_harness_health as health

DETECTORS = (
    health.HealthLoopDetector, health.HealthRunawayDetector,
    health.HealthLatencyDetector, health.HealthInfraDetector,
)
T0 = 1_700_000_000


def _step(number: int, command: str = "ls", *, source: str = "agent", message: str = "",
          timestamp: float | None = None, observation: str = "") -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": number, "source": source, "message": message}
    if command:
        step["tool_calls"] = [{"tool_call_id": f"c{number}", "function_name": "bash",
                               "arguments": {"command": command}}]
    if observation:
        step["observation"] = {"results": [{"content": observation}]}
    if timestamp is not None:
        step["timestamp"] = timestamp
    return step


def _run(run_id: str, steps: list[dict[str, Any]], *, actor: bool = True) -> th.HarnessRun:
    return th.HarnessRun(run_id, "synthetic", "DEV", "unknown", tuple(steps), actor)


def _loop_steps(first: int = 10) -> list[dict[str, Any]]:
    return [_step(first + 2 * i, "pytest -x tests") for i in range(rh.LOOP_COMMAND_RUN)]


@pytest.fixture(autouse=True)
def _fresh_shared() -> Any:
    health._SHARED.clear()
    yield
    health._SHARED.clear()


def _detect_all(runs: Sequence[th.HarnessRun], options: th.HarnessOptions) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for cls in DETECTORS:
        detector = cls(options)
        for record in detector.detect(runs):
            assert th.validate_finding(record) == []
            assert (record["detector"], record["detector_version"], record["reader_model"]) == (
                detector.name, detector.version, detector.reader_model,
            )
            out.setdefault(record["run_id"], {})[record["kind"]] = record
    return out


def _options(tmp_path: Path) -> th.HarnessOptions:
    return th.HarnessOptions(database=tmp_path / "absent.sqlite", repo_root=tmp_path)


def test_steps_only_run_matches_run_health_and_cites_original_step_ids(tmp_path: Path) -> None:
    run = _run("r", _loop_steps(first=10))
    records = _detect_all([run], _options(tmp_path))["r"]
    assert set(records) == {"loop", "runaway", "latency", "infra"}
    assert [records[kind]["detector"] for kind in ("loop", "runaway", "latency", "infra")] == [
        "run_health.loop", "run_health.runaway", "run_health.latency", "run_health.infra",
    ]
    assert records["loop"]["verdict"] == "alarm"
    assert records["loop"]["evidence_steps"] == [10]
    assert records["loop"]["detector_version"] == rh.DETECTOR_VERSION
    assert records["latency"]["verdict"] == "abstain"  # no clock anywhere
    for expected in rh.detect(rh.HealthRun("r", "synthetic", run.steps, {"actor_inputs_available": True})):
        got = records[expected["kind"]]
        assert (got["verdict"], got["score"], got["evidence_steps"], got["summary"]) == (
            expected["verdict"], expected["score"], expected["evidence_steps"], expected["summary"],
        )


def test_run_without_actor_transcript_is_unknown_for_every_kind(tmp_path: Path) -> None:
    run = _run("silent", [_step(1, "", source="user", message="fix it")], actor=False)
    records = _detect_all([run], _options(tmp_path))["silent"]
    assert {record["verdict"] for record in records.values()} == {"unknown"}


def test_store_runs_supply_timestamps_for_admitted_runs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "cloud.sqlite"
    database.touch()
    slow = [_step(i + 1, f"echo {chr(97 + i)}", timestamp=T0 + 400 * i) for i in range(6)]
    calls: list[list[str]] = []

    def fake_store_runs(runs: Sequence[th.HarnessRun], db: Path, repo_root: Path) -> Any:
        calls.append([run.run_id for run in runs])
        assert db == database
        for run in runs:
            if run.run_id == "cloud":
                yield rh.HealthRun("cloud", "goodfire", run.steps, {"stop_reason": "max_turns"})

    monkeypatch.setattr(health, "_store_runs", fake_store_runs)
    cloud = _run("cloud", slow)
    staged = _run("staged", [_step(i + 1, f"echo {chr(97 + i)}") for i in range(6)])
    options = th.HarnessOptions(database=database, repo_root=tmp_path)
    records = _detect_all([cloud, staged], options)
    assert records["cloud"]["latency"]["verdict"] == "alarm"
    assert records["cloud"]["runaway"]["verdict"] == "alarm"  # stop_reason came from the store loader
    assert "stop:max_turns" in records["cloud"]["runaway"]["summary"]
    assert records["staged"]["latency"]["verdict"] == "abstain"
    assert records["staged"]["runaway"]["verdict"] == "clear"
    assert len(calls) == 1 and calls[0] == ["cloud", "staged"]  # one batch, shared by all four detectors


def test_four_detectors_compute_each_run_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []
    original = rh.detect

    def counting(run: rh.HealthRun, feats: Any = None) -> list[dict[str, Any]]:
        seen.append(run.run_id)
        return original(run, feats)

    monkeypatch.setattr(rh, "detect", counting)
    runs = [_run("a", _loop_steps()), _run("b", [_step(1), _step(2, "pytest")])]
    _detect_all(runs, _options(tmp_path))
    assert sorted(seen) == ["a", "b"]


def test_a_different_run_reusing_an_id_is_never_served_a_stale_result(tmp_path: Path) -> None:
    options = _options(tmp_path)
    looped = _run("same", _loop_steps())
    assert health.HealthLoopDetector(options).computation.records([looped])["same"]["loop"]["verdict"] == "alarm"
    fresh = _run("same", [_step(1, "ls"), _step(2, "pytest")])
    record = next(iter(health.HealthLoopDetector(options).detect([fresh])))
    assert record["verdict"] == "clear"
    assert health.shared_computation(options) is health.shared_computation(_options(tmp_path))
