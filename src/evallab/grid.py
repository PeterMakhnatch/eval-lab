"""Parallel, model-free Docker grids over native Harbor job directories (HAR-204)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shlex
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StrictInt, model_validator

from evallab.campaign_execution import docker_available_resources, docker_task_resources
from evallab.dataset_audit_sources import resolve_harbor_dataset
from evallab.execution_contracts import RunRequest, new_ulid, validate_request
from evallab.results import JobRecord, load_jobs
from evallab.schemas import ContractModel

# Preserve the existing local-control posture across cells AND Harbor's internal slots.
LOCAL_TRIAL_CAP = 2
FREE_GRID_AGENTS = frozenset({"oracle", "nop", "cheat"})
PositiveInt = Annotated[StrictInt, Field(ge=1)]


class GridCell(ContractModel):
    task: str | None = Field(default=None, min_length=1)
    dataset: str | None = Field(default=None, min_length=1)
    agent: str = Field(min_length=1)
    attempts: PositiveInt = 1
    concurrency: PositiveInt = 1
    backend: str = "docker"

    @model_validator(mode="after")
    def one_source(self) -> GridCell:
        if (self.task is None) == (self.dataset is None):
            raise ValueError("each cell requires exactly one task directory or dataset ref")
        return self


class GridSpec(ContractModel):
    cells: list[GridCell] = Field(min_length=1)


def load_grid(path: Path) -> GridSpec:
    try:
        return GridSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"invalid grid {path}: {exc}") from exc


def refuse_paid_cells(spec: GridSpec) -> None:
    """No grid option can authorize a model or remote sandbox, even for oracle."""
    refused = [
        (index, cell)
        for index, cell in enumerate(spec.cells, 1)
        if cell.agent not in FREE_GRID_AGENTS or cell.backend != "docker"
    ]
    if refused:
        details = "; ".join(
            f"cell {index}: agent={cell.agent!r}, backend={cell.backend!r}"
            for index, cell in refused
        )
        raise ValueError(
            f"grid refuses non-free cells before executing any cell ({details}). "
            "Prepare a reviewed experiment spec with its real model/cost ceilings, then run: "
            "evallab submit <spec.json>; "
            "evallab approve <spec-id-from-submit> --actor peter; "
            "evallab tick --spec-id <spec-id-from-submit>. "
            "Remove these cells from the free grid; grid never submits, approves, or ticks them."
        )


@dataclass(frozen=True)
class CellOutcome:
    cell: int
    task: str
    agent: str
    status: str
    job_dir: Path | None = None
    concurrency: int = 0
    reason: str | None = None


class _DockerSlots:
    def __init__(self, cpus: float, memory_mb: int) -> None:
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.slots = LOCAL_TRIAL_CAP
        self.condition = threading.Condition()

    @contextmanager
    def reserve(self, cpus: float, memory_mb: int, slots: int) -> Iterator[None]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.cpus >= cpus and self.memory_mb >= memory_mb and self.slots >= slots
            )
            self.cpus -= cpus
            self.memory_mb -= memory_mb
            self.slots -= slots
        try:
            yield
        finally:
            with self.condition:
                self.cpus += cpus
                self.memory_mb += memory_mb
                self.slots += slots
                self.condition.notify_all()


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _cell_tasks(cell: GridCell, root: Path) -> list[Path]:
    if cell.task is not None:
        return [_resolve(root, Path(cell.task))]
    assert cell.dataset is not None
    dataset = resolve_harbor_dataset(cell.dataset, repo_root=root, download=False)
    return [task.path for task in dataset.tasks if task.path is not None]


def run_grid(
    spec: GridSpec,
    *,
    root: Path,
    jobs_dir: Path,
    width: int = LOCAL_TRIAL_CAP,
    execute: Callable[[RunRequest], Path] | None = None,
    resources: Callable[[], tuple[float, int]] = docker_available_resources,
) -> list[CellOutcome]:
    refuse_paid_cells(spec)
    if isinstance(width, bool) or width < 1:
        raise ValueError("grid width must be a positive integer")
    root, jobs_dir = root.resolve(), _resolve(root, jobs_dir)
    outcomes: list[CellOutcome] = []
    pending: list[tuple[int, RunRequest, float, int]] = []
    invocation = new_ulid().lower()
    for index, cell in enumerate(spec.cells, 1):
        if cell.agent == "cheat" and importlib.util.find_spec("evallab.harbor_cheat") is None:
            outcomes.append(
                CellOutcome(
                    index,
                    cell.task or cell.dataset or "",
                    cell.agent,
                    "skipped",
                    reason="evallab.harbor_cheat:CheatAgent is not installed",
                )
            )
            continue
        for task in _cell_tasks(cell, root):
            slug = re.sub(r"[^a-z0-9]+", "-", task.name.lower()).strip("-")[:18] or "task"
            name = f"grid-{index}-{slug}-{cell.agent}-{invocation}"
            request = RunRequest(
                task=task,
                agent=cell.agent,
                name=name,
                jobs_dir=jobs_dir,
                environment="docker",
                model=None,
                attempts=cell.attempts,
                concurrency=min(cell.concurrency, cell.attempts, LOCAL_TRIAL_CAP),
            )
            validate_request(request, repo_root=root)
            cpus, memory_mb = docker_task_resources(task)
            pending.append((index, request, cpus, memory_mb))
    if not pending:
        return outcomes
    available_cpus, available_memory = resources()
    slots = _DockerSlots(available_cpus, available_memory)
    runnable: list[tuple[int, RunRequest, float, int]] = []
    for index, request, cpus, memory in pending:
        concurrency = min(
            request.concurrency, math.floor(available_cpus / cpus), available_memory // memory
        )
        if concurrency < 1:
            outcomes.append(
                CellOutcome(
                    index,
                    str(request.task),
                    request.agent,
                    "skipped",
                    reason=f"declared task limits ({cpus:g} CPU, {memory} MiB) exceed Docker headroom "
                    f"({available_cpus:g} CPU, {available_memory} MiB)",
                )
            )
            continue
        runnable.append((index, replace(request, concurrency=concurrency), cpus, memory))
    if execute is None:
        from evallab.queue import Executor

        executor = Executor.from_repo(root, create_queue=False)

        def execute(request: RunRequest) -> Path:
            return executor.execute_direct(request, ingest=False)

    def run_one(item: tuple[int, RunRequest, float, int]) -> CellOutcome:
        index, request, cpus, memory = item
        try:
            with slots.reserve(
                cpus * request.concurrency, memory * request.concurrency, request.concurrency
            ):
                assert execute is not None
                job = execute(request)
            return CellOutcome(
                index, str(request.task), request.agent, "completed", job, request.concurrency
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return CellOutcome(
                index,
                str(request.task),
                request.agent,
                "failed",
                concurrency=request.concurrency,
                reason=str(exc),
            )

    with ThreadPoolExecutor(max_workers=min(width, LOCAL_TRIAL_CAP)) as pool:
        outcomes.extend(pool.map(run_one, runnable))
    return sorted(outcomes, key=lambda outcome: (outcome.cell, outcome.task))


@dataclass(frozen=True)
class CellComparison:
    job: str
    task: str
    agent: str
    backend: str
    n: int
    unscored: int
    pass_rate: float | None
    cracked_rate: float | None
    harbor_rev: str | None


def compare_jobs(jobs: Sequence[JobRecord]) -> list[CellComparison]:
    """Project native job evidence; exceptions/missing/nonfinite rewards are not failures."""
    rows: list[CellComparison] = []
    for job in jobs:
        groups: dict[tuple[str, str, str], list[float | None]] = {}
        for trial in job.trials:
            agent = str(
                (trial.result.get("agent_info") or {}).get("name")
                or (trial.config.get("agent") or {}).get("name")
                or "unknown"
            )
            if agent == "evallab.harbor_cheat:CheatAgent":
                agent = "cheat"
            backend = str(
                (trial.config.get("environment") or {}).get("type")
                or (job.config.get("environment") or {}).get("type")
                or "unknown"
            )
            key = (str(trial.result.get("task_name", "unknown")), agent, backend)
            reward = trial.primary_reward
            raw_reward = ((trial.result.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward"
            )
            if (
                trial.result.get("exception_info")
                or reward is None
                or isinstance(raw_reward, bool)
                or not math.isfinite(reward)
            ):
                reward = None
            groups.setdefault(key, []).append(reward)
        for (task, agent, backend), rewards in sorted(groups.items()):
            scored = [reward for reward in rewards if reward is not None]
            rate = sum(reward >= 1.0 for reward in scored) / len(scored) if scored else None
            # Cracked means the scripted cheater obtained a verifier pass, not model capability.
            rows.append(
                CellComparison(
                    job.name,
                    task,
                    agent,
                    backend,
                    len(rewards),
                    len(rewards) - len(scored),
                    rate,
                    rate if agent == "cheat" else None,
                    job.harbor_version,
                )
            )
    return rows


def render_compare(rows: Sequence[CellComparison]) -> str:
    lines = [
        "| cell (job) | task | agent | pass rate | cracked rate | N | unscored | backend | Harbor |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:

        def rate(value: float | None) -> str:
            return "—" if value is None else f"{value:.1%}"

        values = [
            row.job,
            row.task,
            row.agent,
            rate(row.pass_rate),
            rate(row.cracked_rate),
            str(row.n),
            str(row.unscored),
            row.backend,
            row.harbor_rev or "unknown",
        ]
        lines.append(
            "| "
            + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in values)
            + " |"
        )
    lines.append(
        "Rates use scored, exception-free finite rewards >= 1; N includes unscored trials."
    )
    return "\n".join(lines)


def grid_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    if args.grid_command == "compare":
        jobs = load_jobs([_resolve(root, path) for path in args.jobs])
        if not jobs:
            raise ValueError("no completed Harbor jobs found")
        print(render_compare(compare_jobs(jobs)))
        return 0
    spec = load_grid(_resolve(root, args.path))
    directory = args.jobs_dir or Path("runs") / f"grid-{new_ulid().lower()}"
    outcomes = run_grid(spec, root=root, jobs_dir=directory, width=args.width)
    for outcome in outcomes:
        print(
            f"cell {outcome.cell} {outcome.agent} {outcome.task}: {outcome.status} "
            f"{outcome.job_dir or outcome.reason} (concurrency={outcome.concurrency})"
        )
    jobs = [outcome.job_dir for outcome in outcomes if outcome.job_dir is not None]
    if jobs:
        print(render_compare(compare_jobs(load_jobs(jobs))))
        print("compare: evallab grid compare " + " ".join(shlex.quote(str(job)) for job in jobs))
    receipt_dir = _resolve(root, directory)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"grid-receipt-{new_ulid().lower()}.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "evallab.grid.receipt/v1",
                "grid": str(_resolve(root, args.path)),
                "width": min(args.width, LOCAL_TRIAL_CAP),
                "outcomes": [
                    {
                        "cell": outcome.cell,
                        "task": outcome.task,
                        "agent": outcome.agent,
                        "status": outcome.status,
                        "job_dir": str(outcome.job_dir) if outcome.job_dir else None,
                        "concurrency": outcome.concurrency,
                        "reason": outcome.reason,
                    }
                    for outcome in outcomes
                ],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"grid receipt: {receipt}")
    return 1 if any(outcome.status != "completed" for outcome in outcomes) else 0


def build_grid_parser(commands: argparse._SubParsersAction) -> None:
    grid = commands.add_parser("grid", help="Parallel $0 Docker controls and native-job comparison")
    actions = grid.add_subparsers(dest="grid_command", required=True)
    run = actions.add_parser("run", help="Run free local cells; refuse model/cloud cells")
    run.add_argument("path", type=Path, metavar="grid.yaml")
    run.add_argument(
        "--width",
        type=int,
        default=LOCAL_TRIAL_CAP,
        help="Maximum parallel cells (also clamped to local two-trial posture)",
    )
    run.add_argument("--jobs-dir", type=Path, help="Job/receipt root (default: runs/grid-<id>)")
    run.set_defaults(func=grid_command)
    compare = actions.add_parser("compare", help="Print verdict rates directly from Harbor jobs")
    compare.add_argument("jobs", type=Path, nargs="+", metavar="job-dir")
    compare.set_defaults(func=grid_command)
