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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.interop import load_harbor_task, verdict_for_reward

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

ORACLE_STAGE_DIR = "/tmp/oracle-solve"
CHEAT_STAGE_PATH = "/tmp/cheat_ladder.py"
CHEAT_EVIDENCE_DIR = "/tmp/cheat-evidence"

AGENT_CPU = 1.0
AGENT_MEMORY_GB = 2.0

SETUP_TIMEOUT_SECONDS = 300.0
FINALIZE_TIMEOUT_SECONDS = 180.0

_CELL_AGENTS = ("oracle", "nop", "cheat")


@dataclass(frozen=True)
class Plan:
    """Fallback plan shape: mirrors interop.ScriptedPlan (files + command)."""

    files: dict[str, bytes] = field(default_factory=dict)
    command: str | None = None


def verifiers_version() -> str | None:
    """Installed verifiers version, or None when the group is absent."""
    try:
        return importlib.metadata.version("verifiers")
    except importlib.metadata.PackageNotFoundError:
        return None


def verifiers_available() -> bool:
    """Whether the verifiers runtime group is importable ($0 probe)."""
    return importlib.util.find_spec("verifiers.v1.tasksets.harbor") is not None


def container_workdir(task_dir: str | Path) -> str:
    """Effective container workdir: [environment] workdir, else Harbor's /app."""
    try:
        config = tomllib.loads((Path(task_dir) / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "/app"
    env = config.get("environment")
    if isinstance(env, dict):
        workdir = env.get("workdir")
        if isinstance(workdir, str) and workdir:
            return workdir
    return "/app"


def load_plan(task: Any, agent: str, attacks: tuple[str, ...]) -> Any:
    """Plan for one cell: interop.scripted_agent_plan when it lands, else fallback.

    The fallback implements the same documented semantics (oracle stages
    solution/ and runs solve.sh; nop stages nothing and runs nothing; cheat
    stages cheat_ladder.py and runs it with the attack subset) without copying
    any ladder code — it only reads cheat_ladder.py's bytes.
    """
    from evallab import interop as _interop

    factory = getattr(_interop, "scripted_agent_plan", None)
    if callable(factory):
        return factory(task, agent, tuple(attacks))
    return _fallback_plan(task, agent, tuple(attacks))


def _fallback_plan(task: Any, agent: str, attacks: tuple[str, ...]) -> Plan:
    if agent == "nop":
        return Plan(files={}, command=None)
    if agent == "oracle":
        solution_dir = task.task_dir / "solution"
        solve = solution_dir / "solve.sh"
        if not solve.is_file():
            raise ValueError(f"{task.task_dir}: oracle needs solution/solve.sh")
        files = {
            f"{ORACLE_STAGE_DIR}/{path.relative_to(solution_dir).as_posix()}": path.read_bytes()
            for path in sorted(solution_dir.rglob("*"))
            if path.is_file()
        }
        return Plan(files=files, command=f"bash {ORACLE_STAGE_DIR}/solve.sh")
    if agent == "cheat":
        from evallab.cheat_ladder import ATTACKS

        unknown = [name for name in attacks if name not in ATTACKS]
        if unknown:
            raise ValueError(f"unknown cheat attacks: {unknown} (ladder: {list(ATTACKS)})")
        from evallab import cheat_ladder as _ladder

        ladder_path = Path(_ladder.__file__ or "")
        files = {CHEAT_STAGE_PATH: ladder_path.read_bytes()}
        command = (
            f"python3 {CHEAT_STAGE_PATH} --cwd {container_workdir(task.task_dir)}"
            f" --evidence-dir {CHEAT_EVIDENCE_DIR}"
        )
        if attacks:
            command += f" --attacks {','.join(attacks)}"
        return Plan(files=files, command=command)
    raise ValueError(f"unknown agent {agent!r} (oracle/nop/cheat only, $0)")


def harness_config(plan: Any) -> dict[str, Any]:
    """Agent harness dict for a plan (narrowed to ScriptedHarnessConfig by verifiers)."""
    return {
        "id": VERIFIERS_HARNESS_ID,
        "command": plan.command,
        "files_b64": {
            path: base64.b64encode(payload).decode("ascii")
            for path, payload in plan.files.items()
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
    task = load_harbor_task(task_dir)
    plan = load_plan(task, agent, attacks)
    image, how = await asyncio.to_thread(resolve_task_image, task)

    harbor_config = HarborConfig(ignore_dockerfile=True, ignore_timeouts=False)
    data = parse_task(task.task_dir, 0, harbor_config)
    if image is not None:
        data = data.model_copy(update={"image": image})
    isolation = "separate" if data.verifier is not None else "shared"
    harbor_task = HarborTask(data, harbor_config.task)

    runtime: dict[str, Any] = {
        "type": "docker",
        "cpu": AGENT_CPU,
        "memory": AGENT_MEMORY_GB,
    }
    if image is not None:
        runtime["image"] = image
    env = HarborEnv(
        HarborEnvConfig(
            taskset={
                "id": "harbor",
                "dataset": "harbor/hello-world",
                "ignore_dockerfile": True,
                "ignore_timeouts": False,
            },
            agent={
                "harness": harness_config(plan),
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
        "isolation": isolation,
        "command": plan.command,
        "staged_files": sorted(plan.files),
        "platform_version": platform_version,
        "tracked_rewards": scored_names,
    }
    (workdir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if trace is not None:
        (workdir / "trace.json").write_text(
            trace.model_dump_json(indent=2), encoding="utf-8"
        )
    return cell_from_trace(
        task_id=task.task_id,
        agent=agent,
        attacks=attacks,
        reward=reward,
        scored=bool(scored_names),
        isolation=isolation,
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
) -> dict:
    """Run one (task, agent) cell under verifiers (contract #3).

    Local docker runtime only, no model calls, $0. Reward >= 1.0 is pass;
    infrastructure failures are error (never fail); an unreachable daemon is
    skipped instead of failing.
    """
    if agent not in _CELL_AGENTS:
        raise ValueError(f"refusing non-scripted agent {agent!r} (oracle/nop/cheat only, $0)")
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
            _arun_cell(Path(task_dir), agent, attacks, workdir=Path(workdir),
                       timeout_seconds=timeout_seconds)
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
