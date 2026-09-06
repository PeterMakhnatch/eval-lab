"""Tests for statistical lesson aggregation views and engine (WS-D)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from evallab.cohort import NOT_COMPARABLE, wilson_interval
from evallab.contextpack import parse_front_matter
from evallab.interpretation.trajectory_quality import (
    QualityStatus,
    TrajectoryQualityReport,
    persist_quality_ledger,
)
from evallab.lessons import (
    DEFAULT_POWER_THRESHOLD,
    GENERATED_HEADER,
    LessonRanking,
    LessonRow,
    LessonsResult,
    _canonical_quality_rows,
    apply_statistical_gating,
    build_lessons,
    check_lessons_freshness,
    collect_lessons_inputs,
    compare_lesson_rows,
    execute_lessons_views,
    generate_lessons_file,
    load_analysis_sidecars,
    load_craft_records,
    load_observation_records,
    load_quality_ledger_bound,
    load_trial_facts,
    parse_observation_markdown,
    populate_duckdb,
    rank_lesson_rows,
    render_lessons_markdown,
)
from evallab.lineage import compute_file_digest, resolve_lineage

PYTEST_DIGEST = "sha256:pytest111111111111111111111111111111111111111111111111111111111111"
GOLDEN_DIGEST = "sha256:golden222222222222222222222222222222222222222222222222222222222222"


def _make_mock_craft_records() -> list[dict]:
    return [
        {
            "task_ref": "local-lab/task-pytest",
            "source_repo": "local-lab/library",
            "version": "1.0",
            "task_digest": PYTEST_DIGEST,
            "instruction_chars": 120,
            "instruction_style": "imperative",
            "env_n_files": 2,
            "env_languages": ["python"],
            "env_services_n": 1,
            "env_multi_container": False,
            "verifier_type": "pytest",
            "anti_cheat": ["hidden_tests"],
            "answer_hiding": "none",
            "difficulty_mechanism": "clerical",
            "human_minutes": 30,
            "pinned_deps": True,
            "facets_schema_version": "1.0",
            "verifier_signals": ["pytest"],
            "unresolved_facets": [],
            "base_image_pin": "digest",
        },
        {
            "task_ref": "local-lab/task-golden",
            "source_repo": "local-lab/library",
            "version": "1.0",
            "task_digest": GOLDEN_DIGEST,
            "instruction_chars": 200,
            "instruction_style": "declarative",
            "env_n_files": 5,
            "env_languages": ["python"],
            "env_services_n": 2,
            "env_multi_container": True,
            "verifier_type": "golden_file",
            "anti_cheat": [],
            "answer_hiding": "none",
            "difficulty_mechanism": "volume",
            "human_minutes": 60,
            "pinned_deps": False,
            "facets_schema_version": "1.0",
            "verifier_signals": ["golden_file"],
            "unresolved_facets": [],
            "base_image_pin": "tag",
        },
    ]


def _make_mock_trial_facts() -> list[dict]:
    facts = []
    # 4 passing trials for task-pytest
    for i in range(4):
        facts.append(
            {
                "experiment_id": "exp-1",
                "job_id": "job-1",
                "trial_id": f"t-pytest-pass-{i}",
                "job_name": "job-1",
                "trial_name": f"trial-pytest-pass-{i}",
                "task_name": "local-lab/task-pytest",
                "task_digest": PYTEST_DIGEST,
                "verifier_digest": "v-1",
                "environment_digest": "e-1",
                "agent_config_digest": "a-1",
                "agent_name": "oracle",
                "agent_version": "1.0",
                "model_name": "none",
                "primary_reward": 1.0,
                "exception_class": None,
                "exception_phase": None,
                "duration_seconds": 10.0,
                "environment_setup_seconds": 1.0,
                "agent_setup_seconds": 1.0,
                "agent_execution_seconds": 5.0,
                "verifier_seconds": 3.0,
                "input_tokens": 100,
                "cache_tokens": 0,
                "output_tokens": 50,
                "cost_usd": 0.01,
                "trajectory_count": 1,
                "invalid_trajectory_count": 0,
                "step_count": 3,
                "llm_call_count": 2,
                "tool_call_count": 3,
                "command_failure_count": 0,
                "repeated_failed_command_count": 0,
                "artifact_count": 1,
                "missing_artifact_count": 0,
                "artifact_set_digest": "art-1",
            }
        )
    # 1 failing trial for task-pytest
    facts.append(
        {
            "experiment_id": "exp-1",
            "job_id": "job-1",
            "trial_id": "t-pytest-fail-0",
            "job_name": "job-1",
            "trial_name": "trial-pytest-fail-0",
            "task_name": "local-lab/task-pytest",
            "task_digest": PYTEST_DIGEST,
            "verifier_digest": "v-1",
            "environment_digest": "e-1",
            "agent_config_digest": "a-1",
            "agent_name": "codex",
            "agent_version": "1.0",
            "model_name": "none",
            "primary_reward": 0.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 15.0,
            "environment_setup_seconds": 1.0,
            "agent_setup_seconds": 1.0,
            "agent_execution_seconds": 10.0,
            "verifier_seconds": 3.0,
            "input_tokens": 200,
            "cache_tokens": 0,
            "output_tokens": 100,
            "cost_usd": 0.02,
            "trajectory_count": 1,
            "invalid_trajectory_count": 0,
            "step_count": 5,
            "llm_call_count": 4,
            "tool_call_count": 5,
            "command_failure_count": 2,
            "repeated_failed_command_count": 1,
            "artifact_count": 1,
            "missing_artifact_count": 0,
            "artifact_set_digest": "art-2",
        }
    )
    # 1 exception trial for task-pytest
    facts.append(
        {
            "experiment_id": "exp-1",
            "job_id": "job-1",
            "trial_id": "t-pytest-exc-0",
            "job_name": "job-1",
            "trial_name": "trial-pytest-exc-0",
            "task_name": "local-lab/task-pytest",
            "task_digest": PYTEST_DIGEST,
            "verifier_digest": "v-1",
            "environment_digest": "e-1",
            "agent_config_digest": "a-1",
            "agent_name": "codex",
            "agent_version": "1.0",
            "model_name": "none",
            "primary_reward": None,
            "exception_class": "TimeoutError",
            "exception_phase": "execution",
            "duration_seconds": 60.0,
            "environment_setup_seconds": 1.0,
            "agent_setup_seconds": 1.0,
            "agent_execution_seconds": 58.0,
            "verifier_seconds": 0.0,
            "input_tokens": 50,
            "cache_tokens": 0,
            "output_tokens": 10,
            "cost_usd": 0.005,
            "trajectory_count": 0,
            "invalid_trajectory_count": 0,
            "step_count": 0,
            "llm_call_count": 0,
            "tool_call_count": 0,
            "command_failure_count": 0,
            "repeated_failed_command_count": 0,
            "artifact_count": 0,
            "missing_artifact_count": 0,
            "artifact_set_digest": "",
        }
    )

    # 2 passing trials for task-golden
    for i in range(2):
        facts.append(
            {
                "experiment_id": "exp-2",
                "job_id": "job-2",
                "trial_id": f"t-golden-pass-{i}",
                "job_name": "job-2",
                "trial_name": f"trial-golden-pass-{i}",
                "task_name": "local-lab/task-golden",
                "task_digest": GOLDEN_DIGEST,
                "verifier_digest": "v-2",
                "environment_digest": "e-2",
                "agent_config_digest": "a-2",
                "agent_name": "oracle",
                "agent_version": "1.0",
                "model_name": "none",
                "primary_reward": 1.0,
                "exception_class": None,
                "exception_phase": None,
                "duration_seconds": 20.0,
                "environment_setup_seconds": 2.0,
                "agent_setup_seconds": 1.0,
                "agent_execution_seconds": 12.0,
                "verifier_seconds": 5.0,
                "input_tokens": 150,
                "cache_tokens": 0,
                "output_tokens": 80,
                "cost_usd": 0.02,
                "trajectory_count": 1,
                "invalid_trajectory_count": 0,
                "step_count": 4,
                "llm_call_count": 3,
                "tool_call_count": 4,
                "command_failure_count": 0,
                "repeated_failed_command_count": 0,
                "artifact_count": 1,
                "missing_artifact_count": 0,
                "artifact_set_digest": "art-3",
            }
        )
    # 1 failing trial for task-golden
    facts.append(
        {
            "experiment_id": "exp-2",
            "job_id": "job-2",
            "trial_id": "t-golden-fail-0",
            "job_name": "job-2",
            "trial_name": "trial-golden-fail-0",
            "task_name": "local-lab/task-golden",
            "task_digest": GOLDEN_DIGEST,
            "verifier_digest": "v-2",
            "environment_digest": "e-2",
            "agent_config_digest": "a-2",
            "agent_name": "codex",
            "agent_version": "1.0",
            "model_name": "none",
            "primary_reward": 0.0,
            "exception_class": None,
            "exception_phase": None,
            "duration_seconds": 25.0,
            "environment_setup_seconds": 2.0,
            "agent_setup_seconds": 1.0,
            "agent_execution_seconds": 18.0,
            "verifier_seconds": 4.0,
            "input_tokens": 300,
            "cache_tokens": 0,
            "output_tokens": 120,
            "cost_usd": 0.03,
            "trajectory_count": 1,
            "invalid_trajectory_count": 0,
            "step_count": 6,
            "llm_call_count": 5,
            "tool_call_count": 6,
            "command_failure_count": 1,
            "repeated_failed_command_count": 0,
            "artifact_count": 1,
            "missing_artifact_count": 0,
            "artifact_set_digest": "art-4",
        }
    )
    return facts


def _make_mock_analysis_sidecars() -> list[dict]:
    return [
        {
            "analysis_id": "a-001",
            "job_id": "job-1",
            "source_trial_id": "t-pytest-fail-0",
            "validity": "valid_agent_attempt",
            "primary_category": "tool_use",
            "summary": "repeated command loop in build step",
            "earliest_failure_step_id": 2,
            "confidence": "high",
            "validation_status": "valid",
            "source_path": "derived/analysis/a/analysis.json",
            "source_digest": (
                "sha256:"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        }
    ]


def _make_mock_observation_records() -> list[dict]:
    return [
        {
            "trial_id": "t-pytest-fail-0",
            "trial_name": "trial-pytest-fail-0",
            "job": "job-1",
            "agent": "codex",
            "model": "none",
            "task": "local-lab/task-pytest",
            "reward": 0.0,
            "steps_taken": 5,
            "first_failure_step": 2,
            "loop_detected": True,
            "loop_step": 3,
            "verified_before_done": False,
            "tool_errors": 2,
            "summary": "Tool loop detected in step 3",
        }
    ]


def test_duckdb_views_against_fixtures(tmp_path: Path) -> None:
    """Test SQL view execution and join correctness on structured fixtures."""
    craft_records = _make_mock_craft_records()
    trial_facts = _make_mock_trial_facts()
    analysis_sidecars = _make_mock_analysis_sidecars()
    observation_records = _make_mock_observation_records()

    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft_records,
            trial_facts=trial_facts,
            analysis_sidecars=analysis_sidecars,
            observation_records=observation_records,
        )
        views_data = execute_lessons_views(con)

    # 1. v_outcome_by_verifier_type checks
    verifier_rows = views_data["v_outcome_by_verifier_type"]
    assert len(verifier_rows) == 2
    pytest_row = next(r for r in verifier_rows if r["verifier_type"] == "pytest")
    assert pytest_row["total_trials_n"] == 6
    assert pytest_row["n"] == 5
    assert pytest_row["passed_n"] == 4
    assert pytest_row["exceptions_n"] == 1
    assert pytest_row["never_measured_n"] == 0
    assert pytest_row["excluded_n"] == 1
    assert pytest_row["failed_unexcepted_n"] == 1
    assert pytest_row["pass_rate_pct"] == pytest.approx(80.0)

    golden_row = next(r for r in verifier_rows if r["verifier_type"] == "golden_file")
    assert golden_row["n"] == 3
    assert golden_row["passed_n"] == 2
    assert golden_row["exceptions_n"] == 0
    assert golden_row["pass_rate_pct"] == pytest.approx(66.67, abs=0.01)

    # 2. v_loop_rate_by_env checks
    loop_rows = views_data["v_loop_rate_by_env"]
    assert len(loop_rows) == 2
    single_cont_row = next(r for r in loop_rows if not r["env_multi_container"])
    assert single_cont_row["total_trials_n"] == 6
    assert single_cont_row["annotated_n"] == 1
    assert single_cont_row["unannotated_n"] == 5
    assert single_cont_row["n"] == 1
    assert single_cont_row["loops_n"] == 1
    assert single_cont_row["loop_rate_pct"] == pytest.approx(100.0)
    multi_cont_row = next(r for r in loop_rows if r["env_multi_container"])
    assert multi_cont_row["total_trials_n"] == 3
    assert multi_cont_row["annotated_n"] == 0
    assert multi_cont_row["unannotated_n"] == 3
    assert multi_cont_row["n"] == 0

    # 3. v_failure_by_facet checks
    failure_rows = views_data["v_failure_by_facet"]
    assert len(failure_rows) > 0
    tool_use_row = next(
        (
            r
            for r in failure_rows
            if r["facet_name"] == "verifier_type"
            and r["facet_value"] == "pytest"
            and r["model_failure_category"] == "tool_use"
        ),
        None,
    )
    assert tool_use_row is not None
    assert tool_use_row["model_diagnosis_source"] == "validated_analysis_sidecar"
    assert tool_use_row["mechanical_failure_category"] == "unscored_failure"
    assert tool_use_row["mechanical_diagnosis_source"] == "trial_facts"
    assert tool_use_row["model_analysis_ids"] == ["a-001"]
    assert "failure_category" not in tool_use_row
    assert "validity" not in tool_use_row
    assert tool_use_row["failures_n"] == 1
    pytest_facet_rows = [
        row
        for row in failure_rows
        if row["facet_name"] == "verifier_type" and row["facet_value"] == "pytest"
    ]
    assert sum(row["total_trials_n"] for row in pytest_facet_rows) == 6
    assert sum(row["n"] for row in pytest_facet_rows) == 5
    assert sum(row["exceptions_n"] for row in pytest_facet_rows) == 1
    assert sum(row["never_measured_n"] for row in pytest_facet_rows) == 0
    assert sum(row["excluded_n"] for row in pytest_facet_rows) == 1
    exception_row = next(
        row
        for row in pytest_facet_rows
        if row["mechanical_failure_category"] == "exception"
    )
    gated_exception = apply_statistical_gating(
        {"v_failure_by_facet": [exception_row]}
    )["v_failure_by_facet"][0]
    assert gated_exception.n == 0
    assert gated_exception.wilson_95 is None
    assert not gated_exception.powered


def test_failure_diagnoses_keep_sources_distinct_and_deduplicate_valid_sidecars() -> None:
    sidecars = _make_mock_analysis_sidecars()
    sidecars.extend(
        [
            {
                **sidecars[0],
                "analysis_id": "a-002",
                "primary_category": "duplicate_diagnosis",
                "source_path": "derived/analysis/z/analysis.json",
                "source_digest": (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
            },
            {
                **sidecars[0],
                "analysis_id": "a-000-invalid",
                "primary_category": "invalid_diagnosis",
                "validation_status": "invalid",
                "source_path": "derived/analysis/0/analysis.json",
            },
        ]
    )

    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=_make_mock_trial_facts(),
            analysis_sidecars=sidecars,
            observation_records=_make_mock_observation_records(),
        )
        rows = execute_lessons_views(con)["v_failure_by_facet"]

    pytest_verifier_rows = [
        row
        for row in rows
        if row["facet_name"] == "verifier_type" and row["facet_value"] == "pytest"
    ]
    chosen = next(
        row
        for row in pytest_verifier_rows
        if row["model_failure_category"] == "tool_use"
    )
    assert chosen["model_analysis_ids"] == ["a-001"]
    assert chosen["model_sidecar_paths"] == ["derived/analysis/a/analysis.json"]
    assert chosen["mechanical_failure_category"] == "unscored_failure"
    assert sum(row["total_trials_n"] for row in pytest_verifier_rows) == 6
    assert not any(
        row["model_failure_category"] in {"duplicate_diagnosis", "invalid_diagnosis"}
        for row in pytest_verifier_rows
    )



def test_verifier_outcomes_group_on_the_projected_unclassified_key() -> None:
    craft = _make_mock_craft_records()
    craft[0] = {**craft[0], "verifier_type": None}
    craft[1] = {**craft[1], "verifier_type": "unclassified"}

    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft,
            trial_facts=_make_mock_trial_facts(),
            analysis_sidecars=[],
            observation_records=[],
        )
        rows = execute_lessons_views(con)["v_outcome_by_verifier_type"]

    assert len(rows) == 1
    assert rows[0]["verifier_type"] == "unclassified"
    assert rows[0]["total_trials_n"] == 9


def test_capability_denominator_reports_exception_and_never_measured_exclusions() -> None:
    facts = _make_mock_trial_facts()
    never_measured = {
        **next(row for row in facts if row["exception_class"] is not None),
        "trial_id": "t-pytest-never-measured",
        "exception_class": None,
        "exception_phase": None,
    }
    facts.append(never_measured)

    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=facts,
            analysis_sidecars=[],
            observation_records=[],
        )
        rows = execute_lessons_views(con)["v_outcome_by_verifier_type"]

    pytest_row = next(row for row in rows if row["verifier_type"] == "pytest")
    assert pytest_row["total_trials_n"] == 7
    assert pytest_row["n"] == 5
    assert pytest_row["passed_n"] == 4
    assert pytest_row["exceptions_n"] == 1
    assert pytest_row["never_measured_n"] == 1
    assert pytest_row["excluded_n"] == 2
    assert pytest_row["pass_rate_pct"] == pytest.approx(80.0)


def test_views_join_tasks_only_by_exact_digest() -> None:
    mismatched = {
        **_make_mock_trial_facts()[0],
        "trial_id": "matching-name-wrong-digest",
        "task_digest": (
            "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        ),
    }
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=[mismatched],
            analysis_sidecars=[],
            observation_records=[],
        )
        views = execute_lessons_views(con)

    assert views["v_failure_by_facet"] == []
    assert views["v_loop_rate_by_env"] == []
    assert views["v_outcome_by_verifier_type"] == []


def test_markdown_observations_never_become_deterministic_trial_facts(tmp_path: Path) -> None:
    observation = _make_mock_observation_records()[0]
    assert observation["steps_taken"] > 0
    assert observation["tool_errors"] > 0
    assert load_trial_facts(tmp_path) == []


def test_production_lessons_discovery_excludes_test_fixtures(tmp_path: Path) -> None:
    payload = {
        "analysis_id": "analysis-production",
        "job_id": "job-1",
        "source_trial_id": "trial-1",
        "validation_status": "valid",
        "output": {
            "validity": "valid_agent_attempt",
            "primary_category": "tool_use",
            "summary": "annotation",
            "confidence": "high",
        },
    }
    production = tmp_path / "derived/analysis/production/analysis.json"
    fixture = tmp_path / "tests/fixtures/explorer/analyses/fixture/analysis.json"
    production.parent.mkdir(parents=True)
    fixture.parent.mkdir(parents=True)
    production.write_text(json.dumps(payload), encoding="utf-8")
    fixture.write_text(
        json.dumps({**payload, "analysis_id": "analysis-fixture"}),
        encoding="utf-8",
    )

    sidecars = load_analysis_sidecars(tmp_path)
    inputs = collect_lessons_inputs(tmp_path)

    assert [row["analysis_id"] for row in sidecars] == ["analysis-production"]
    assert sidecars[0]["source_path"] == "derived/analysis/production/analysis.json"
    assert sidecars[0]["source_digest"] == compute_file_digest(production)
    input_paths = {row["path"] for row in inputs}
    assert "derived/analysis/production/analysis.json" in input_paths
    assert not any(path.startswith("tests/fixtures/") for path in input_paths)


def test_statistical_gating_power_threshold() -> None:
    """Test that small n (< threshold) is labeled 'insufficient n' and not generalized."""
    raw_views = {
        "v_outcome_by_verifier_type": [
            {
                "source_repo": "test-repo",
                "verifier_type": "pytest",
                "n": 10,
                "passed_n": 8,
                "exceptions_n": 0,
                "pass_rate_pct": 80.0,
            },
            {
                "source_repo": "test-repo",
                "verifier_type": "custom",
                "n": 3,
                "passed_n": 1,
                "exceptions_n": 1,
                "pass_rate_pct": 33.3,
            },
        ],
        "v_loop_rate_by_env": [
            {
                "source_repo": "test-repo",
                "env_services_n": 1,
                "env_multi_container": False,
                "env_files_bucket": "1_to_5_files",
                "n": 2,
                "loops_n": 1,
                "loop_rate_pct": 50.0,
            }
        ],
        "v_failure_by_facet": [
            {
                "source_repo": "test-repo",
                "facet_name": "verifier_type",
                "facet_value": "pytest",
                "model_failure_category": "tool_use",
                "model_validity": "valid_agent_attempt",
                "mechanical_failure_category": "unscored_failure",
                "mechanical_validity": "measured_agent_attempt",
                "n": 6,
                "failures_n": 2,
                "failure_rate_pct": 33.3,
            },
            {
                "source_repo": "test-repo",
                "facet_name": "difficulty_mechanism",
                "facet_value": "volume",
                "model_failure_category": None,
                "model_validity": None,
                "mechanical_failure_category": "exception",
                "mechanical_validity": "exception_trial",
                "n": 1,
                "failures_n": 1,
                "failure_rate_pct": 100.0,
            },
        ],
    }

    gated = apply_statistical_gating(raw_views, power_threshold=5)

    # Check v_outcome_by_verifier_type
    verifier_lessons = gated["v_outcome_by_verifier_type"]
    assert len(verifier_lessons) == 2
    powered_v = verifier_lessons[0]
    assert powered_v.powered is True
    assert powered_v.status == "sufficient"
    assert "pass_rate=80.0%" in powered_v.finding
    assert powered_v.wilson_95 is not None

    underpowered_v = verifier_lessons[1]
    assert underpowered_v.powered is False
    assert underpowered_v.status == "insufficient n"
    assert underpowered_v.finding == "insufficient n"
    assert underpowered_v.wilson_95 is not None

    # Check v_loop_rate_by_env
    loop_lessons = gated["v_loop_rate_by_env"]
    assert len(loop_lessons) == 1
    assert loop_lessons[0].powered is False
    assert loop_lessons[0].status == "insufficient n"
    assert loop_lessons[0].finding == "insufficient n"

    # Check v_failure_by_facet
    failure_lessons = gated["v_failure_by_facet"]
    assert len(failure_lessons) == 2
    assert failure_lessons[0].powered is True
    assert failure_lessons[0].status == "sufficient"
    assert "failure_rate=33.3%" in failure_lessons[0].finding

    assert failure_lessons[1].powered is False
    assert failure_lessons[1].status == "insufficient n"
    assert failure_lessons[1].finding == "insufficient n"


def test_wilson_interval_edges() -> None:
    """Test Wilson 95% confidence interval boundary cases."""
    assert wilson_interval(0, 0) is None
    ci_zero = wilson_interval(0, 10)
    assert ci_zero is not None
    assert ci_zero[0] == pytest.approx(0.0, abs=1e-4)
    assert ci_zero[1] > 0.0

    ci_full = wilson_interval(10, 10)
    assert ci_full is not None
    assert ci_full[0] < 1.0
    assert ci_full[1] == pytest.approx(1.0, abs=1e-4)


def test_parse_observation_markdown(tmp_path: Path) -> None:
    """Test parsing observatory markdown records."""
    obs_file = tmp_path / "test_obs.md"
    obs_file.write_text(
        """# Observation record

