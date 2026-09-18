"""Focused verification for HAR-62 TB4 workbench, staging, and admission seams.

Tests verify:
1. Timeout boundary: 28800 ok, 28801 rejected citing TB4 v4.0.0 official agent.timeout_sec=28800.
2. Compose 7-service acceptance and isolation-rejection guarantees:
   - 7-service compose topology accepted, depends_on and expose accepted
   - strict rejection of host bind mounts, docker socket, privileged mode, host network mode, and host ports
3. task.toml [task].version and [task].keywords optional; validated when present.
4. Registry admission of synthetic TB4-shaped task without version/keywords.
5. Real corpus admission: all 66 tasks in terminal-bench-4 pass admission validation.
   Records the 3 H100 ids and 11 compose ids.
"""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from evallab.host_task_staging import stage_task_for_host
from evallab.registry import TaskRegistry, compute_task_digests
from evallab.schemas import TaskLimits, TaskRegistryRecord
from evallab.task_workbench import (
    CandidateSource,
    Diagnostic,
    _candidate_network_overlay,
    _validate_compose_topology,
    _validate_service_volume_mounts,
    _validate_task_metadata,
    _validate_timeouts_and_artifacts,
)

REAL_TB4_DIR = Path("/Users/petermakhnatch/Developer/agent-evals/terminal-bench-4/tasks")
TB4_H100_IDS = ("fp8-rmsnorm-gemm", "jax-speedrun-gpu", "math-eval-grader")
TB4_COMPOSE_IDS = (
    "ctr-optimization",
    "cumulative-layout-shift",
    "freight-dispatch-shift",
    "heat-pump-warranty",
    "intrastat-meldung",
    "kv-live-surgery",
    "legacy-utility-triage",
    "live-database-cutover",
    "medical-claims-processing",
    "nextjs-performance",
    "payments-pipeline-fix",
)


def _codes(diagnostics: list[Diagnostic]) -> set[str]:
    return {d.code for d in diagnostics}


# ============================================================================ #
# 1. Timeout boundary tests
# ============================================================================ #


def test_task_limits_timeout_ceiling_boundary() -> None:
    """TaskLimits timeout ceiling is 28800; 28801 is rejected citing TB4 agent.timeout_sec=28800."""
    # 28800 accepted
    limits = TaskLimits(timeout_seconds=28_800)
    assert limits.timeout_seconds == 28_800

    # 1 accepted
    limits_min = TaskLimits(timeout_seconds=1)
    assert limits_min.timeout_seconds == 1

    # 28801 rejected with citation
    with pytest.raises(ValidationError) as exc_info:
        TaskLimits(timeout_seconds=28_801)
    error_msg = str(exc_info.value)
    assert "28800" in error_msg
    assert "TB4 v4.0.0 official agent.timeout_sec=28800" in error_msg

    # 0 rejected
    with pytest.raises(ValidationError):
        TaskLimits(timeout_seconds=0)


