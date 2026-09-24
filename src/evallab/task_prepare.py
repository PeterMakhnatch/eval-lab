"""Prepare one local Harbor task directory for repeatable execution.

Preparation freezes the task bytes and emits an ordinary validated
``ExperimentSpec`` that the existing submit route consumes unchanged. It stops
there: no queue writes, no admission or authorization, no credential probes,
values, or cloud calls, so preparation works without credentials.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from evallab.execution_contracts import (
    CONTROL_AGENTS,
    DEEPSEEK_MODEL_SELECTOR,
    GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
    GLM_SELFHOSTED_FT_MODEL_SELECTOR,
    MAX_TRIAL_TIMEOUT_SECONDS,
    RLM_AGENT,
    SAFE_JOB_NAME,
    TERMINUS_AGENT,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENCODE_AGENT,
    ZAI_OPENCODE_MODEL_SELECTORS,
    RunRequest,
    validate_request,
)
from evallab.registry import compute_task_digests
from evallab.schemas import EXPLORATION_JOBS_ROOT, ExperimentSpec
from evallab.task_import import import_task_package

#: Retained snapshots live here, repo-relative, content-addressed.
PREPARED_TASKS_REL = "runs/.prepared-tasks"
#: Prepared specs live here by default, named ``<job-name>.json``.
PREPARED_SPECS_REL = "derived/prepared"

#: Request/default numeric caps reused from the retained pilot.
PILOT_MAX_REQUESTS = 200
PILOT_MAX_INPUT_TOKENS = 5_000_000
PILOT_MAX_OUTPUT_TOKENS = 131_072

MINI_SWE_AGENT = "mini-swe-agent"
METERED_AGENTS = frozenset({MINI_SWE_AGENT, ZAI_OPENCODE_AGENT, TERMINUS_AGENT})
MINI_SWE_MODELS = frozenset(
    {
        DEEPSEEK_MODEL_SELECTOR,
        ZAI_OPENAPI_MODEL_SELECTOR,
        GLM_SELFHOSTED_BASE_MODEL_SELECTOR,
        GLM_SELFHOSTED_FT_MODEL_SELECTOR,
    }
)
#: Local Docker plus the TB4 remote backends (mirrors craft.TB4_REMOTE_ENVIRONMENTS).
SUPPORTED_ENVIRONMENTS = frozenset({"docker", "modal", "beam", "daytona"})


@dataclass(frozen=True)
class PreparedTask:
    """Result of freezing one task directory into a runnable spec."""

    spec: ExperimentSpec
    spec_path: Path
    source: Path
    task_path: Path
    resources: dict[str, object]
    task_timeout_seconds: int | None
    warnings: tuple[str, ...]


def _read_task_toml(task_dir: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"task source has no task.toml: {task_dir}") from None
    except OSError as exc:
        raise ValueError(f"cannot read task.toml in {task_dir}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"task.toml does not parse in {task_dir}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"task.toml is not a table in {task_dir}")
    return payload


def _table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _official_timeout_seconds(payload: dict[str, Any], task_dir: Path) -> int | None:
    """Keep the declared agent deadline distinct from build and verification time."""
    timeout = _table(payload, "agent").get("timeout_sec")
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or int(timeout) != timeout
    ):
        raise ValueError(f"agent.timeout_sec must be a positive whole number in {task_dir}")
    return int(timeout)


def _task_identity(payload: dict[str, Any], task_dir: Path) -> tuple[str, str | None]:
    task_table = _table(payload, "task")
    raw_name = task_table.get("name")
    display_name = str(raw_name).strip() if raw_name is not None else task_dir.name
    if not display_name:
        raise ValueError(f"task name is empty in {task_dir / 'task.toml'}")
    raw_version = task_table.get("version", payload.get("version"))
    version = str(raw_version).strip() if raw_version is not None else None
    return display_name, version


def _task_resources(
    payload: dict[str, Any],
    *,
    display_name: str,
    version: str | None,
    official_timeout_seconds: int | None,
) -> dict[str, object]:
    resources: dict[str, object] = {
        "task_name": display_name,
        "task_version": version,
        "official_timeout_seconds": official_timeout_seconds,
    }
    environment = _table(payload, "environment")
    for key in (
        "cpus",
        "memory_mb",
        "storage_mb",
        "gpus",
        "gpu_types",
        "os",
        "network_mode",
        "build_timeout_sec",
        "docker_image",
    ):
        value = environment.get(key)
        if isinstance(value, (str, int, float, bool, list)):
            resources[key] = value
    for table_name, label in (("agent", "agent_timeout_sec"), ("verifier", "verifier_timeout_sec")):
        value = _table(payload, table_name).get("timeout_sec")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            resources[label] = value
    return resources


def _resolve_ceilings(
    *,
    agent: str,
    max_requests: int,
    max_input_tokens: int,
    max_output_tokens: int,
    max_total_tokens: int | None,
    cost_limit_usd: float | None,
    warnings: list[str],
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    """Resolve metered ceilings; non-metered harnesses get none, never fakes."""
    if agent in METERED_AGENTS:
        if cost_limit_usd is None:
            raise ValueError(
                f"metered agent {agent!r} requires explicit cost_limit_usd: "
                "pass the per-trial spend ceiling in USD"
            )
        if cost_limit_usd <= 0:
            raise ValueError("cost_limit_usd must be positive")
        for label, value in (
            ("max_requests", max_requests),
            ("max_input_tokens", max_input_tokens),
            ("max_output_tokens", max_output_tokens),
        ):
            if value is None or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if max_total_tokens is None:
            derived = max_input_tokens + max_output_tokens
            warnings.append(f"derived max_total_tokens={derived} as input plus output ceilings")
            return max_requests, max_input_tokens, max_output_tokens, derived, cost_limit_usd
        if max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive")
        if max_total_tokens > max_input_tokens + max_output_tokens:
            raise ValueError("total-token ceiling exceeds input plus output ceilings")
        return (
            max_requests,
            max_input_tokens,
            max_output_tokens,
            max_total_tokens,
            cost_limit_usd,
        )
    # The pilot defaults are request ceilings, not task facts: silently forwarding
    # them to a harness that cannot enforce them would fake support, so an
    # explicitly passed ceiling is refused while bare defaults are dropped.
    if agent != RLM_AGENT and cost_limit_usd is not None:
        raise ValueError(
            f"agent {agent!r} cannot enforce provider cost ceilings; omit cost_limit_usd"
        )
    if max_total_tokens is not None:
        raise ValueError(
            f"agent {agent!r} cannot enforce provider token ceilings; omit max_total_tokens"
        )
    if (max_requests, max_input_tokens, max_output_tokens) != (
        PILOT_MAX_REQUESTS,
        PILOT_MAX_INPUT_TOKENS,
        PILOT_MAX_OUTPUT_TOKENS,
    ):
        raise ValueError(
            f"agent {agent!r} cannot enforce provider request/cost/token ceilings; "
            "omit max_requests, max_input_tokens, and max_output_tokens"
        )
    return None, None, None, None, cost_limit_usd if agent == RLM_AGENT else None


def _output_path(repo: Path, path: Path) -> Path:
    target = path if path.is_absolute() else repo / path
    if not target.resolve().is_relative_to(repo) or target.resolve() == repo:
        raise ValueError(f"output escapes repository or names its root: {path}")
    for part in (target, *target.parents):
        if part == repo:
            break
        if part.is_symlink():
            raise ValueError(f"output path contains a symlink: {part}")
    return target.resolve()


def prepare_task(
    repo_root: Path,
    source: Path,
    *,
    name: str,
    agent: str,
    model: str | None,
    environment: str,
    timeout_seconds: int | None = None,
    cost_limit_usd: float | None = None,
    est_cost_usd: float | None = None,
    max_requests: int = PILOT_MAX_REQUESTS,
    max_input_tokens: int = PILOT_MAX_INPUT_TOKENS,
    max_output_tokens: int = PILOT_MAX_OUTPUT_TOKENS,
    max_total_tokens: int | None = None,
    output: Path | None = None,
    submitted_by: str = "operator",
) -> PreparedTask:
    """Freeze one local task directory into a validated, submittable spec.

    The snapshot is a byte-independent copy under ``runs/.prepared-tasks`` and
    the spec pins its exact package and verifier digests. Repeating the same
    request is idempotent; a different request that collides with an existing
    spec file refuses instead of overwriting.
    """
    warnings: list[str] = []
    repo = Path(repo_root).resolve()
    if not repo.is_dir():
        raise ValueError(f"repository root is not a directory: {repo_root}")
    if not SAFE_JOB_NAME.fullmatch(name):
        raise ValueError("job names must be 3-80 lowercase letters, numbers, or hyphens")
    if environment not in SUPPORTED_ENVIRONMENTS:
        raise ValueError(
            f"unsupported environment {environment!r}: "
            f"expected one of {sorted(SUPPORTED_ENVIRONMENTS)}"
        )
    if agent not in CONTROL_AGENTS and not model:
        raise ValueError("paid harness preparation requires an explicit model selector")
    if agent in CONTROL_AGENTS and model is not None:
        raise ValueError(f"the {agent} control does not accept a model")
    if agent == MINI_SWE_AGENT and model is not None and model not in MINI_SWE_MODELS:
        raise ValueError(f"mini-swe-agent requires one of {sorted(MINI_SWE_MODELS)}, got {model!r}")
    if (
        agent in (ZAI_OPENCODE_AGENT, RLM_AGENT)
        and model is not None
        and model not in ZAI_OPENCODE_MODEL_SELECTORS
    ):
        raise ValueError(
            f"{agent} requires one of the exact models "
            f"{sorted(ZAI_OPENCODE_MODEL_SELECTORS)}, got {model!r}"
        )
    if est_cost_usd is not None and est_cost_usd < 0:
        raise ValueError("est_cost_usd cannot be negative")
    if environment != "docker" and est_cost_usd is None:
        raise ValueError(
            f"remote environment {environment!r} requires explicit est_cost_usd: "
            "pass the total cost estimate in USD, trial plus infrastructure"
        )

    resolved_source = Path(source).resolve()
    if not resolved_source.is_dir():
        raise FileNotFoundError(f"task source directory does not exist: {source}")
    payload = _read_task_toml(resolved_source)
    display_name, version = _task_identity(payload, resolved_source)
    official_timeout = _official_timeout_seconds(payload, resolved_source)

    if timeout_seconds is None:
        resolved_timeout = official_timeout
        if resolved_timeout is None:
            raise ValueError(
                "task has no declared agent.timeout_sec; pass --timeout-seconds explicitly"
            )
        if resolved_timeout > MAX_TRIAL_TIMEOUT_SECONDS:
            raise ValueError(
                f"task timeout is {resolved_timeout}s, beyond the Lab limit of "
                f"{MAX_TRIAL_TIMEOUT_SECONDS}s; use an explicit shorter diagnostic limit"
            )
    else:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= MAX_TRIAL_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be between 1 and {MAX_TRIAL_TIMEOUT_SECONDS} seconds"
            )
        resolved_timeout = int(timeout_seconds)
        if official_timeout is not None and resolved_timeout != official_timeout:
            warnings.append(
                f"diagnostic timeout {resolved_timeout}s differs from the declared "
                f"agent timeout {official_timeout}s for {display_name!r}"
            )

    ceilings = _resolve_ceilings(
        agent=agent,
        max_requests=max_requests,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_total_tokens=max_total_tokens,
        cost_limit_usd=cost_limit_usd,
        warnings=warnings,
    )
    ceiling_requests, ceiling_input, ceiling_output, ceiling_total, ceiling_cost = ceilings
    estimated_cost = est_cost_usd if est_cost_usd is not None else (ceiling_cost or 0.0)
    if ceiling_cost is not None and estimated_cost < ceiling_cost:
        raise ValueError("estimated cost must cover the model cost ceiling plus any infrastructure")

    candidate = Path(output) if output is not None else Path(PREPARED_SPECS_REL) / f"{name}.json"
    spec_path = _output_path(repo, candidate)
    snapshot_root = _output_path(repo, Path(PREPARED_TASKS_REL))

    # This validates the command contract; only the queue can authorize spending.
    request = RunRequest(
        task=resolved_source,
        agent=agent,
        name=name,
        jobs_dir=repo / EXPLORATION_JOBS_ROOT,
        environment=environment,
        model=model,
        timeout_seconds=resolved_timeout,
        allow_billable=True,
        max_requests=ceiling_requests,
        max_input_tokens=ceiling_input,
        max_output_tokens=ceiling_output,
        max_total_tokens=ceiling_total,
        cost_limit_usd=ceiling_cost,
    )
    validate_request(request)

    snapshot, _source_digest = import_task_package(resolved_source, snapshot_root)
    if _read_task_toml(snapshot) != payload:
        raise ValueError(
            "task configuration changed during preparation; retry with a stable source"
        )
    task_rel = snapshot.relative_to(repo).as_posix()
    digests = compute_task_digests(snapshot)
    resources = _task_resources(
        payload,
        display_name=display_name,
        version=version,
        official_timeout_seconds=official_timeout,
    )

    spec = ExperimentSpec(
        name=name,
        hypothesis=(
            f"Prepared {display_name!r} version={version or 'unspecified'} for {agent}"
            f"/{model or 'control'} on {environment} from frozen snapshot {task_rel}"
        ),
        purpose="baseline",
        task=task_rel,
        task_path=task_rel,
        agent=agent,
        model=model,
        environment=environment,
        jobs_dir=EXPLORATION_JOBS_ROOT,
        timeout_seconds=resolved_timeout,
        submitted_by=submitted_by,
        est_cost_usd=estimated_cost,
        task_version=version,
        verifier_digest=digests.verifier,
        task_package_digest=digests.package,
        max_requests=ceiling_requests,
        max_input_tokens=ceiling_input,
        max_output_tokens=ceiling_output,
        max_total_tokens=ceiling_total,
        cost_limit_usd=ceiling_cost,
    )

    validate_request(replace(request, task=snapshot))

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with spec_path.open("x", encoding="utf-8") as stream:
            stream.write(spec.model_dump_json(indent=2) + "\n")
    except FileExistsError:
        try:
            existing = ExperimentSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FileExistsError(
                f"existing spec is unreadable; refusing overwrite: {spec_path}"
            ) from exc
        if existing != spec:
            raise FileExistsError(
                f"existing spec differs from this request; refusing overwrite: {spec_path}"
            ) from None
    return PreparedTask(
        spec=spec,
        spec_path=spec_path,
        source=resolved_source,
        task_path=snapshot,
        resources=resources,
        task_timeout_seconds=official_timeout,
        warnings=tuple(warnings),
    )
