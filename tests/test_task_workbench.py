from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from evallab.task_workbench import (
    NETWORK_OVERLAY_CONTENT,
    NETWORK_OVERLAY_RELATIVE,
    CandidateSource,
    ControlBundle,
    ControlObservation,
    ControlsNotAdmittedError,
    HarborControlBackend,
    PacketConflictError,
    UnsafePathError,
    WorkbenchError,
    _harbor_task_digest,
    check_candidate,
    classify_trial_outcome,
    inspect_candidate,
    load_control_bundle,
    run_cli,
    run_controls,
    write_packet,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/task_workbench"
VALID = FIXTURES / "valid"
CASES = FIXTURES / "cases"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _source() -> CandidateSource:
    return CandidateSource(
        source_uri="local/uppercase-fixture",
        source_ref="local/uppercase-fixture@1.0.0",
        license="MIT",
        provenance_zone="02-local-evidence",
    )


def _copy_candidate(tmp_path: Path, case: str | None = None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    task = repo / "library/synthetic/m007/uppercase-fixture"
    task.parent.mkdir(parents=True)
    shutil.copytree(VALID, task)
    if case is None:
        return repo, task
    case_root = CASES / case
    manifest_path = case_root / "case.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        for relative in manifest.get("remove", []):
            (task / relative).unlink()
        if "symlink" in manifest:
            target = repo / "outside.txt"
            target.write_text("outside\n")
            link = task / manifest["symlink"]
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(manifest["target"])
    for source in sorted(case_root.rglob("*")):
        if not source.is_file() or source.name in {"case.json", "controls.json"}:
            continue
        relative = source.relative_to(case_root)
        destination = task / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return repo, task


def _inspect(repo: Path, task: Path):
    return inspect_candidate(repo_root=repo, task_path=task, source=_source())


def _fixture_overrides(case: str | None) -> dict[str, dict[str, object]]:
    if case is None:
        return {}
    path = CASES / case / "controls.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    return value["override"]


class FixtureBackend:
    def __init__(self, *, case: str | None = None) -> None:
        self.overrides = _fixture_overrides(case)
        self.calls: list[str] = []

    def run(self, *, repo_root, task_dir, candidate, plan, run_root):
        self.calls.append(plan.control_id)
        override = self.overrides.get(plan.control_id, {})
        status = override.get("status", "completed")
        reward = override.get("reward", plan.expected_reward)
        if status != "completed":
            reward = None
        seed = override.get("verifier_output_seed", f"{plan.kind}:{reward}")
        stage = run_root / "staging" / plan.control_id
        shutil.copytree(task_dir, stage)
        if plan.mutation_path is not None:
            mutation = stage / plan.mutation_path
            shutil.copyfile(mutation, stage / "solution/solve.sh")
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(NETWORK_OVERLAY_CONTENT)
        staged_digest = _tree_digest(stage)
        verifier_output_digest = None
        evidence_digest = None
        job_path = None
        if status == "completed":
            job_name = plan.command[plan.command.index("--job-name") + 1]
            job = run_root / "jobs" / job_name
            trial = job / f"{plan.control_id}__fixture"
            trial.mkdir(parents=True)
            (job / "result.json").write_bytes(
                _canonical(
                    {
                        "id": f"job-{plan.control_id}",
                        "n_total_trials": 1,
                        "stats": {},
                        "finished_at": "2026-08-15T00:00:00Z",
                    }
                )
            )
            vector = {"reward": reward}
            overlay_path = str(overlay.resolve())
            stage_path = str(stage.resolve())
            (trial / "result.json").write_bytes(
                _canonical(
                    {
                        "id": f"trial-{plan.control_id}",
                        "task_name": candidate["task_name"],
                        "trial_name": f"{plan.control_id}__fixture",
                        "task_id": {"path": stage_path},
                        "task_checksum": "c" * 64,
                        "config": {
                            "task": {"path": stage_path},
                            "environment": {
                                "type": "docker",
                                "extra_docker_compose": [overlay_path],
                            },
                        },
                        "agent_info": {"name": plan.agent},
                        "verifier_result": {"rewards": vector},
                        "verifier_environment_mode": "separate",
                        "exception_info": None,
                    }
                )
            )
            (trial / "lock.json").write_bytes(
                _canonical(
                    {
                        "task": {
                            "name": plan.control_id,
                            "version": candidate["task_version"],
                            "type": "local",
                            "digest": _harbor_task_digest(stage),
                            "path": stage_path,
                        },
                        "agent": {"name": plan.agent},
                        "environment": {
                            "type": "docker",
                            "extra_docker_compose": [overlay_path],
                        },
                        "extra_docker_compose": [
                            {
                                "path": overlay_path,
                                "digest": _digest(NETWORK_OVERLAY_CONTENT),
                            }
                        ],
                        "verifier": {
                            "disable": False,
                            "environment_mode": "separate",
                        },
                    }
                )
            )
            verifier_output_digest = _digest(_canonical(vector))
            if "verifier_output_seed" in override:
                verifier_output_digest = _digest(str(seed).encode())
            evidence_digest = _tree_digest(job)
            job_path = job.relative_to(repo_root).as_posix()
        digests = candidate["digests"]
        return ControlObservation(
            control_id=plan.control_id,
            status=status,
            reward=reward,
            reward_vector={"reward": reward} if reward is not None else {},
            verifier_output_digest=verifier_output_digest,
            evidence_digest=evidence_digest,
            image_digest=digests["image_definition"],
            verifier_digest=digests["verifier"],
            source_package_digest=digests["package"],
            staged_package_digest=staged_digest,
            command=plan.command,
            command_digest=plan.command_digest,
            job_path=job_path,
            exception_type=None,
            diagnostic=override.get("diagnostic"),
        )


def _bundle(
    inspection,
    *,
    repo: Path,
    task: Path,
    case: str | None = None,
) -> ControlBundle:
    backend = FixtureBackend(case=case)
    run_root = repo / "runs/task-workbench" / inspection.candidate["candidate_id"]
    observations = [
        backend.run(
            repo_root=repo,
            task_dir=task,
            candidate=inspection.candidate,
            plan=plan,
            run_root=run_root,
        )
        for plan in inspection.control_plan
    ]
    return ControlBundle.build(
        candidate_id=inspection.candidate["candidate_id"],
        source_package_digest=inspection.candidate["digests"]["package"],
        observations=observations,
    )


def _codes(inspection) -> set[str]:
    return {item.code for item in inspection.diagnostics}


def _tree_snapshot(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): _digest(item.read_bytes())
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.is_symlink()
    }


def _tree_digest(path: Path) -> str:
    payload = [
        {
            "path": item.relative_to(path).as_posix(),
            "type": "file",
            "size_bytes": item.stat().st_size,
            "digest": _digest(item.read_bytes()),
        }
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.is_symlink()
    ]
    return _digest(_canonical(payload))


def test_valid_candidate_inspection_freezes_every_digest_and_safe_command(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)

    assert inspection.static_passed
    assert inspection.diagnostics == ()
    assert len(inspection.control_plan) == 7
    assert [item.control_id for item in inspection.control_plan[:4]] == [
        "oracle-1",
        "oracle-2",
        "oracle-3",
        "nop-1",
    ]
    assert len([item for item in inspection.control_plan if item.kind == "adversarial"]) == 3
    assert all(item.agent in {"oracle", "nop"} for item in inspection.control_plan)
    assert all(item.concurrency == 1 for item in inspection.control_plan)
    assert all("--model" not in item.command for item in inspection.control_plan)
    assert all(
        item.command[item.command.index("--n-concurrent") + 1] == "1"
        for item in inspection.control_plan
    )
    assert all("--extra-docker-compose" in item.command for item in inspection.control_plan)
    assert all(
        item.command[item.command.index("--extra-docker-compose") + 1].endswith(
            NETWORK_OVERLAY_RELATIVE
        )
        for item in inspection.control_plan
    )

    candidate = inspection.candidate
    assert candidate["admission_boundary"] == {
        "candidate_only": True,
        "can_queue": False,
        "can_register": False,
        "can_freeze": False,
        "can_publish": False,
        "can_edit_policy": False,
        "required_next_actor": "human-created library/registry record",
    }
    assert set(candidate["digests"]) == {
        "package",
        "task_toml",
        "instruction",
        "image_definition",
        "solution",
        "verifier",
        "adversarial_controls",
        "artifact_config",
        "source_metadata",
    }
    assert all(value.startswith("sha256:") for value in candidate["digests"].values())
    assert all(
        set(item) == {"path", "role", "type", "size_bytes", "digest"} for item in candidate["files"]
    )


def test_plan_rebuild_is_byte_identical_and_checkout_location_independent(
    tmp_path: Path,
) -> None:
    first_repo, first_task = _copy_candidate(tmp_path / "first")
    second_repo, second_task = _copy_candidate(tmp_path / "second")
    first = _inspect(first_repo, first_task).to_dict()
    second = _inspect(second_repo, second_task).to_dict()

    assert _canonical(first) == _canonical(second)


def test_missing_required_file_is_a_task_defect(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "missing-files")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert "required_file_missing" in _codes(inspection)
    finding = next(item for item in inspection.diagnostics if item.code == "required_file_missing")
    assert finding.path == "tests/test.sh"
    assert finding.classification == "task_defect"


def test_candidate_argument_and_symlink_path_escape_are_refused(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "path-escape")
    inspection = _inspect(repo, task)
    assert "path_escape" in _codes(inspection)

    outside = tmp_path / "outside-task"
    shutil.copytree(VALID, outside)
    with pytest.raises(UnsafePathError, match="escapes repository"):
        inspect_candidate(repo_root=repo, task_path=outside, source=_source())


def test_json_form_copy_of_hidden_solution_is_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "json-copy-leak")
    inspection = _inspect(repo, task)

    assert "agent_image_hidden_leak" in _codes(inspection)


