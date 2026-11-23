from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from evallab.process_job import process_job
from evallab.results_home import _reward_summary
from evallab.sim.engine import STEP_LIMIT, Simulation

_REQUIRED = (
    "clean-pass",
    "clean-fail",
    "forged",
    "mismatch",
    "orphan",
    "exception",
    "no-reward",
)


@dataclass(frozen=True, slots=True)
class Shape:
    kind: str
    reward: float | None
    tests: int
    passed: int
    failed: int
    kinds: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    scored: bool = True
    exception: bool = False
    result_reward: bool = True
    reward_txt: float | None = None


@dataclass(frozen=True, slots=True)
class Expectation:
    n_trials: int
    n_pass: int
    n_fail: int
    n_unscored: int
    kinds: dict[str, int]


def _number_text(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _required_shapes() -> list[Shape]:
    return [
        Shape("clean-pass", 1.0, 2, 2, 0),
        Shape("clean-fail", 0.0, 2, 0, 2),
        Shape(
            "forged",
            1.0,
            2,
            0,
            2,
            kinds=("forged",),
            flags=("reward_report:forged:0/2-passed",),
        ),
        Shape(
            "mismatch",
            1.0,
            2,
            2,
            0,
            kinds=("mismatch",),
            flags=("reward_report:mismatch:1-vs-0",),
            reward_txt=0.0,
        ),
        Shape(
            "orphan",
            None,
            2,
            1,
            1,
            kinds=("orphan",),
            flags=("reward_report:orphan:0.5",),
            scored=False,
            exception=True,
            result_reward=False,
            reward_txt=0.5,
        ),
        Shape(
            "exception",
            None,
            2,
            1,
            1,
            scored=False,
            exception=True,
            result_reward=False,
        ),
        Shape("no-reward", None, 0, 0, 0, scored=False, result_reward=False),
    ]


def _random_shape(rng: Random) -> Shape:
    choices = (
        Shape(
            "forged-mismatch",
            1.0,
            2,
            0,
            2,
            kinds=("forged", "mismatch"),
            flags=(
                "reward_report:forged:0/2-passed",
                "reward_report:mismatch:1-vs-0.5",
            ),
            reward_txt=0.5,
        ),
        Shape(
            "partial-credit",
            1.0,
            3,
            2,
            1,
            kinds=("partial-credit",),
            flags=("reward_report:partial-credit:2/3-passed",),
        ),
        Shape(
            "mismatch-zero",
            0.0,
            2,
            2,
            0,
            kinds=("mismatch",),
            flags=("reward_report:mismatch:0-vs-1",),
            reward_txt=1.0,
        ),
        Shape(
            "orphan-full",
            None,
            2,
            1,
            1,
            kinds=("orphan",),
            flags=("reward_report:orphan:1",),
            scored=False,
            exception=True,
            result_reward=False,
            reward_txt=1.0,
        ),
        Shape(
            "forged-three",
            1.0,
            3,
            0,
            3,
            kinds=("forged",),
            flags=("reward_report:forged:0/3-passed",),
        ),
    )
    return rng.choice(choices)


def shapes_for(rng: Random) -> list[Shape]:
    shapes = _required_shapes()
    assert [shape.kind for shape in shapes] == list(_REQUIRED)
    if rng.random() < 0.5:
        shapes.append(_random_shape(rng))
    rng.shuffle(shapes)
    return shapes


def expectation_for(shapes: list[Shape]) -> Expectation:
    scored = [shape for shape in shapes if shape.scored]
    return Expectation(
        n_trials=len(shapes),
        n_pass=sum(1 for shape in scored if (shape.reward or 0.0) >= 1.0),
        n_fail=sum(1 for shape in scored if (shape.reward or 0.0) < 1.0),
        n_unscored=len(shapes) - len(scored),
        kinds=dict(sorted(Counter(kind for shape in shapes for kind in shape.kinds).items())),
    )


def _write_trial(root: Path, name: str, shape: Shape) -> None:
    directory = root / name
    verifier = directory / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"task_name": "simulation-task", "trial_name": name}
    if shape.result_reward:
        result["verifier_result"] = {"rewards": {"reward": shape.reward}}
    if shape.exception:
        result["exception_info"] = {"exception_type": "RuntimeError"}
    _write_json(directory / "result.json", result)
    if shape.tests > 0:
        _write_json(
            verifier / "ctrf.json",
            {
                "results": {
                    "summary": {
                        "tests": shape.tests,
                        "passed": shape.passed,
                        "failed": shape.failed,
                    }
                }
            },
        )
    if shape.reward_txt is not None:
        text = _number_text(shape.reward_txt)
        (verifier / "reward.txt").write_text(text, encoding="utf-8")