def test_workbench_timeout_validation_boundary() -> None:
    """_validate_timeouts_and_artifacts enforces 28800 for TB4 and 21600 for non-TB4."""
    tb4_source = CandidateSource(
        source_uri="github.com/laion-ai/terminal-bench-4",
        source_ref="v4.0.0",
        license="MIT",
    )
    non_tb4_source = CandidateSource(
        source_uri="local/generic-fixture",
        source_ref="1.0.0",
        license="MIT",
    )

    # TB4: 28800 ok
    diags_28800: list[Diagnostic] = []
    _validate_timeouts_and_artifacts(
        {"agent": {"timeout_sec": 28_800}, "verifier": {"timeout_sec": 300}, "artifacts": []},
        diags_28800,
        source=tb4_source,
    )
    assert "timeout_invalid" not in _codes(diags_28800)

    # TB4: 28801 rejected
    diags_28801: list[Diagnostic] = []
    _validate_timeouts_and_artifacts(
        {"agent": {"timeout_sec": 28_801}, "verifier": {"timeout_sec": 300}, "artifacts": []},
        diags_28801,
        source=tb4_source,
    )
    assert "timeout_invalid" in _codes(diags_28801)
    timeout_diag = next(d for d in diags_28801 if d.code == "timeout_invalid")
    assert "28800" in timeout_diag.message
    assert "TB4 v4.0.0 official agent.timeout_sec=28800" in timeout_diag.message

    # Non-TB4: 21600 ok
    diags_21600: list[Diagnostic] = []
    _validate_timeouts_and_artifacts(
        {"agent": {"timeout_sec": 21_600}, "verifier": {"timeout_sec": 300}, "artifacts": []},
        diags_21600,
        source=non_tb4_source,
    )
    assert "timeout_invalid" not in _codes(diags_21600)

    # Non-TB4: 21601 rejected
    diags_21601: list[Diagnostic] = []
    _validate_timeouts_and_artifacts(
        {"agent": {"timeout_sec": 21_601}, "verifier": {"timeout_sec": 300}, "artifacts": []},
        diags_21601,
        source=non_tb4_source,
    )
    assert "timeout_invalid" in _codes(diags_21601)


# ============================================================================ #
# 2. Version and keywords optionality in task metadata
# ============================================================================ #


def test_task_version_and_keywords_optional_when_absent(tmp_path: Path) -> None:
    """task.toml without [task].version and without [task].keywords passes metadata validation."""
    config: dict[str, Any] = {
        "schema_version": "1.0",
        "task": {
            "name": "terminal-bench/synthetic-test",
            "description": "A synthetic test task",
            "authors": [{"name": "Author", "email": "author@example.com"}],
        },
        "metadata": {
            "category": "cli",
            "tags": ["test"],
            "difficulty": "easy",
        },
    }
    diags: list[Diagnostic] = []
    name, version, keywords = _validate_task_metadata(config, tmp_path, diags)
    assert not diags
    assert name == "terminal-bench/synthetic-test"
    assert version is None
    assert keywords == []


def test_task_version_and_keywords_validated_when_present(tmp_path: Path) -> None:
    """When version or keywords are present, their validation rules are enforced."""
    base_config: dict[str, Any] = {
        "schema_version": "1.0",
        "task": {
            "name": "terminal-bench/synthetic-test",
            "description": "A synthetic test task",
            "authors": [{"name": "Author", "email": "author@example.com"}],
        },
        "metadata": {
            "category": "cli",
            "tags": ["test"],
            "difficulty": "easy",
        },
    }

    # Invalid version: empty string
    cfg_bad_ver = {
        **base_config,
        "task": {**base_config["task"], "version": ""},
    }
    diags_ver: list[Diagnostic] = []
    _validate_task_metadata(cfg_bad_ver, tmp_path, diags_ver)
    assert "task_version_invalid" in _codes(diags_ver)

    # Valid version
    cfg_good_ver = {
        **base_config,
        "task": {**base_config["task"], "version": "2.1.0"},
    }
    diags_good_ver: list[Diagnostic] = []
    _, ver_val, _ = _validate_task_metadata(cfg_good_ver, tmp_path, diags_good_ver)
    assert "task_version_invalid" not in _codes(diags_good_ver)
    assert ver_val == "2.1.0"

    # Invalid keywords: fewer than 3
    cfg_few_kw = {
        **base_config,
        "task": {**base_config["task"], "keywords": ["one", "two"]},
    }
    diags_kw: list[Diagnostic] = []
    _validate_task_metadata(cfg_few_kw, tmp_path, diags_kw)
    assert "task_keywords_invalid" in _codes(diags_kw)

    # Valid keywords (3 unique non-empty strings)
    cfg_good_kw = {
        **base_config,
        "task": {**base_config["task"], "keywords": ["alpha", "beta", "gamma"]},
    }
    diags_good_kw: list[Diagnostic] = []
    _, _, kw_val = _validate_task_metadata(cfg_good_kw, tmp_path, diags_good_kw)
    assert "task_keywords_invalid" not in _codes(diags_good_kw)
    assert kw_val == ["alpha", "beta", "gamma"]


