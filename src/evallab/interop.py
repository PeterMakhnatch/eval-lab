"""HAR-204: Harbor <-> Inspect AI <-> Karotte <-> verifiers interop (local, $0 only).

Model-free, spend-free bridges:

- ``export-harbor`` converts a Harbor task directory into an Inspect AI task
  (``--to inspect``) or a Karotte environment scaffold (``--to karotte``).
  The inspect path never imports ``inspect_harbor``; the karotte path flags
  every lossy or impossible mapping in a generated ``MAPPING.md`` instead of
  silently dropping it.
- ``matrix`` runs scripted oracle/nop/cheat agents against each wired target
  (``MATRIX_TARGETS`` registry: harbor + inspect here, karotte/verifiers wired
  at integration) and prints the grading table plus a JSON envelope.

The scripted agents are platform-neutral plans (``scripted_agent_plan``):
oracle stages ``solution/`` and runs ``solve.sh``; nop does nothing; cheat
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


#: Absolute container path the cheat ladder is staged at for scripted agents.
CHEAT_LADDER_CONTAINER_PATH = "/tmp/cheat_ladder.py"

#: Container workdir the inspect runner materializes (mirrors the repo mount
#: Harbor agents see natively); the plan command runs with this as its cwd.
INSPECT_TASK_WORKDIR = "/taskwork"

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

    Oracle stages ``solution/`` and runs ``solve.sh`` (and needs
    ``solution/solve.sh`` present); nop stages nothing and runs nothing
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
        return ScriptedPlan(files={}, command="bash solution/solve.sh")
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
    """
    flags: list[dict[str, str]] = []
    env_dir = task.task_dir / "environment"
    if any((env_dir / name).is_file() for name in ("docker-compose.yaml", "docker-compose.yml")):
        flags.append(
            {
                "code": "multi-service-compose",
                "detail": (
                    "task ships its own Compose file; the scaffold maps only the main "
                    "environment/Dockerfile to a single Containerfile. Extra services "
                    "must be re-expressed as karotte data mounts or sidecars manually."
                ),
            }
        )
    test_sh = task.task_dir / task.tests_dir / "test.sh"
    try:
        test_text = test_sh.read_text(encoding="utf-8", errors="replace")
    except OSError:
        test_text = ""
    if re.search(r"(^|\s)(apt-get|apt|sudo|yum|apk)(\s|$)", test_text):
        flags.append(
            {
                "code": "verifier-as-root",
                "detail": (
                    "tests/test.sh installs system packages (apt/sudo), i.e. the Harbor "
                    "verifier assumes root. Karotte judges may run confined/non-root; "
                    "fold these deps into the Containerfile or grant the judge privilege."
                ),
            }
        )
    if task.network_mode not in (None, "none"):
        flags.append(
            {
                "code": "network-policy",
                "detail": (
                    f"task.toml network_mode={task.network_mode!r} has no karotte "
                    "equivalent; the scaffold runs under default karotte confinement. "
                    "Re-check any verifier that depends on public egress."
                ),
            }
        )
    if task.resources:
        flags.append(
            {
                "code": "resource-rounding",
                "detail": (
                    f"task resources {task.resources} do not map 1:1 onto karotte "
                    "hardware plugins (named buckets); pick the nearest bucket and "
                    "record the choice in the environment manifest."
                ),
            }
        )
    if task.environment_mode == "separate":
        flags.append(
            {
                "code": "separate-verifier-image",
                "detail": (
                    "verifier.environment_mode=separate: Harbor scores in a dedicated "
                    "verifier image (tests/Dockerfile). The scaffold judges inside one "
                    "container; merge verifier-only system deps into the Containerfile."
                ),
            }
        )
    if not task.artifacts:
        flags.append(
            {
                "code": "no-submission-paths",
                "detail": (
                    "task.toml declares no artifacts, so Step.submission_paths cannot be "
                    "derived; the scaffold scores the whole workdir. Declare the files "
                    "the student must hand in."
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
                    "mapping; re-provide them as karotte tools or drop with justification."
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
                    "scaffold does not execute solve.sh, so these are documentation only."
                ),
            }
        )
    return flags


