from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from evallab.registry import (
    TaskCertificationError,
    TaskRegistry,
    audit_registry,
    certification_envelope_from_packet,
    compute_task_digests,
    inventory_tasks,
    verify_certification_packet,
)
from evallab.registry import _canonical_bytes as registry_canonical_bytes
from evallab.registry import _digest_bytes as registry_digest_bytes
from evallab.schemas import TaskRegistryRecord
from evallab.task_workbench import (
    _MODELLED_CONSTRUCT_VALUES,
    _SUPPORTED_ENVIRONMENT_KEYS,
    BUILD_NETWORK_PATTERN,
    NETWORK_OVERLAY_CONTENT,
    NETWORK_OVERLAY_RELATIVE,
    NETWORK_SCRIPT_PATTERN,
    SUPPORTED_TASK_CONFIG,
    CandidateSource,
    ControlBundle,
    ControlObservation,
    ControlsNotAdmittedError,
    HarborControlBackend,
    PacketConflictError,
    UnsafePathError,
    WorkbenchError,
    _candidate_network_overlay,
    _effective_verifier_network,
    _harbor_task_digest,
    _sha256_bytes,
    _verifier_output_digest,
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

# Harbor is installed as a standalone CLI tool, not as an importable dependency
# of this package, so the workbench reproduces its verifier-network resolution
# instead of calling it. This probe runs the real resolver inside Harbor's own
# interpreter so the reproduction can be pinned against it.
_HARBOR_RESOLUTION_PROBE = """
import json, sys, tomllib

from harbor.models.task.config import TaskConfig
from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
from harbor.trial.network_policy import resolve_verifier_phase_policy

results = []
for document in json.loads(sys.stdin.read()):
    config = TaskConfig.model_validate(tomllib.loads(document))
    resolved = []
    for step in (config.steps or [None]):
        env = resolve_effective_verifier_env_config(config, step)
        if env is None:
            resolved.append(None)
            continue
        baseline = env.resolve_baseline()
        phase = resolve_verifier_phase_policy(config, step, baseline=baseline)
        resolved.append([baseline.network_mode.value, phase.network_mode.value])
    results.append(resolved)
print(json.dumps(results))
"""


def _harbor_probe(script: str, documents: list[str]) -> object:
    """Run `script` over `documents` inside Harbor's own interpreter."""
    executable = shutil.which("harbor")
    interpreter = Path(executable).resolve().parent / "python" if executable else None
    if interpreter is None or not interpreter.exists():
        pytest.skip("harbor is not installed; cannot pin the mirror against its resolver")
    completed = subprocess.run(
        [str(interpreter), "-c", script],
        input=json.dumps(documents),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _harbor_resolution(documents: list[str]) -> list[list[list[str] | None]]:
    return cast("list[list[list[str] | None]]", _harbor_probe(_HARBOR_RESOLUTION_PROBE, documents))


# The F-06 widening rests on each admitted value being one Harbor folds away.
# This probe resolves whole documents through Harbor's own model so that claim is
# pinned against Harbor 0.21.0 rather than against a reading of it.
_HARBOR_TASK_CONFIG_PROBE = """
import json, sys, tomllib

from harbor.models.task.config import TaskConfig

results = []
for document in json.loads(sys.stdin.read()):
    config = TaskConfig.model_validate(tomllib.loads(document))
    results.append(
        {
            "dump": config.model_dump(mode="json"),
            # harbor/environments/base.py:367-369
            "effective_gpus": config.environment.gpus or 0,
        }
    )
print(json.dumps(results))
"""


def _harbor_task_configs(documents: list[str]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", _harbor_probe(_HARBOR_TASK_CONFIG_PROBE, documents))


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
        stage = run_root / "staging" / plan.control_id
        shutil.copytree(task_dir, stage)
        if plan.mutation_path is not None:
            mutation = stage / plan.mutation_path
            shutil.copyfile(mutation, stage / "solution/solve.sh")
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(_candidate_network_overlay(candidate))
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
                                "digest": _digest(_candidate_network_overlay(candidate)),
                            }
                        ],
                        "verifier": {
                            "disable": False,
                            "environment_mode": "separate",
                        },
                    }
                )
            )
            verifier = trial / "verifier"
            verifier.mkdir()
            (verifier / "reward.txt").write_text(f"{reward}\n")
            (verifier / "test-stdout.txt").write_text(
                str(override.get("verifier_stdout", ""))
            )
            verifier_output_digest = _verifier_output_digest(trial)
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


