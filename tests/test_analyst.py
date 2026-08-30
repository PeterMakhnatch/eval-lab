"""Tests for ANALYST durable agent analysis with stored reasoning trajectories."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from evallab.analyst import (
    AnalystResult,
    Analyzer,
    JudgeStage,
    ModelAnalyzer,
    ModelProviderRefusedError,
    StubAnalyzer,
    TrialData,
    assemble_context,
    list_analyses,
    resolve_trial,
    run_analysis,
    run_trajectory_judge,
)
from evallab.cli import run_cli
from evallab.evidence_store import archive_evidence
from evallab.lance import build_trajectory_windows
from evallab.lineage import resolve_lineage
from evallab.schemas import AnalysisRecord, ConfidenceClaim, EvidenceCitation


def _create_synthetic_trial(
    root: Path, job_name: str = "job_01", trial_name: str = "trial_01"
) -> Path:
    """Create a synthetic trial with raw result and trajectory files."""
    trial_dir = root / "runs" / job_name / trial_name
    (trial_dir / "agent").mkdir(parents=True, exist_ok=True)

    result_file = trial_dir / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "task_name": "event-summary",
                "primary_reward": 0.0,
                "exception_class": "AssertionError",
                "exception_phase": "eval",
                "duration_seconds": 12.5,
            }
        ),
        encoding="utf-8",
    )

    traj_file = trial_dir / "agent" / "trajectory.json"
    traj_file.write_text(
        json.dumps(
            {
                "schema_version": "ATIF-1.0",
                "session_id": "sess_01",
                "steps": [
                    {
                        "step_id": 0,
                        "source": "system",
                        "timestamp": "2026-08-17T00:00:00Z",
                        "message": "Task initialized",
                    },
                    {
                        "step_id": 1,
                        "source": "agent",
                        "timestamp": "2026-08-17T00:00:05Z",
                        "message": "Attempted file parse but encountered assertion error",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    return trial_dir


def test_stub_analyzer_roundtrip_and_view(tmp_path: Path) -> None:
    """A stub analyzer produces a record that round-trips to disk and back."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    stub = StubAnalyzer(
        category="parser_failure",
        summary="Agent failed during event data parsing due to schema mismatch.",
        evidence=[
            EvidenceCitation(path="runs/job_01/trial_01/agent/trajectory.json", step=1),
            EvidenceCitation(path="runs/job_01/trial_01/result.json", step=None),
        ],
    )

    record, traj, rec_path, traj_path = run_analysis(
        "trial_01",
        analyzer=stub,
        repo_root=tmp_path,
        derived_root=derived,
    )

    assert rec_path.is_file()
    assert traj_path.is_file()

    # Verify JSON deserialization into AnalysisRecord
    loaded_raw = json.loads(rec_path.read_text(encoding="utf-8"))
    clean_dict = {
        k: v for k, v in loaded_raw.items() if k not in {"inputs", "summary", "created_at"}
    }
    reconstructed = AnalysisRecord.model_validate(clean_dict)
    assert reconstructed.analysis_id == record.analysis_id
    assert reconstructed.trial_id == record.trial_id
    assert reconstructed.category == "parser_failure"
    assert len(reconstructed.evidence) == 2

    # Query through DuckDB with sql/analyst.sql views
    sql = Path("sql/analyst.sql").read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        analyses_file = derived / "analyses" / "analyses.parquet"
        traj_file = derived / "analyst_trajectories" / "analyst_trajectories.parquet"
        con.execute(f"SET VARIABLE analyses_parquet = '{analyses_file}';")
        con.execute(f"SET VARIABLE analyst_trajectories_parquet = '{traj_file}';")
        con.execute(
            f"CREATE TABLE analysis_records AS SELECT * FROM read_parquet('{analyses_file}');"
        )
        con.execute(
            f"CREATE TABLE analyst_trajectories AS SELECT * FROM read_parquet('{traj_file}');"
        )
        con.execute(sql)

        rows = con.execute(
            "SELECT analysis_id, category, evidence_count FROM v_analysis_records"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == record.analysis_id
        assert rows[0][1] == "parser_failure"
        assert rows[0][2] == 2


def test_analyst_trajectory_joins_to_conclusion(tmp_path: Path) -> None:
    """The analyst's trajectory is stored and joins to its conclusion by analysis_id."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    custom_steps = [
        {
            "step_id": 10,
            "source": "validator",
            "timestamp": "2026-08-17T01:00:00Z",
            "message": "Inspected reward dimensions",
        }
    ]
    stub = StubAnalyzer(
        category="assertion_error",
        steps=custom_steps,
    )

    record, traj, rec_path, traj_path = run_analysis(
        "trial_01",
        analyzer=stub,
        repo_root=tmp_path,
        derived_root=derived,
    )

    assert traj_path.is_file()
    traj_data = json.loads(traj_path.read_text(encoding="utf-8"))
    assert traj_data["analysis_id"] == record.analysis_id
    assert len(traj_data["steps"]) >= 3  # attacher + reader + analyzer + custom step

    # Test joining in DuckDB
    sql = Path("sql/analyst.sql").read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        analyses_file = derived / "analyses" / "analyses.parquet"
        traj_file = derived / "analyst_trajectories" / "analyst_trajectories.parquet"
        con.execute(
            f"CREATE TABLE analysis_records AS SELECT * FROM read_parquet('{analyses_file}');"
        )
        con.execute(
            f"CREATE TABLE analyst_trajectories AS SELECT * FROM read_parquet('{traj_file}');"
        )
        con.execute(sql)

        joined = con.execute(
            "SELECT analysis_id, category, step_id, step_source, step_message "
            "FROM v_analysis_with_trajectory WHERE analysis_id = ?",
            (record.analysis_id,),
        ).fetchall()

        assert len(joined) >= 3
        for row in joined:
            assert row[0] == record.analysis_id
            assert row[1] == "assertion_error"


def test_empty_evidence_rejected_not_stored(tmp_path: Path) -> None:
    """A result with empty evidence is rejected, not stored."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    class EmptyEvidenceAnalyzer(Analyzer):
        def analyze(self, prompt: str, context: str) -> AnalystResult:
            return AnalystResult(
                category="speculative_guess",
                summary="Opinion without evidence citation",
                evidence=[],  # Empty evidence!
                confidence=ConfidenceClaim(level="low"),
                steps=[],
            )

    with pytest.raises(ValueError, match="Analysis rejected: conclusion has no cited evidence"):
        run_analysis(
            "trial_01",
            analyzer=EmptyEvidenceAnalyzer(),
            repo_root=tmp_path,
            derived_root=derived,
        )

    analysis_dir = tmp_path / "research" / "analysis"
    if analysis_dir.exists():
        stored_files = list(analysis_dir.glob("*.json"))
        assert stored_files == [], "No analysis files should be stored on rejection"


def test_multiple_analyses_persist_without_overwrite(tmp_path: Path) -> None:
    """Two analyses of the same trial both persist; neither overwrites the other."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    stub1 = StubAnalyzer(category="hypothesis_1", summary="First analyst perspective")
    stub2 = StubAnalyzer(category="hypothesis_2", summary="Second analyst differing perspective")

    rec1, _, file1, traj1 = run_analysis(
        "trial_01", analyzer=stub1, repo_root=tmp_path, derived_root=derived
    )
    rec2, _, file2, traj2 = run_analysis(
        "trial_01", analyzer=stub2, repo_root=tmp_path, derived_root=derived
    )

    assert rec1.analysis_id != rec2.analysis_id
    assert rec1.trial_id == rec2.trial_id
    assert file1 != file2
    assert traj1 != traj2
    assert file1.is_file()
    assert file2.is_file()

    listed = list_analyses(tmp_path, trial_id=rec1.trial_id)
    assert len(listed) == 2
    categories = {r["category"] for r in listed}
    assert categories == {"hypothesis_1", "hypothesis_2"}

    # Both present in Parquet view
    sql = Path("sql/analyst.sql").read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        analyses_file = derived / "analyses" / "analyses.parquet"
        traj_file = derived / "analyst_trajectories" / "analyst_trajectories.parquet"
        con.execute(
            f"CREATE TABLE analysis_records AS SELECT * FROM read_parquet('{analyses_file}');"
        )
        con.execute(
            f"CREATE TABLE analyst_trajectories AS SELECT * FROM read_parquet('{traj_file}');"
        )
        con.execute(sql)

        rows = con.execute(
            "SELECT analysis_id, category FROM v_analysis_records ORDER BY category"
        ).fetchall()
        assert len(rows) == 2
        assert {rows[0][1], rows[1][1]} == {"hypothesis_1", "hypothesis_2"}


def test_lineage_resolution_on_stored_record(tmp_path: Path) -> None:
    """evallab lineage on a stored record resolves at least one input rather than unrecorded."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    record, _, rec_path, _ = run_analysis(
        "trial_01",
        analyzer=StubAnalyzer(),
        repo_root=tmp_path,
        derived_root=derived,
    )

    rel_rec_path = f"research/analysis/{record.analysis_id}.json"
    node = resolve_lineage(rel_rec_path, repo_root=tmp_path, explicit_derived=derived)

    assert node.resolved is True
    assert node.status == "resolved"
    assert len(node.inputs) >= 1
    # Check that it resolved to Zone 1 evidence files
    assert any(child.zone == "z1" for child in node.inputs)

    # Also test CLI lineage command
    ret = run_cli(["lineage", rel_rec_path], workspace=tmp_path)
    assert ret == 0


