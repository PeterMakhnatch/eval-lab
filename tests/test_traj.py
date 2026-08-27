"""Tests for M030 LOOP-TRAJ:
Trajectory analysis, mechanical features, loop detection, and review queue.
"""
from __future__ import annotations

import json
import multiprocessing
import tempfile
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from evallab import cli
from evallab.evidence.facts import AnalyzerCallResult
from evallab.labels import (
    evaluate_heuristic_precision,
    label_trajectory,
    label_trajectory_with_model,
    load_behavior_labels,
    persist_behavior_label,
    select_review_queue,
)
from evallab.traj import (
    TRAJ_FEATURES_PARQUET_SCHEMA,
    LoopSuspicion,
    StepOutline,
    TrajectoryOutline,
    _analyze_loop_suspicion,
    _build_phases,
    outline_trajectory,
    project_trajectory_features,
    render_outline,
)


def _persist_after_barrier(label, repo_root: Path, derived_root: Path, barrier) -> None:
    barrier.wait()
    persist_behavior_label(
        label, repo_root=repo_root, derived_root=derived_root
    )



@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def real_canary_trial(repo_root: Path) -> Path:
    canary_dir = (
        repo_root
        / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
    )
    assert canary_dir.is_dir(), f"Expected canary trial at {canary_dir}"
    return canary_dir


def test_outline_real_canary_trajectory(real_canary_trial: Path, repo_root: Path) -> None:
    """Outline real ATIF canary evidence deterministically."""
    outline = outline_trajectory(real_canary_trial, repo_root=repo_root)

    assert outline.status == "featured"
    assert outline.unavailable_reason is None
    assert outline.trial_name == "event-summary__h2D9f6f"
    assert outline.task_name == "local-lab/event-summary"
    assert outline.agent_name == "codex"
    assert outline.model_name == "gpt-5.6-terra"
    assert outline.primary_reward == 1.0
    assert outline.total_steps == 11
    assert outline.agent_steps == 6
    assert outline.system_steps == 3
    assert outline.user_steps == 2
    assert outline.total_tool_calls == 5
    assert outline.total_errors == 0
    assert outline.loop_suspicion.score == 0.0
    assert outline.loop_suspicion.detected is False
    assert outline.total_prompt_tokens > 80000
    assert outline.step_to_first_tool == 6

    # Ordered phases
    assert len(outline.phases) == 3
    assert outline.phases[0].phase_type == "setup"
    assert outline.phases[0].step_start == 1
    assert outline.phases[0].step_end == 3

    assert outline.phases[1].phase_type == "prompt"
    assert outline.phases[1].step_start == 4
    assert outline.phases[1].step_end == 5

    assert outline.phases[2].phase_type == "work"
    assert outline.phases[2].step_start == 6
    assert outline.phases[2].step_end == 11

    # Text rendering
    rendered = render_outline(outline)
    assert "TRAJECTORY OUTLINE: event-summary__h2D9f6f [FEATURED]" in rendered
    assert "local-lab/event-summary" in rendered
    assert "codex (gpt-5.6-terra)" in rendered
    assert "ORDERED PHASES:" in rendered
    assert "METRICS SUMMARY:" in rendered


def test_path_jail_refuses_escaping_paths(repo_root: Path) -> None:
    """Paths escaping allowed roots resolve to accounted_unavailable status."""
    escaping_target = "../../etc/passwd"
    outline = outline_trajectory(escaping_target, repo_root=repo_root)

    assert outline.status == "accounted_unavailable"
    assert "path_escapes_jail" in (outline.unavailable_reason or "")
    assert outline.total_steps == 0
    assert outline.total_tool_calls == 0