def _bound_registry_record(
    repo: Path, task: Path, certification_path: Path
) -> TaskRegistryRecord:
    digests = compute_task_digests(task)
    relative_task = task.relative_to(repo).as_posix()
    certification = certification_envelope_from_packet(
        repo,
        certification_path,
        task_id="uppercase-fixture",
        task_version="1.0.0",
        task_path=relative_task,
        package_digest=digests.package,
    )
    return TaskRegistryRecord(
        task_id="uppercase-fixture",
        task_family="uppercase-fixture",
        version="1.0.0",
        task_path=relative_task,
        digests=digests,
        source_uri="local/uppercase-fixture",
        source_ref="local/uppercase-fixture@1.0.0",
        license="MIT",
        provenance_zone="02-local-evidence",
        is_synthetic=True,
        certification=certification,
        state="candidate",
        state_reason="certificate_bound_pending_human_admission",
        allowed_uses=["measurement"],
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

def test_workbench_registry_canonical_and_package_digests_are_identical(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    payload = {"z": [3, 2, 1], "a": {"unicode": "λ"}}
    canonical = _canonical(payload)
    assert registry_canonical_bytes(payload) == canonical
    assert registry_digest_bytes(canonical) == _sha256_bytes(canonical)
    assert _inspect(repo, task).candidate["digests"]["registry_package"] == (
        compute_task_digests(task).package
    )



def test_valid_candidate_inspection_freezes_every_digest_and_safe_command(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)

    assert inspection.static_passed
    assert inspection.diagnostics == ()
    assert len(inspection.control_plan) == 10
    assert [item.control_id for item in inspection.control_plan[:6]] == [
        "oracle-1",
        "oracle-2",
        "oracle-3",
        "fair-alternative",
        "nop-1",
        "nop-2",
    ]
    assert len([item for item in inspection.control_plan if item.kind == "adversarial"]) == 3
    assert inspection.control_plan[-1].control_id == "please-hack"
    assert all(item.agent in {"oracle", "nop"} for item in inspection.control_plan)
    assert all(item.concurrency == 1 for item in inspection.control_plan)
    assert all("--model" not in item.command for item in inspection.control_plan)
    assert all(
        item.command[item.command.index("--n-concurrent") + 1] == "1"
        for item in inspection.control_plan
    )
    assert all("--extra-docker-compose" in item.command for item in inspection.control_plan)
    assert all("--include-task-name" in item.command for item in inspection.control_plan)
    assert all(
        item.command[item.command.index("--include-task-name") + 1] == item.control_id
        for item in inspection.control_plan
    )
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
        "registry_package",
        "task_toml",
        "instruction",
        "image_definition",
        "solution",
        "verifier",
        "adversarial_controls",
        "fair_alternative",
        "please_hack",
        "leakage_scan",
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


def test_agent_visible_symlink_to_hidden_golden_is_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path, "golden-symlink")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    finding = next(item for item in inspection.diagnostics if item.code == "symlink_unsupported")
    assert finding.path == "environment/golden-link.txt"


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


def test_remote_add_and_build_time_network_fetch_are_rejected(tmp_path: Path) -> None:
    remote_repo, remote_task = _copy_candidate(tmp_path / "remote")
    (remote_task / "environment/Dockerfile").write_text(
        "FROM ubuntu@sha256:" + "a" * 64 + "\n"
        "ADD --checksum=sha256:" + "b" * 64
        + " https://example.invalid/archive.tar /app/archive.tar\n"
    )
    remote = _inspect(remote_repo, remote_task)
    assert "remote_docker_add" in _codes(remote)
    assert "build_network_use" in _codes(remote)

    fetch_repo, fetch_task = _copy_candidate(tmp_path / "fetch")
    (fetch_task / "tests/Dockerfile").write_text(
        "FROM ubuntu@sha256:" + "a" * 64 + "\n"
        "RUN git clone https://example.invalid/verifier.git /tests\n"
    )
    fetch = _inspect(fetch_repo, fetch_task)
    assert "build_network_use" in _codes(fetch)


def test_build_context_script_bypassing_the_dockerfile_is_rejected(tmp_path: Path) -> None:
    """`COPY setup.sh` + `RUN sh /tmp/setup.sh` hides the fetch outside Dockerfile lines."""
    repo, task = _copy_candidate(tmp_path / "script", "build-context-script")
    inspection = _inspect(repo, task)

    dockerfile = (task / "environment/Dockerfile").read_text()
    assert "curl" not in dockerfile and "https://" not in dockerfile
    assert not inspection.static_passed
    finding = next(
        item
        for item in inspection.diagnostics
        if item.code == "build_network_use" and item.path == "environment/setup.sh"
    )
    assert finding.severity == "error"
    assert finding.classification == "task_defect"


def test_unscannable_build_context_file_fails_closed(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "binary")
    (task / "environment/toolchain.bin").write_bytes(b"\xff\xfe\x00binary payload")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert any(
        item.code == "build_context_unreadable" and item.path == "environment/toolchain.bin"
        for item in inspection.diagnostics
    )


def test_networked_verifier_environment_is_rejected(tmp_path: Path) -> None:
    """An absent [verifier.environment] inherits [environment], overlay and all."""
    inherited_repo, inherited_task = _copy_candidate(
        tmp_path / "inherited", "networked-verifier"
    )
    inherited = _inspect(inherited_repo, inherited_task)
    assert not inherited.static_passed
    finding = next(
        item for item in inherited.diagnostics if item.code == "verifier_network_not_isolated"
    )
    assert finding.severity == "error"
    assert "'public'" in finding.message
    assert "[environment] (inherited)" in finding.message
    assert (
        inherited.candidate["network_policy"]["verifier_effective_baseline"] == "public"
    )


def test_verifier_environment_table_does_not_inherit_a_no_network_default(
    tmp_path: Path,
) -> None:
    """Harbor defaults EnvironmentConfig.network_mode to public; an empty table is public."""
    repo, task = _copy_candidate(tmp_path / "empty-table")
    config = (task / "task.toml").read_text()
    (task / "task.toml").write_text(config + '\n[verifier.environment]\ncpus = 1\n')
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    finding = next(
        item for item in inspection.diagnostics if item.code == "verifier_network_not_isolated"
    )
    assert "[verifier.environment]" in finding.message
    assert inspection.candidate["network_policy"]["verifier_effective_baseline"] == "public"


def test_verifier_phase_override_cannot_reopen_the_network(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "phase")
    config = (task / "task.toml").read_text().replace(
        '[verifier]\ntimeout_sec = 30.0',
        '[verifier]\nnetwork_mode = "public"\ntimeout_sec = 30.0',
    )
    (task / "task.toml").write_text(config)
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    codes = _codes(inspection)
    assert "verifier_phase_network_not_isolated" in codes
    # The baseline is still no-network, so only the phase override is reported.
    assert "verifier_network_not_isolated" not in codes
    assert inspection.candidate["network_policy"]["verifier_effective_phase"] == "public"


def test_step_level_verifier_network_is_refused_as_unsupported(tmp_path: Path) -> None:
    """[[steps]] resolves verifier-first in Harbor, so task-level tables prove nothing.

    The fixture's task-level tables are fully compliant. The pre-existing mirror
    therefore resolves a `no-network` baseline and reports nothing, which is
    exactly the false green: Harbor would start this step's verifier with full
    egress. Only the unsupported-configuration refusal catches it, and this test
    asserts that by pinning both halves.
    """
    repo, task = _copy_candidate(tmp_path / "steps", "step-verifier-network")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    finding = next(
        item
        for item in inspection.diagnostics
        if item.code == "unsupported_task_configuration"
    )
    assert finding.severity == "error"
    # A construct the workbench cannot model is a limitation of the workbench,
    # not evidence that the task is defective.
    assert finding.classification == "harness_defect"
    assert finding.path == "task.toml"
    assert finding.message.startswith("steps is outside")

    # The refusal is the only thing standing between this task and a packet: the
    # network mirror still sees a compliant task-level configuration.
    codes = _codes(inspection)
    assert codes == {"unsupported_task_configuration"}
    assert inspection.candidate["network_policy"]["verifier_effective_baseline"] == "no-network"


def test_verifier_build_context_script_fetch_is_rejected(tmp_path: Path) -> None:
    """`COPY . /tests` + `RUN sh bootstrap.sh` hides the fetch outside Dockerfile lines.

    Harbor builds the separate verifier image from `tests/` with
    `extra_docker_compose` cleared, so no `build.network=none` overlay reaches
    it and this scan is the only boundary. `apk add` is deliberately chosen: the
    runtime pattern that already covered `tests/` does not match it, so a pass
    here can only come from the build pattern now applied to the context.
    """
    repo, task = _copy_candidate(tmp_path / "verifier-build", "verifier-build-script")
    inspection = _inspect(repo, task)

    dockerfile = (task / "tests/Dockerfile").read_text()
    assert "apk" not in dockerfile and "https://" not in dockerfile
    assert not NETWORK_SCRIPT_PATTERN.search((task / "tests/bootstrap.sh").read_text())

    assert not inspection.static_passed
    finding = next(
        item
        for item in inspection.diagnostics
        if item.code == "build_network_use" and item.path == "tests/bootstrap.sh"
    )
    assert finding.severity == "error"
    assert "separate verifier image" in finding.message
    # Nothing else fires, so the refusal is not incidental to another check.
    assert _codes(inspection) == {"build_network_use"}


def test_unscannable_verifier_build_context_file_fails_closed(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "verifier-binary")
    (task / "tests/toolchain.bin").write_bytes(b"\xff\xfe\x00binary payload")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert any(
        item.code == "build_context_unreadable" and item.path == "tests/toolchain.bin"
        for item in inspection.diagnostics
    )


def test_task_authored_compose_under_tests_escapes_nothing(tmp_path: Path) -> None:
    """The two-line file that turned a compliant `no-network` task into full egress.

    Harbor builds and runs the separate verifier environment with
    `environment_dir = tests/` (`Trial._verifier_env_build_context`), and
    `DockerEnvironment._environment_docker_compose_path` is
    `environment_dir / "docker-compose.yaml"`, layered into
    `_docker_compose_paths` for `build` and `up` alike. A service there that
    declares its own `network_mode` is excluded from
    `_egress_controlled_service_names`, so it never joins the egress-control
    sidecar namespace that implements `no-network`.

    Every assertion before the refusal exists to show the refusal can only come
    from the new filename allowlist: the `task.toml` is inside the supported
    surface, the mirror resolves `no-network` for both baseline and phase, and
    neither content pattern matches the YAML read as text.
    """
    repo, task = _copy_candidate(tmp_path / "verifier-compose", "verifier-compose-escape")
    compose = task / "tests/docker-compose.yaml"
    text = compose.read_text()

    assert yaml.safe_load(text)["services"]["main"]["network_mode"] == "bridge"
    assert not NETWORK_SCRIPT_PATTERN.search(text)
    assert not BUILD_NETWORK_PATTERN.search(text)
    config = tomllib.loads((task / "task.toml").read_text())
    assert _effective_verifier_network(config)[:2] == ("no-network", "no-network")

    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    finding = next(
        item
        for item in inspection.diagnostics
        if item.code == "custom_compose_unsupported"
    )
    assert finding.path == "tests/docker-compose.yaml"
    assert finding.severity == "error"
    assert "separate verifier image" in finding.message
    assert _codes(inspection) == {"custom_compose_unsupported"}


@pytest.mark.parametrize(
    "relative",
    [
        "environment/docker-compose.yaml",
        "environment/docker-compose.yml",
        "environment/compose.yaml",
        "environment/docker-compose.override.yaml",
        "tests/docker-compose.yaml",
        "tests/docker-compose.yml",
        "tests/compose.yml",
        "tests/docker-compose.override.yml",
        "tests/fixtures/docker-compose.yaml",
    ],
)
def test_compose_filenames_are_refused_in_every_build_context(
    tmp_path: Path, relative: str
) -> None:
    """One refusal covers the whole Compose namespace, in both build contexts.

    Harbor 0.21.0 reads only `<environment dir>/docker-compose.yaml`, but which
    directory becomes an environment directory is Harbor's choice: the previous
    round refused the `environment/` spelling by exact path and left the `tests/`
    one, which Harbor hands the separate verifier, entirely unexamined.
    """
    repo, task = _copy_candidate(tmp_path / relative.replace("/", "-").replace(".", "-"))
    destination = task / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("services:\n  main:\n    network_mode: bridge\n")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert _codes(inspection) == {"custom_compose_unsupported"}
    assert [item.path for item in inspection.diagnostics] == [relative]


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("environment/.env", "compose_env_file_unsupported"),
        ("tests/.env", "compose_env_file_unsupported"),
        ("tests/.env.local", "compose_env_file_unsupported"),
        ("environment/.dockerignore", "build_context_ignore_unsupported"),
        ("tests/.dockerignore", "build_context_ignore_unsupported"),
    ],
)
def test_other_interpreted_build_context_files_are_refused(
    tmp_path: Path, relative: str, code: str
) -> None:
    """`.env` and `.dockerignore` are read as configuration, so they are refused.

    Harbor invokes `docker compose --project-directory <environment dir>` and
    never passes `--env-file`, so Compose reads that directory's `.env` and
    interpolates it into every Compose document including its own. Docker's
    builder reads `.dockerignore` to drop paths from the context, so the files
    the workbench scanned would not be the files in the image.
    """
    repo, task = _copy_candidate(tmp_path / relative.replace("/", "-").replace(".", "-"))
    (task / relative).write_text("PLACEHOLDER=1\n")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert _codes(inspection) == {code}
    assert [item.path for item in inspection.diagnostics] == [relative]


def test_reference_fixture_has_no_interpreted_build_context_configuration(
    tmp_path: Path,
) -> None:
    """The filename allowlist must not refuse the task the workbench certifies."""
    repo, task = _copy_candidate(tmp_path / "filenames")
    inspection = _inspect(repo, task)

    assert inspection.static_passed
    assert not {
        "custom_compose_unsupported",
        "compose_env_file_unsupported",
        "build_context_ignore_unsupported",
    } & _codes(inspection)


@pytest.mark.parametrize(
    "relative",
    [
        # `compose` must be a whole word: `\b` fails between `compose` and `r`.
        "tests/composer.json",
        # Compose reads `.env`, never `.envrc`.
        "environment/.envrc",
        # Harbor resolves exact names; nothing reads a differently-prefixed file.
        "tests/service-compose.yaml",
    ],
)
def test_lookalike_filenames_are_not_refused(tmp_path: Path, relative: str) -> None:
    """The allowlist covers the namespace Harbor resolves, not everything near it."""
    repo, task = _copy_candidate(tmp_path / relative.replace("/", "-").replace(".", "-"))
    (task / relative).write_text("{}\n")
    inspection = _inspect(repo, task)

    assert inspection.static_passed, _codes(inspection)


def test_verifier_build_uv_sync_is_rejected(tmp_path: Path) -> None:
    """`RUN uv sync` is this repository's own idiom and used to pass unnoticed."""
    repo, task = _copy_candidate(tmp_path / "verifier-uv-sync", "verifier-uv-sync")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert _codes(inspection) == {"build_network_use"}
    assert [item.path for item in inspection.diagnostics] == ["tests/Dockerfile"]


# One plain spelling per package-manager family the build-time scan now names.
# These are not obfuscations: each is the documented way to install dependencies
# in that ecosystem, and the separate verifier image has no other boundary.
_BUILD_INSTALL_IDIOMS = [
    "uv sync --frozen",
    "uv add ruff",
    "uv lock",
    "npm i left-pad",
    "pnpm i",
    "npx cowsay hello",
    "poetry install --no-root",
    "pipenv install",
    "bundle install",
    "composer install",
    "conda install -y numpy",
    "micromamba install -y numpy",
    "brew install jq",
    "pacman -Syu --noconfirm",
    "dotnet restore",
    "dotnet add package Newtonsoft.Json",
    "mvn install -DskipTests",
    "./gradlew build",
    "gradle build",
    "pip download requests",
    "cargo fetch",
    "go mod download",
]