def test_no_model_invoked_without_model_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert the default path raises or refuses rather than reaching a provider."""
    # 1. ModelAnalyzer without model raises ModelProviderRefusedError
    with pytest.raises(
        ModelProviderRefusedError, match="Model analyzer requires an explicit model selector"
    ):
        ModelAnalyzer(model=None)

    # 2. ModelAnalyzer with model selector documents token spend and refuses execution
    analyzer = ModelAnalyzer(model="gpt-4o")
    with pytest.raises(ModelProviderRefusedError, match="spends tokens"):
        analyzer.analyze("prompt", "context")

    # 3. Default run_analysis (model=None, analyzer=None) uses StubAnalyzer with ZERO network calls
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    # Guard against any socket or network access during default run
    import socket

    def guarded_socket(*args, **kwargs):
        raise AssertionError("Network socket opened during default test run!")

    monkeypatch.setattr(socket, "socket", guarded_socket)

    # Default run works without network
    record, _, _, _ = run_analysis("trial_01", model=None, repo_root=tmp_path, derived_root=derived)
    assert record.model == "stub"

    # 4. run_analysis with model flag raises ModelProviderRefusedError
    with pytest.raises(ModelProviderRefusedError):
        run_analysis("trial_01", model="gpt-4o", repo_root=tmp_path, derived_root=derived)


def test_sql_views_in_clean_duckdb_session() -> None:
    """The view resolves in a clean DuckDB session with no pre-created tables."""
    sql = Path("sql/analyst.sql").read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(sql)

        for view_name in [
            "v_analysis_records",
            "v_analyst_trajectories",
            "v_analysis_with_trajectory",
        ]:
            rows = con.execute(f"SELECT * FROM {view_name}").fetchall()
            assert rows == [], f"View {view_name} should return empty result in clean session"


def test_cli_analyst_commands(tmp_path: Path) -> None:
    """Test evallab analyst run, list, show CLI subcommands."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    # 1. run CLI
    ret = run_cli(
        ["analyst", "run", "trial_01", "--derived-root", str(derived)],
        workspace=tmp_path,
    )
    assert ret == 0

    records = list_analyses(tmp_path)
    assert len(records) == 1
    analysis_id = records[0]["analysis_id"]

    # 2. list CLI
    ret_list = run_cli(["analyst", "list"], workspace=tmp_path)
    assert ret_list == 0

    # 3. show CLI
    ret_show = run_cli(["analyst", "show", analysis_id], workspace=tmp_path)
    assert ret_show == 0

    # 4. show CLI with --json
    ret_show_json = run_cli(["analyst", "show", analysis_id, "--json"], workspace=tmp_path)
    assert ret_show_json == 0


