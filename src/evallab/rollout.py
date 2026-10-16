"""Batch checkpoint evaluation on Modal (the rollout engine).

Runs many Harbor trials at once in Modal sandboxes through the same
``RunRequest`` / ``Executor`` path as the MiMo census, for one model
selector (a Tinker base model or a trained checkpoint), and reports the
two numbers the training loop needs: tasks/hour and cost per task.

Cost accounting: model tokens come from each job's ``lab-metadata.json``
provider usage (settled by the proxy ledger at the pinned Tinker prices);
the Modal sandbox is estimated from Harbor trial seconds at Modal list
prices (CPU $0.0000131/core/s, memory $0.00000222/GiB/s) using the task's
declared ``[environment]`` resources. Estimates are labelled as such and
never mixed with settled token dollars in the per-trial rows; the batch
also writes ``spend.jsonl`` rows tagged for the day ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MODAL_APP_NAME = "eval-rollout"
SPEND_TAG = "2026-10-16"
MODAL_CPU_USD_PER_CORE_SECOND = 0.0000131
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000222
DEFAULT_AGENT = "terminus-2"


@dataclass(frozen=True)
class TrialOutcome:
    task: str
    name: str
    reward: float | None
    passed: bool | None
    wall_seconds: float
    sandbox_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    model_cost_usd: float | None
    sandbox_cost_usd: float | None
    job_dir: str
    error: str | None


def sandbox_cost_usd(seconds: float, *, cpus: float, memory_mb: float) -> float:
    """Modal list-price estimate for one sandbox of the given shape."""
    gib = memory_mb / 1024.0
    return seconds * (cpus * MODAL_CPU_USD_PER_CORE_SECOND + gib * MODAL_MEMORY_USD_PER_GIB_SECOND)


def task_resources(task: Path) -> tuple[float, float]:
    """Declared ``[environment]`` cpus/memory_mb, else Harbor-ish defaults."""
    try:
        document = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return 1.0, 2048.0
    environment = document.get("environment", {})
    cpus = environment.get("cpus", 1)
    memory_mb = environment.get("memory_mb", 2048)
    return float(cpus), float(memory_mb)


def _task_id(task: Path) -> str:
    """The task package's declared canonical id (bare, no dataset prefix).

    From ``[task] name`` (``mimo-v2.6-rl/format-code-task-000142`` ->
    ``format-code-task-000142``), falling back to ``[metadata]
    source_id``, then the directory name. Specs must carry this: the
    setup fingerprint keys ledger and lineage lookups by it, and the
    directory name is a package digest for store variants, not an id.
    """
    try:
        document = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return task.name
    name = document.get("task", {}).get("name")
    if isinstance(name, str) and name:
        return name.split("/")[-1]
    source_id = document.get("metadata", {}).get("source_id")
    if isinstance(source_id, str) and source_id:
        return source_id
    return task.name


def summarize(outcomes: list[TrialOutcome], batch_wall_seconds: float) -> dict[str, Any]:
    """Batch summary: throughput and cost per task over completed trials."""
    completed = [o for o in outcomes if o.error is None]
    passed = [o for o in completed if o.passed]
    costs = [
        (o.model_cost_usd or 0.0) + (o.sandbox_cost_usd or 0.0) for o in completed
    ]
    hours = batch_wall_seconds / 3600.0
    return {
        "trials": len(outcomes),
        "completed": len(completed),
        "errors": len(outcomes) - len(completed),
        "passed": len(passed),
        "pass_rate": (len(passed) / len(completed)) if completed else None,
        "batch_wall_seconds": round(batch_wall_seconds, 1),
        "tasks_per_hour": (len(completed) / hours) if hours > 0 else None,
        "total_cost_usd": round(sum(costs), 4),
        "cost_per_task_usd": (round(sum(costs) / len(completed), 4) if completed else None),
    }


def _trial_reward(job_dir: Path) -> float | None:
    for result_path in sorted(job_dir.rglob("result.json")):
        if result_path.parent == job_dir:
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        verifier_result = data.get("verifier_result") or {}
        rewards = verifier_result.get("rewards") or {}
        if isinstance(reward, (int, float)):
            return float(reward)
    return None


def _trial_error(job_dir: Path) -> str | None:
    """The trial's recorded exception, when its result carries one."""
    for result_path in sorted(job_dir.rglob("result.json")):
        if result_path.parent == job_dir:
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exc = data.get("exception_info") if isinstance(data, dict) else None
        if exc:
            kind = exc.get("exception_type") or exc.get("type") if isinstance(exc, dict) else None
            return f"trial exception: {kind or 'unknown'}"
    return None