def test_hidden_golden_leak_is_named_without_disclosing_hidden_bytes(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "hidden-leak")
    inspection = _inspect(repo, task)
    findings = [item for item in inspection.diagnostics if item.code == "golden_data_leak"]

    assert len(findings) == 1
    assert findings[0].path == "tests/golden.txt"
    assert "ALPHA-BETA-GAMMA" not in findings[0].message


def test_runtime_network_use_and_unpinned_dependency_are_rejected(tmp_path: Path) -> None:
    network_repo, network_task = _copy_candidate(tmp_path / "network", "network-use")
    network = _inspect(network_repo, network_task)
    assert "runtime_network_use" in _codes(network)

    pin_repo, pin_task = _copy_candidate(tmp_path / "pin", "unpinned-dependency")
    pin = _inspect(pin_repo, pin_task)
    assert "base_image_unpinned" in _codes(pin)


def test_forged_registration_is_rejected_but_real_record_is_only_observed(
    tmp_path: Path,
) -> None:
    forged_repo, forged_task = _copy_candidate(tmp_path / "forged", "forged-registration")
    assert "forged_registration" in _codes(_inspect(forged_repo, forged_task))

    repo, task = _copy_candidate(tmp_path / "observed")
    registry = repo / "library/registry"
    registry.mkdir(parents=True)
    record = registry / "uppercase-fixture.json"
    record.write_text(
        json.dumps(
            {
                "task_id": "uppercase-fixture",
                "task_path": "library/synthetic/m007/uppercase-fixture",
                "state": "registered",
            }
        )
    )
    before = record.read_bytes()
    observation = _inspect(repo, task).candidate["registration_observation"]
    assert observation["state"] == "registered"
    assert observation["path_matches"] is True
    assert record.read_bytes() == before