@pytest.mark.parametrize(
    "bad_path",
    ["missing.json", "../result.json", "/tmp/result.json"],
)
def test_analysis_rejects_missing_or_escaping_citation_paths(
    tmp_path: Path,
    bad_path: str,
) -> None:
    _create_synthetic_trial(tmp_path)

    class BadPathAnalyzer:
        def analyze(self, prompt: str, context: str) -> AnalystResult:
            del prompt, context
            return AnalystResult(
                category="parser_failure",
                summary="bad citation",
                evidence=[EvidenceCitation(path=bad_path, step=None)],
                confidence=ConfidenceClaim(level="high"),
            )

    with pytest.raises(ValueError, match="citation path"):
        run_analysis(
            "trial_01",
            analyzer=BadPathAnalyzer(),
            repo_root=tmp_path,
            derived_root=tmp_path / "derived",
        )
    assert not (tmp_path / "research" / "analysis").exists()


def test_analysis_binds_tool_citation_to_cited_step(tmp_path: Path) -> None:
    trial_dir = _create_synthetic_trial(tmp_path)
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    payload = json.loads(trajectory_path.read_text())
    payload["steps"][0]["tool_calls"] = [{"tool_name": "read", "tool_call_id": "call-step-0"}]
    trajectory_path.write_text(json.dumps(payload))

    class MisboundAnalyzer:
        def analyze(self, prompt: str, context: str) -> AnalystResult:
            del prompt, context
            return AnalystResult(
                category="tool_error",
                summary="misbound citation",
                evidence=[
                    EvidenceCitation(
                        path="runs/job_01/trial_01/agent/trajectory.json",
                        step=1,
                    )
                ],
                confidence=ConfidenceClaim(level="high"),
                citation_metadata=[{"tool_call_id": "call-step-0"}],
            )

    with pytest.raises(ValueError, match="tool_call.*at step 1"):
        run_analysis(
            "trial_01",
            analyzer=MisboundAnalyzer(),
            repo_root=tmp_path,
            derived_root=tmp_path / "derived",
        )


