"""HAR-204: Harbor <-> Inspect AI <-> Karotte interop (local, $0 only).

Three commands, all model-free and spend-free:

- ``export-harbor`` converts a Harbor task directory into an Inspect AI task
  (``--to inspect``) or a Karotte environment scaffold (``--to karotte``).
  The inspect path never imports ``inspect_harbor``; the karotte path flags
  every lossy or impossible mapping in a generated ``MAPPING.md`` instead of
  silently dropping it.
- ``run-inspect`` runs a local Harbor task dir under Inspect AI through the
  repo-pinned ``inspect-harbor`` generic interface (``harbor(path=...)`` task
  + oracle solver + local docker sandbox) and prints that verdict next to the
  Harbor-native oracle verdict for the same task.
- ``parity`` runs oracle/nop on each locally executable target and prints the
  verdict-equality matrix, including the karotte-validate column.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def load_harbor_task(task_dir: str | Path) -> HarborTask:
    """Read a Harbor task directory; raise a clear error when it is not one."""
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
    if not solve_script.is_file():
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
) -> dict[str, Any]:
    """Run the Harbor-native oracle/nop control locally (docker, $0).

    Goes through the repo's own guarded ``Executor.execute_direct`` path, so
    the verdict is the same one a native lab control would record. Returns a
    verdict dict; infrastructure failures are ``error``, never ``fail``.
    """
    from evallab.execution_contracts import RunRequest
    from evallab.queue import Executor
    from evallab.results import load_job

    if agent not in ("oracle", "nop"):
        raise ValueError(f"refusing non-control agent {agent!r} (oracle/nop only, $0)")
    root = Path(repo_root)
    task = load_harbor_task(task_dir)
    harbor_rev = harbor_revision()
    admitted, admission_detail = docker_admission()
    if not admitted:
        return {
            "target": "harbor",
            "agent": agent,
            "task_id": task.task_id,
            "verdict": "skip",
            "reward": None,
            "reason": admission_detail,
            "job_dir": None,
            "harbor_rev": harbor_rev,
        }
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


SKIP_NO_DAEMON = "shared daemon not admitted"


def docker_admission() -> tuple[bool, str]:
    """Attempt shared-daemon admission via the repo's own resource gate.

    Read-only: it never touches other owners' containers. Returns
    ``(True, detail)`` when a live-Docker run may proceed, else
    ``(False, reason)``; live paths report ``shared daemon not admitted``
    and skip instead of failing.
    """
    try:
        from evallab.campaign_execution import docker_available_resources
    except ImportError as exc:
        return False, f"{SKIP_NO_DAEMON}: admission helper unavailable ({exc})"
    try:
        cpus, memory_mb = docker_available_resources()
    except Exception as exc:
        return False, f"{SKIP_NO_DAEMON}: {type(exc).__name__}: {exc}"
    return True, f"admitted with {cpus:.1f} CPUs / {memory_mb} MiB available"


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
# run-inspect: Harbor task dir under Inspect AI (pinned inspect-harbor)
# ---------------------------------------------------------------------------
INSPECT_DRIVER = '''"""Run one local Harbor task under Inspect AI (generated driver, $0)."""
import json
import sys
from pathlib import Path

TASK_DIR = Path(@@TASK_DIR_REPR@@)
TIMEOUT = @@TIMEOUT_REPR@@


def main() -> int:
    import inspect_ai
    import inspect_harbor
    from inspect_ai import eval as inspect_eval

    try:
        oracle_solver = inspect_harbor.oracle
    except AttributeError:
        from inspect_harbor._harbor.solver import oracle as oracle_solver
    task_obj = inspect_harbor.harbor(path=str(TASK_DIR), sandbox_env_name="docker")
    logs = inspect_eval(
        tasks=[task_obj],
        solver=[oracle_solver()],
        model="mockllm/model",
        limit=1,
        log_format="json",
    )
    if not logs or not logs[0].samples:
        print(json.dumps({"verdict": "error", "reason": "inspect produced no samples"}))
        return 0
    sample = logs[0].samples[0]
    if sample.error:
        print(json.dumps({"verdict": "error", "reason": str(sample.error)[:500]}))
        return 0
    values = [s.value for s in (sample.scores or {}).values()]
    reward = None
    for value in values:
        if isinstance(value, bool):
            reward = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            reward = float(value)
        elif value == "C":
            reward = 1.0
        elif value == "I":
            reward = 0.0
    if reward is None:
        print(json.dumps({"verdict": "error", "reason": f"unreadable score values: {values!r}"[:300]}))
        return 0
    print(json.dumps({
        "verdict": "pass" if reward >= 1.0 else "fail",
        "reward": reward,
        "inspect_ai": inspect_ai.__version__,
        "inspect_harbor": getattr(inspect_harbor, "__version__", "unknown"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def run_inspect_oracle(
    task_dir: str | Path,
    *,
    timeout_seconds: int = 1800,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Run a Harbor task dir under Inspect AI with the oracle solver ($0).

    Uses the repo-pinned inspect-harbor generic interface
    (``harbor(path=...)`` + oracle solver + local docker sandbox) inside a
    pinned ``uv run --with`` subprocess, so the lab venv never needs the
    inspect stack. Admission-gated: skips with ``shared daemon not admitted``
    when the shared Docker daemon refuses.
    """
    task = load_harbor_task(task_dir)
    admitted, admission_detail = docker_admission()
    if not admitted:
        return {
            "target": "inspect",
            "agent": "oracle",
            "task_id": task.task_id,
            "verdict": "skip",
            "reward": None,
            "reason": admission_detail,
            "inspect_ai": INSPECT_AI_PIN,
            "inspect_harbor": INSPECT_HARBOR_PIN,
        }
    work = (
        Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="interop-inspect-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    driver = work / "inspect_driver.py"
    driver.write_text(
        INSPECT_DRIVER.replace("@@TASK_DIR_REPR@@", repr(str(task.task_dir))).replace(
            "@@TIMEOUT_REPR@@", repr(timeout_seconds)
        ),
        encoding="utf-8",
    )
    uv = shutil.which("uv")
    if uv is None:
        return {
            "target": "inspect",
            "agent": "oracle",
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": "uv binary not found; cannot provision the pinned inspect stack",
            "inspect_ai": INSPECT_AI_PIN,
            "inspect_harbor": INSPECT_HARBOR_PIN,
        }
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
            timeout=timeout_seconds + 300,
            cwd=str(work),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "target": "inspect",
            "agent": "oracle",
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": f"inspect run timed out after {exc.timeout}s",
            "inspect_ai": INSPECT_AI_PIN,
            "inspect_harbor": INSPECT_HARBOR_PIN,
        }
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
        return {
            "target": "inspect",
            "agent": "oracle",
            "task_id": task.task_id,
            "verdict": "error",
            "reward": None,
            "reason": f"inspect driver produced no verdict JSON (exit {completed.returncode}): {detail}",
            "inspect_ai": INSPECT_AI_PIN,
            "inspect_harbor": INSPECT_HARBOR_PIN,
        }
    return {
        "target": "inspect",
        "agent": "oracle",
        "task_id": task.task_id,
        "verdict": payload.get("verdict", "error"),
        "reward": payload.get("reward"),
        "reason": payload.get("reason"),
        "inspect_ai": payload.get("inspect_ai", INSPECT_AI_PIN),
        "inspect_harbor": payload.get("inspect_harbor", INSPECT_HARBOR_PIN),
    }


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
# parity: verdict-equality matrix across locally executable targets
# ---------------------------------------------------------------------------
COMPARABLE_VERDICTS = frozenset({"pass", "fail"})


