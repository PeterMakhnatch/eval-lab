from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.execution_contracts import RunRequest
from evallab.grid import GridSpec, compare_jobs, load_grid, render_compare, run_grid
from evallab.results import load_job


def task(root: Path, name: str, *, cpus: float = 1, memory: int = 512) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "task.toml").write_text(
        f'[task]\nname = "{name}"\nversion = "1.0.0"\n'
        f"[environment]\ncpus = {cpus}\nmemory_mb = {memory}\n"
    )
    return path


def spec(*cells: dict) -> GridSpec:
    return GridSpec.model_validate({"cells": list(cells)})


def test_load_grid_task_and_dataset_sources(tmp_path: Path) -> None:
    path = tmp_path / "grid.yaml"
    path.write_text(
        "cells:\n"
        "  - {task: library/tasks/one, agent: oracle, attempts: 3, concurrency: 2, backend: docker}\n"
        "  - {dataset: benchmark@1.0, agent: nop}\n"
    )
    grid = load_grid(path)
    assert grid.cells[0].attempts == 3
    assert grid.cells[0].concurrency == 2
    assert grid.cells[1].dataset == "benchmark@1.0"


@pytest.mark.parametrize(
    "cell",
    [
        {"agent": "nop"},
        {"task": "a", "dataset": "b", "agent": "nop"},
        {"task": "a", "agent": "nop", "attempts": 0},
        {"task": "a", "agent": "nop", "concurrency": True},
        {"task": "a", "agent": "nop", "model": "paid/model"},
    ],
)
def test_malformed_cells_refuse(cell: dict) -> None:
    with pytest.raises(ValueError):
        spec(cell)


@pytest.mark.parametrize(
    "agent,backend",
    [
        ("terminus-2", "docker"),
        ("oracle", "daytona"),
        ("cheat", "modal"),
    ],
)
def test_paid_cells_refuse_whole_grid_before_any_io(
    tmp_path: Path,
    agent: str,
    backend: str,
) -> None:
    def forbidden(*args):
        pytest.fail("paid grid touched an execution/resource boundary")

    grid = spec(
        {"task": "unavailable", "agent": "nop"},
        {"task": "also-unavailable", "agent": agent, "backend": backend},
    )
    with pytest.raises(ValueError, match="evallab submit") as error:
        run_grid(grid, root=tmp_path, jobs_dir=Path("runs"), execute=forbidden, resources=forbidden)
    message = str(error.value)
    assert "evallab approve <spec-id-from-submit> --actor peter" in message
    assert "evallab tick --spec-id <spec-id-from-submit>" in message


def test_oracle_nop_two_tasks_execute_in_parallel_at_zero_cost(tmp_path: Path) -> None:
    task(tmp_path, "one")
    task(tmp_path, "two")
    rendezvous = threading.Barrier(2)
    guard = threading.Lock()
    active = peak = 0
    seen: list[RunRequest] = []

    def execute(request: RunRequest) -> Path:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            seen.append(request)
        rendezvous.wait(timeout=5)
        with guard:
            active -= 1
        return request.jobs_dir / request.name

    grid = spec(
        *({"task": name, "agent": agent} for name in ("one", "two") for agent in ("oracle", "nop"))
    )
    outcomes = run_grid(
        grid,
        root=tmp_path,
        jobs_dir=Path("runs"),
        width=8,
        execute=execute,
        resources=lambda: (8, 4096),
    )
    assert peak == 2
    assert {(request.task.name, request.agent) for request in seen} == {
        ("one", "oracle"),
        ("one", "nop"),
        ("two", "oracle"),
        ("two", "nop"),
    }
    assert all(
        request.model is None and not request.allow_billable and request.environment == "docker"
        for request in seen
    )
    assert all(outcome.status == "completed" for outcome in outcomes)


def test_width_and_internal_trials_share_local_cap(tmp_path: Path) -> None:
    task(tmp_path, "one")
    task(tmp_path, "two")
    calls: list[RunRequest] = []
    guard = threading.Lock()
    active_slots = peak = 0

    def execute(request: RunRequest) -> Path:
        nonlocal active_slots, peak
        with guard:
            active_slots += request.concurrency
            peak = max(peak, active_slots)
            calls.append(request)
        # A second two-slot cell cannot start before this one releases its slots.
        with guard:
            active_slots -= request.concurrency
        return request.jobs_dir / request.name

    grid = spec(
        *(
            {"task": name, "agent": "oracle", "attempts": 5, "concurrency": 5}
            for name in ("one", "two")
        )
    )
    result = run_grid(
        grid,
        root=tmp_path,
        jobs_dir=Path("runs"),
        width=1,
        execute=execute,
        resources=lambda: (8, 4096),
    )
    assert peak == 2
    assert all(request.concurrency == 2 and request.attempts == 5 for request in calls)
    assert all(outcome.status == "completed" for outcome in result)


def test_docker_headroom_clamps_concurrency_not_declared_task_limits(tmp_path: Path) -> None:
    path = task(tmp_path, "large", cpus=1.5, memory=2048)
    before = (path / "task.toml").read_bytes()
    requests = []

    def execute(request: RunRequest) -> Path:
        requests.append(request)
        return request.jobs_dir / request.name

    result = run_grid(
        spec({"task": "large", "agent": "nop", "attempts": 4, "concurrency": 4}),
        root=tmp_path,
        jobs_dir=Path("runs"),
        execute=execute,
        resources=lambda: (2, 3072),
    )
    assert result[0].status == "completed"
    assert requests[0].concurrency == 1
    assert requests[0].attempts == 4
    assert (path / "task.toml").read_bytes() == before