def test_cas_hydration_rejects_tampered_blob(tmp_path: Path) -> None:
    source_trial = _create_synthetic_trial(tmp_path / "source")
    store = tmp_path / "evidence-store"
    archive = archive_evidence(
        source_trial,
        store,
        record_id="trial_01",
        kind="trial",
    )

    tampered_trial = _create_synthetic_trial(tmp_path / "tampered")
    (tampered_trial / "result.json").write_text('{"tampered": true}')
    tampered_archive = archive_evidence(
        tampered_trial,
        store,
        record_id="tampered",
        kind="trial",
    )
    archive.blob_path.write_bytes(tampered_archive.blob_path.read_bytes())

    with pytest.raises(ValueError, match="CAS evidence digest mismatch"):
        resolve_trial(
            "trial_01",
            tmp_path,
            explicit_derived=tmp_path / "derived",
            runs_root=tmp_path / "absent-runs",
            evidence_store_root=store,
            cas_uri=archive.uri,
        )


def test_result_only_cas_records_only_hydrated_member(tmp_path: Path) -> None:
    source = tmp_path / "result-only"
    source.mkdir()
    (source / "result.json").write_text("{}")
    store = tmp_path / "evidence-store"
    archive = archive_evidence(
        source,
        store,
        record_id="result-only",
        kind="trial",
    )

    trial = resolve_trial(
        "result-only",
        tmp_path,
        explicit_derived=tmp_path / "derived",
        runs_root=tmp_path / "absent-runs",
        evidence_store_root=store,
        cas_uri=archive.uri,
    )
    assert trial.trajectory_steps == []
    assert trial.result_payload == {}
    assert [item["member"] for item in trial.inputs] == ["result.json"]
    assert trial.inputs[0]["digest"].startswith("sha256:")
    assert trial.inputs[0]["content_digest"] == archive.content_digest


