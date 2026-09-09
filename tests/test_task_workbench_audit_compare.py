from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from evallab.task_workbench import compare_quality_audits, run_cli


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): _digest(p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def audit_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Retain a declared verifier change with independently checkable receipt bytes."""
    image = "sha256:" + _digest(b"image")
    task = tmp_path / "task-snapshot"
    _json(task / "task.toml", {})
    (task / "tests").mkdir()
    (task / "tests/test_state.py").write_bytes(b"candidate verifier")
    original = tmp_path / "original-tests/test_state.py"
    original.parent.mkdir()
    original.write_bytes(b"baseline verifier")
    _json(tmp_path / "task-manifest.json", _manifest(task))
    runner = tmp_path / "runner.py"
    runner.write_bytes(b"retained runner source")
    declaration = tmp_path / "declaration.json"
    _json(
        declaration,
        {
            "image_id": image,
            "runner_sha256": _digest(runner.read_bytes()),
            "upstream_verifier_sha256": _digest(original.read_bytes()),
            "repaired_verifier_sha256": _digest((task / "tests/test_state.py").read_bytes()),
            "arms": {
                "probe": {
                    "action_command": ["true"],
                    "expected_reward": "0",
                    "label": "reviewer annotation",
                }
            },
        },
    )
    for side, verifier, reward in (
        ("before", original, 1),
        ("after", task / "tests/test_state.py", 0),
    ):
        run = tmp_path / side
        _json(
            run / "run_meta.json",
            {
                "run_id": side,
                "meta": {
                    "task_dir": str(task),
                    "image": image,
                    "verifier_sha256": _digest(verifier.read_bytes()),
                },
            },
        )
        _json(run / "summary.json", {"arms": {"probe": {"reward": reward}}})
        arm = run / "probe"
        _json(
            arm / "result.json",
            {"arm": "probe", "reward": str(reward), "action_exit": 0, "verifier_exit": 0},
        )
        _json(arm / "action-inspect.json", [{"Path": "true", "Args": [], "Image": image}])
        _json(arm / "task_file/input/data.json", {"value": 2})
        _json(arm / "task_file/result.json", {"value": 3})
        _json(arm / "task-file-manifest.json", _manifest(arm / "task_file"))
    return tmp_path / "before", tmp_path / "after", declaration


def _compare(pair: tuple[Path, Path, Path]) -> dict[str, Any]:
    before, after, declaration = pair
    return compare_quality_audits(before, after, declaration_path=declaration)


def test_declared_axis_reports_neutral_delta(audit_pair: tuple[Path, Path, Path]) -> None:
    report = _compare(audit_pair)
    row = report["arms"]["probe"]
    assert report["experiment_axis"]["status"] == "declared_verifier_change"
    assert row["pairing"] == {"status": "matched", "reasons": []}
    assert row["observed_reward_delta"] == -1
    assert row["declaration"]["expected_reward"] == "0"
    assert row["before"]["observed_reward"] == 1
    assert row["after"]["observed_reward"] == 0


def test_no_output_is_known_absence_not_missing_evidence(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    for run in audit_pair[:2]:
        (run / "probe/task_file/result.json").unlink()
        _json(run / "probe/task-file-manifest.json", _manifest(run / "probe/task_file"))
        _json(
            run / "probe/result.json",
            {"arm": "probe", "reward": "0", "action_exit": 0, "verifier_exit": 0},
        )
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["output_identity"]["status"] == "same"
    assert row["pairing"]["status"] == "matched"
    assert row["observed_reward_delta"] == 0


def test_missing_verifier_identity_cannot_borrow_shared_snapshot(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    before = audit_pair[0]
    meta = json.loads((before / "run_meta.json").read_text())
    del meta["meta"]["verifier_sha256"]
    _json(before / "run_meta.json", meta)
    report = _compare(audit_pair)
    assert report["before"]["provenance_binding"]["package_snapshot_status"] == "verified"
    assert report["before"]["provenance_binding"]["executed_verifier_status"] == "unbound"
    assert report["arms"]["probe"]["pairing"]["status"] == "unqualified"
    assert "before_executed_verifier_unbound" in report["arms"]["probe"]["pairing"]["reasons"]


def test_changed_runtime_input_is_not_a_matched_verifier_experiment(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    arm = audit_pair[1] / "probe"
    _json(arm / "task_file/input/data.json", {"value": 99})
    _json(arm / "task-file-manifest.json", _manifest(arm / "task_file"))
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["status"] == "changed"
    assert row["input_identity"]["changed_paths"] == ["input/data.json"]
    assert row["pairing"]["status"] == "unqualified"
    assert "runtime_input_changed" in row["pairing"]["reasons"]
    assert row["observed_reward_delta"] == -1  # Measurement remains visible, but qualified.


@pytest.mark.parametrize("corruption", ["tamper", "omit", "extra", "escape"])
def test_runtime_manifest_must_cover_exact_retained_bytes(
    audit_pair: tuple[Path, Path, Path], corruption: str
) -> None:
    arm = audit_pair[1] / "probe"
    manifest = json.loads((arm / "task-file-manifest.json").read_text())
    if corruption == "tamper":
        _json(arm / "task_file/input/data.json", {"value": 99})
    elif corruption == "omit":
        del manifest["input/data.json"]
    elif corruption == "extra":
        _json(arm / "task_file/input/undeclared.json", {})
    else:
        manifest["../result.json"] = _digest((arm / "result.json").read_bytes())
    _json(arm / "task-file-manifest.json", manifest)
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["status"] == "mismatched"
    assert row["pairing"]["status"] == "unqualified"


def test_missing_manifest_does_not_fall_back_to_package(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    (audit_pair[1] / "probe/task-file-manifest.json").unlink()
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["status"] == "unbound"
    assert row["pairing"]["status"] == "unqualified"


def test_missing_and_declared_only_arms_preserve_coverage(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    before, after, declaration = audit_pair
    shutil.rmtree(after / "probe")
    _json(after / "summary.json", {"arms": {}})
    declared = json.loads(declaration.read_text())
    declared["arms"]["never_run"] = {"expected_reward": "1"}
    _json(declaration, declared)
    arms = _compare(audit_pair)["arms"]
    assert set(arms) == {"probe", "never_run"}
    assert arms["probe"]["before"]["execution_status"] == "completed"
    assert arms["probe"]["after"] is None
    assert arms["never_run"]["before"] is None
    assert arms["never_run"]["after"] is None
    assert all(
        row["observed_reward_delta"] is None and row["pairing"]["status"] == "unqualified"
        for row in arms.values()
    )


def test_summary_reward_without_execution_does_not_create_delta(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    (audit_pair[1] / "probe/result.json").unlink()
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["after"]["execution_status"] == "missing"
    assert row["after"]["observed_reward"] == 0
    assert row["observed_reward_delta"] is None


def test_undeclared_verifier_change_is_unqualified(audit_pair: tuple[Path, Path, Path]) -> None:
    row = compare_quality_audits(*audit_pair[:2])["arms"]["probe"]
    assert "undeclared_verifier_change" in row["pairing"]["reasons"]
    assert row["pairing"]["status"] == "unqualified"


def test_cli_unqualified_is_inspection_success(
    audit_pair: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["audit-compare", *map(str, audit_pair[:2])]
    assert run_cli([*args, "--format", "text"]) == 0
    assert "probe" in capsys.readouterr().out
    assert run_cli([*args, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["arms"]["probe"]["pairing"]["status"] == "unqualified"
    assert report["arms"]["probe"]["observed_reward_delta"] == -1


def test_wrong_recorded_arm_does_not_pair_by_directory(audit_pair: tuple[Path, Path, Path]) -> None:
    _json(
        audit_pair[1] / "probe/result.json",
        {"arm": "different_probe", "reward": "0", "action_exit": 0, "verifier_exit": 0},
    )
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["pairing"]["status"] == "unqualified"
    assert "after_arm_identity_unbound" in row["pairing"]["reasons"]


def test_nonfinite_reward_cannot_emit_invalid_json(audit_pair: tuple[Path, Path, Path]) -> None:
    _json(
        audit_pair[1] / "probe/result.json",
        {"arm": "probe", "reward": "NaN", "action_exit": 0, "verifier_exit": 0},
    )
    report = _compare(audit_pair)
    assert report["arms"]["probe"]["observed_reward_delta"] is None
    assert report["arms"]["probe"]["after"]["observed_reward"] is None
    json.dumps(report, allow_nan=False)


def test_missing_execution_does_not_claim_action_or_file_changes(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    shutil.rmtree(audit_pair[1] / "probe")
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["changed_paths"] == []
    assert row["output_identity"]["changed_paths"] == []
    assert "action_identity_changed" not in row["pairing"]["reasons"]
    assert "declared_action_mismatch" not in row["pairing"]["reasons"]
    assert row["after"]["execution_status"] == "missing"


def test_complete_output_only_manifest_retains_known_empty_inputs(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    for run in audit_pair[:2]:
        shutil.rmtree(run / "probe/task_file/input")
        _json(run / "probe/task-file-manifest.json", _manifest(run / "probe/task_file"))
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["status"] == "same"
    assert row["output_identity"]["status"] == "same"
    assert row["pairing"]["status"] == "matched"


def test_runtime_root_symlink_cannot_borrow_other_run_bytes(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    before, after, _ = audit_pair
    shutil.rmtree(after / "probe/task_file")
    (after / "probe/task_file").symlink_to(before / "probe/task_file", target_is_directory=True)
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["input_identity"]["status"] == "mismatched"
    assert row["pairing"]["status"] == "unqualified"


def test_null_docker_args_are_an_empty_argument_vector(audit_pair: tuple[Path, Path, Path]) -> None:
    path = audit_pair[1] / "probe/action-inspect.json"
    inspection = json.loads(path.read_text())
    inspection[0]["Args"] = None
    _json(path, inspection)
    assert _compare(audit_pair)["arms"]["probe"]["pairing"]["status"] == "matched"


def test_changed_image_keeps_each_side_binding_distinct(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    after = audit_pair[1]
    image = "sha256:" + _digest(b"different image")
    meta = json.loads((after / "run_meta.json").read_text())
    meta["meta"]["image"] = image
    _json(after / "run_meta.json", meta)
    path = after / "probe/action-inspect.json"
    inspection = json.loads(path.read_text())
    inspection[0]["Image"] = image
    _json(path, inspection)
    row = _compare(audit_pair)["arms"]["probe"]
    assert "image_identity_changed" in row["pairing"]["reasons"]
    assert "after_action_image_unbound" not in row["pairing"]["reasons"]
    assert "after_action_image_mismatched" not in row["pairing"]["reasons"]


def test_verifier_corruption_is_mismatched_not_absent(audit_pair: tuple[Path, Path, Path]) -> None:
    (audit_pair[0].parent / "original-tests/test_state.py").write_bytes(b"tampered verifier")
    report = _compare(audit_pair)
    assert report["before"]["provenance_binding"]["executed_verifier_status"] == "mismatched"
    reasons = report["arms"]["probe"]["pairing"]["reasons"]
    assert "before_executed_verifier_mismatched" in reasons
    assert "before_executed_verifier_unbound" not in reasons


def test_explicit_declaration_cannot_omit_axis_identity(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    before, after, declaration = audit_pair
    meta = json.loads((after / "run_meta.json").read_text())
    baseline = json.loads((before / "run_meta.json").read_text())
    meta["meta"]["verifier_sha256"] = baseline["meta"]["verifier_sha256"]
    _json(after / "run_meta.json", meta)
    declared = json.loads(declaration.read_text())
    del declared["upstream_verifier_sha256"], declared["repaired_verifier_sha256"]
    _json(declaration, declared)
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["pairing"]["status"] == "unqualified"
    assert "declared_verifier_identity_unbound" in row["pairing"]["reasons"]


def test_one_sided_archives_cannot_silently_downgrade_to_byte_only(
    audit_pair: tuple[Path, Path, Path],
) -> None:
    arm = audit_pair[0] / "probe"
    files = {
        path.relative_to(arm / "task_file").as_posix(): path.read_bytes()
        for path in (arm / "task_file").rglob("*")
        if path.is_file()
    }
    manifest = {
        name: {
            "sha256": _digest(data),
            "size": len(data),
            "uid": 1000,
            "gid": 1000,
            "mode": "0o644",
        }
        for name, data in files.items()
    }
    for kind in ("action", "verifier"):
        archive_path = arm / f"{kind}-task-file.tar"
        with tarfile.open(archive_path, "w") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(f"./{name}")
                member.size, member.uid, member.gid, member.mode = len(data), 1000, 1000, 0o644
                archive.addfile(member, io.BytesIO(data))
        _json(arm / f"{kind}-task-file.manifest.json", manifest)
        _json(
            arm / f"{kind}-task-file.receipt.json",
            {"exit": 0, "archive_sha256": _digest(archive_path.read_bytes())},
        )
    row = _compare(audit_pair)["arms"]["probe"]
    assert row["transport_identity"]["before"]["status"] == "verified"
    assert row["pairing"]["status"] == "unqualified"
    assert "after_transport_unbound" in row["pairing"]["reasons"]
    assert row["observed_reward_delta"] == -1
