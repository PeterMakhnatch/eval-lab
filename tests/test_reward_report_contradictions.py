"""Offline fixture contracts for the reward/report contradiction table (HAR-437)."""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "research/experiments/reward-report-contradictions/build.py"
HEADER = (
    "date,job,trial,task,reward,reward_source,reward_txt,tests,passed,failed,skipped,"
    "reason,classification"
)


@pytest.fixture
def build():
    spec = importlib.util.spec_from_file_location("reward_report_contradictions", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def add_trial(
    root: Path,
    *,
    date: str = "2026-11-14",
    job: str = "job",
    trial: str = "trial",
    task: str = "terminal-bench/example",
    reward: float | None = 1,
    reward_txt: str | None = None,
    statuses: tuple[str, ...] | None = ("failed",),
) -> Path:
    trial_dir = root / date / job / trial
    verifier = trial_dir / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    result = {"task_name": task}
    if reward is not None:
        result["verifier_result"] = {"rewards": {"reward": reward}}
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if reward_txt is not None:
        (verifier / "reward.txt").write_text(reward_txt, encoding="utf-8")
    if statuses is not None:
        write_report(trial_dir, {"results": {"tests": [{"status": s} for s in statuses]}})
    return trial_dir


def write_report(trial_dir: Path, report: dict) -> None:
    (trial_dir / "verifier" / "ctrf.json").write_text(json.dumps(report), encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_table(build, tmp_path: Path) -> list[dict[str, str]]:
    root = tmp_path / "results"
    root.mkdir(exist_ok=True)
    out = tmp_path / "out"
    assert build.main(["--root", str(root), "--out-dir", str(out)]) == 0
    return read_rows(out / "contradictions.csv")


def test_har_434_exact_csv_row(build, tmp_path: Path) -> None:
    add_trial(
        tmp_path / "results",
        job="HAR-434-har-ico-cheat-exact",
        trial="har-ico-cheat-exact__gvP3TLq",
        task="terminal-bench/ico-path-patch",
        reward_txt="1",
        statuses=("failed",) * 19,
    )
    rows = build_table(build, tmp_path)
    target = tmp_path / "out" / "contradictions.csv"
    with target.open(newline="", encoding="utf-8") as handle:
        assert csv.DictReader(handle).fieldnames == HEADER.split(",")
    assert target.read_bytes().startswith((HEADER + "\n").encode("utf-8"))
    assert b"\r" not in target.read_bytes()
    assert rows == [
        {
            "date": "2026-11-14",
            "job": "HAR-434-har-ico-cheat-exact",
            "trial": "har-ico-cheat-exact__gvP3TLq",
            "task": "terminal-bench/ico-path-patch",
            "reward": "1",
            "reward_source": "result",
            "reward_txt": "1",
            "tests": "19",
            "passed": "0",
            "failed": "19",
            "skipped": "0",
            "reason": "full reward with 19 failed tests",
            "classification": "forged",
        }
    ]


def test_consistent_and_missing_reports_emit_no_rows(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "results"
    add_trial(root, trial="passed", statuses=("passed", "passed"))
    add_trial(root, trial="zero-reward", reward=0)
    add_trial(root, trial="no-report", statuses=None)
    assert build_table(build, tmp_path) == []
    assert (tmp_path / "out" / "contradictions.csv").read_text(encoding="utf-8") == HEADER + "\n"
    assert capsys.readouterr().out == (
        "scanned=3 comparable=2 with_reward=3 contradictions=0 "
        "forged=0 partial-credit=0 unknown=0\nwrote contradictions.csv\n"
    )


def test_all_skipped_positive_reward_is_unknown(build, tmp_path: Path) -> None:
    add_trial(tmp_path / "results", reward=0.5, statuses=("skipped", "pending"))
    row, = build_table(build, tmp_path)
    assert (row["tests"], row["passed"], row["failed"], row["skipped"]) == ("2", "0", "0", "2")
    assert row["reason"] == "positive reward with no passed test (0 failed, 2 skipped)"
    assert row["classification"] == "unknown"


def test_full_reward_with_partial_passes(build, tmp_path: Path) -> None:
    add_trial(tmp_path / "results", statuses=("passed",) * 2 + ("failed",) * 4)
    row, = build_table(build, tmp_path)
    assert (row["tests"], row["passed"], row["failed"], row["skipped"]) == ("6", "2", "4", "0")
    assert row["reason"] == "full reward with 4 failed tests"
    assert row["classification"] == "partial-credit"


def test_reward_txt_fallback_without_verifier_result(build, tmp_path: Path) -> None:
    add_trial(tmp_path / "results", reward=None, reward_txt="0.5", statuses=("failed",) * 2)
    row, = build_table(build, tmp_path)
    assert (row["reward"], row["reward_source"], row["reward_txt"]) == ("0.5", "reward_txt", "0.5")
    assert row["reason"] == "positive reward with no passed test (2 failed, 0 skipped)"
    assert row["classification"] == "forged"


def test_result_reward_wins_but_disagreement_is_visible(build, tmp_path: Path) -> None:
    add_trial(tmp_path / "results", reward_txt="0", statuses=("failed", "failed"))
    row, = build_table(build, tmp_path)
    assert (row["reward"], row["reward_source"], row["reward_txt"]) == ("1", "result", "0")
    assert row["reason"] == "full reward with 2 failed tests; reward.txt says 0"
    assert row["classification"] == "forged"


def test_ctrf_summary_fallback_is_comparable(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trial = add_trial(tmp_path / "results", statuses=None)
    write_report(trial, {"results": {"summary": {"tests": 6, "passed": 2, "failed": 3}}})
    row, = build_table(build, tmp_path)
    assert (row["tests"], row["passed"], row["failed"], row["skipped"]) == ("6", "2", "3", "1")
    assert row["reason"] == "full reward with 3 failed tests"
    assert row["classification"] == "partial-credit"
    assert capsys.readouterr().out == (
        "scanned=1 comparable=1 with_reward=1 contradictions=1 "
        "forged=0 partial-credit=1 unknown=0\nwrote contradictions.csv\n"
    )


def test_malformed_and_empty_ctrf_are_not_comparable(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payloads = ("{invalid", "[]", "{}", '{"results": {"tests": []}}')
    for index, payload in enumerate(payloads):
        trial = add_trial(tmp_path / "results", trial=f"invalid-{index}", statuses=None)
        (trial / "verifier" / "ctrf.json").write_text(payload, encoding="utf-8")
    assert build_table(build, tmp_path) == []
    assert capsys.readouterr().out == (
        "scanned=4 comparable=0 with_reward=4 contradictions=0 "
        "forged=0 partial-credit=0 unknown=0\nwrote contradictions.csv\n"
    )


def test_scan_uses_only_exact_published_layout(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "results"
    valid = add_trial(root, trial="valid")
    add_trial(root, date="not-a-date", trial="wrong-date")
    (root / "2026-11-14" / "job" / "result.json").write_text("{}", encoding="utf-8")
    add_trial(root, job="job/sub", trial="too-deep")
    unpublished = add_trial(root, trial="unpublished")
    (unpublished / "result.json").unlink()
    (root / "2026-11-14" / "not-a-directory").write_text("ignored", encoding="utf-8")
    rows = build_table(build, tmp_path)
    assert [row["trial"] for row in rows] == [valid.name]
    assert capsys.readouterr().out == (
        "scanned=1 comparable=1 with_reward=1 contradictions=1 "
        "forged=1 partial-credit=0 unknown=0\nwrote contradictions.csv\n"
    )


def test_check_detects_cell_drift_and_missing_csv_without_writes(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "results"
    add_trial(root)
    build_table(build, tmp_path)
    out = tmp_path / "out"
    original = (out / "contradictions.csv").read_bytes()
    assert original.splitlines()[0].decode("utf-8") == HEADER
    copied = tmp_path / "copied"
    shutil.copytree(out, copied)
    target = copied / "contradictions.csv"
    argv = ["--check", "--root", str(root), "--out-dir", str(copied)]
    capsys.readouterr()
    assert build.main(argv) == 0
    assert target.read_bytes() == original
    assert "reproduces from the results root" in capsys.readouterr().out

    rows = read_rows(target)
    rows[0]["task"] = "terminal-bench/changed"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER.split(","), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    drifted = target.read_bytes()
    assert build.main(argv) == 1
    assert target.read_bytes() == drifted
    error = capsys.readouterr().err
    assert "missing or stale" in error
    assert "--- contradictions.csv (committed)" in error
    assert "+++ contradictions.csv (rebuilt)" in error
    assert "terminal-bench/changed" in error

    target.unlink()
    assert build.main(argv) == 1
    assert not target.exists()
    assert "missing or stale" in capsys.readouterr().err
    assert build.main(["--check", "--root", str(root), "--out-dir", str(out)]) == 0
    assert (out / "contradictions.csv").read_bytes() == original


def test_missing_results_root_exits_2_without_creating_output(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "missing-results"
    out = tmp_path / "out"
    assert build.main(["--root", str(root), "--out-dir", str(out)]) == 2
    assert capsys.readouterr().err == f"build: results root is missing or not a directory: {root}\n"
    assert not out.exists()
    assert not root.exists()


def test_build_is_byte_deterministic_and_sorts_date_job_trial(build, tmp_path: Path) -> None:
    root = tmp_path / "results"
    paths = [
        ("2026-11-15", "job-a", "trial-a"),
        ("2026-11-14", "job-b", "trial-a"),
        ("2026-11-14", "job-a", "trial-b"),
        ("2026-11-14", "job-a", "trial-a"),
    ]
    for date, job, trial in paths:
        add_trial(root, date=date, job=job, trial=trial)
    rows = build_table(build, tmp_path)
    first = (tmp_path / "out" / "contradictions.csv").read_bytes()
    assert [(row["date"], row["job"], row["trial"]) for row in rows] == sorted(paths)
    second_out = tmp_path / "second-out"
    assert build.main(["--root", str(root), "--out-dir", str(second_out)]) == 0
    assert (second_out / "contradictions.csv").read_bytes() == first


def test_reason_classification_and_number_text_units(build) -> None:
    assert build.contradiction_reason(1, (2, 2, 0, 0)) == ""
    assert build.contradiction_reason(0, (2, 0, 2, 0)) == ""
    assert build.contradiction_reason(0.5, (2, 1, 1, 0)) == ""
    assert build.contradiction_reason(1, (0, 0, 0, 0)) == ""
    assert build.contradiction_reason(1, (1, 0, 1, 0)) == "full reward with 1 failed test"
    assert build.contradiction_reason(1, (2, 0, 2, 0)) == "full reward with 2 failed tests"
    assert build.classify(1, (2, 0, 2, 0)) == "forged"
    assert build.classify(1, (3, 1, 2, 0)) == "partial-credit"
    assert build.classify(0.5, (2, 0, 0, 2)) == "unknown"
    assert build.classify(0, (2, 0, 2, 0)) == ""
    assert build.number_text(1.0) == "1"
    assert build.number_text(0.5) == "0.5"