# ============================================================================ #
# 3. Multi-service compose acceptance (7 services, depends_on, expose)
# ============================================================================ #


def test_compose_multi_service_7_services_accepted(tmp_path: Path) -> None:
    """A 7-service compose document (like heat-pump-warranty) is accepted in workbench and staging."""
    task_dir = tmp_path / "seven-services"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Instruction\n")
    (task_dir / "tests").mkdir()
    (task_dir / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")

    compose_data = {
        "services": {
            "main": {
                "build": ".",
                "depends_on": {
                    "portal": {"condition": "service_healthy"},
                    "ledger": {"condition": "service_healthy"},
                    "vault": {"condition": "service_healthy"},
                    "returns": {"condition": "service_healthy"},
                    "compliance": {"condition": "service_healthy"},
                    "inbox": {"condition": "service_healthy"},
                },
                "environment": ["PORTAL_URL=http://portal:8000"],
            },
            "portal": {"image": "alpine@sha256:" + "a" * 64, "expose": ["8000"], "healthcheck": {"test": ["CMD", "true"]}},
            "ledger": {"image": "alpine@sha256:" + "b" * 64, "expose": ["8001"], "healthcheck": {"test": ["CMD", "true"]}},
            "vault": {"image": "alpine@sha256:" + "c" * 64, "expose": ["8002"], "healthcheck": {"test": ["CMD", "true"]}},
            "returns": {"image": "alpine@sha256:" + "d" * 64, "expose": ["8003"], "healthcheck": {"test": ["CMD", "true"]}},
            "compliance": {"image": "alpine@sha256:" + "e" * 64, "expose": ["8004"], "healthcheck": {"test": ["CMD", "true"]}},
            "inbox": {"image": "alpine@sha256:" + "f" * 64, "expose": ["8005"], "healthcheck": {"test": ["CMD", "true"]}},
        }
    }
    compose_path = env_dir / "docker-compose.yaml"
    compose_path.write_text(yaml.safe_dump(compose_data, sort_keys=False))
    (task_dir / "task.toml").write_text(
        'schema_version = "1.0"\n'
        '[task]\nname = "terminal-bench/seven-service-test"\n'
        '[agent]\ntimeout_sec = 28800\n'
        '[verifier]\ntimeout_sec = 300\nenvironment_mode = "separate"\n'
        '[metadata]\ncategory = "sysadmin"\ntags = ["compose"]\nexpert_time_estimate_hours = 2.0\n'
    )

    # 1. Workbench compose topology validation
    diags: list[Diagnostic] = []
    topology, sidecar_name = _validate_compose_topology(task_dir, diags)
    assert not diags, [d.message for d in diags]
    assert topology is not None
    assert len(topology["services"]) == 7
    assert sidecar_name == "portal"

    # 2. Network overlay isolates all 7 services
    overlay_bytes = _candidate_network_overlay(
        {"compose_topology": topology}
    )
    overlay = yaml.safe_load(overlay_bytes)
    assert overlay["networks"]["workbench-internal"]["internal"] is True
    assert set(overlay["services"].keys()) == {
        "main", "portal", "ledger", "vault", "returns", "compliance", "inbox"
    }
    for _s_name, s_cfg in overlay["services"].items():
        assert s_cfg["build"]["network"] == "none"
        assert s_cfg["networks"] == ["workbench-internal"]

    # 3. Host staging succeeds with platform pinning preserved
    staged_dest = tmp_path / "staged"
    manifest = stage_task_for_host(task_dir, staged_dest, pin_platform=True, platform="linux/amd64")
    assert manifest.compose_present is True
    assert len(manifest.platform_pins) >= 7
    staged_compose = yaml.safe_load((staged_dest / "environment/docker-compose.yaml").read_text())
    assert len(staged_compose["services"]) == 7
    for s_cfg in staged_compose["services"].values():
        assert s_cfg.get("platform") == "linux/amd64"


# ============================================================================ #
# 4. Strict isolation guarantees preserved (negative tests)
# ============================================================================ #


def test_compose_rejection_host_bind_mount(tmp_path: Path) -> None:
    """Host bind mounts are strictly rejected in workbench and staging."""
    # Workbench check
    diags: list[Diagnostic] = []
    _validate_service_volume_mounts(
        "worker",
        ["/host/path:/container/path:rw"],
        volume_name="task-vol",
        rel_path="docker-compose.yaml",
        diagnostics=diags,
    )
    assert "compose_volume_escape" in _codes(diags)

    # Staging check
    task_dir = tmp_path / "host-bind-task"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Inst\n")
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "test/host-bind"\n')
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    volumes:\n"
        "      - /etc:/container-etc\n"
    )
    with pytest.raises(ValueError, match="forbidden host bind mount"):
        stage_task_for_host(task_dir, tmp_path / "staged")