def test_missing_trajectory_file_accounting(repo_root: Path) -> None:
    """Missing trajectory file results in explicit accounted_unavailable state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_trial = Path(tmpdir) / "empty_trial"
        tmp_trial.mkdir()
        (tmp_trial / "result.json").write_text(
            json.dumps(
                {
                    "id": "trial-missing-1",
                    "trial_name": "empty_trial",
                    "task_name": "task-x",
                }
            )
        )

        outline = outline_trajectory(tmp_trial, repo_root=Path(tmpdir))

        assert outline.status == "accounted_unavailable"
        assert outline.unavailable_reason == "missing_trajectory_file"
        assert outline.total_steps == 0
        assert outline.total_tool_calls == 0
        assert outline.total_errors == 0


def test_unparseable_trajectory_json_accounting() -> None:
    """Corrupt JSON in trajectory results in accounted_unavailable with exception detail."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_trial = Path(tmpdir) / "corrupt_trial"
        agent_dir = tmp_trial / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "trajectory.json").write_text("{corrupted json...")
        (tmp_trial / "result.json").write_text(json.dumps({"id": "trial-corrupt-1"}))

        outline = outline_trajectory(tmp_trial, repo_root=Path(tmpdir))

        assert outline.status == "accounted_unavailable"
        assert "unparseable_trajectory_json" in (outline.unavailable_reason or "")
        assert outline.total_steps == 0


def test_loop_suspicion_consecutive_commands() -> None:
    """Repeated consecutive commands trigger loop suspicion."""
    steps = [
        StepOutline(
            step_id=1,
            source="agent",
            timestamp=None,
            model_name="model-a",
            tool_name="exec",
            tool_command="pytest tests/unit",
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.001,
            thought_snippet=None,
        ),
        StepOutline(
            step_id=2,
            source="agent",
            timestamp=None,
            model_name="model-a",
            tool_name="exec",
            tool_command="pytest tests/unit",
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.001,
            thought_snippet=None,
        ),
        StepOutline(
            step_id=3,
            source="agent",
            timestamp=None,
            model_name="model-a",
            tool_name="exec",
            tool_command="pytest tests/unit",
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.001,
            thought_snippet=None,
        ),
    ]

    loop = _analyze_loop_suspicion(steps)
    assert loop.repeated_command_count >= 1
    assert loop.score >= 0.35
    assert any("repeated_consecutive_command" in r for r in loop.reasons)


def test_loop_suspicion_failing_commands() -> None:
    """Repeated failing commands trigger loop suspicion."""
    steps = [
        StepOutline(
            step_id=i,
            source="agent",
            timestamp=None,
            model_name="model-a",
            tool_name="exec",
            tool_command="cargo build",
            exit_code=1,
            is_error=True,
            error_message="compile error",
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.001,
            thought_snippet=None,
        )
        for i in range(1, 5)
    ]

    loop = _analyze_loop_suspicion(steps)
    assert loop.repeated_error_count >= 1
    assert loop.score >= 0.50
    assert loop.detected is True
    assert any("repeated_failing_command" in r for r in loop.reasons)


def test_loop_suspicion_cyclic_pattern() -> None:
    """Alternating tool calling patterns trigger cyclic tool pattern loop suspicion."""
    tools = ["read", "grep", "read", "grep", "read", "grep"]
    steps = [
        StepOutline(
            step_id=i,
            source="agent",
            timestamp=None,
            model_name="model-a",
            tool_name=t,
            tool_command=f"arg_{i}",
            exit_code=0,
            is_error=False,
            error_message=None,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            cost_usd=0.001,
            thought_snippet=None,
        )
        for i, t in enumerate(tools, start=1)
    ]

    loop = _analyze_loop_suspicion(steps)
    assert loop.cyclic_patterns_count >= 1
    assert any("cyclic_tool_pattern" in r for r in loop.reasons)