@pytest.mark.parametrize("command", _BUILD_INSTALL_IDIOMS)
def test_plain_install_idioms_are_rejected_in_the_verifier_build(
    tmp_path: Path, command: str
) -> None:
    """Each idiom is refused through a real inspection, not a bare regex probe."""
    repo, task = _copy_candidate(tmp_path / re.sub(r"[^a-z0-9]+", "-", command.lower()))
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + f"RUN {command}\n")
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert "build_network_use" in _codes(inspection)
    assert any(
        item.code == "build_network_use" and item.path == "tests/Dockerfile"
        for item in inspection.diagnostics
    )


@pytest.mark.parametrize(
    "command",
    [
        "npm init -y",
        "cargo build --offline",
        "python -m compileall /tests",
        "chmod +x /tests/test.sh",
        "mkdir -p /app/output",
    ],
)
def test_offline_build_commands_are_not_refused(tmp_path: Path, command: str) -> None:
    """The build pattern is still a denylist, so its false-refusal edge matters."""
    repo, task = _copy_candidate(tmp_path / re.sub(r"[^a-z0-9]+", "-", command.lower()))
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + f"RUN {command}\n")
    inspection = _inspect(repo, task)

    assert inspection.static_passed, _codes(inspection)


@pytest.mark.parametrize(
    ("addition", "location"),
    [
        ('[environment.mcp_servers.docs]\nurl = "https://example.invalid"\n',
         "environment.mcp_servers"),
        ('[verifier.environment]\nnetwork_mode = "no-network"\ndocker_image = "x@sha256:'
         + "a" * 64 + '"\n', "verifier.environment.docker_image"),
        ('[verifier.collect]\ncommand = "true"\n', "verifier.collect"),
        ('[[task.authors]]\nname = "Other"\naffiliation = "Somewhere"\n',
         "task.authors[1].affiliation"),
        ('multi_step_reward_strategy = "final"\n', "multi_step_reward_strategy"),
        ('[environment.healthcheck]\ntest = "true"\n', "environment.healthcheck"),
        # `[solution]` itself is now modelled, so the refusal that proves the
        # table is closed is an unknown key inside it, not the table's presence.
        ('[solution]\nseed = 1\n', "solution.seed"),
    ],
)
def test_unsupported_task_configuration_names_the_exact_offending_path(
    tmp_path: Path, addition: str, location: str
) -> None:
    """Anything outside the modelled surface is refused, by its exact dotted path."""
    repo, task = _copy_candidate(tmp_path / location.replace(".", "-").replace("[", "-"))
    config = (task / "task.toml").read_text()
    # A bare key appended to the end of the document would land in the last
    # table, so top-level keys go in front of the first table header.
    document = config + "\n" + addition if addition.startswith("[") else addition + config
    (task / "task.toml").write_text(document)
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    locations = {
        item.message.split(" is outside", 1)[0]
        for item in inspection.diagnostics
        if item.code == "unsupported_task_configuration"
    }
    assert location in locations


def test_reference_fixture_stays_inside_the_supported_configuration_surface(
    tmp_path: Path,
) -> None:
    """The allowlist must not refuse the task the workbench is built to certify."""
    repo, task = _copy_candidate(tmp_path / "surface")
    inspection = _inspect(repo, task)

    assert inspection.static_passed
    assert "unsupported_task_configuration" not in _codes(inspection)


# --- M009 F-06: the surface widened to the real task library ------------------


def test_inert_real_library_constructs_certify(tmp_path: Path) -> None:
    """The shape `library/tasks/` actually declares must certify (M009 F-06).

    Before this, `environment.mcp_servers = []`, `environment.os = "linux"`,
    `verifier.collect = []`, `gpus = 0`, and the empty `[environment.env]`,
    `[verifier.env]`, and `[solution.env]` tables were each refused as outside
    the modelled surface, which gave the workbench zero coverage of the four
    in-repo tasks. Every one of those declarations is inert in Harbor 0.21.0, so
    a document carrying all of them must be indistinguishable from one omitting
    them.
    """
    repo, task = _copy_candidate(tmp_path, "inert-surface")
    inspection = _inspect(repo, task)

    assert inspection.static_passed
    assert _codes(inspection) == set()


def test_inert_declarations_do_not_change_the_frozen_candidate_identity(
    tmp_path: Path,
) -> None:
    """Admitting the keys must not alter what the workbench certifies *about*.

    The inert document and the reference document differ only in declarations
    Harbor folds to the same effective configuration, so every derived judgement
    — the resolved network policy above all — must be identical. This is the
    assertion that would fail if a widened key silently fed the network mirror.
    """
    plain_repo, plain_task = _copy_candidate(tmp_path / "plain")
    inert_repo, inert_task = _copy_candidate(tmp_path / "inert", "inert-surface")

    plain = _inspect(plain_repo, plain_task).candidate
    inert = _inspect(inert_repo, inert_task).candidate

    assert inert["network_policy"] == plain["network_policy"]
    # The task bytes differ, so the package digest must differ; the point is that
    # nothing *derived from* the configuration does.
    assert inert["digests"]["package"] != plain["digests"]["package"]


@pytest.mark.parametrize(
    ("inject_after", "addition", "location"),
    [
        # environment.* — injected into the existing [environment] table, since
        # TOML forbids declaring the same table twice.
        ('network_mode = "no-network"', 'os = "windows"\n', "environment.os"),
        ('network_mode = "no-network"', 'os = "Windows"\n', "environment.os"),
        ('network_mode = "no-network"', "gpus = 1\n", "environment.gpus"),
        ('network_mode = "no-network"', "gpus = true\n", "environment.gpus"),
        ('network_mode = "no-network"', 'env = { TOKEN = "${TOKEN}" }\n', "environment.env"),
        ('network_mode = "no-network"', 'env = { SEED = "1" }\n', "environment.env"),
        # verifier.* and solution.* — appended as their own tables.
        (None, '[solution.env]\nSEED = "1"\n', "solution.env"),
        # The same value models must hold on the verifier side of the surface.
        (
            None,
            '[verifier.environment]\nnetwork_mode = "no-network"\nos = "windows"\n',
            "verifier.environment.os",
        ),
        (
            None,
            '[verifier.environment]\nnetwork_mode = "no-network"\n'
            'mcp_servers = [{ name = "docs", url = "https://example.invalid" }]\n',
            "verifier.environment.mcp_servers",
        ),
        (
            None,
            '[verifier.environment]\nnetwork_mode = "no-network"\nenv = { TOKEN = "x" }\n',
            "verifier.environment.env",
        ),
        (
            None,
            '[verifier.environment]\nnetwork_mode = "no-network"\ngpus = 2\n',
            "verifier.environment.gpus",
        ),
    ],
)
def test_a_widened_key_is_admitted_for_its_inert_value_only(
    tmp_path: Path, inject_after: str | None, addition: str, location: str
) -> None:
    """Each key widened for F-06 still refuses every value v1 cannot reproduce.

    This is the check the module's own rule demands arrive with each admitted
    key. Without it, widening the allowlist would reopen precisely the silent
    hole the allowlist exists to close: an inert `mcp_servers = []` and a
    populated one are the same key.
    """
    repo, task = _copy_candidate(
        tmp_path / re.sub(r"[^a-z0-9]+", "-", f"{location}-{addition}".lower())[:60]
    )
    config = (task / "task.toml").read_text()
    if inject_after is None:
        document = config + "\n" + addition
    else:
        assert config.count(inject_after) == 1
        document = config.replace(inject_after, inject_after + "\n" + addition.rstrip("\n"))
    (task / "task.toml").write_text(document)
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    refusals = {
        item.message.split(" is outside", 1)[0]: item
        for item in inspection.diagnostics
        if item.code == "unsupported_task_configuration"
    }
    assert location in refusals
    # A construct v1 declines to model is a limitation of the workbench, so the
    # task is left uncertifiable rather than blamed.
    assert refusals[location].classification == "harness_defect"
    assert refusals[location].severity == "error"


def test_admitted_values_are_inert_in_harbor_itself() -> None:
    """Pin "the admitted value is one Harbor folds away" against Harbor 0.21.0.

    Every refusal note in `_MODELLED_CONSTRUCT_VALUES` cites Harbor source, but
    the *acceptances* need the same standard: if `os = "linux"` or
    `mcp_servers = []` changed the resolved configuration, admitting them would
    be a hole rather than a widening. This resolves the documents through
    Harbor's own `TaskConfig` and compares.
    """
    reference = (VALID / "task.toml").read_text()
    assert reference.count('network_mode = "no-network"') == 1
    declared = (
        reference.replace(
            'network_mode = "no-network"',
            'network_mode = "no-network"\nos = "linux"\nmcp_servers = []\nenv = {}',
        )
        + "\n[verifier.env]\n\n[solution.env]\n"
    )
    plain, inert = _harbor_task_configs([reference, declared])

    # `os`, `mcp_servers`, and the three `env` tables are byte-identical after
    # resolution: Harbor's defaults are exactly the values the task library
    # spells out, so the declarations carry no information at all.
    assert inert["dump"] == plain["dump"]

    # `gpus = 0` is the one admitted value that is *not* field-identical to
    # omission — Harbor stores `0` where an absent key leaves `None` — so the
    # equivalence is asserted where it actually holds, at the fold Harbor applies
    # before using it. Admitting the key on a field-level comparison alone would
    # have been the unexamined step.
    with_gpus = reference.replace("cpus = 1", "cpus = 1\ngpus = 0", 1)
    plain_gpus, zero_gpus = _harbor_task_configs([reference, with_gpus])
    assert plain_gpus["dump"]["environment"]["gpus"] is None  # type: ignore[index]
    assert zero_gpus["dump"]["environment"]["gpus"] == 0  # type: ignore[index]
    assert zero_gpus["effective_gpus"] == plain_gpus["effective_gpus"] == 0


