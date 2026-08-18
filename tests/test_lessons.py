"""Tests for statistical lesson aggregation views and engine (WS-D)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from evallab.cohort import wilson_interval
from evallab.contextpack import parse_front_matter
from evallab.lessons import (
    DEFAULT_POWER_THRESHOLD,
    GENERATED_HEADER,
    LessonsResult,
    apply_statistical_gating,
    build_lessons,
    execute_lessons_views,
    generate_lessons_file,
    parse_observation_markdown,
    populate_duckdb,
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
    assert pytest_row["n"] == 6
    assert pytest_row["passed_n"] == 4
    assert pytest_row["exceptions_n"] == 1
    assert pytest_row["failed_unexcepted_n"] == 1
    assert pytest_row["pass_rate_pct"] == pytest.approx(66.67, abs=0.01)
    assert pytest_row["exception_rate_pct"] == pytest.approx(16.67, abs=0.01)

    golden_row = next(r for r in verifier_rows if r["verifier_type"] == "golden_file")
    assert golden_row["n"] == 3
    assert golden_row["passed_n"] == 2
    assert golden_row["exceptions_n"] == 0
    assert golden_row["pass_rate_pct"] == pytest.approx(66.67, abs=0.01)

    # 2. v_loop_rate_by_env checks
    loop_rows = views_data["v_loop_rate_by_env"]
    assert len(loop_rows) == 2
    single_cont_row = next(r for r in loop_rows if not r["env_multi_container"])
    assert single_cont_row["n"] == 6
    assert single_cont_row["loops_n"] == 1
    assert single_cont_row["loop_rate_pct"] == pytest.approx(16.67, abs=0.01)

    # 3. v_failure_by_facet checks
    failure_rows = views_data["v_failure_by_facet"]
    assert len(failure_rows) > 0
    tool_use_row = next(
        (
            r
            for r in failure_rows
            if r["facet_name"] == "verifier_type"
            and r["facet_value"] == "pytest"
            and r["failure_category"] == "tool_use"
        ),
        None,
    )
    assert tool_use_row is not None
    assert tool_use_row["failures_n"] == 1


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
                "failure_category": "tool_use",
                "validity": "valid_agent_attempt",
                "n": 6,
                "failures_n": 2,
                "failure_rate_pct": 33.3,
            },
            {
                "source_repo": "test-repo",
                "facet_name": "difficulty_mechanism",
                "facet_value": "volume",
                "failure_category": "timeout",
                "validity": "harness_failure",
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