def test_context_keeps_complete_errors_and_counts_only_ordinary_steps() -> None:
    stderr = "x" * 6000
    steps = [
        {
            "step_id": 0,
            "source": "agent",
            "error": "boom",
            "stderr": stderr,
        },
        *[
            {
                "step_id": index + 1,
                "source": "agent",
                "message": f"ordinary-{index}",
            }
            for index in range(26)
        ],
    ]
    trial = TrialData(
        trial_id="trial",
        job_id="job",
        job_name="job",
        trial_name="trial",
        task_name="task",
        primary_reward=0.0,
        exception_class="RuntimeError",
        agent_name="agent",
        model_name="model",
        trajectory_path=None,
        result_path=None,
        trajectory_steps=steps,
        inputs=[],
        result_payload={"traceback": "full traceback"},
    )
    _, context = assemble_context(trial)
    assert stderr in context
    assert "full traceback" in context
    assert "[ordinary steps bounded: included 24 of 26]" in context


def test_analysis_rejects_out_of_rubric_category_before_persistence(
    tmp_path: Path,
) -> None:
    _create_synthetic_trial(tmp_path)
    analyzer = StubAnalyzer(category="free_form_category")
    with pytest.raises(ValueError, match="analyst-context-v2 enum"):
        run_analysis(
            "trial_01",
            analyzer=analyzer,
            repo_root=tmp_path,
            derived_root=tmp_path / "derived",
        )
    assert not (tmp_path / "research" / "analysis").exists()


def test_run_analysis_persists_bindings_and_decision_ineligible(tmp_path: Path) -> None:
    """run_analysis binds source identities and decision_eligible remains False."""
    _create_synthetic_trial(tmp_path)
    derived = tmp_path / "derived" / "parquet"

    manifest_digest = "sha256:" + "a" * 64
    snapshot_digest = "sha256:" + "b" * 64
    queue_digest = "sha256:" + "c" * 64

    stub = StubAnalyzer(
        category="parser_failure",
        summary="Agent failed during parser test.",
        evidence=[
            EvidenceCitation(path="runs/job_01/trial_01/result.json", step=None),
        ],
    )

    record, traj, rec_path, traj_path = run_analysis(
        "trial_01",
        analyzer=stub,
        repo_root=tmp_path,
        derived_root=derived,
        analysis_role="review_queue_review",
        source_manifest_digest=manifest_digest,
        source_snapshot_digest=snapshot_digest,
        source_queue_digest=queue_digest,
    )

    assert record.analysis_role == "review_queue_review"
    assert record.source_manifest_digest == manifest_digest
    assert record.source_snapshot_digest == snapshot_digest
    assert record.source_queue_digest == queue_digest
    assert record.decision_eligible is False

    # Check persisted conclusion JSON
    loaded_raw = json.loads(rec_path.read_text(encoding="utf-8"))
    assert loaded_raw["analysis_role"] == "review_queue_review"
    assert loaded_raw["source_manifest_digest"] == manifest_digest
    assert loaded_raw["source_snapshot_digest"] == snapshot_digest
    assert loaded_raw["source_queue_digest"] == queue_digest
    assert loaded_raw["decision_eligible"] is False

    # Check list_analyses projection
    listed = list_analyses(tmp_path)
    assert len(listed) == 1
    assert listed[0]["analysis_role"] == "review_queue_review"
    assert listed[0]["source_manifest_digest"] == manifest_digest
    assert listed[0]["source_snapshot_digest"] == snapshot_digest
    assert listed[0]["source_queue_digest"] == queue_digest
    assert listed[0]["decision_eligible"] is False

    # Check Parquet table projection
    with duckdb.connect(":memory:") as con:
        analyses_file = derived / "analyses" / "analyses.parquet"
        con.execute(f"CREATE TABLE analyses AS SELECT * FROM read_parquet('{analyses_file}');")
        row = con.execute(
            "SELECT analysis_role, source_manifest_digest, source_snapshot_digest, source_queue_digest, decision_eligible FROM analyses"
        ).fetchone()
        assert row is not None
        assert row[0] == "review_queue_review"
        assert row[1] == manifest_digest
        assert row[2] == snapshot_digest
        assert row[3] == queue_digest
        assert row[4] is False