def test_static_failure_makes_zero_control_calls(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "missing-files")
    inspection = _inspect(repo, task)
    backend = FixtureBackend()

    with pytest.raises(ControlsNotAdmittedError, match="zero controls"):
        run_controls(
            inspection=inspection,
            repo_root=repo,
            task_path=task,
            backend=backend,
        )
    assert backend.calls == []
    assert not (repo / "runs").exists()


def test_valid_controls_certify_and_rescan_is_idempotent(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    backend = FixtureBackend()
    bundle = run_controls(
        inspection=inspection,
        repo_root=repo,
        task_path=task,
        backend=backend,
    )

    assert len(backend.calls) == 7
    assert check_candidate(inspection, bundle, repo_root=repo).disposition == (
        "certified_for_review"
    )
    backend.calls.clear()
    second = run_controls(
        inspection=inspection,
        repo_root=repo,
        task_path=task,
        backend=backend,
    )
    assert backend.calls == []
    assert second == bundle


@pytest.mark.parametrize(
    ("case", "code", "disposition"),
    [
        ("nondeterminism", "verifier_nondeterministic", "needs_changes"),
        ("permissive-verifier", "verifier_permissive", "needs_changes"),
        ("false-negative-verifier", "oracle_false_negative", "needs_changes"),
        ("interrupted-controls", "control_interrupted", "harness_blocked"),
    ],
)
def test_control_regression_fixtures(
    case: str, code: str, disposition: str, tmp_path: Path
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    report = check_candidate(
        inspection,
        _bundle(inspection, repo=repo, task=task, case=case),
        repo_root=repo,
    )

    assert report.disposition == disposition
    assert code in {item.code for item in report.diagnostics}
    if case == "interrupted-controls":
        finding = next(item for item in report.diagnostics if item.code == code)
        assert finding.classification == "harness_defect"
        assert all(item.classification != "agent_failure" for item in report.diagnostics)


def test_outcome_classifier_separates_task_harness_and_agent_failures() -> None:
    assert (
        classify_trial_outcome(agent="codex", reward=0.0, exception_type=None, expected_reward=1.0)
        == "agent_failure"
    )
    assert (
        classify_trial_outcome(
            agent="codex",
            reward=None,
            exception_type="EnvironmentBuildError",
            expected_reward=1.0,
        )
        == "task_defect"
    )
    assert (
        classify_trial_outcome(
            agent="oracle",
            reward=None,
            exception_type="RewardFileNotFoundError",
            expected_reward=1.0,
        )
        == "task_defect"
    )
    assert (
        classify_trial_outcome(
            agent="codex",
            reward=None,
            exception_type="NonZeroAgentExitCodeError",
            expected_reward=1.0,
        )
        == "agent_failure"
    )
    assert (
        classify_trial_outcome(agent="oracle", reward=0.0, exception_type=None, expected_reward=1.0)
        == "task_defect"
    )
    assert (
        classify_trial_outcome(agent="nop", reward=0.0, exception_type=None, expected_reward=0.0)
        == "expected"
    )


def test_task_owned_docker_build_failure_is_not_a_harness_defect(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    backend = HarborControlBackend(
        command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="EnvironmentBuildError: Dockerfile failed to build",
        ),
        environment_provider=lambda: {},
    )
    plan = inspection.control_plan[0]

    observation = backend.run(
        repo_root=repo,
        task_dir=task,
        candidate=inspection.candidate,
        plan=plan,
        run_root=repo / "runs/task-workbench" / inspection.candidate["candidate_id"],
    )

    assert observation.status == "harness_error"
    assert observation.failure_classification == "task_defect"


def test_control_bundle_rejects_unknown_fields_and_digest_tampering(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    value = _bundle(inspection, repo=repo, task=task).to_dict()

    value["unknown"] = True
    with pytest.raises(WorkbenchError, match="unknown fields"):
        ControlBundle.from_dict(value)
    value.pop("unknown")
    value["bundle_digest"] = "sha256:" + "0" * 64
    with pytest.raises(WorkbenchError, match="digest mismatch"):
        ControlBundle.from_dict(value)


def test_self_authored_controls_without_retained_job_cannot_certify(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    valid = _bundle(inspection, repo=repo, task=task)
    forged_observations = list(valid.observations)
    forged_observations[0] = replace(
        forged_observations[0],
        job_path="runs/does-not-exist",
        evidence_digest="sha256:" + "a" * 64,
        staged_package_digest="sha256:" + "b" * 64,
    )
    forged = ControlBundle.build(
        candidate_id=valid.candidate_id,
        source_package_digest=valid.source_package_digest,
        observations=forged_observations,
    )

    report = check_candidate(inspection, forged, repo_root=repo)

    assert report.disposition != "certified_for_review"
    assert "control_job_path_invalid" in {item.code for item in report.diagnostics}


def test_retained_job_and_stage_bytes_are_recomputed_before_certification(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    bundle = _bundle(inspection, repo=repo, task=task)
    observation = bundle.observations[0]
    assert observation.job_path is not None
    job_result = repo / observation.job_path / "result.json"
    job_result.write_text(job_result.read_text() + " ")

    report = check_candidate(inspection, bundle, repo_root=repo)

    assert report.disposition == "needs_changes"
    assert "control_evidence_tampered" in {item.code for item in report.diagnostics}


def test_wrong_task_job_and_missing_network_binding_cannot_certify(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    valid = _bundle(inspection, repo=repo, task=task)
    observations = list(valid.observations)
    first = observations[0]
    assert first.job_path is not None
    job = repo / first.job_path
    trial_result_path = next(path for path in job.glob("*/result.json"))
    trial_result = json.loads(trial_result_path.read_text())
    trial_result["task_name"] = "other/task"
    trial_result["config"]["task"]["path"] = str(repo / "other-task")
    trial_result["config"]["environment"]["extra_docker_compose"] = []
    trial_result_path.write_bytes(_canonical(trial_result))
    observations[0] = replace(first, evidence_digest=_tree_digest(job))
    forged = ControlBundle.build(
        candidate_id=valid.candidate_id,
        source_package_digest=valid.source_package_digest,
        observations=observations,
    )

    report = check_candidate(inspection, forged, repo_root=repo)
    codes = {item.code for item in report.diagnostics}

    assert report.disposition == "needs_changes"
    assert "control_task_identity_mismatch" in codes
    assert "control_network_binding_mismatch" in codes


def test_packet_rebuild_is_byte_identical_and_never_overwrites(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    report = check_candidate(
        inspection,
        _bundle(inspection, repo=repo, task=task),
        repo_root=repo,
    )
    first_paths = write_packet(repo_root=repo, report=report)
    first_bytes = tuple(path.read_bytes() for path in first_paths)
    first_evidence = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in sorted(first_paths[0].parent.joinpath("evidence").glob("*.json"))
    }
    second_paths = write_packet(repo_root=repo, report=report)

    assert second_paths == first_paths
    assert tuple(path.read_bytes() for path in second_paths) == first_bytes
    assert len(first_evidence) == 7
    assert {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in sorted(first_paths[0].parent.joinpath("evidence").glob("*.json"))
    } == first_evidence
    candidate = json.loads(first_paths[0].read_text())
    certification = json.loads(first_paths[1].read_text())
    assert candidate["admission_boundary"]["can_register"] is False
    assert certification["admission_granted"] is False
    assert certification["certified"] is True
    assert len(certification["retained_evidence"]) == 7
    assert all(
        (repo / item["path"]).is_file() for item in certification["retained_evidence"]
    )
    assert "ALPHA-BETA-GAMMA" not in b"".join(first_evidence.values()).decode()

    first_paths[1].write_text("tampered\n")
    with pytest.raises(PacketConflictError, match="non-identical"):
        write_packet(repo_root=repo, report=report)


def test_packet_path_cannot_target_registry_queue_policy_or_outside(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    report = check_candidate(_inspect(repo, task))

    for target in (
        repo / "library/registry",
        repo / "queue/proposed",
        repo / "policy",
        tmp_path / "outside",
    ):
        with pytest.raises(UnsafePathError, match="only under"):
            write_packet(repo_root=repo, report=report, output_root=target)


def test_failed_candidate_packet_preserves_exact_diagnostics(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "missing-files")
    inspection = _inspect(repo, task)
    report = check_candidate(inspection)
    candidate_path, certification_path = write_packet(repo_root=repo, report=report)

    assert task.is_dir()
    assert candidate_path.is_file()
    certification = json.loads(certification_path.read_text())
    assert certification["status"] == "needs_changes"
    assert certification["check_vector"] == {
        "invalid_outputs_rejected": False,
        "isolation": False,
        "nop_exact_0": False,
        "oracle_exact_1": False,
        "static": False,
        "verifier_deterministic": False,
    }
    assert any(item["code"] == "required_file_missing" for item in certification["diagnostics"])


def test_controls_pending_packet_does_not_claim_unobserved_control_success(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    report = check_candidate(_inspect(repo, task))
    _, certification_path = write_packet(repo_root=repo, report=report)
    certification = json.loads(certification_path.read_text())

    assert certification["status"] == "controls_pending"
    assert certification["check_vector"]["static"] is True
    assert certification["check_vector"]["isolation"] is True
    assert certification["check_vector"]["oracle_exact_1"] is False
    assert certification["check_vector"]["nop_exact_0"] is False
    assert certification["check_vector"]["invalid_outputs_rejected"] is False
    assert certification["check_vector"]["verifier_deterministic"] is False


def test_source_provenance_and_license_fail_closed(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    source = CandidateSource(
        source_uri="https://example.invalid/repo",
        source_ref="main",
        license="unknown",
        provenance_zone="01-external",
    )
    inspection = inspect_candidate(repo_root=repo, task_path=task, source=source)

    assert {"source_ref_unpinned", "license_missing"} <= _codes(inspection)


def test_inspect_check_and_packet_never_change_candidate_bytes(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    before = _tree_snapshot(task)
    inspection = _inspect(repo, task)
    report = check_candidate(
        inspection,
        _bundle(inspection, repo=repo, task=task),
        repo_root=repo,
    )
    write_packet(repo_root=repo, report=report)

    assert _tree_snapshot(task) == before


def test_cli_plan_check_packet_without_shared_cli_wiring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, task = _copy_candidate(tmp_path)
    common = [
        str(task),
        "--repo-root",
        str(repo),
        "--source-uri",
        "local/uppercase-fixture",
        "--source-ref",
        "local/uppercase-fixture@1.0.0",
        "--license",
        "MIT",
        "--zone",
        "02-local-evidence",
    ]
    assert run_cli(["plan", *common]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    inspection = _inspect(repo, task)
    controls_path = tmp_path / "controls.json"
    controls_path.write_bytes(
        _canonical(_bundle(inspection, repo=repo, task=task).to_dict())
    )

    assert run_cli(["check", *common, "--controls", str(controls_path)]) == 0
    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["disposition"] == "certified_for_review"
    assert run_cli(["packet", *common, "--controls", str(controls_path)]) == 0
    packet_payload = json.loads(capsys.readouterr().out)
    assert packet_payload["candidate"].startswith("research/registration/candidates/")
    assert plan_payload["candidate"]["candidate_id"] == check_payload["candidate_id"]


def test_module_has_no_queue_policy_publish_or_registry_write_capability() -> None:
    source = (ROOT / "src/evallab/task_workbench.py").read_text()

    assert "from evallab.queue" not in source
    assert "import evallab.queue" not in source
    assert "TaskRegistryRecord" not in source
    assert "library/registry" in source  # read-only observation is deliberate
    assert "research/registration/candidates" in source
    assert "--model" not in source
    assert "gh " not in source


def test_load_bundle_fixture_file_and_unknown_json_fail_closed(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    path = tmp_path / "controls.json"
    expected = _bundle(inspection, repo=repo, task=task)
    path.write_bytes(_canonical(expected.to_dict()))
    assert load_control_bundle(path) == expected

    path.write_text("{not-json")
    with pytest.raises(WorkbenchError, match="invalid control bundle"):
        load_control_bundle(path)
