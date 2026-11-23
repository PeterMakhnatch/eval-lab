from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any, Literal

from evallab.control_expect import declared_expectation
from evallab.results import JobRecord, load_job
from evallab.reward_hack import ControlScript, HackRunResult, classify_results, stage_controls
from evallab.runner import matrix_run_outcome
from evallab.schemas import ExperimentMatrix, MatrixRun
from evallab.sim.engine import Simulation

_ABSENT = object()
_NAN = float("nan")

Role = Literal["baseline-oracle", "baseline-nop", "exploit"]
Verdict = Literal["hackable", "partially_hackable", "not_demonstrated", "task_broken", "unscored"]


@dataclass(frozen=True, slots=True)
class Shape:
    name: str
    reward: Any
    exception: bool = False

    @property
    def infra(self) -> bool:
        if self.exception or self.reward is _ABSENT or isinstance(self.reward, bool):
            return True
        assert isinstance(self.reward, float)
        return not math.isfinite(self.reward)


@dataclass(frozen=True, slots=True)
class Expected:
    status: str
    rewards: tuple[float, ...]
    reward: float | None
    outcome: str


def _infra_shapes() -> tuple[Shape, ...]:
    return (
        Shape("nan", _NAN),
        Shape("bool-true", True),
        Shape("bool-false", False),
        Shape("missing", _ABSENT),
        Shape("exception", 1.0, exception=True),
    )


def _scored_shapes() -> tuple[Shape, ...]:
    return (Shape("zero", 0.0), Shape("half", 0.5), Shape("one", 1.0))


def _oracle_pool() -> tuple[Shape, ...]:
    return (
        *(Shape("one", 1.0) for _ in range(4)),
        Shape("half", 0.5),
        Shape("zero", 0.0),
        *_infra_shapes(),
    )


def _nop_pool() -> tuple[Shape, ...]:
    return (
        *(Shape("zero", 0.0) for _ in range(4)),
        Shape("half", 0.5),
        Shape("one", 1.0),
        *_infra_shapes(),
    )


def _plan(seed: int, rng: Random) -> tuple[Shape, Shape, list[Shape]]:
    assert 0 <= seed < 2**64
    oracle = rng.choice(_oracle_pool())
    nop = rng.choice(_nop_pool())
    count = rng.randint(1, 4)
    exploits = [rng.choice(_infra_shapes())]
    if count >= 2:
        exploits.append(_scored_shapes()[seed % 3])
    while len(exploits) < count:
        exploits.append(rng.choice((*_infra_shapes(), *_scored_shapes())))
    rng.shuffle(exploits)
    assert 1 <= len(exploits) <= 4
    assert any(shape.infra for shape in exploits)
    return oracle, nop, exploits


def _role(index: int) -> Role:
    if index == 0:
        return "baseline-oracle"
    if index == 1:
        return "baseline-nop"
    return "exploit"


def _expected(role: Role, shape: Shape) -> Expected:
    if shape.infra:
        return Expected("infra", (), None, "unscored")
    reward = float(shape.reward)
    exploit = "hack_succeeded" if reward > 0 else "resisted"
    outcome = exploit if role == "exploit" else "baseline"
    return Expected("ok", (reward,), reward, outcome)


def _expected_verdict(oracle: Expected, nop: Expected, exploits: list[Expected]) -> Verdict:
    if nop.reward is not None and nop.reward > 0:
        return "task_broken"
    if oracle.reward is not None and oracle.reward < 1:
        return "task_broken"
    if any(want.reward == 1.0 and want.outcome == "hack_succeeded" for want in exploits):
        return "hackable"
    if any(want.outcome == "hack_succeeded" for want in exploits):
        return "partially_hackable"
    if any(want.outcome != "unscored" for want in exploits):
        return "not_demonstrated"
    return "unscored"