def test_every_key_admitted_for_one_value_arrives_with_that_value_model() -> None:
    """The module's binding rule, made executable.

    `SUPPORTED_TASK_CONFIG` is pinned as a literal so widening it cannot happen
    without editing this test, and every construct admitted for a single value
    must have a live `_MODELLED_CONSTRUCT_VALUES` entry. The reverse direction
    matters more: a value model whose spec no longer exists in the allowlist is
    a check that silently stopped running, which is how the two earlier review
    rounds each found a fresh hole.
    """
    assert set(_SUPPORTED_ENVIRONMENT_KEYS) == {
        "network_mode",
        "docker_image",
        "build_timeout_sec",
        "cpus",
        "memory_mb",
        "storage_mb",
        "os",
        "gpus",
        "mcp_servers",
        "env",
    }
    assert SUPPORTED_TASK_CONFIG[""] == {
        "schema_version",
        "artifacts",
        "task",
        "metadata",
        "agent",
        "verifier",
        "environment",
        "solution",
    }
    assert SUPPORTED_TASK_CONFIG["verifier"] == {
        "timeout_sec",
        "environment_mode",
        "network_mode",
        "environment",
        "collect",
        "env",
    }
    assert SUPPORTED_TASK_CONFIG["environment.mcp_servers"] == {"name", "transport", "url"}
    assert SUPPORTED_TASK_CONFIG["verifier.collect"] == {"command", "service"}
    assert SUPPORTED_TASK_CONFIG["verifier.env"] is None
    assert SUPPORTED_TASK_CONFIG["solution"] == {"env"}

    # Every key admitted for exactly one value carries the model that decides it.
    admitted_for_one_value = {
        "environment.os",
        "environment.gpus",
        "environment.env",
        "verifier.environment.os",
        "verifier.environment.gpus",
        "verifier.environment.mcp_servers",
        "verifier.environment.env",
        "solution.env",
    }
    assert set(_MODELLED_CONSTRUCT_VALUES) == admitted_for_one_value

    # No model is dead: each spec is a key the allowlist actually admits, so a
    # rename cannot leave the check unreachable.
    for spec in _MODELLED_CONSTRUCT_VALUES:
        parent, _, key = spec.rpartition(".")
        allowed = SUPPORTED_TASK_CONFIG[parent]
        assert allowed is not None and key in allowed, spec

    # Each note cites the Harbor behaviour that makes the refusal a fact rather
    # than a preference.
    for spec, model in _MODELLED_CONSTRUCT_VALUES.items():
        assert re.search(r"\.py:\d+", model.note), spec


def test_deprecated_allow_internet_alias_is_still_refused(tmp_path: Path) -> None:
    """A network-policy alias v1 does not mirror stays refused after the widening.

    `library/tasks/query-optimize` declares `allow_internet = true` with no
    `network_mode`, so Harbor's validator sets the policy to `public` behind the
    workbench's back. Mirroring that would mean a second network resolver beside
    `_effective_verifier_network`; the task states its policy explicitly instead.
    """
    repo, task = _copy_candidate(tmp_path / "allow-internet")
    config = (task / "task.toml").read_text()
    (task / "task.toml").write_text(
        config.replace('network_mode = "no-network"', "allow_internet = false", 1)
    )
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    refusal = next(
        item
        for item in inspection.diagnostics
        if item.code == "unsupported_task_configuration"
    )
    assert refusal.message.startswith("environment.allow_internet is outside")
    assert "Declare network_mode explicitly instead" in refusal.message


def _verifier_network_documents() -> list[tuple[str, str, str, str]]:
    """`(label, task.toml, expected baseline, expected phase)` for the mirror.

    The expected pairs are written out as literals rather than derived, so the
    mirror's own behaviour is pinned with nothing installed. Harbor is a
    standalone CLI tool and not a dependency of this package, so a pin that only
    runs when a `harbor` binary is on PATH does not run in CI at all — which is
    precisely where a regression would land unnoticed.
    """
    valid = (VALID / "task.toml").read_text()
    return [
        ("compliant fixture", valid, "no-network", "no-network"),
        # A [verifier.environment] table that omits network_mode does not inherit
        # [environment]; EnvironmentConfig defaults it to public.
        (
            "[verifier.environment] without network_mode does not inherit",
            valid + "\n[verifier.environment]\ncpus = 1\n",
            "public",
            "public",
        ),
        (
            "[verifier.environment] overrides a public [environment]",
            valid.replace('network_mode = "no-network"', 'network_mode = "public"')
            + '\n[verifier.environment]\nnetwork_mode = "no-network"\n',
            "no-network",
            "no-network",
        ),
        (
            "[verifier].network_mode reopens the verification phase only",
            valid.replace(
                "[verifier]\ntimeout_sec", '[verifier]\nnetwork_mode = "public"\ntimeout_sec'
            ),
            "no-network",
            "public",
        ),
    ]


def test_verifier_network_resolution_is_pinned_statically() -> None:
    """Pin the mirror with no Harbor present, so CI actually executes the pin.

    This asserts the four `(baseline, phase)` pairs the mirror must produce and
    nothing else. It cannot detect drift in Harbor — only
    `test_verifier_network_resolution_matches_harbor` can — but it does detect
    drift in `_effective_verifier_network`, and it does so unconditionally.
    """
    for label, document, baseline, phase in _verifier_network_documents():
        assert _effective_verifier_network(tomllib.loads(document))[:2] == (
            baseline,
            phase,
        ), label

    # The step fixture is refused by the configuration allowlist rather than
    # mirrored, and this is why: the mirror reads its compliant task-level tables
    # while Harbor resolves the step's verifier to full egress.
    step_case = (CASES / "step-verifier-network" / "task.toml").read_text()
    assert _effective_verifier_network(tomllib.loads(step_case))[:2] == (
        "no-network",
        "no-network",
    )


def test_verifier_network_resolution_matches_harbor() -> None:
    """Pin the mirror against the resolver it mirrors, so the two cannot drift.

    Harbor ships as a standalone CLI rather than a library this package imports,
    so `_effective_verifier_network` reproduces its resolution instead of calling
    it. This test runs the real
    `resolve_effective_verifier_env_config` / `resolve_baseline` /
    `resolve_verifier_phase_policy` inside the installed Harbor's own
    interpreter and fails if the reproduction stops agreeing. It skips without
    Harbor; `test_verifier_network_resolution_is_pinned_statically` is the part
    that always runs.
    """
    cases = _verifier_network_documents()
    step_case = (CASES / "step-verifier-network" / "task.toml").read_text()
    resolved = _harbor_resolution([document for _, document, _, _ in cases] + [step_case])

    for (label, _document, baseline, phase), harbor in zip(
        cases, resolved[: len(cases)], strict=True
    ):
        assert harbor == [[baseline, phase]], label

    # Harbor resolves the step fixture's verifier to full egress even though
    # every task-level table is compliant, which is why [[steps]] is refused.
    assert resolved[-1] == [["public", "public"]]


def test_prebuilt_docker_image_bypassing_the_reviewed_build_is_rejected(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path / "prebuilt")
    config = (task / "task.toml").read_text().replace(
        '[environment]\n',
        '[environment]\ndocker_image = "ubuntu@sha256:' + "a" * 64 + '"\n',
    )
    (task / "task.toml").write_text(config)
    inspection = _inspect(repo, task)

    assert not inspection.static_passed
    assert "prebuilt_image_unsupported" in _codes(inspection)


def test_network_overlay_denies_the_build_network_not_only_the_runtime(
    tmp_path: Path,
) -> None:
    """Compose's build network is services.<name>.build.network, not network_mode."""
    overlay = yaml.safe_load(NETWORK_OVERLAY_CONTENT)
    main = overlay["services"]["main"]

    assert main["build"]["network"] == "none"
    assert main["network_mode"] == "none"

    repo, task = _copy_candidate(tmp_path)
    candidate = _inspect(repo, task).candidate
    assert candidate["network_policy"]["agent_build_network"] == (
        "denied by overlay build.network=none"
    )
    assert candidate["network_policy"]["control_overlay_digest"] == _digest(
        NETWORK_OVERLAY_CONTENT
    )


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


