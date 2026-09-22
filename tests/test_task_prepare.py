"""Behavioral regression tests for repeatable task preparation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evallab.execution_contracts import RunRequest, build_command
from evallab.task_import import package_digest
from evallab.task_prepare import prepare_task

ORACLE_KWARGS: dict[str, object] = {
    "agent": "oracle",
    "model": None,
    "environment": "docker",
}


def _task(
    root: Path,
    *,
    name: str = "lab/demo-task",
    version: str = "1.2.0",
    agent_timeout: float = 120.0,
    verifier_timeout: float = 60.0,
) -> Path:
    package = root / "demo-task"
    package.mkdir(parents=True)
    (package / "task.toml").write_text(
        'schema_version = "1.4"\n'
        f'[task]\nname = "{name}"\nversion = "{version}"\n'
        f"[agent]\ntimeout_sec = {agent_timeout}\n"
        f"[verifier]\ntimeout_sec = {verifier_timeout}\n"
        "[environment]\ncpus = 1\nmemory_mb = 512\n",
        encoding="utf-8",
    )
    (package / "instruction.md").write_text("Do the demo thing.\n", encoding="utf-8")
    environment = package / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    tests = package / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
    script = package / "run.sh"
    script.write_text("#!/bin/sh\necho demo\n", encoding="utf-8")
    script.chmod(0o755)
    return package


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    return repo


def test_prepare_freezes_snapshot_against_later_source_edits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    prepared = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]

    snapshot_bytes = (prepared.task_path / "instruction.md").read_bytes()
    snapshot_digest = package_digest(prepared.task_path)

    (source / "instruction.md").write_text("Do something else entirely.\n", encoding="utf-8")
    (source / "extra.txt").write_text("new file\n", encoding="utf-8")

    assert (prepared.task_path / "instruction.md").read_bytes() == snapshot_bytes
    assert package_digest(prepared.task_path) == snapshot_digest
    assert not (prepared.task_path / "extra.txt").exists()


def test_prepare_preserves_executable_modes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prepared = prepare_task(repo, _task(tmp_path / "external"), name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    assert os.stat(prepared.task_path / "run.sh").st_mode & 0o111


def test_prepare_is_idempotent_for_same_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    first = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    before = first.spec_path.read_bytes()
    second = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    assert second.task_path == first.task_path
    assert second.spec_path.read_bytes() == before


def test_prepare_refuses_to_overwrite_a_different_spec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    first = prepare_task(repo, source, name="demo-run", **ORACLE_KWARGS)  # type: ignore[arg-type]
    before = first.spec_path.read_bytes()
    with pytest.raises(FileExistsError, match="refusing overwrite"):
        prepare_task(repo, source, name="demo-run", agent="nop", model=None, environment="docker")
    assert first.spec_path.read_bytes() == before


def test_prepare_refuses_drifted_snapshot(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    prepared = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    (prepared.task_path / "instruction.md").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="drifted"):
        prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]


def test_prepare_rejects_bad_paths_and_inputs_without_a_spec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    with pytest.raises(ValueError, match="job names"):
        prepare_task(repo, source, name="Bad_Name", **ORACLE_KWARGS)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="escapes repository"):
        prepare_task(
            repo,
            source,
            name="demo-oracle",
            output=Path("/tmp/elsewhere.json"),
            **ORACLE_KWARGS,  # type: ignore[arg-type]
        )
    corpus = tmp_path / "corpus"
    (corpus / "nested").mkdir(parents=True)
    (corpus / "nested" / "task.toml").write_text('[task]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no task.toml"):
        prepare_task(repo, corpus, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported environment"):
        prepare_task(
            repo, source, name="demo-oracle", agent="oracle", model=None, environment="mars"
        )
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()


def test_prepare_requires_explicit_cost_for_metered_and_est_for_remote(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    with pytest.raises(ValueError, match="cost_limit_usd"):
        prepare_task(
            repo,
            source,
            name="demo-paid",
            agent="mini-swe-agent",
            model="deepseek/deepseek-flash",
            environment="docker",
        )
    with pytest.raises(ValueError, match="est_cost_usd"):
        prepare_task(
            repo,
            source,
            name="demo-remote",
            agent="mini-swe-agent",
            model="zai/glm-5.3-flash",
            environment="daytona",
            cost_limit_usd=2.5,
        )
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()


def test_prepare_keeps_agent_and_verifier_deadlines_separate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external", agent_timeout=120.0, verifier_timeout=60.0)
    official = prepare_task(repo, source, name="demo-official", **ORACLE_KWARGS)
    command = build_command(
        RunRequest(
            task=official.task_path,
            agent="oracle",
            name=official.spec.name,
            jobs_dir=repo / "runs",
            timeout_seconds=official.spec.timeout_seconds,
        )
    )
    assert command[command.index("--agent-timeout-multiplier") + 1] == "1"

    diagnostic = prepare_task(
        repo, source, name="demo-diagnostic", timeout_seconds=60, **ORACLE_KWARGS
    )
    assert diagnostic.spec.timeout_seconds == 60
    assert diagnostic.task_timeout_seconds == 120
    assert diagnostic.warnings


def test_prepare_never_silently_truncates_a_task_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external", agent_timeout=36_000)
    with pytest.raises(ValueError, match="explicit shorter diagnostic"):
        prepare_task(repo, source, name="demo-official", **ORACLE_KWARGS)
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()

    explicit = prepare_task(
        repo, source, name="demo-diagnostic", timeout_seconds=600, **ORACLE_KWARGS
    )
    assert explicit.task_timeout_seconds == 36_000
    assert explicit.spec.timeout_seconds == 600


def test_prepare_cannot_write_through_a_snapshot_root_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes repository"):
        prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)

    assert list(outside.iterdir()) == []
    assert not (repo / "derived").exists()
