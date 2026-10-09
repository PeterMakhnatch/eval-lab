"""HAR-204: Harbor <-> Inspect AI <-> Karotte <-> verifiers interop (local, $0 only).

Model-free, spend-free bridges:

- ``export-harbor`` converts a Harbor task directory into an Inspect AI task
  (``--to inspect``) or the runnable karotte env ``interop_karotte.run_cell``
  assembles (``--to karotte``). Both paths never import their frameworks;
  the karotte path flags every lossy or impossible mapping in a generated
  ``MAPPING.md`` instead of silently dropping it.
- ``matrix`` runs scripted oracle/nop/cheat agents against each target in the
  ``MATRIX_TARGETS`` registry (harbor, inspect, karotte via ``interop_karotte``,
  verifiers via ``interop_verifiers``) and prints the grading table plus a
  JSON envelope.

The scripted agents are platform-neutral plans (``scripted_agent_plan``):
oracle runs ``/solution/solve.sh`` (each runner provides the task's
``solution/`` at Harbor's ``/solution``, oracle cells only); nop does nothing; cheat
stages the stdlib-only ``evallab.cheat_ladder`` and runs it with the attack
subset (empty = full ladder). Every target runner exposes
``run_cell(task_dir, agent, attacks, *, workdir, timeout_seconds)`` returning
a pass/fail/skipped/error verdict dict (reward >= 1.0 passes).

Live-Docker paths attempt execution exactly like ``evallab run`` /
``evallab cheat run`` (direct execution, no campaign-queue admission gate):
infrastructure failures are ``error`` cells, never ``fail``.

This module is stdlib-only: ``inspect_ai``/``inspect_harbor``/``karotte`` are
invoked in subprocesses via pinned ``uv run --with`` so exporting never
requires them to be installed.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.cheat_ladder import parse_attack_selection

INSPECT_AI_PIN = "0.3.276"
INSPECT_HARBOR_PIN = "1.0.0"
KAROTTE_PIN = "3.0.59"
# CI installs Harbor as a uv tool (0.21.0); the lab lock pins 0.24.0. The exact
# binary revision that produced a verdict is recorded in every run output.
HARBOR_TOOL_FALLBACK = "0.21.0"

_JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")


@dataclass
class HarborTask:
    """The Harbor task-dir surface the converters read."""

    task_dir: Path
    task_id: str
    instruction: str
    solve_script: Path  # relative to task_dir
    tests_dir: Path  # relative to task_dir
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)

    @property
    def network_mode(self) -> str | None:
        env = self.config.get("environment")
        return env.get("network_mode") if isinstance(env, dict) else None

    @property
    def resources(self) -> dict[str, Any]:
        env = self.config.get("environment")
        if not isinstance(env, dict):
            return {}
        return {key: env[key] for key in ("cpus", "memory_mb", "storage_mb", "gpus") if key in env}

    @property
    def environment_mode(self) -> str | None:
        verifier = self.config.get("verifier")
        return verifier.get("environment_mode") if isinstance(verifier, dict) else None


def load_harbor_task(task_dir: str | Path, *, require_solution: bool = True) -> HarborTask:
    """Read a Harbor task directory; raise a clear error when it is not one.

    ``solution/solve.sh`` is required only when ``require_solution`` is true:
    only the oracle plan executes the reference solution, so nop/cheat cells
    on solution-less tasks (e.g. MiMo 002552) load with
    ``require_solution=False``. The verifier (``tests/test.sh``) is always
    required -- every platform grades through it.
    """
    root = Path(task_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"task directory not found: {root}")
    instruction_file = root / "instruction.md"
    if not instruction_file.is_file():
        raise ValueError(f"{root}: missing instruction.md (not a Harbor task dir)")
    config_file = root / "task.toml"
    if not config_file.is_file():
        raise ValueError(f"{root}: missing task.toml (not a Harbor task dir)")
    try:
        config = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{root}: unreadable task.toml: {exc}") from exc
    if not isinstance(config, dict) or "task" not in config:
        raise ValueError(f"{root}: task.toml has no [task] section")
    solve_script = root / "solution" / "solve.sh"
    if require_solution and not solve_script.is_file():
        raise ValueError(f"{root}: missing solution/solve.sh (no oracle solver to map)")
    tests_dir = root / "tests"
    if not (tests_dir / "test.sh").is_file():
        raise ValueError(f"{root}: missing tests/test.sh (no scorer to map)")
    task_section = config.get("task", {})
    name = task_section.get("name") if isinstance(task_section, dict) else None
    raw_metadata = config.get("metadata")
    metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_artifacts = config.get("artifacts")
    artifacts: list[str] = (
        [str(a) for a in raw_artifacts] if isinstance(raw_artifacts, list) else []
    )
    return HarborTask(
        task_dir=root,
        task_id=str(name or root.name),
        instruction=instruction_file.read_text(encoding="utf-8"),
        solve_script=Path("solution/solve.sh"),
        tests_dir=Path("tests"),
        config=config,
        metadata=metadata,
        artifacts=artifacts,
    )


def task_workdir(task: HarborTask) -> str:
    """Effective container workdir for a task (Harbor 0.24 definition.py rule).

    task.toml ``[environment].workdir`` wins; else the final-stage Dockerfile
    ``WORKDIR`` (each ``FROM`` resets; relative values resolve against the
    stage workdir); else ``/`` -- Harbor's ``task_env_config.workdir``
    defaults to None, docker exec passes ``-w`` only when set, and the image
    WORKDIR falls back to Docker's ``/``. Always absolute.
    """
    env = task.config.get("environment")
    if isinstance(env, dict):
        declared = env.get("workdir")
        if isinstance(declared, str) and declared:
            return declared if declared.startswith("/") else "/" + declared
    try:
        text = (task.task_dir / "environment" / "Dockerfile").read_text(encoding="utf-8")
    except OSError:
        return "/"
    parsed: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            parsed = None
        elif keyword == "WORKDIR":
            value = rest.strip().strip("\"'")
            if value:
                parsed = value if value.startswith("/") else str(Path(parsed or "/") / value)
    return parsed or "/"


#: Absolute container path the cheat ladder is staged at for scripted agents.
CHEAT_LADDER_CONTAINER_PATH = "/tmp/cheat_ladder.py"

#: Bounds for task files staged into a foreign sandbox (solution/ + tests/).
#: Tasks larger than this need a runner with a real mount, not write_file.
MAX_STAGED_FILE_BYTES = 1_000_000

#: Sandbox container bounds used when the task declares none (shared laptop
#: daemon: stay small; the smoke tasks are sqlite + pytest, not GPU jobs).
DEFAULT_INSPECT_CPUS = 2
DEFAULT_INSPECT_MEMORY_MB = 2048


@dataclass
class ScriptedPlan:
    """A platform-neutral scripted agent: files to stage plus one command."""

    files: dict[str, bytes]
    """Absolute container path -> bytes to stage before running."""
    command: str | None
    """Shell command to run once as the agent, in the task workdir; None = nop."""


def _cheat_ladder_bytes() -> bytes:
    """Read the single-file ladder staged into foreign sandboxes."""
    return (Path(__file__).with_name("cheat_ladder.py")).read_bytes()


def scripted_agent_plan(
    task: HarborTask, agent: str, attacks: tuple[str, ...] = ()
) -> ScriptedPlan:
    """Build the scripted plan for one agent: oracle/nop/cheat.

    Oracle runs ``bash /solution/solve.sh`` from the workdir, Harbor's own
    oracle convention; the runner makes the task's ``solution/`` tree
    available at ``/solution`` for oracle cells only (needs
    ``solution/solve.sh``); nop stages nothing and runs nothing
    (``command`` is None); cheat stages ``cheat_ladder.py`` and runs it with
    the attack subset (empty ``attacks`` = full ladder). Unknown agents,
    unknown attack names, and an oracle plan for a solution-less task raise
    ``ValueError``.
    """
    if agent == "nop":
        return ScriptedPlan(files={}, command=None)
    if agent == "oracle":
        if not (task.task_dir / "solution" / "solve.sh").is_file():
            raise ValueError(f"{task.task_dir}: oracle plan needs solution/solve.sh")
        return ScriptedPlan(files={}, command="bash /solution/solve.sh")
    if agent == "cheat":
        selected = parse_attack_selection(",".join(attacks) if attacks else None)
        command = f"python3 {CHEAT_LADDER_CONTAINER_PATH} --cwd . --evidence-dir ./cheat"
        if attacks:
            command += f" --attacks {','.join(selected)}"
        return ScriptedPlan(
            files={CHEAT_LADDER_CONTAINER_PATH: _cheat_ladder_bytes()},
            command=command,
        )
    raise ValueError(f"unknown scripted agent {agent!r} (oracle/nop/cheat only)")


def harbor_revision() -> str:
    """Identify the Harbor binary that produces native verdicts ($0 query)."""
    try:
        dist_version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        dist_version = None
    binary = shutil.which("harbor")
    binary_version: str | None = None
    if binary is not None:
        try:
            completed = subprocess.run(
                [binary, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            binary_version = (completed.stdout or completed.stderr).strip().splitlines()[0].strip()
    if dist_version is not None and binary_version:
        return f"{binary_version} (dist {dist_version})"
    return binary_version or (
        f"dist {dist_version}"
        if dist_version
        else f"harbor binary not found (CI tool {HARBOR_TOOL_FALLBACK})"
    )


def parse_reward_bytes(
    txt: bytes | None, js: bytes | None, *, source: str = "reward file"
) -> float:
    """Parse a Harbor reward from raw file bytes.

    ``reward.txt`` (a float) wins; otherwise ``reward.json`` under the
    ``score``/``reward`` key. Missing files raise ``FileNotFoundError``
    (an error verdict, never a silent fail); unparseable content raises
    ``ValueError``.
    """
    if txt is not None:
        try:
            return float(txt.decode("utf-8", errors="replace").strip())
        except ValueError as exc:
            raise ValueError(f"{source}/reward.txt is not a number") from exc
    if js is not None:
        try:
            payload = json.loads(js.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}/reward.json is not valid JSON") from exc
        for key in ("score", "reward"):
            if isinstance(payload, dict) and isinstance(payload.get(key), (int, float)):
                return float(payload[key])
        raise ValueError(f"{source}/reward.json has no numeric score/reward key")
    raise FileNotFoundError(f"{source}: neither reward.txt nor reward.json exists")


def read_reward_file(reward_dir: str | Path) -> float:
    """Read a Harbor reward from a logs directory on disk."""
    directory = Path(reward_dir)
    txt = directory / "reward.txt"
    js = directory / "reward.json"
    return parse_reward_bytes(
        txt.read_bytes() if txt.is_file() else None,
        js.read_bytes() if js.is_file() else None,
        source=str(directory),
    )


def verdict_for_reward(reward: float) -> str:
    """Map a Harbor reward to a pass/fail verdict string."""
    return "pass" if reward >= 1.0 else "fail"


def _slug(task_id: str, suffix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:48] or "task"
    name = f"interop-{suffix}-{slug}"[:70]
    if not _JOB_NAME_RE.match(name):
        name = f"interop-{suffix}-task"
    return name


def run_harbor_control(
    task_dir: str | Path,
    agent: str,
    *,
    repo_root: str | Path,
    jobs_dir: str | Path | None = None,
    timeout_seconds: int | None = None,
    require_solution: bool = True,
) -> dict[str, Any]:
    """Run the Harbor-native oracle/nop control locally (docker, $0).

    Direct execution through ``Executor.execute_direct`` -- the same path
    ``evallab run`` uses, with no campaign-queue admission gate. Returns a
    verdict dict; infrastructure failures are ``error``, never ``fail``.
    ``require_solution=False`` loads solution-less tasks (nop needs no
    reference solution); whether Harbor itself then runs is its verdict.
    """
    from evallab.execution_contracts import RunRequest
    from evallab.queue import Executor
    from evallab.results import load_job

    if agent not in ("oracle", "nop"):
        raise ValueError(f"refusing non-control agent {agent!r} (oracle/nop only, $0)")
    root = Path(repo_root)
    task = load_harbor_task(task_dir, require_solution=require_solution)
    harbor_rev = harbor_revision()
    work = (
        Path(jobs_dir) if jobs_dir is not None else Path(tempfile.mkdtemp(prefix="interop-jobs-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    request_kwargs: dict[str, Any] = {
        "task": task.task_dir,
        "agent": agent,
        "name": _slug(task.task_id, agent),
        "jobs_dir": work,
        "environment": "docker",
    }
    if timeout_seconds is not None:
        request_kwargs["timeout_seconds"] = timeout_seconds
    request = RunRequest(**request_kwargs)  # type: ignore[arg-type]
    try:
        job_dir = Executor.from_repo(root, create_queue=False).execute_direct(request, ingest=False)
    except Exception as exc:
        return {
            "target": "harbor",
            "agent": agent,
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": f"harbor execution failed: {type(exc).__name__}: {exc}",
            "job_dir": None,
            "harbor_rev": harbor_rev,
        }
    try:
        job = load_job(job_dir)
        reward = job.trials[0].primary_reward if len(job.trials) == 1 else None
    except (OSError, ValueError, IndexError) as exc:
        return {
            "target": "harbor",
            "agent": agent,
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": f"unreadable native job: {exc}",
            "job_dir": str(job_dir),
            "harbor_rev": harbor_rev,
        }
    if reward is None:
        return {
            "target": "harbor",
            "agent": agent,
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": "native trial recorded no reward",
            "job_dir": str(job_dir),
            "harbor_rev": harbor_rev,
        }
    return {
        "target": "harbor",
        "agent": agent,
        "task_id": task.task_id,
        "verdict": verdict_for_reward(reward),
        "reward": reward,
        "reason": None,
        "job_dir": str(job_dir),
        "harbor_rev": harbor_rev,
    }


def _repo_root() -> Path:
    """Repo root for direct execution (this file lives at src/evallab/)."""
    return Path(__file__).resolve().parents[2]


def _trial_reward_from_job(job_dir: Path) -> float:
    """Read the single trial's primary reward; raise when unscored."""
    from evallab.results import load_job

    job = load_job(job_dir)
    if len(job.trials) != 1:
        raise ValueError(f"expected one trial in {job_dir}, found {len(job.trials)}")
    reward = job.trials[0].primary_reward
    if reward is None:
        raise ValueError(f"native trial in {job_dir} recorded no reward")
    return reward