def test_phase_grouping_and_ordering() -> None:
    """Steps are partitioned into clean, contiguous semantic phases."""
    steps = [
        StepOutline(1, "system", None, None, None, None, None, False, None, 10, 0, 0, 0.0, None),
        StepOutline(2, "system", None, None, None, None, None, False, None, 10, 0, 0, 0.0, None),
        StepOutline(3, "user", None, None, None, None, None, False, None, 20, 0, 0, 0.0, None),
        StepOutline(
            4, "agent", None, None, "read", "foo.txt", 0, False, None, 30, 10, 0, 0.001, None
        ),
        StepOutline(
            5, "agent", None, None, "edit", "foo.txt", 0, False, None, 40, 20, 0, 0.002, None
        ),
    ]

    phases = _build_phases(steps)
    assert len(phases) == 3
    assert phases[0].phase_type == "setup"
    assert phases[0].step_count == 2
    assert phases[1].phase_type == "prompt"
    assert phases[1].step_count == 1
    assert phases[2].phase_type == "work"
    assert phases[2].step_count == 2
    assert phases[2].tool_calls == 2


def test_project_mechanical_features_to_parquet(repo_root: Path) -> None:
    """Extract mechanical features from all runs and write Parquet table."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_root = Path(tmpdir) / "traj_features"
        result = project_trajectory_features(
            runs_roots=[repo_root / "research/evidence/runs"],
            output_root=out_root,
            repo_root=repo_root,
        )

        assert result.total_scanned > 0
        assert result.featured_count > 0
        assert result.output_path.is_file()

        tbl = pq.read_table(result.output_path)
        assert tbl.num_rows == result.table_rows
        for col_name in TRAJ_FEATURES_PARQUET_SCHEMA.names:
            assert col_name in tbl.column_names


def test_review_queue_prioritization_and_diversity(repo_root: Path) -> None:
    """Review queue selects unlabeled real-agent trials across diverse task families."""
    queue = select_review_queue(
        limit=3,
        runs_roots=[repo_root / "research/evidence/runs"],
        repo_root=repo_root,
    )

    assert len(queue) <= 3
    assert len(queue) > 0
    # Controls excluded
    for item in queue:
        assert item.agent_name.lower() not in {"oracle", "nop"}
        assert item.next_command.startswith("uv run evallab traj label")
        assert item.suggested_taxonomy is not None

    # Determinism: re-running yields identical selection
    queue_again = select_review_queue(
        limit=3,
        runs_roots=[repo_root / "research/evidence/runs"],
        repo_root=repo_root,
    )
    assert [i.trial_id for i in queue] == [i.trial_id for i in queue_again]


def test_human_label_idempotence(repo_root: Path) -> None:
    """Labeling persists human labels idempotently without duplication."""
    with tempfile.TemporaryDirectory() as tmpdir:
        derived = Path(tmpdir) / "derived/parquet"

        trial_cand = (
            repo_root
            / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
        )
        l1 = label_trajectory(
            trial_cand,
            label="tool_use",
            note="Initial observation",
            author="peter",
            repo_root=repo_root,
            derived_root=derived / "behavior_labels",
        )

        assert l1.label == "tool_use"
        assert l1.rationale == "Initial observation"
        assert l1.provenance == "human"

        labels = load_behavior_labels(
            repo_root=repo_root, derived_root=derived / "behavior_labels"
        )
        assert len(labels) == 1
        assert labels[0].trial_id == l1.trial_id

        # Update note / taxonomy idempotently
        l2 = label_trajectory(
            trial_cand,
            label="clean_success",
            note="Updated verdict after review",
            author="peter",
            repo_root=repo_root,
            derived_root=derived / "behavior_labels",
        )
        assert l2.label == "clean_success"
        labels_after = load_behavior_labels(
            repo_root=repo_root, derived_root=derived / "behavior_labels"
        )
        assert len(labels_after) == 1
        assert labels_after[0].label == "clean_success"
        assert labels_after[0].rationale == "Updated verdict after review"

def test_label_persistence_preserves_noop_bytes_and_concurrent_writers(
    repo_root: Path,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source = (
            repo_root
            / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
        )
        staging = root / "staging"
        first = label_trajectory(
            source, "clean_success", author="alice", repo_root=repo_root,
            derived_root=staging,
        )
        second = label_trajectory(
            source, "tool_use", author="bob", repo_root=repo_root,
            derived_root=staging,
        )
        destination = root / "concurrent"
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=_persist_after_barrier,
                args=(label, repo_root, destination, barrier),
            )
            for label in (first, second)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            assert process.exitcode == 0

        persisted = load_behavior_labels(repo_root, destination)
        assert {item.author for item in persisted} == {"alice", "bob"}

        parquet = destination / "behavior_labels.parquet"
        before = parquet.read_bytes()
        persist_behavior_label(first, repo_root=repo_root, derived_root=destination)
        assert parquet.read_bytes() == before




def test_heuristic_precision_evaluation(repo_root: Path) -> None:
    """Heuristic precision report compares human labels vs heuristic proposals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        derived = Path(tmpdir) / "derived/parquet"
        trial_cand = (
            repo_root
            / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
        )

        label_trajectory(
            trial_cand,
            label="clean_success",
            note="Ground truth",
            author="peter",
            repo_root=repo_root,
            derived_root=derived / "behavior_labels",
        )

        report = evaluate_heuristic_precision(
            repo_root=repo_root, derived_root=derived / "behavior_labels"
        )

        assert report.human_label_count == 1
        assert report.matched_trials_count == 1
        assert report.exact_taxonomy_matches == 1
        assert report.precision == 1.0

