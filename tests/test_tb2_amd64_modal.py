"""TB2 amd64/Modal grading stays off the answer-key bytes and cannot spend by accident."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.execution_contracts import (
    collected_secret_values,
    new_ulid,
    subscription_environment,
)
from evallab.runner import staged_matrix_request
from evallab.schemas import ExperimentMatrix, MatrixRun


def _matrix(repo: Path, *, platform: str | None) -> ExperimentMatrix:
    return ExperimentMatrix(
        matrix_id=new_ulid(),
        name="pin-grade",
        hypothesis="Pin only the staged execution copy.",
        benchmark_family="tb2",
        task_id="pin-task",
        task=str(repo / "task"),
        task_package_digest="sha256:" + "ab" * 32,
        verifier_digest="sha256:" + "cd" * 32,
        docker_platform=platform,
        runs=[MatrixRun(name="pin", agent="oracle", solution="grade.sh")],
    )


def _package(repo: Path, *, compose: str | None = None) -> None:
    task = repo / "task"
    (task / "environment").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "task.toml").write_text('schema_version = "1.1"\n', encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text("FROM debian:13\nRUN true\n", encoding="utf-8")
    (task / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    (repo / "grade.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    if compose is not None:
        (task / "environment" / "docker-compose.yaml").write_text(compose, encoding="utf-8")


def test_platform_pin_is_only_on_the_staged_copy(tmp_path: Path) -> None:
    _package(tmp_path)
    source = tmp_path / "task" / "environment" / "Dockerfile"
    original = source.read_bytes()
    matrix = _matrix(tmp_path, platform="linux/amd64")

    with staged_matrix_request(matrix, matrix.runs[0], repo_root=tmp_path) as (request, provenance):
        staged = request.task / "environment"
        dockerfile = (staged / "Dockerfile").read_text(encoding="utf-8")
        compose = (staged / "docker-compose.yaml").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM --platform=linux/amd64 debian:13\n")
    assert "platform: linux/amd64" in compose
    assert provenance["docker_platform"] == "linux/amd64"
    assert source.read_bytes() == original
    assert not (tmp_path / "task" / "environment" / "docker-compose.yaml").exists()


def test_platform_pin_refuses_to_rewrite_an_authored_compose(tmp_path: Path) -> None:
    authored = "services:\n  main:\n    image: example\n"
    _package(tmp_path, compose=authored)
    matrix = _matrix(tmp_path, platform="linux/amd64")

    with (
        pytest.raises(ValueError, match="refusing to rewrite"),
        staged_matrix_request(matrix, matrix.runs[0], repo_root=tmp_path),
    ):
        pass

    assert (tmp_path / "task" / "environment" / "docker-compose.yaml").read_text() == authored






def test_harbor_child_receives_platform_and_modal_tokens_only_when_asked() -> None:
    source = {
        "HOME": "/safe/home",
        "PATH": "/safe/bin",
        "MODAL_TOKEN_ID": "modal-id",
        "MODAL_TOKEN_SECRET": "modal-secret",
        "OPENAI_API_KEY": "must-not-forward",
    }

    plain = subscription_environment(source)
    assert "MODAL_TOKEN_ID" not in plain
    assert "DOCKER_DEFAULT_PLATFORM" not in plain
    assert "OPENAI_API_KEY" not in plain

    forwarded = subscription_environment(
        source,
        include_modal_credentials=True,
        docker_platform="linux/amd64",
    )
    assert forwarded["MODAL_TOKEN_ID"] == "modal-id"
    assert forwarded["MODAL_TOKEN_SECRET"] == "modal-secret"
    assert forwarded["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
    assert "OPENAI_API_KEY" not in forwarded
    assert "modal-secret" in collected_secret_values(source)