def _class_name(task_id: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", task_id)
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return (name or "Harbor") + "Task"


KAROTTE_TASK_TEMPLATE = '''"""Karotte environment scaffold exported from Harbor task @@TASK_ID@@.

Generated by ``evallab interop export-harbor --to karotte``. See MAPPING.md for
every lossy mapping. Layout contract:

- ``student_data``: paths the student may write (Harbor artifacts +
  task workdirs, e.g. @@ARTIFACTS@@).
- ``root_data``: verifier-only material (tests/, solution/, judge_entry.py);
  the student never sees these.
"""
from __future__ import annotations

import sys
from pathlib import Path

from karotte import Step, Task
from karotte.judges.executable_judge import ExecutableJudge
from karotte.judges.judge import Judge

HERE = Path(__file__).resolve().parent
INSTRUCTIONS = (HERE / "instruction.md").read_text(encoding="utf-8")

# Student-writable submission paths (Harbor artifacts, absolute as Step requires).
SUBMISSION_PATHS = @@SUBMISSION_PATHS@@


class @@STEP_CLASS@@(Step):
    """One step: follow the Harbor instruction, hand in the artifacts."""

    @property
    def instructions(self) -> str:
        return INSTRUCTIONS

    @property
    def judge(self) -> Judge:
        # ExecutableJudge runs the Harbor verifier (tests/test.sh) via
        # judge_entry.py, which converts reward.txt/reward.json to the
        # {"score": float, "metadata": dict} karotte expects.
        return ExecutableJudge(
            [sys.executable, str(HERE / "judge_entry.py"), "judge_output.json"],
            continue_threshold=1.0,
        )

    @property
    def submission_paths(self) -> tuple[Path, ...] | None:
        paths = tuple(Path(p) for p in SUBMISSION_PATHS)
        return paths or None


class @@TASK_CLASS@@(Task):
    """Harbor task @@TASK_ID@@ as a single-step karotte task."""

    id = "@@TASK_SLUG@@"

    @property
    def system_prompt(self) -> str | None:
        return None

    @property
    def steps(self) -> list[Step]:
        return [@@STEP_CLASS@@(self.config)]

    @property
    def tools(self) -> list[str]:
        # Default tool surface; confirm against the karotte tool plugin in use.
        # Flagged in MAPPING.md when the Harbor task needs more (see mcp-servers).
        return ["bash", "python"]
'''

KAROTTE_JUDGE_TEMPLATE = '''"""Karotte judge entry: run the Harbor verifier, emit karotte scoring JSON.

Usage: judge_entry.py <output.json>  (ExecutableJudge rewrites the final arg to
a temp path.) Runs tests/test.sh from the environment root, converts the
reward.txt/reward.json it writes to {"score": float, "metadata": dict}.
A missing reward file scores 0 with metadata {"error": ...}, never silently.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DATA = HERE  # verifier-only: tests/, solution/, this script


def read_reward(reward_dir: Path) -> tuple[float | None, str | None]:
    txt = reward_dir / "reward.txt"
    js = reward_dir / "reward.json"
    if txt.is_file():
        try:
            return float(txt.read_text(encoding="utf-8").strip()), None
        except ValueError:
            return None, "reward.txt is not a number"
    if js.is_file():
        try:
            payload = json.loads(js.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return None, f"reward.json is not valid JSON: {exc}"
        for key in ("score", "reward"):
            if isinstance(payload, dict) and isinstance(payload.get(key), (int, float)):
                return float(payload[key]), None
        return None, "reward.json has no numeric score/reward key"
    return None, "neither reward.txt nor reward.json exists"


def main() -> int:
    output = Path(sys.argv[-1])
    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["bash", str(ROOT_DATA / "tests" / "test.sh")],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(ROOT_DATA),
    )
    reward, error = read_reward(logs)
    if error is not None:
        payload = {"score": 0.0, "metadata": {"error": error, "exit": completed.returncode}}
    else:
        assert reward is not None
        payload = {
            "score": reward,
            "metadata": {"exit": completed.returncode, "verdict": "pass" if reward >= 1.0 else "fail"},
        }
    output.write_text(json.dumps(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

KAROTTE_FAKE_MODEL = '''"""Nop-behavior fake model for local karotte runs (no inference, $0).

``karotte.fake_model.setup_fake_model`` imports ``get_messages`` from
``environment.fake_model``; returning no messages makes the student a no-op,
so a fake-model run must FAIL on a sound task (the nop control analogue).
"""
from __future__ import annotations

from typing import Any


def get_messages(config: Any) -> list[Any]:
    return []
'''


def export_karotte(task_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Export a Harbor task dir to a karotte environment scaffold dir."""
    task = load_harbor_task(task_dir)
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"refusing to write into non-empty directory: {out}")
    env = out / "environment"
    (env / "tests").mkdir(parents=True, exist_ok=True)
    (env / "solution").mkdir(parents=True, exist_ok=True)
    (env / "instruction.md").write_text(task.instruction, encoding="utf-8")
    for child in sorted((task.task_dir / task.tests_dir).iterdir()):
        dest = env / "tests" / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    solution_src = task.task_dir / "solution"
    for child in sorted(solution_src.iterdir()):
        dest = env / "solution" / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    dockerfile_src = task.task_dir / "environment" / "Dockerfile"
    if dockerfile_src.is_file():
        dockerfile_body = dockerfile_src.read_text(encoding="utf-8")
        dockerfile_note = "copied verbatim; verifier-only deps flagged in MAPPING.md"
    else:
        dockerfile_body = "FROM python:3.12-slim-bookworm\n"
        dockerfile_note = "missing: fell back to python:3.12-slim"
    (out / "Containerfile").write_text(
        f"# Karotte scaffold for Harbor task {task.task_id}.\n"
        f"# Derived from environment/Dockerfile ({dockerfile_note}).\n" + dockerfile_body,
        encoding="utf-8",
    )
    submission = [a if a.startswith("/") else f"/app/{a.lstrip('/')}" for a in task.artifacts]
    task_class = _class_name(task.task_id)
    step_class = task_class.removesuffix("Task") + "Step"
    slug = re.sub(r"[^a-z0-9]+", "-", task.task_id.lower()).strip("-")[:200] or "harbor-task"
    artifacts_doc = ", ".join(submission) if submission else "(none declared)"
    (env / "task.py").write_text(
        KAROTTE_TASK_TEMPLATE.replace("@@TASK_ID@@", task.task_id)
        .replace("@@TASK_CLASS@@", task_class)
        .replace("@@STEP_CLASS@@", step_class)
        .replace("@@TASK_SLUG@@", slug)
        .replace("@@ARTIFACTS@@", artifacts_doc)
        .replace("@@SUBMISSION_PATHS@@", repr(submission)),
        encoding="utf-8",
    )
    (env / "judge_entry.py").write_text(KAROTTE_JUDGE_TEMPLATE, encoding="utf-8")
    (env / "fake_model.py").write_text(KAROTTE_FAKE_MODEL, encoding="utf-8")
    flags = karotte_flags(task)
    mapping = (
        f"# Karotte scaffold mapping: {task.task_id}\n\n"
        f"Source: `{task.task_dir}`\n\n"
        "| Harbor | Karotte scaffold |\n"
        "| --- | --- |\n"
        "| instruction.md | Step.instructions (environment/task.py) |\n"
        "| task artifacts | Step.submission_paths (student_data) |\n"
        "| tests/test.sh | ExecutableJudge via environment/judge_entry.py |\n"
        "| reward.txt / reward.json | judge score float; missing file = score 0 + error metadata |\n"
        "| solution/solve.sh | root_data reference only (scaffold never executes it) |\n"
        "| environment/Dockerfile | Containerfile (single image) |\n"
        "| task.toml [task] + [metadata] | Task.id + module docstring |\n"
        "\n## student_data / root_data split\n\n"
        f"- student_data (student-writable): {artifacts_doc}\n"
        "- root_data (verifier-only, student never sees): tests/, solution/, judge_entry.py\n"
        "\n## Lossy or impossible mappings (never silently dropped)\n\n"
        + (
            "".join(f"- **{f['code']}**: {f['detail']}\n" for f in flags)
            if flags
            else "None: the Harbor surface above maps directly.\n"
        )
    )
    (out / "MAPPING.md").write_text(mapping, encoding="utf-8")
    return {
        "target": "karotte",
        "task_id": task.task_id,
        "out_dir": str(out),
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
                    await sb.write_file(
                        manifest['workdir'] + '/' + rel, (TASK_DIR / rel).read_bytes()
                    )
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


def _collect_task_files(task: HarborTask) -> tuple[list[str], list[str]]:
    """Stageable solution/ + tests/ files (task-relative posix) plus skipped-large.

    Mirrors the repo mount Harbor agents see natively, so recon attacks observe
    the same surface. Files over ``MAX_STAGED_FILE_BYTES`` are skipped and
    reported; tasks that need more want a runner with a real mount.
    """
    collected: list[str] = []
    skipped: list[str] = []
    for base in ("solution", "tests"):
        root = task.task_dir / base
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
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
    task_files, skipped = _collect_task_files(task) if agent != "nop" else ([], [])
    manifest = {
        "agent": agent,
        "attacks": list(attacks),
        "command": plan.command,
        "files": blobs,
        "task_files": task_files,
        "skipped_large": skipped,
        "workdir": INSPECT_TASK_WORKDIR,
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
# karotte-validate: structural validation of an exported scaffold ($0)
# ---------------------------------------------------------------------------
KAROTTE_VALIDATE_SNIPPET = """
import importlib.util
import json
import sys
from pathlib import Path

ENV_DIR = Path(@@ENV_DIR_REPR@@)
sys.path.insert(0, str(ENV_DIR.parent))

spec = importlib.util.spec_from_file_location("interop_env_task", ENV_DIR / "task.py")
assert spec and spec.loader, "cannot load environment/task.py"
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from karotte import Step, Task
from karotte.judges.judge import Judge
from karotte.schemas.evaluation_run_config import EvaluationRunConfig

tasks = [
    obj for obj in vars(module).values()
    if isinstance(obj, type) and issubclass(obj, Task) and obj is not Task
]
assert tasks, "no Task subclass in environment/task.py"
config = EvaluationRunConfig(run_id="interop-validate", task_id=tasks[0].__name__, model="", model_api_key="")
report = {"task_classes": [c.__name__ for c in tasks], "steps": []}
for cls in tasks:
    inst = cls(config)
    assert isinstance(inst.system_prompt, (str, type(None))), "system_prompt must be str|None"
    steps = list(inst.steps)
    assert steps, "Task.steps is empty"
    for step in steps:
        assert isinstance(step, Step), "step is not a Step"
        assert isinstance(step.instructions, str) and step.instructions.strip(), "empty instructions"
        assert isinstance(step.judge, Judge), "judge is not a Judge"
        paths = step.submission_paths
        assert paths is None or all(p.is_absolute() for p in paths), "submission_paths must be absolute"
        report["steps"].append({
            "instructions_chars": len(step.instructions),
            "judge": type(step.judge).__name__,
            "submission_paths": [str(p) for p in paths] if paths else [],
        })
print(json.dumps({"verdict": "pass", "report": report}))
"""


def validate_karotte(env_dir: str | Path) -> dict[str, Any]:
    """Structurally validate an exported karotte scaffold (no containers, $0).

    Imports ``environment/task.py`` with karotte on the path, instantiates the
    Task with a sample run config, and checks steps/instructions/judge/
    submission_paths. This is validate-only: a real fake-model run needs the
    karotte env image built around the scaffold (documented follow-up).
    """
    env = Path(env_dir)
    task_file = env / "task.py"
    if not task_file.is_file():
        return {
            "target": "karotte-validate",
            "verdict": "error",
            "reason": f"{env}: no environment/task.py (export a karotte scaffold first)",
        }
    uv = shutil.which("uv")
    if uv is None:
        return {
            "target": "karotte-validate",
            "verdict": "error",
            "reason": "uv binary not found; cannot provision karotte for validation",
        }
    try:
        completed = subprocess.run(
            [
                uv,
                "run",
                "--no-project",
                "--with",
                f"karotte=={KAROTTE_PIN}",
                "python",
                "-c",
                KAROTTE_VALIDATE_SNIPPET.replace("@@ENV_DIR_REPR@@", repr(str(env))),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(env),
        )
    except subprocess.TimeoutExpired:
        return {
            "target": "karotte-validate",
            "verdict": "error",
            "reason": "karotte validation timed out",
        }
    for line in (completed.stdout or "").strip().splitlines()[::-1]:
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("verdict") == "pass":
            return {
                "target": "karotte-validate",
                "verdict": "pass",
                "reward": None,
                "reason": None,
                "report": candidate.get("report"),
                "karotte": KAROTTE_PIN,
                "note": (
                    "validate-only: a real fake-model run needs the karotte env "
                    "image built around this scaffold (see research/interop/README.md)"
                ),
            }
    detail = ((completed.stdout or "") + (completed.stderr or ""))[-800:]
    return {
        "target": "karotte-validate",
        "verdict": "fail",
        "reward": None,
        "reason": f"scaffold failed structural validation (exit {completed.returncode}): {detail}",
    }


# ---------------------------------------------------------------------------
# matrix: per-task grading table across wired targets
# ---------------------------------------------------------------------------
MATRIX_TARGETS: dict[str, Callable[..., dict[str, Any]]] = {
    "harbor": run_harbor_cell,
    "inspect": run_inspect_cell,
}
"""Target name -> ``run_cell`` runner. Karotte/verifiers runners wire in here."""

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
    # Row identity needs no reference solution; per-agent strictness lives in
    # the cells (oracle errors there on solution-less tasks, nop/cheat run).
    task = load_harbor_task(task_dir, require_solution=False)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    cells: dict[str, str] = {}
    details: dict[str, Any] = {}
    for target in targets:
        runner = MATRIX_TARGETS.get(target)
        for agent in agents:
            key = f"{target}:{agent}"
            if runner is None:
                cell = {
                    "target": target,
                    "agent": agent,
                    "attacks": list(attacks),
                    "verdict": "skipped",
                    "reward": None,
                    "reason": f"target {target!r} has no runner in MATRIX_TARGETS",
                    "platform_version": None,
                    "evidence": None,
                }
            else:
                try:
                    cell = runner(
                        task.task_dir,
                        agent,
                        tuple(attacks),
                        workdir=work / f"{target}-{agent}",
                        timeout_seconds=timeout_seconds,
                    )
                except Exception as exc:
                    cell = {
                        "target": target,
                        "agent": agent,
                        "attacks": list(attacks),
                        "verdict": "error",
                        "reward": None,
                        "reason": f"cell runner raised {type(exc).__name__}: {exc}",
                        "platform_version": None,
                        "evidence": None,
                    }
            cells[key] = cell.get("verdict", "error")
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