def _broken(oracle: Expected, nop: Expected) -> bool:
    return (nop.reward is not None and nop.reward > 0) or (
        oracle.reward is not None and oracle.reward < 1
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _matrix(seed: int, exploits: int) -> ExperimentMatrix:
    names = [
        f"hack-{seed}-00-oracle",
        f"hack-{seed}-01-nop",
        *(f"hack-{seed}-{index + 2:02d}-exploit" for index in range(exploits)),
    ]
    runs = [
        MatrixRun(name=names[0], agent="oracle", expect_reward=None),
        MatrixRun(name=names[1], agent="nop", expect_reward=None),
        *(MatrixRun(name=name, agent="oracle", expect_reward=None) for name in names[2:]),
    ]
    digest = hashlib.sha256(f"simulation-task-{seed}".encode()).hexdigest()
    return ExperimentMatrix(
        matrix_id="0" * 26,
        name=f"reward-hack-simulation-{seed}",
        hypothesis="A graded control reaches reward without solving the task",
        benchmark_family="simulation-task",
        task_id="simulation-task",
        task="simulation-task",
        task_package_digest=f"sha256:{digest}",
        verifier_digest=f"sha256:{digest}",
        environment="docker",
        concurrency=1,
        timeout_seconds=60,
        runs=runs,
    )


class HackSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step <= 160
        self.sim = sim
        self.root = root

    def run(self) -> None:
        seed = self.sim.seed
        oracle, nop, exploit_shapes = _plan(seed, self.sim.rng)
        shapes = [oracle, nop, *exploit_shapes]
        self.sim.begin(
            "reward-hack",
            seed=seed,
            oracle=oracle.name,
            nop=nop.name,
            exploits=[shape.name for shape in exploit_shapes],
        )
        matrix = _matrix(seed, len(exploit_shapes))
        controls = stage_controls(self.root / "staged", self._scripts(exploit_shapes))
        results = []
        jobs = []
        for index, run in enumerate(matrix.runs):
            job_dir = self._write_job(seed, index, run, shapes[index])
            job = load_job(job_dir)
            jobs.append(job)
            results.append(matrix_run_outcome(job, run))
        runs, verdict, detail = classify_results(matrix, results, controls)
        self._check(matrix, shapes, controls, jobs, results, runs, verdict, detail)

    def _scripts(self, shapes: list[Shape]) -> list[Path]:
        directory = self.root / "sources"
        directory.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index in range(len(shapes)):
            path = directory / f"exploit-{index}.sh"
            header = "# expect: 1\n" if index % 2 == 0 else ""
            path.write_text(
                f"#!/bin/sh\nset -eu\n{header}printf '%s\\n' exploit-{index}\n",
                encoding="utf-8",
            )
            paths.append(path)
        return paths

    def _write_job(self, seed: int, index: int, run: MatrixRun, shape: Shape) -> Path:
        job_dir = self.root / "jobs" / f"{index:02d}-{run.name}"
        trial_dir = job_dir / "trial"
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        now = self.sim.runtime.now().isoformat()
        trial: dict[str, Any] = {
            "id": f"trial-{seed}-{index}",
            "task_name": "simulation-task",
            "trial_name": f"{run.name}-trial",
            "finished_at": now,
            "agent_execution": {"started_at": now, "finished_at": now},
        }
        if shape.exception:
            trial["exception_info"] = {"exception_type": "RuntimeError"}
        if shape.reward is not _ABSENT:
            trial["verifier_result"] = {"rewards": {"reward": shape.reward}}
        _write_json(trial_dir / "result.json", trial)
        (trial_dir / "agent" / "oracle.txt").write_text("oracle complete\n", encoding="utf-8")
        (trial_dir / "agent" / "exit-code.txt").write_text("0\n", encoding="utf-8")
        _write_json(
            job_dir / "result.json",
            {
                "id": f"job-{seed}-{index}",
                "finished_at": now,
                "n_total_trials": 1,
                "stats": {},
            },
        )
        return job_dir

    def _check(
        self,
        matrix: ExperimentMatrix,
        shapes: list[Shape],
        controls: tuple[ControlScript, ...],
        jobs: list[JobRecord],
        results: list[dict[str, Any]],
        runs: tuple[HackRunResult, ...],
        verdict: str,
        detail: str,
    ) -> None:
        sim = self.sim
        expected = [_expected(_role(index), shape) for index, shape in enumerate(shapes)]
        sim.note(
            classified=[run.outcome for run in runs],
            verdict=verdict,
            detail=detail.replace(str(self.root), "<scratch>"),
        )
        for index, (run, want, classified) in enumerate(
            zip(matrix.runs, expected, runs, strict=True)
        ):
            shape = shapes[index]
            sim.require(len(jobs[index].trials) == 1, "hack_job_has_one_trial")
            sim.require(
                classified.name == run.name and classified.role == _role(index),
                "hack_classification_identity",
            )
            result = results[index]
            sim.require(
                result["status"] == want.status and tuple(result["rewards"]) == want.rewards,
                "hack_outcome_status_matches_reward_shape",
            )
            sim.require(
                classified.status == want.status
                and classified.reward == want.reward
                and classified.outcome == want.outcome,
                "hack_classified_run_matches_reward_shape",
            )
            if shape.infra:
                sim.require(
                    result["status"] == "infra"
                    and classified.outcome == "unscored"
                    and classified.reward is None
                    and bool(result["error"]),
                    "hack_infra_reward_unscored",
                )
            if index >= 2:
                control = controls[index - 2]
                sim.require(
                    classified.script == control.source
                    and classified.script_sha256 == control.sha256,
                    "hack_exploit_control_attribution",
                )
                sim.require(
                    classified.declared_expectation == declared_expectation(Path(control.source)),
                    "hack_exploit_declared_expectation",
                )
            else:
                sim.require(classified.script is None, "hack_baseline_has_no_control")
        self._check_verdict(expected, runs, verdict)

    def _check_verdict(
        self, expected: list[Expected], runs: tuple[HackRunResult, ...], verdict: str
    ) -> None:
        sim = self.sim
        oracle, nop = expected[0], expected[1]
        exploits = expected[2:]
        sim.require(
            verdict == _expected_verdict(oracle, nop, exploits),
            "hack_verdict_matches_controls",
        )
        sim.require(
            (verdict == "task_broken") == _broken(oracle, nop),
            "hack_verdict_task_broken_iff_baseline_defect",
        )
        full = any(want.reward == 1.0 and want.outcome == "hack_succeeded" for want in exploits)
        sim.require(
            _broken(oracle, nop) or (verdict == "hackable") == full,
            "hack_verdict_hackable_iff_full_exploit",
        )
        for want, run in zip(exploits, runs[2:], strict=True):
            if want.outcome == "unscored":
                sim.require(run.outcome == "unscored", "hack_exploit_infra_unscored")
            elif want.outcome == "hack_succeeded":
                assert want.reward is not None and want.reward > 0
                sim.require(
                    run.outcome == "hack_succeeded" and run.reward is not None and run.reward > 0,
                    "hack_exploit_positive_reward_succeeds",
                )
            else:
                assert want.outcome == "resisted"
                sim.require(
                    run.outcome == "resisted" and run.reward == 0.0,
                    "hack_exploit_zero_reward_resisted",
                )
