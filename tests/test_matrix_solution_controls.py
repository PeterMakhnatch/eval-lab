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
            agent = trial / "agent"
            agent.mkdir()
            if self.failure != "missing_oracle_log":
                (agent / "oracle.txt").write_text("")
            if self.failure == "oracle_exit" and attempt == call.n_attempts - 1:
                (agent / "exit-code.txt").write_text("127")
            elif self.failure == "invalid_oracle_exit":
                (agent / "exit-code.txt").write_text("unknown")
            reward = float("nan") if self.failure == "nan" else self.reward
            (trial / "result.json").write_text(json.dumps({
                "task_name": "task",
                "trial_name": trial.name,
                "finished_at": "2026-09-11T12:00:01Z",
                "agent_execution": (
                    None if self.failure == "unexecuted" else {
                        "started_at": "2026-09-11T12:00:00Z",
                        "finished_at": (
                            None if self.failure == "unfinished_oracle"
                            else "2026-09-11T12:00:01Z"
                        ),
                    }
                ),
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


@pytest.mark.parametrize("failure", [
    "exception", "missing_result", "missing_reward", "nan", "nonzero",
    "oracle_exit", "invalid_oracle_exit", "missing_oracle_log", "unexecuted", "unfinished_oracle",
])
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
    solution = workspace / "task/controls/mutant.sh"
    original = solution.read_bytes()
    saved_receipt = receipt(workspace)
    solution.write_text("#!/bin/sh\necho changed\n")
    assert execute(workspace, harbor, reuse=True) == 1
    assert len(harbor.calls) == 1
    assert receipt(workspace) == saved_receipt
    solution.write_bytes(original)
    assert execute(workspace, harbor, reuse=True) == 0
    assert len(harbor.calls) == 1


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


def test_failed_oracle_in_later_attempt_invalidates_the_control(workspace: Path) -> None:
    path = workspace / "matrix.json"
    matrix = json.loads(path.read_text())
    matrix["runs"][0]["attempts"] = 2
    path.write_text(json.dumps(matrix))
    assert execute(workspace, FixtureHarbor("oracle_exit")) == 1
    result = receipt(workspace)["results"][0]
    assert result["status"] == "infra"
    assert result["rewards"] == []


@pytest.mark.parametrize("step_solution", [False, True])
def test_declared_steps_refuse_override_before_dispatch_and_provenance(
    workspace: Path, step_solution: bool
) -> None:
    task = workspace / "task"
    (task / "task.toml").write_text('version = "1.0"\n[[steps]]\nname = "prepare"\n')
    if step_solution:
        solution = task / "steps/prepare/solution"
        solution.mkdir(parents=True)
        (solution / "solve.sh").write_text("#!/bin/sh\necho original step\n")
    original = compute_task_digests(task).package
    harbor = FixtureHarbor(reward=1.0)
    path = workspace / "matrix.json"
    matrix = json.loads(path.read_text())
    matrix["runs"][0]["expect_reward"] = 1.0
    path.write_text(json.dumps(matrix))
    assert execute(workspace, harbor) == 1
    assert harbor.calls == []
    result = receipt(workspace)["results"][0]
    assert result["status"] == "infra"
    assert result["rewards"] == []
    assert "solution_sha256" not in result
    assert "staged_task_digest" not in result
    assert compute_task_digests(task).package == original


def test_refused_rerun_preserves_receipt_and_remains_reusable(workspace: Path) -> None:
    harbor = FixtureHarbor()
    assert execute(workspace, harbor) == 0
    receipt_path = workspace / "runs/.executor/01ARZ3NDEKTSV4RRFFQ69G5FAV.matrix.json"
    successful_receipt = receipt_path.read_bytes()
    job = workspace / "runs/mutant"
    job_bytes = {path.relative_to(job): path.read_bytes() for path in job.rglob("*") if path.is_file()}
    assert execute(workspace, harbor) == 1
    assert len(harbor.calls) == 1
    assert receipt_path.read_bytes() == successful_receipt
    invocations = [
        json.loads(line)
        for line in receipt_path.with_suffix(".invocations.jsonl").read_text().splitlines()
    ]
    assert [row["result"]["status"] for row in invocations] == ["ok", "infra"]
    assert invocations[-1]["result"]["rewards"] == []
    assert execute(workspace, harbor, reuse=True) == 0
    assert len(harbor.calls) == 1
    assert receipt_path.read_bytes() == successful_receipt
    assert {
        path.relative_to(job): path.read_bytes() for path in job.rglob("*") if path.is_file()
    } == job_bytes
