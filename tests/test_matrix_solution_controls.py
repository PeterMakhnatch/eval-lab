import argparse
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.cli import _matrix_command
from evallab.fetch import ControlCall, DatasetListing, FetchError
from evallab.registry import compute_task_digests
from evallab.schemas import MatrixRun


class FixtureHarbor:
    def __init__(self, failure: str | None = None, reward: float = 0.0) -> None:
        self.failure = failure
        self.reward = reward
        self.calls: list[ControlCall] = []
        self.snapshots: list[dict] = []

    def list_hub_datasets(self) -> list[DatasetListing]:
        raise AssertionError("matrix must not query datasets")

    def download(self, pin: str, dest: Path) -> None:
        raise AssertionError("matrix must not download tasks")

    def run_control(self, call: ControlCall) -> float:
        self.calls.append(call)
        solve = call.task_path / "solution/solve.sh"
        self.snapshots.append({
            "script": solve.read_bytes(),
            "mode": solve.stat().st_mode & 0o777,
            "controls_present": (call.task_path / "controls").exists(),
            "nested_control": (call.task_path / "tests/controls/fixture").read_text(),
            "digest": compute_task_digests(call.task_path).package,
        })
        job = call.jobs_dir / call.job_name
        job.mkdir(parents=True)
        if self.failure == "missing_result":
            return 0.0  # A backend scalar without evidence is not a valid control.
        (job / "result.json").write_text(json.dumps({
            "n_total_trials": call.n_attempts,
            "stats": {},
            "finished_at": "2026-09-11T12:00:01Z",
        }))
        for attempt in range(call.n_attempts):
            trial = job / f"task__{attempt}"
            trial.mkdir()
            reward = float("nan") if self.failure == "nan" else self.reward
            (trial / "result.json").write_text(json.dumps({
                "task_name": "task",
                "trial_name": trial.name,
                "finished_at": "2026-09-11T12:00:01Z",
                "verifier_result": {
                    "rewards": {} if self.failure == "missing_reward" else {"reward": reward}
                },
                "exception_info": (
                    {"exception_type": "VerifierTimeoutError"}
                    if self.failure == "exception" else None
                ),
            }))
        if self.failure == "nonzero":
            raise FetchError("harbor exited 1")
        return self.reward


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    for directory in ("solution", "controls", "tests/controls"):
        (task / directory).mkdir(parents=True)
    (task / "task.toml").write_text('version = "1.0"\n')
    (task / "solution/solve.sh").write_text("#!/bin/sh\necho original\n")
    (task / "controls/mutant.sh").write_text("#!/bin/sh\necho mutant\n")
    (task / "tests/controls/fixture").write_text("retained verifier fixture")
    digests = compute_task_digests(task)
    (tmp_path / "matrix.json").write_text(json.dumps({
        "schema_version": 2,
        "matrix_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "name": "negative-control",
        "hypothesis": "the verifier rejects the mutant",
        "benchmark_family": "fixture",
        "task_id": "fixture-task",
        "task": "task",
        "task_package_digest": digests.package,
        "verifier_digest": digests.verifier,
        "jobs_dir": "runs",
        "runs": [{
            "name": "mutant", "agent": "oracle", "expect_reward": 0.0,
            "solution": "task/controls/mutant.sh",
        }],
    }))
    return tmp_path


def execute(root: Path, harbor: FixtureHarbor, *, reuse: bool = False) -> int:
    return _matrix_command(
        argparse.Namespace(path=Path("matrix.json"), reuse_existing=reuse), root, harbor=harbor
    )


def receipt(root: Path) -> dict:
    return json.loads(
        (root / "runs/.executor/01ARZ3NDEKTSV4RRFFQ69G5FAV.matrix.json").read_text()
    )


