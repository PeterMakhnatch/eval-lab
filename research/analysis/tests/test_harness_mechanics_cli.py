"""Focused behavioral tests for Harness Mechanics Lab CLI and reader integration."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.trajectory_ir import TrajectoryIR

_PKG_ROOT = Path(__file__).resolve().parents[1] / "harness-mechanics"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

cli = importlib.import_module("harness_mechanics.cli")


def _mock_diagnostics(ir: TrajectoryIR) -> dict[str, Any]:
    """Mock diagnostic inspector conforming to frozen contract."""
    return {
        "stopping": {
            "mechanism": "stopping",
            "observations": [
                {
                    "code": "STOP_NORMAL",
                    "step_id": 1,
                    "tool_call_id": None,
                    "locator": "steps[0]",
                    "evidence_kind": "observed",
                    "summary": "Completed normally",
                }
            ],
            "unknowns": [],
        },
        "clipping": {
            "mechanism": "clipping",
            "observations": [],
            "unknowns": ["compaction_unobserved"],
        },
        "shell": {
            "mechanism": "shell",
            "observations": [],
            "unknowns": [],
        },
    }


def _mock_report(records: list[dict[str, Any]], evidence_kind: str) -> tuple[dict[str, Any], str]:
    """Mock report generator conforming to frozen contract."""
    source_count = len(records)
    analyzed_count = sum(1 for r in records if r.get("status") == "analyzed")
    unsupported_count = sum(1 for r in records if r.get("status") == "unsupported")
    error_count = sum(1 for r in records if r.get("status") == "error")

    report = {
        "schema_version": 1,
        "analysis_kind": "harness-mechanics-observational",
        "evidence_kind": evidence_kind,
        "records": records,
        "summary": {
            "source_count": source_count,
            "analyzed_count": analyzed_count,
            "unsupported_count": unsupported_count,
            "error_count": error_count,
            "observed_counts_by_mechanism": {"stopping": 1, "clipping": 0, "shell": 0},
            "hypothesis_counts_by_mechanism": {"stopping": 0, "clipping": 0, "shell": 0},
            "unknown_counts_by_mechanism": {"stopping": 0, "clipping": 1, "shell": 0},
        },
        "limitations": ["Observational study over retained trajectories."],
    }
    rendered = f"Summary: {source_count} sources ({analyzed_count} analyzed, {unsupported_count} unsupported, {error_count} errors)\n"
    return report, rendered


def _mock_ablation(report: dict[str, Any]) -> dict[str, Any]:
    """Mock ablation plan generator conforming to frozen contract."""
    return {
        "schema_version": 1,
        "artifact_kind": "ablation-proposal",
        "execution_authorized": False,
        "proposals": [],
        "limitations": ["Proposals require manual approval."],
    }


def _make_valid_trajectory_dict() -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-session-001",
        "agent": {
            "name": "test-agent",
            "version": "1.0",
            "model_name": "test-model-alpha",
            "extra": {"harness": "harbor-harness"},
        },
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "Hello world",
                "tool_calls": [],
                "observation": {"results": []},
            }
        ],
    }


def test_sha256_and_source_immutability(tmp_path: Path) -> None:
    """Source file bytes are hashed accurately and source files are not modified."""
    traj_file = tmp_path / "trajectory.json"
    content = json.dumps(_make_valid_trajectory_dict(), indent=2).encode("utf-8")
    traj_file.write_bytes(content)

    mtime_before = traj_file.stat().st_mtime_ns
    expected_sha256 = hashlib.sha256(content).hexdigest()

    record = cli.process_trajectory_source(
        raw_path=str(traj_file),
        resolved_path=traj_file.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )

    assert record["status"] == "analyzed"
    assert record["source"]["sha256"] == expected_sha256
    assert record["source"]["size_bytes"] == len(content)
    assert record["identity"]["session_id"] == "test-session-001"
    assert record["identity"]["model"] == "test-model-alpha"
    assert record["identity"]["harness"] == "harbor-harness"

    # Verify file was not modified
    assert traj_file.read_bytes() == content
    assert traj_file.stat().st_mtime_ns == mtime_before


def test_duplicate_resolved_paths_deduplication(tmp_path: Path) -> None:
    """Repeated paths resolving to the same file emit only one record."""
    traj_file = tmp_path / "trajectory.json"
    traj_file.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")

    out_dir = tmp_path / "output"
    args = cli.parse_args(
        [
            "--trajectory",
            str(traj_file),
            "--trajectory",
            str(traj_file),
            "--trajectory",
            "trajectory.json",
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir),
        ]
    )

    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )

    assert exit_code == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert len(report["records"]) == 1


def test_distinct_files_with_identical_content_not_conflated(tmp_path: Path) -> None:
    """Two different file paths with identical content produce distinct records."""
    data = json.dumps(_make_valid_trajectory_dict())
    dir1 = tmp_path / "run1"
    dir2 = tmp_path / "run2"
    dir1.mkdir()
    dir2.mkdir()

    f1 = dir1 / "trajectory.json"
    f2 = dir2 / "trajectory.json"
    f1.write_text(data, encoding="utf-8")
    f2.write_text(data, encoding="utf-8")

    out_dir = tmp_path / "output"
    args = cli.parse_args(
        [
            "--trajectory",
            str(f1),
            "--trajectory",
            str(f2),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir),
        ]
    )

    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )

    assert exit_code == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert len(report["records"]) == 2
    paths = {r["source"]["path"] for r in report["records"]}
    assert "run1/trajectory.json" in paths
    assert "run2/trajectory.json" in paths


def test_manifest_loading_and_resolution(tmp_path: Path) -> None:
    """Valid manifest with paths relative to repo root parses correctly."""
    traj_file = tmp_path / "sub" / "trajectory.json"
    traj_file.parent.mkdir()
    traj_file.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "evidence_kind": "fixture",
                "trajectories": ["sub/trajectory.json"],
            }
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "output"
    args = cli.parse_args(
        [
            "--manifest",
            str(manifest_file),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir),
        ]
    )

    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )

    assert exit_code == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["evidence_kind"] == "fixture"
    assert len(report["records"]) == 1
    assert report["records"][0]["source"]["path"] == "sub/trajectory.json"


def test_conflicting_evidence_kind_rejected(tmp_path: Path) -> None:
    """Conflicting evidence kinds between CLI and manifest return exit code 2."""
    # Case 1: manifest is fixture, CLI is historical
    manifest_file1 = tmp_path / "manifest1.json"
    manifest_file1.write_text(
        json.dumps(
            {
                "evidence_kind": "fixture",
                "trajectories": ["trajectory.json"],
            }
        ),
        encoding="utf-8",
    )
    out_dir1 = tmp_path / "out1"
    args1 = cli.parse_args(
        [
            "--manifest",
            str(manifest_file1),
            "--evidence-kind",
            "historical",
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir1),
        ]
    )
    assert (
        cli.run(
            args1,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert not out_dir1.exists()

    # Case 2: manifest is model-run, CLI is fixture
    manifest_file2 = tmp_path / "manifest2.json"
    manifest_file2.write_text(
        json.dumps(
            {
                "evidence_kind": "model-run",
                "trajectories": ["trajectory.json"],
            }
        ),
        encoding="utf-8",
    )
    out_dir2 = tmp_path / "out2"
    args2 = cli.parse_args(
        [
            "--manifest",
            str(manifest_file2),
            "--evidence-kind",
            "fixture",
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir2),
        ]
    )
    assert (
        cli.run(
            args2,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert not out_dir2.exists()

    # Case 3: manifest specifies unsupported/invalid evidence_kind
    manifest_file3 = tmp_path / "manifest3.json"
    manifest_file3.write_text(
        json.dumps(
            {
                "evidence_kind": "unsupported-provenance",
                "trajectories": ["trajectory.json"],
            }
        ),
        encoding="utf-8",
    )
    out_dir3 = tmp_path / "out3"
    args3 = cli.parse_args(
        [
            "--manifest",
            str(manifest_file3),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir3),
        ]
    )
    assert (
        cli.run(
            args3,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert not out_dir3.exists()


def test_mutually_supplied_source_modes_rejected() -> None:
    """Supplying both --trajectory and --manifest is rejected."""
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--trajectory",
                "traj.json",
                "--manifest",
                "manifest.json",
                "--repo-root",
                ".",
                "--output",
                "out",
            ]
        )


def test_missing_source_file_handled_safely(tmp_path: Path) -> None:
    """Nonexistent trajectory file produces error record and exit code 2 with report preserved."""
    out_dir = tmp_path / "output"
    args = cli.parse_args(
        [
            "--trajectory",
            "nonexistent.json",
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir),
        ]
    )

    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )

    assert exit_code == 2
    assert (out_dir / "report.json").is_file()
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert len(report["records"]) == 1
    rec = report["records"][0]
    assert rec["status"] == "error"
    assert rec["source"]["sha256"] is None
    assert rec["source"]["size_bytes"] is None
    assert rec["diagnostics"] == {}
    assert len(rec["warnings"]) > 0


def test_bounded_json_validation_corrupted_and_non_dict(tmp_path: Path) -> None:
    """Malformed JSON syntax and non-dict root produce error status with safe warnings."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{this is not json}", encoding="utf-8")

    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")

    rec1 = cli.process_trajectory_source(
        raw_path=str(bad_json),
        resolved_path=bad_json.resolve(),
        repo_root=tmp_path,
    )
    assert rec1["status"] == "error"
    assert len(rec1["warnings"]) > 0
    assert rec1["diagnostics"] == {}

    rec2 = cli.process_trajectory_source(
        raw_path=str(list_json),
        resolved_path=list_json.resolve(),
        repo_root=tmp_path,
    )
    assert rec2["status"] == "error"
    assert len(rec2["warnings"]) > 0
    assert rec2["diagnostics"] == {}


