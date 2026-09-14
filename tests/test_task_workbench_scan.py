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