def _outcome(
    task: Path, name: str, job_dir: Path, wall_seconds: float, error: str | None
) -> TrialOutcome:
    reward = _trial_reward(job_dir) if error is None else None
    if error is None and reward is None:
        # A trial that died before grading has no reward; record its
        # exception so the row is an explicit error, never a silent pass.
        error = _trial_error(job_dir)
    sandbox_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_cost: float | None = None
    metadata_path = job_dir / "lab-metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        usage = metadata.get("provider_usage", {})
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(usage.get("cost_usd"), (int, float)):
            model_cost = float(usage["cost_usd"])
        started, finished = metadata.get("started_at"), metadata.get("finished_at")
        if isinstance(started, str) and isinstance(finished, str):
            from datetime import datetime

            try:
                delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                sandbox_seconds = delta.total_seconds()
            except ValueError:
                sandbox_seconds = None
    cpus, memory_mb = task_resources(task)
    sandbox_cost = (
        sandbox_cost_usd(sandbox_seconds, cpus=cpus, memory_mb=memory_mb)
        if sandbox_seconds is not None
        else None
    )
    return TrialOutcome(
        task=str(task),
        name=name,
        reward=reward,
        passed=(reward >= 1.0) if reward is not None else None,
        wall_seconds=round(wall_seconds, 1),
        sandbox_seconds=sandbox_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_cost_usd=model_cost,
        sandbox_cost_usd=round(sandbox_cost, 6) if sandbox_cost is not None else None,
        job_dir=str(job_dir),
        error=error,
    )


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in name)[:36]


_NAME_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _arm_slug(model: str) -> str:
    """Arm namespace carried in the job name, e.g. ``...-rds1-b``.

    Deterministic transform of the selector tail: lowercase, then every
    character outside ``[a-z0-9-]`` becomes ``-`` — the ExperimentSpec
    name alphabet (``^[a-z0-9][a-z0-9-]+$``). No truncation: the full
    tail survives, so model-version and adapter distinctions cannot
    collapse. Arm map for the self-hosted MiMo selectors:
    base -> ``mimo-v2-6-distill-qwen-9b``,
    ``:rds1-a`` -> ``mimo-v2-6-distill-qwen-9b-rds1-a``,
    ``:rds1-b`` -> ``mimo-v2-6-distill-qwen-9b-rds1-b``,
    ``:rds1-c`` -> ``mimo-v2-6-distill-qwen-9b-rds1-c``.
    Arm-disjoint by construction: two arms of one batch never share a
    job address under the shared ``runs`` jobs root, so a later arm
    cannot clobber an earlier arm's retained jobs (regrade custody
    reads them). Outcome rows carry the resulting ``job_dir``, keeping
    every row traceable to its arm and spec.
    """
    tail = model.rsplit('/', 1)[-1].replace(':', '-').lower()
    return "".join(c if c in _NAME_ALPHABET else "-" for c in tail)