def run_harbor_cell(
    task_dir: str | Path,
    agent: str,
    attacks: tuple[str, ...] = (),
    *,
    workdir: str | Path,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one matrix cell on the Harbor target (oracle/nop/cheat, docker, $0).

    Oracle/nop reuse ``run_harbor_control``; cheat reuses the ``evallab cheat``
    lane (``cheat.run_cheat_trial``), never a copy of it. Returns the shared
    cell dict: pass/fail/skipped/error verdict (reward >= 1.0 passes),
    reward, reason, platform version, and evidence path.
    """
    from evallab.cheat import run_cheat_trial

    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    harbor_rev = harbor_revision()
    cell: dict[str, Any] = {
        "target": "harbor",
        "agent": agent,
        "attacks": list(attacks),
        "platform_version": harbor_rev,
    }
    try:
        task = load_harbor_task(task_dir, require_solution=(agent == "oracle"))
    except (FileNotFoundError, ValueError) as exc:
        cell.update(
            {
                "verdict": "error",
                "reward": None,
                "reason": f"cannot load task: {exc}",
                "evidence": None,
            }
        )
        return cell
    if agent in ("oracle", "nop"):
        # --attacks selects the ladder subset for cheat cells only; controls
        # ignore it (matrix passes one attack list for the whole row).
        cell["attacks"] = []
        try:
            result = run_harbor_control(
                task.task_dir,
                agent,
                repo_root=_repo_root(),
                jobs_dir=work / "jobs",
                timeout_seconds=timeout_seconds,
                require_solution=(agent == "oracle"),
            )
        except Exception as exc:
            cell.update(
                {
                    "verdict": "error",
                    "reward": None,
                    "reason": f"harbor execution failed: {type(exc).__name__}: {exc}",
                    "evidence": None,
                }
            )
            return cell
        cell.update(
            {
                "verdict": result["verdict"],
                "reward": result.get("reward"),
                "reason": result.get("reason"),
                "evidence": result.get("job_dir"),
            }
        )
        return cell
    if agent == "cheat":
        try:
            selected = parse_attack_selection(",".join(attacks) if attacks else None)
        except ValueError as exc:
            cell.update(
                {
                    "verdict": "error",
                    "reward": None,
                    "reason": f"unknown attack: {exc}",
                    "evidence": None,
                }
            )
            return cell
        try:
            from evallab.harbor_view import installed_harbor_version

            version = installed_harbor_version()
            if version is None or version < (0, 24):
                raise RuntimeError(
                    "evallab cheat lane needs Harbor >= 0.24 (uv sync --frozen --extra laminar)"
                )
            job_dir, _payload = run_cheat_trial(
                task.task_dir,
                selected,
                repo_root=_repo_root(),
                jobs_dir=work / "jobs",
                name=_slug(task.task_id, "cheat"),
                timeout_seconds=timeout_seconds or 600,
            )
            reward = _trial_reward_from_job(job_dir)
        except Exception as exc:
            cell.update(
                {
                    "verdict": "error",
                    "reward": None,
                    "reason": f"harbor cheat run failed: {type(exc).__name__}: {exc}",
                    "evidence": None,
                }
            )
            return cell
        cell.update(
            {
                "verdict": verdict_for_reward(reward),
                "reward": reward,
                "reason": None,
                "evidence": str(job_dir),
            }
        )
        return cell
    cell.update(
        {
            "verdict": "error",
            "reward": None,
            "reason": f"unknown agent {agent!r} (oracle/nop/cheat only)",
            "evidence": None,
        }
    )
    return cell


#: Live runs deliberately skip the campaign queue's dedicated-host admission
#: gate (``campaign_execution.docker_available_resources`` refuses whenever
#: ANY container on the shared laptop daemon is unbounded, e.g. the lab's own
#: postgres or another agent's long-running container). Local ``evallab run``
#: and ``evallab cheat run`` use no such pre-check: they attempt direct
#: execution and surface Docker failures as errors. Interop live cells do the
#: same -- infrastructure failures are ``error`` cells, never ``fail``.


# ---------------------------------------------------------------------------
# export-harbor --to inspect
# ---------------------------------------------------------------------------
INSPECT_TASK_TEMPLATE = '''"""Inspect AI task exported from Harbor task @@TASK_ID@@.

Generated by ``evallab interop export-harbor --to inspect``. Pure ``inspect_ai``:
this file never imports ``inspect_harbor``. Run with::

    inspect eval task.py --sandbox docker --model mockllm/model

The oracle solver runs the Harbor reference solution; the scorer runs the
Harbor verifier (``tests/test.sh``) and grades the ``reward.txt``/``reward.json``
it writes. A missing reward file is an *error*, never a silent fail.
"""
from __future__ import annotations

import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.util import sandbox

TASK_ID = "@@TASK_ID@@"
TASK_DIR = Path(__file__).resolve().parent
VERIFIER_TIMEOUT = @@VERIFIER_TIMEOUT@@
METADATA = json.loads((TASK_DIR / "metadata.json").read_text(encoding="utf-8"))


def parse_reward_bytes(
    txt: bytes | None, js: bytes | None, *, source: str = "reward file"
) -> float:
    """Reward from raw file bytes: reward.txt float, else reward.json score/reward."""
    if txt is not None:
        try:
            return float(txt.decode("utf-8", errors="replace").strip())
        except ValueError as exc:
            raise ValueError(f"{source}/reward.txt is not a number") from exc
    if js is not None:
        try:
            payload = json.loads(js.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}/reward.json is not valid JSON") from exc
        for key in ("score", "reward"):
            if isinstance(payload, dict) and isinstance(payload.get(key), (int, float)):
                return float(payload[key])
        raise ValueError(f"{source}/reward.json has no numeric score/reward key")
    raise FileNotFoundError(f"{source}: neither reward.txt nor reward.json exists")


async def _copy_dir(local_dir: Path, container_path: str) -> None:
    for file_path in sorted(local_dir.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(local_dir).as_posix()
            await sandbox().write_file(f"{container_path}/{rel}", file_path.read_bytes())


@solver
def oracle_solver() -> Solver:
    """Reference-solution solver: copies solution/ in and runs solve.sh."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await _copy_dir(TASK_DIR / "solution", "/solution")
        await sandbox().exec(["bash", "/solution/solve.sh"])
        return state

    return solve


@scorer(metrics=[])
def harbor_test_scorer() -> Scorer:
    """Verifier scorer: runs tests/test.sh, grades its reward file."""

    async def score(state: TaskState, target: Target) -> Score:
        await sandbox().exec(["mkdir", "-p", "/tests", "/logs/verifier"])
        await _copy_dir(TASK_DIR / "tests", "/tests")
        completed = await sandbox().exec(
            ["bash", "/tests/test.sh"], timeout=VERIFIER_TIMEOUT
        )
        log = f"Test exit code: {completed.returncode}\\n\\n{(completed.stdout or '')[-2000:]}"
        try:
            txt_raw = await sandbox().read_file("/logs/verifier/reward.txt")
        except Exception:
            txt_raw = None
        try:
            json_raw = await sandbox().read_file("/logs/verifier/reward.json")
        except Exception:
            json_raw = None
        txt = txt_raw.encode() if isinstance(txt_raw, str) else txt_raw
        js = json_raw.encode() if isinstance(json_raw, str) else json_raw
        try:
            reward = parse_reward_bytes(txt, js, source="/logs/verifier")
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"{TASK_ID}: verifier wrote no reward file; {log}"
            ) from exc
        return Score(
            value=reward,
            answer="PASS" if reward >= 1.0 else "FAIL",
            explanation=log,
        )

    return score


