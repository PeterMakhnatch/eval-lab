"""Behavioural tests for the separate-verifier@1 transform.

Covers the transform output contract only (task.toml fields, agent build
context free of tests, artifact declaration, wrapper/orig split). No Docker,
no Harbor runs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from evallab.separate_verifier import (
    SNAP_DIR,
    TRAJECTORY_ARTIFACT,
    TRANSFORM_ID,
    build_changes,
    derive_separate_verifier,
    read_parent_info,
)
from evallab.task_variants import VariantInvalid

MARKER = "test_hidden_example"

PARENT_TOML = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/format-code-task-000000"
description = "synthetic parent"

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 2100.0
user = "root"

[environment]
docker_image = "docker.io/example/repo@sha256:0000000000000000000000000000000000000000000000000000000000000000"
workdir = "/testbed"
cpus = 2
memory_mb = 8192
network_mode = "public"
build_timeout_sec = 1800.0
"""

PARENT_TEST_SH = """#!/bin/bash
M=/var/lib/mimo
echo grading
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    (root / "environment").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "task.toml").write_text(PARENT_TOML, encoding="utf-8")
    (root / "instruction.md").write_text("fix it", encoding="utf-8")
    (root / "environment" / "Dockerfile").write_text(
        "FROM docker.io/example/repo@sha256:0000\n", encoding="utf-8"
    )
    (root / "tests" / "test.sh").write_text(PARENT_TEST_SH, encoding="utf-8")
    (root / "tests" / "test.patch").write_text("fake patch", encoding="utf-8")
    (root / "tests" / "test_command.sh").write_text("true\n", encoding="utf-8")
    return root


def test_task_toml_declares_separate_mode_and_snapshot_artifact(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    config = tomllib.loads(changes["task.toml"].decode("utf-8"))

    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["timeout_sec"] == 2100.0
    assert config["verifier"]["user"] == "root"
    assert "environment" not in config["verifier"]

    assert config["artifacts"] == [SNAP_DIR, TRAJECTORY_ARTIFACT]
    assert SNAP_DIR.startswith("/var/tmp/")
    assert SNAP_DIR.rstrip("/") != "/logs/artifacts"

    hooks = config["verifier"]["collect"]
    assert len(hooks) == 2
    assert all(hook["service"] == "main" for hook in hooks)
    assert all(hook["user"] == "root" for hook in hooks)
    # Probe runs before the snapshot so tarballs are not scanned.
    assert MARKER in hooks[0]["command"]
    assert "workspace.tgz" in hooks[1]["command"]
    assert MARKER not in hooks[1]["command"]
    assert "workspace.tgz" not in hooks[0]["command"]
    # Untouched sections survive byte-identical in spirit.
    assert config["environment"]["docker_image"].startswith("docker.io/example")
    assert config["environment"]["workdir"] == "/testbed"
    assert config["task"]["name"] == "mimo-v2.6-rl/format-code-task-000000"


def test_trajectory_artifact_reaches_integrity_default_path(parent_dir: Path) -> None:
    """The declared trajectory lands where the integrity runner looks by default."""
    changes, inputs = build_changes(parent_dir, marker=MARKER)
    config = tomllib.loads(changes["task.toml"].decode("utf-8"))
    assert TRAJECTORY_ARTIFACT == "/logs/agent/trajectory.json"
    assert TRAJECTORY_ARTIFACT in config["artifacts"]
    # Outside the convention dir: no overlap/skip against the implicit entry,
    # so regrade coverage can be satisfied by a collected manifest entry.
    assert not TRAJECTORY_ARTIFACT.startswith("/logs/artifacts/")
    assert inputs["trajectory_artifact"] == TRAJECTORY_ARTIFACT


def test_composes_after_integrity_tail(parent_dir: Path) -> None:
    """Integrity-first ordering: the scoring tail must land inside test-orig.sh."""
    from evallab.integrity_reward import build_test_sh

    tailed = build_test_sh((parent_dir / "tests" / "test.sh").read_bytes())
    (parent_dir / "tests" / "test.sh").write_bytes(tailed)
    changes, _ = build_changes(parent_dir, marker=MARKER)
    orig = changes["tests/test-orig.sh"].decode("utf-8")
    assert "rewardkit-integrity@1" in orig
    assert "run_integrity.py" in orig
    wrapper = changes["tests/test.sh"].decode("utf-8")
    assert "exec /tests/test-orig.sh" in wrapper
    # Applying separate first would strand the tail after an exec line.
    assert "rewardkit-integrity@1" not in wrapper


def test_agent_build_context_unchanged_but_verifier_bundles_tests(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    # No change touches environment/: the agent image build is untouched.
    assert not any(key.startswith("environment/") for key in changes)
    dockerfile = changes["tests/Dockerfile"].decode("utf-8")
    assert "docker.io/example/repo@sha256:" in dockerfile
    assert "COPY . /tests" in dockerfile
    assert "--platform=linux/amd64" in dockerfile
    assert "chmod +x /tests/test.sh /tests/test-orig.sh" in dockerfile


def test_wrapper_restores_snapshot_and_orig_is_verbatim(
    parent_dir: Path,
) -> None:
    changes, _ = build_changes(parent_dir, marker=MARKER)
    assert changes["tests/test-orig.sh"].decode("utf-8") == PARENT_TEST_SH

    wrapper = changes["tests/test.sh"].decode("utf-8")
    assert "workspace.tgz" in wrapper
    assert "git-hidden.tgz" in wrapper
    assert "exec /tests/test-orig.sh" in wrapper
    assert MARKER in wrapper  # verifier-side probe
    # The wrapper carries no hidden-test content beyond the marker.
    assert "Weight calculate failed" not in wrapper


def test_solution_injected_only_when_parent_has_none(parent_dir: Path) -> None:
    solve = b"#!/bin/bash\necho oracle\n"
    changes, inputs = build_changes(parent_dir, marker=MARKER, solution_sh=solve)
    assert changes["solution/solve.sh"] == solve
    assert inputs["solution"].startswith("sha256:")

    (parent_dir / "solution").mkdir()
    (parent_dir / "solution" / "solve.sh").write_text("existing", encoding="utf-8")
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir, marker=MARKER, solution_sh=solve)


def test_refuses_already_separate_or_missing_pieces(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir, marker="  ")
    with pytest.raises(VariantInvalid):
        read_parent_info(parent_dir / "missing")

    text = (parent_dir / "task.toml").read_text(encoding="utf-8")
    (parent_dir / "task.toml").write_text(
        text.replace(
            "[verifier]\ntimeout_sec", '[verifier]\nenvironment_mode = "separate"\ntimeout_sec'
        ),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid):
        read_parent_info(parent_dir)


def test_derive_records_transform_id(parent_dir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_separate_verifier(
        parent_dir,
        marker=MARKER,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID
    assert record.task_name == "mimo-v2.6-rl/format-code-task-000000"
    assert {change.path for change in record.files} == {
        "task.toml",
        "tests/test.sh",
        "tests/test-orig.sh",
        "tests/Dockerfile",
    }
    expected_record = (
        tmp_path / "library" / "task-variants" / record.task_slug / f"{record.digest12}.json"
    )
    assert expected_record.is_file()
    assert (store / record.task_slug / record.digest12).is_dir()