def validate_model_selector(model: str | None) -> str:
    """Fail fast on a selector the harness cannot route; return its kind.

    Admitted: Tinker base/checkpoint selectors, the pinned OpenRouter
    routes, and the self-hosted MiMo base plus its LoRA adapters
    (``selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B[:rds1-a|...]``,
    admitted by #941). The self-hosted route also needs the LoRA server
    address in ``EVALLAB_MIMO_SELFHOSTED_UPSTREAM``; refuse here, before
    any queue submission, when it is missing.
    """
    import os

    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_UPSTREAM_ENV,
        is_mimo_selfhosted_model,
        is_openrouter_model,
        is_tinker_terminus_model,
        parse_mimo_selfhosted_model,
        parse_openrouter_model,
        parse_tinker_model,
    )

    if is_tinker_terminus_model(model):
        parse_tinker_model(model)
        return "tinker"
    if is_openrouter_model(model):
        parse_openrouter_model(model)
        return "openrouter"
    if is_mimo_selfhosted_model(model):
        parse_mimo_selfhosted_model(model)
        if not os.environ.get(MIMO_SELFHOSTED_UPSTREAM_ENV):
            raise ValueError(
                f"self-hosted MiMo trials need {MIMO_SELFHOSTED_UPSTREAM_ENV} "
                "set to the LoRA server URL (Trainer deploys it with "
                "EVALLAB_MIMO_LORAS=rds1-a=..., one server for all arms)"
            )
        return "selfhosted"
    raise ValueError(
        f"unroutable model selector {model!r}: expected a tinker/, "
        "openrouter-metered/, or selfhosted/ selector admitted by the harness"
    )