@task
def harbor_task() -> Task:
    instruction = (TASK_DIR / "instruction.md").read_text(encoding="utf-8")
    return Task(
        dataset=[
            Sample(
                input=instruction,
                id=TASK_ID,
                metadata=METADATA.get("metadata", {}),
            )
        ],
        solver=[oracle_solver()],
        scorer=harbor_test_scorer(),
        sandbox=("docker", str(TASK_DIR / "compose.yaml")),
        metadata={"harbor_task": TASK_ID, **METADATA.get("metadata", {})},
    )
'''

INSPECT_COMPOSE_TEMPLATE = """# Sandbox image for the exported Inspect AI task (Harbor: @@TASK_ID@@).
# Built from the Harbor environment Dockerfile, unchanged.
services:
  default:
    build:
      context: .
      dockerfile: environment/Dockerfile
"""


def export_inspect(task_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Export a Harbor task dir to a self-contained Inspect AI task dir."""
    task = load_harbor_task(task_dir)
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"refusing to write into non-empty directory: {out}")
    (out / "solution").mkdir(parents=True, exist_ok=True)
    (out / "tests").mkdir(parents=True, exist_ok=True)
    (out / "environment").mkdir(parents=True, exist_ok=True)
    (out / "instruction.md").write_text(task.instruction, encoding="utf-8")
    # Copy the whole solution dir (solve.sh plus any helpers like solve.py):
    # oracle_solver stages all of solution/ into the sandbox at runtime.
    for child in sorted((task.task_dir / "solution").iterdir()):
        dest = out / "solution" / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    for child in sorted((task.task_dir / task.tests_dir).iterdir()):
        dest = out / "tests" / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    dockerfile = task.task_dir / "environment" / "Dockerfile"
    if dockerfile.is_file():
        shutil.copy2(dockerfile, out / "environment" / "Dockerfile")
    verifier = task.config.get("verifier")
    timeout = 600
    if isinstance(verifier, dict) and isinstance(verifier.get("timeout_sec"), (int, float)):
        timeout = int(verifier["timeout_sec"])
    metadata_payload = {
        "harbor_task": task.task_id,
        "metadata": task.metadata,
        "task": task.config.get("task", {}),
    }
    (out / "metadata.json").write_text(
        json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "task.py").write_text(
        INSPECT_TASK_TEMPLATE.replace("@@TASK_ID@@", task.task_id).replace(
            "@@VERIFIER_TIMEOUT@@", str(timeout)
        ),
        encoding="utf-8",
    )
    (out / "compose.yaml").write_text(
        INSPECT_COMPOSE_TEMPLATE.replace("@@TASK_ID@@", task.task_id), encoding="utf-8"
    )
    notes: list[str] = []
    if task.network_mode not in (None, "bridge", "none"):
        notes.append(
            f"network_mode={task.network_mode!r}: the Harbor agent/verifier network "
            "policy has no Inspect-AI-sandbox equivalent; the exported compose service "
            "uses the default docker network."
        )
    if task.environment_mode == "separate":
        notes.append(
            "verifier.environment_mode=separate: Harbor runs this verifier in a "
            "separate image (see tests/Dockerfile); the export scores in the same "
            "sandbox image, so verifier-only system deps must be folded in manually."
        )
    mapping = (
        f"# Inspect export mapping: {task.task_id}\n\n"
        f"Source: `{task.task_dir}`\n\n"
        "| Harbor | Inspect export |\n"
        "| --- | --- |\n"
        "| instruction.md | Sample input (task.py `harbor_task`) |\n"
        "| solution/solve.sh | `oracle_solver` (copies solution/, runs solve.sh) |\n"
        "| tests/test.sh | `harbor_test_scorer` (runs test.sh, grades reward file) |\n"
        "| reward.txt / reward.json | `parse_reward_bytes`; missing file = error |\n"
        "| task.toml [task] + [metadata] | metadata.json + Sample/Task metadata |\n"
        "| environment/Dockerfile | environment/Dockerfile via compose.yaml |\n"
        "\n## Lossy mappings\n\n"
        + (
            "".join(f"- {note}\n" for note in notes)
            if notes
            else "None: every Harbor surface above has a direct Inspect equivalent.\n"
        )
    )
    (out / "MAPPING.md").write_text(mapping, encoding="utf-8")
    return {
        "target": "inspect",
        "task_id": task.task_id,
        "out_dir": str(out),
        "files": sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()),
        "lossy_notes": notes,
    }


# ---------------------------------------------------------------------------
# export-harbor --to karotte
# ---------------------------------------------------------------------------
def karotte_flags(task: HarborTask) -> list[dict[str, str]]:
    """Flag every lossy or impossible Harbor -> Karotte mapping.

    Returns ``[{code, detail}]``; an empty list means a faithful mapping.
    Anything detected here lands in the generated MAPPING.md, never dropped.
    Only real losses of the generic runner are flagged: the judge replays the
    task's own tests/test.sh as root against collected submission copies, so
    verifier packaging (apt/sudo) and artifact-less tasks map faithfully, and
    task-declared cpus/memory_mb bound the run container like elsewhere.
    """
    flags: list[dict[str, str]] = []
    env_dir = task.task_dir / "environment"
    if any((env_dir / name).is_file() for name in ("docker-compose.yaml", "docker-compose.yml")):
        flags.append(
            {
                "code": "multi-service-compose",
                "detail": (
                    "task ships its own Compose file; the generic runner builds "
                    "one image and errors on compose tasks. Re-express extra "
                    "services as data mounts or sidecars manually."
                ),
            }
        )
    if task.network_mode not in (None, "none"):
        flags.append(
            {
                "code": "network-policy",
                "detail": (
                    f"task.toml network_mode={task.network_mode!r} has no karotte "
                    "equivalent: karotte cannot enforce Harbor network policies, "
                    "it only confines the student (or degrades to open). "
                    "Re-check any verifier that depends on egress control."
                ),
            }
        )
    if task.environment_mode == "separate":
        flags.append(
            {
                "code": "separate-verifier-image",
                "detail": (
                    "verifier.environment_mode=separate: Harbor scores in a dedicated "
                    "verifier image (tests/Dockerfile). The generic runner grades "
                    "in the same container with the task toolchain; fold "
                    "verifier-only system deps into the grade or accept the drift."
                ),
            }
        )
    env = task.config.get("environment")
    if isinstance(env, dict) and env.get("mcp_servers"):
        flags.append(
            {
                "code": "mcp-servers",
                "detail": (
                    "task declares environment MCP servers, which have no karotte "
                    "mapping (no sidecars); re-provide them as karotte tools or "
                    "drop with justification."
                ),
            }
        )
    solution_env = task.config.get("solution")
    if isinstance(solution_env, dict) and solution_env.get("env"):
        flags.append(
            {
                "code": "solution-env",
                "detail": (
                    "task declares [solution.env] overrides for the oracle run; the "
                    "generic oracle runs solve.sh without them, so these are "
                    "documentation only."
                ),
            }
        )
    return flags


def export_karotte(task_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Export a Harbor task dir to the runnable karotte env the runner builds.

    Writes the same deterministic tree ``interop_karotte.run_cell`` assembles
    (vendored support package, generic task package, nop fake model, the
    task's own tests/ under root_data, harness Containerfile, pinned
    project), plus MAPPING.md. Offline and deterministic: no karotte import,
    no ``uv lock``, no docker. The base image tag for Dockerfile tasks is
    reserved (``evallab-harbor-<digest>``) and built on first run; run
    ``uv lock && uv sync`` in the output dir before ``karotte build``.
    """
    from evallab.interop_karotte import (
        _dir_digest,
        resolve_spec,
        write_env_tree,
    )

    task = load_harbor_task(task_dir, require_solution=False)
    task.task_dir = task.task_dir.resolve()
    spec = resolve_spec(task)
    if spec.base_kind == "docker_image":
        base_tag = spec.base_ref
    else:
        base_tag = f"evallab-harbor-{_dir_digest(task.task_dir / 'environment')}"
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"refusing to write into non-empty directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    write_env_tree(
        task,
        spec,
        out,
        base_tag=base_tag,
        plan=scripted_agent_plan(task, "nop", ()),
    )
    flags = karotte_flags(task)
    submission_doc = ", ".join(spec.submission_paths) or "(none)"
    mapping = (
        f"# Karotte env mapping: {task.task_id}\n\n"
        f"Source: `{task.task_dir}`\n\n"
        f"Base image: `{base_tag}`"
        f"{' (pinned task image)' if spec.base_kind == 'docker_image' else ' (reserved; built from environment/Dockerfile on first run)'}\n\n"
        "| Harbor | Karotte env |\n"
        "| --- | --- |\n"
        "| instruction.md | Step.instructions (generic task package) |\n"
        f"| task artifacts | Step.submission_paths ({submission_doc}) |\n"
        "| tests/test.sh | generic judge restores collected copies at original paths, runs `bash /tests/test.sh` from "
        f"{spec.workdir}, grades /logs/verifier via interop.parse_reward_bytes |\n"
        "| solution/solve.sh | mounted at `/solution` for oracle cells only |\n"
        "| environment/Dockerfile | harness Containerfile FROM the Harbor student image |\n"
        "| task.toml [task] + [metadata] | Task.id + module docstring |\n"
        "\n## Submission / custody split\n\n"
        f"- student-writable: {submission_doc} (collected root-only, workdir wiped, copies graded)\n"
        "- verifier-only, student never sees: root_data/tests, scoring module, fake model\n"
        "\n## Lossy or impossible mappings (never silently dropped)\n\n"
        + (
            "".join(f"- **{f['code']}**: {f['detail']}\n" for f in flags)
            if flags
            else "None: the Harbor surface above maps directly.\n"
        )
        + "\n## Build\n\n"
        "- `uv lock && uv sync` in this dir (network), then `karotte build --runtime docker`.\n"
        "- Fake model default: nop (single text message, zero tool calls).\n"
    )
    (out / "MAPPING.md").write_text(mapping, encoding="utf-8")
    return {
        "target": "karotte",
        "task_id": task.task_id,
        "karotte_id": spec.karotte_id,
        "out_dir": str(out),
        "base": base_tag,
        "files": sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()),
        "flags": [f["code"] for f in flags],
    }


# ---------------------------------------------------------------------------
# inspect: scripted oracle/nop/cheat cells under Inspect AI (pinned)
# ---------------------------------------------------------------------------
INSPECT_SCRIPTED_DRIVER = '''"""Run one scripted agent (oracle/nop/cheat) under Inspect AI (generated, $0)."""
import importlib.metadata
import json
from pathlib import Path

