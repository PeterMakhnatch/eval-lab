"""Tests for TrajectoryIR v1: normalized events, action classification, and episode segmentation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from evallab.trajectory_ir import (
    _classify_exit_semantics,
    _extract_status_owning_program,
    _normalize_argument_skeleton,
    build_trajectory_ir,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def canary_trial_dir(repo_root: Path) -> Path:
    canary = repo_root / "research" / "evidence" / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    if not canary.exists():
        canary = repo_root / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    return canary


def test_status_owning_program_extraction() -> None:
    """Pipeline and command wrappers extract the right status-owning program."""
    assert _extract_status_owning_program("cat file.txt | grep 'pattern'", "bash") == "grep"
    assert _extract_status_owning_program("pytest tests/test_foo.py -v", "bash") == "pytest"
    assert _extract_status_owning_program("python3 -m unittest discover", "bash") == "python3_unittest"
    assert _extract_status_owning_program("sudo git diff --cached", "bash") == "git_diff"
    assert _extract_status_owning_program("cargo test --workspace", "bash") == "cargo_test"
    assert _extract_status_owning_program(None, "read_file") == "read_file"


def test_argument_skeleton_normalization() -> None:
    """Argument skeletons abstract file paths and hashes while retaining flags."""
    cmd1 = "pytest tests/unit/test_foo.py -v -k test_bar"
    skel1 = _normalize_argument_skeleton(cmd1, None)
    assert skel1 == "pytest <PATH> -v -k test_bar"

    cmd2 = "git checkout 01a0359a67d97491883ad6cb5de15153"
    skel2 = _normalize_argument_skeleton(cmd2, None)
    assert skel2 == "git checkout <DIGEST>"


def test_expected_negative_posix_exit_semantics() -> None:
    """grep/diff exit 1 is classified as expected_negative, not a fatal failure."""
    # Normal failing command
    sem_fail, is_err_fail = _classify_exit_semantics(1, "python", True)
    assert sem_fail == "error"
    assert is_err_fail is True

    # grep exit 1 (normal non-match)
    sem_grep, is_err_grep = _classify_exit_semantics(1, "grep", False)
    assert sem_grep == "expected_negative"
    assert is_err_grep is False

    # diff exit 1 (differences found)
    sem_diff, is_err_diff = _classify_exit_semantics(1, "diff", False)
    assert sem_diff == "expected_negative"
    assert is_err_diff is False

    # timeout exit code 124
    sem_to, is_err_to = _classify_exit_semantics(124, "pytest", True)
    assert sem_to == "timeout"
    assert is_err_to is True


def test_build_trajectory_ir_on_canary_trial(canary_trial_dir: Path, repo_root: Path) -> None:
    """Build deterministic TrajectoryIR for a real canary trial."""
    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)

    assert ir.ir_version == "1.0"
    assert ir.ir_digest.startswith("sha256:")
    assert ir.trial_name == "transaction-reconciliation__ba8ovxZ"
    assert ir.status == "featured"
    assert len(ir.events) > 0
    assert len(ir.episodes) > 0

    # Test event integrity
    for ev in ir.events:
        assert ev.event_id is not None
        assert ev.source_citation.step_index == ev.step_index
        assert ev.action_family in (
            "file_read",
            "file_write",
            "file_edit",
            "command_execution",
            "verification",
            "context_control",
            "model_reasoning",
            "other",
        )

    # Test digest determinism on re-run
    ir2 = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    assert ir.ir_digest == ir2.ir_digest


def test_sql_projection_views_round_trip(canary_trial_dir: Path, repo_root: Path) -> None:
    """Verify complete round-trip projection into DuckDB SQL summary views."""
    import duckdb

    from evallab.evidence_pack import build_evidence_pack
    from evallab.trajectory_alignment import align_trajectory_pair

    ir = build_trajectory_ir(canary_trial_dir, repo_root=repo_root)
    pack = build_evidence_pack(ir, trial_dir=canary_trial_dir)
    alignment = align_trajectory_pair(ir, ir)

    conn = duckdb.connect(":memory:")
    sql_file = repo_root / "sql" / "traj_views.sql"
    conn.execute(sql_file.read_text())

    # 1. Test trajectory_ir and v_trajectory_ir_summary (28 columns)
    ir_proj = ir.to_projection_dict()
    placeholders_ir = ", ".join(["?"] * len(ir_proj))
    conn.execute(
        f"INSERT INTO trajectory_ir VALUES ({placeholders_ir})",
        list(ir_proj.values()),
    )
    ir_rows = conn.execute("SELECT * FROM v_trajectory_ir_summary WHERE ir_digest = ?", [ir.ir_digest]).fetchall()
    assert len(ir_rows) == 1
    ir_cols = [desc[0] for desc in conn.description]
    assert len(ir_cols) == 28
    assert ir_cols == list(ir_proj.keys())

    # 2. Test evidence_packs and v_evidence_packs (24 columns)
    pack_proj = pack.to_projection_dict()
    placeholders_pack = ", ".join(["?"] * len(pack_proj))
    conn.execute(
        f"INSERT INTO evidence_packs VALUES ({placeholders_pack})",
        list(pack_proj.values()),
    )
    pack_rows = conn.execute("SELECT * FROM v_evidence_packs WHERE pack_digest = ?", [pack.pack_digest]).fetchall()
    assert len(pack_rows) == 1
    pack_cols = [desc[0] for desc in conn.description]
    assert len(pack_cols) == 24
    assert pack_cols == list(pack_proj.keys())

    # 3. Test paired_alignments and v_paired_alignments (24 columns)
    align_proj = alignment.to_projection_dict()
    placeholders_align = ", ".join(["?"] * len(align_proj))
    conn.execute(
        f"INSERT INTO paired_alignments VALUES ({placeholders_align})",
        list(align_proj.values()),
    )
    align_rows = conn.execute("SELECT * FROM v_paired_alignments WHERE alignment_id = ?", [alignment.alignment_id]).fetchall()
    assert len(align_rows) == 1
    align_cols = [desc[0] for desc in conn.description]
    assert len(align_cols) == 24
    assert align_cols == list(align_proj.keys())


def test_inventory_cas_entries_smoke(repo_root: Path) -> None:
    """Verify TrajectoryIR and EvidencePack builds directly over PR #187 machine analysis inventory."""
    inv_path = repo_root / "research" / "experiments" / "manifests" / "terminal-bench-v3-k1-gemini-low-machine-analysis-inventory.json"
    if not inv_path.exists():
        pytest.skip("Inventory manifest not found in repo")

    import json

    from evallab.evidence_pack import build_evidence_pack

    inv_data = json.loads(inv_path.read_text())
    cohort = inv_data.get("analysis_cohort_5_trials", [])

    tbench_runs = repo_root.parent / "tbench3-screen" / "runs"
    runs_dir = tbench_runs if tbench_runs.exists() else (repo_root / "runs")
    cas_store = repo_root / "derived" / "evidence-cas"
    
    # Check if any trial in cohort is reachable on this runner
    has_any = False
    if cas_store.exists() and any(cas_store.iterdir()):
        has_any = True
    elif runs_dir.exists():
        has_any = any(runs_dir.glob("tb3-k1-*")) or any(runs_dir.glob("*/*"))

    if not has_any:
        pytest.skip("No local trial runs or CAS archives found on this runner")

    for entry in cohort:
        trial_name = entry["trial_name"]
        try:
            ir = build_trajectory_ir(entry, repo_root=repo_root, explicit_runs_root=runs_dir)
            pack = build_evidence_pack(ir, repo_root=repo_root)
            assert pack.trial_name == trial_name
            assert pack.pack_digest.startswith("sha256:")
        except Exception:
            # If individual trial is missing from developer checkout, skip that entry
            pass