def test_heuristic_precision_counts_every_human_author(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        derived = Path(tmpdir) / "behavior_labels"
        trial = (
            repo_root
            / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
        )
        label_trajectory(
            trial, "clean_success", author="alice", repo_root=repo_root,
            derived_root=derived,
        )
        label_trajectory(
            trial, "tool_use", author="bob", repo_root=repo_root,
            derived_root=derived,
        )

        report = evaluate_heuristic_precision(repo_root, derived)

        assert report.human_label_count == 2
        assert report.matched_trials_count == 2
        assert report.exact_taxonomy_matches == 1
        assert report.precision == 0.5
        assert [item["human_author"] for item in report.disagreements] == ["bob"]


def test_model_labeler_uses_guarded_adapter_and_validates_evidence() -> None:
    outline = TrajectoryOutline(
        trial_id="t1",
        job_id="j1",
        trial_name="trial-1",
        job_name="job-1",
        task_name="task-1",
        agent_name="codex",
        agent_version=None,
        model_name="model-x",
        status="featured",
        unavailable_reason=None,
        source_path="path",
        source_sha256=f"sha256:{'a' * 64}",
        duration_seconds=10.0,
        primary_reward=1.0,
        exception_class=None,
        total_steps=1,
        agent_steps=1,
        system_steps=0,
        user_steps=0,
        total_tool_calls=0,
        total_errors=0,
        recovery_count=0,
        step_to_first_tool=None,
        step_to_first_edit=None,
        time_to_first_tool_seconds=None,
        time_to_first_edit_seconds=None,
        total_prompt_tokens=10,
        total_completion_tokens=10,
        total_cached_tokens=0,
        total_cost_usd=0.001,
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        phases=(),
        steps=(StepOutline(
            step_id=1, source="agent", timestamp=None, model_name="model-x",
            tool_name=None, tool_command=None, exit_code=None, is_error=False,
            error_message=None, prompt_tokens=10, completion_tokens=10,
            cached_tokens=0, cost_usd=0.001, thought_snippet="inspected evidence",
        ),),
        citations=(),
    )

    def adapter(prompt: str, schema: dict[str, object]) -> AnalyzerCallResult:
        assert "do not infer unobserved filesystem effects" in prompt
        assert schema["type"] == "object"
        return AnalyzerCallResult(raw_output=json.dumps({
            "label": "evidence_driven",
            "rationale": "The recorded step cites the inspected evidence.",
            "confidence": "high",
            "evidence": [{"step_id": 1, "supports": "Inspected evidence"}],
        }))

    label = label_trajectory_with_model(
        outline,
        adapter=adapter,
        model="model-x",
        agent="guarded-test",
        agent_version="1",
        rubric_digest=f"sha256:{'b' * 64}",
    )
    assert label.label == "evidence_driven"
    assert label.provenance == "model"
    assert label.evidence[0].step_id == 1


def test_sql_traj_views_with_data() -> None:
    """Test sql/traj_views.sql in DuckDB with populated fixtures."""
    sql = Path("sql/traj_views.sql").read_text()
    with duckdb.connect(":memory:") as con:
        con.execute(sql)

        # Insert fixture feature
        con.execute(
            """
                INSERT INTO traj_features VALUES (
                    't-001', 'j-001', 'trial-001', 'job-001', 'task-a',
                    'codex', '0.1.0', 'gpt-5.6', 'featured', NULL,
                    'path/to/traj', 'sha256:abc', 10, 8, 1, 1,
                    6, 2, '{"exec":6}', 2, 2, 0.75, true, '["repeated_cmd"]', 2,
                    2, 4, 1.2, 5.4, 5000, 500, 4000, 0.05, 1.0, NULL, 35.0, '2026-08-19',
                    450.0, 2
                )
            """
        )
        con.execute(
            """
            INSERT INTO behavior_labels (
                schema_version, label_id, target_type, target_id, trial_id,
                trial_name, task_name, taxonomy, label, rationale, provenance,
                author, created_at, evidence_json, source_sha256, model_name
            ) VALUES (
                1, 'sha256:model-label', 'trajectory', 't-001', 't-001',
                'trial-001', 'task-a', 'trajectory_behavior/v1', 'tool_use',
                'Model identified tool use', 'model', 'analyzer', '2026-08-19',
                '[]', 'sha256:abc', 'judge-model'
            ), (
                1, 'sha256:human-trial-label', 'trial', 't-001', 't-001',
                'trial-001', 'task-a', 'trial_analysis/v1', 'correctness',
                'Human reviewed the trial result', 'human', 'peter', '2026-08-19',
                '[]', 'sha256:abc', NULL
            )
            """
        )

        loops = con.execute("SELECT * FROM v_traj_loops").fetchall()
        assert len(loops) == 1
        assert loops[0][0] == "t-001"
        assert loops[0][8] == 2  # error_count
        assert loops[0][9] == 0.75  # loop_suspicion_score

        tool_mix = con.execute("SELECT * FROM v_traj_tool_mix").fetchall()
        assert len(tool_mix) == 1
        assert tool_mix[0][0] == "task-a"
        assert tool_mix[0][5] == 6  # total_tool_calls

        recovery = con.execute("SELECT * FROM v_traj_error_recovery").fetchall()
        assert len(recovery) == 1
        assert recovery[0][4] == 2  # total_recoveries = 2
        assert recovery[0][5] == 1.0  # recovery_rate = 2/2 = 1.0
        labels = con.execute(
            """
            SELECT label, label_model_name, trajectory_model_name
            FROM v_traj_labels
            WHERE label_id = 'sha256:model-label'
            """
        ).fetchall()
        assert labels == [("tool_use", "judge-model", "gpt-5.6")]
        queue = con.execute(
            "SELECT trial_id FROM v_traj_queue ORDER BY trial_id"
        ).fetchall()
        assert queue == [("t-001",)]

        summary = con.execute("SELECT * FROM v_traj_summary").fetchone()
        assert summary[0] == 1  # total_trials
        assert summary[1] == 1  # featured_trials
        assert summary[3] == 1  # loop_detected_trials
        assert summary[9] == 1  # human_labels_count


def test_cli_traj_commands(real_canary_trial: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test CLI dispatch for evallab traj subcommands."""
    # 1. outline
    rc = cli.run_cli(["traj", "outline", str(real_canary_trial)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TRAJECTORY OUTLINE: event-summary__h2D9f6f" in out

    # outline --json
    rc_json = cli.run_cli(["traj", "outline", str(real_canary_trial), "--json"])
    assert rc_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["trial_name"] == "event-summary__h2D9f6f"

    # 2. queue
    rc_q = cli.run_cli(["traj", "queue", "--limit", "2"])
    assert rc_q == 0
    out_q = capsys.readouterr().out
    assert "TRAJECTORY REVIEW QUEUE" in out_q

    # 3. label
    rc_lbl = cli.run_cli(
        ["traj", "label", str(real_canary_trial), "test_label", "--note", "CLI label test"]
    )
    assert rc_lbl == 0
    out_lbl = capsys.readouterr().out
    assert "Recorded label: event-summary__h2D9f6f -> test_label by peter [human]" in out_lbl

    # 4. project
    rc_proj = cli.run_cli(["traj", "project"])
    assert rc_proj == 0
    out_proj = capsys.readouterr().out
    assert "Projected" in out_proj

    # 5. report
    rc_rep = cli.run_cli(["traj", "report"])
    assert rc_rep == 0
    out_rep = capsys.readouterr().out
    assert "HEURISTIC PRECISION REPORT" in out_rep


def test_invalid_trajectory_shape_is_accounted(repo_root: Path) -> None:
    """Valid JSON with a non-object payload cannot become empty success."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial = Path(tmpdir) / "job" / "trial"
        (trial / "agent").mkdir(parents=True)
        (trial / "agent" / "trajectory.json").write_text("[]")
        (trial / "result.json").write_text(json.dumps({"id": "shape-1"}))

        outline = outline_trajectory(trial, repo_root=Path(tmpdir))

        assert outline.status == "accounted_unavailable"
        assert outline.unavailable_reason.startswith("invalid_trajectory_shape")
        assert outline.total_steps == 0


def test_feature_projection_is_byte_deterministic(repo_root: Path) -> None:
    """Repeated projection of immutable sources produces the same Parquet bytes."""
    runs = repo_root / "research/evidence/runs"
    with tempfile.TemporaryDirectory() as tmpdir:
        first = project_trajectory_features(
            runs_roots=[runs],
            output_root=Path(tmpdir) / "one",
            repo_root=repo_root,
        )
        second = project_trajectory_features(
            runs_roots=[runs],
            output_root=Path(tmpdir) / "two",
            repo_root=repo_root,
        )

        assert first.table_rows == second.table_rows
        assert first.sha256 == second.sha256
        assert first.output_path.read_bytes() == second.output_path.read_bytes()


def test_label_same_submission_is_a_true_noop(repo_root: Path) -> None:
    """Idempotence preserves the original label timestamp and file bytes."""
    trial = (
        repo_root
        / "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        labels_root = Path(tmpdir) / "labels"
        first = label_trajectory(
            trial,
            "clean_success",
            note="same",
            repo_root=repo_root,
            derived_root=labels_root,
        )
        before = (labels_root / "behavior_labels.parquet").read_bytes()
        second = label_trajectory(
            trial,
            "clean_success",
            note="same",
            repo_root=repo_root,
            derived_root=labels_root,
        )

        assert second == first
        assert (labels_root / "behavior_labels.parquet").read_bytes() == before


def test_attach_surface_exposes_trajectory_features(repo_root: Path) -> None:
    """The shared DuckDB attach surface can query the projected feature table."""
    from evallab.attach import attach

    runs = repo_root / "research/evidence/runs"
    with tempfile.TemporaryDirectory() as tmpdir:
        derived = Path(tmpdir) / "derived"
        projection = project_trajectory_features(
            runs_roots=[runs],
            output_root=derived / "traj_features",
            repo_root=repo_root,
        )
        attached = attach(repo_root=repo_root, explicit_derived=derived)
        try:
            row = attached.connection.execute(
                "SELECT trial_id, status FROM traj_features "
                "WHERE source_sha256 <> '' ORDER BY trial_id LIMIT 1"
            ).fetchone()
        finally:
            attached.connection.close()

        assert projection.featured_count > 0
        assert row is not None
        assert row[1] == "featured"
