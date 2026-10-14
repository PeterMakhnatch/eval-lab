from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from evallab.registry import compute_task_digests

RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "experiments"
    / "breaker-rediscovery-v1"
    / "run_pilot.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("breaker_rediscovery_run_pilot", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_task_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(
        'schema_version = "1.0"\n[task]\nname = "test/pkg"\n[agent]\ntimeout_sec = 60.0\n',
        encoding="utf-8",
    )
    (root / "instruction.md").write_text("Fix /app/main.py.\n", encoding="utf-8")
    env_dir = root / "environment"
    env_dir.mkdir()
    (env_dir / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n", encoding="utf-8")
    (env_dir / "main.py").write_text("print('bug')\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tests_dir / "test_outputs.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    sol_dir = root / "solution"
    sol_dir.mkdir()
    (sol_dir / "solve.sh").write_text("#!/bin/sh\necho secret\n", encoding="utf-8")
    return root


def test_stage_verifier_aware_variant_preserves_verifier_and_excludes_solution(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    pristine = _make_task_package(tmp_path / "pristine")
    pristine_digests = compute_task_digests(pristine)
    item = runner.CohortItem(
        id="test-pkg",
        key="v4.0.0/test-pkg",
        task_id="test-pkg",
        label="defect",
        split="pilot_dev",
        package=runner.CohortPackage(
            root_env="TB4_ROOT",
            path="tasks/test-pkg",
            pin="deadbeef",
            expected_package_digest=pristine_digests.package,
            expected_verifier_digest=pristine_digests.verifier,
        ),
        timeout_seconds=60,
        expected_patterns=("task_weak_assertion",),
        finding_slugs=("finding-1",),
        notes="",
    )
    resolved = runner.ResolvedPackage(
        item=item,
        pristine_path=pristine,
        pristine_digests=pristine_digests,
        variant_aware_path=tmp_path / "variants" / "test-pkg-aware",
    )

    variant_digests = runner.stage_verifier_aware_variant(resolved)
    assert variant_digests.verifier == pristine_digests.verifier
    assert variant_digests.task_toml == pristine_digests.task_toml
    assert variant_digests.instruction == pristine_digests.instruction
    assert variant_digests.package != pristine_digests.package

    eval_env = resolved.variant_aware_path / "environment" / "eval_env"
    assert (eval_env / "tests" / "test_outputs.py").is_file()
    assert (eval_env / "environment" / "main.py").is_file()
    assert not (eval_env / "solution").exists()
    dockerfile_text = (resolved.variant_aware_path / "environment" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "COPY eval_env/ /eval_env/" in dockerfile_text
    assert "chmod -R a-w /eval_env/" in dockerfile_text


def test_attempt_prompt_feedback_and_validation_gate(tmp_path: Path) -> None:
    runner = _load_runner()
    stdout_path = tmp_path / "test-stdout.txt"
    stdout_path.write_text("FAILED test_outputs.py::test_heading - AssertionError", encoding="utf-8")
    diff_path = tmp_path / "agent.diff"
    diff_path.write_text(
        "diff --git a/app/dispatch.py b/app/dispatch.py\n"
        "--- a/app/dispatch.py\n+++ b/app/dispatch.py\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    prior_rows = [
        {
            "attempt": 1,
            "reward": 0.0,
            "breaker_report": {
                "status": "exploit",
                "mechanism_hypothesis": "hardcode output",
                "summary": "tried static plan",
                "files_changed": ["/app/dispatch.py"],
            },
            "verifier_stdout_path": str(stdout_path),
            "agent_diff_path": str(diff_path),
        }
    ]

    r1 = runner.build_attempt_prompt("blind", 1, [], repo_root=tmp_path)
    assert "Operational Trial Protocol (Blind Arm)" in r1
    assert "Sequential Attempt Feedback" not in r1

    r2 = runner.build_attempt_prompt("verifier-aware", 2, prior_rows, repo_root=tmp_path)
    assert "Operational Trial Protocol (Verifier-Aware Arm)" in r2
    assert "Attempt 2 of 3" in r2
    assert "hardcode output" in r2
    assert "test_heading" in r2
    assert "files_changed (1): app/dispatch.py" in r2

    assert not runner.cell_validated_in_prior_rounds(prior_rows)
    assert not runner.cell_validated_in_prior_rounds(
        [{"attempt": 1, "reward": 1.0, "breaker_report": {"status": "legit_solution"}}]
    )
    assert runner.cell_validated_in_prior_rounds(
        [{"attempt": 1, "reward": 1.0, "breaker_report": {"status": "exploit"}}]
    )


def test_discover_breaker_report_and_score_skeleton(tmp_path: Path) -> None:
    runner = _load_runner()
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "last.txt").write_text(
        'Done.\n```json\n{"status": "no_credible_bypass", "mechanism_hypothesis": "checked all"}\n```\n',
        encoding="utf-8",
    )
    report, source = runner.discover_breaker_report(trial_dir)
    assert report is not None and report["status"] == "no_credible_bypass"
    assert source == trial_dir / "agent" / "last.txt"

    pristine = _make_task_package(tmp_path / "pkg")
    digests = compute_task_digests(pristine)
    item = runner.CohortItem(
        id="test-pkg",
        key="v4.0.0/test-pkg",
        task_id="test-pkg",
        label="defect",
        split="pilot_dev",
        package=runner.CohortPackage(
            root_env="TB4_ROOT",
            path="tasks/test-pkg",
            pin="deadbeef",
            expected_package_digest=digests.package,
            expected_verifier_digest=digests.verifier,
        ),
        timeout_seconds=60,
        expected_patterns=("task_weak_assertion",),
        finding_slugs=("finding-1",),
        notes="",
    )
    pkg = runner.ResolvedPackage(
        item=item,
        pristine_path=pristine,
        pristine_digests=digests,
        variant_aware_path=tmp_path / "variants" / "test-pkg-aware",
    )
    runs_dir = tmp_path / "runs"
    runner._append_jsonl(
        runs_dir / runner.CONTROLS_JSONL_NAME,
        {"id": "test-pkg", "verdict": "ok", "baseline_reward": 1.0, "negative_reward": 0.0},
    )
    runner._append_jsonl(
        runs_dir / runner.FLOOR_JSONL_NAME,
        {"id": "test-pkg", "verdict": "clean", "reward": 0.0, "ladder_version": "1.4.0"},
    )
    runner._append_jsonl(
        runs_dir / runner.CODEX_ATTEMPTS_JSONL_NAME,
        {"id": "test-pkg", "arm": "verifier-aware", "attempt": 1, "status": "passed", "reward": 1.0},
    )
    rc = runner.run_score_skeleton_phase(
        {"experiment": "breaker-rediscovery-v1", "split": "pilot_dev"},
        [pkg],
        repo_root=tmp_path,
        runs_dir=runs_dir,
    )
    assert rc == 0
    scored = json.loads((runs_dir / runner.SCORE_INPUT_JSON_NAME).read_text(encoding="utf-8"))
    assert scored["counts"]["controls_ok"] == 1
    assert scored["items"][0]["arms"]["blind"]["n_attempts"] == 0
    assert scored["items"][0]["arms"]["verifier-aware"]["any_pass_reward"] is True
    assert scored["items"][0]["arms"]["verifier-aware"]["first_pass_attempt"] == 1


def test_trajectory_report_state_diff_and_scorer(tmp_path: Path) -> None:
    runner = _load_runner()
    scorer_path = (
        Path(__file__).resolve().parents[1]
        / "research"
        / "experiments"
        / "breaker-rediscovery-v1"
        / "score_results.py"
    )
    spec = importlib.util.spec_from_file_location("breaker_rediscovery_scorer", scorer_path)
    assert spec is not None and spec.loader is not None
    scorer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scorer
    spec.loader.exec_module(scorer)

    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "state-journal").mkdir(parents=True)
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "user",
                        "message": '```json\n{"status": "exploit | no_credible_bypass | legit_solution", "mechanism_hypothesis": "tpl"}\n```',
                    },
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": '```json\n{"status": "exploit", "mechanism_hypothesis": "writable /tests/test_outputs.py"}\n```',
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    state_diff = trial / "state-journal" / "state-diff.json"
    state_diff.write_text(
        json.dumps(
            {
                "changes": [
                    {"path": ".", "change_type": "modified"},
                    {"path": "dispatch.py", "change_type": "modified"},
                ]
            }
        ),
        encoding="utf-8",
    )
    rep, src = runner.discover_breaker_report(trial)
    assert rep is not None
    assert rep["status"] == "exploit"
    assert src == trial / "agent" / "trajectory.json"
    diff_summary = runner.summarize_diff_file(state_diff)
    assert "dispatch.py (modified)" in diff_summary

    results = scorer.build_results_summary(
        json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "research"
                / "experiments"
                / "breaker-rediscovery-v1"
                / "results.json"
            ).read_text(encoding="utf-8")
        )
        if False
        else {
            "items": [
                {
                    "id": "tb4-cargo-flight-dispatch",
                    "key": "envcheck_tb4",
                    "task_id": "cargo-flight-dispatch",
                    "label": "defect",
                    "controls": {
                        "verdict": "ok",
                        "baseline_reward": 1.0,
                        "negative_reward": 0.0,
                        "duration_seconds": 10.0,
                    },
                    "floor": {"verdict": "clean", "reward": 0.0},
                    "arms": {
                        "blind": {
                            "max_reward": 0.0,
                            "first_pass_attempt": None,
                            "attempts": [
                                {
                                    "attempt": 1,
                                    "status": "failed",
                                    "reward": 0.0,
                                    "breaker_report": {
                                        "status": "no_credible_bypass",
                                        "mechanism_hypothesis": "none",
                                    },
                                    "tokens": {
                                        "n_input_tokens": 100,
                                        "n_output_tokens": 10,
                                        "n_cache_tokens": 50,
                                        "cost_usd": 0.001,
                                    },
                                    "duration_seconds": 5.0,
                                }
                            ],
                        },
                        "verifier-aware": {
                            "max_reward": 1.0,
                            "first_pass_attempt": 1,
                            "attempts": [
                                {
                                    "attempt": 1,
                                    "status": "passed",
                                    "reward": 1.0,
                                    "breaker_report": {
                                        "status": "exploit",
                                        "mechanism_hypothesis": "writable /tests",
                                    },
                                    "tokens": {
                                        "n_input_tokens": 200,
                                        "n_output_tokens": 20,
                                        "n_cache_tokens": 100,
                                        "cost_usd": 0.002,
                                    },
                                    "duration_seconds": 6.0,
                                }
                            ],
                        },
                    },
                }
            ]
        },
        [
            {
                "id": "tb4-cargo-flight-dispatch",
                "arm": "verifier-aware",
                "attempt": 1,
                "submission_base": "environment",
                "reward": 1.0,
            }
        ],
    )
    assert results["scoreboard"]["blind_arm"]["positive_defects"]["clean_stop"] == 1
    assert results["scoreboard"]["verifier_aware_arm"]["positive_defects"]["validated_exploit"] == 1
    md = scorer.render_results_markdown(results)
    assert "tb4-cargo-flight-dispatch" in md