def test_control_runs_unsupported_not_agent_behavior(tmp_path: Path) -> None:
    """Oracle and nop control runs are marked unsupported and not analyzed."""
    # 1. Oracle agent
    oracle_traj = _make_valid_trajectory_dict()
    oracle_traj["agent"]["name"] = "oracle"
    f_oracle = tmp_path / "oracle.json"
    f_oracle.write_text(json.dumps(oracle_traj), encoding="utf-8")
    rec_oracle = cli.process_trajectory_source(
        raw_path=str(f_oracle),
        resolved_path=f_oracle.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec_oracle["status"] == "unsupported"
    assert rec_oracle["diagnostics"] == {}
    assert len(rec_oracle["warnings"]) > 0

    # 2. Nop agent
    nop_traj = _make_valid_trajectory_dict()
    nop_traj["agent"]["name"] = "nop"
    f_nop = tmp_path / "nop.json"
    f_nop.write_text(json.dumps(nop_traj), encoding="utf-8")
    rec_nop = cli.process_trajectory_source(
        raw_path=str(f_nop),
        resolved_path=f_nop.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec_nop["status"] == "unsupported"
    assert rec_nop["diagnostics"] == {}
    assert len(rec_nop["warnings"]) > 0

    # 3. Explicit control flag
    ctrl_traj = _make_valid_trajectory_dict()
    ctrl_traj["is_control"] = True
    f_ctrl = tmp_path / "ctrl.json"
    f_ctrl.write_text(json.dumps(ctrl_traj), encoding="utf-8")
    rec_ctrl = cli.process_trajectory_source(
        raw_path=str(f_ctrl),
        resolved_path=f_ctrl.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec_ctrl["status"] == "unsupported"
    assert rec_ctrl["diagnostics"] == {}
    assert len(rec_ctrl["warnings"]) > 0


def test_ordinary_agent_with_control_substring_analyzed_normally(tmp_path: Path) -> None:
    """Ordinary named agents containing 'nop' or 'oracle' as substrings are not wrongly rejected."""
    # Agent name containing 'nop' substring (e.g. canopy, sinopia, panoply)
    canopy_traj = _make_valid_trajectory_dict()
    canopy_traj["agent"]["name"] = "canopy-agent-v1"
    f_canopy = tmp_path / "canopy.json"
    f_canopy.write_text(json.dumps(canopy_traj), encoding="utf-8")
    rec_canopy = cli.process_trajectory_source(
        raw_path=str(f_canopy),
        resolved_path=f_canopy.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec_canopy["status"] == "analyzed"
    assert rec_canopy["warnings"] == []
    assert rec_canopy["identity"]["harness"] == "harbor-harness"

    sinopia_traj = _make_valid_trajectory_dict()
    sinopia_traj["agent"]["name"] = "sinopia"
    f_sinopia = tmp_path / "sinopia.json"
    f_sinopia.write_text(json.dumps(sinopia_traj), encoding="utf-8")
    rec_sinopia = cli.process_trajectory_source(
        raw_path=str(f_sinopia),
        resolved_path=f_sinopia.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec_sinopia["status"] == "analyzed"
    assert rec_sinopia["warnings"] == []


def test_non_trajectory_json_unsupported(tmp_path: Path) -> None:
    """Valid non-trajectory JSON (e.g. comparison artifact) is marked unsupported."""
    f = tmp_path / "comparison.json"
    f.write_text(json.dumps({"comparison_id": "c1", "mode": "causal"}), encoding="utf-8")

    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )

    assert rec["status"] == "unsupported"
    assert rec["diagnostics"] == {}
    assert len(rec["warnings"]) > 0


def test_malformed_steps_field_error(tmp_path: Path) -> None:
    """Malformed steps field (string or dict) is rejected as error before IR parsing."""
    bad_steps1 = _make_valid_trajectory_dict()
    bad_steps1["steps"] = "not-a-list"
    f1 = tmp_path / "bad_steps1.json"
    f1.write_text(json.dumps(bad_steps1), encoding="utf-8")
    rec1 = cli.process_trajectory_source(
        raw_path=str(f1),
        resolved_path=f1.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec1["status"] == "error"
    assert rec1["diagnostics"] == {}
    assert len(rec1["warnings"]) > 0

    bad_steps2 = _make_valid_trajectory_dict()
    bad_steps2["steps"] = {"step1": "invalid"}
    f2 = tmp_path / "bad_steps2.json"
    f2.write_text(json.dumps(bad_steps2), encoding="utf-8")
    rec2 = cli.process_trajectory_source(
        raw_path=str(f2),
        resolved_path=f2.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec2["status"] == "error"
    assert rec2["diagnostics"] == {}
    assert len(rec2["warnings"]) > 0


def test_malformed_step_item_not_an_object(tmp_path: Path) -> None:
    """Non-dict items in steps list are rejected as error before permissive IR conversion."""
    traj = _make_valid_trajectory_dict()
    traj["steps"] = [123, "string-step", None]
    f = tmp_path / "bad_step_items.json"
    f.write_text(json.dumps(traj), encoding="utf-8")
    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "error"
    assert rec["diagnostics"] == {}
    assert len(rec["warnings"]) > 0


def test_empty_step_dict_rejected_as_error(tmp_path: Path) -> None:
    """Empty dictionary in steps list must not silently become fabricated agent turn."""
    traj = _make_valid_trajectory_dict()
    traj["steps"] = [{}]
    f = tmp_path / "empty_step.json"
    f.write_text(json.dumps(traj), encoding="utf-8")
    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "error"
    assert rec["diagnostics"] == {}
    assert any("empty object" in w for w in rec["warnings"])


def test_flattened_observation_results_without_native_observation_rejected(tmp_path: Path) -> None:
    """Flattened normalized-IR observation_results input is rejected to preserve raw source pointers."""
    traj = {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-session-ir",
        "agent": {"name": "test-agent"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "hello",
                "observation_results": [{"source_call_id": "c1", "content": "out"}],
            }
        ],
    }
    f = tmp_path / "flattened_ir.json"
    f.write_text(json.dumps(traj), encoding="utf-8")
    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "error"
    assert rec["diagnostics"] == {}
    assert any("flattened 'observation_results'" in w for w in rec["warnings"])


def test_explicitly_empty_valid_atif_steps_remains_neutral(tmp_path: Path) -> None:
    """Explicitly empty valid steps list remains neutral and analyzes without fabrication."""
    traj = _make_valid_trajectory_dict()
    traj["steps"] = []
    f = tmp_path / "empty_steps.json"
    f.write_text(json.dumps(traj), encoding="utf-8")
    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "analyzed"
    assert rec["diagnostics"] != {}


def test_malformed_nested_tool_calls_shape_error(tmp_path: Path) -> None:
    """Malformed tool_calls shapes within steps are rejected before IR parsing."""
    # tool_calls is not a list
    traj1 = _make_valid_trajectory_dict()
    traj1["steps"][0]["tool_calls"] = "not-a-list"
    f1 = tmp_path / "bad_tc1.json"
    f1.write_text(json.dumps(traj1), encoding="utf-8")
    rec1 = cli.process_trajectory_source(
        raw_path=str(f1),
        resolved_path=f1.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec1["status"] == "error"
    assert rec1["diagnostics"] == {}
    assert len(rec1["warnings"]) > 0

    # tool call item is not a dict
    traj2 = _make_valid_trajectory_dict()
    traj2["steps"][0]["tool_calls"] = [123]
    f2 = tmp_path / "bad_tc2.json"
    f2.write_text(json.dumps(traj2), encoding="utf-8")
    rec2 = cli.process_trajectory_source(
        raw_path=str(f2),
        resolved_path=f2.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec2["status"] == "error"
    assert rec2["diagnostics"] == {}
    assert len(rec2["warnings"]) > 0

    # tool_call is not a dict
    traj3 = _make_valid_trajectory_dict()
    traj3["steps"][0]["tool_call"] = "not-a-dict"
    f3 = tmp_path / "bad_tc3.json"
    f3.write_text(json.dumps(traj3), encoding="utf-8")
    rec3 = cli.process_trajectory_source(
        raw_path=str(f3),
        resolved_path=f3.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec3["status"] == "error"
    assert rec3["diagnostics"] == {}
    assert len(rec3["warnings"]) > 0


def test_malformed_nested_observation_results_shape_error(tmp_path: Path) -> None:
    """Malformed observation structures within steps are rejected before IR parsing."""
    # observation_results is not a list
    traj1 = _make_valid_trajectory_dict()
    traj1["steps"][0]["observation_results"] = "not-a-list"
    f1 = tmp_path / "bad_obs1.json"
    f1.write_text(json.dumps(traj1), encoding="utf-8")
    rec1 = cli.process_trajectory_source(
        raw_path=str(f1),
        resolved_path=f1.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec1["status"] == "error"
    assert rec1["diagnostics"] == {}
    assert len(rec1["warnings"]) > 0

    # observation_results item is not a dict
    traj2 = _make_valid_trajectory_dict()
    traj2["steps"][0]["observation_results"] = ["not-a-dict"]
    f2 = tmp_path / "bad_obs2.json"
    f2.write_text(json.dumps(traj2), encoding="utf-8")
    rec2 = cli.process_trajectory_source(
        raw_path=str(f2),
        resolved_path=f2.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec2["status"] == "error"
    assert rec2["diagnostics"] == {}
    assert len(rec2["warnings"]) > 0

    # observation.results is not a list
    traj3 = _make_valid_trajectory_dict()
    traj3["steps"][0]["observation"] = {"results": "not-a-list"}
    f3 = tmp_path / "bad_obs3.json"
    f3.write_text(json.dumps(traj3), encoding="utf-8")
    rec3 = cli.process_trajectory_source(
        raw_path=str(f3),
        resolved_path=f3.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec3["status"] == "error"
    assert rec3["diagnostics"] == {}
    assert len(rec3["warnings"]) > 0

    # observation.results item is not a dict
    traj4 = _make_valid_trajectory_dict()
    traj4["steps"][0]["observation"] = {"results": ["not-a-dict"]}
    f4 = tmp_path / "bad_obs4.json"
    f4.write_text(json.dumps(traj4), encoding="utf-8")
    rec4 = cli.process_trajectory_source(
        raw_path=str(f4),
        resolved_path=f4.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec4["status"] == "error"
    assert rec4["diagnostics"] == {}
    assert len(rec4["warnings"]) > 0


def test_refuse_clobbering_existing_reports(tmp_path: Path) -> None:
    """Existing report.json, report.txt, or ablation-plan.json is never clobbered."""
    traj = tmp_path / "traj.json"
    traj.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")

    # 1. Existing report.json
    out_dir1 = tmp_path / "out1"
    out_dir1.mkdir()
    existing_report = out_dir1 / "report.json"
    existing_report.write_text("existing content json", encoding="utf-8")
    args1 = cli.parse_args(
        [
            "--trajectory",
            str(traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir1),
        ]
    )
    assert (
        cli.run(
            args1,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert existing_report.read_text(encoding="utf-8") == "existing content json"

    # 2. Existing report.txt
    out_dir2 = tmp_path / "out2"
    out_dir2.mkdir()
    existing_txt = out_dir2 / "report.txt"
    existing_txt.write_text("existing content txt", encoding="utf-8")
    args2 = cli.parse_args(
        [
            "--trajectory",
            str(traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir2),
        ]
    )
    assert (
        cli.run(
            args2,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert existing_txt.read_text(encoding="utf-8") == "existing content txt"

    # 3. Existing ablation-plan.json
    out_dir3 = tmp_path / "out3"
    out_dir3.mkdir()
    existing_abl = out_dir3 / "ablation-plan.json"
    existing_abl.write_text("existing content abl", encoding="utf-8")
    args3 = cli.parse_args(
        [
            "--trajectory",
            str(traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir3),
        ]
    )
    assert (
        cli.run(
            args3,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    assert existing_abl.read_text(encoding="utf-8") == "existing content abl"


def test_write_outputs_atomically_refuses_existing_file(tmp_path: Path) -> None:
    """Direct write_outputs_atomically call raises FileExistsError on existing target or symlink."""
    # Existing directory
    out_dir1 = tmp_path / "out_atomic_refusal"
    out_dir1.mkdir()
    (out_dir1 / "report.json").write_text("prior content", encoding="utf-8")

    with pytest.raises(FileExistsError):
        cli.write_outputs_atomically(
            output_dir=out_dir1,
            report={},
            report_text="text",
            ablation_plan={},
        )
    assert (out_dir1 / "report.json").read_text(encoding="utf-8") == "prior content"

    # Dangling symlink
    out_symlink = tmp_path / "dangling_out_symlink"
    out_symlink.symlink_to(tmp_path / "nonexistent_target")
    with pytest.raises(FileExistsError):
        cli.write_outputs_atomically(
            output_dir=out_symlink,
            report={},
            report_text="text",
            ablation_plan={},
        )


def test_relative_trajectory_symlink_escaping_repo_root_rejected(tmp_path: Path) -> None:
    """Relative trajectory path symlinked outside repo root is rejected without reading."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external_dir = tmp_path / "external"
    external_dir.mkdir()

    # External file
    secret_file = external_dir / "external_traj.json"
    secret_file.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")

    # Symlink inside repo_root pointing to external file
    symlink_in_repo = repo_root / "escaping_symlink.json"
    symlink_in_repo.symlink_to(secret_file)

    out_dir = tmp_path / "out_escape"
    args = cli.parse_args(
        [
            "--trajectory",
            "escaping_symlink.json",
            "--repo-root",
            str(repo_root),
            "--output",
            str(out_dir),
        ]
    )
    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )
    assert exit_code == 2
    assert not out_dir.exists()

    # Verify process_trajectory_source also rejects escaping relative path
    rec = cli.process_trajectory_source(
        raw_path="escaping_symlink.json",
        resolved_path=symlink_in_repo.resolve(),
        repo_root=repo_root,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "error"
    assert rec["diagnostics"] == {}
    assert len(rec["warnings"]) > 0


def test_relative_manifest_symlink_escaping_repo_root_rejected(tmp_path: Path) -> None:
    """Relative manifest path symlinked outside repo root is rejected without reading."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external_dir = tmp_path / "external"
    external_dir.mkdir()

    external_manifest = external_dir / "external_manifest.json"
    external_manifest.write_text(
        json.dumps({"evidence_kind": "fixture", "trajectories": []}),
        encoding="utf-8",
    )

    symlink_manifest = repo_root / "escaping_manifest.json"
    symlink_manifest.symlink_to(external_manifest)

    out_dir = tmp_path / "out_escape_manifest"
    args = cli.parse_args(
        [
            "--manifest",
            "escaping_manifest.json",
            "--repo-root",
            str(repo_root),
            "--output",
            str(out_dir),
        ]
    )
    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )
    assert exit_code == 2
    assert not out_dir.exists()


def test_non_trajectory_config_lacking_steps_and_schema_unsupported(tmp_path: Path) -> None:
    """Arbitrary JSON with an agent configuration but lacking steps and schema is unsupported."""
    agent_config = {
        "agent": {
            "name": "eval-model-agent",
            "version": "1.0",
            "model_name": "claude-3-opus",
        },
        "eval_parameters": {"timeout": 30, "retries": 3},
    }
    f = tmp_path / "agent_config.json"
    f.write_text(json.dumps(agent_config), encoding="utf-8")

    rec = cli.process_trajectory_source(
        raw_path=str(f),
        resolved_path=f.resolve(),
        repo_root=tmp_path,
        diagnostics_runner=_mock_diagnostics,
    )
    assert rec["status"] == "unsupported"
    assert rec["diagnostics"] == {}
    assert len(rec["warnings"]) > 0


def test_atomic_output_publishing(tmp_path: Path) -> None:
    """Outputs are written atomically in a new directory."""
    traj = tmp_path / "traj.json"
    traj.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")
    out_dir = tmp_path / "new_output"

    args = cli.parse_args(
        [
            "--trajectory",
            str(traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_dir),
        ]
    )

    exit_code = cli.run(
        args,
        diagnostics_runner=_mock_diagnostics,
        report_runner=_mock_report,
        ablation_runner=_mock_ablation,
    )

    assert exit_code == 0
    assert (out_dir / "report.json").is_file()
    assert (out_dir / "report.txt").is_file()
    assert (out_dir / "ablation-plan.json").is_file()

    # Verify JSON content is valid
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["analysis_kind"] == "harness-mechanics-observational"

    ablation = json.loads((out_dir / "ablation-plan.json").read_text(encoding="utf-8"))
    assert ablation["artifact_kind"] == "ablation-proposal"
    assert ablation["execution_authorized"] is False


def test_exit_codes_all_valid_vs_has_unsupported_or_error(tmp_path: Path) -> None:
    """Exit code is 0 when all records are analyzed, 2 when any is unsupported or error."""
    valid_traj = tmp_path / "valid.json"
    valid_traj.write_text(json.dumps(_make_valid_trajectory_dict()), encoding="utf-8")

    bad_traj = tmp_path / "bad.json"
    bad_traj.write_text("{bad", encoding="utf-8")

    out_valid = tmp_path / "out_valid"
    args_valid = cli.parse_args(
        [
            "--trajectory",
            str(valid_traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_valid),
        ]
    )
    assert (
        cli.run(
            args_valid,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 0
    )

    out_mixed = tmp_path / "out_mixed"
    args_mixed = cli.parse_args(
        [
            "--trajectory",
            str(valid_traj),
            "--trajectory",
            str(bad_traj),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(out_mixed),
        ]
    )
    assert (
        cli.run(
            args_mixed,
            diagnostics_runner=_mock_diagnostics,
            report_runner=_mock_report,
            ablation_runner=_mock_ablation,
        )
        == 2
    )
    # Reports preserved even on error
    assert (out_mixed / "report.json").is_file()
    assert (out_mixed / "report.txt").is_file()
    assert (out_mixed / "ablation-plan.json").is_file()
