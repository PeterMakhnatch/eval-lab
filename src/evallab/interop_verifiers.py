"""xplat verifiers runner: Harbor tasks under Prime ``verifiers``, model-free ($0).

Re-assessment of the README's ``needs-paid`` row: verifiers v1 episodes do run
as ``Env.run(task, agents)``, but the rollout's model traffic goes through
whatever ``Harness.launch`` does — and a harness may run its loop without any
model call at all (verifiers ships exactly this shape in its
``agent-user-v1`` test fixture: a scripted harness driving
``runtime.run_program``). ``evallab.vf_scripted_harness.ScriptedHarness`` is
that shape pointed at a fixed plan (stage files, run one command, or nop), so
oracle/nop/cheat cells need zero inference: no model API, no paid sandbox,
local ``docker`` runtime only.

Grading is the task's own ``tests/test.sh`` through verifiers' ``HarborEnv``
(this taskset's default env): shared-verifier tasks grade in the agent's box
with stale reward files cleared before ``test.sh`` runs; separate-verifier
tasks grade in a fresh box with only declared artifacts restored. The cell
records which isolation mode graded it.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import importlib.util
import json
import math
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from evallab.interop import (
    HarborTask as LocalHarborTask,
)
from evallab.interop import (
    admit_local_task,
    load_harbor_task,
    scripted_agent_plan,
    task_workdir,
    verdict_for_reward,
)

VERIFIERS_PIN = "0.3.1"
VERIFIERS_HARNESS_ID = "evallab_vf_scripted_harness"
SCRIPTED_MODEL = "scripted/none"


def _ensure_harness_alias() -> None:
    """Register the scripted harness under its single-level plugin id.

    verifiers imports a harness id as a top-level module (a dotted id makes
    its ``find_spec`` probe raise), so the harness module is aliased in
    ``sys.modules`` before any env construction. Lazy: importing the harness
    module requires the opt-in verifiers group.
    """
    import importlib
    import sys

    if VERIFIERS_HARNESS_ID not in sys.modules:
        module = importlib.import_module("evallab.vf_scripted_harness")
        if getattr(module, "HARNESS_PLUGIN_ID", None) != VERIFIERS_HARNESS_ID:
            raise RuntimeError(
                "harness plugin id drift: "
                f"{getattr(module, 'HARNESS_PLUGIN_ID', None)!r} != {VERIFIERS_HARNESS_ID!r}"
            )
        sys.modules[VERIFIERS_HARNESS_ID] = module


AGENT_CPU = 2.0
AGENT_MEMORY_GB = 2.0

SETUP_TIMEOUT_SECONDS = 600.0
FINALIZE_TIMEOUT_SECONDS = 180.0

_CELL_AGENTS = ("oracle", "nop", "cheat")


def verifiers_version() -> str | None:
    """Installed verifiers version, or None when the group is absent."""
    try:
        return importlib.metadata.version("verifiers")
    except importlib.metadata.PackageNotFoundError:
        return None


def verifiers_available() -> bool:
    """Whether the verifiers runtime group is importable ($0 probe)."""
    try:
        return importlib.util.find_spec("verifiers.v1.tasksets.harbor") is not None
    except (ImportError, ValueError):
        # A missing parent package makes a nested find_spec raise, not return None.
        return False


def materialize_files(task: Any, agent: str, plan: Any) -> dict[str, bytes]:
    """Files to stage for a plan: the plan's own plus runner-side materialization.

    The factory is task-independent; the verifiers container only holds the
    task image's content, so the oracle's ``solution/`` is uploaded from the
    task package to Harbor's ``/solution`` (the plan runs
    ``bash /solution/solve.sh``). Oracle cells only. No plan logic is
    duplicated: file bytes come from the package, the command from the factory.
    """
    files = dict(plan.files)
    if agent == "oracle":
        solution_dir = task.task_dir / "solution"
        for path in sorted(solution_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(solution_dir).as_posix()
                files[f"/solution/{rel}"] = path.read_bytes()
    return files


def harness_config(plan: Any, workdir: str) -> dict[str, Any]:
    """Agent harness dict for a plan (narrowed to ScriptedHarnessConfig by verifiers)."""
    return {
        "id": VERIFIERS_HARNESS_ID,
        "command": plan.command,
        "workdir": workdir,
        "files_b64": {
            path: base64.b64encode(payload).decode("ascii") for path, payload in plan.files.items()
        },
    }


def _slug(task_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:48] or "task"
    return slug


def resolve_task_image(task: Any) -> tuple[str | None, str]:
    """Image for a cell: declared docker_image, else a local build of environment/.

    verifiers never builds Dockerfiles, so Dockerfile-only tasks are built here
    with plain local ``docker build`` ($0) and referenced by tag — the pattern
    verifiers' own harbor docs prescribe (build it yourself, name the
    reference). Returns (image, how) where how is declared/built/default.
    """
    env = task.config.get("environment")
    declared = env.get("docker_image") if isinstance(env, dict) else None
    if isinstance(declared, str) and declared:
        return declared, "declared"
    dockerfile = task.task_dir / "environment" / "Dockerfile"
    if dockerfile.is_file():
        tag = f"evallab-vf/{_slug(task.task_id)}:latest"
        try:
            built = subprocess.run(
                ["docker", "build", "-t", tag, str(task.task_dir / "environment")],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"docker build failed for {task.task_id}: {exc}") from exc
        if built.returncode != 0:
            detail = (built.stderr or built.stdout).strip()[-2000:]
            raise RuntimeError(f"docker build failed for {task.task_id}: {detail}")
        return tag, "built"
    return None, "default"


def agent_runtime(image: str | None, resources: Any) -> dict[str, Any]:
    """Docker runtime dict: task-declared cpu/memory win, else bounded defaults."""
    runtime: dict[str, Any] = {
        "type": "docker",
        "cpu": resources.cpu or AGENT_CPU,
        "memory": resources.memory or AGENT_MEMORY_GB,
    }
    if image is not None:
        runtime["image"] = image
    return runtime


def _network_policy(
    phase: dict[str, Any], environment: dict[str, Any]
) -> tuple[str, tuple[str, ...]]:
    """The Harbor 0.24 phase override, baseline, and legacy-default precedence."""
    mode = phase.get("network_mode")
    source = phase
    if mode is None:
        source = environment
        mode = environment.get("network_mode")
        if mode is None:
            mode = "no-network" if environment.get("allow_internet") is False else "public"
    hosts = source.get("allowed_hosts") or []
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ValueError("network allowed_hosts must be a list of hostnames")
    return mode, tuple(sorted({host.strip().lower().rstrip(".") for host in hosts}))


def _validate_task_contract(task: LocalHarborTask, isolation: str) -> str:
    """Refuse known lossy mappings before parsing or building any task image.

    verifiers 0.3.1's harbor.taskset.parse_verifier_extras rejects user/service
    overrides. parse_task also omits Compose, agent users, multi-step execution,
    and shared-verifier phase networks; DockerConfig.disk is advisory only.
    The upstream parser remains authoritative for the rest of the schema.
    """
    environment = task.config.get("environment", {})
    agent = task.config.get("agent", {})
    verifier = task.config.get("verifier", {})
    if not all(isinstance(section, dict) for section in (environment, agent, verifier)):
        raise ValueError("environment, agent, and verifier must be task.toml tables")
    declared = verifier.get("environment")
    if declared is not None and not isinstance(declared, dict):
        raise ValueError("[verifier.environment] must be a task.toml table")
    mode = verifier.get("environment_mode") or ("separate" if declared is not None else "shared")
    if mode not in ("shared", "separate"):
        raise ValueError(f"unsupported [verifier].environment_mode: {mode!r}")
    if mode == "shared" and declared is not None:
        raise ValueError("[verifier.environment] is incompatible with environment_mode='shared'")
    effective = "shared" if isolation == "shared" else mode
    unsupported: list[str] = []
    if task.config.get("steps"):
        unsupported.append("multi-step tasks")
    for name, section in (("agent", agent), ("verifier", verifier)):
        if section.get("user") is not None:
            unsupported.append(f"[{name}].user")
    for hook in verifier.get("collect", []):
        if isinstance(hook, dict):
            if hook.get("user") is not None:
                unsupported.append("[[verifier.collect]].user")
            if hook.get("service", "main") != "main":
                unsupported.append("[[verifier.collect]] additional service")
    for artifact in task.config.get("artifacts", []):
        if isinstance(artifact, dict) and artifact.get("service") not in (None, "main"):
            unsupported.append("artifact additional service")
    environments = [("environment", environment)]
    if effective == "separate" and declared is not None:
        environments.append(("verifier.environment", declared))
        if not declared.get("docker_image"):
            unsupported.append(
                "[verifier.environment] without docker_image "
                "(tests/Dockerfile is not built; substituting the agent image is refused)"
            )
    for name, config in environments:
        if str(config.get("os", "linux")).lower() != "linux":
            unsupported.append(f"[{name}].os (Linux containers only)")
        for field in ("mcp_servers", "skills_dir", "gpu_types", "tpu"):
            if config.get(field):
                unsupported.append(f"[{name}].{field}")
        if config.get("storage_mb") is not None or config.get("storage") is not None:
            unsupported.append(f"[{name}].storage_mb (Docker disk limits are not enforced)")
        for field in ("cpus", "memory_mb"):
            value = config.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"[{name}].{field} must be finite and positive")
    for directory in (["environment", "tests"] if effective == "separate" else ["environment"]):
        if any(
            (task.task_dir / directory / name).is_file()
            for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
        ):
            unsupported.append(f"{directory}/Compose (single-image Docker runtime only)")
    if (
        effective == "separate"
        and not (declared or {}).get("docker_image")
        and (task.task_dir / "tests" / "Dockerfile").is_file()
    ):
        unsupported.append("tests/Dockerfile (separate verifier image is not built)")
    agent_network = _network_policy(agent, environment)
    verifier_network = _network_policy(verifier, declared if declared is not None else environment)
    if effective == "shared" and agent_network != verifier_network:
        unsupported.append(
            "shared verifier phase network differs from the agent policy "
            "(verifiers cannot switch the shared box's policy between phases)"
        )
    for name, (network_mode, hosts) in (
        ("agent", agent_network),
        ("verifier", verifier_network),
    ):
        # network_rule_matches uses URL host globs, not Harbor IP/CIDR rules.
        if network_mode == "allowlist" and any("/" in host or ":" in host for host in hosts):
            unsupported.append(f"[{name}].allowed_hosts IP CIDR/IPv6 rules")
    if unsupported:
        raise ValueError(
            f"{task.task_id}: unsupported verifiers {VERIFIERS_PIN} task contract: "
            + "; ".join(unsupported)
        )
    return effective


def _admission_task(task: LocalHarborTask, data: Any) -> LocalHarborTask:
    """Conservatively reserve both boxes at the larger resolved CPU/RAM limit."""
    resources = [data.resources]
    if data.verifier is not None and not data.verifier.fresh_copy:
        resources.append(data.verifier.resources)
    environment = dict(task.config.get("environment", {}))
    environment.update(
        cpus=max(item.cpu if item.cpu is not None else AGENT_CPU for item in resources),
        memory_mb=max(
            round((item.memory if item.memory is not None else AGENT_MEMORY_GB) * 1024)
            for item in resources
        ),
    )
    if any(item.gpu for item in resources):
        environment["gpus"] = 1  # Admission refuses all GPU requests, regardless of count.
    return replace(task, config={**task.config, "environment": environment})


def cell_from_trace(
    *,
    task_id: str,
    agent: str,
    attacks: tuple[str, ...],
    reward: float | None,
    scored: bool,
    isolation: str,
    platform_version: str,
    evidence: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Map a graded (or failed) episode onto the contract #3 cell dict."""
    if error is not None or not scored or reward is None:
        return {
            "target": "verifiers",
            "agent": agent,
            "attacks": list(attacks),
            "verdict": "error",
            "reward": reward,
            "reason": error or "verifiers episode recorded no reward (grading never ran)",
            "platform_version": platform_version,
            "evidence": evidence,
            "task_id": task_id,
            "isolation": isolation,
        }
    return {
        "target": "verifiers",
        "agent": agent,
        "attacks": list(attacks),
        "verdict": verdict_for_reward(reward),
        "reward": reward,
        "reason": None,
        "platform_version": platform_version,
        "evidence": evidence,
        "task_id": task_id,
        "isolation": isolation,
    }