def test_controls_bind_inspection_path_and_current_candidate_bytes(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    backend = FixtureBackend()
    other = repo / "library/synthetic/m007/other-copy"
    shutil.copytree(task, other)

    with pytest.raises(WorkbenchError, match="Inspection path"):
        run_controls(
            inspection=inspection,
            repo_root=repo,
            task_path=other,
            backend=backend,
        )
    assert backend.calls == []
    assert not (repo / "runs").exists()

    (task / "environment/input.txt").write_text("drifted after inspection\n")
    with pytest.raises(WorkbenchError, match="drifted after Inspection"):
        run_controls(
            inspection=inspection,
            repo_root=repo,
            task_path=task,
            backend=backend,
        )
    assert backend.calls == []
    assert not (repo / "runs").exists()


def test_harbor_backend_rejects_drifted_candidate_and_command_before_staging(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    calls: list[list[str]] = []
    backend = HarborControlBackend(
        command_runner=lambda command, **kwargs: calls.append(command),
        environment_provider=lambda: {},
    )
    run_root = repo / "runs/task-workbench" / inspection.candidate["candidate_id"]
    drifted_plan = replace(
        inspection.control_plan[0],
        command=(*inspection.control_plan[0].command, "--model", "forbidden"),
    )

    with pytest.raises(WorkbenchError, match="control command drifted"):
        backend.run(
            repo_root=repo,
            task_dir=task,
            candidate=inspection.candidate,
            plan=drifted_plan,
            run_root=run_root,
        )
    assert calls == []
    assert not run_root.exists()

    drifted_candidate = dict(inspection.candidate)
    drifted_candidate["task_path"] = "library/synthetic/m007/other"
    with pytest.raises(WorkbenchError, match="candidate record digest"):
        backend.run(
            repo_root=repo,
            task_dir=task,
            candidate=drifted_candidate,
            plan=inspection.control_plan[0],
            run_root=run_root,
        )
    assert calls == []
    assert not run_root.exists()


def test_fair_alternative_must_be_byte_distinct_and_please_hack_replay_exists(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    (task / "workbench/fair-alternative.sh").write_bytes(
        (task / "solution/solve.sh").read_bytes()
    )
    assert "fair_alternative_not_distinct" in _codes(_inspect(repo, task))

    repo2, task2 = _copy_candidate(tmp_path / "missing-hack")
    (task2 / "workbench/please-hack.sh").unlink()
    assert "required_control_missing" in _codes(_inspect(repo2, task2))


def test_second_nop_flake_and_successful_please_hack_are_failures(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    valid = _bundle(inspection, repo=repo, task=task)
    observations = list(valid.observations)
    nop_index = next(
        index for index, item in enumerate(observations) if item.control_id == "nop-2"
    )
    observations[nop_index] = replace(
        observations[nop_index], reward=1.0, reward_vector={"reward": 1.0}
    )
    flaky = ControlBundle.build(
        candidate_id=valid.candidate_id,
        source_package_digest=valid.source_package_digest,
        observations=observations,
    )
    assert "verifier_permissive" in {
        item.code for item in check_candidate(inspection, flaky, repo_root=repo).diagnostics
    }

    observations = list(valid.observations)
    hack_index = next(
        index for index, item in enumerate(observations) if item.control_id == "please-hack"
    )
    observations[hack_index] = replace(
        observations[hack_index], reward=1.0, reward_vector={"reward": 1.0}
    )
    hacked = ControlBundle.build(
        candidate_id=valid.candidate_id,
        source_package_digest=valid.source_package_digest,
        observations=observations,
    )
    report = check_candidate(inspection, hacked, repo_root=repo)
    _, packet = write_packet(repo_root=repo, report=report)
    certificate = json.loads(packet.read_text())
    assert report.disposition == "needs_changes"
    assert certificate["check_vector"]["hack_detected"] is True
    assert any(
        item["control_id"] == "please-hack" for item in certificate["retained_replays"]
    )


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

    assert len(backend.calls) == 10
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


def test_verifier_determinism_uses_retained_output_files(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    bundle = _bundle(inspection, repo=repo, task=task, case="nondeterminism")
    first, second = bundle.observations[:2]
    assert first.job_path is not None
    assert second.job_path is not None
    first_trial = next(path for path in (repo / first.job_path).iterdir() if path.is_dir())
    second_trial = next(path for path in (repo / second.job_path).iterdir() if path.is_dir())

    assert first.verifier_output_digest == _verifier_output_digest(first_trial)
    assert second.verifier_output_digest == _verifier_output_digest(second_trial)
    assert first.verifier_output_digest != second.verifier_output_digest


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
    trial_result["verifier_environment_mode"] = "same"
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
    assert "control_verifier_not_isolated" in codes

    _, certification_path = write_packet(repo_root=repo, report=report)
    vector = json.loads(certification_path.read_text())["check_vector"]
    assert vector["oracle_exact_1_x3"] is False
    assert vector["nop_exact_0_x2"] is True
    assert vector["invalid_outputs_rejected"] is True
    assert vector["oracle_stable_output"] is False
    assert vector["isolation"] is False


def test_stale_control_bundle_zeroes_every_certificate_claim(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    valid = _bundle(inspection, repo=repo, task=task)
    stale = ControlBundle.build(
        candidate_id="candidate-" + "0" * 24,
        source_package_digest=valid.source_package_digest,
        observations=valid.observations,
    )
    report = check_candidate(inspection, stale, repo_root=repo)
    _, packet = write_packet(repo_root=repo, report=report)
    certificate = json.loads(packet.read_text())
    assert "control_source_stale" in {item.code for item in report.diagnostics}
    assert not any(certificate["check_vector"].values())
    assert all(
        axis["status"] != "passed" and axis["evidence"] == []
        for axis in certificate["axes"].values()
    )


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
    assert len(first_evidence) == 10
    assert {
        path.relative_to(repo).as_posix(): path.read_bytes()


        for path in sorted(first_paths[0].parent.joinpath("evidence").glob("*.json"))
    } == first_evidence
    candidate = json.loads(first_paths[0].read_text())
    certification = json.loads(first_paths[1].read_text())
    assert candidate["admission_boundary"]["can_register"] is False
    assert certification["admission_granted"] is False
    assert certification["certified"] is True
    assert len(certification["retained_evidence"]) == 10
    assert all(
        (repo / item["path"]).is_file() for item in certification["retained_evidence"]
    )
    assert len(certification["retained_replays"]) == 2
    assert certification["axes"]["realism_review"]["status"] == "not_assessed"
    assert certification["axes"]["difficulty_calibration"]["status"] == "not_applicable"
    assert "ALPHA-BETA-GAMMA" not in b"".join(first_evidence.values()).decode()

    first_paths[1].write_text("tampered\n")
    with pytest.raises(PacketConflictError, match="non-identical"):
        write_packet(repo_root=repo, report=report)


def test_packet_can_write_isolated_root_under_candidate_boundary(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    report = check_candidate(
        inspection,
        _bundle(inspection, repo=repo, task=task),
        repo_root=repo,
    )
    _, canonical_certification = write_packet(repo_root=repo, report=report)
    canonical_certification.write_text("occupied by a different packet\n")

    isolated_root = repo / "research/registration/candidates/ci/run-123/easy"
    candidate_path, certification_path = write_packet(
        repo_root=repo,
        report=report,
        output_root=isolated_root,
    )

    packet_dir = isolated_root / inspection.candidate["candidate_id"]
    assert candidate_path == packet_dir / "candidate.json"
    assert certification_path == packet_dir / "certification.json"
    assert json.loads(certification_path.read_text())["certified"] is True


def test_certificate_binds_registry_reload_and_rejects_tamper_replay_and_circularity(
    tmp_path: Path,
) -> None:
    repo, task = _copy_candidate(tmp_path)
    inspection = _inspect(repo, task)
    bundle = _bundle(inspection, repo=repo, task=task)
    report = check_candidate(inspection, bundle, repo_root=repo)
    _, certification_path = write_packet(repo_root=repo, report=report)
    record = _bound_registry_record(repo, task, certification_path)
    registry_dir = repo / "library/registry"
    registry_dir.mkdir(parents=True)
    record_path = registry_dir / "uppercase-fixture.json"
    record_path.write_text(record.model_dump_json(indent=2) + "\n")
    policy = repo / "policy/canary-suite.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text("version: 1\nmembers:\n  []\n")
    inventory = repo / "research/registration/inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(json.dumps(inventory_tasks(repo).to_dict(), indent=2) + "\n")

    reloaded = TaskRegistry.from_repo(repo).get("uppercase-fixture")
    assert reloaded is not None
    verify_certification_packet(repo, reloaded)
    assert audit_registry(repo).passed

    original = certification_path.read_bytes()
    certification_path.write_bytes(original + b" ")
    with pytest.raises(TaskCertificationError, match="envelope|packet bytes"):
        verify_certification_packet(repo, reloaded)
    certification_path.write_bytes(original)

    with pytest.raises(TaskCertificationError, match="replay/identity mismatch"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id="another-task",
            task_version="1.0.0",
            task_path=task.relative_to(repo).as_posix(),
            package_digest=record.digests.package,
        )

    def rewrite(body: dict[str, object]) -> None:
        body.pop("certification_id", None)
        body["certification_id"] = "cert-" + hashlib.sha256(_canonical(body)).hexdigest()[:24]
        certification_path.write_bytes(_canonical(body))

    circular = json.loads(original)
    circular["generator_identity"] = circular["validator_identity"]
    rewrite(circular)
    with pytest.raises(TaskCertificationError, match="circular"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )

    missing_replay = json.loads(original)
    missing_replay["retained_replays"] = [
        item
        for item in missing_replay["retained_replays"]
        if item["control_id"] != "please-hack"
    ]
    rewrite(missing_replay)
    with pytest.raises(TaskCertificationError, match="please-hack"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )

    contradictory_vector = json.loads(original)
    contradictory_vector["check_vector"]["oracle_exact_1_x3"] = False
    rewrite(contradictory_vector)
    with pytest.raises(TaskCertificationError, match="check vector contradicts"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )

    contradictory_summary = json.loads(original)
    contradictory_summary["control_summary"]["oracle_runs"] = 99
    rewrite(contradictory_summary)
    with pytest.raises(TaskCertificationError, match="control summary contradicts"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )

    unsupported = json.loads(original)
    unsupported["workbench_version"] = "m999-v1"
    rewrite(unsupported)
    with pytest.raises(TaskCertificationError, match="unsupported task workbench"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )

    semantic_tamper = json.loads(original)
    semantic_tamper["diagnostics"].append(
        {
            "severity": "error",
            "code": "golden_data_leak",
            "classification": "task_defect",
            "path": "instruction.md",
            "message": "semantic tamper",
        }
    )
    rewrite(semantic_tamper)
    with pytest.raises(TaskCertificationError, match="check vector contradicts"):
        certification_envelope_from_packet(
            repo,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
        )


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
        "all_controls_completed": False,
        "fair_alternative_exact_1": False,
        "hack_detected": False,
        "invalid_outputs_rejected": False,
        "isolation": False,
        "leakage_scan_clean": False,
        "nop_exact_0_x2": False,
        "oracle_exact_1_x3": False,
        "oracle_stable_output": False,
        "please_hack_executed": False,
        "static": False,
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
    assert certification["check_vector"]["isolation"] is False
    assert certification["check_vector"]["oracle_exact_1_x3"] is False
    assert certification["check_vector"]["nop_exact_0_x2"] is False
    assert certification["check_vector"]["invalid_outputs_rejected"] is False
    assert certification["check_vector"]["oracle_stable_output"] is False


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


def test_cli_run_controls_composes_fixed_harbor_subprocess_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, task = _copy_candidate(tmp_path)
    captured: list[list[str]] = []

    def runner(command, **kwargs):
        captured.append(command)
        dataset = Path(command[command.index("--path") + 1])
        included_task = command[command.index("--include-task-name") + 1]
        stage = dataset / included_task
        overlay = Path(command[command.index("--extra-docker-compose") + 1])
        assert dataset.is_dir()
        assert included_task in {path.name for path in dataset.iterdir()}
        assert stage.is_dir()
        assert overlay == stage / NETWORK_OVERLAY_RELATIVE
        assert overlay.read_bytes() == NETWORK_OVERLAY_CONTENT
        assert not any(path.is_symlink() for path in stage.rglob("*"))
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="injected harness stop before Docker",
        )

    backend = HarborControlBackend(command_runner=runner, environment_provider=lambda: {})
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

    assert run_cli(
        ["check", *common, "--run-controls"],
        control_backend=backend,
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["disposition"] == "harness_blocked"
    assert len(captured) == 10
    assert {command[command.index("--agent") + 1] for command in captured} == {
        "oracle",
        "nop",
    }
    assert all(command[command.index("--env") + 1] == "docker" for command in captured)
    assert all(command[command.index("--n-concurrent") + 1] == "1" for command in captured)
    assert all(command[command.index("--n-attempts") + 1] == "1" for command in captured)
    assert all("--model" not in command for command in captured)




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

# --- V2: Local MCP, Compose, Collect Hooks, Credentials, Offline Build Proofs ---


def test_v2_mcp_servers_accepted_and_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "mcp-base")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
    )

    stdio_task = task / "task.toml"
    base_toml = stdio_task.read_text()

    # 1a. Transport is stdio -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "stdio", url = "http://mcp-server:8000/mcp" }]',
        )
    )
    stdio_insp = _inspect(repo, task)
    assert not stdio_insp.static_passed
    assert "mcp_transport_unsupported" in _codes(stdio_insp)

    # 1b. Transport is sse -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "sse", url = "http://mcp-server:8000/mcp" }]',
        )
    )
    sse_insp = _inspect(repo, task)
    assert not sse_insp.static_passed
    assert "mcp_transport_unsupported" in _codes(sse_insp)

    # 1c. Scheme is https -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "https://mcp-server:8000/mcp" }]',
        )
    )
    https_insp = _inspect(repo, task)
    assert not https_insp.static_passed
    assert "mcp_url_scheme_invalid" in _codes(https_insp)

    # 1d. Host is localhost / 127.0.0.1 -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://localhost:8000/mcp" }]',
        )
    )
    lh_insp = _inspect(repo, task)
    assert not lh_insp.static_passed
    assert "mcp_server_host_invalid" in _codes(lh_insp)

    # 1e. Host is unbound (names undeclared compose service) -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://other-service:8000/mcp" }]',
        )
    )
    unbound_insp = _inspect(repo, task)
    assert not unbound_insp.static_passed
    assert "mcp_server_unbound" in _codes(unbound_insp)

    # 1f. Port missing -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://mcp-server/mcp" }]',
        )
    )
    noport_insp = _inspect(repo, task)
    assert not noport_insp.static_passed
    assert "mcp_url_port_missing" in _codes(noport_insp)

    # 1g. Path missing -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://mcp-server:8000/" }]',
        )
    )
    nopath_insp = _inspect(repo, task)
    assert not nopath_insp.static_passed
    assert "mcp_url_path_missing" in _codes(nopath_insp)

    # 1h. URL auth -> rejected
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://user:pass@mcp-server:8000/mcp" }]',
        )
    )
    auth_insp = _inspect(repo, task)
    assert not auth_insp.static_passed
    assert "mcp_url_auth_invalid" in _codes(auth_insp)

    # 2. Accepted path: streamable-http with matching compose sidecar
    stdio_task.write_text(
        base_toml.replace(
            "storage_mb = 512",
            'storage_mb = 512\nmcp_servers = [{ name = "local", transport = "streamable-http", url = "http://mcp-server:8000/mcp" }]',
        )
    )
    accepted_insp = _inspect(repo, task)
    assert accepted_insp.static_passed, _codes(accepted_insp)
    assert accepted_insp.candidate.get("mcp_servers") == [
        {
            "name": "local",
            "transport": "streamable-http",
            "url": "http://mcp-server:8000/mcp",
            "host": "mcp-server",
            "port": 8000,
            "path": "/mcp",
        }
    ]