- **template_version:** observatory-1
- **trial_id:** 11111111-2222-3333-4444-555555555555
- **trial_name:** sample-trial
- **job:** sample-job
- **agent:** codex
- **model:** test-model
- **task:** local-lab/test-task
- **reward:** 1.0
- **steps_taken:** 4
- **first_failure_step:** none
- **loop_detected:** yes
- **loop_step:** 2
- **verified_before_done:** yes
- **tool_errors:** 1
- **summary:** Test run summary
- **evidence_files:** result.json
"""
    )

    parsed = parse_observation_markdown(obs_file)
    assert parsed is not None
    assert parsed["trial_id"] == "11111111-2222-3333-4444-555555555555"
    assert parsed["reward"] == 1.0
    assert parsed["steps_taken"] == 4
    assert parsed["loop_detected"] is True
    assert parsed["loop_step"] == 2
    assert parsed["verified_before_done"] is True
    assert parsed["tool_errors"] == 1


def test_markdown_rendering_and_file_generation(tmp_path: Path) -> None:
    """Test lessons markdown generation and file writing."""
    craft_records = _make_mock_craft_records()
    trial_facts = _make_mock_trial_facts()
    analysis_sidecars = _make_mock_analysis_sidecars()
    observation_records = _make_mock_observation_records()

    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft_records,
            trial_facts=trial_facts,
            analysis_sidecars=analysis_sidecars,
            observation_records=observation_records,
        )
        raw_views = execute_lessons_views(con)

    gated = apply_statistical_gating(raw_views, power_threshold=DEFAULT_POWER_THRESHOLD)
    all_lessons = [item for sublist in gated.values() for item in sublist]

    result = LessonsResult(
        generated_at=datetime.now(UTC),
        power_threshold=DEFAULT_POWER_THRESHOLD,
        total_lessons=len(all_lessons),
        powered_lessons=sum(1 for x in all_lessons if x.powered),
        underpowered_lessons=sum(1 for x in all_lessons if not x.powered),
        lessons_by_view=gated,
        records_summary={"craft_records": 2, "trial_facts": 9},
    )

    markdown = render_lessons_markdown(result)
    assert f"<!-- {GENERATED_HEADER} -->" in markdown
    assert "# Statistical Lessons & Aggregation Views" in markdown
    assert "v_outcome_by_verifier_type" in markdown
    assert "v_loop_rate_by_env" in markdown
    assert "v_failure_by_facet" in markdown
    assert "insufficient n" in markdown

    out_file = tmp_path / "research/lessons.md"
    out_file.parent.mkdir(parents=True)
    out_file.write_text(markdown)
    assert out_file.is_file()


def test_standalone_sql_script_with_fallbacks() -> None:
    """Test that sql/lessons.sql can run in a clean DuckDB session with zero pre-created tables."""
    sql = Path("sql/lessons.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)
        for view_name in [
            "v_failure_by_facet",
            "v_loop_rate_by_env",
            "v_outcome_by_verifier_type",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == []


def test_empty_views_render_insufficient_n_never_silent() -> None:
    """Test that empty view results produce gated 'insufficient n' rows, never silence."""
    gated = apply_statistical_gating({})
    for view_name in ["v_failure_by_facet", "v_loop_rate_by_env", "v_outcome_by_verifier_type"]:
        rows = gated[view_name]
        assert len(rows) >= 1
        assert rows[0].powered is False
        assert rows[0].status == "insufficient n"
        assert rows[0].finding == "insufficient n"

    all_lessons = [item for sublist in gated.values() for item in sublist]
    res = LessonsResult(
        generated_at=datetime.now(UTC),
        power_threshold=5,
        total_lessons=len(all_lessons),
        powered_lessons=0,
        underpowered_lessons=len(all_lessons),
        lessons_by_view=gated,
        records_summary={},
    )
    md = render_lessons_markdown(res)
    assert "insufficient n" in md
    assert md.count("insufficient n") >= 3


def test_build_lessons_on_repository_root(tmp_path: Path) -> None:
    """Test full build_lessons execution over repository evidence."""
    repo_root = Path(__file__).resolve().parents[1]
    result = build_lessons(repo_root)
    assert result.total_lessons > 0
    assert "v_outcome_by_verifier_type" in result.lessons_by_view
    assert "v_loop_rate_by_env" in result.lessons_by_view
    assert "v_failure_by_facet" in result.lessons_by_view

    # The writer is exercised against a scratch target, never the committed
    # `research/lessons.md`: a test that rewrites a tracked generated artifact
    # leaves every checkout dirty and invites an accidental `git add -A` from
    # committing test-derived findings over the real corpus.
    target = tmp_path / "research" / "lessons.md"
    lessons_path = generate_lessons_file(repo_root, output_path=target)
    assert lessons_path == target
    assert lessons_path.is_file()
    content = lessons_path.read_text(encoding="utf-8")
    assert GENERATED_HEADER in content
    assert "Statistical Lessons & Aggregation Views" in content


def _sample_lessons_fixture_tree(tmp_path: Path) -> Path:
    root = tmp_path
    sql_dir = root / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    real_sql = Path(__file__).resolve().parents[1] / "sql" / "lessons.sql"
    (sql_dir / "lessons.sql").write_text(real_sql.read_text(encoding="utf-8"), encoding="utf-8")

    task_dir = root / "library" / "tasks" / "sample-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        '[task]\nname = "sample-task"\nversion = "1.0.0"\n', encoding="utf-8"
    )

    obs_dir = root / "research" / "observations" / "sample-job"
    obs_dir.mkdir(parents=True, exist_ok=True)
    (obs_dir / "sample_obs.md").write_text(
        "---\njob: sample-job\ntrial_id: t1\ntask: sample-task\nreward: 1.0\n---\n# Observation\n",
        encoding="utf-8",
    )
    persist_quality_ledger(
        _make_quality_ledger_reports(), [], root / "derived" / "parquet"
    )
    return root


def test_lessons_front_matter_declares_valid_inputs_list(tmp_path: Path) -> None:
    root = _sample_lessons_fixture_tree(tmp_path)
    result = build_lessons(root)
    markdown = render_lessons_markdown(result)
    fm, _body = parse_front_matter(markdown)
    assert fm is not None
    assert "inputs" in fm
    assert isinstance(fm["inputs"], list)
    assert len(fm["inputs"]) > 0
    for item in fm["inputs"]:
        assert isinstance(item, dict)
        assert "path" in item and isinstance(item["path"], str)
        assert "digest" in item and isinstance(item["digest"], str)
        assert item["digest"].startswith("sha256:")
        assert len(item["digest"]) == 71


def test_lessons_generation_convergence_two_consecutive_runs(tmp_path: Path) -> None:
    root = _sample_lessons_fixture_tree(tmp_path)
    target = root / "research" / "lessons.md"
    fixed_time = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    first_path = generate_lessons_file(root, output_path=target, generated_at=fixed_time)
    first_content = first_path.read_text(encoding="utf-8")
    second_path = generate_lessons_file(root, output_path=target, generated_at=fixed_time)
    second_content = second_path.read_text(encoding="utf-8")
    assert first_content == second_content


def test_committed_lessons_are_fresh_and_lineage_bound() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert check_lessons_freshness(repo_root)
    node = resolve_lineage("research/lessons.md", repo_root=repo_root)
    assert node.status == "resolved"


def test_lessons_recorded_digests_match_actual_file_digests(tmp_path: Path) -> None:
    root = _sample_lessons_fixture_tree(tmp_path)
    result = build_lessons(root)
    markdown = render_lessons_markdown(result)
    fm, _body = parse_front_matter(markdown)
    assert fm is not None and "inputs" in fm
    for item in fm["inputs"]:
        target_file = root / item["path"]
        assert target_file.is_file()
        expected = compute_file_digest(target_file)
        assert item["digest"] == expected


def test_lineage_resolution_on_generated_lessons(tmp_path: Path) -> None:
    root = _sample_lessons_fixture_tree(tmp_path)
    target = root / "research" / "lessons.md"
    generate_lessons_file(root, output_path=target)

    node = resolve_lineage("research/lessons.md", repo_root=root)
    assert node.status == "resolved"
    assert len(node.inputs) > 0
    assert any(child.path == "sql/lessons.sql" for child in node.inputs)
    assert node.status != "unrecorded"


def test_statistical_gating_every_row_carries_n_and_cohort_interval_or_marker() -> None:
    """Test that every emitted row carries n + cohort.py interval or the insufficient-n marker."""
    repo_root = Path(__file__).resolve().parents[1]
    result = build_lessons(repo_root)

    for view_name, rows in result.lessons_by_view.items():
        assert len(rows) > 0, f"view {view_name} must emit rows or fallback"
        for row in rows:
            assert isinstance(row.n, int)
            assert row.n >= 0
            if row.powered:
                assert row.wilson_95 is not None
                assert row.status == "sufficient"
                assert f"n={row.n}" in row.finding
                low, high = row.wilson_95
                assert 0.0 <= low <= high <= 1.0
            else:
                assert row.status == "insufficient n"
                assert row.finding == "insufficient n"


def test_refuse_to_rank_propagates_from_cohort() -> None:
    """Test that refuse-to-rank propagates NOT_COMPARABLE from cohort.py."""
    # 1. Underpowered row comparison refuses to rank
    row_underpowered = LessonRow(
        lesson_id="test_001",
        view_name="v_test",
        dimension="dim_a",
        metric_name="pass_rate",
        n=3,
        k=3,
        rate=1.0,
        wilson_95=wilson_interval(3, 3),
        powered=False,
        status="insufficient n",
        finding="insufficient n",
    )
    row_powered = LessonRow(
        lesson_id="test_002",
        view_name="v_test",
        dimension="dim_b",
        metric_name="pass_rate",
        n=10,
        k=8,
        rate=0.8,
        wilson_95=wilson_interval(8, 10),
        powered=True,
        status="sufficient",
        finding="pass_rate=80.0%",
    )

    ranking = compare_lesson_rows(row_underpowered, row_powered)
    assert isinstance(ranking, LessonRanking)
    assert ranking.rankable is False
    assert ranking.ranking is None
    assert ranking.statement.startswith(NOT_COMPARABLE)
    assert any("insufficient n" in r for r in ranking.refusal_reasons)

    # 2. Overlapping confidence intervals refuse to rank
    row_c = LessonRow(
        lesson_id="test_003",
        view_name="v_test",
        dimension="dim_c",
        metric_name="pass_rate",
        n=10,
        k=7,
        rate=0.7,
        wilson_95=wilson_interval(7, 10),
        powered=True,
        status="sufficient",
        finding="pass_rate=70.0%",
    )
    ranking_overlap = compare_lesson_rows(row_powered, row_c)
    assert ranking_overlap.rankable is False
    assert ranking_overlap.ranking is None
    assert ranking_overlap.statement.startswith(NOT_COMPARABLE)
    assert any("intervals overlap" in r for r in ranking_overlap.refusal_reasons)

    # 3. Uninformative all-zero column refuses to rank
    row_zero_1 = LessonRow(
        lesson_id="test_004",
        view_name="v_test",
        dimension="dim_z1",
        metric_name="loop_rate",
        n=10,
        k=0,
        rate=0.0,
        wilson_95=wilson_interval(0, 10),
        powered=True,
        status="sufficient",
        finding="loop_rate=0.0%",
    )
    row_zero_2 = LessonRow(
        lesson_id="test_005",
        view_name="v_test",
        dimension="dim_z2",
        metric_name="loop_rate",
        n=10,
        k=0,
        rate=0.0,
        wilson_95=wilson_interval(0, 10),
        powered=True,
        status="sufficient",
        finding="loop_rate=0.0%",
    )
    ranking_zero = compare_lesson_rows(row_zero_1, row_zero_2)
    assert ranking_zero.rankable is False
    assert ranking_zero.ranking is None
    assert ranking_zero.statement.startswith(NOT_COMPARABLE)
    assert any("uninformative metric" in r for r in ranking_zero.refusal_reasons)

    # 4. Disjoint confidence intervals produce ranked result
    row_high = LessonRow(
        lesson_id="test_006",
        view_name="v_test",
        dimension="golden_file",
        metric_name="pass_rate",
        n=20,
        k=20,
        rate=1.0,
        wilson_95=wilson_interval(20, 20),
        powered=True,
        status="sufficient",
        finding="pass_rate=100.0%",
    )
    row_low = LessonRow(
        lesson_id="test_007",
        view_name="v_test",
        dimension="hybrid",
        metric_name="pass_rate",
        n=20,
        k=0,
        rate=0.0,
        wilson_95=wilson_interval(0, 20),
        powered=True,
        status="sufficient",
        finding="pass_rate=0.0%",
    )
    ranking_distinct = compare_lesson_rows(row_high, row_low)
    assert ranking_distinct.rankable is True
    assert ranking_distinct.ranking == "golden_file > hybrid"
    assert ranking_distinct.statement.startswith("Ranking: golden_file > hybrid")

    # 5. rank_lesson_rows over collection
    all_rankings = rank_lesson_rows([row_high, row_low, row_zero_1])
    assert len(all_rankings) == 3


# --------------------------------------------------------------------------- #
# Evidence Quality Ledger join
# --------------------------------------------------------------------------- #

FIXED_EVALUATED_AT = "2026-08-17T12:00:00+00:00"


def _make_quality_ledger_reports() -> list[TrajectoryQualityReport]:
    """Ledger rows covering every quality state, an unevaluated trial, and an orphan."""
    specs: list[tuple[str, str, QualityStatus, str | None]] = [
        ("job-1", "t-pytest-pass-0", QualityStatus.PASS, None),
        ("job-1", "t-pytest-pass-1", QualityStatus.PASS, None),
        ("job-1", "t-pytest-pass-2", QualityStatus.PASS, None),
        ("job-1", "t-pytest-pass-3", QualityStatus.PASS, None),
        ("job-1", "t-pytest-fail-0", QualityStatus.WARN, None),
        ("job-1", "t-pytest-exc-0", QualityStatus.QUARANTINE, "missing_trajectory_file"),
        ("job-2", "t-golden-pass-0", QualityStatus.PASS, None),
        ("job-2", "t-golden-pass-1", QualityStatus.NOT_EVALUATED, None),
        ("job-2", "t-golden-fail-0", QualityStatus.FAIL, None),
        ("job-ghost", "t-ghost", QualityStatus.PASS, None),
    ]
    return [
        TrajectoryQualityReport(
            job_id=job_id,
            trial_id=trial_id,
            document_id=f"doc:{trial_id}",
            raw_atif_digest=None,
            raw_result_digest=None,
            check_version="v1.0.0",
            check_digest="sha256:fixture",
            status=status,
            is_ingestable=status is not QualityStatus.QUARANTINE,
            is_analysis_ready=status
            not in (QualityStatus.QUARANTINE, QualityStatus.NOT_EVALUATED),
            quarantine_reason=reason,
            findings_count=0,
            warnings_count=0,
            errors_count=0,
            evaluated_at=FIXED_EVALUATED_AT,
        )
        for job_id, trial_id, status, reason in specs
    ]


def _write_quality_ledger(root: Path) -> Path:
    reports_path, _findings_path = persist_quality_ledger(
        _make_quality_ledger_reports(), [], root / "derived" / "parquet"
    )
    return reports_path


def _strip_quality_columns(rows: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in row.items() if not key.startswith("quality_")}
        for row in rows
    ]


def _ledger_dicts() -> list[dict]:
    return [report.to_dict() for report in _make_quality_ledger_reports()]


def test_canonical_quality_rows_dedupes_and_refuses_conflicts() -> None:
    reports = [
        {"job_id": "job-1", "trial_id": "t1", "status": "pass"},
        {"job_id": "other", "trial_id": "t1", "status": "warn"},
        {"job_id": "job-1", "trial_id": "t2", "status": "pass"},
        {"job_id": "job-1", "trial_id": "t2", "status": "fail"},
        {"job_id": "", "trial_id": "t3", "status": "fail"},
        {"job_id": "", "trial_id": "t1", "status": "fail"},
    ]
    rows = _canonical_quality_rows(reports)
    keyed = {(r["job_id"], r["trial_id"]): r["status"] for r in rows}
    # exact pairs survive; the empty-identity shadow of an identified trial is dropped
    assert keyed[("job-1", "t1")] == "pass"
    assert ("job-1", "t2") not in keyed  # conflicting statuses refuse
    assert keyed[("", "t3")] == "fail"  # empty-identity sole binding kept
    assert ("", "t1") not in keyed
    shuffled = _canonical_quality_rows(list(reversed(reports)))
    assert rows == shuffled


def test_canonical_quality_rows_empty_list() -> None:
    assert _canonical_quality_rows([]) == []


def test_quality_views_decompose_from_raw_ledger_rows() -> None:
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=_make_mock_trial_facts(),
            analysis_sidecars=_make_mock_analysis_sidecars(),
            observation_records=_make_mock_observation_records(),
            quality_reports=_ledger_dicts(),
        )
        views = execute_lessons_views(con)

    verifier = {row["verifier_type"]: row for row in views["v_outcome_by_verifier_type"]}
    pytest_row = verifier["pytest"]
    assert (pytest_row["n"], pytest_row["passed_n"]) == (5, 4)
    assert pytest_row["total_trials_n"] == 5  # quarantined trial excluded in SQL
    quality = (
        pytest_row["quality_pass_n"],
        pytest_row["quality_warn_n"],
        pytest_row["quality_fail_n"],
        pytest_row["quality_quarantine_n"],
    )
    assert quality == (4, 1, 0, 1)
    golden_row = verifier["golden_file"]
    assert (
        golden_row["quality_pass_n"],
        golden_row["quality_warn_n"],
        golden_row["quality_fail_n"],
        golden_row["quality_quarantine_n"],
    ) == (1, 0, 1, 0)


def test_quarantined_trials_stay_out_of_eligibility_but_are_counted() -> None:
    craft = _make_mock_craft_records()
    sidecars = _make_mock_analysis_sidecars()
    observations = _make_mock_observation_records()
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft,
            trial_facts=_make_mock_trial_facts(),
            analysis_sidecars=sidecars,
            observation_records=observations,
            quality_reports=_ledger_dicts(),
        )
        with_ledger = execute_lessons_views(con)
    # Ledger quarantine must equal manual exclusion of the same trial.
    manually_excluded = [
        row for row in _make_mock_trial_facts() if row["trial_id"] != "t-pytest-exc-0"
    ]
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft,
            trial_facts=manually_excluded,
            analysis_sidecars=sidecars,
            observation_records=observations,
        )
        manual = execute_lessons_views(con)

    pytest_row = next(
        row
        for row in with_ledger["v_outcome_by_verifier_type"]
        if row["verifier_type"] == "pytest"
    )
    assert pytest_row["quality_quarantine_n"] == 1  # counted separately
    assert pytest_row["total_trials_n"] == 5  # 6 facts minus the quarantined one
    for view_name, rows in with_ledger.items():
        manual_rows = _strip_quality_columns(manual[view_name])
        # Math rows must match exactly; quarantine counting may surface extra
        # zero-math group rows (e.g. the quarantined exception trial's facet
        # bucket) that exist only to carry the quarantine count.
        math_rows = [
            r
            for r in _strip_quality_columns(rows)
            if r["total_trials_n"] > 0 or r["n"] > 0
        ]
        assert math_rows == manual_rows
        extras = [
            r
            for r in _strip_quality_columns(rows)
            if r["total_trials_n"] == 0 and r["n"] == 0
        ]
        assert all(
            r["mechanical_failure_category"] == "exception"
            for r in extras
            if "mechanical_failure_category" in r
        )

    gated_with = apply_statistical_gating(with_ledger)
    gated_manual = apply_statistical_gating(manual)
    for view_name, rows in gated_with.items():
        # Compare measurable rows; zero-n quarantine-count rows carry no math.
        with_math = [
            (row.n, row.k, row.rate, row.wilson_95, row.powered, row.status)
            for row in rows
            if row.n > 0
        ]
        manual_math = [
            (row.n, row.k, row.rate, row.wilson_95, row.powered, row.status)
            for row in gated_manual[view_name]
            if row.n > 0
        ]
        assert with_math == manual_math


def test_all_quarantine_ledger_empties_math_but_keeps_counts() -> None:
    facts = _make_mock_trial_facts()
    reports = [
        {"job_id": row["job_id"], "trial_id": row["trial_id"], "status": "quarantine"}
        for row in facts
    ]
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=facts,
            analysis_sidecars=_make_mock_analysis_sidecars(),
            observation_records=_make_mock_observation_records(),
            quality_reports=reports,
        )
        views = execute_lessons_views(con)
    for _view_name, rows in views.items():
        for row in rows:
            assert row["n"] == 0
            assert row["total_trials_n"] == 0
            assert row["quality_quarantine_n"] > 0
            assert row["quality_pass_n"] == 0


def test_cross_job_ledger_identity_never_binds_at_view_level() -> None:
    # t-golden-fail-0 exists only under job-9 in this ledger while the fact
    # row belongs to another job: the view must leave it ungated (quality
    # columns zero for it) rather than bind a foreign identity.
    facts = [row for row in _make_mock_trial_facts() if row["trial_id"] == "t-golden-fail-0"]
    reports = [{"job_id": "job-9", "trial_id": "t-golden-fail-0", "status": "fail"}]
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=facts,
            analysis_sidecars=_make_mock_analysis_sidecars(),
            observation_records=_make_mock_observation_records(),
            quality_reports=reports,
        )
        views = execute_lessons_views(con)
    golden_row = next(
        row
        for row in views["v_outcome_by_verifier_type"]
        if row["verifier_type"] == "golden_file"
    )
    assert golden_row["quality_fail_n"] == 0
    assert golden_row["n"] == 1  # math unchanged: unbound rows stay ungated


def test_ledger_row_order_does_not_change_views() -> None:
    reports = _ledger_dicts()
    def build(order):
        with duckdb.connect(":memory:") as con:
            populate_duckdb(
                con,
                craft_records=_make_mock_craft_records(),
                trial_facts=_make_mock_trial_facts(),
                analysis_sidecars=_make_mock_analysis_sidecars(),
                observation_records=_make_mock_observation_records(),
                quality_reports=order,
            )
            return execute_lessons_views(con)
    assert build(reports) == build(list(reversed(reports)))


def test_rendered_markdown_decomposes_ledger_counts() -> None:
    with duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=_make_mock_craft_records(),
            trial_facts=_make_mock_trial_facts(),
            analysis_sidecars=_make_mock_analysis_sidecars(),
            observation_records=_make_mock_observation_records(),
            quality_reports=_ledger_dicts(),
        )
        gated = apply_statistical_gating(execute_lessons_views(con))
    all_lessons = [item for sublist in gated.values() for item in sublist]
    result = LessonsResult(
        generated_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
        power_threshold=DEFAULT_POWER_THRESHOLD,
        total_lessons=len(all_lessons),
        powered_lessons=sum(1 for item in all_lessons if item.powered),
        underpowered_lessons=sum(1 for item in all_lessons if not item.powered),
        lessons_by_view=gated,
        records_summary={
            "craft_records": 2,
            "trial_facts": 9,
            "quality_ledger_evaluated": 10,
            "quality_ledger_pass": 6,
            "quality_ledger_warn": 1,
            "quality_ledger_fail": 1,
            "quality_ledger_quarantine": 1,
        },
    )

    markdown = render_lessons_markdown(result)
    assert (
        "- **Evidence Quality Ledger:** 10 evaluated trials "
        "(pass 6, warn 1, fail 1, quarantine 1)" in markdown
    )
    assert (
        "Ledger Pass | Ledger Warn | Ledger Fail | Ledger Quarantine | Status | Finding |"
        in markdown
    )
    assert "| 4 | 1 | 0 | 1 | `sufficient` |" in markdown


def test_committed_snapshot_has_no_cross_partition_ghosts() -> None:
    """Snapshot lint: loaded trial facts equal distinct (job_id, trial_id)."""
    repo_root = Path(__file__).resolve().parents[1]
    snapshots = sorted(repo_root.glob("derived/parquet/compact/dt=*/trial_facts.parquet"))
    if not snapshots:
        pytest.skip("no committed trial_facts snapshot in this checkout")
    facts = load_trial_facts(repo_root)
    identities = {(str(r["job_id"]), str(r["trial_id"])) for r in facts}
    assert len(facts) == len(identities)


def test_populate_duckdb_fails_loudly_on_schema_driven_row_loss() -> None:
    malformed = _make_mock_trial_facts()
    malformed[0] = {**malformed[0], "primary_reward": "not-a-number"}
    with duckdb.connect(":memory:") as con:
        try:
            populate_duckdb(
                con,
                craft_records=_make_mock_craft_records(),
                trial_facts=malformed,
                analysis_sidecars=_make_mock_analysis_sidecars(),
                observation_records=_make_mock_observation_records(),
            )
        except Exception:
            return  # loud refusal is the required behavior
    raise AssertionError("populate_duckdb silently accepted schema-invalid rows")


def test_end_to_end_rendered_totals_match_independent_pipeline_recompute() -> None:
    """Full pipeline: rendered view totals equal an independent recompute over
    the same registered inputs — no rows may vanish between load and render."""
    repo_root = Path(__file__).resolve().parents[1]
    result = build_lessons(repo_root)
    facts = load_trial_facts(repo_root)
    {(str(r["job_id"]), str(r["trial_id"])) for r in facts}
    assert result.records_summary["trial_facts"] == len(facts)

    craft_records = load_craft_records(repo_root)
    craft_by_digest = {c["task_digest"] for c in craft_records if c.get("task_digest")}
    joined_identities = {
        (str(r["job_id"]), str(r["trial_id"]))
        for r in facts
        if r.get("task_digest") in craft_by_digest
    }
    import duckdb as _duckdb

    from evallab.lessons import execute_lessons_views, populate_duckdb
    with _duckdb.connect(":memory:") as con:
        populate_duckdb(
            con,
            craft_records=craft_records,
            trial_facts=facts,
            analysis_sidecars=load_analysis_sidecars(repo_root),
            observation_records=load_observation_records(repo_root),
            quality_reports=list(load_quality_ledger_bound(repo_root).rows),
        )
        views = execute_lessons_views(con)
    rendered_total = sum(
        int(row["total_trials_n"]) for rows in views.values() for row in rows
    )
    assert rendered_total == len(joined_identities)


def test_render_without_ledger_keeps_zero_columns_and_no_summary_line() -> None:
    gated = apply_statistical_gating({})
    result = LessonsResult(
        generated_at=datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
        power_threshold=DEFAULT_POWER_THRESHOLD,
        total_lessons=3,
        powered_lessons=0,
        underpowered_lessons=3,
        lessons_by_view=gated,
        records_summary={"craft_records": 0, "trial_facts": 0},
    )

    markdown = render_lessons_markdown(result)
    assert "Evidence Quality Ledger:" not in markdown
    assert (
        "| - | none | 0 | 0 | 0 | 0.0% | n/a | 0 | 0 | 0 | 0 | 0 | 0 | "
        "`insufficient n` | insufficient n |" in markdown
    )


def test_collect_lessons_inputs_binds_quality_ledger(tmp_path: Path) -> None:
    root = _sample_lessons_fixture_tree(tmp_path)
    paths = {item["path"] for item in collect_lessons_inputs(root)}
    assert "derived/parquet/trajectory_quality_reports.parquet" in paths