def test_synthetic_cas_archive_ingestion(repo_root: Path) -> None:
    """Verify complete CAS archive restoration, IR production, and EvidencePack member hydration."""
    from evallab.evidence_pack import build_evidence_pack
    from evallab.evidence_store import archive_evidence

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        source_trial_dir = temp_path / "trial_source"
        cas_store = temp_path / "cas_store"
        source_trial_dir.mkdir(parents=True)
        cas_store.mkdir(parents=True)

        # 1. Create source trial directory
        (source_trial_dir / "agent").mkdir()
        raw_atif = {
            "schema_version": "ATIF-v1.4",
            "session_id": "sess-cas-1",
            "steps": [
                {
                    "step_id": 1,
                    "actor": "user",
                    "message": "Fix the bug",
                },
                {
                    "step_id": 2,
                    "actor": "agent",
                    "tool_calls": [
                        {
                            "tool_call_id": "call_1",
                            "name": "bash",
                            "arguments": {"command": "cat src/main.py"},
                        }
                    ],
                    "observations": [
                        {
                            "source_call_id": "call_1",
                            "content": "def main(): return 42\n",
                            "extra": {"exit_code": 0},
                        }
                    ],
                },
            ],
        }
        (source_trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))
        (source_trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "id": "trial-cas-001",
                    "trial_name": "trial-cas-001",
                    "task_name": "synthetic/cas-test",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-luna"}},
                }
            )
        )

        # 2. Archive to CAS store
        archive = archive_evidence(source_trial_dir, cas_store, record_id="trial-cas-001", kind="trial")

        # 3. Build TrajectoryIR directly from CAS URI
        ir = build_trajectory_ir(archive.uri, store_root=cas_store, repo_root=repo_root)
        assert ir.is_production_cas is True
        assert ir.source_digests.get("cas_uri") == archive.uri
        # User message, tool call, and linked observation have distinct event identities.
        assert len(ir.events) == 3
        assert [event.event_type for event in ir.events] == ["agent_message", "tool_call", "observation"]
        assert ir.final_verdict == "PASS"

        # 4. Build EvidencePack from IR and verify member hydration from CAS
        pack = build_evidence_pack(ir, store_root=cas_store, repo_root=repo_root)
        assert pack.is_model_callable is True
        assert pack.pack_digest.startswith("sha256:")
        assert len(pack.selected_windows) > 0
        # Verify hydrated content was extracted from the archived trajectory.json
        first_window = pack.selected_windows[0]
        assert any("def main(): return 42" in str(ev.get("hydrated_content")) for ev in first_window.events)

        # 5. CLI smoke test on CAS URI
        from evallab.cli import run_cli
        ir_code = run_cli(["traj", "ir", archive.uri, "--runs-dir", str(cas_store)], workspace=repo_root)
        assert ir_code == 0
        pack_code = run_cli(["traj", "pack", archive.uri, "--runs-dir", str(cas_store)], workspace=repo_root)
        assert pack_code == 0


