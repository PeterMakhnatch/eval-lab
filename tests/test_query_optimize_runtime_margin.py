"""Fixture tests for the query-optimize runtime-margin builder.

Every case writes its own results root under ``tmp_path``: the tests never read
``~/Developer/eval-lab-results``.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "research/experiments/query-optimize-runtime-margin/build.py"
#: Shape of the dict ``test_compare_golden_vs_solution_runtime`` prints.
RUNTIME_LINE = (
    "Running iteration 0 of 5\n"
    "{'iterations': 5, 'golden': {'median_s': 1.0, 'min_s': 0.9, 'max_s': 1.1}, "
    "'solution': {'median_s': 1.02, 'min_s': 0.95, 'max_s': 1.05}, "
    "'speedup_solution_vs_golden': 0.9803921568627451}\n"
    "6 passed in 12.34s\n"
)


def load_builder():
    spec = importlib.util.spec_from_file_location("query_optimize_runtime_margin", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def add_trial(
    root: Path,
    *,
    job: str,
    trial: str,
    task_name: str,
    stdout: str | None = None,
    reward: float = 1.0,
    agent: str = "oracle",
    command: list[str] | None = None,
) -> Path:
    """Write one published job/trial pair; returns the trial directory."""
    job_dir = root / "2026-01-01" / job
    trial_dir = job_dir / trial
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    metadata = {"task_staging": {"declared_task_name": task_name}}
    if command is not None:
        metadata["command"] = command
    (job_dir / "lab-metadata.json").write_text(json.dumps(metadata))
    (trial_dir / "config.json").write_text(
        json.dumps({"agent": {"name": agent}, "task": {"path": f"/stage/{trial}"}})
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": task_name,
                "task_id": {"path": f"/stage/{trial}"},
                "verifier_result": {"rewards": {"reward": reward}},
                "agent_info": {"name": agent},
            }
        )
    )
    if stdout is not None:
        (trial_dir / "verifier" / "test-stdout.txt").write_text(stdout)
    return trial_dir


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_build_parses_ratio_and_keeps_unmeasured_trials(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(
        root,
        job="000-oracle",
        trial="oracle__aaa",
        task_name="terminal-bench/query-optimize",
        stdout=RUNTIME_LINE,
    )
    add_trial(
        root,
        job="001-nop",
        trial="nop__bbb",
        task_name="terminal-bench/query-optimize",
        reward=0.0,
        agent="nop",
    )
    add_trial(
        root,
        job="002-other",
        trial="other__ccc",
        task_name="local-lab/event-summary",
        stdout=RUNTIME_LINE,
    )
    out = tmp_path / "ratios.csv"

    assert build.main(["--root", str(root), "--out", str(out)]) == 0

    rows = read_rows(out)
    assert [row["trial"] for row in rows] == ["oracle__aaa", "nop__bbb"]
    assert tuple(rows[0]) == build.COLUMNS
    assert rows[0] == {
        "job": "000-oracle",
        "trial": "oracle__aaa",
        "role": "oracle",
        "reward": "1.0",
        "golden_median_s": "1.0",
        "solution_median_s": "1.02",
        "ratio": "1.02",
    }
    assert rows[1]["reward"] == "0.0"
    assert rows[1]["golden_median_s"] == ""
    assert rows[1]["solution_median_s"] == ""
    assert rows[1]["ratio"] == ""


def test_check_passes_clean_csv_and_fails_on_drift(
    tmp_path: Path, capsys
) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(
        root,
        job="000-oracle",
        trial="oracle__aaa",
        task_name="terminal-bench/query-optimize",
        stdout=RUNTIME_LINE,
    )
    out = tmp_path / "ratios.csv"
    argv = ["--root", str(root), "--out", str(out)]

    assert build.main(argv) == 0
    capsys.readouterr()
    assert build.main(["--check", *argv]) == 0
    assert "reproduces" in capsys.readouterr().out

    out.write_text(out.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    capsys.readouterr()
    assert build.main([*argv, "--check"]) == 1
    assert "stale" in capsys.readouterr().err


def test_missing_results_root_is_an_error(tmp_path: Path) -> None:
    build = load_builder()
    absent = tmp_path / "absent"
    assert build.main(["--root", str(absent), "--out", str(tmp_path / "out.csv")]) == 2


def test_role_uses_job_token_then_ladder_marker_then_agent(tmp_path: Path) -> None:
    build = load_builder()
    root = tmp_path / "results"
    add_trial(
        root,
        job="har-qo-hardened",
        trial="hardened__aaa",
        task_name="terminal-bench/query-optimize",
        agent="evallab.harbor_cheat:CheatAgent",
        command=["harbor", "run", "--agent-env", "EVALLAB_CHEAT_LADDER_VERSION=4.0.0"],
    )
    add_trial(
        root,
        job="har-qo-hardened-control",
        trial="control__bbb",
        task_name="terminal-bench/query-optimize",
        agent="evallab.harbor_cheat:CheatAgent",
    )
    add_trial(
        root,
        job="har-qo-interop-cheat",
        trial="cheat__ccc",
        task_name="terminal-bench/query-optimize",
        agent="evallab.harbor_cheat:CheatAgent",
        command=["harbor", "run", "--agent-env", "EVALLAB_CHEAT_LADDER_VERSION=4.0.0"],
    )
    out = tmp_path / "ratios.csv"

    assert build.main(["--root", str(root), "--out", str(out)]) == 0

    roles = {row["trial"]: row["role"] for row in read_rows(out)}
    assert roles == {
        "hardened__aaa": "ladder",
        "control__bbb": "cheat",
        "cheat__ccc": "cheat",
    }
