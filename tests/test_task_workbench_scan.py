from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evallab.task_workbench import CandidateSource, UnsafePathError, run_cli, scan_candidates

FIXTURE = Path(__file__).parent / "fixtures/task_workbench/valid"
SOURCE = CandidateSource("local/scan-fixture", "local/scan-fixture@1.0.0", "MIT")


def _candidate(root: Path, name: str) -> Path:
    destination = root / name
    shutil.copytree(FIXTURE, destination)
    return destination


def _scan(root: Path) -> dict:
    return scan_candidates(repo_root=root, task_root=root, source=SOURCE)


def _args(root: Path) -> list[str]:
    return [
        "scan",
        str(root),
        "--repo-root",
        str(root),
        "--source-uri",
        SOURCE.source_uri,
        "--source-ref",
        SOURCE.source_ref,
        "--license",
        SOURCE.license,
    ]


def _format_args(root: Path, fmt: str) -> list[str]:
    return _args(root) + ["--format", fmt]


def test_scan_preserves_valid_siblings_and_distinguishes_inspection_errors(tmp_path: Path) -> None:
    valid = _candidate(tmp_path, "a-valid")
    refused = _candidate(tmp_path, "b-refused")
    malformed = _candidate(tmp_path, "c-unreadable")
    (refused / "tests/Dockerfile").unlink()
    (refused / "solution/solve.sh").unlink()
    (malformed / "instruction.md").write_bytes(b"\xffPRIVATE-CANDIDATE-BYTES")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    report = _scan(tmp_path)
    rows = {row["task_path"]: row for row in report["tasks"]}
    assert {name: row["status"] for name, row in rows.items()} == {
        "a-valid": "static_passed",
        "b-refused": "static_failed",
        "c-unreadable": "inspection_error",
    }
    assert rows["a-valid"]["package_digest"].startswith("sha256:")
    assert rows["c-unreadable"]["error_type"] == "UnicodeDecodeError"
    assert "PRIVATE-CANDIDATE-BYTES" not in json.dumps(report)
    assert report["summary"]["discovered"] == 3
    # Count affected tasks, not multiple findings of the same type in one task.
    assert report["summary"]["tasks_by_diagnostic"]["required_file_missing"] == 1
    assert report["controls_executed"] is False
    assert "verifier_alignment" in report["not_assessed"]
    assert "training_utility" in report["not_assessed"]
    assert _scan(tmp_path) == report
    assert before == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert valid.is_dir()


def test_scan_refuses_candidate_symlinks_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "collection"
    linked = _candidate(root, "a-linked")
    _candidate(root, "b-valid")
    outside = tmp_path / "private.txt"
    outside.write_text("PRIVATE-OUTSIDE-TARGET")
    (linked / "instruction.md").unlink()
    (linked / "instruction.md").symlink_to(outside)
    read_text = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        if path.resolve() == outside:
            raise AssertionError("must not dereference candidate symlink")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    report = _scan(root)
    assert report["tasks"][0]["error_type"] == "UnsafePathError"
    assert report["tasks"][1]["status"] == "static_passed"
    assert "PRIVATE-OUTSIDE-TARGET" not in json.dumps(report)

    with pytest.raises(UnsafePathError):
        scan_candidates(repo_root=root, task_root=tmp_path, source=SOURCE)


def test_scan_cli_reports_no_candidates_without_claiming_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(_args(tmp_path)) == 2
    empty = json.loads(capsys.readouterr().out)
    assert empty["summary"]["discovered"] == 0
    _candidate(tmp_path, "valid")
    assert run_cli(_args(tmp_path)) == 0
    passed = json.loads(capsys.readouterr().out)
    assert passed["tasks"][0]["status"] == "static_passed"
    refused = _candidate(tmp_path, "refused")
    (refused / "task.toml").write_text("[task\n")
    assert run_cli(_args(tmp_path)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["discovered"] == 2
    assert report["summary"]["static_failed"] == 1
    assert report["summary"]["static_passed"] == 1
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "research").exists()


def test_scan_accepts_single_task_root_and_rejects_missing_root(tmp_path: Path) -> None:
    task = _candidate(tmp_path, "single")
    report = scan_candidates(repo_root=tmp_path, task_root=task, source=SOURCE)
    assert [row["task_path"] for row in report["tasks"]] == ["single"]
    with pytest.raises(RuntimeError, match="candidate root is missing"):
        scan_candidates(repo_root=tmp_path, task_root=tmp_path / "missing", source=SOURCE)


