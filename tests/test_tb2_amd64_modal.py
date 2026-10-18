"""TB2 amd64/Modal grading stays off the answer-key bytes and cannot spend by accident."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from evallab.execution_contracts import (
    collected_secret_values,
    new_ulid,
    subscription_environment,
)
from evallab.regrade import CloudSpendGate, SpendCapReached
from evallab.runner import staged_matrix_request
from evallab.schemas import ExperimentMatrix, MatrixRun
from evallab.vcheck import oracle_submission
from evallab.verifier_mutation import grade_submission

EXPERIMENT = Path(__file__).parents[1] / "research/experiments/tb2-to-tb21-v1"
sys.path.insert(0, str(EXPERIMENT))
import run_tb2_to_tb21 as driver  # noqa: E402


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


def test_modal_grade_refuses_without_a_gate_before_harbor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CloudSpendGate"):
        grade_submission(
            tmp_path,
            oracle_submission(),
            repo_root=tmp_path,
            run_dir=tmp_path / "vcheck",
            environment="modal",
        )


def test_modal_grade_reserves_before_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package(tmp_path)
    calls: list[ExperimentMatrix] = []

    def fake_runner(matrix: ExperimentMatrix, root: Path) -> list[dict[str, object]]:
        calls.append(matrix)
        return [{"name": matrix.runs[0].name, "status": "ok", "rewards": [1.0]}]

    monkeypatch.setattr("evallab.regrade.lab_harbor_problem", lambda: None)
    monkeypatch.setattr("evallab.verifier_mutation._default_runner", fake_runner)
    gate = CloudSpendGate("tb2 amd64 test", 1.0, 0.2)

    result = grade_submission(
        tmp_path / "task",
        oracle_submission(),
        repo_root=tmp_path,
        run_dir=tmp_path / "vcheck",
        environment="modal",
        spend_gate=gate,
        modal_app_name="tb2-amd64-modal",
        modal_resource_policy="limit",
        modal_sandbox_create_max_attempts=1,
    )

    assert result.status == "ok"
    assert gate.executions == 1
    assert calls[0].environment == "modal"
    assert calls[0].modal_app_name == "tb2-amd64-modal"
    assert calls[0].docker_platform is None
    broke = CloudSpendGate("tb2 amd64 test", 0.01, 0.02)
    with pytest.raises(SpendCapReached):
        grade_submission(
            tmp_path / "task",
            oracle_submission(),
            repo_root=tmp_path,
            run_dir=tmp_path / "vcheck",
            environment="modal",
            spend_gate=broke,
        )
    assert len(calls) == 1


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


def test_modal_backend_refuses_a_missing_approval_or_a_cap_over_one_dollar() -> None:
    with pytest.raises(ValueError, match="--approve"):
        driver.build_grade_backend(
            environment="modal",
            platform="linux/amd64",
            max_usd=1,
            approve=None,
            usd_per_execution=0.2,
        )
    with pytest.raises(ValueError, match="Modal cap"):
        driver.build_grade_backend(
            environment="modal",
            platform="",
            max_usd=1.01,
            approve="recorded",
            usd_per_execution=0.2,
        )
    local = driver.build_grade_backend(
        environment="docker",
        platform="linux/amd64",
        max_usd=None,
        approve=None,
        usd_per_execution=0.2,
    )
    assert local.environment == "docker"
    assert local.docker_platform == "linux/amd64"
    assert local.spend_gate is None
    modal = driver.build_grade_backend(
        environment="modal",
        platform="linux/amd64",
        max_usd=1,
        approve="recorded approval",
        usd_per_execution=0.2,
    )
    assert modal.environment == "modal"
    assert modal.docker_platform is None
    assert modal.spend_gate is not None
    assert modal.spend_gate.cap_usd == 1


def test_version_timeout_uses_the_graded_package_not_the_tb2_timeout(tmp_path: Path) -> None:
    package = tmp_path / "caffe-tb21"
    package.mkdir()
    (package / "task.toml").write_text("[agent]\ntimeout_sec = 3600\n", encoding="utf-8")
    cell = driver.TaskCell(
        "pair",
        "pair",
        "terminal-bench-2",
        "caffe-cifar-10",
        {"timeout_seconds": 1200, "subsplit": "pr53_modified"},
    )

    def report(_runs_dir: Path) -> dict[str, object]:
        return {
            "packages": {
                "tb21/caffe-cifar-10": {"digest_match": True, "path": str(package)},
            }
        }

    original = driver.materialize.load_report
    driver.materialize.load_report = report
    try:
        assert driver.version_timeout(cell, "tb21", tmp_path) == 3600
        assert driver.version_timeout(cell, "tb2", tmp_path) == 1200
    finally:
        driver.materialize.load_report = original
