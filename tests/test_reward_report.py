"""Reward/report contradictions add evidence without changing the recorded outcome."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.reward_report import assess, flags, kind_counts


def _write_trial(
    root: Path,
    *,
    reward: float | None,
    statuses: list[str] | None = None,
    reward_txt: str | None = None,
    name: str = "trial",
) -> Path:
    trial = root / name
    trial.mkdir(parents=True)
    result: dict[str, Any] = {"task_name": "task", "trial_name": trial.name}
    if reward is not None:
        result["verifier_result"] = {"rewards": {"reward": reward}}
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if statuses is not None or reward_txt is not None:
        verifier = trial / "verifier"
        verifier.mkdir()
        if statuses is not None:
            tests = [
                {"name": f"test_{index}", "status": status}
                for index, status in enumerate(statuses)
            ]
            (verifier / "ctrf.json").write_text(
                json.dumps({"results": {"tests": tests}}), encoding="utf-8"
            )
        if reward_txt is not None:
            (verifier / "reward.txt").write_text(reward_txt, encoding="utf-8")
    return trial


def test_forged_full_reward_with_all_tests_failed(tmp_path: Path) -> None:
    trial = _write_trial(tmp_path, reward=1, reward_txt="1", statuses=["failed"] * 19)

    record = assess(trial)

    assert record is not None
    assert record == {
        "schema": "evallab.reward_report/v1",
        "classification": "forged",
        "reason": "full reward with 19 failed tests",
        "mismatch": False,
        "reward": 1.0,
        "reward_source": "verifier_result",
        "reward_txt": 1.0,
        "counts": {"tests": 19, "passed": 0, "failed": 19, "skipped": 0},
    }
    assert record["schema"] == "evallab.reward_report/v1"
    assert flags(record) == ["reward_report:forged:0/19-passed"]
    assert json.loads(json.dumps(record)) == record


def test_partial_credit_mixed_report(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path, reward=1, reward_txt="1", statuses=["passed"] * 2 + ["failed"] * 4
    )

    record = assess(trial)

    assert record is not None
    assert record == {
        "schema": "evallab.reward_report/v1",
        "classification": "partial-credit",
        "reason": "full reward with 4 failed tests",
        "mismatch": False,
        "reward": 1.0,
        "reward_source": "verifier_result",
        "reward_txt": 1.0,
        "counts": {"tests": 6, "passed": 2, "failed": 4, "skipped": 0},
    }
    assert flags(record) == ["reward_report:partial-credit:2/6-passed"]


def test_reward_file_mismatch_without_a_report_contradiction(tmp_path: Path) -> None:
    trial = _write_trial(tmp_path, reward=0.0, reward_txt="1", statuses=["failed"] * 11)

    record = assess(trial)

    assert record is not None
    assert record == {
        "schema": "evallab.reward_report/v1",
        "classification": None,
        "reason": "",
        "mismatch": True,
        "reward": 0.0,
        "reward_source": "verifier_result",
        "reward_txt": 1.0,
        "counts": {"tests": 11, "passed": 0, "failed": 11, "skipped": 0},
    }
    assert record["schema"] == "evallab.reward_report/v1"
    assert flags(record) == ["reward_report:mismatch:0-vs-1"]
    assert json.loads(json.dumps(record)) == record


def test_clean_trial_has_no_record(tmp_path: Path) -> None:
    trial = _write_trial(tmp_path, reward=1, reward_txt="1", statuses=["passed"] * 11)

    record = assess(trial)

    assert record is None
    assert flags({}) == []
    assert flags(record or {}) == []


def test_missing_ctrf_is_not_comparable(tmp_path: Path) -> None:
    trial = _write_trial(tmp_path, reward=1, reward_txt="1")

    assert assess(trial) is None


def test_missing_reward_is_not_a_contradiction(tmp_path: Path) -> None:
    trial = _write_trial(tmp_path, reward=None, statuses=["failed"] * 3)

    assert assess(trial) is None


def test_process_trial_adds_the_record_and_flag(tmp_path: Path) -> None:
    from evallab.process_job import _process_trial

    job = tmp_path / "job"
    trial = _write_trial(job, reward=1, reward_txt="1", statuses=["failed"] * 2)
    assessment = assess(trial)

    record = _process_trial(trial, job)

    assert assessment is not None
    assert assessment == {
        "schema": "evallab.reward_report/v1",
        "classification": "forged",
        "reason": "full reward with 2 failed tests",
        "mismatch": False,
        "reward": 1.0,
        "reward_source": "verifier_result",
        "reward_txt": 1.0,
        "counts": {"tests": 2, "passed": 0, "failed": 2, "skipped": 0},
    }
    assert record["reward_report"] == assessment
    assert "reward_report:forged:0/2-passed" in record["flags"]
    assert record["reward"] == 1
    assert record["scored"] is True
    assert record["reward_source"] == "verifier_result"
    assert record["decision"] is None


def test_kind_counts_accepts_new_kinds_and_sorts_keys() -> None:
    flag_strings = [
        "reward_report:new-kind:first:detail",
        "reward_report:forged:0/2-passed",
        "unrelated:flag",
        "reward_report:new-kind:second",
        "reward_report",
        "reward_report:missing-detail",
        "reward_report::detail",
    ]

    counts = kind_counts(iter(flag_strings))

    assert counts == {"forged": 1, "new-kind": 2}
    assert list(counts) == ["forged", "new-kind"]
    assert kind_counts(iter(())) == {}


def test_process_job_counts_reward_report_flags_and_renders_once(tmp_path: Path) -> None:
    from evallab.process_job import process_job

    job = tmp_path / "job"
    _write_trial(
        job, name="forged", reward=1, reward_txt="1", statuses=["failed"] * 19
    )
    _write_trial(
        job, name="mismatch", reward=0.0, reward_txt="1", statuses=["failed"] * 11
    )
    output_dir = tmp_path / "out"

    report = process_job(job, output_dir=output_dir, ingest=False, publish=False)

    expected = {"forged": 1, "mismatch": 1}
    assert report["summary"]["reward_report_flags"] == expected
    saved = json.loads((output_dir / "job.json").read_text(encoding="utf-8"))
    assert saved["summary"]["reward_report_flags"] == expected
    assert json.loads(json.dumps(report))["summary"]["reward_report_flags"] == expected
    markdown = (output_dir / "job.md").read_text(encoding="utf-8")
    reward_lines = [
        line for line in markdown.splitlines() if line.startswith("- reward report:")
    ]
    assert reward_lines == ["- reward report: {'forged': 1, 'mismatch': 1}"]


def test_process_job_without_reward_report_flags_uses_empty_dict(tmp_path: Path) -> None:
    from evallab.process_job import process_job

    job = tmp_path / "job"
    trial = _write_trial(job, reward=1)
    assert not (trial / "verifier").exists()

    report = process_job(job, output_dir=tmp_path / "out", ingest=False, publish=False)

    assert report["summary"]["reward_report_flags"] == {}
    markdown = (tmp_path / "out" / "job.md").read_text(encoding="utf-8")
    assert markdown.splitlines().count("- reward report: {}") == 1