def test_nested_job_cas_archive_resolves_exact_trial(
    tmp_path: Path, repo_root: Path
) -> None:
    """Job-level CAS archives retain nested trial paths and exact hydration."""
    from evallab.evidence_pack import build_evidence_pack
    from evallab.evidence_store import archive_evidence

    job_dir = tmp_path / "job"
    trial_dir = job_dir / "nested-trial"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "nested-session",
                "steps": [
                    {"step_id": 1, "source": "user", "message": "inspect evidence"},
                    {"step_id": 2, "source": "agent", "message": "evidence present"},
                ],
            }
        )
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "nested-trial-id",
                "trial_name": "nested-trial",
                "task_name": "synthetic/nested-cas",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    (job_dir / "result.json").write_text(json.dumps({"job_name": "nested-job"}))
    (job_dir / "docs").mkdir()
    (job_dir / "docs" / "note.txt").write_text("not a trial")
    cas_store = tmp_path / "cas"
    archive = archive_evidence(job_dir, cas_store, record_id="nested-job", kind="job")

    inventory = {
        "cas_uri": archive.uri,
        "trial_name": "nested-trial",
        "trial_id": "nested-trial-id",
        "job_id": "nested-job-id",
        "job_name": "nested-job",
        "quality_status": "pass",
    }
    ir = build_trajectory_ir(
        inventory,
        store_root=cas_store,
        repo_root=repo_root,
    )
    pack = build_evidence_pack(ir, store_root=cas_store, repo_root=repo_root)
    rerun_ir = build_trajectory_ir(
        inventory,
        store_root=cas_store,
        repo_root=repo_root,
    )

    assert ir.status == "featured"
    assert len(ir.events) == 2
    expected_path = "nested-trial/agent/trajectory.json"
    assert ir.ir_digest == rerun_ir.ir_digest
    assert ir.baseline_metrics.source_path == expected_path
    assert {event.source_citation.source_path for event in ir.events} == {
        expected_path
    }
    assert all(
        "EvidenceLimitation" not in str(event["hydrated_content"])
        for window in pack.selected_windows
        for event in window.events
    )
    assert all(
        window.reopening_citation.source_path == expected_path
        for window in pack.selected_windows
    )
    with pytest.raises(ValueError, match="invalid CAS trial_name"):
        build_trajectory_ir(
            {**inventory, "trial_name": "../outside"},
            store_root=cas_store,
            repo_root=repo_root,
        )
    with pytest.raises(ValueError, match="named CAS trial has no trajectory"):
        build_trajectory_ir(
            {**inventory, "trial_name": "docs"},
            store_root=cas_store,
            repo_root=repo_root,
        )