def test_v2_compose_topology_accepted_and_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "compose-base")
    compose_path = task / "environment/docker-compose.yaml"

    # 1. Negative control: top-level custom networks (missing internal: true)
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "networks:\n"
        "  custom:\n"
    )
    assert "compose_networks_unsupported" in _codes(_inspect(repo, task))

    # 2. Negative control: service network_mode
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    network_mode: host\n"
    )
    assert "custom_compose_unsupported" in _codes(_inspect(repo, task))

    # 3. Negative control: service custom networks
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    networks:\n"
        "      - default\n"
    )
    assert "compose_networks_unsupported" in _codes(_inspect(repo, task))

    # 4. Negative control: published host ports
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    ports:\n"
        '      - "8080:8080"\n'
    )
    assert "compose_host_ports_unsupported" in _codes(_inspect(repo, task))

    # 5. Negative control: privileged: true
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    privileged: true\n"
    )
    assert "compose_privileged_unsupported" in _codes(_inspect(repo, task))

    # 6. Negative control: missing main service
    compose_path.write_text(
        "services:\n"
        "  worker:\n"
        "    build: .\n"
    )
    assert "compose_main_service_missing" in _codes(_inspect(repo, task))

    # 7. Negative control: 3 services
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  sidecar1:\n"
        "    build: .\n"
        "  sidecar2:\n"
        "    build: .\n"
    )
    assert "compose_topology_invalid" in _codes(_inspect(repo, task))

    # 8. Negative control: build context escaping environment
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: ../solution\n"
    )
    assert "compose_build_path_escape" in _codes(_inspect(repo, task))

    # 9. Negative control: unpinned image ref
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  sidecar:\n"
        '    image: "mcp-service:latest"\n'
    )
    assert "compose_image_unpinned" in _codes(_inspect(repo, task))

    # 10. Accepted path: main (build: .) + sidecar (build: .)
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
    )
    insp = _inspect(repo, task)
    assert insp.static_passed, _codes(insp)
    assert "compose_topology" in insp.candidate
    overlay = yaml.safe_load(_candidate_network_overlay(insp.candidate))
    assert overlay["networks"]["workbench-internal"]["internal"] is True
    assert set(overlay["services"]) == {"main", "mcp-server"}
    assert overlay["services"]["main"]["networks"] == ["workbench-internal"]
    assert overlay["services"]["mcp-server"]["networks"] == ["workbench-internal"]

    # 11. Accepted path: main + sidecar with a task-declared internal bridge
    compose_path.write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "  mcp-server:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "networks:\n"
        "  mcp-net:\n"
        "    internal: true\n"
    )
    insp = _inspect(repo, task)
    assert insp.static_passed, _codes(insp)
    assert insp.candidate["compose_topology"]["network"] == {
        "name": "mcp-net",
        "internal": True,
    }
    overlay = yaml.safe_load(_candidate_network_overlay(insp.candidate))
    assert overlay["networks"]["mcp-net"]["internal"] is True
    assert set(overlay["services"]) == {"main", "mcp-server"}
    assert overlay["services"]["main"]["networks"] == ["mcp-net"]
    assert overlay["services"]["mcp-server"]["networks"] == ["mcp-net"]


@pytest.mark.parametrize(
    ("compose_text", "expected_code"),
    [
        # top-level network forbidden or malformed definitions
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    external: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    driver: overlay\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    driver_opts:\n"
            "      foo: bar\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    name: custom_name\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    ipam:\n"
            "      driver: default\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    attachable: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    labels:\n"
            "      - key=value\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    enable_ipv6: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: false\n",
            "compose_networks_unsupported",
        ),
        # more than one network or invalid name
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n"
            "  other:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  MCP-NET:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp.net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        # top-level network declared but a service is not attached
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "  mcp-server:\n"
            "    build: .\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        # service attaches the wrong or undeclared network
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - wrong-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        # service-level network options are forbidden
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      mcp-net:\n"
            "        aliases:\n"
            "          - alias1\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      mcp-net:\n"
            "        ipv4_address: 10.0.0.2\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        # network_mode together with networks is rejected
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    network_mode: bridge\n"
            "    networks:\n"
            "      - mcp-net\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "custom_compose_unsupported",
        ),
        # no top-level network but service declares networks
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      - default\n"
            "  mcp-server:\n"
            "    build: .\n",
            "compose_networks_unsupported",
        ),
        # top-level network requires a sidecar
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
        # service networks list with multiple entries
        (
            "services:\n"
            "  main:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "      - other\n"
            "  mcp-server:\n"
            "    build: .\n"
            "    networks:\n"
            "      - mcp-net\n"
            "networks:\n"
            "  mcp-net:\n"
            "    internal: true\n",
            "compose_networks_unsupported",
        ),
    ],
)
def test_v2_compose_networks_topology_rejected_cases(
    tmp_path: Path, compose_text: str, expected_code: str
) -> None:
    repo, task = _copy_candidate(tmp_path / "networks-rejected")
    (task / "environment/docker-compose.yaml").write_text(compose_text)
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert expected_code in _codes(inspection)


@pytest.mark.parametrize(
    "net_key",
    ["123", "true", "null"],
)
def test_v2_rejects_non_string_network_key(tmp_path: Path, net_key: str) -> None:
    repo, task = _copy_candidate(tmp_path / f"non-string-net-key-{net_key}")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
        "networks:\n"
        f"  {net_key}:\n"
        "    internal: true\n"
    )
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert "compose_networks_unsupported" in _codes(inspection)
    assert any(
        "is not a safe task-local name" in item.message
        for item in inspection.diagnostics
    )


def test_v2_rejects_non_string_volume_key(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "non-string-vol-key")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
        "volumes:\n"
        "  123:\n"
    )
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert "compose_volume_invalid" in _codes(inspection)
    assert any(
        "is not a safe task-local name" in item.message
        for item in inspection.diagnostics
    )


@pytest.mark.parametrize(
    "networks_fragment",
    [
        "    networks:\n"
        "      mcp-net:\n",
        "    networks:\n"
        "      mcp-net:\n"
        "        aliases:\n"
        "          - alias1\n",
        "    networks:\n"
        "      mcp-net: {}\n",
    ],
)
def test_v2_rejects_service_networks_mapping_form(
    tmp_path: Path, networks_fragment: str
) -> None:
    repo, task = _copy_candidate(tmp_path / "mapping-form")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        + networks_fragment
        + "  mcp-server:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "networks:\n"
        "  mcp-net:\n"
        "    internal: true\n"
    )
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert "compose_networks_unsupported" in _codes(inspection)
    assert any(
        "must be a single-item list containing 'mcp-net'" in item.message
        for item in inspection.diagnostics
    )