WORK = Path(@@WORK_REPR@@)
TASK_DIR = Path(@@TASK_DIR_REPR@@)


def _reward_from_values(values):
    reward = None
    for value in values:
        if isinstance(value, bool):
            reward = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            reward = float(value)
        elif value == 'C':
            reward = 1.0
        elif value == 'I':
            reward = 0.0
    return reward


def _dist_version(name, module=None):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return getattr(module, '__version__', 'unknown') if module is not None else 'unknown'


def main() -> int:
    manifest = json.loads((WORK / 'manifest.json').read_text(encoding='utf-8'))
    agent = manifest['agent']
    import inspect_ai
    import inspect_harbor
    from inspect_ai import eval as inspect_eval
    from inspect_ai.solver import Generate, Solver, TaskState, solver
    from inspect_ai.util import sandbox

    try:
        from inspect_harbor._harbor.sandbox_utils import resolve_env_vars
    except ImportError:
        resolve_env_vars = None

    @solver
    def scripted_solver() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            record = {'agent': agent, 'command': manifest.get('command')}
            if agent != 'nop':
                sb = sandbox()
                await sb.exec(['mkdir', '-p', manifest['workdir'], '/logs/verifier'])
                for rel in manifest.get('task_files', []):
                    await sb.write_file('/' + rel, (TASK_DIR / rel).read_bytes())
                for container_path, blob in manifest.get('files', []):
                    await sb.write_file(container_path, (WORK / blob).read_bytes())
                command = manifest.get('command')
                if command:
                    solution_env_raw = state.metadata.get('solution_env', {})
                    if solution_env_raw and resolve_env_vars is not None:
                        solution_env = resolve_env_vars(solution_env_raw)
                    else:
                        solution_env = None
                    completed = await sb.exec(
                        ['bash', '-c', command],
                        cwd=manifest['workdir'],
                        timeout=manifest.get('agent_timeout'),
                        timeout_retry=False,
                        env=solution_env,
                    )
                    record['returncode'] = completed.returncode
                    record['stdout_tail'] = (completed.stdout or '')[-4000:]
                    record['stderr_tail'] = (completed.stderr or '')[-4000:]
            (WORK / 'agent-exec.json').write_text(json.dumps(record, indent=2) + '\\n')
            return state

        return solve

    task_obj = inspect_harbor.harbor(
        path=str(TASK_DIR),
        sandbox_env_name='docker',
        override_cpus=manifest.get('override_cpus'),
        override_memory_mb=manifest.get('override_memory_mb'),
    )
    logs = inspect_eval(
        tasks=[task_obj],
        solver=[scripted_solver()],
        model='mockllm/model',
        limit=1,
        log_format='json',
        log_dir=str(WORK / 'inspect-logs'),
    )
    if not logs or not logs[0].samples:
        print(json.dumps({'verdict': 'error', 'reason': 'inspect produced no samples'}))
        return 0
    sample = logs[0].samples[0]
    if sample.error:
        print(json.dumps({'verdict': 'error', 'reason': str(sample.error)[:500]}))
        return 0
    reward = _reward_from_values([s.value for s in (sample.scores or {}).values()])
    if reward is None:
        values = [repr(s.value) for s in (sample.scores or {}).values()]
        detail = ','.join(values)[:300]
        print(json.dumps({'verdict': 'error', 'reason': 'unreadable scores: ' + detail}))
        return 0
    print(json.dumps({
        'verdict': 'pass' if reward >= 1.0 else 'fail',
        'reward': reward,
        'inspect_ai': _dist_version('inspect-ai', inspect_ai),
        'inspect_harbor': _dist_version('inspect-harbor', inspect_harbor),
    }))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
