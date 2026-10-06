"""Local, zero-execution previews of Harbor's authoritative trial diff.

The queue keeps its existing authorization and runner. This module only resolves
local source evidence and feeds the same staged target inputs to Harbor's planner.
Reuse records reference old trial IDs; they are not additional observations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from evallab.execution_contracts import RunRequest, build_command, subscription_command
from evallab.results import discover_job_dirs
from evallab.schemas import ExperimentSpec
from evallab.setup_fingerprint import lock_setup_fingerprint


@dataclass(frozen=True)
class TrialDiff:
    attempt: int
    action: str
    task: str
    reason: str
    source_job: str | None = None
    source_trial: str | None = None
    source_trial_id: str | None = None
    source_lock_sha256: str | None = None


@dataclass(frozen=True)
class DiffPreview:
    trials: tuple[TrialDiff, ...]
    sources: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    harbor_version: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(trial.action for trial in self.trials)
        return {action: counts[action] for action in ("reuse", "regrade", "rerun")}

    @property
    def all_reused(self) -> bool:
        return bool(self.trials) and all(trial.action == "reuse" for trial in self.trials)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "evallab.job_diff/v1",
            "harbor_version": self.harbor_version,
            "counts": self.counts,
            "sources": [str(path) for path in self.sources],
            "trials": [asdict(trial) for trial in self.trials],
            "warnings": list(self.warnings),
        }


def _cohort_cell(spec: ExperimentSpec) -> tuple[Any, ...] | None:
    # A research question/card can span independent replicate attempts.
    cohort = (
        ("campaign-attempt", spec.campaign_attempt_id)
        if spec.campaign_attempt_id
        else ("grid", spec.grid_id)
    )
    if cohort[1] is None:
        return None
    point = spec.grid_point or {}
    return (
        cohort,
        spec.task_id or spec.task,
        spec.task_instance_id,
        spec.generator_seed,
        point.get("arm_id"),
        json.dumps(point.get("factors", {}), sort_keys=True),
        json.dumps(point.get("factor_bindings", {}), sort_keys=True),
        spec.campaign_attempt_id,
    )


def diff_sources(request: RunRequest, *, repo_root: Path) -> tuple[Path, ...]:
    """Explicit approval-bound sources, or finished jobs in the same cohort cell.

    A campaign attempt must match exactly. For ordinary multi-attempt jobs Harbor
    consumes each distinct source UUID once, so increasing N requires new trials.
    An explicit empty list opts out when an independent stochastic draw is wanted.
    """
    if request.diff_sources:
        return tuple(path.resolve() for path in request.diff_sources)
    spec = request.experiment_spec
    if spec is None:
        return ()
    if spec.diff_sources is not None:
        return tuple(
            dict.fromkeys((repo_root / Path(path)).resolve() for path in spec.diff_sources)
        )
    cell = _cohort_cell(spec)
    if cell is None:
        return ()
    result = []
    for job in discover_job_dirs([request.jobs_dir]):
        if job.resolve() == (request.jobs_dir / request.name).resolve():
            continue
        try:
            previous = ExperimentSpec.model_validate_json(
                (job / "experiment-spec.json").read_text()
            )
        except (OSError, ValueError):
            continue
        if _cohort_cell(previous) == cell:
            result.append(job.resolve())
    return tuple(sorted(set(result)))


def harbor_job_config(
    request: RunRequest, *, repo_root: Path, setup_fingerprint: str | None = None
) -> Any:
    """Parse the real argv with Harbor's own config-only CLI callback.

    Never call ``start``: that callback can execute a job. Using the shared
    config builder avoids a second mapping of the lab's agent options/overlays.
    """
    import typer  # ty: ignore[unresolved-import]
    from harbor.cli.jobs import build_job_config  # ty: ignore[unresolved-import]
    from typer.main import get_command  # ty: ignore[unresolved-import]

    command = build_command(
        replace(request, diff_sources=()),
        setup_fingerprint=(
            setup_fingerprint
            if setup_fingerprint is not None
            else lock_setup_fingerprint(request, repo_root=repo_root)
        ),
    )
    command = subscription_command(request, command, repo_root=repo_root)
    harbor_index = next(
        index
        for index, arg in enumerate(command[:-1])
        if Path(arg).name == "harbor" and command[index + 1] == "run"
    )
    app = typer.Typer(add_completion=False)
    app.command()(build_job_config)
    parser = get_command(app)
    with parser.make_context("harbor-config", command[harbor_index + 2 :]) as context:
        return parser.invoke(context)


def _local_plan(request: RunRequest, *, repo_root: Path, setup_fingerprint: str) -> Any:
    from harbor.job_plan import JobPlan  # ty: ignore[unresolved-import]
    from harbor.tasks.client import TaskDownloadResult  # ty: ignore[unresolved-import]

    config = harbor_job_config(request, repo_root=repo_root, setup_fingerprint=setup_fingerprint)
    if any(not dataset.is_local() for dataset in config.datasets):
        raise ValueError("diff preflight requires locally resolved task packages")
    tasks = asyncio.run(JobPlan.resolve_task_configs(config))
    if any(task.is_git_task() or task.is_package_task() for task in tasks):
        raise ValueError("diff preflight requires locally resolved task packages")
    for agent in config.agents:
        if any(not Path(skill).is_dir() for skill in agent.skills):
            raise ValueError("diff preflight requires locally resolved skills")
    downloads = {
        task.get_task_id(): TaskDownloadResult(
            path=task.get_local_path(), download_time_sec=0.0, cached=True
        )
        for task in tasks
    }
    return JobPlan.from_resolved(
        config, task_configs=tasks, metrics={}, task_download_results=downloads
    )


def preview_diff(request: RunRequest, *, repo_root: Path) -> DiffPreview:
    """Show per-trial reuse/regrade/rerun without agents, sandboxes or downloads."""
    sources = diff_sources(request, repo_root=repo_root)
    if not sources:
        return DiffPreview(
            trials=tuple(
                TrialDiff(index + 1, "rerun", request.task.name, "no selected prior source job")
                for index in range(request.attempts)
            )
        )
    from harbor.job_diff import (  # ty: ignore[unresolved-import]
        compare_job_trials,
        resolve_diff_sources,
    )

    from evallab.runner import stage_request_task

    with TemporaryDirectory(prefix="evallab-diff-") as temporary:
        stage_root = Path(temporary).resolve()
        task, _ = stage_request_task(request, stage_root / "task")
        staged_request = replace(request, task=task)
        if request.toolbox_path is not None:
            from evallab.toolbox import stage_toolbox

            skill, _ = stage_toolbox(
                request.toolbox_path,
                request.toolbox_sha256,
                staging_root=stage_root / "toolbox",
                repo_root=repo_root,
            )
            staged_request = replace(staged_request, skill=skill)
        plan = _local_plan(
            staged_request,
            repo_root=repo_root,
            setup_fingerprint=lock_setup_fingerprint(request, repo_root=repo_root),
        )
        version = plan.job_lock.harbor.version
        compatible = []
        warnings = []
        for source in sources:
            # Old locks are readable evidence, but not comparable identities.
            try:
                lock = json.loads((source / "lock.json").read_text())
                result = json.loads((source / "result.json").read_text())
            except (OSError, ValueError) as exc:
                raise ValueError(f"diff source job lock/result is unreadable: {source}") from exc
            if not isinstance(result, dict) or not result.get("finished_at"):
                raise ValueError(f"diff source job is not finished: {source}")
            if not isinstance(lock, dict) or not isinstance(lock.get("harbor", {}), dict):
                raise ValueError(f"diff source job lock is malformed: {source}")
            source_version = lock.get("harbor", {}).get("version")
            if version is None or source_version != version:
                warnings.append(f"{source}: Harbor {source_version!r} != {version!r}; not reused")
            else:
                compatible.append(source)
        resolved, source_trials = (
            asyncio.run(
                resolve_diff_sources([str(path) for path in compatible], jobs_dir=request.jobs_dir)
            )
            if compatible
            else ([], [])
        )
        diff = compare_job_trials(plan, source_trials)
        if diff.skipped_source_trials:
            warnings.append(
                f"{diff.skipped_source_trials} source trials have no semantic task version"
            )
        if diff.n_regrade_downgraded:
            warnings.append(f"{diff.n_regrade_downgraded} regrades require reruns (not regradable)")
        trials = []
        for index, trial in enumerate(diff.trials):
            source = trial.source
            trials.append(
                TrialDiff(
                    attempt=index + 1,
                    action=trial.action.value,
                    task=trial.task_name,
                    reason=trial.reason,
                    source_job=source.job_label if source else None,
                    source_trial=str(source.trial_dir) if source else None,
                    source_trial_id=str(source.id) if source else None,
                    source_lock_sha256=(
                        "sha256:"
                        + hashlib.sha256((source.trial_dir / "lock.json").read_bytes()).hexdigest()
                        if source
                        else None
                    ),
                )
            )
        return DiffPreview(
            trials=tuple(trials),
            sources=tuple(path for _, path in resolved),
            warnings=tuple(warnings),
            harbor_version=version,
        )


def render_diff(preview: DiffPreview) -> str:
    counts = preview.counts
    lines = [
        "HARBOR DIFF (stored observations are reused, not new attempts)",
        f"  reuse={counts['reuse']} regrade={counts['regrade']} rerun={counts['rerun']}",
    ]
    for trial in preview.trials:
        source = f" <- {trial.source_trial}" if trial.source_trial else ""
        lines.append(f"  {trial.attempt}: {trial.task}: {trial.action}{source}: {trial.reason}")
    lines.extend(f"  warning: {warning}" for warning in preview.warnings)
    return "\n".join(lines) + "\n"
