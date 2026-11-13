"""Fixture tests for the library verifier-headroom builder.

Every case writes its own results root and task tree under ``tmp_path``: the
tests never read ``~/Developer/eval-lab-results`` or the real ``library/tasks``.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "research/experiments/library-verifier-headroom/build.py"
QUERY_OPTIMIZE = "terminal-bench/query-optimize"


def load_builder():
    spec = importlib.util.spec_from_file_location("library_verifier_headroom", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_tasks(build, tasks_root: Path, timeouts: dict[str, float] | None = None) -> None:
    """Write a task.toml for every declared package except omitted ones."""
    overrides = timeouts or {}
    for name in build.PACKAGES:
        if overrides.get(name) is None and name in overrides:
            continue
        directory = tasks_root / name.replace("/", "__")
        directory.mkdir(parents=True, exist_ok=True)
        timeout = overrides.get(name, 60.0)
        mode = "shared" if name == QUERY_OPTIMIZE else "separate"
        (directory / "task.toml").write_text(
            f'[task]\nname = "{name}"\n\n'
            f'[verifier]\ntimeout_sec = {timeout}\nenvironment_mode = "{mode}"\n',
            encoding="utf-8",
        )


def add_trial(
    root: Path,
    *,
    job: str,
    trial: str,
    package: str,
    date: str = "2026-11-13",
    stdout: str | None = None,
    reward: float = 1.0,
    started: str = "2026-11-13T00:00:00.000000Z",
    finished: str = "2026-11-13T00:00:20.000000Z",
    exception: str | None = None,
    mode: str = "shared",
) -> Path:
    """Write one published job/trial pair; returns the trial directory."""
    trial_dir = root / date / job / trial
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    exception_info = None
    if exception is not None:
        exception_info = {"exception_type": exception, "exception_message": "boom"}
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": package,
                "verifier": {"started_at": started, "finished_at": finished},
                "verifier_result": {"rewards": {"reward": reward}},
                "exception_info": exception_info,
                "verifier_environment_mode": mode,
            }
        ),
        encoding="utf-8",
    )
    if stdout is not None:
        (trial_dir / "verifier" / "test-stdout.txt").write_text(stdout, encoding="utf-8")
    return trial_dir


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_tables(build, tmp_path: Path, tasks: dict[str, float] | None = None):
    """Build both CSVs from fixture inputs and return (trials, headroom)."""
    root = tmp_path / "results"
    tasks_root = tmp_path / "tasks"
    root.mkdir(exist_ok=True)
    write_tasks(build, tasks_root, tasks)
    out = tmp_path / "out"
    out.mkdir()
    status = build.main(
        ["--root", str(root), "--tasks-root", str(tasks_root), "--out-dir", str(out)]
    )
    assert status == 0
    return read_rows(out / "trials.csv"), read_rows(out / "headroom.csv"), out


def headroom_row(rows: list[dict[str, str]], package: str) -> dict[str, str]:
    matches = [row for row in rows if row["package"] == package]
    assert len(matches) == 1
    return matches[0]


def test_trial_row_parses_last_pytest_summary_and_phase(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(
        root,
        job="job-a",
        trial="trial-a",
        package=QUERY_OPTIMIZE,
        stdout=(
            "collected 6 items\n===== 1 failed in 7.50s =====\n===== 6 passed in 12.34s =====\n"
        ),
    )
    add_trial(root, job="job-a", trial="trial-b", package=QUERY_OPTIMIZE, reward=0.0)
    add_trial(
        root,
        job="job-b",
        trial="trial-c",
        package="local-lab/event-summary",
        started="2026-11-13T00:00:00.000000Z",
        finished="2026-11-13T00:02:10.673000Z",
    )
    tasks_root = tmp_path / "tasks"
    write_tasks(build, tasks_root, {QUERY_OPTIMIZE: 1800.0})
    out = tmp_path / "out"

    assert (
        build.main(["--root", str(root), "--tasks-root", str(tasks_root), "--out-dir", str(out)])
        == 0
    )

    trials = read_rows(out / "trials.csv")
    assert [row["trial"] for row in trials] == ["trial-a", "trial-b", "trial-c"]
    assert tuple(trials[0]) == build.TRIAL_COLUMNS
    assert trials[0] == {
        "date": "2026-11-13",
        "job": "job-a",
        "trial": "trial-a",
        "package": QUERY_OPTIMIZE,
        "reward": "1.0",
        "phase_s": "20.0",
        "pytest_s": "12.34",
        "timeout_sec": "1800.0",
        "exception_type": "",
    }
    assert trials[1]["pytest_s"] == ""
    assert trials[1]["reward"] == "0.0"
    assert trials[2]["phase_s"] == "130.673"
    assert trials[2]["timeout_sec"] == "60.0"


def test_headroom_statistics_and_pytest_ratio(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    for index, (phase, pytest_s) in enumerate(((10.0, 1.0), (20.0, 2.0), (30.0, 90.0)), start=1):
        add_trial(
            root,
            job="job-a",
            trial=f"trial-{index}",
            package=QUERY_OPTIMIZE,
            stdout=f"===== 6 passed in {pytest_s}s =====\n",
            started="2026-11-13T00:00:00.000000Z",
            finished=f"2026-11-13T00:00:{int(phase):02d}.000000Z",
        )
    tasks_root = tmp_path / "tasks"
    write_tasks(build, tasks_root, {QUERY_OPTIMIZE: 100.0})
    out = tmp_path / "out"

    assert (
        build.main(["--root", str(root), "--tasks-root", str(tasks_root), "--out-dir", str(out)])
        == 0
    )

    rows = read_rows(out / "headroom.csv")
    assert tuple(rows[0]) == build.HEADROOM_COLUMNS
    row = headroom_row(rows, QUERY_OPTIMIZE)
    assert row["trials"] == "3"
    assert row["n"] == "3"
    assert row["phase_p50_s"] == "20.0"
    assert row["phase_p95_s"] == "29.0"
    assert row["phase_max_s"] == "30.0"
    assert row["pytest_max_s"] == "90.0"
    assert row["pytest_n"] == "3"
    assert row["max_pytest_ratio"] == "0.9"
    assert row["timeout_sec"] == "100.0"
    assert row["timeout_exceptions"] == "0"
    assert row["modes_seen"] == "shared:3"
    assert row["task_path"].endswith("__query-optimize/task.toml")


def test_timeout_exception_is_counted(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(
        root,
        job="job-a",
        trial="trial-a",
        package=QUERY_OPTIMIZE,
        exception="VerifierTimeoutError",
        reward=0.0,
    )
    add_trial(
        root,
        job="job-a",
        trial="trial-b",
        package=QUERY_OPTIMIZE,
        exception="EnvironmentStartTimeoutError",
        reward=0.0,
    )
    trials, headroom, _ = build_tables(build, tmp_path, {QUERY_OPTIMIZE: 1800.0})
    assert [row["exception_type"] for row in trials] == [
        "VerifierTimeoutError",
        "EnvironmentStartTimeoutError",
    ]
    row = headroom_row(headroom, QUERY_OPTIMIZE)
    assert row["timeout_exceptions"] == "1"
    assert row["exception_types"] == ("EnvironmentStartTimeoutError:1;VerifierTimeoutError:1")
    assert row["n"] == "2"
    assert row["pytest_max_s"] == ""
    assert row["max_pytest_ratio"] == ""


def test_unlisted_and_missing_variants_are_excluded(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(root, job="job-a", trial="trial-a", package=QUERY_OPTIMIZE)
    add_trial(
        root,
        job="job-a",
        trial="upstream",
        package="terminal-bench/break-filter-js-from-html",
    )
    add_trial(root, job="job-a", trial="other", package="mimo-v2.6-rl/candidate-0260")
    add_trial(
        root,
        job="job-b",
        trial="unreadable",
        package=QUERY_OPTIMIZE,
        stdout="===== 1 passed in 4.00s =====\n",
    )
    (root / "2026-11-13" / "job-b" / "unreadable" / "result.json").unlink()
    (root / "not-a-date" / "job-c" / "trial-c").mkdir(parents=True)
    (root / "not-a-date" / "job-c" / "trial-c" / "result.json").write_text(
        json.dumps({"task_name": QUERY_OPTIMIZE}), encoding="utf-8"
    )
    trials, headroom, _ = build_tables(build, tmp_path, {QUERY_OPTIMIZE: 1800.0})
    assert [row["trial"] for row in trials] == ["trial-a"]
    assert headroom_row(headroom, QUERY_OPTIMIZE)["trials"] == "1"
    assert len(headroom) == len(build.PACKAGES)


def test_check_passes_clean_and_fails_on_either_drift(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(root, job="job-a", trial="trial-a", package=QUERY_OPTIMIZE)
    tasks_root = tmp_path / "tasks"
    write_tasks(build, tasks_root, {QUERY_OPTIMIZE: 1800.0})
    out = tmp_path / "out"
    out.mkdir()
    argv = ["--root", str(root), "--tasks-root", str(tasks_root), "--out-dir", str(out)]
    assert build.main(argv) == 0
    assert build.main(["--check", *argv]) == 0

    (out / "headroom.csv").write_text("drift\n", encoding="utf-8")
    assert build.main(["--check", *argv]) == 1

    assert build.main(argv) == 0
    (out / "trials.csv").write_text("", encoding="utf-8")
    assert build.main(["--check", *argv]) == 1


def test_missing_results_root_and_task_metadata_exit_2(tmp_path: Path) -> None:
    build = load_builder()
    absent = tmp_path / "absent"
    tasks_root = tmp_path / "tasks"
    write_tasks(build, tasks_root)
    out = tmp_path / "out"
    assert (
        build.main(["--root", str(absent), "--tasks-root", str(tasks_root), "--out-dir", str(out)])
        == 2
    )

    root = tmp_path / "results"
    (root / "2026-11-13" / "job" / "trial").mkdir(parents=True)
    missing = tmp_path / "missing-tasks"
    write_tasks(build, missing, {QUERY_OPTIMIZE: None})
    assert (
        build.main(["--root", str(root), "--tasks-root", str(missing), "--out-dir", str(out)]) == 2
    )
