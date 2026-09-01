from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import evallab.storage.data_backfill as data_backfill
from evallab.cli import run_cli
from evallab.storage.data_backfill import (
    DESIGN_CELL_BINDING_ABSENT,
    EVENT_JOURNAL_ABSENT,
    FAMILY_BINDING_ABSENT,
    FINAL_STATE_ABSENT,
    HISTORICAL_CONTRACT_FILENAME,
    NETWORK_ISOLATION_EVIDENCE_ABSENT,
    OPPORTUNITY_COUNTS_UNBOUND,
    TASK_REGISTRY_REVISION_UNBOUND,
    VERIFIER_TRUTH_DIGEST_ABSENT,
    HistoricalRegenerationConflict,
    HistoricalRegenerationCountMismatch,
    run_historical_contract_regeneration,
)
from evallab.storage.historical_git_snapshot import (
    capture_historical_source_snapshot,
)

DIGEST = "sha256:" + "a" * 64


def _promoted_trial(
    runs_root: Path,
    *,
    locator: str = "job/action-memory-v1-indexed-4k-semantic_distractor-s2026__trial",
    truth: bool = True,
    events: bool = True,
    final_state: bool = True,
    task_name: str = "action-memory-v1-indexed-4k-semantic_distractor-s2026",
    task_path: str = "/Users/example/tmp/evallab-zai-action-memory-v1-s2026",
) -> Path:
    trial = runs_root / locator
    (trial / "artifacts/app/output").mkdir(parents=True, exist_ok=True)
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": task_name,
                "trial_name": task_name,
                "task_name": task_name,
                "task_id": {"path": task_path},
            }
        ),
        encoding="utf-8",
    )
    (trial / "config.json").write_text(
        json.dumps({"task": {"path": task_path}}),
        encoding="utf-8",
    )
    (trial / "lock.json").write_text(
        json.dumps(
            {
                "task": {
                    "name": task_name,
                    "version": "9.9.9-in-name-only",
                    "path": task_path,
                    "digest": DIGEST,
                }
            }
        ),
        encoding="utf-8",
    )
    verifier = {"reward": 1.0}
    if truth:
        verifier["truth_digest"] = DIGEST
    (trial / "verifier/result.json").write_text(json.dumps(verifier), encoding="utf-8")
    manifest = [
        {
            "source": "/app/output/benchmark-events.jsonl",
            "destination": "artifacts/app/output/benchmark-events.jsonl",
            "type": "file",
            "status": "ok" if events else "missing",
            "service": None,
        },
        {
            "source": "/app/output/final-state.json",
            "destination": "artifacts/app/output/final-state.json",
            "type": "file",
            "status": "ok" if final_state else "missing",
            "service": None,
        },
    ]
    (trial / "artifacts/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if events:
        (trial / "artifacts/app/output/benchmark-events.jsonl").write_text(
            '{"event_type":"complete"}\n',
            encoding="utf-8",
        )
    if final_state:
        (trial / "artifacts/app/output/final-state.json").write_text(
            json.dumps({"final_digest": DIGEST}),
            encoding="utf-8",
        )
    return trial


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_revision(tmp_path: Path) -> str:
    if not (tmp_path / ".git").exists():
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.name", "Historical Test")
        _git(tmp_path, "config", "user.email", "historical@example.test")
        _git(tmp_path, "add", "--", "runs")
        _git(tmp_path, "commit", "-qm", "historical source")
    return _git(tmp_path, "rev-parse", "HEAD")


def _reviewed_digests(tmp_path: Path, revision: str) -> tuple[str, str]:
    capture = capture_historical_source_snapshot(
        repo_root=tmp_path,
        runs_root=Path("runs"),
        source_revision=revision,
    )
    plan = data_backfill._plan_historical_contract_regeneration(
        capture,
        destination_runs_root=tmp_path / "runs",
    )
    return capture.snapshot.snapshot_digest, plan.manifest.content_digest


def _run(
    tmp_path: Path,
    *,
    promoted: int = 1,
    derivable: int = 1,
    apply: bool = False,
    source_revision: str | None = None,
):
    revision = source_revision or _source_revision(tmp_path)
    expected_snapshot = None
    expected_plan = None
    if apply:
        expected_snapshot, expected_plan = _reviewed_digests(tmp_path, revision)
    return run_historical_contract_regeneration(
        repo_root=tmp_path,
        runs_root=Path("runs"),
        source_revision=revision,
        manifest_out=tmp_path / "historical-regeneration.json",
        expect_promoted=promoted,
        expect_derivable=derivable,
        expect_source_snapshot=expected_snapshot,
        expect_plan_digest=expected_plan,
        apply=apply,
    )


def _applied_contract(tmp_path: Path, trial: Path) -> dict:
    _run(tmp_path, apply=True)
    return json.loads((trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME).read_text())


def test_family_is_never_inferred_from_task_name(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    contract = _applied_contract(tmp_path, trial)
    assert contract["family"] is None
    assert contract["construct"] is None
    assert FAMILY_BINDING_ABSENT in contract["hold_reasons"]


def test_seed_is_never_parsed_from_a_name(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs", task_name="benchmark-s2026")
    contract = _applied_contract(tmp_path, trial)
    assert contract["seed"] is None
    assert contract["cell_id"] is None
    assert DESIGN_CELL_BINDING_ABSENT in contract["hold_reasons"]


def test_arm_dose_and_representation_are_never_substring_matched(tmp_path: Path) -> None:
    trial = _promoted_trial(
        tmp_path / "runs",
        task_name="indexed-4k-semantic_distractor",
    )
    contract = _applied_contract(tmp_path, trial)
    assert contract["arm"] is None
    assert contract["dose"] is None
    assert contract["representation"] is None
    assert contract["representation_order"] is None


def test_opportunity_counts_are_never_defaulted(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    contract = _applied_contract(tmp_path, trial)
    assert contract["opportunity_counts"] is None
    assert OPPORTUNITY_COUNTS_UNBOUND in contract["hold_reasons"]


def test_task_identity_never_falls_back_to_name_path_or_trial_id(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs", task_name="authoritative-looking-task-id")
    contract = _applied_contract(tmp_path, trial)
    assert contract["task_id"] is None
    assert contract["registry_revision_digest"] is None
    assert contract["task_content_digest"] == DIGEST
    assert TASK_REGISTRY_REVISION_UNBOUND in contract["hold_reasons"]


def test_path_markers_never_establish_platform_or_isolation(tmp_path: Path) -> None:
    trial = _promoted_trial(
        tmp_path / "runs",
        task_path="/tmp/evallab-zai-darwin-network-disabled/action-memory-v1",
    )
    contract = _applied_contract(tmp_path, trial)
    assert contract["runtime_platform"] is None
    assert contract["network_isolation_status"] == "unavailable"
    assert NETWORK_ISOLATION_EVIDENCE_ABSENT in contract["hold_reasons"]


def test_missing_final_state_is_typed_hold_before_loadability(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs", final_state=False)
    result = _run(tmp_path)
    row = result.manifest.dispositions[0]
    assert row.classification == "descriptive-incomplete"
    assert FINAL_STATE_ABSENT in row.hold_reasons
    assert result.manifest.truth_missing_final_state_count == 1
    contract = _applied_contract(tmp_path, trial)
    assert contract["source_complete"] is False
    assert contract["loadable"] is False


def test_missing_truth_and_events_emit_complete_reason_sets(tmp_path: Path) -> None:
    _promoted_trial(
        tmp_path / "runs",
        locator="job/no-truth-no-events",
        truth=False,
        events=False,
        final_state=False,
    )
    _promoted_trial(
        tmp_path / "runs",
        locator="job/no-truth-with-events",
        truth=False,
        events=True,
        final_state=False,
    )
    result = _run(tmp_path, promoted=2, derivable=0)
    rows = {row.trial_locator: row for row in result.manifest.dispositions}
    absent = rows["job/no-truth-no-events"]
    assert VERIFIER_TRUTH_DIGEST_ABSENT in absent.hold_reasons
    assert EVENT_JOURNAL_ABSENT in absent.hold_reasons
    assert FINAL_STATE_ABSENT in absent.hold_reasons
    present = rows["job/no-truth-with-events"]
    assert VERIFIER_TRUTH_DIGEST_ABSENT in present.hold_reasons
    assert EVENT_JOURNAL_ABSENT not in present.hold_reasons
    assert result.manifest.truth_missing_count == 2
    assert result.manifest.truth_missing_events_count == 1


def test_unexpected_existing_output_preserves_original_bytes(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    original = b"operator-owned-conflict\n"
    output.write_bytes(original)
    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)
    assert output.read_bytes() == original
    assert not (tmp_path / "historical-regeneration.json").exists()


def test_second_apply_is_verify_only_and_idempotent(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    first = _run(tmp_path, apply=True)
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    first_output = output.read_bytes()
    first_manifest = (tmp_path / "historical-regeneration.json").read_bytes()
    second = _run(tmp_path, apply=True)
    assert second.created_output_count == 0
    assert second.verified_output_count == 1
    assert second.manifest_bytes == first.manifest_bytes
    assert output.read_bytes() == first_output
    assert (tmp_path / "historical-regeneration.json").read_bytes() == first_manifest


def test_dry_run_predicts_apply_disposition_bytes_exactly(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    dry_run = _run(tmp_path)
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    assert not output.exists()
    dry_manifest = (tmp_path / "historical-regeneration.json").read_bytes()
    applied = _run(tmp_path, apply=True)
    assert applied.manifest_bytes == dry_run.manifest_bytes
    assert (tmp_path / "historical-regeneration.json").read_bytes() == dry_manifest
    expected = applied.manifest.outputs[0]
    assert expected.path.endswith(HISTORICAL_CONTRACT_FILENAME)
    assert expected.digest == "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()


def test_representation_order_drift_refuses_instead_of_rederiving(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    _run(tmp_path, apply=True)
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    drifted = json.loads(output.read_text())
    drifted["representation_order"] = ["semantic", "indexed"]
    drifted_bytes = json.dumps(drifted, sort_keys=True).encode("utf-8")
    output.write_bytes(drifted_bytes)
    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)
    assert output.read_bytes() == drifted_bytes


def test_expected_count_mismatch_produces_zero_writes(tmp_path: Path) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    with pytest.raises(HistoricalRegenerationCountMismatch):
        _run(tmp_path, promoted=2)
    assert not (trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME).exists()
    assert not (tmp_path / "historical-regeneration.json").exists()


def test_zero_descriptive_historical_records_are_analysis_ready_or_admissible(
    tmp_path: Path,
) -> None:
    _promoted_trial(tmp_path / "runs", locator="job/complete")
    _promoted_trial(
        tmp_path / "runs",
        locator="job/incomplete",
        final_state=False,
    )
    _promoted_trial(
        tmp_path / "runs",
        locator="job/refused",
        truth=False,
        events=False,
        final_state=False,
    )
    result = _run(tmp_path, promoted=3, derivable=2)
    assert result.manifest.descriptive_record_count == 2
    assert result.manifest.analysis_ready_count == 0
    assert result.manifest.admissible_count == 0
    assert all(row.readiness == "HOLD" for row in result.manifest.dispositions)
    assert all(row.admissible is False for row in result.manifest.dispositions)


def test_contract_regeneration_cli_defaults_to_nonmutating_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    revision = _source_revision(tmp_path)
    manifest = tmp_path / "cli-manifest.json"
    assert (
        run_cli(
            [
                "data",
                "backfill",
                "contracts",
                "--repo-root",
                str(tmp_path),
                "--runs-root",
                "runs",
                "--source-revision",
                revision,
                "--expect-promoted",
                "1",
                "--expect-derivable",
                "1",
                "--manifest-out",
                str(manifest),
            ],
            workspace=tmp_path,
        )
        == 0
    )
    output, error = capsys.readouterr()
    assert error == ""
    assert "historical contracts dry-run: commit=" in output
    assert "snapshot=sha256:" in output
    assert "1 promoted, 1 descriptive" in output
    assert "0 ANALYSIS_READY, 0 admissible" in output
    assert "plan=sha256:" in output
    assert manifest.is_file()
    assert not (trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME).exists()


def test_identical_output_symlink_is_refused_without_touching_external_bytes(
    tmp_path: Path,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    _run(tmp_path, apply=True)
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    external = tmp_path / "external-contract.json"
    output.replace(external)
    original = external.read_bytes()
    output.symlink_to(external)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert output.is_symlink()
    assert external.read_bytes() == original


def test_dangling_output_symlink_is_refused_without_creating_its_target(
    tmp_path: Path,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    external = tmp_path / "missing-external-contract.json"
    output.symlink_to(external)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert output.is_symlink()
    assert not external.exists()
    assert not (tmp_path / "historical-regeneration.json").exists()


@pytest.mark.parametrize("target_kind", ["directory", "fifo"])
def test_nonregular_output_target_is_refused_without_mutation(
    tmp_path: Path,
    target_kind: str,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    if target_kind == "directory":
        output.mkdir()
    else:
        os.mkfifo(output)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert output.is_dir() if target_kind == "directory" else stat_is_fifo(output)
    assert not (tmp_path / "historical-regeneration.json").exists()


def stat_is_fifo(path: Path) -> bool:
    return (path.lstat().st_mode & 0o170000) == 0o010000


def test_manifest_target_symlink_is_refused_without_touching_external_bytes(
    tmp_path: Path,
) -> None:
    _promoted_trial(tmp_path / "runs")
    _run(tmp_path)
    manifest = tmp_path / "historical-regeneration.json"
    external = tmp_path / "external-manifest.json"
    manifest.replace(external)
    original = external.read_bytes()
    manifest.symlink_to(external)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path)

    assert manifest.is_symlink()
    assert external.read_bytes() == original


def _inject_publication_winner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conflict: bool,
) -> None:
    original_link = data_backfill._link_historical_temp
    injected = False

    def racing_link(
        temporary_name: str,
        target_name: str,
        *,
        directory_fd: int,
    ) -> None:
        nonlocal injected
        if target_name == HISTORICAL_CONTRACT_FILENAME and not injected:
            injected = True
            temporary_fd = os.open(temporary_name, os.O_RDONLY, dir_fd=directory_fd)
            try:
                with os.fdopen(temporary_fd, "rb", closefd=False) as handle:
                    winner = handle.read()
            finally:
                os.close(temporary_fd)
            if conflict:
                winner = b"concurrent-operator-owned-conflict\n"
            winner_fd = os.open(
                target_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=directory_fd,
            )
            try:
                os.write(winner_fd, winner)
                os.fsync(winner_fd)
            finally:
                os.close(winner_fd)
        original_link(
            temporary_name,
            target_name,
            directory_fd=directory_fd,
        )

    monkeypatch.setattr(data_backfill, "_link_historical_temp", racing_link)


def test_concurrent_conflicting_writer_wins_without_being_clobbered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    _inject_publication_winner(monkeypatch, conflict=True)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert output.read_bytes() == b"concurrent-operator-owned-conflict\n"
    assert not (tmp_path / "historical-regeneration.json").exists()


def test_concurrent_identical_writer_converges_as_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    _inject_publication_winner(monkeypatch, conflict=False)

    result = _run(tmp_path, apply=True)

    assert result.created_output_count == 0
    assert result.verified_output_count == 1
    assert "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest() == (
        result.manifest.outputs[0].digest
    )


def test_symlinked_output_parent_is_refused_at_commit_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    artifacts = trial / "artifacts"
    external = tmp_path / "external-artifacts"
    original_publish = data_backfill._atomic_create_or_verify_historical_anchored
    injected = False

    def replace_parent_then_publish(
        anchor,
        relative_path: Path,
        content: bytes,
    ) -> bool:
        nonlocal injected
        if relative_path.name == HISTORICAL_CONTRACT_FILENAME and not injected:
            injected = True
            artifacts.replace(external)
            artifacts.symlink_to(external, target_is_directory=True)
        return original_publish(anchor, relative_path, content)

    monkeypatch.setattr(
        data_backfill,
        "_atomic_create_or_verify_historical_anchored",
        replace_parent_then_publish,
    )

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert artifacts.is_symlink()
    assert not (external / HISTORICAL_CONTRACT_FILENAME).exists()
    assert not (tmp_path / "historical-regeneration.json").exists()


def test_symlink_target_winning_commit_race_is_refused_without_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _promoted_trial(tmp_path / "runs")
    external = tmp_path / "external-race-winner.json"
    original_link = data_backfill._link_historical_temp
    injected = False

    def symlink_race(
        temporary_name: str,
        target_name: str,
        *,
        directory_fd: int,
    ) -> None:
        nonlocal injected
        if target_name == HISTORICAL_CONTRACT_FILENAME and not injected:
            injected = True
            external.write_bytes(b"external-race-winner\n")
            os.symlink(external, target_name, dir_fd=directory_fd)
        original_link(
            temporary_name,
            target_name,
            directory_fd=directory_fd,
        )

    monkeypatch.setattr(data_backfill, "_link_historical_temp", symlink_race)

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert external.read_bytes() == b"external-race-winner\n"
    assert not (tmp_path / "historical-regeneration.json").exists()


@pytest.mark.parametrize(
    "replacement_kind",
    ["symlink", "directory", "conflict", "identical-regular"],
)
def test_existing_identical_output_is_reread_at_return_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    trial = _promoted_trial(tmp_path / "runs")
    _run(tmp_path, apply=True)
    output = trial / "artifacts" / HISTORICAL_CONTRACT_FILENAME
    manifest = tmp_path / "historical-regeneration.json"
    manifest.unlink()
    external = tmp_path / "existing-output-external.json"
    external.write_bytes(output.read_bytes())
    external_bytes = external.read_bytes()
    injected = False

    def swap_existing_target(stage: str) -> None:
        nonlocal injected
        if stage != "before_existing_identical_return" or injected:
            return
        injected = True
        if replacement_kind == "symlink":
            output.unlink()
            output.symlink_to(external)
        elif replacement_kind == "directory":
            output.unlink()
            output.mkdir()
        elif replacement_kind == "conflict":
            output.write_bytes(b"conflicting-return-boundary-bytes\n")
        else:
            replacement = output.with_suffix(".replacement")
            replacement.write_bytes(external_bytes)
            replacement.replace(output)

    monkeypatch.setattr(
        data_backfill,
        "_historical_publication_boundary",
        swap_existing_target,
    )

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path, apply=True)

    assert external.read_bytes() == external_bytes
    assert not manifest.exists()


def test_existing_identical_manifest_is_reread_no_follow_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _promoted_trial(tmp_path / "runs")
    _run(tmp_path)
    manifest = tmp_path / "historical-regeneration.json"
    external = tmp_path / "existing-manifest-external.json"
    external.write_bytes(manifest.read_bytes())
    external_bytes = external.read_bytes()
    injected = False

    def swap_existing_manifest(stage: str) -> None:
        nonlocal injected
        if stage == "before_existing_identical_return" and not injected:
            injected = True
            manifest.unlink()
            manifest.symlink_to(external)

    monkeypatch.setattr(
        data_backfill,
        "_historical_publication_boundary",
        swap_existing_manifest,
    )

    with pytest.raises(HistoricalRegenerationConflict):
        _run(tmp_path)

    assert manifest.is_symlink()
    assert external.read_bytes() == external_bytes
