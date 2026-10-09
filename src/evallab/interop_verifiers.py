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
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from evallab.interop import load_harbor_task, scripted_agent_plan, task_workdir, verdict_for_reward

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


AGENT_CPU = 1.0
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
    return importlib.util.find_spec("verifiers.v1.tasksets.harbor") is not None


def materialize_files(task: Any, agent: str, plan: Any) -> dict[str, bytes]:
    """Files to stage for a plan: the plan's own plus runner-side materialization.

    The factory is task-independent; the verifiers container only holds the
    task image's content, so the oracle's ``solution/`` (which the plan command
    addresses relative to the workdir) is uploaded from the task package here:
    at ``{workdir}/solution/`` for the relative command and at ``/solution/``
    for solutions that reference it absolutely (the inspect runner stages it
    there too). No plan logic is duplicated: file bytes come from the package,
    the command from the factory.
    """
    files = dict(plan.files)
    if agent == "oracle":
        # The factory guarantees solution/solve.sh for oracle plans; stage the
        # whole solution dir from the task package.
        solution_dir = task.task_dir / "solution"
        workdir = task_workdir(task).rstrip("/")
        for path in sorted(solution_dir.rglob("*")):
            if path.is_file():
                payload = path.read_bytes()
                rel = path.relative_to(solution_dir).as_posix()
                files[f"{workdir}/solution/{rel}"] = payload
                files[f"/solution/{rel}"] = payload
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


def _docker_reachable() -> tuple[bool, str]:
    try:
        probed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker unreachable: {exc}"
    if probed.returncode != 0:
        detail = (probed.stderr or probed.stdout).strip()[-500:]
        return False, f"docker unreachable: {detail}"
    return True, f"docker {probed.stdout.strip()}"


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
    task_dir: Path,
    agent: str,
    attacks: tuple[str, ...],
    *,
    workdir: Path,
    timeout_seconds: int | None,
    isolation: str = "default",
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

    _ensure_harness_alias()
    version = verifiers_version() or "unknown"
    platform_version = f"verifiers {version}"
    task = load_harbor_task(task_dir, require_solution=(agent == "oracle"))
    plan = scripted_agent_plan(task, agent, tuple(attacks))
    plan = replace(plan, files=materialize_files(task, agent, plan))
    container_cwd = task_workdir(task)
    image, how = await asyncio.to_thread(resolve_task_image, task)

    force_shared = isolation == "shared"
    harbor_config = HarborConfig(
        ignore_dockerfile=True,
        ignore_timeouts=False,
        ignore_separate_verifier=force_shared,
    )
    data = parse_task(task.task_dir, 0, harbor_config)
    if image is not None:
        data = data.model_copy(update={"image": image})
    effective_isolation = "separate" if data.verifier is not None else "shared"
    harbor_task = HarborTask(data, harbor_config.task)

    # Task-declared resources are bounded by authorship; the runner's defaults
    # only bound tasks that declare nothing.
    runtime = agent_runtime(image, data.resources)
    env = HarborEnv(
        HarborEnvConfig(
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
) -> dict:
    """Run one (task, agent) cell under verifiers (contract #3).

    Local docker runtime only, no model calls, $0. Reward >= 1.0 is pass;
    infrastructure failures are error (never fail); an unreachable daemon is
    skipped instead of failing. ``isolation`` is ``"default"`` (the task's own
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
    if not verifiers_available():
        return {
            "target": "verifiers",
            "agent": agent,
            "attacks": list(attacks),
            "verdict": "error",
            "reward": None,
            "reason": (
                f"verifiers {VERIFIERS_PIN} is not installed "
                "(opt-in group: uv sync --group xplat-verifiers --extra laminar)"
            ),
            "platform_version": f"verifiers {VERIFIERS_PIN} (absent)",
            "evidence": None,
            "task_id": Path(task_dir).name,
            "isolation": "unknown",
        }
    ok, detail = _docker_reachable()
    if not ok:
        return {
            "target": "verifiers",
            "agent": agent,
            "attacks": list(attacks),
            "verdict": "skipped",
            "reward": None,
            "reason": detail,
            "platform_version": f"verifiers {verifiers_version() or 'unknown'}",
            "evidence": None,
            "task_id": Path(task_dir).name,
            "isolation": "unknown",
        }
    try:
        return asyncio.run(
            _arun_cell(
                Path(task_dir),
                agent,
                attacks,
                workdir=Path(workdir),
                timeout_seconds=timeout_seconds,
                isolation=isolation,
            )
        )
    except Exception as exc:
        return {
            "target": "verifiers",
            "agent": agent,
            "attacks": list(attacks),
            "verdict": "error",
            "reward": None,
            "reason": f"verifiers cell failed: {type(exc).__name__}: {str(exc)[:500]}",
            "platform_version": f"verifiers {verifiers_version() or 'unknown'}",
            "evidence": None,
            "task_id": Path(task_dir).name,
            "isolation": "unknown",
        }