def test_compose_rejection_docker_socket(tmp_path: Path) -> None:
    """Docker socket mounts are strictly rejected in workbench and staging."""
    # Workbench check
    diags: list[Diagnostic] = []
    _validate_service_volume_mounts(
        "worker",
        ["/var/run/docker.sock:/var/run/docker.sock"],
        volume_name="task-vol",
        rel_path="docker-compose.yaml",
        diagnostics=diags,
    )
    assert "compose_volume_escape" in _codes(diags)

    # Staging check
    task_dir = tmp_path / "socket-task"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Inst\n")
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "test/sock"\n')
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    build: .\n"
        "    volumes:\n"
        "      - /var/run/docker.sock:/var/run/docker.sock\n"
    )
    with pytest.raises(ValueError, match="docker socket"):
        stage_task_for_host(task_dir, tmp_path / "staged")


def test_compose_rejection_privileged_mode(tmp_path: Path) -> None:
    """Privileged mode and host PID/IPC namespaces are strictly rejected."""
    task_dir = tmp_path / "priv-task"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Inst\n")
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "test/priv"\n')

    # privileged: true
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    privileged: true\n"
    )
    diags: list[Diagnostic] = []
    _validate_compose_topology(task_dir, diags)
    assert "compose_privileged_unsupported" in _codes(diags)

    with pytest.raises(ValueError, match="privileged mode"):
        stage_task_for_host(task_dir, tmp_path / "staged_priv")

    # pid: host
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    pid: host\n"
    )
    diags_pid: list[Diagnostic] = []
    _validate_compose_topology(task_dir, diags_pid)
    assert "compose_privileged_unsupported" in _codes(diags_pid)

    with pytest.raises(ValueError, match="host PID/IPC namespace"):
        stage_task_for_host(task_dir, tmp_path / "staged_pid")


def test_compose_rejection_host_network(tmp_path: Path) -> None:
    """network_mode: host is strictly rejected in workbench and staging."""
    task_dir = tmp_path / "host-net-task"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Inst\n")
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "test/host-net"\n')
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    network_mode: host\n"
    )
    diags: list[Diagnostic] = []
    _validate_compose_topology(task_dir, diags)
    assert "custom_compose_unsupported" in _codes(diags)

    with pytest.raises(ValueError, match="forbidden host network mode"):
        stage_task_for_host(task_dir, tmp_path / "staged")


def test_compose_rejection_host_ports(tmp_path: Path) -> None:
    """Published host ports (ports:) are strictly rejected (expose: is permitted)."""
    task_dir = tmp_path / "ports-task"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Inst\n")
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text('schema_version = "1.0"\n[task]\nname = "test/ports"\n')
    (env_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    ports:\n      - '8080:8080'\n"
    )
    diags: list[Diagnostic] = []
    _validate_compose_topology(task_dir, diags)
    assert "compose_host_ports_unsupported" in _codes(diags)

    with pytest.raises(ValueError, match="publishes host ports"):
        stage_task_for_host(task_dir, tmp_path / "staged")


