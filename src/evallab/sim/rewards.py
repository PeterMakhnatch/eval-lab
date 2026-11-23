from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from evallab import job_verify, probe03, regrade, results, reward_report
from evallab.sim.engine import Simulation

_NAN = float("nan")
_INFINITY = float("inf")
_ABSENT = object()
_VALUES = (0.0, 0.25, 0.5, 0.75, 1.0, 2.5, -2.5, -0.5)
_SOURCES = ("result", "reward_json", "reward_txt", "orphan")


@dataclass(frozen=True, slots=True)
class Shape:
    name: str
    value: Any
    source: str
    exception: bool = False
    assess_anomaly: bool = False


@dataclass(frozen=True, slots=True)
class Expectation:
    probe_value: float | None
    probe_scored: bool
    results_value: float | None
    regrade_value: float | None
    verify_problem: str | None
    assess_kind: str
    assess_reward: float | None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _problem(value: Any) -> str | None:
    assert value is not _ABSENT
    if isinstance(value, bool):
        return "reward_bool"
    if not isinstance(value, int | float):
        return "reward_non_numeric"
    if not math.isfinite(value):
        return "reward_non_finite"
    if float(value) == -1:
        return "reward_unscored_sentinel"
    return None


def _expected(shape: Shape) -> Expectation:
    number = _finite(shape.value)
    in_result = shape.source == "result"
    file_number = None
    if shape.source in ("reward_json", "reward_txt", "orphan") and not shape.exception:
        file_number = number
    probe_value = number if in_result else file_number
    probe_scored = probe_value is not None and probe_value != -1
    kept = number if in_result else None
    verify_problem = None if shape.value is _ABSENT else _problem(shape.value)
    if probe_scored and shape.assess_anomaly:
        assess_kind, assess_reward = "record", probe_value
    elif not probe_scored and shape.exception and shape.source == "reward_txt":
        assess_kind, assess_reward = "orphan", None
    else:
        assess_kind, assess_reward = "none", None
    return Expectation(
        probe_value=probe_value,
        probe_scored=probe_scored,
        results_value=kept,
        regrade_value=kept,
        verify_problem=verify_problem,
        assess_kind=assess_kind,
        assess_reward=assess_reward,
    )


def _number_text(value: Any) -> str:
    assert isinstance(value, float)
    return repr(value) if not math.isfinite(value) else format(value, ".17g")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _write_anomaly(verifier: Path, shape: Shape) -> None:
    value = shape.value
    assert isinstance(value, float) and math.isfinite(value)
    if value > 0:
        _write_json(verifier / "ctrf.json", {"results": {"tests": [{"status": "failed"}]}})
        if shape.source != "reward_txt":
            (verifier / "reward.txt").write_text(_number_text(value), encoding="utf-8")
    else:
        assert shape.source != "reward_txt"
        (verifier / "reward.txt").write_text(_number_text(value + 1.0), encoding="utf-8")


def _write(root: Path, shape: Shape) -> Path:
    directory = root / shape.name
    verifier = directory / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    if shape.source == "orphan":
        (verifier / "reward.txt").write_text(_number_text(shape.value), encoding="utf-8")
        return directory
    result: dict[str, Any] = {"task_name": "simulation-task", "trial_name": shape.name}
    if shape.source == "result":
        result["verifier_result"] = {"rewards": {"reward": shape.value}}
    elif shape.source == "result_empty":
        result["verifier_result"] = {"rewards": {}}
    if shape.exception:
        result["exception_info"] = {"exception_type": "RuntimeError"}
    _write_json(directory / "result.json", result)
    if shape.source == "reward_json":
        _write_json(verifier / "reward.json", {"reward": shape.value})
    elif shape.source == "reward_txt":
        (verifier / "reward.txt").write_text(_number_text(shape.value), encoding="utf-8")
    if shape.assess_anomaly:
        _write_anomaly(verifier, shape)
    return directory