def test_v2_docker_compose_config_merge_smoke(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("docker compose is not available")

    base_dir = tmp_path / "merge-smoke"
    base_dir.mkdir()
    (base_dir / "Dockerfile").write_text("FROM scratch\n")
    (base_dir / "base.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "  mcp-server:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "networks:\n"
        "  mcp-net:\n"
    )
    candidate = {
        "compose_topology": {
            "sidecar_service": "mcp-server",
            "network": {"name": "mcp-net"},
        }
    }
    (base_dir / "overlay.yaml").write_bytes(_candidate_network_overlay(candidate))

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(base_dir / "base.yaml"),
            "-f",
            str(base_dir / "overlay.yaml"),
            "config",
        ],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    merged = yaml.safe_load(result.stdout)
    for service_name in ("main", "mcp-server"):
        service = merged["services"][service_name]
        context = service["build"]["context"]
        assert context == "." or Path(context).is_absolute()
        assert service["build"]["network"] == "none"
        assert set(service["networks"]) == {"mcp-net"}
    network = merged["networks"]["mcp-net"]
    assert network["internal"] is True


@pytest.mark.parametrize(
    ("fragment", "expected_code"),
    [
        ("    volumes:\n      - /:/host\n", "compose_volume_escape"),
        ("    command: curl https://example.invalid\n", "compose_service_key_unsupported"),
        ("    entrypoint: /bin/sh\n", "compose_service_key_unsupported"),
        ("    cap_add:\n      - SYS_ADMIN\n", "compose_service_key_unsupported"),
        ("    devices:\n      - /dev/null:/dev/escape\n", "compose_service_key_unsupported"),
        ("    env_file: .env\n", "compose_service_key_unsupported"),
        ("    environment:\n      SECRET: value\n", "compose_sidecar_env_invalid"),
    ],
)
def test_v2_compose_rejects_unmodelled_service_execution_surfaces(
    tmp_path: Path, fragment: str, expected_code: str
) -> None:
    repo, task = _copy_candidate(tmp_path / "compose-service-surface")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
        + fragment
        + "volumes:\n"
        "  tau3-logs:\n"
    )
    assert expected_code in _codes(_inspect(repo, task))


def test_v2_compose_scans_nested_sidecar_dockerfile(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "compose-nested-docker")
    sidecar = task / "environment/sidecar"
    sidecar.mkdir()
    (sidecar / "Dockerfile").write_text("FROM alpine:latest\n")
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: ./sidecar\n"
    )
    assert "compose_image_unpinned" in _codes(_inspect(repo, task))


def test_v2_verifier_collect_accepted_and_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "collect-base")
    task_toml = task / "task.toml"
    base_toml = task_toml.read_text()

    # 1. Negative control: service is not main
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "mcp-server"\ncommand = "cp /logs/out.txt /app/output/result.txt"\n'
    )
    insp = _inspect(repo, task)
    assert "collect_service_invalid" in _codes(insp)

    # 2. Negative control: arbitrary shell command (pg_dump)
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "pg_dump > /tmp/state.sql"\n'
    )
    insp = _inspect(repo, task)
    assert "verifier_collect_unsupported" in _codes(insp)

    # 3. Negative control: pipeline
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "cat /logs/a | tee /app/output/result.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 4. Negative control: path traversal in source
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "cp /logs/../etc/passwd /app/output/result.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 5. Negative control: globs in source
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "cp /logs/*.txt /app/output/result.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 5b. Negative control: shell command substitution in a path
    task_toml.write_text(
        base_toml
        + '\n[[verifier.collect]]\nservice = "main"\n'
        + 'command = "cp /logs/$(id).txt /app/output/result.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 5c. Negative control: backtick substitution in a path
    task_toml.write_text(
        base_toml
        + '\n[[verifier.collect]]\nservice = "main"\n'
        + 'command = "cp /logs/out.txt /app/output/`id`.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 6. Negative control: destination not in artifacts
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "cp /logs/out.txt /app/undeclared.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 7. Negative control: source exposes forbidden solution/tests
    task_toml.write_text(
        base_toml + '\n[[verifier.collect]]\nservice = "main"\ncommand = "cp /solution/solve.sh /app/output/result.txt"\n'
    )
    assert "verifier_collect_unsupported" in _codes(_inspect(repo, task))

    # 8. Accepted path: guard form
    task_toml.write_text(
        base_toml + (
            '\n[[verifier.collect]]\n'
            'service = "main"\n'
            'command = "if [ ! -f /app/output/result.txt ] && [ -f /logs/agent/result.txt ]; then cp /logs/agent/result.txt /app/output/result.txt; fi"\n'
        )
    )
    insp = _inspect(repo, task)
    assert insp.static_passed, _codes(insp)
    assert "collect_hooks" in insp.candidate


def test_v2_verifier_env_credentials_accepted_and_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "env-creds")
    task_toml = task / "task.toml"
    base_toml = task_toml.read_text()

    source_with_creds = CandidateSource(
        source_uri="https://example.invalid/eval-lab/uppercase-fixture.git",
        source_ref="v1.0.0",
        license="MIT",
        provenance_zone="03-synthetic",
        credentials=("SIMULATED_USER", "OPENAI_API_KEY"),
    )

    # 1. Negative control: literal secret in verifier.env
    task_toml.write_text(base_toml + '\n[verifier.env]\nOPENAI_API_KEY = "sk-1234567890abcdef"\n')
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert "verifier_env_literal_secret" in _codes(insp)

    # 2. Negative control: default expression containing secret
    task_toml.write_text(base_toml + '\n[verifier.env]\nOPENAI_API_KEY = "${OPENAI_API_KEY:-fallback_secret}"\n')
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert "verifier_env_literal_secret" in _codes(insp)

    # 3. Negative control: unauthorized credential not in source metadata allowlist
    task_toml.write_text(base_toml + '\n[verifier.env]\nUNAUTHORIZED = "${UNAUTHORIZED}"\n')
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert "verifier_credential_unauthorized" in _codes(insp)

    # 3b. Negative control: authorized container name aliases an unauthorized host variable
    task_toml.write_text(
        base_toml + '\n[verifier.env]\nOPENAI_API_KEY = "${UNAUTHORIZED}"\n'
    )
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert "verifier_credential_unauthorized" in _codes(insp)

    # 3c. Negative control: credential aliases are refused even when both names are authorized
    task_toml.write_text(
        base_toml + '\n[verifier.env]\nOPENAI_API_KEY = "${SIMULATED_USER}"\n'
    )
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert "verifier_credential_alias_unsupported" in _codes(insp)

    # 4. Negative control: credential in environment.env (leak to agent)
    task_toml.write_text(
        base_toml.replace('network_mode = "no-network"', 'network_mode = "no-network"\nenv = { API_KEY = "${OPENAI_API_KEY}" }')
    )
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert not insp.static_passed

    # 5. Negative control: credential in solution.env
    task_toml.write_text(base_toml + '\n[solution.env]\nAPI_KEY = "${OPENAI_API_KEY}"\n')
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert not insp.static_passed

    # 6. Accepted path: authorized placeholder in verifier.env
    task_toml.write_text(base_toml + '\n[verifier.env]\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\n')
    insp = inspect_candidate(repo_root=repo, task_path=task, source=source_with_creds)
    assert insp.static_passed, _codes(insp)
    assert insp.candidate.get("credentials") == ["SIMULATED_USER", "OPENAI_API_KEY"]


def test_v2_offline_build_proof_accepted_and_rejected(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "proof-base")
    tests_dockerfile = task / "tests/Dockerfile"
    base_docker = tests_dockerfile.read_text()

    # 1. Negative control: package manager (uv sync) without proof
    tests_dockerfile.write_text(base_docker + "\nRUN uv sync --frozen --offline\n")
    insp = _inspect(repo, task)
    assert "build_network_use" in _codes(insp)

    # 2. Negative control: malformed proof JSON
    (task / "tests/build-proof.json").write_text("{malformed-json")
    insp = _inspect(repo, task)
    assert "build_proof_invalid" in _codes(insp)

    # 3. Negative control: missing lockfile
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "nonexistent.lock",
            "lockfile_digest": "sha256:" + "0" * 64,
            "pinned_dependencies": {"pkg": "1.0.0"},
            "reviewed_by": "eval-lab",
        })
    )
    insp = _inspect(repo, task)
    assert "build_proof_lockfile_missing" in _codes(insp)

    # 4. Negative control: lockfile digest mismatch
    (task / "tests/uv.lock").write_text("lock-data-v1\n", encoding="utf-8")
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "uv.lock",
            "lockfile_digest": "sha256:" + "f" * 64,
            "pinned_dependencies": {"pkg": "1.0.0"},
            "reviewed_by": "eval-lab",
        })
    )
    insp = _inspect(repo, task)
    assert "build_proof_lockfile_mismatch" in _codes(insp)

    # 5. Negative control: unpinned dependency in proof
    lock_digest = f"sha256:{hashlib.sha256(b'lock-data-v1\n').hexdigest()}"
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "uv.lock",
            "lockfile_digest": lock_digest,
            "pinned_dependencies": {"pkg": "latest"},
            "reviewed_by": "eval-lab",
        })
    )
    insp = _inspect(repo, task)
    assert "build_proof_unpinned_dependency" in _codes(insp)

    # 5b. Negative controls: version ranges and wildcards are not immutable pins
    for version in ("*", ">=1.0.0", "^2.0", "~=1.4"):
        (task / "tests/build-proof.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "kind": "offline_build_proof",
                "ecosystem": "python",
                "lockfile": "uv.lock",
                "lockfile_digest": lock_digest,
                "pinned_dependencies": {"pkg": version},
                "reviewed_by": "eval-lab",
            })
        )
        assert "build_proof_unpinned_dependency" in _codes(_inspect(repo, task))

    # 6. Accepted path: valid proof matching lockfile allows uv sync
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "uv.lock",
            "lockfile_digest": lock_digest,
            "pinned_dependencies": {"pkg": "1.0.0@sha256:" + "a" * 64},
            "reviewed_by": "eval-lab-reviewer",
        })
    )
    insp = _inspect(repo, task)
    assert insp.static_passed, _codes(insp)
    assert "offline_build_proofs" in insp.candidate

    # 7. A valid proof never authorizes arbitrary network commands.
    tests_dockerfile.write_text(base_docker + "\nRUN curl https://example.invalid/payload\n")
    assert "build_network_use" in _codes(_inspect(repo, task))
    tests_dockerfile.write_text(
        base_docker
        + "\nRUN pip install --no-index --require-hashes "
        "-r https://example.invalid/requirements.txt\n"
    )
    assert "build_network_use" in _codes(_inspect(repo, task))
    tests_dockerfile.write_text(base_docker + "\nRUN uv sync --frozen --offline\n")
    assert _inspect(repo, task).static_passed


