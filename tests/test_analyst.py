"""Tests for ANALYST durable agent analysis with stored reasoning trajectories."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from evallab.analyst import (
    AnalystResult,
    Analyzer,
    ModelAnalyzer,
    ModelProviderRefusedError,
    StubAnalyzer,
    list_analyses,
    run_analysis,
)
from evallab.cli import run_cli
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
    record, _, _, _ = run_analysis(
        "trial_01", model=None, repo_root=tmp_path, derived_root=derived
    )
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