def test_solution_is_staged_without_mutating_the_task(workspace: Path) -> None:
    original = compute_task_digests(workspace / "task").package
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 0
    snapshot = harbor.snapshots[0]
    assert snapshot["script"] == b"#!/bin/sh\necho mutant\n"
    assert snapshot["mode"] == 0o755
    assert not snapshot["controls_present"]
    assert snapshot["nested_control"] == "retained verifier fixture"
    staged = harbor.calls[0].task_path
    assert staged != workspace / "task"
    assert not staged.exists()
    assert compute_task_digests(workspace / "task").package == original
    record = receipt(workspace)
    assert record["matrix"]["task_package_digest"] == original
    result = record["results"][0]
    assert result["status"] == "ok"
    assert result["solution_sha256"] == hashlib.sha256(snapshot["script"]).hexdigest()
    assert result["staged_task_digest"] == snapshot["digest"] != original


@pytest.mark.parametrize("overrides", [{"agent": "nop"}, {"allow_billable": True}])
def test_solution_is_only_a_nonbillable_oracle_control(overrides: dict) -> None:
    payload = {"name": "mutant", "agent": "oracle", "solution": "control.sh"} | overrides
    with pytest.raises(ValidationError, match="solution requires"):
        MatrixRun.model_validate(payload)


@pytest.mark.parametrize("failure", ["exception", "missing_result", "missing_reward", "nan", "nonzero"])
def test_infra_is_not_a_passing_negative_control(
    workspace: Path, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    harbor = FixtureHarbor(failure)
    assert execute(workspace, harbor) == 1
    result = receipt(workspace)["results"][0]
    assert result["status"] == "infra"
    assert result["rewards"] == []
    assert "mutant: infra" in capsys.readouterr().err
    assert not harbor.calls[0].task_path.exists()


def test_finite_unexpected_reward_is_a_mismatch(workspace: Path) -> None:
    assert execute(workspace, FixtureHarbor(reward=1.0)) == 1
    assert receipt(workspace)["results"][0]["status"] == "mismatch"
    assert receipt(workspace)["results"][0]["rewards"] == [1.0]


@pytest.mark.parametrize("solution", ["missing.sh", "task/controls", "../outside.sh"])
def test_bad_solution_path_fails_before_backend(workspace: Path, solution: str) -> None:
    path = workspace / "matrix.json"
    matrix = json.loads(path.read_text())
    matrix["runs"][0]["solution"] = solution
    path.write_text(json.dumps(matrix))
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 1
    assert harbor.calls == []
    result = receipt(workspace)["results"][0]
    assert result["status"] == "infra"
    assert "matrix solution" in result["error"]


def test_reuse_requires_matching_control_provenance(workspace: Path) -> None:
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 0
    assert execute(workspace, harbor, reuse=True) == 0
    assert len(harbor.calls) == 1
    (workspace / "task/controls/mutant.sh").write_text("#!/bin/sh\necho changed\n")
    assert execute(workspace, harbor, reuse=True) == 1
    assert len(harbor.calls) == 1
    assert receipt(workspace)["results"][0]["status"] == "infra"


def test_reuse_cannot_turn_nonzero_exit_into_a_pass(workspace: Path) -> None:
    harbor = FixtureHarbor("nonzero")
    assert execute(workspace, harbor) == 1
    assert execute(workspace, harbor, reuse=True) == 1
    assert receipt(workspace)["results"][0]["status"] == "infra"
    assert len(harbor.calls) == 1


def test_solution_staging_preserves_the_symlink_rejection_boundary(workspace: Path) -> None:
    outside = workspace / "outside"
    outside.write_text("not part of the task")
    (workspace / "task/tests/leak").symlink_to(outside)
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 1
    assert harbor.calls == []
    result = receipt(workspace)["results"][0]
    assert result["status"] == "infra"
    assert "reject symlinks" in result["error"]
    assert outside.read_text() == "not part of the task"


def test_no_override_keeps_the_original_task(workspace: Path) -> None:
    path = workspace / "matrix.json"
    matrix = json.loads(path.read_text())
    del matrix["runs"][0]["solution"]
    path.write_text(json.dumps(matrix))
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 0
    assert harbor.calls[0].task_path == workspace / "task"
    assert harbor.snapshots[0]["script"] == b"#!/bin/sh\necho original\n"
    assert harbor.snapshots[0]["controls_present"]
    assert (workspace / "task").is_dir()