def _shapes(rng: Random) -> list[Shape]:
    shapes = [
        Shape("result-valid-one", 1.0, "result", assess_anomaly=True),
        Shape("result-valid-zero", 0.0, "result", assess_anomaly=True),
        Shape("result-fraction", 0.5, "result", assess_anomaly=True),
        Shape("result-negative", -0.5, "result", assess_anomaly=True),
        Shape("result-unscored-sentinel", -1.0, "result"),
        Shape("result-bool-true", True, "result"),
        Shape("result-bool-false", False, "result"),
        Shape("result-nan", _NAN, "result"),
        Shape("result-infinity", _INFINITY, "result"),
        Shape("result-negative-infinity", -_INFINITY, "result"),
        Shape("result-string", "0.5", "result"),
        Shape("result-missing-rewards", _ABSENT, "missing"),
        Shape("result-empty-rewards", _ABSENT, "result_empty"),
        Shape("orphan-reward-txt", 0.5, "orphan"),
        Shape("reward-json-nan", _NAN, "reward_json"),
        Shape("reward-txt-infinity", _INFINITY, "reward_txt"),
        Shape("exception-reward-json", 1.0, "reward_json", exception=True),
        Shape("exception-reward-txt", 1.0, "reward_txt", exception=True),
    ]
    for index in range(rng.randrange(6, 23)):
        value = rng.choice(_VALUES)
        source = rng.choice(_SOURCES)
        exception = source != "orphan" and rng.random() < 0.1
        anomaly = (
            not exception
            and source in ("result", "reward_json", "reward_txt")
            and (value > 0 or source != "reward_txt")
        )
        shapes.append(Shape(f"random-{index:02d}", value, source, exception, anomaly))
    return shapes


class RewardSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        self.sim = sim
        self.root = root

    def run(self) -> None:
        shapes = _shapes(self.sim.rng)
        assert len(shapes) >= len(_SOURCES)
        self.sim.begin("reward-readers", shapes=len(shapes))
        for shape in shapes:
            self._check(shape, _write(self.root, shape), _expected(shape))

    def _check(self, shape: Shape, directory: Path, expected: Expectation) -> None:
        sim = self.sim
        probe_value, probe_scored, _ = probe03.read_reward(directory)
        rewards = results.load_trial(directory).rewards
        observation = regrade.read_reward_observation(directory)
        assessment = reward_report.assess(directory)
        problem = None if shape.value is _ABSENT else job_verify._reward_value_problem(shape.value)
        sim.require(
            probe_value == expected.probe_value and probe_scored == expected.probe_scored,
            "reward_readers_probe03_contract",
        )
        sim.require(
            rewards.get("reward") == expected.results_value,
            "reward_readers_results_contract",
        )
        if expected.regrade_value is None:
            sim.require(observation is None, "reward_readers_regrade_contract")
        else:
            sim.require(
                observation is not None
                and observation.rewards == {"reward": expected.regrade_value},
                "reward_readers_regrade_contract",
            )
        sim.require(problem == expected.verify_problem, "reward_readers_job_verify_contract")
        self._check_assess(shape, assessment, expected)
        dropped = (
            rewards.get("reward") is None
            and observation is None
            and assessment is None
            and not probe_scored
        )
        if (
            shape.source == "result"
            and isinstance(shape.value, float)
            and not math.isfinite(shape.value)
        ):
            sim.require(dropped, "reward_readers_drop_non_finite")
        if shape.source == "result" and isinstance(shape.value, bool):
            sim.require(dropped, "reward_readers_drop_bool")
        if shape.name == "result-unscored-sentinel":
            sim.require(
                rewards.get("reward") == -1.0
                and observation is not None
                and observation.rewards == {"reward": -1.0}
                and probe_value == -1.0
                and not probe_scored
                and problem == "reward_unscored_sentinel"
                and assessment is None,
                "reward_readers_unscored_sentinel_split",
            )

    def _check_assess(
        self, shape: Shape, assessment: dict[str, Any] | None, expected: Expectation
    ) -> None:
        sim = self.sim
        if expected.assess_kind == "none":
            sim.require(assessment is None, "reward_readers_reward_report_contract")
            return
        if expected.assess_kind == "orphan":
            sim.require(
                assessment is not None
                and assessment.get("orphan") is True
                and assessment.get("reward") is None
                and assessment.get("reward_txt") == _finite(shape.value),
                "reward_readers_reward_report_contract",
            )
            return
        assert expected.assess_kind == "record"
        sim.require(
            assessment is not None and assessment.get("reward") == expected.assess_reward,
            "reward_readers_reward_report_contract",
        )