def _judge_windows():
    return build_trajectory_windows(
        [
            {"step_id": 1, "message": "memory handle was omitted"},
            {"step_id": 2, "message": "clean replay completed successfully"},
        ],
        snapshot_digest="sha256:" + "1" * 64,
        source_digest="sha256:" + "2" * 64,
        redaction_policy_digest="sha256:" + "3" * 64,
        source_is_redacted=True,
        job_id="judge-job",
        trial_id="judge-trial",
        window_steps=1,
        stride_steps=1,
    )


class ScriptedTrajectoryAnalyzer:
    model = "scripted-trajectory-judge"

    def __init__(self, *, missing_counterevidence: bool = False) -> None:
        self.missing_counterevidence = missing_counterevidence

    def analyze(self, prompt: str, context: str) -> AnalystResult:
        windows = _judge_windows()
        support = EvidenceCitation(path=windows[0].window_digest, step=1)
        counter = EvidenceCitation(path=windows[1].window_digest, step=2)
        if "Stage: TRIAGE" in prompt:
            summary = "The omission window is high signal."
            contradictions: list[EvidenceCitation] = []
            alternatives: list[str] = []
        elif "Stage: INSPECT" in prompt:
            summary = "The omitted handle precedes the stale binding."
            contradictions = []
            alternatives = []
        else:
            summary = "The cited trajectory supports a memory failure."
            contradictions = [] if self.missing_counterevidence else [counter]
            alternatives = ["The omission may instead reflect a capture failure."]
        return AnalystResult(
            category="memory_failure",
            summary=summary,
            evidence=[support],
            contradicting_evidence=contradictions,
            alternative_explanations=alternatives,
            confidence=ConfidenceClaim(
                level="high",
                n=1,
                provenance_digest="sha256:" + "4" * 64,
            ),
        )


def test_trajectory_judge_runs_triage_inspect_final_repeats_and_disagreement() -> None:
    windows = _judge_windows()
    runs, disagreement = run_trajectory_judge(
        ScriptedTrajectoryAnalyzer(),
        windows,
        rubric="Classify the primary agentic failure with exact window citations.",
        repeats=3,
    )
    assert len(runs) == 3
    assert all(
        tuple(stage.stage for stage in run.stages)
        == (JudgeStage.TRIAGE, JudgeStage.INSPECT, JudgeStage.FINAL)
        for run in runs
    )
    assert all(run.stages[-1].supporting_citations for run in runs)
    assert all(run.stages[-1].contradicting_citations for run in runs)
    assert all(run.stages[-1].alternative_explanations for run in runs)
    assert all(run.decision_eligible is False for run in runs)
    assert disagreement.consensus_category == "memory_failure"
    assert disagreement.agreement_rate == 1.0
    assert disagreement.unresolved is False
    assert disagreement.decision_eligible is False


def test_trajectory_judge_refuses_final_without_counterevidence() -> None:
    with pytest.raises(ValueError, match="contradicting citations"):
        run_trajectory_judge(
            ScriptedTrajectoryAnalyzer(missing_counterevidence=True),
            _judge_windows(),
            rubric="Require counterevidence.",
            repeats=1,
        )
