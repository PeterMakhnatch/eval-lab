"""One reward-or-unscored result from every published-trial reward reader (HAR-453).

The four readers are ``probe03.read_reward``, ``qa.trace._rewards``,
``reward_report.assess`` and the HAR-437 builder's ``reward_values``.
Harbor 0.24 reads ``verifier/reward.json`` before ``verifier/reward.txt``.
A captured ``result.json`` reward wins. A present but invalid captured reward
is unscored and never falls back to a file. A reward of ``-1`` is unscored.
An exception without a captured reward keeps its files as evidence (HAR-446).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab import probe03, reward_report
from evallab.qa import trace

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "research/experiments/reward-report-contradictions/build.py"


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reward_report_readers", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trial(tmp_path: Path, files: dict[str, Any]) -> Path:
    trial = tmp_path / "trial"
    for rel, payload in files.items():
        path = trial / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
    return trial


def _captured(reward: object) -> dict[str, Any]:
    return {"trial_name": "trial", "verifier_result": {"rewards": {"reward": reward}}}


#: (files, expected reward or None, expected classification, expected mismatch).
SHAPES = (
    pytest.param(
        {"result.json": _captured(-1), "verifier/reward.txt": "-1"},
        None,
        None,
        False,
        id="captured-minus-one",
    ),
    pytest.param(
        {"result.json": _captured(True), "verifier/reward.txt": "0"},
        None,
        None,
        False,
        id="captured-boolean-with-reward-file",
    ),
    pytest.param(
        {
            "result.json": '{"trial_name": "trial", '
            '"verifier_result": {"rewards": {"reward": NaN}}}',
            "verifier/reward.txt": "1",
        },
        None,
        None,
        False,
        id="captured-non-finite-with-reward-file",
    ),
    pytest.param(
        {
            "result.json": {"trial_name": "trial", "verifier_result": {}},
            "verifier/reward.json": {"reward": 0.25},
            "verifier/ctrf.json": {"results": {"tests": [{"status": "failed"}]}},
        },
        0.25,
        "forged",
        False,
        id="json-reward-file-only",
    ),
    pytest.param(
        {
            "result.json": {"trial_name": "trial", "verifier_result": {}},
            "verifier/reward.json": {"reward": 1.0},
            "verifier/reward.txt": "0",
        },
        1.0,
        None,
        True,
        id="json-reward-file-beats-text",
    ),
    pytest.param(
        {
            "result.json": _captured(1),
            "verifier/ctrf.json": {
                "results": {"tests": [], "summary": {"tests": 19, "passed": 0, "failed": 19}}
            },
        },
        1.0,
        "forged",
        False,
        id="summary-only-forged-ctrf",
    ),
    pytest.param(
        {
            "result.json": {
                "trial_name": "trial",
                "exception_info": {"exception_type": "RuntimeError"},
                "verifier_result": {},
            },
            "verifier/reward.txt": "1",
        },
        None,
        None,
        False,
        id="exception-with-leftover-file",
    ),
)


def _probe_reward(trial: Path) -> float | None:
    value, scored, _ = probe03.read_reward(trial)
    return value if scored else None


def _trace_reward(trial: Path) -> float | None:
    return trace._rewards(trial)["value"]


def _report_reward(trial: Path) -> float | None:
    record = reward_report.assess(trial)
    return None if record is None else record["reward"]


def _builder_reward(build: ModuleType, trial: Path) -> float | None:
    result = build.read_json(trial / "result.json")
    return build.reward_values(trial, result)[0]


@pytest.mark.parametrize(("files", "expected", "classification", "mismatch"), SHAPES)
def test_every_reader_returns_one_reward_or_unscored(
    build: ModuleType,
    tmp_path: Path,
    files: dict[str, Any],
    expected: float | None,
    classification: str | None,
    mismatch: bool,
) -> None:
    trial = _trial(tmp_path, files)

    values = {
        "probe03": _probe_reward(trial),
        "trace": _trace_reward(trial),
        "reward_report": _report_reward(trial),
        "builder": _builder_reward(build, trial),
    }

    assert values == dict.fromkeys(values, expected)


@pytest.mark.parametrize(("files", "expected", "classification", "mismatch"), SHAPES)
def test_ctrf_counts_and_contradictions_agree(
    build: ModuleType,
    tmp_path: Path,
    files: dict[str, Any],
    expected: float | None,
    classification: str | None,
    mismatch: bool,
) -> None:
    trial = _trial(tmp_path, files)
    report = reward_report.ctrf_counts(trial)
    built = build.ctrf_counts(build.read_json(trial / "verifier" / "ctrf.json").get("results"))
    assert (None if report is None else tuple(report.values())) == built

    record = reward_report.assess(trial)
    row = build.trial_row(
        "2026-11-15", "job", trial, build.read_json(trial / "result.json")
    )
    assert (None if row is None else row["classification"]) == classification
    assert (None if record is None else record["classification"]) == classification
    assert (False if record is None else record["mismatch"]) is mismatch
