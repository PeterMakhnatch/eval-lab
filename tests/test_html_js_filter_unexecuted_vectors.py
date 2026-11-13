"""Offline HAR-424 contracts for HTML-filter unexecuted-vector accounting."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / "research/experiments/html-js-filter-unexecuted-vectors/build.py"
TASK_NAME = "terminal-bench/html-js-filter"
HEADER = "job,trial,role,reward,batches_run,timed_out_batches,vectors_unexecuted\n"


@pytest.fixture
def builder() -> ModuleType:
    assert BUILDER.is_file(), f"HAR-424 builder is missing: {BUILDER}"
    spec = importlib.util.spec_from_file_location("html_js_filter_vectors_test", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_trial(
    root: Path,
    *,
    job: str = "job-a",
    trial: str = "trial-a",
    task_name: str = TASK_NAME,
    role: str | None = "oracle",
    stdout: str | None = "",
    reward: str | None = "1.0\n",
    config_role: str | None = None,
) -> Path:
    trial_dir = root / "2026-11-13" / job / trial
    verifier = trial_dir / "verifier"
    verifier.mkdir(parents=True)
    result = {"task_name": task_name}
    if role is not None:
        result["config"] = {"agent": {"name": role}}
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if config_role is not None:
        (trial_dir / "config.json").write_text(
            json.dumps({"agent": {"name": config_role}}), encoding="utf-8"
        )
    if stdout is not None:
        (verifier / "test-stdout.txt").write_text(stdout, encoding="utf-8")
    if reward is not None:
        (verifier / "reward.txt").write_text(reward, encoding="utf-8")
    return trial_dir


def test_timed_out_batch_counts_sixteen_unexecuted_vectors(
    builder: ModuleType, tmp_path: Path
) -> None:
    stdout = (
        "RUN BATCH 1 (contains 16 tests)\n"
        "  Failed to load URL: Page.goto: Timeout 20000ms exceeded.\n"
        "RUN BATCH 2 (contains 16 tests)\n"
        "Batch completed.\n"
        "RUN BATCH 3 (contains 16 tests)\n"
        "Batch completed.\n"
        "✓ Filter successfully blocked all 444 XSS attack vectors!\n"
    )
    write_trial(tmp_path, stdout=stdout)

    assert builder.parse_stdout(stdout) == (3, 1)
    assert builder.scan_results(tmp_path) == [
        {
            "job": "job-a",
            "trial": "trial-a",
            "role": "oracle",
            "reward": "1.0",
            "batches_run": 3,
            "timed_out_batches": 1,
            "vectors_unexecuted": 16,
        }
    ]


def test_clean_run_has_no_unexecuted_vectors(builder: ModuleType, tmp_path: Path) -> None:
    stdout = "".join(f"RUN BATCH {index} (contains 16 tests)\n" for index in range(1, 29))
    stdout += "✓ Filter successfully blocked all 444 XSS attack vectors!\n"
    write_trial(tmp_path, stdout=stdout)

    assert builder.parse_stdout(stdout) == (28, 0)
    row = builder.scan_results(tmp_path)[0]
    assert (row["batches_run"], row["timed_out_batches"], row["vectors_unexecuted"]) == (
        28,
        0,
        0,
    )


def test_failure_before_browser_has_no_batches(builder: ModuleType, tmp_path: Path) -> None:
    stdout = "Failed to create browser or process file: BrowserType.launch: Target closed\n"
    write_trial(tmp_path, stdout=stdout, role="nop", reward="0.0\n")

    assert builder.parse_stdout(stdout) == (0, 0)
    row = builder.scan_results(tmp_path)[0]
    assert (row["batches_run"], row["timed_out_batches"], row["vectors_unexecuted"]) == (
        0,
        0,
        0,
    )


def test_failure_before_first_batch_is_ignored(builder: ModuleType, tmp_path: Path) -> None:
    stdout = (
        "Failed to load URL: pre-batch diagnostic\n"
        "RUN BATCH 1 (contains 16 tests)\n"
        "Batch completed.\n"
        "RUN BATCH 2 (contains 12 tests)\n"
    )
    write_trial(tmp_path, stdout=stdout)

    assert builder.parse_stdout(stdout) == (2, 0)
    row = builder.scan_results(tmp_path)[0]
    assert (row["batches_run"], row["timed_out_batches"], row["vectors_unexecuted"]) == (
        2,
        0,
        0,
    )


def test_timeout_count_is_per_block_and_includes_final_block(builder: ModuleType) -> None:
    stdout = (
        "RUN BATCH 1 (contains 16 tests)\n"
        "Failed to load URL: first attempt\n"
        "\tFailed to load URL: repeated attempt\n"
        "RUN BATCH 2 (contains 16 tests)\n"
        "Batch completed.\n"
        "RUN BATCH 3 (contains 12 tests)\n"
        "Failed to load URL: final block"
    )

    assert builder.parse_stdout(stdout) == (3, 2)


def test_batch_markers_must_match_complete_lines(builder: ModuleType) -> None:
    stdout = (
        "prefix RUN BATCH 1 (contains 16 tests)\n"
        " RUN BATCH 2 (contains 16 tests)\n"
        "RUN BATCH 3 (contains 16 tests) suffix\n"
        "RUN BATCH four (contains 16 tests)\n"
        "RUN BATCH 5 (contains sixteen tests)\n"
        "RUN BATCH 6 (contains 12 tests)\n"
    )

    assert builder.parse_stdout(stdout) == (1, 0)


def test_scan_excludes_other_tasks_and_missing_results(builder: ModuleType, tmp_path: Path) -> None:
    write_trial(tmp_path, trial="other-task", task_name="terminal-bench/other")
    incomplete = tmp_path / "2026-11-13" / "job-a" / "missing-result" / "verifier"
    incomplete.mkdir(parents=True)
    (incomplete / "test-stdout.txt").write_text(
        "RUN BATCH 1 (contains 16 tests)\nFailed to load URL: timeout\n", encoding="utf-8"
    )

    assert builder.scan_results(tmp_path) == []


def test_row_fields_preserve_names_role_reward_and_missing_artifacts(
    builder: ModuleType, tmp_path: Path
) -> None:
    write_trial(
        tmp_path,
        job="published-job",
        trial="published-trial",
        role="published-agent",
        config_role="fallback-must-not-win",
        reward="  1.0\n",
    )
    write_trial(tmp_path, job="unscored-job", trial="unscored-trial", reward=None, stdout=None)

    assert builder.scan_results(tmp_path) == [
        {
            "job": "published-job",
            "trial": "published-trial",
            "role": "published-agent",
            "reward": "1.0",
            "batches_run": 0,
            "timed_out_batches": 0,
            "vectors_unexecuted": 0,
        },
        {
            "job": "unscored-job",
            "trial": "unscored-trial",
            "role": "oracle",
            "reward": "",
            "batches_run": 0,
            "timed_out_batches": 0,
            "vectors_unexecuted": 0,
        },
    ]


@pytest.mark.parametrize(("config_role", "expected"), [("nop", "nop"), (None, "unknown")])
def test_role_falls_back_to_trial_config_then_unknown(
    builder: ModuleType, tmp_path: Path, config_role: str | None, expected: str
) -> None:
    write_trial(tmp_path, role=None, config_role=config_role)

    assert builder.scan_results(tmp_path)[0]["role"] == expected


def test_csv_header_field_order_and_trial_sorting(builder: ModuleType, tmp_path: Path) -> None:
    for job, trial in [("job-z", "trial-a"), ("job-a", "trial-z"), ("job-a", "trial-a")]:
        write_trial(tmp_path, job=job, trial=trial)

    rows = builder.scan_results(tmp_path)
    assert [(row["job"], row["trial"]) for row in rows] == [
        ("job-a", "trial-a"),
        ("job-a", "trial-z"),
        ("job-z", "trial-a"),
    ]
    assert tuple(HEADER.rstrip("\n").split(",")) == builder.COLUMNS
    assert all(set(row) == set(builder.COLUMNS) for row in rows)
    assert builder.render_csv(rows) == (
        HEADER
        + "job-a,trial-a,oracle,1.0,0,0,0\n"
        + "job-a,trial-z,oracle,1.0,0,0,0\n"
        + "job-z,trial-a,oracle,1.0,0,0,0\n"
    )
    assert builder.render_csv([]) == HEADER


def test_cli_writes_checks_and_detects_changed_count(builder: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "results"
    write_trial(root, stdout="RUN BATCH 1 (contains 16 tests)\nFailed to load URL: timeout\n")
    out = tmp_path / "output" / "trials.csv"
    args = ["--results-root", str(root), "--output", str(out)]
    expected = HEADER + "job-a,trial-a,oracle,1.0,1,1,16\n"

    assert builder.main(args) == 0
    assert out.read_bytes() == expected.encode("utf-8")
    assert builder.main([*args, "--check"]) == 0
    out.write_text(expected.replace(",1,1,16\n", ",2,1,16\n"), encoding="utf-8")
    assert builder.main([*args, "--check"]) == 1
    assert out.read_text(encoding="utf-8") == expected.replace(",1,1,16\n", ",2,1,16\n")


def test_cli_check_missing_output_does_not_create_it(builder: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "results"
    root.mkdir()
    out = tmp_path / "missing" / "trials.csv"

    assert builder.main(["--results-root", str(root), "--output", str(out), "--check"]) == 1
    assert not out.exists()


def test_cli_missing_results_root_returns_two(builder: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "missing-results"
    out = tmp_path / "output.csv"

    assert builder.main(["--results-root", str(root), "--output", str(out)]) == 2
    assert not root.exists()
    assert not out.exists()