async def _arun_cell(
    task: LocalHarborTask,
    agent: str,
    attacks: tuple[str, ...],
    *,
    workdir: Path,
    timeout_seconds: int | None,
    isolation: str = "default",
    ladder_version: str | None = None,
) -> dict[str, Any]:
    from verifiers.v1.clients import ModelContext  # ty: ignore[unresolved-import]
    from verifiers.v1.configs.client import EvalClientConfig  # ty: ignore[unresolved-import]
    from verifiers.v1.tasksets.harbor import (  # ty: ignore[unresolved-import]
        HarborConfig,
        HarborEnv,
        HarborEnvConfig,
        HarborTask,
    )
    from verifiers.v1.tasksets.harbor.taskset import parse_task  # ty: ignore[unresolved-import]

    version = verifiers_version() or "unknown"
    platform_version = f"verifiers {version}"
    plan = scripted_agent_plan(task, agent, tuple(attacks), ladder_version=ladder_version)
    plan = replace(plan, files=materialize_files(task, agent, plan))
    container_cwd = task_workdir(task)

    force_shared = isolation == "shared"
    harbor_config = HarborConfig(
        ignore_dockerfile=True,
        ignore_timeouts=False,
        ignore_separate_verifier=force_shared,
    )
    data = parse_task(task.task_dir, 0, harbor_config)
    _ensure_harness_alias()
    effective_isolation = "separate" if data.verifier is not None else "shared"
    preflight = {
        "task_id": task.task_id,
        "platform_version": platform_version,
        "isolation": effective_isolation,
        "isolation_requested": isolation,
        "isolation_override": (
            "separate verifier environment discarded; grading in the agent box"
            if force_shared
            and (
                task.environment_mode == "separate"
                or task.config.get("verifier", {}).get("environment") is not None
            )
            else None
        ),
        "agent_resources": data.resources.model_dump(mode="json"),
        "verifier_resources": (
            (
                data.resources if data.verifier.fresh_copy else data.verifier.resources
            ).model_dump(mode="json")
            if data.verifier is not None
            else None
        ),
        "admission_strategy": "max(agent, verifier) CPU/RAM per sandbox",
    }
    (workdir / "preflight.json").write_text(json.dumps(preflight, indent=2) + "\n")
    await asyncio.to_thread(
        admit_local_task,
        _admission_task(task, data),
        workdir,
        sandboxes=2 if data.verifier is not None else 1,
    )
    image, how = await asyncio.to_thread(resolve_task_image, task)
    if image is not None:
        data = data.model_copy(update={"image": image})
    harbor_task = HarborTask(data, harbor_config.task)

    # Task-declared resources are bounded by authorship; the runner's defaults
    # only bound tasks that declare nothing.
    runtime = agent_runtime(image, data.resources)
    verifier_runtime = None
    if data.verifier is not None:
        verifier_runtime = agent_runtime(
            data.verifier.image or image,
            data.resources if data.verifier.fresh_copy else data.verifier.resources,
        )
    env = HarborEnv(
        HarborEnvConfig(
            verifier_runtime=verifier_runtime,
            taskset={
                "id": "harbor",
                "dataset": "harbor/hello-world",
                "ignore_dockerfile": True,
                "ignore_timeouts": False,
                "ignore_separate_verifier": force_shared,
            },
            agent={
                "harness": harness_config(plan, container_cwd),
                "model": SCRIPTED_MODEL,
                "runtime": runtime,
                "timeout": {
                    "setup": SETUP_TIMEOUT_SECONDS,
                    "finalize": FINALIZE_TIMEOUT_SECONDS,
                },
            },
        )
    )
    ctx = ModelContext(model=SCRIPTED_MODEL, client=EvalClientConfig())
    episode_coro = env.run_episode(harbor_task, ctx)
    if timeout_seconds is not None:
        episode = await asyncio.wait_for(episode_coro, timeout_seconds)
    else:
        episode = await episode_coro

    traces = list(episode.traces)
    trace = traces[0] if traces else None
    rewards = dict(trace.rewards) if trace is not None else {}
    scored_names = [name for name, entry in rewards.items() if entry is not None]
    reward = float(trace.reward) if trace is not None and scored_names else None
    error: str | None = None
    if trace is None or not scored_names:
        parts = [
            f"{err.type}: {(err.message or '')[:300]}"
            for err in list(episode.errors) + (list(trace.errors) if trace else [])
        ]
        error = "; ".join(parts) or "episode recorded no reward"
    workdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "task_id": task.task_id,
        "agent": agent,
        "attacks": list(attacks),
        "image": image,
        "image_source": how,
        "isolation": effective_isolation,
        "isolation_requested": isolation,
        "command": plan.command,
        "workdir": container_cwd,
        "staged_files": sorted(plan.files),
        "platform_version": platform_version,
        "tracked_rewards": scored_names,
    }
    (workdir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if trace is not None:
        (workdir / "trace.json").write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    return cell_from_trace(
        task_id=task.task_id,
        agent=agent,
        attacks=attacks,
        reward=reward,
        scored=bool(scored_names),
        isolation=effective_isolation,
        platform_version=platform_version,
        evidence=str(workdir),
        error=error,
    )


def run_cell(
    task_dir: Path,
    agent: str,
    attacks: tuple[str, ...],
    *,
    workdir: Path,
    timeout_seconds: int | None,
    isolation: str = "default",
    ladder_version: str | None = None,
) -> dict:
    """Run one (task, agent) cell under verifiers (contract #3).

    Local docker runtime only, no model calls, $0. Reward >= 1.0 is pass;
    infrastructure failures, missing dependencies, unsupported contracts, and
    admission refusal are errors with persisted evidence, never skips.
    ``isolation`` is ``"default"`` (the task's own
    shared/separate declaration) or ``"shared"`` (force grading in the agent's
    box via ignore_separate_verifier); forcing a separate box for a shared
    task is not supported by HarborEnv, so it is refused. The cell records
    the effective mode.
    """
    if agent not in _CELL_AGENTS:
        raise ValueError(f"refusing non-scripted agent {agent!r} (oracle/nop/cheat only, $0)")
    if isolation not in ("default", "shared"):
        raise ValueError(f"unknown isolation {isolation!r} (default/shared only, $0)")
    attacks = tuple(attacks)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    task_id = Path(task_dir).name
    effective_isolation = "unknown"
    platform_version = f"verifiers {verifiers_version() or 'unknown'}"
    try:
        if not verifiers_available():
            platform_version = f"verifiers {VERIFIERS_PIN} (absent)"
            raise ModuleNotFoundError(
                f"verifiers {VERIFIERS_PIN} is not installed or its runtime is unavailable "
                "(opt-in group: uv sync --group xplat-verifiers --extra laminar)"
            )
        task = load_harbor_task(task_dir, require_solution=(agent == "oracle"))
        task_id = task.task_id
        effective_isolation = _validate_task_contract(task, isolation)
        return asyncio.run(
            _arun_cell(
                task,
                agent,
                attacks,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
                isolation=isolation,
                ladder_version=ladder_version,
            )
        )
    except Exception as exc:
        cell = cell_from_trace(
            task_id=task_id,
            agent=agent,
            attacks=attacks,
            reward=None,
            scored=False,
            isolation=effective_isolation,
            platform_version=platform_version,
            evidence=str(workdir),
            error=f"verifiers cell failed: {type(exc).__name__}: {exc}",
        )
        (workdir / "error.json").write_text(
            json.dumps({**cell, "isolation_requested": isolation}, indent=2) + "\n"
        )
        return cell