def _summary_text(n_pass: int, n_fail: int, n_unscored: int, forged: int) -> str:
    base = f"{n_pass} pass, {n_fail} fail, {n_unscored} unscored"
    return f"{base}, forged {forged}" if forged > 0 else base


class ProcessJobSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step <= STEP_LIMIT
        self.sim = sim
        self.root = root

    def run(self) -> None:
        shapes = shapes_for(self.sim.rng)
        expected = expectation_for(shapes)
        assert 4 <= expected.n_trials <= 8
        assert expected.n_pass + expected.n_fail + expected.n_unscored == expected.n_trials
        job = self.root / "job"
        published = self.root / "published"
        _write_json(job / "config.json", {})
        names = [f"{index:02d}-{shape.kind}" for index, shape in enumerate(shapes)]
        for name, shape in zip(names, shapes, strict=True):
            _write_trial(job, name, shape)
        report = self.process(job, published / "processed", expected)
        self.check_trials(published / "processed", names, shapes)
        self.check_summary(report, expected)
        self.check_index(published, expected)

    def process(self, job: Path, output: Path, expected: Expectation) -> dict[str, Any]:
        self.sim.begin(
            "process-job",
            trials=expected.n_trials,
            kinds=sorted(expected.kinds),
            publish=False,
            ingest=False,
        )
        return process_job(job, output_dir=output, publish=False, ingest=False)

    def check_summary(self, report: dict[str, Any], expected: Expectation) -> None:
        sim = self.sim
        summary = report["summary"]
        sim.note(
            n_pass=summary["n_pass"],
            n_fail=summary["n_fail"],
            n_unscored=summary["n_unscored"],
            flags=summary["reward_report_flags"],
        )
        sim.require(summary["n_trials"] == expected.n_trials, "process_job_trial_count")
        total = summary["n_pass"] + summary["n_fail"] + summary["n_unscored"]
        sim.require(total == summary["n_trials"], "process_job_outcome_partition")
        sim.require(
            (summary["n_pass"], summary["n_fail"], summary["n_unscored"])
            == (expected.n_pass, expected.n_fail, expected.n_unscored),
            "process_job_outcome_counts",
        )
        sim.require(
            summary["reward_report_flags"] == expected.kinds,
            "process_job_reward_report_kinds",
        )

    def check_trials(self, output: Path, names: list[str], shapes: list[Shape]) -> None:
        sim = self.sim
        for name, shape in zip(names, shapes, strict=True):
            record = json.loads((output / f"trial-{name}.json").read_text(encoding="utf-8"))
            flags = sorted(flag for flag in record["flags"] if flag.startswith("reward_report:"))
            sim.note(trial=name, scored=record["scored"], flags=flags)
            sim.require(record["trial_name"] == name, "process_job_trial_identity")
            if shape.kind in ("exception", "orphan"):
                sim.require(not record["scored"], "process_job_exception_unscored")
            sim.require(record["scored"] == shape.scored, "process_job_trial_scored")
            sim.require(flags == sorted(shape.flags), "process_job_trial_flags")

    def check_index(self, published: Path, expected: Expectation) -> None:
        sim = self.sim
        forged = expected.kinds.get("forged", 0)
        sim.begin("process-job-index", forged=forged)
        text = _reward_summary(published)
        sim.require(
            text == _summary_text(expected.n_pass, expected.n_fail, expected.n_unscored, forged),
            "process_job_index_summary",
        )
        variant = self.root / "index-variant"
        _write_json(
            variant / "processed" / "job.json",
            {
                "summary": {
                    "n_pass": expected.n_pass,
                    "n_fail": expected.n_fail,
                    "n_unscored": expected.n_unscored,
                    "reward_report_flags": {} if forged > 0 else {"forged": 2},
                }
            },
        )
        variant_text = _reward_summary(variant)
        sim.note(index_variant=variant_text)
        sim.require(
            variant_text
            == _summary_text(
                expected.n_pass,
                expected.n_fail,
                expected.n_unscored,
                0 if forged > 0 else 2,
            ),
            "process_job_index_forged_condition",
        )