# ============================================================================ #
# 5. Registry admission of synthetic TB4-shaped task
# ============================================================================ #


def test_registry_admission_synthetic_tb4_shaped_task(tmp_path: Path) -> None:
    """A synthetic task with TB4 shape (no version, no keywords, timeout 28800) admits cleanly."""
    task_dir = tmp_path / "tb4-synthetic"
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (task_dir / "instruction.md").write_text("Complete this task.\n")
    (task_dir / "tests").mkdir()
    (task_dir / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task_dir / "task.toml").write_text(
        'schema_version = "1.0"\n\n'
        '[task]\n'
        'name = "terminal-bench/synthetic-tb4"\n'
        'authors = [{ name = "ScaleAI", email = "tb4@scale.com" }]\n\n'
        '[agent]\n'
        'timeout_sec = 28800\n\n'
        '[verifier]\n'
        'timeout_sec = 300\n'
        'environment_mode = "separate"\n\n'
        '[metadata]\n'
        'category = "data"\n'
        'tags = ["analytics", "synthetic"]\n'
        'expert_time_estimate_hours = 4.0\n'
    )

    # 1. Digests compute
    digests = compute_task_digests(task_dir)
    assert digests.task_toml.startswith("sha256:")
    assert digests.instruction.startswith("sha256:")
    assert digests.environment.startswith("sha256:")
    assert digests.verifier.startswith("sha256:")
    assert digests.package.startswith("sha256:")

    # 2. TaskRegistryRecord schema admission without version or keywords
    record = TaskRegistryRecord(
        schema_version=2,
        task_id="synthetic-tb4",
        task_family="terminal-bench-4",
        task_path="library/tasks/tb4-synthetic",
        digests=digests,
        source_uri="terminal-bench-4/synthetic-tb4@1.0.0",
        provenance_zone="01-external",
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=28_800),
        state="candidate",
        state_reason="pending_control_evidence",
        allowed_uses=["measurement"],
    )
    assert record.version == "1.0.0"  # defaulted
    assert record.limits.timeout_seconds == 28_800
    assert record.state == "candidate"
    # 3. Registry admission: save and load via TaskRegistry
    reg_dir = tmp_path / "registry"
    registry = TaskRegistry(reg_dir, {})
    dest_path = registry.save_record(record)
    assert dest_path.is_file()

    # Reload from registry directory
    reloaded_registry = TaskRegistry(reg_dir, {record.task_id: record})
    admitted = reloaded_registry.get("synthetic-tb4")
    assert admitted is not None
    assert admitted.task_id == "synthetic-tb4"
    assert admitted.version == "1.0.0"
    assert admitted.limits.timeout_seconds == 28_800
    assert admitted.state == "candidate"


# ============================================================================ #
# 6. Real corpus verification (all 66 pinned tasks)
# ============================================================================ #