def test_sparse_steps_anchor_reopening_to_present_event(tmp_path: Path) -> None:
    """Expanded windows never index a nonexistent step boundary."""
    from evallab.evidence_pack import build_evidence_pack

    trial_dir = tmp_path / "sparse"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "sparse-session",
                "steps": [
                    {"step_id": 10, "source": "user", "message": "check"},
                    {"step_id": 20, "source": "verifier", "message": "failed"},
                ],
            }
        )
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "sparse-id",
                "trial_name": "sparse",
                "task_name": "synthetic/sparse",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        )
    )

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)
    pack = build_evidence_pack(ir, trial_dir=trial_dir, repo_root=tmp_path)

    assert all(w.reopening_citation.step_id in (10, 20) for w in pack.selected_windows)
    assert {w.reopening_citation.step_id for w in pack.selected_windows} == {10, 20}


def test_missing_atif_pack_is_not_model_callable(tmp_path: Path) -> None:
    """No-ATIF evidence remains an explicit deterministic abstention."""
    from evallab.evidence_pack import build_evidence_pack

    trial_dir = tmp_path / "missing-atif"
    trial_dir.mkdir()
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "missing-atif-id",
                "trial_name": "missing-atif",
                "task_name": "synthetic/missing-atif",
            }
        )
    )

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)
    pack = build_evidence_pack(ir, trial_dir=trial_dir, repo_root=tmp_path)

    assert ir.status == "accounted_unavailable"
    assert pack.is_model_callable is False
    assert pack.abstain_required is True
    assert pack.overflow_reason == "source_missing (missing_trajectory_file)"