def run_rollout(
    tasks: list[Path],
    *,
    model: str | None,
    agent: str = DEFAULT_AGENT,
    concurrency: int = 8,
    out_dir: Path,
    root: Path,
    timeout_seconds: int = 1200,
    cost_limit_usd: float | None = None,
    actor: str | None = None,
    est_cost_usd: float = 0.25,
    attempts: int = 1,
    max_requests: int = 300,
    max_input_tokens: int = 2_000_000,
    max_output_tokens: int = 300_000,
    execute: Any = None,
    poll_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run one trial per task on Modal; write batch outputs.

    Billable agents cannot use direct execution (the standing-policy queue
    owns spend consent), so the production path submits one ExperimentSpec
    per task, records the caller's approval, and drains the queue with
    ``tick``. ``execute`` is a test seam with the direct-call signature.
    """
    if execute is None:
        validate_model_selector(model)
    jobs_dir = out_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    batch_start = time.monotonic()

    if execute is not None:
        from evallab.execution_contracts import RunRequest

        def one(pair: tuple[int, Path, int]) -> TrialOutcome:
            index, task, attempt = pair
            suffix = f"-a{attempt}" if attempts > 1 else ""
            name = f"rollout-{index:03d}-{_slug(task.name)}{suffix}"
            request = RunRequest(
                task=task,
                agent=agent,
                name=name,
                jobs_dir=jobs_dir,
                environment="modal",
                model=model,
                concurrency=1,
                attempts=1,
                timeout_seconds=timeout_seconds,
                allow_billable=True,
                cost_limit_usd=cost_limit_usd,
                modal_app_name=MODAL_APP_NAME,
            )
            start = time.monotonic()
            try:
                job_dir = execute(request)
                return _outcome(task, name, Path(job_dir), time.monotonic() - start, None)
            except Exception as exc:  # noqa: BLE001 - one trial must not kill the batch
                return _outcome(
                    task, name, jobs_dir / name, time.monotonic() - start,
                    f"{type(exc).__name__}: {exc}",
                )

        pairs = [
            (index, task, attempt)
            for index, task in enumerate(tasks)
            for attempt in range(1, max(1, attempts) + 1)
        ]
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            outcomes = list(pool.map(one, pairs))
        batch_wall = time.monotonic() - batch_start
        return _write_batch(out_dir, outcomes, batch_wall, model=model, agent=agent, concurrency=concurrency, attempts=attempts)

    if not actor:
        raise ValueError(
            "a billable rollout requires an authorizing actor "
            "(--actor 'name as Peter's delegate, cap and steer'); never defaulted"
        )
    if model is None:
        raise ValueError("a queued rollout requires a model selector")
    from evallab.execution_contracts import is_mimo_selfhosted_model, new_ulid
    from evallab.queue import Executor
    from evallab.schemas import ExperimentSpec

    root = root.resolve()
    executor = Executor.from_repo(root)
    mimo_fields: dict[str, Any] = {}
    if is_mimo_selfhosted_model(model):
        # HAR-149: a MiMo batch names its reference setup, locks egress,
        # and declares every intended difference. The harness additions
        # and the request ceiling are the campaign's approved deviations
        # (Research, 2026-10-16); the lock and ledger gates stay hard.
        from evallab.mimoagent_worker import WRAPPER_ADDITIONS
        from evallab.schemas import ReferenceDeviation

        mimo_fields = {
            "reference_profile": "xiaomi-mimo-rl",
            "egress_lock": True,
            "deviations": [
                ReferenceDeviation(
                    field="harness.additions",
                    value=dict(WRAPPER_ADDITIONS),
                    reason=(
                        "Eval Lab mimoagent wrapper additions are part of the "
                        "lab harness, not the Xiaomi reference harness; "
                        "approved campaign deviation (Research, 2026-10-16)."
                    ),
                ),
                ReferenceDeviation(
                    field="budgets.max_requests",
                    value=max_requests,
                    reason=(
                        "Campaign per-trial request ceiling below the "
                        "reference 500-step limit; approved campaign "
                        "deviation (Research, 2026-10-16)."
                    ),
                ),
            ],
        }
    specs: list[ExperimentSpec] = []
    for index, task in enumerate(tasks):
        task = task.resolve()
        try:
            task_rel = task.relative_to(root).as_posix()
        except ValueError:
            raise ValueError(f"task must be inside the repo root: {task}") from None
        for attempt in range(1, max(1, attempts) + 1):
            suffix = f"-a{attempt}" if attempts > 1 else ""
            spec = ExperimentSpec(
                spec_id=new_ulid(),
                name=f"rollout-{_arm_slug(model)}-{index:03d}-{_slug(task.name)}{suffix}",
                hypothesis=f"Rollout throughput/cost measurement for {model} on held-out tasks",
                purpose="baseline",
                task=task_rel,
                task_path=task_rel,
                task_id=_task_id(task),
                agent=agent,
                model=model,
                environment="modal",
                jobs_dir="runs",
                submitted_by="infra-rollout",
                timeout_seconds=timeout_seconds,
                est_cost_usd=est_cost_usd,
                cost_limit_usd=cost_limit_usd,
                max_requests=max_requests,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                max_total_tokens=max_input_tokens + max_output_tokens,
                concurrency=1,
                attempts=1,
                **mimo_fields,
            )
            executor.submit(spec)
            executor.queue.approve(str(spec.spec_id), actor=actor)
            specs.append(spec)

    spec_ids = [str(spec.spec_id) for spec in specs]
    deadline = batch_start + timeout_seconds * 2 + 1800
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        executor.tick(parallel=max(1, concurrency), spec_ids=spec_ids)
        states = {}
        for spec in specs:
            path = executor.queue.locate(str(spec.spec_id))
            states[str(spec.spec_id)] = path.parent.name
        if all(state in {"done", "failed"} for state in states.values()):
            break
        time.sleep(poll_seconds)
    batch_wall = time.monotonic() - batch_start

    outcomes = []
    for spec in specs:
        state = states.get(str(spec.spec_id), "unknown")
        job_dir = root / spec.jobs_dir / spec.name
        error = None if state == "done" else f"queue state: {state}"
        outcomes.append(
            _outcome(root / spec.task, spec.name, job_dir, batch_wall, error)
        )
    return _write_batch(out_dir, outcomes, batch_wall, model=model, agent=agent, concurrency=concurrency, attempts=attempts)


def _write_batch(
    out_dir: Path,
    outcomes: list[TrialOutcome],
    batch_wall: float,
    *,
    model: str | None,
    agent: str,
    concurrency: int,
    attempts: int = 1,
) -> dict[str, Any]:
    summary = summarize(outcomes, batch_wall)
    summary.update(
        {"model": model, "agent": agent, "concurrency": concurrency, "attempts": attempts}
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text(
        "".join(json.dumps(asdict(o)) + "\n" for o in outcomes), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "spend.jsonl").write_text(
        json.dumps(
            {
                "tag": SPEND_TAG,
                "label": "rollout",
                "model": model,
                "cost_usd": summary["total_cost_usd"],
                "basis": "settled model tokens + Modal list-price sandbox estimate",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def _rollout_run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    tasks: list[Path] = []
    for raw in args.task or []:
        tasks.append(Path(raw).expanduser())
    if args.tasks_file:
        for line in Path(args.tasks_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tasks.append(Path(line).expanduser())
    if args.tasks_dir:
        base = Path(args.tasks_dir).expanduser()
        tasks.extend(sorted(p.parent for p in base.glob("*/task.toml")))
    tasks = [t if t.is_absolute() else (root / t) for t in tasks]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("error: no tasks given (--task, --tasks-file, or --tasks-dir)")
        return 2
    missing = [t for t in tasks if not (t / "task.toml").is_file()]
    if missing:
        print(f"error: task dirs without task.toml: {[str(t) for t in missing[:5]]}")
        return 2
    if not args.actor:
        print(
            "error: --actor is required (who authorizes this spend, recorded in the queue)",
            file=sys.stderr,
        )
        return 2
    summary = run_rollout(
        tasks,
        model=args.model,
        agent=args.agent,
        concurrency=args.concurrency,
        out_dir=Path(args.out).expanduser(),
        root=root,
        timeout_seconds=args.timeout,
        cost_limit_usd=args.cost_limit_usd,
        actor=args.actor,
        est_cost_usd=args.est_cost_usd,
        max_requests=args.max_requests,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        attempts=args.attempts,
    )
    print(json.dumps(summary, indent=2))
    print(f"results: {args.out}/results.jsonl")
    return 0 if summary["errors"] == 0 else 1


def build_rollout_parser(commands: argparse._SubParsersAction) -> None:  # noqa: SLF001
    """Register the ``evallab rollout`` subcommand (one self-contained block)."""
    parser = commands.add_parser(
        "rollout",
        help="Batch checkpoint evaluation on Modal (tasks/hour, cost/task)",
        description=(
            "Run many Harbor trials at once in Modal sandboxes for one model "
            "selector (Tinker base model or trained checkpoint) and report "
            "tasks/hour and cost per task."
        ),
    )
    sub = parser.add_subparsers(dest="rollout_command", required=True)
    run = sub.add_parser("run", help="Run a batch of tasks on Modal")
    run.add_argument("--task", action="append", help="Task package dir (repeatable)")
    run.add_argument("--tasks-file", help="File with one task package dir per line")
    run.add_argument("--tasks-dir", help="Dir whose immediate children are task packages")
    run.add_argument(
        "--model",
        required=True,
        help="Model selector, e.g. tinker/Qwen/Qwen3.5-9B or tinker/<base>@tinker://<run>:train:0/sampler_weights/10",
    )
    run.add_argument("--agent", default=DEFAULT_AGENT, help=f"Harbor agent (default: {DEFAULT_AGENT})")
    run.add_argument("--concurrency", type=int, default=8, help="Trials in flight at once (default: 8)")
    run.add_argument("--timeout", type=int, default=1200, help="Per-trial timeout seconds (default: 1200)")
    run.add_argument("--limit", type=int, default=None, help="Only the first N tasks")
    run.add_argument(
        "--cost-limit-usd", type=float, default=None, help="Per-trial model cost ceiling"
    )
    run.add_argument("--out", required=True, help="Output dir (results.jsonl, summary.json, spend.jsonl)")
    run.add_argument(
        "--actor",
        required=True,
        help="Who authorizes this spend (recorded in queue/events.jsonl; never defaulted)",
    )
    run.add_argument(
        "--est-cost-usd", type=float, default=0.25, help="Estimated cost per trial for queue admission (default: 0.25)"
    )
    run.add_argument("--max-requests", type=int, default=300, help="Per-trial provider request ceiling (default: 300)")
    run.add_argument("--max-input-tokens", type=int, default=2_000_000, help="Per-trial input token ceiling (default: 2000000)")
    run.add_argument("--max-output-tokens", type=int, default=300_000, help="Per-trial output token ceiling (default: 300000)")
    run.add_argument(
        "--attempts", type=int, default=1,
        help="Independent trials per task (default: 1); use >1 to average over serving noise",
    )
    run.set_defaults(func=_rollout_run_command)