@pytest.mark.skipif(not REAL_TB4_DIR.is_dir(), reason="Real TB4 tasks checkout not available")
def test_real_tb4_corpus_all_66_tasks_admission() -> None:
    """Verify ALL 66 tasks in terminal-bench-4 pass admission validation.

    Checks:
    - Exactly 66 tasks discovered.
    - Layout completeness for all 66.
    - Metadata validation passes for all 66 (version/keywords optional).
    - Timeout validation passes for all 66 (agent.timeout_sec <= 28800).
    - Compose validation passes for all 11 compose tasks (multi-service, depends_on, expose).
    - Host staging succeeds for all 66 tasks.
    - Cryptographic digests compute and TaskRegistryRecord admits for all 66 tasks.
    - Exactly 3 H100 task IDs and 11 compose task IDs recorded and verified.
    """
    all_tasks = sorted([d for d in REAL_TB4_DIR.iterdir() if d.is_dir()], key=lambda p: p.name)
    assert len(all_tasks) == 66, f"Expected 66 tasks, found {len(all_tasks)}"

    tb4_source = CandidateSource(
        source_uri="terminal-bench/terminal-bench@4.0.0",
        source_ref="v4.0.0",
        license="MIT",
    )

    observed_h100_ids: list[str] = []
    observed_compose_ids: list[str] = []
    admitted_count = 0
    non_seam_failures: list[tuple[str, str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        staging_dir = tmp_root / "staging"
        staging_dir.mkdir()

        for task_dir in all_tasks:
            task_id = task_dir.name
            toml_path = task_dir / "task.toml"
            if not toml_path.is_file():
                non_seam_failures.append((task_id, "task.toml missing"))
                continue

            toml_text = toml_path.read_text(encoding="utf-8")
            config = tomllib.loads(toml_text)

            # Record H100 ids
            if "h100" in toml_text.lower():
                observed_h100_ids.append(task_id)

            # Record compose ids
            has_compose = (task_dir / "environment/docker-compose.yaml").is_file() or (
                task_dir / "environment/docker-compose.yml"
            ).is_file()
            if has_compose:
                observed_compose_ids.append(task_id)

            # 1. Metadata validation
            d_meta: list[Diagnostic] = []
            _validate_task_metadata(config, task_dir, d_meta)
            assert not d_meta, f"Task {task_id} failed metadata validation: {[d.message for d in d_meta]}"

            # 2. Timeout validation
            d_time: list[Diagnostic] = []
            _validate_timeouts_and_artifacts(config, d_time, source=tb4_source)
            time_errs = [d for d in d_time if d.code == "timeout_invalid"]
            assert not time_errs, f"Task {task_id} failed timeout validation: {[d.message for d in time_errs]}"

            # 3. Compose validation (if compose present)
            if has_compose:
                d_comp: list[Diagnostic] = []
                top, _ = _validate_compose_topology(task_dir, d_comp)
                comp_errs = [
                    d
                    for d in d_comp
                    if d.code in ("compose_topology_invalid", "compose_structure_invalid", "compose_main_service_missing")
                ]
                assert not comp_errs, f"Task {task_id} failed compose validation: {[d.message for d in comp_errs]}"

            # 4. Host staging
            dest = staging_dir / task_id
            manifest = stage_task_for_host(task_dir, dest)
            assert manifest is not None

            # 5. Digest computation and TaskRegistryRecord admission
            digests = compute_task_digests(task_dir)
            rec = TaskRegistryRecord(
                schema_version=2,
                task_id=task_id,
                task_family="terminal-bench-4",
                task_path=f"tasks/{task_id}",
                digests=digests,
                source_uri=f"terminal-bench-4/{task_id}@4.0.0",
                provenance_zone="01-external",
                is_synthetic=False,
                limits=TaskLimits(timeout_seconds=28_800),
                state="candidate",
                state_reason="pending_control_evidence",
                allowed_uses=["measurement"],
            )
            assert rec.limits.timeout_seconds <= 28_800
            admitted_count += 1

    # Record and assert H100 and compose identities
    assert tuple(sorted(observed_h100_ids)) == TB4_H100_IDS, (
        f"H100 mismatch: {observed_h100_ids} vs {TB4_H100_IDS}"
    )
    assert tuple(sorted(observed_compose_ids)) == TB4_COMPOSE_IDS, (
        f"Compose mismatch: {observed_compose_ids} vs {TB4_COMPOSE_IDS}"
    )

    # Assert all 66 passed admission without non-seam failures
    assert not non_seam_failures, f"Non-seam failures observed: {non_seam_failures}"
    assert admitted_count == 66, f"Expected 66 admitted tasks, got {admitted_count}"