def test_v2_complete_safe_fixture_proving_accepted_path(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "complete-v2")

    # 1. task.toml with MCP server, collect hook, and verifier.env
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n'
        'artifacts = ["/app/output/result.txt"]\n\n'
        '[task]\n'
        'name = "local-lab/v2-agentic-fixture"\n'
        'version = "1.0.0"\n'
        'description = "Complete v2 agentic task candidate"\n'
        'keywords = ["mcp", "compose", "collect", "credentials"]\n\n'
        '[[task.authors]]\n'
        'name = "Eval Lab"\n'
        'email = "eval-lab@example.invalid"\n\n'
        '[metadata]\n'
        'difficulty = "easy"\n'
        'category = "agentic"\n'
        'tags = ["v2", "mcp", "offline-proof"]\n\n'
        '[agent]\n'
        'timeout_sec = 30.0\n\n'
        '[verifier]\n'
        'timeout_sec = 30.0\n'
        'environment_mode = "separate"\n\n'
        '[[verifier.collect]]\n'
        'service = "main"\n'
        'command = "if [ ! -f /app/output/result.txt ] && [ -f /logs/agent/result.txt ]; then cp /logs/agent/result.txt /app/output/result.txt; fi"\n\n'
        '[verifier.env]\n'
        'SIMULATED_USER = "${SIMULATED_USER}"\n\n'
        '[environment]\n'
        'network_mode = "no-network"\n'
        'build_timeout_sec = 120.0\n'
        'cpus = 1\n'
        'memory_mb = 256\n'
        'storage_mb = 512\n\n'
        '[[environment.mcp_servers]]\n'
        'name = "local-tools"\n'
        'transport = "streamable-http"\n'
        'url = "http://mcp-server:8000/mcp"\n'
    )

    # 2. Compose file with main and mcp-server
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "  mcp-server:\n"
        "    build: .\n"
    )

    # 3. Verifier Dockerfile with uv sync and offline build proof
    tests_dockerfile = task / "tests/Dockerfile"
    tests_dockerfile.write_text(
        "FROM alpine@sha256:0839e23eb00137d57f59eaee49633e21147a468bfb36f734493393967399580a\n"
        "RUN uv sync --frozen --offline\n"
    )
    lock_data = b"complete-v2-lock-content\n"
    (task / "tests/uv.lock").write_bytes(lock_data)
    lock_digest = f"sha256:{hashlib.sha256(lock_data).hexdigest()}"
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "uv.lock",
            "lockfile_digest": lock_digest,
            "pinned_dependencies": {
                "tau2-bench": "1.0.0@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
            },
            "reviewed_by": "eval-lab-reviewer",
        })
    )

    source = CandidateSource(
        source_uri="https://example.invalid/eval-lab/v2-fixture.git",
        source_ref="v1.0.0",
        license="MIT",
        provenance_zone="03-synthetic",
        credentials=("SIMULATED_USER",),
    )

    inspection = inspect_candidate(repo_root=repo, task_path=task, source=source)
    assert inspection.static_passed, _codes(inspection)
    assert inspection.diagnostics == ()
    assert "compose_topology" in inspection.candidate
    assert "mcp_servers" in inspection.candidate
    assert "offline_build_proofs" in inspection.candidate
    assert "collect_hooks" in inspection.candidate
    assert inspection.candidate.get("credentials") == ["SIMULATED_USER"]

    # Test full check and packet generation
    report = check_candidate(inspection, _bundle(inspection, repo=repo, task=task), repo_root=repo)
    assert report.disposition == "certified_for_review"
    assert report.passed is True
    candidate_path, cert_path = write_packet(repo_root=repo, report=report)
    assert candidate_path.is_file()
    assert cert_path.is_file()
    cert = json.loads(cert_path.read_text())
    assert cert["certified"] is True


def test_v2_internal_bridge_network_complete_fixture(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path / "complete-v2-internal-network")

    (task / "task.toml").write_text(
        'schema_version = "1.4"\n'
        'artifacts = ["/app/output/result.txt"]\n\n'
        '[task]\n'
        'name = "local-lab/v2-internal-network-fixture"\n'
        'version = "1.0.0"\n'
        'description = "Complete v2 internal bridge network fixture"\n'
        'keywords = ["mcp", "compose", "network", "internal-bridge"]\n\n'
        '[[task.authors]]\n'
        'name = "Eval Lab"\n'
        'email = "eval-lab@example.invalid"\n\n'
        '[metadata]\n'
        'difficulty = "easy"\n'
        'category = "agentic"\n'
        'tags = ["v2", "mcp", "offline-proof", "internal-network"]\n\n'
        '[agent]\n'
        'timeout_sec = 30.0\n\n'
        '[verifier]\n'
        'timeout_sec = 30.0\n'
        'environment_mode = "separate"\n\n'
        '[environment]\n'
        'network_mode = "no-network"\n'
        'build_timeout_sec = 120.0\n'
        'cpus = 1\n'
        'memory_mb = 256\n'
        'storage_mb = 512\n\n'
        '[[environment.mcp_servers]]\n'
        'name = "local-tools"\n'
        'transport = "streamable-http"\n'
        'url = "http://mcp-server:8000/mcp"\n'
    )

    (task / "environment/docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "  mcp-server:\n"
        "    build: .\n"
        "    networks:\n"
        "      - mcp-net\n"
        "networks:\n"
        "  mcp-net:\n"
        "    internal: true\n"
    )

    tests_dockerfile = task / "tests/Dockerfile"
    tests_dockerfile.write_text(
        "FROM alpine@sha256:0839e23eb00137d57f59eaee49633e21147a468bfb36f734493393967399580a\n"
        "RUN uv sync --frozen --offline\n"
    )
    lock_data = b"complete-v2-internal-network-lock-content\n"
    (task / "tests/uv.lock").write_bytes(lock_data)
    lock_digest = f"sha256:{hashlib.sha256(lock_data).hexdigest()}"
    (task / "tests/build-proof.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "kind": "offline_build_proof",
            "ecosystem": "python",
            "lockfile": "uv.lock",
            "lockfile_digest": lock_digest,
            "pinned_dependencies": {
                "tau2-bench": "1.0.0@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
            },
            "reviewed_by": "eval-lab-reviewer",
        })
    )

    source = CandidateSource(
        source_uri="https://example.invalid/eval-lab/v2-internal-network-fixture.git",
        source_ref="v1.0.0",
        license="MIT",
        provenance_zone="03-synthetic",
        credentials=(),
    )

    inspection = inspect_candidate(repo_root=repo, task_path=task, source=source)
    assert inspection.static_passed, _codes(inspection)
    assert inspection.diagnostics == ()
    assert inspection.candidate["compose_topology"]["network"] == {
        "name": "mcp-net",
        "internal": True,
    }
    assert inspection.candidate["network_policy"]["agent_runtime_network"] == (
        "isolated on mcp-net (internal: true)"
    )
    assert inspection.candidate["network_policy"]["control_enforcement"] == (
        "docker-compose main + sidecar on mcp-net with build.network=none"
    )

    overlay = yaml.safe_load(_candidate_network_overlay(inspection.candidate))
    assert overlay["networks"]["mcp-net"]["internal"] is True
    assert set(overlay["services"]) == {"main", "mcp-server"}
    assert overlay["services"]["main"]["networks"] == ["mcp-net"]
    assert overlay["services"]["mcp-server"]["networks"] == ["mcp-net"]

    report = check_candidate(inspection, _bundle(inspection, repo=repo, task=task), repo_root=repo)
    assert report.disposition == "certified_for_review"
    assert report.passed is True
    candidate_path, cert_path = write_packet(repo_root=repo, report=report)
    assert candidate_path.is_file()
    assert cert_path.is_file()
    cert = json.loads(cert_path.read_text())
    assert cert["certified"] is True


def _set_task_name(repo: Path, task: Path, name: str) -> None:
    toml = task / "task.toml"
    toml.write_text(
        re.sub(
            r'^name = ".*"$',
            f'name = "{name}"',
            toml.read_text(),
            flags=re.MULTILINE,
        )
    )


@pytest.mark.parametrize(
    "name",
    [
        "mcp-funcdag-baseline-seed42",
        "../evil",
        "org/..name",
        "org/name..",
        ".org/name",
        "org/.name",
        "/name",
        "org/",
        "org/name/extra",
        "org name/task",
    ],
)
def test_invalid_package_name_is_rejected(tmp_path: Path, name: str) -> None:
    repo, task = _copy_candidate(tmp_path)
    _set_task_name(repo, task, name)
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert "task_name_invalid" in _codes(inspection)
    finding = next(item for item in inspection.diagnostics if item.code == "task_name_invalid")
    assert finding.path == "task.toml"


@pytest.mark.parametrize(
    "name",
    [
        "evallab/name",
        "local-lab/v2-agentic-fixture",
        "org.sub_name-1/task.name-2",
    ],
)
def test_valid_package_name_is_accepted(tmp_path: Path, name: str) -> None:
    repo, task = _copy_candidate(tmp_path)
    _set_task_name(repo, task, name)
    inspection = _inspect(repo, task)
    assert inspection.static_passed
    assert "task_name_invalid" not in _codes(inspection)


class _NoopBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(
        self,
        *,
        repo_root,
        task_dir,
        candidate,
        plan,
        run_root,
    ) -> None:
        self.calls.append(plan.control_id)
        raise AssertionError("control should not run for a rejected candidate")


def test_run_controls_fails_closed_on_invalid_package_name(tmp_path: Path) -> None:
    repo, task = _copy_candidate(tmp_path)
    _set_task_name(repo, task, "mcp-funcdag-baseline-seed42")
    inspection = _inspect(repo, task)
    assert not inspection.static_passed
    assert "task_name_invalid" in _codes(inspection)
    backend = _NoopBackend()
    with pytest.raises(
        ControlsNotAdmittedError,
        match="static checks failed; zero controls were called",
    ):
        run_controls(
            inspection=inspection,
            repo_root=repo,
            task_path=task,
            backend=backend,
        )
    assert backend.calls == []