def test_unfit_cell_skips_without_execution(tmp_path: Path) -> None:
    task(tmp_path, "large", cpus=4, memory=8192)

    def forbidden(request):
        pytest.fail("unfit cell was executed")

    result = run_grid(
        spec({"task": "large", "agent": "oracle"}),
        root=tmp_path,
        jobs_dir=Path("runs"),
        execute=forbidden,
        resources=lambda: (2, 4096),
    )
    assert result[0].status == "skipped"
    assert "declared task limits" in (result[0].reason or "")


def test_cell_failure_releases_slots_and_other_cells_finish(tmp_path: Path) -> None:
    task(tmp_path, "one")
    task(tmp_path, "two")

    def execute(request: RunRequest) -> Path:
        if request.task.name == "one":
            raise RuntimeError("native harness failed")
        return request.jobs_dir / request.name

    result = run_grid(
        spec(*({"task": name, "agent": "nop"} for name in ("one", "two"))),
        root=tmp_path,
        jobs_dir=Path("runs"),
        execute=execute,
        resources=lambda: (1, 512),
    )
    assert [outcome.status for outcome in result] == ["failed", "completed"]
    assert result[0].reason == "native harness failed"


def test_missing_cheat_skips_with_reason_and_no_docker_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evallab.grid.importlib.util.find_spec", lambda name: None)

    def forbidden():
        pytest.fail("skipped cheat cell probed Docker")

    result = run_grid(
        spec({"task": "missing", "agent": "cheat"}),
        root=tmp_path,
        jobs_dir=Path("runs"),
        resources=forbidden,
    )
    assert result[0].status == "skipped"
    assert "evallab.harbor_cheat:CheatAgent" in (result[0].reason or "")


def job(root: Path, name: str, agent: str, rewards: list, *, exception: int | None = None) -> Path:
    path = root / name
    path.mkdir()
    (path / "result.json").write_text(
        json.dumps(
            {
                "id": name,
                "finished_at": "2026-10-08T00:00:00Z",
                "stats": {},
                "n_total_trials": len(rewards),
            }
        )
    )
    (path / "config.json").write_text(json.dumps({"environment": {"type": "docker"}}))
    (path / "lock.json").write_text(json.dumps({"harbor": {"version": "0.24.0"}}))
    for index, reward in enumerate(rewards):
        trial = path / f"task__{index}"
        trial.mkdir()
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "task",
                    "trial_name": trial.name,
                    "id": trial.name,
                    "agent_info": {"name": agent},
                    "verifier_result": {"rewards": {"reward": reward}}
                    if reward is not None
                    else None,
                    "exception_info": {"exception_type": "HarnessFailure"}
                    if exception == index
                    else None,
                }
            )
        )
    return path


def test_live_docker_admission_is_opt_in() -> None:
    """Attempt daemon admission only when explicitly requested; never in CI."""
    import os

    if os.environ.get("EVALLAB_GRID_DOCKER_SMOKE") != "1":
        pytest.skip("live Docker smoke is opt-in (EVALLAB_GRID_DOCKER_SMOKE=1)")
    from evallab.campaign_execution import docker_available_resources

    try:
        cpus, memory_mb = docker_available_resources()
    except (OSError, RuntimeError, ValueError) as exc:
        pytest.skip(f"shared daemon not admitted: {exc}")
    assert cpus >= 0 and memory_mb >= 0


def test_compare_math_uses_native_jobs_and_excludes_unscored(tmp_path: Path) -> None:
    oracle = job(tmp_path, "oracle", "oracle", [1, 0, None, float("nan"), 1, True], exception=4)
    cheat = job(tmp_path, "cheat", "evallab.harbor_cheat:CheatAgent", [1, 0.5, 1])
    rows = compare_jobs([load_job(oracle), load_job(cheat)])
    normal, cracked = rows
    assert (normal.n, normal.unscored, normal.pass_rate) == (6, 4, 0.5)
    assert normal.cracked_rate is None
    assert cracked.n == 3 and cracked.unscored == 0
    assert cracked.pass_rate == pytest.approx(2 / 3)
    assert cracked.cracked_rate == pytest.approx(2 / 3)
    assert cracked.backend == "docker" and cracked.harbor_rev == "0.24.0"
    table = render_compare(rows)
    assert "50.0%" in table and "66.7%" in table


def test_compare_all_unscored_is_unknown_not_zero(tmp_path: Path) -> None:
    path = job(tmp_path, "unknown", "nop", [None, float("inf")])
    row = compare_jobs([load_job(path)])[0]
    assert row.n == row.unscored == 2
    assert row.pass_rate is None


def test_compare_cli_reads_jobs_without_store(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    paths = [job(tmp_path, "oracle", "oracle", [1]), job(tmp_path, "nop", "nop", [0])]
    assert run_cli(["grid", "compare", *map(str, paths)], workspace=tmp_path) == 0
    output = capsys.readouterr().out
    assert "100.0%" in output and "0.0%" in output
    assert "docker" in output and "0.24.0" in output
    assert not (tmp_path / "queue").exists()