def test_scan_cli_text_preserves_severity_on_static_passed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A static pass with warning diagnostics must not hide their severity."""
    import evallab.task_workbench as tw

    _candidate(tmp_path, "warned")
    real_inspect = tw.inspect_candidate

    def inspect_with_warning(*args, **kwargs) -> tw.Inspection:
        inspection = real_inspect(*args, **kwargs)
        warning = tw.Diagnostic(
            severity="warning",
            code="review_advisory",
            classification="task_defect",
            path="instruction.md",
            message="Retain this advice",
        )
        return tw.Inspection(
            candidate=inspection.candidate,
            diagnostics=(warning,) + inspection.diagnostics,
            control_plan=inspection.control_plan,
        )

    monkeypatch.setattr(tw, "inspect_candidate", inspect_with_warning)

    assert run_cli(_format_args(tmp_path, "text")) == 0
    text = capsys.readouterr().out
    assert "warned: static_passed" in text
    assert "[warning] review_advisory (task_defect; instruction.md): Retain this advice" in text
    assert "review_advisory: 1" in text
    assert "static_passed does not mean clean or certified" in text

    assert run_cli(_format_args(tmp_path, "json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["status"] == "static_passed"
    assert payload["tasks"][0]["diagnostics"][0]["severity"] == "warning"


def test_scan_cli_text_reports_failed_candidate_identity_and_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failed candidates keep provenance and diagnostics in text output."""
    refused = _candidate(tmp_path, "b-refused")
    (refused / "tests/Dockerfile").unlink()
    (refused / "solution/solve.sh").unlink()

    assert run_cli(_format_args(tmp_path, "text")) == 1
    text = capsys.readouterr().out
    assert "b-refused: static_failed" in text
    assert text.count("[error] required_file_missing") == 2
    assert "candidate_id: candidate-" in text
    assert "package: sha256:" in text

    assert run_cli(_format_args(tmp_path, "json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["status"] == "static_failed"
    assert len(payload["tasks"][0]["diagnostics"]) == 2


def test_audit_evidence_consumes_synthetic_and_binds_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify audit-evidence loads runs, computes provenance binding, and reports arms."""
    import hashlib

    from evallab.task_workbench import load_quality_audit_evidence, run_cli

    task_dir = tmp_path / "mock-task"
    task_dir.mkdir()
    test_file = task_dir / "test.py"
    test_file.write_text("print('hello')\n")
    test_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

    audit_dir = tmp_path / "mock-audit"
    audit_dir.mkdir()
    (audit_dir / "run_meta.json").write_text(
        json.dumps({
            "run_id": "test-run-123",
            "meta": {
                "task_dir": str(task_dir),
                "image": "test-image:latest",
            },
        })
    )
    (audit_dir / "audit-rollup.json").write_text(
        json.dumps({
            "hashes": {
                "mock-task": {
                    "test.py": test_sha,
                }
            }
        })
    )
    (audit_dir / "summary.json").write_text(
        json.dumps({
            "run_id": "test-run-123",
            "findings": {
                "status": "pass",
                "per_arm": {
                    "oracle": {
                        "label": "positive reference",
                        "expected_reward": "1.0",
                    }
                },
            },
            "arms": {
                "oracle": {
                    "reward": "1.0",
                    "ctrf_summary": {"passed": 5, "tests": 5},
                }
            },
        })
    )
    oracle_arm = audit_dir / "oracle"
    oracle_arm.mkdir()
    (oracle_arm / "result.json").write_text(
        json.dumps({
            "reward": "1.0",
            "action_exit": 0,
            "verifier_exit": 0,
            "ctrf_summary": {"passed": 5, "tests": 5},
        })
    )

    evidence = load_quality_audit_evidence(audit_dir)
    assert evidence["run_id"] == "test-run-123"
    assert evidence["provenance_binding"]["status"] == "verified"
    assert evidence["provenance_binding"]["matched_files"] == 1
    assert evidence["arms"]["oracle"]["observed_reward"] == 1.0
    assert evidence["arms"]["oracle"]["execution_status"] == "completed"

    # Test text formatting via CLI
    exit_code = run_cli(["audit-evidence", str(audit_dir), "--format", "text"])
    assert exit_code == 0
    text = capsys.readouterr().out
    assert "Quality audit evidence:" in text
    assert "Run ID: test-run-123" in text
    assert "Provenance binding: verified (1 files matched)" in text
    assert "- oracle: status=completed, reward=1.0 (tests: 5/5)" in text
    assert "Declared label: positive reference (expected: 1.0)" in text

    # Changed bytes must invalidate provenance binding
    test_file.write_text("print('tampered')\n")
    tampered = load_quality_audit_evidence(audit_dir)
    assert tampered["provenance_binding"]["status"] == "mismatched"
    assert "test.py" in tampered["provenance_binding"]["mismatches"]


def test_audit_evidence_exercises_real_pipeline_record_and_dashboard_artifacts(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify real lane quality-audit directories parse correctly and preserve boundaries."""
    from evallab.task_workbench import load_quality_audit_evidence, run_cli

    p_audit = Path("/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/pipeline-record-quality-audit")
    d_audit = Path("/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/facet-key-order-probe-run-audited-2")

    if not p_audit.exists() or not d_audit.exists():
        pytest.skip("real quality-audit artifacts not available on host")

    # Pipeline record audit
    p_ev = load_quality_audit_evidence(p_audit)
    assert p_ev["run_id"] == "facet-probe-5299bacf-ed3"
    assert p_ev["provenance_binding"]["status"] == "verified"
    assert set(p_ev["arms"].keys()) == {"drop_event", "empty_values", "nop", "oracle", "reordered_json"}
    # empty_values has observed reward 1.0 even though declared expected is 0.0
    assert p_ev["arms"]["empty_values"]["observed_reward"] == 1.0
    assert p_ev["arms"]["empty_values"]["declared_expected_reward"] == 0.0
    assert p_ev["arms"]["reordered_json"]["observed_reward"] == 1.0

    # Dashboard audit
    d_ev = load_quality_audit_evidence(d_audit)
    assert d_ev["run_id"] == "facet-probe-46b10b32-d10"
    assert d_ev["provenance_binding"]["status"] == "verified"
    assert set(d_ev["arms"].keys()) == {"nop", "oracle", "reordered_json", "wrong_value"}
    # dashboard reordered_json has observed reward 0.0
    assert d_ev["arms"]["reordered_json"]["observed_reward"] == 0.0

    # CLI text rendering on real artifacts
    assert run_cli(["audit-evidence", str(p_audit), "--format", "text"]) == 0
    p_text = capsys.readouterr().out
    assert "empty_values: status=completed, reward=1.0" in p_text
    assert "reordered_json: status=completed, reward=1.0" in p_text

    assert run_cli(["audit-evidence", str(d_audit), "--format", "text"]) == 0
    d_text = capsys.readouterr().out
    assert "reordered_json: status=completed, reward=0.0" in d_text