'''


def _collect_solution_files(task: HarborTask) -> tuple[list[str], list[str]]:
    """Stageable ``solution/`` files (task-relative posix) plus skipped-large.

    Oracle cells only: the reference solution lands at ``/solution`` like
    Harbor's oracle upload. Agents never see ``tests/`` (Harbor uploads tests
    after the agent) and nop/cheat never see the solution, so staging nothing
    else keeps cheat cells as blind as on Harbor. Files over
    ``MAX_STAGED_FILE_BYTES`` are skipped and reported.
    """
    collected: list[str] = []
    skipped: list[str] = []
    root = task.task_dir / "solution"
    paths = sorted(root.rglob("*")) if root.is_dir() else []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(task.task_dir).as_posix()
        if path.stat().st_size > MAX_STAGED_FILE_BYTES:
            skipped.append(rel)
            continue
        collected.append(rel)
    return collected, skipped


def _write_inspect_run(
    work: Path,
    task: HarborTask,
    *,
    agent: str,
    attacks: tuple[str, ...],
    agent_timeout: int,
    override_cpus: int,
    override_memory_mb: int,
) -> Path:
    """Stage the manifest, plan blobs, and driver for one inspect cell (no docker).

    Pure file staging: safe to unit-test without a daemon. Raises ``ValueError``
    for unknown agents/attacks via ``scripted_agent_plan``.
    """
    plan = scripted_agent_plan(task, agent, attacks)
    work.mkdir(parents=True, exist_ok=True)
    stage = work / "stage"
    stage.mkdir(exist_ok=True)
    blobs: list[list[str]] = []
    for index, (container_path, data) in enumerate(plan.files.items()):
        name = f"blob-{index}.bin"
        (stage / name).write_bytes(data)
        blobs.append([container_path, f"stage/{name}"])
    task_files, skipped = _collect_solution_files(task) if agent == "oracle" else ([], [])
    manifest = {
        "agent": agent,
        "attacks": list(attacks),
        "command": plan.command,
        "files": blobs,
        "task_files": task_files,
        "skipped_large": skipped,
        "workdir": task_workdir(task),
        "agent_timeout": agent_timeout,
        "override_cpus": override_cpus,
        "override_memory_mb": override_memory_mb,
    }
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    driver = work / "inspect_driver.py"
    driver.write_text(
        INSPECT_SCRIPTED_DRIVER.replace("@@WORK_REPR@@", repr(str(work))).replace(
            "@@TASK_DIR_REPR@@", repr(str(task.task_dir))
        ),
        encoding="utf-8",
    )
    return driver


def _docker_ps() -> dict[str, str] | None:
    """Running container ID -> image, or None when the daemon is unreachable."""
    try:
        completed = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Image}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    running: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 1)
        if parts:
            running[parts[0]] = parts[1] if len(parts) > 1 else ""
    return running


def _cleanup_inspect_containers(before: dict[str, str] | None, work: Path) -> dict[str, Any]:
    """Remove sandbox containers this cell started; never touch others'.

    Ownership is strict: only containers absent from ``before`` whose image
    name starts with inspect_harbor's content-addressed ``hb__`` task-image
    prefix are removed. Anything else new is reported, not removed.
    """
    report: dict[str, Any] = {"removed": [], "left": []}
    after = _docker_ps()
    if before is None or after is None:
        report["left"].append("daemon unreachable for cleanup snapshot")
        (work / "container-cleanup.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return report
    for cid, image in sorted(after.items()):
        if cid in before:
            continue
        repo = image.rsplit("/", 1)[-1].split(":")[0]
        if not repo.startswith("hb__"):
            report["left"].append(f"{cid} ({image or 'no-image'})")
            continue
        try:
            completed = subprocess.run(
                ["docker", "rm", "--force", cid],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            report["left"].append(f"{cid} ({image}): {type(exc).__name__}")
            continue
        if completed.returncode == 0:
            report["removed"].append(f"{cid} ({image})")
        else:
            report["left"].append(f"{cid} ({image}): {completed.stderr.strip()[-200:]}")
    (work / "container-cleanup.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def run_inspect_cell(
    task_dir: str | Path,
    agent: str,
    attacks: tuple[str, ...] = (),
    *,
    workdir: str | Path,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one matrix cell on the Inspect AI target (oracle/nop/cheat, $0).

    The pinned subprocess driver builds the ``inspect_harbor.harbor(path=...)``
    task with a bounded local docker sandbox and runs a generated scripted
    solver: it stages the plan files via ``sandbox().write_file`` and runs the
    plan command via ``sandbox().exec`` (nop is a no-op solver), then the
    inspect_harbor scorer grades. Sandbox containers are bounded (task
    resources, else 2 CPU / 2048 MB) and removed when verifiably ours.
    """
    attacks = tuple(attacks)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    cell: dict[str, Any] = {
        "target": "inspect",
        "agent": agent,
        "attacks": list(attacks),
        "platform_version": f"inspect-ai {INSPECT_AI_PIN} + inspect-harbor {INSPECT_HARBOR_PIN}",
    }

    def _fail(reason: str) -> dict[str, Any]:
        cell.update({"verdict": "error", "reward": None, "reason": reason, "evidence": str(work)})
        return cell

    try:
        task = load_harbor_task(task_dir, require_solution=(agent == "oracle"))
    except (FileNotFoundError, ValueError) as exc:
        return _fail(f"cannot load task: {exc}")

    if agent not in ("oracle", "nop", "cheat"):
        return _fail(f"unknown agent {agent!r} (oracle/nop/cheat only)")
    if agent in ("oracle", "nop"):
        # --attacks selects the ladder subset for cheat cells only; controls
        # ignore it (matrix passes one attack list for the whole row).
        attacks = ()
        cell["attacks"] = []
    try:
        scripted_agent_plan(task, agent, attacks)
    except ValueError as exc:
        return _fail(f"cannot build {agent} plan: {exc}")
    agent_timeout = timeout_seconds or 900
    resources = task.resources
    raw_cpus = resources.get("cpus")
    override_cpus = (
        int(raw_cpus)
        if isinstance(raw_cpus, (int, float)) and not isinstance(raw_cpus, bool) and raw_cpus > 0
        else DEFAULT_INSPECT_CPUS
    )
    raw_memory = resources.get("memory_mb")
    override_memory_mb = (
        int(raw_memory)
        if isinstance(raw_memory, int) and not isinstance(raw_memory, bool) and raw_memory > 0
        else DEFAULT_INSPECT_MEMORY_MB
    )
    before = _docker_ps()
    try:
        driver = _write_inspect_run(
            work,
            task,
            agent=agent,
            attacks=attacks,
            agent_timeout=agent_timeout,
            override_cpus=override_cpus,
            override_memory_mb=override_memory_mb,
        )
    except (OSError, ValueError) as exc:
        return _fail(f"cannot stage inspect run: {type(exc).__name__}: {exc}")
    uv = shutil.which("uv")
    if uv is None:
        return _fail("uv binary not found; cannot provision the pinned inspect stack")
    try:
        completed = subprocess.run(
            [
                uv,
                "run",
                "--no-project",
                "--with",
                f"inspect-ai=={INSPECT_AI_PIN}",
                "--with",
                f"inspect-harbor=={INSPECT_HARBOR_PIN}",
                "python",
                str(driver),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=agent_timeout + 600,
            cwd=str(work),
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup_inspect_containers(before, work)
        return _fail(f"inspect run timed out after {exc.timeout}s")
    _cleanup_inspect_containers(before, work)
    payload: dict[str, Any] | None = None
    for line in (completed.stdout or "").strip().splitlines()[::-1]:
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            payload = candidate
            break
    if payload is None:
        detail = ((completed.stdout or "") + (completed.stderr or ""))[-800:]
        return _fail(
            f"inspect driver produced no verdict JSON (exit {completed.returncode}): {detail}"
        )
    if payload.get("inspect_ai") or payload.get("inspect_harbor"):
        cell["platform_version"] = (
            f"inspect-ai {payload.get('inspect_ai', INSPECT_AI_PIN)} + "
            f"inspect-harbor {payload.get('inspect_harbor', INSPECT_HARBOR_PIN)}"
        )
    cell.update(
        {
            "verdict": payload.get("verdict", "error"),
            "reward": payload.get("reward"),
            "reason": payload.get("reason"),
            "evidence": str(work),
        }
    )
    return cell


# ---------------------------------------------------------------------------
# matrix: per-task grading table across wired targets
# ---------------------------------------------------------------------------
def run_karotte_cell(
    task_dir: str | Path, agent: str, attacks: tuple[str, ...] = (), **kw: Any
) -> dict[str, Any]:
    """Karotte target (lazy: ``interop_karotte`` imports this module)."""
    from evallab.interop_karotte import run_cell

    return run_cell(task_dir, agent, attacks, **kw)


def run_verifiers_cell(
    task_dir: str | Path, agent: str, attacks: tuple[str, ...] = (), **kw: Any
) -> dict[str, Any]:
    """Prime verifiers target (lazy: ``interop_verifiers`` imports this module)."""
    from evallab.interop_verifiers import run_cell

    return run_cell(Path(task_dir), agent, tuple(attacks), **kw)


MATRIX_TARGETS: dict[str, Callable[..., dict[str, Any]]] = {
    "harbor": run_harbor_cell,
    "inspect": run_inspect_cell,
    "karotte": run_karotte_cell,
    "verifiers": run_verifiers_cell,
}
"""Target name -> ``run_cell`` runner (contract: task_dir, agent, attacks, *, workdir, timeout_seconds)."""

MATRIX_AGENTS = ("oracle", "nop", "cheat")


def _cell_display(target: str, agent: str, verdict: str) -> str:
    """Table text: control cells show pass/fail, cheat cells show cracked/clean."""
    del target
    if agent == "cheat":
        return {"pass": "cracked", "fail": "clean"}.get(verdict, verdict)
    return verdict


def grade_task_row(
    cells: dict[str, str], *, targets: list[str], agents: list[str]
) -> tuple[str, list[str]]:
    """Grade one task row: ``ok`` | ``broken`` | ``n/a`` plus problem list.

    Grading is broken when a comparable oracle cell is not ``pass`` or a
    comparable nop cell is not ``fail``. Skipped/error cells carry no grading
    signal; rows with no comparable control cell grade ``n/a``.
    """
    problems: list[str] = []
    seen_control = False
    for target in targets:
        for agent in agents:
            if agent not in ("oracle", "nop"):
                continue
            verdict = cells.get(f"{target}:{agent}")
            if verdict not in ("pass", "fail"):
                continue
            seen_control = True
            if agent == "oracle" and verdict != "pass":
                problems.append(f"{target}:oracle expected pass, got {verdict}")
            if agent == "nop" and verdict != "fail":
                problems.append(f"{target}:nop expected fail, got {verdict}")
    if problems:
        return "broken", problems
    return ("ok" if seen_control else "n/a"), []


def _matrix_cell(
    target: str, agent: str, attacks: tuple[str, ...], verdict: str, reason: str
) -> dict[str, Any]:
    """A cell the matrix decides itself (no runner call): skipped or error."""
    return {
        "target": target,
        "agent": agent,
        "attacks": list(attacks),
        "verdict": verdict,
        "reward": None,
        "reason": reason,
        "platform_version": None,
        "evidence": None,
    }


def matrix_task_row(
    task_dir: str | Path,
    *,
    targets: tuple[str, ...],
    agents: tuple[str, ...],
    attacks: tuple[str, ...],
    workdir: str | Path,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run every (target, agent) cell for one task dir through ``MATRIX_TARGETS``."""
    # Row identity needs no reference solution. A solution-less task has no
    # oracle control on any platform, so that cell is skipped here uniformly
    # instead of each runner reporting it differently; nop/cheat still run.
    task = load_harbor_task(task_dir, require_solution=False)
    has_solution = (task.task_dir / "solution" / "solve.sh").is_file()
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    cells: dict[str, str] = {}
    details: dict[str, Any] = {}
    for target in targets:
        runner = MATRIX_TARGETS.get(target)
        for agent in agents:
            key = f"{target}:{agent}"
            cell_attacks = tuple(attacks) if agent == "cheat" else ()
            if runner is None:
                cell = _matrix_cell(
                    target,
                    agent,
                    cell_attacks,
                    "skipped",
                    f"target {target!r} has no runner in MATRIX_TARGETS",
                )
            elif agent == "oracle" and not has_solution:
                cell = _matrix_cell(
                    target,
                    agent,
                    cell_attacks,
                    "skipped",
                    "task ships no solution/solve.sh; no oracle control",
                )
            else:
                try:
                    cell = runner(
                        task.task_dir,
                        agent,
                        cell_attacks,
                        workdir=work / f"{target}-{agent}",
                        timeout_seconds=timeout_seconds,
                    )
                except Exception as exc:
                    cell = _matrix_cell(
                        target,
                        agent,
                        cell_attacks,
                        "error",
                        f"cell runner raised {type(exc).__name__}: {exc}",
                    )
            cells[key] = str(cell.get("verdict", "error"))
            details[key] = cell
    grading, problems = grade_task_row(cells, targets=list(targets), agents=list(agents))
    versions: dict[str, Any] = {}
    for target in targets:
        for agent in agents:
            version = details.get(f"{target}:{agent}", {}).get("platform_version")
            if version is not None and target not in versions:
                versions[target] = version
    return {
        "task_id": task.task_id,
        "task_dir": str(task.task_dir),
        "cells": cells,
        "grading": grading,
        "problems": problems,
        "versions": versions,
        "details": details,
    }


def render_grading_matrix(
    rows: list[dict[str, Any]], *, targets: list[str], agents: list[str]
) -> str:
    """Render the matrix: control cells pass/fail, cheat cells cracked/clean."""
    columns = [f"{target}:{agent}" for target in targets for agent in agents]
    header = ["task", *columns, "grading"]
    widths = [max(len(r["task_id"]) for r in rows + [{"task_id": "task"}])]
    widths += [max(len(c), 7) for c in columns] + [7]

    def fmt(values: list[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True))

    lines = [fmt(header)]
    for row in rows:
        cells = row["cells"]
        lines.append(
            fmt(
                [
                    row["task_id"],
                    *(
                        _cell_display(target, agent, cells.get(f"{target}:{agent}", "-"))
                        for target in targets
                        for agent in agents
                    ),
                    row["grading"],
                ]
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_task_dirs(values: list[str], root: Path) -> list[Path]:
    resolved: list[Path] = []
    for value in values:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved.append(candidate)
    return resolved


def interop_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    command = getattr(args, "interop_command", None)
    if command == "export-harbor":
        try:
            task_dirs = _resolve_task_dirs([args.task_dir], root)
            if args.to == "inspect":
                summary = export_inspect(task_dirs[0], args.out)
            elif args.to == "karotte":
                summary = export_karotte(task_dirs[0], args.out)
            else:
                raise ValueError(f"unknown export target {args.to!r}")
        except (OSError, ValueError, FileNotFoundError) as exc:
            print(f"interop export-harbor: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"exported {summary['task_id']} -> {summary['target']}: {summary['out_dir']}")
            for name in summary["files"]:
                print(f"  {name}")
            extra = summary.get("flags", summary.get("lossy_notes", []))
            if extra:
                print("lossy mappings (see MAPPING.md):")
                for item in extra:
                    print(f"  - {item if isinstance(item, str) else item}")
        return 0
    if command == "matrix":
        task_dirs = _resolve_task_dirs(args.task_dirs, root)
        try:
            attacks = parse_attack_selection(args.attacks)
        except ValueError as exc:
            print(f"interop matrix: {exc}", file=sys.stderr)
            return 2
        targets = list(args.targets)
        agents = list(args.agents)
        work = Path(tempfile.mkdtemp(prefix="interop-matrix-"))
        rows = [
            matrix_task_row(
                task_dir,
                targets=tuple(targets),
                agents=tuple(agents),
                attacks=attacks,
                workdir=work / f"task-{index}",
                timeout_seconds=args.timeout,
            )
            for index, task_dir in enumerate(task_dirs)
        ]
        versions: dict[str, Any] = {}
        for row in rows:
            for target, version in row["versions"].items():
                versions.setdefault(target, version)
        envelope = {
            "rows": rows,
            "targets": targets,
            "agents": agents,
            "attacks": list(attacks),
            "versions": versions,
            "workdir": str(work),
            "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if args.json:
            print(json.dumps(envelope, indent=2, sort_keys=True))
        else:
            print(render_grading_matrix(rows, targets=targets, agents=agents))
            for target, version in versions.items():
                print(f"{target} version: {version}")
            print(f"workdir: {work}")
            for row in rows:
                for problem in row["problems"]:
                    print(f"  {row['task_id']}: grading broken -- {problem}")
                for key, detail in row["details"].items():
                    reason = detail.get("reason") if isinstance(detail, dict) else None
                    if reason and detail.get("verdict") not in ("pass", "fail"):
                        print(f"  {row['task_id']} {key}: {reason}")
        broken = sum(1 for r in rows if r["grading"] == "broken")
        if not broken and all(r["grading"] == "n/a" for r in rows):
            print(
                "note: no row had a comparable control cell "
                "(all skipped/error); exit 0 is not agreement"
            )
        return 1 if broken else 0
    print("interop: unknown command", file=sys.stderr)
    return 2


def build_interop_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "interop",
        help="Harbor <-> Inspect AI <-> Karotte <-> verifiers matrix ($0, model-free)",
        description=__doc__.split("\n\n")[0] if __doc__ else "Harbor interop",
    )
    sub = parser.add_subparsers(dest="interop_command", required=True)
    p = sub.add_parser("export-harbor", help="Convert a Harbor task dir to inspect|karotte output")
    p.add_argument("task_dir", help="Harbor task directory (e.g. library/tasks/<name>)")
    p.add_argument("--to", required=True, choices=("inspect", "karotte"))
    p.add_argument("--out", type=Path, required=True, help="Empty output directory")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=interop_command)
    p = sub.add_parser("matrix", help="Grading matrix across wired targets and agents")
    p.add_argument("task_dirs", nargs="+", help="Harbor task directories")
    p.add_argument(
        "--targets",
        nargs="+",
        default=["harbor", "inspect", "karotte", "verifiers"],
        choices=("harbor", "inspect", "karotte", "verifiers"),
    )
    p.add_argument(
        "--agents",
        nargs="+",
        default=["oracle", "nop", "cheat"],
        choices=("oracle", "nop", "cheat"),
    )
    p.add_argument(
        "--attacks",
        default=None,
        help="comma-separated cheat attack subset (cheat cells only); default: full ladder",
    )
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=interop_command)