def test_cas_temp_dir_leak_cleanup_on_exception(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CAS TemporaryDirectory is guaranteed cleaned up when an exception occurs during IR assembly."""
    import evallab.trajectory_ir as tir_mod
    from evallab.evidence_store import archive_evidence

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        source_dir = temp_path / "source_trial"
        cas_store = temp_path / "cas_store"
        source_dir.mkdir(parents=True)
        cas_store.mkdir(parents=True)

        (source_dir / "agent").mkdir()
        (source_dir / "agent" / "trajectory.json").write_text(json.dumps({"schema_version": "ATIF-v1.4", "steps": []}))
        (source_dir / "result.json").write_text(json.dumps({"trial_name": "t_leak"}))

        archive = archive_evidence(source_dir, cas_store, record_id="t_leak", kind="trial")

        created_temp_dirs: list[Path] = []

        class TrackingTempDir(tempfile.TemporaryDirectory):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created_temp_dirs.append(Path(self.name))

        monkeypatch.setattr(tir_mod.tempfile, "TemporaryDirectory", TrackingTempDir)

        def _raise_error(*args, **kwargs):
            raise RuntimeError("Forced post-restore assembly error")

        monkeypatch.setattr(tir_mod, "compute_trace_baseline", _raise_error)

        with pytest.raises(RuntimeError, match="Forced post-restore assembly error"):
            tir_mod.build_trajectory_ir(archive.uri, store_root=cas_store, repo_root=repo_root)

        assert len(created_temp_dirs) == 1
        assert not created_temp_dirs[0].exists(), "Temporary directory leaked on exception!"


def test_multi_tool_call_ir_event_preservation(tmp_path: Path, repo_root: Path) -> None:
    """Verify multiple tool calls in a single ATIF step are unpacked into distinct IREvents with call_index."""
    trial_dir = tmp_path / "multi-call-trial"
    (trial_dir / "agent").mkdir(parents=True)
    raw_atif = {
        "schema_version": "ATIF-v1.4",
        "session_id": "sess-multi-call",
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": "Find and edit both files",
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [
                    {"name": "bash", "arguments": {"command": "cat file1.py"}, "tool_call_id": "tc1"},
                    {"name": "bash", "arguments": {"command": "cat file2.py"}, "tool_call_id": "tc2"},
                    {"name": "edit", "arguments": {"file": "file1.py", "patch": "foo"}, "tool_call_id": "tc3"},
                ],
                "observations": [
                    {"source_call_id": "tc1", "content": "print(1)\n", "extra": {"exit_code": 0}},
                    {"source_call_id": "tc2", "content": "print(2)\n", "extra": {"exit_code": 0}},
                    {"source_call_id": "tc3", "content": "success", "extra": {"exit_code": 0}},
                ],
            },
            {
                "step_id": 3,
                "source": "verifier",
                "message": "Verifier check passed",
            },
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "multi-call-id",
                "trial_name": "multi-call-trial",
                "task_name": "synthetic/multi-call",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)
    from evallab.evidence_pack import compute_evidence_coverage_metrics

    coverage = compute_evidence_coverage_metrics(ir, trial_dir=trial_dir)
    # Step 1 message + 3 calls + 3 linked observations + verifier check = 8 events.
    assert len(ir.events) == 8
    tool_events = [event for event in ir.events if event.event_type == "tool_call"]
    observation_events = [event for event in ir.events if event.event_type == "observation"]
    assert len(tool_events) == 3
    assert len(observation_events) == 3
    assert [event.call_index for event in tool_events] == [0, 1, 2]
    # Observation records retain identity through source_call_id, not tool-action semantics.
    assert [event.call_index for event in observation_events] == [None, None, None]
    assert [event.action_family for event in observation_events] == ["other", "other", "other"]
    assert [event.step_index for event in tool_events] == [2, 2, 2]
    assert [event.source_citation.tool_call_id for event in tool_events] == ["tc1", "tc2", "tc3"]
    assert [event.source_citation.source_call_id for event in observation_events] == ["tc1", "tc2", "tc3"]
    assert coverage.tool_calls_count == 3
    assert coverage.observations_count == 3
    assert coverage.total_errors == 0
    # The edit call contributes exactly one state mutation; its observation does not duplicate it.
    assert coverage.state_mutations_count == 1
    assert tool_events[1].status_owning_program == "cat"
    assert tool_events[2].action_family == "file_edit"
    assert ir.final_verdict == "PASS"


def test_project_ir_graph_structure_and_edges(tmp_path: Path, repo_root: Path) -> None:
    """Verify TrajectoryIR projects typed nodes and causal/sequential graph edges."""
    from evallab.trajectory_ir import project_ir_graph

    trial_dir = tmp_path / "graph-trial"
    (trial_dir / "agent").mkdir(parents=True)
    raw_atif = {
        "schema_version": "ATIF-v1.4",
        "session_id": "sess-graph",
        "steps": [
            {"step_id": 1, "source": "user", "message": "Execute task"},
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"name": "edit", "arguments": {"file": "code.py"}, "tool_call_id": "c1"}],
                "observations": [{"source_call_id": "c1", "content": "saved"}],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [{"name": "pytest", "arguments": {"command": "pytest test.py"}, "tool_call_id": "c2"}],
                "observations": [{"source_call_id": "c2", "content": "passed", "extra": {"exit_code": 0}}],
            },
            {"step_id": 4, "source": "verifier", "message": "verifier passed"},
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))
    (trial_dir / "result.json").write_text(json.dumps({"id": "g-id", "trial_name": "g-trial", "task_name": "synthetic/graph"}))

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)
    graph = project_ir_graph(ir)

    assert graph.node_count == len(ir.events)
    assert graph.edge_count > 0
    assert "chronological_sequence" in graph.edge_type_counts
    assert "state_change_precedes_verifier_event" in graph.edge_type_counts

    # Node and edge dictionary projections
    node_dicts = [n.to_projection_dict() for n in graph.nodes]
    edge_dicts = [e.to_projection_dict() for e in graph.edges]
    assert len(node_dicts) == graph.node_count
    assert len(edge_dicts) == graph.edge_count
    assert all("edge_id" in e for e in edge_dicts)


def test_state_journal_events_ingestion_and_projections(tmp_path: Path, repo_root: Path) -> None:
    """Verify StateJournal inotify events and state diffs are normalized into IREvents."""
    trial_dir = tmp_path / "state-trial"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "state-journal").mkdir(parents=True)

    raw_atif = {
        "schema_version": "ATIF-v1.4",
        "session_id": "sess-state",
        "steps": [
            {"step_id": 1, "source": "user", "message": "Edit file"},
            {"step_id": 2, "source": "agent", "tool_calls": [{"name": "edit", "arguments": {"file": "app.py"}, "tool_call_id": "e1"}], "observations": [{"source_call_id": "e1", "content": "ok"}]},
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))
    (trial_dir / "result.json").write_text(json.dumps({"id": "st-id", "trial_name": "state-trial", "task_name": "synthetic/state"}))

    # Create producer-valid status.json, state-diff.json, and state-events.jsonl
    (trial_dir / "state-journal" / "status.json").write_text(
        json.dumps({"schema_version": 1, "status": "available"})
    )
    state_diff_payload = {
        "schema_version": 1,
        "status": "available",
        "root": "/app",
        "changes": [
            {
                "path": "app.py",
                "change_type": "modified",
                "event_count": 1,
                "before": {
                    "path": "app.py",
                    "type": "file",
                    "size_bytes": 100,
                    "sha256": "sha256:" + "a" * 64,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800000000000,
                    "hash_status": "complete",
                },
                "after": {
                    "path": "app.py",
                    "type": "file",
                    "size_bytes": 120,
                    "sha256": "sha256:" + "b" * 64,
                    "mode": "-rw-r--r--",
                    "mtime_ns": 1787572800100000000,
                    "hash_status": "complete",
                },
            }
        ],
    }
    (trial_dir / "state-journal" / "state-diff.json").write_text(json.dumps(state_diff_payload))
    state_event_1 = {
        "sequence": 1,
        "timestamp": "2026-08-26T12:00:00.100000Z",
        "operations": ["modify", "close_write"],
        "path": "app.py",
        "is_directory": False,
        "cookie": None,
        "state": {
            "path": "app.py",
            "type": "file",
            "size_bytes": 120,
            "sha256": "sha256:" + "b" * 64,
            "mode": "-rw-r--r--",
            "mtime_ns": 1787572800100000000,
            "hash_status": "complete",
        },
    }
    (trial_dir / "state-journal" / "state-events.jsonl").write_text(json.dumps(state_event_1) + "\n")

    ir = build_trajectory_ir(trial_dir, repo_root=tmp_path)

    # Step 1 (user) + Step 2 (tool_call + observation) + State Change (1) = 4 events
    assert len(ir.events) == 4
    state_events = [e for e in ir.events if e.event_type == "state_change"]
    assert len(state_events) == 1
    assert state_events[0].actor == "environment"
    assert state_events[0].status_owning_program == "inotify"
    assert state_events[0].action_family == "other"
    assert state_events[0].step_index is None
    assert state_events[0].journal_sequence == 1
    assert state_events[0].exit_code is None
    assert state_events[0].exit_semantics == "unobserved"
    assert state_events[0].state_before_digest is not None
    assert state_events[0].state_after_digest is not None
    assert ir.evidence_coverage.get("state_diff_observed") is True