def matrix_equal(cells: dict[str, str]) -> bool | None:
    """Whether the comparable (pass/fail) cell verdicts agree.

    ``None`` when fewer than two cells are comparable (skips/errors carry no
    verdict signal); otherwise True iff every comparable verdict is identical.
    """
    comparable = sorted(v for v in cells.values() if v in COMPARABLE_VERDICTS)
    if len(comparable) < 2:
        return None
    return len(set(comparable)) == 1


def parity_row(
    task_dir: str | Path,
    *,
    repo_root: str | Path,
    targets: tuple[str, ...] = ("harbor", "inspect", "karotte-validate"),
    jobs_dir: str | Path | None = None,
    timeout_seconds: int | None = None,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Run oracle/nop on each locally executable target for one task dir."""
    task = load_harbor_task(task_dir)
    harbor_rev = harbor_revision()
    cells: dict[str, str] = {}
    details: dict[str, Any] = {}
    work = (
        Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="interop-parity-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    if "harbor" in targets:
        for agent in ("oracle", "nop"):
            result = run_harbor_control(
                task.task_dir,
                agent,
                repo_root=repo_root,
                jobs_dir=jobs_dir,
                timeout_seconds=timeout_seconds,
            )
            cells[f"harbor:{agent}"] = result["verdict"]
            details[f"harbor:{agent}"] = result
    if "inspect" in targets:
        result = run_inspect_oracle(task.task_dir, workdir=work / "inspect")
        cells["inspect:oracle"] = result["verdict"]
        details["inspect:oracle"] = result
        details["inspect:nop"] = {
            "target": "inspect",
            "agent": "nop",
            "task_id": task.task_id,
            "verdict": "n/a",
            "reason": "inspect_harbor ships no nop solver; nop signal comes from harbor:nop",
        }
    if "karotte-validate" in targets:
        scaffold = work / "karotte-scaffold"
        try:
            export_karotte(task.task_dir, scaffold)
            result = validate_karotte(scaffold / "environment")
        except (OSError, ValueError) as exc:
            result = {
                "target": "karotte-validate",
                "verdict": "error",
                "reason": f"scaffold export failed: {exc}",
            }
        cells["karotte-validate"] = result["verdict"]
        details["karotte-validate"] = result
    equal = matrix_equal(cells)
    return {
        "task_id": task.task_id,
        "task_dir": str(task.task_dir),
        "cells": cells,
        "equal": equal,
        "harbor_rev": harbor_rev,
        "details": details,
    }


def render_matrix(rows: list[dict[str, Any]]) -> str:
    """Render the parity equality matrix as aligned text."""
    columns = ["harbor:oracle", "harbor:nop", "inspect:oracle", "karotte-validate"]
    header = ["task", *columns, "equal"]
    widths = [max(len(r["task_id"]) for r in rows + [{"task_id": "task"}])]
    widths += [max(len(c), 6) for c in columns] + [5]

    def fmt(values: list[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True))

    lines = [fmt(header)]
    for row in rows:
        cells = row["cells"]
        equal = row["equal"]
        lines.append(
            fmt(
                [
                    row["task_id"],
                    *(cells.get(c, "-") for c in columns),
                    {True: "True", False: "False", None: "n/a"}[equal],
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
    if command == "run-inspect":
        task_dirs = _resolve_task_dirs([args.task_dir], root)
        task = load_harbor_task(task_dirs[0])
        inspected = run_inspect_oracle(task_dirs[0], timeout_seconds=args.timeout)
        native = run_harbor_control(
            task_dirs[0],
            "oracle",
            repo_root=root,
            jobs_dir=args.jobs_dir,
            timeout_seconds=args.timeout,
        )
        report = {
            "task_id": task.task_id,
            "inspect_oracle": inspected,
            "harbor_oracle": native,
            "agree": (
                inspected["verdict"] == native["verdict"]
                if inspected["verdict"] in COMPARABLE_VERDICTS
                and native["verdict"] in COMPARABLE_VERDICTS
                else None
            ),
            "harbor_rev": native.get("harbor_rev", harbor_revision()),
            "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"task: {task.task_id}")
            print(
                f"  inspect oracle: {inspected['verdict']}"
                + (
                    f" (reward {inspected.get('reward')})"
                    if inspected.get("reward") is not None
                    else ""
                )
                + (f" -- {inspected.get('reason')}" if inspected.get("reason") else "")
            )
            print(
                f"  harbor  oracle: {native['verdict']}"
                + (f" (reward {native.get('reward')})" if native.get("reward") is not None else "")
                + (f" -- {native.get('reason')}" if native.get("reason") else "")
            )
            print(f"  agree: {report['agree']}")
            print(f"  harbor_rev: {report['harbor_rev']}")
        comparable = report["agree"] is not None
        return 0 if (report["agree"] or not comparable) else 1
    if command == "parity":
        task_dirs = _resolve_task_dirs(args.task_dirs, root)
        rows = [
            parity_row(
                task_dir,
                repo_root=root,
                targets=tuple(args.targets),
                jobs_dir=args.jobs_dir,
                timeout_seconds=args.timeout,
            )
            for task_dir in task_dirs
        ]
        envelope = {
            "rows": rows,
            "harbor_rev": harbor_revision(),
            "targets": list(args.targets),
            "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if args.json:
            print(json.dumps(envelope, indent=2, sort_keys=True))
        else:
            print(render_matrix(rows))
            print(f"harbor_rev: {envelope['harbor_rev']}")
            for row in rows:
                for key in ("harbor:oracle", "harbor:nop", "inspect:oracle", "karotte-validate"):
                    detail = row["details"].get(key, {})
                    reason = detail.get("reason") if isinstance(detail, dict) else None
                    if reason and detail.get("verdict") not in COMPARABLE_VERDICTS:
                        print(f"  {row['task_id']} {key}: {reason}")
        disagreements = sum(1 for r in rows if r["equal"] is False)
        if not disagreements and all(r["equal"] is None for r in rows):
            print(
                "note: no row had two comparable verdicts "
                "(all skipped/error); exit 0 is not agreement"
            )
        return 1 if disagreements else 0
    print("interop: unknown command", file=sys.stderr)
    return 2


def build_interop_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "interop",
        help="Harbor <-> Inspect AI <-> Karotte converters and parity ($0, model-free)",
        description=__doc__.split("\n\n")[0] if __doc__ else "Harbor interop",
    )
    sub = parser.add_subparsers(dest="interop_command", required=True)
    p = sub.add_parser("export-harbor", help="Convert a Harbor task dir to inspect|karotte output")
    p.add_argument("task_dir", help="Harbor task directory (e.g. library/tasks/<name>)")
    p.add_argument("--to", required=True, choices=("inspect", "karotte"))
    p.add_argument("--out", type=Path, required=True, help="Empty output directory")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=interop_command)
    p = sub.add_parser(
        "run-inspect",
        help="Run a Harbor task dir under Inspect AI (oracle) next to the native verdict",
    )
    p.add_argument("task_dir", help="Harbor task directory")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--jobs-dir", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=interop_command)
    p = sub.add_parser("parity", help="Verdict-equality matrix across locally executable targets")
    p.add_argument("task_dirs", nargs="+", help="Harbor task directories")
    p.add_argument(
        "--targets",
        nargs="+",
        default=["harbor", "inspect", "karotte-validate"],
        choices=("harbor", "inspect", "karotte-validate"),
    )
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--jobs-dir", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=interop_command)
