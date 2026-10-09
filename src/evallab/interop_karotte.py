"""Run Harbor tasks under karotte's own grading custody (local Docker, $0).

For one Harbor task dir and one scripted agent (oracle/nop/cheat), this module
assembles a real karotte environment (via karotte's own ``create_env``
template), builds its image, and executes a fake-model run
(``use_fake_model``: scripted ``get_messages``, no inference, no spend)
through ``karotte run --runtime docker``. Grading goes through karotte's own
``collect_submission`` + ``ExecutableJudge`` path: the step's
``pre_scoring_hook`` collects the declared submission paths into a root-only
staging dir (killing student processes and wiping the workdir first) and the
judge grades those copies with the Harbor verifier logic.

``run_cell`` is the cross-platform cell entry point (oracle/nop/cheat); it
returns ``pass``/``fail``/``skipped``/``error`` with the reward, a reason, the
karotte version, and an evidence path. Containers are karotte's own
``karotte_run_<run_id>`` (``--rm`` auto-removed); a watcher thread additionally
bounds each run container (task-declared cpus/memory_mb, else 2 CPUs / 2 GiB)
via ``docker update`` without ever touching containers it did not start.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.interop import (
    CHEAT_LADDER_CONTAINER_PATH,
    KAROTTE_PIN,
    HarborTask,
    ScriptedPlan,
    load_harbor_task,
    parse_reward_bytes,
    scripted_agent_plan,
    task_workdir,
    verdict_for_reward,
)

#: Image tag karotte's ``run`` reads (hardcoded upstream); content-keyed tags
#: below let cells skip rebuilds and retag this name.
KAROTTE_IMAGE = "karotte"
#: Cache tag prefix for built task images (``evallab-karotte-<digest>``).
IMAGE_CACHE_PREFIX = "evallab-karotte"
#: Cache tag prefix for built Harbor base images (``evallab-harbor-<digest>``).
HARBOR_IMAGE_PREFIX = "evallab-harbor"
#: Default bounds applied to each run container (cpus, memory).
RUN_CPUS = "2"
RUN_MEMORY = "2g"
#: Build budget floor per task image; the run phase uses ``timeout_seconds``.
BUILD_TIMEOUT_SECONDS = 1800
DEFAULT_RUN_TIMEOUT_SECONDS = 1200
#: Per-command timeout (seconds) for scripted bash tool calls.
BASH_TIMEOUT_S = 300
#: Timeout for the cheat-ladder tool call (large repos, emulated arch).
CHEAT_TIMEOUT_S = 1500
#: Files the cell runner never hashes into the image digest (per-cell inputs).
_UNHASHED_NAMES = {"fake_model.py"}
_UNHASHED_DIRS = {"out"}
#: Oracle solution tree, mounted (never baked) for oracle cells only.
SOLUTION_MOUNT = "/solution"


def _karotte_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Host env for karotte subprocesses: ``CI`` unset.

    karotte prefixes docker/buildx probes with ``sudo`` when ``CI`` is set,
    which fails on passwordless-sudo laptops even though the daemon is
    reachable; unsetting it recovers the normal local-docker path.
    """
    env = dict(os.environ)
    env.pop("CI", None)
    if extra:
        env.update(extra)
    return env


def docker_daemon_ok(timeout: int = 30) -> tuple[bool, str]:
    """Probe the shared Docker daemon (read-only ``docker info``)."""
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker binary not on PATH"
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker info failed: {type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return False, f"docker info failed: {(completed.stderr or '').strip()[-300:]}"
    return True, f"docker server {(completed.stdout or '').strip()}"


# ---------------------------------------------------------------------------
# Generic Harbor task spec (no per-task adapters)
# ---------------------------------------------------------------------------
# Agent scripts come from ``evallab.interop.scripted_agent_plan`` (contract
# #2): identical plans on every platform. This module only replays them.


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _artifact_sources(task: HarborTask) -> list[str]:
    """Absolute submission sources from task.toml artifacts (str or table)."""
    raw = task.config.get("artifacts")
    if not isinstance(raw, list):
        return []
    sources: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = str(entry.get("source", ""))
        else:
            continue
        if candidate.startswith("/"):
            sources.append(candidate)
    return sources


@dataclass(frozen=True)
class GenericSpec:
    """Everything karotte needs, derived from the Harbor task (no hand work)."""

    task_key: str
    karotte_id: str
    workdir: str
    base_kind: str
    base_ref: str
    submission_paths: tuple[str, ...]
    has_solution: bool
    grade_timeout: float
    cpus: str
    memory: str


def _karotte_slug(task_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:120] or "harbor-task"


def resolve_spec(task: HarborTask) -> GenericSpec:
    """Derive the runnable spec: workdir, base image, submission scope."""
    env_cfg = task.config.get("environment")
    env_cfg = env_cfg if isinstance(env_cfg, dict) else {}
    workdir = task_workdir(task)
    env_dir = task.task_dir / "environment"
    if any((env_dir / name).is_file() for name in ("docker-compose.yaml", "docker-compose.yml")):
        raise ValueError(
            f"{task.task_id}: multi-service compose has no single-image karotte mapping"
        )
    docker_image = env_cfg.get("docker_image")
    if isinstance(docker_image, str) and docker_image:
        base_kind, base_ref = "docker_image", docker_image
    elif (env_dir / "Dockerfile").is_file():
        base_kind, base_ref = "dockerfile", ""
    else:
        raise ValueError(
            f"{task.task_id}: no environment/Dockerfile or [environment] docker_image "
            "to build the student image from"
        )
    sources = _artifact_sources(task)
    if sources:
        submissions = tuple(sources)
    elif workdir == "/":
        raise ValueError(
            f"{task.task_id}: workdir / with no artifacts: refusing whole-filesystem submission"
        )
    else:
        submissions = (workdir,)
    verifier = task.config.get("verifier")
    verifier = verifier if isinstance(verifier, dict) else {}
    try:
        grade_timeout = float(verifier.get("timeout_sec") or 600)
    except (TypeError, ValueError):
        grade_timeout = 600.0
    grade_timeout = min(max(grade_timeout, 300.0), 2100.0)
    resources = task.resources
    raw_cpus = resources.get("cpus")
    cpus = (
        str(raw_cpus)
        if isinstance(raw_cpus, (int, float)) and not isinstance(raw_cpus, bool) and raw_cpus > 0
        else RUN_CPUS
    )
    raw_memory = resources.get("memory_mb")
    memory = (
        f"{int(raw_memory)}m"
        if isinstance(raw_memory, (int, float))
        and not isinstance(raw_memory, bool)
        and raw_memory > 0
        else RUN_MEMORY
    )
    return GenericSpec(
        task_key=task.task_dir.name,
        karotte_id=_karotte_slug(task.task_id),
        workdir=workdir,
        base_kind=base_kind,
        base_ref=base_ref,
        submission_paths=submissions,
        has_solution=(task.task_dir / "solution" / "solve.sh").is_file(),
        grade_timeout=grade_timeout,
        cpus=cpus,
        memory=memory,
    )


def _dir_digest(path: Path) -> str:
    """Content hash of a build-context directory (sorted relpath + bytes)."""
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file() or child.is_symlink():
            continue
        rel = child.relative_to(path).as_posix()
        digest.update(rel.encode() + b"\0" + child.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def ensure_harbor_base(
    task: HarborTask, spec: GenericSpec, *, build_timeout: int
) -> dict[str, Any]:
    """Provide the Harbor student image: pull a pinned ref or build the Dockerfile.

    Build context is the task's ``environment/`` dir (Harbor semantics);
    Dockerfile builds are content-keyed (``evallab-harbor-<digest>``).
    """
    if spec.base_kind == "docker_image":
        ref = spec.base_ref
        inspected = subprocess.run(
            ["docker", "image", "inspect", ref],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=_karotte_env(),
        )
        if inspected.returncode != 0:
            pulled = subprocess.run(
                ["docker", "pull", ref],
                check=False,
                capture_output=True,
                text=True,
                timeout=build_timeout,
                env=_karotte_env(),
            )
            if pulled.returncode != 0:
                raise RuntimeError(f"docker pull {ref} failed: {(pulled.stderr or '')[-2000:]}")
            detail = "pulled"
        else:
            detail = "cached"
        return {"tag": ref, "kind": "docker_image", "detail": detail}
    env_dir = task.task_dir / "environment"
    tag = f"{HARBOR_IMAGE_PREFIX}-{_dir_digest(env_dir)}"
    inspected = subprocess.run(
        ["docker", "image", "inspect", tag],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=_karotte_env(),
    )
    if inspected.returncode == 0:
        return {"tag": tag, "kind": "dockerfile", "detail": "cached"}
    env_cfg = task.config.get("environment")
    env_cfg = env_cfg if isinstance(env_cfg, dict) else {}
    try:
        timeout = max(int(env_cfg.get("build_timeout_sec") or 600), 300)
    except (TypeError, ValueError):
        timeout = 600
    timeout = min(timeout, 3600)
    built = subprocess.run(
        ["docker", "build", "--file", str(env_dir / "Dockerfile"), "--tag", tag, str(env_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(task.task_dir),
        env=_karotte_env(),
    )
    if built.returncode != 0:
        detail = ((built.stdout or "") + (built.stderr or ""))[-3000:]
        raise RuntimeError(f"harbor base build failed: {detail}")
    return {"tag": tag, "kind": "dockerfile", "detail": "built"}


# ---------------------------------------------------------------------------
# Fake-model scripts: the scripted agent each cell replays
# ---------------------------------------------------------------------------


def _tool_call(call_id: str, command: str, timeout_s: int) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": command, "timeout_s": timeout_s}),
        },
    }


def plan_messages(
    commands: tuple[str, ...],
    *,
    command_timeout_s: int = BASH_TIMEOUT_S,
    done_text: str = "Done.",
) -> list[dict[str, Any]]:
    """Map scripted shell commands onto fake-model replay messages.

    Each command becomes one assistant message with a single ``bash`` tool
    call; a final text-only message ends the step (a step runs until a message
    carries no tool call). No commands (nop) yields one text-only message, so
    the step ends with zero tool calls and grading scores the untouched state.
    """
    messages = [
        {
            "role": "assistant",
            "content": f"Running scripted step command {i + 1}.",
            "tool_calls": [_tool_call(f"tool_call_{i}", command, command_timeout_s)],
        }
        for i, command in enumerate(commands)
    ]
    messages.append({"role": "assistant", "content": done_text, "tool_calls": []})
    return messages


def fake_model_source(plan: ScriptedPlan, workdir: str) -> str:
    """Render ``environment/fake_model.py`` for one cell from its plan.

    A plan command becomes one ``bash`` tool call run with cwd at the task
    workdir (files ride per-run mounts); a None command (nop) becomes one
    text-only message, so the step ends with zero tool calls.
    """
    if plan.command is None:
        messages = plan_messages((), done_text="No changes; grading the initial state.")
    else:
        timeout = CHEAT_TIMEOUT_S if CHEAT_LADDER_CONTAINER_PATH in plan.command else BASH_TIMEOUT_S
        wrapped = f"cd {shlex.quote(workdir)} && {plan.command}"
        messages = plan_messages((wrapped,), command_timeout_s=timeout)
    return (
        '"""Scripted $0 agent for one karotte cell (no inference)."""\n'
        "from __future__ import annotations\n"
        "\n"
        "from typing import Any\n"
        "\n"
        "from karotte.schemas import ChatCompletionMessageToolCall, Function, Message\n"
        "\n"
        f"_MESSAGES = {json.dumps(messages, indent=2)}\n"
        "\n"
        "\n"
        "def get_messages(config: Any) -> list[Message]:\n"
        "    return [\n"
        "        Message(\n"
        "            role=m['role'],\n"
        "            content=m.get('content'),\n"
        "            tool_calls=[\n"
        "                ChatCompletionMessageToolCall(\n"
        "                    id=c['id'],\n"
        "                    type=c['type'],\n"
        "                    function=Function(name=c['function']['name'],\n"
        "                                    arguments=c['function']['arguments']),\n"
        "                )\n"
        "                for c in m.get('tool_calls', [])\n"
        "            ] or None,\n"
        "        )\n"
        "        for m in _MESSAGES\n"
        "    ]\n"
    )


# ---------------------------------------------------------------------------
# Environment assembly (vendored template + task overlay + harness image)
# ---------------------------------------------------------------------------
# The `environment` support package below is vendored verbatim from karotte
# 3.0.59's default template (src/environment/__init__.py, paths.py,
# submissions.py, system_prompts.py; tasks/__init__.py is empty upstream).
# It is the minimal set the harness needs (get_tasks, collect_submission,
# system prompts, permission probes); refresh these on a pin bump. Vendoring
# keeps `export-harbor --to karotte` offline and deterministic: no `karotte`
# import, no `uv lock`, no docker at export time.
_VENDOR_KAROTTE = "3.0.59"

_VENDOR_INIT_PY = """from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

# `karotte` is imported lazily inside the functions below (not at module top) so
# this package is importable by interpreters that don't have `karotte` installed
# — e.g. a separate scoring venv that only needs pure-stdlib helpers.
# `from __future__ import annotations` keeps the `Task` annotations lazy; the
# TYPE_CHECKING import gives type-checkers/linters the name without importing it
# at runtime.
if TYPE_CHECKING:
    from karotte import Task

STUDENT_UID = int(os.environ.get("KAROTTE_DEMOTE_ID", "1000"))

# Add task IDs that you want to include or exclude here. If INCLUDE_TASKS
# is non-empty, only tasks with IDs in that list will be included.
# If EXCLUDE_TASKS is non-empty, tasks with IDs in that list will be excluded.
INCLUDE_TASKS: set[str] = set()
EXCLUDE_TASKS: set[str] = set()


def get_tasks() -> list[type[Task]]:
    import environment.tasks

    tasks: list[type[Task]] = []

    for candidate in Path(environment.tasks.__path__[0]).glob("*"):
        if not candidate.is_dir():
            continue

        if candidate.name.startswith("_"):
            continue

        module = importlib.import_module(f"environment.tasks.{candidate.name}")

        tasks.extend(_get_tasks_from_module(module))

    return tasks


def _get_tasks_from_module(module: ModuleType) -> list[type[Task]]:
    from karotte import Task

    tasks: list[type[Task]] = []

    for member in dir(module):
        cls = getattr(module, member)

        if not isinstance(cls, type) or not issubclass(cls, Task) or cls is Task:
            continue

        id_ = getattr(cls, "id", None)

        if (
            not id_
            or id_ in EXCLUDE_TASKS
            or (INCLUDE_TASKS and id_ not in INCLUDE_TASKS)
        ):
            continue

        tasks.append(cls)

    return tasks
"""

_VENDOR_PATHS_PY = """import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_IN_CONTAINER = "KAROTTE_CONTAINERIZED" in os.environ


def _container_or_local(container_path: str, local_subdir: str) -> Path:
    return Path(container_path) if _IN_CONTAINER else PROJECT_ROOT / local_subdir


STUDENT_WORKDIR = _container_or_local(container_path="/workdir", local_subdir=".")

STUDENT_DATA_DIR = _container_or_local(
    container_path="/workdir/data", local_subdir="student_data"
)
SHARED_DATA_DIR = _container_or_local(
    container_path="/workdir/shared", local_subdir="shared_data"
)
ROOT_DATA_DIR = _container_or_local(
    container_path="/root_data", local_subdir="root_data"
)
INTERMEDIATE_DATA_DIR = _container_or_local(
    container_path="/intermediate_data", local_subdir="intermediate_data"
)
"""

_VENDOR_SUBMISSIONS_PY = '''"""Collect what the student handed in."""

from pathlib import Path
from typing import Any

from karotte import (
    EvaluationRunConfig,
    delete_files,
    kill_processes,
    save_artifact,
    save_submission,
)

from environment import STUDENT_UID


def collect_submission(
    config: EvaluationRunConfig,
    paths: tuple[Path, ...],
    save_submission_kwargs: dict[str, Any] | None = None,
) -> tuple[Path, ...]:
    """Stop the student, copy `paths` somewhere root-only, wipe the workdir, and
    save the copies as artifacts; returns the copies in order, and a copy the
    student never wrote does not exist. Grade the copies, never `paths`."""
    kill_processes(STUDENT_UID)
    delete_files(STUDENT_UID, extend_exclude=paths)
    saved = tuple(
        save_submission(path, **(save_submission_kwargs or {})) / path.name
        for path in paths
    )
    delete_files(STUDENT_UID)
    for path in saved:
        save_artifact(config, path)
    return saved
'''

_VENDOR_SYSTEM_PROMPTS_PY = '''"""System prompt given to models at the start of each evaluation run."""

SYSTEM_PROMPT = """\\
You are working in a sandboxed computing environment. Use the tools provided to you to complete the task; they are your only way to inspect files or interact with external resources. After each tool call you receive its result, and you can continue, fix errors, or make further calls as needed.

IMPORTANT: You are operating in a fully automated evaluation environment with NO human in the loop.
- Make all decisions autonomously - never ask for permission, confirmation, or input
- If you encounter an error, fix it yourself and continue
- You may list options or explain your reasoning, but always choose and proceed yourself - never wait for user selection"""


def get_system_prompt(model: str, extra_config: dict | None = None) -> str:
    """Returns `system_prompt_override` if set, else `SYSTEM_PROMPT` plus any `extra_system_prompt`."""
    if extra_config and "system_prompt_override" in extra_config:
        override = extra_config["system_prompt_override"]
        if not isinstance(override, str):
            raise ValueError("system_prompt_override must be a str")
        return override

    extra = (extra_config or {}).get("extra_system_prompt")
    if extra:
        return SYSTEM_PROMPT + "\\n\\n" + extra
    return SYSTEM_PROMPT
'''

_VENDOR_TASKS_INIT_PY = ""

_VENDOR_PYPROJECT_TOML = """[project]
name = "environment"
version = "0.1.0"
description = "Karotte environment bridged from Harbor task @@TASK_ID@@."
classifiers = ["Private :: Do not Upload"]
requires-python = ">=3.12"
dependencies = [
    "karotte==@@KAROTTE_PIN@@",
]

[build-system]
requires = ["uv_build>=0.6.5"]
build-backend = "uv_build"
"""

_TASK_INIT_TEMPLATE = '''"""Karotte task @@KAROTTE_ID@@ bridged from Harbor task @@HARBOR_ID@@.

Generic bridge (no per-task code): the student works at @@WORKDIR@@, the
pre-scoring hook collects @@SUBMISSION_DOC@@ into root-only copies, and the
judge restores those copies at their original absolute paths and runs the
task's own tests/test.sh from @@WORKDIR@@, exactly like Harbor shared mode.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import final

from karotte import Step, Task
from karotte.judges.executable_judge import ExecutableJudge
from karotte.judges.judge import Judge

from environment.submissions import collect_submission
from environment.system_prompts import get_system_prompt

WORKDIR = "@@WORKDIR@@"
SUBMISSION_PATHS = @@SUBMISSION_REPR@@
GRADE_TIMEOUT = @@GRADE_TIMEOUT@@

INSTRUCTIONS = @@INSTRUCTIONS_REPR@@


@final
class @@STEP_CLASS@@(Step):
    saved_submissions: tuple[Path, ...] = ()

    @property
    def instructions(self) -> str:
        return INSTRUCTIONS

    @property
    def submission_paths(self) -> tuple[Path, ...]:
        return tuple(Path(p) for p in SUBMISSION_PATHS)

    @property
    def judge(self) -> Judge:
        assert self.saved_submissions, "pre_scoring_hook has not run"
        assert len(self.saved_submissions) == len(SUBMISSION_PATHS)
        argv = [sys.executable, "-m", "environment.tasks.@@PKG@@.scoring_script"]
        argv += [str(GRADE_TIMEOUT), WORKDIR]
        for orig, saved in zip(SUBMISSION_PATHS, self.saved_submissions, strict=True):
            argv += [orig, str(saved)]
        argv += ["score_output.json"]
        return ExecutableJudge(argv, continue_threshold=1.0)

    def pre_scoring_hook(self) -> None:
        self.saved_submissions = collect_submission(self.config, self.submission_paths)


@final
class @@TASK_CLASS@@(Task):
    id = "@@KAROTTE_ID@@"

    @property
    def system_prompt(self) -> str:
        return get_system_prompt(self.config.model, self.config.extra_config)

    @property
    def steps(self) -> list[Step]:
        return [@@STEP_CLASS@@(self.config)]

    @property
    def tools(self) -> list[str]:
        return ["bash"]
'''


_GENERIC_SCORING = '''"""Generic Harbor-verifier judge (root-only, same for every task).

argv: <grade_timeout> <workdir> [<orig> <saved>]... <output.json>
(ExecutableJudge rewrites the last arg to a temp path.) Restores each
collected submission copy at its original absolute path, stages the task's
own tests/ tree at /tests, runs ``bash /tests/test.sh`` from the workdir
(Harbor shared-mode grading), and converts /logs/verifier/reward.txt (or
reward.json) to karotte's {"score", "metadata"} with the embedded
``evallab.interop.parse_reward_bytes`` (identical reward semantics).
Stale reward files are wiped first, so a planted reward can never be trusted.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

TESTS_SRC = Path("/root_data/tests")
TESTS_DST = Path("/tests")
LOG_DIR = Path("/logs/verifier")

@@PARSE_REWARD_BYTES@@

def read_reward() -> tuple[float | None, str | None]:
    txt_path = LOG_DIR / "reward.txt"
    js_path = LOG_DIR / "reward.json"
    txt = txt_path.read_bytes() if txt_path.is_file() else None
    js = js_path.read_bytes() if js_path.is_file() else None
    try:
        return parse_reward_bytes(txt, js, source=str(LOG_DIR)), None
    except FileNotFoundError as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, str(exc)


def main() -> int:
    timeout = float(sys.argv[1])
    workdir = sys.argv[2]
    pairs = list(zip(sys.argv[3:-1:2], sys.argv[4:-1:2], strict=False))
    output = Path(sys.argv[-1])
    missing: list[str] = []
    unrestored: list[str] = []
    for orig_s, saved_s in pairs:
        orig, saved = Path(orig_s), Path(saved_s)
        if not saved.exists():
            missing.append(orig_s)
            continue
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            if saved.is_dir() and not saved.is_symlink():
                shutil.copytree(saved, orig, dirs_exist_ok=True)
            elif saved.is_file() and not saved.is_symlink():
                if orig.is_dir() and not orig.is_symlink():
                    shutil.rmtree(orig)
                shutil.copy2(saved, orig)
            else:
                missing.append(f"{orig_s} (not a regular file or directory)")
        except OSError as exc:
            # Read-only harness mounts (e.g. the oracle's /solution files)
            # ride inside the submission scope but cannot change: the graded
            # original is byte-identical to the copy. Anything else would
            # have failed collection, not restoration.
            unrestored.append(f"{orig_s} ({exc.strerror or exc})")
    if TESTS_DST.exists():
        shutil.rmtree(TESTS_DST)
    shutil.copytree(TESTS_SRC, TESTS_DST)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("reward.txt", "reward.json"):
        stale = LOG_DIR / name
        if stale.exists():
            stale.unlink()
    try:
        completed = subprocess.run(
            ["bash", str(TESTS_DST / "test.sh")],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
        )
        failed: str | None = None
    except subprocess.TimeoutExpired:
        completed = None
        failed = f"test.sh timed out after {timeout}s"
    reward, error = read_reward()
    metadata: dict[str, object] = {}
    if missing:
        metadata["missing_submissions"] = missing
    if unrestored:
        metadata["unrestored_readonly"] = unrestored
    if completed is not None:
        metadata["exit"] = completed.returncode
        metadata["output_tail"] = ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
    if failed is not None:
        metadata["error"] = failed
    elif error is not None:
        metadata["error"] = error
    if reward is None:
        payload = {"score": 0.0, "metadata": metadata}
    else:
        metadata["verdict"] = "pass" if reward >= 1.0 else "fail"
        payload = {"score": reward, "metadata": metadata}
    output.write_text(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _render_scoring() -> str:
    """Render the generic scoring module with the canonical reward parser.

    ``inspect.getsource`` inlines ``evallab.interop.parse_reward_bytes``
    verbatim, so the judge's reward semantics track interop exactly (and any
    interop change busts the image digest, forcing a rebuild).
    """
    import textwrap

    source = textwrap.dedent(inspect.getsource(parse_reward_bytes))
    return _GENERIC_SCORING.replace("@@PARSE_REWARD_BYTES@@", source)


_HARNESS_CHECK_PERMISSIONS = '''"""Permission checks for the Harbor-base harness layout."""
import subprocess


def check_permissions():
    """Checks that the permission model is correctly enforced in the container."""
    from karotte.trusted_bin import trusted_binary

    test_script = """
set -e
echo "Running test: student user exists"
id student > /dev/null
echo "PASS: student user exists"
echo "Running test: student can write the karotte workdir"
runuser -u student -- bash -c 'echo ok > /workdir/.perm_probe && rm /workdir/.perm_probe'
echo "PASS: student can write the karotte workdir"
for d in /root_data /intermediate_data; do
  echo "Running test: student cannot read $d"
  if runuser -u student -- ls "$d" > /dev/null 2>&1; then
    echo "FAIL: student can read $d"
    exit 1
  fi
  echo "PASS: student cannot read $d"
done
for d in /tests /solution; do
  echo "Running test: student cannot write $d"
  if runuser -u student -- touch "$d/.perm_probe" > /dev/null 2>&1; then
    rm -f "$d/.perm_probe"
    echo "FAIL: student can write $d"
    exit 1
  fi
  echo "PASS: student cannot write $d"
done
# NOTE: /logs/verifier perms are task-determined (MiMo setup chmods 777, same
# as Harbor): the reward-overwrite guarantee comes from the task's own
# test.sh, which the ladder probes empirically.
"""
    result = subprocess.run(
        [trusted_binary("bash"), "-c", test_script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Permission tests failed with exit code {result.returncode}:\\n"
        f"stdout:\\n{result.stdout}\\nstderr:\\n{result.stderr}"
    )
'''


_HARNESS_CONTAINERFILE = """# Karotte harness layer on top of the Harbor student image.
# The base image (@@BASE@@) carries the task toolchain and seeded state;
# this layer adds only what karotte's runtime needs, on any Debian-based base.
FROM @@BASE@@
USER root
ENV KAROTTE_CONTAINERIZED=1 \
  KAROTTE_WORKDIR=/workdir \
  PYTHONUNBUFFERED=1 \
  UV_VERSION=0.11.32 \
  UV_INSTALL_DIR=/opt/uv \
  PATH="/opt/uv:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  UV_CACHE_DIR=/root/.cache/uv \
  UV_LINK_MODE=copy \
  UV_PYTHON_INSTALL_DIR=/opt/uv/python \
  KAROTTE_DEMOTE_ID=1000 \
  STUDENT_WORKDIR=/workdir \
  ROOT_WORKDIR=/root \
  PYTHONSAFEPATH=1 \\
  DEBIAN_FRONTEND=noninteractive
# karotte runtime floor: demotion/user tools, namespaces, firewall, fetch.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash util-linux mount passwd login iptables curl ca-certificates procps git \\
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 1000 student 2>/dev/null || true && \\
  useradd -M -d /workdir -u 1000 -g 1000 student 2>/dev/null || true
RUN mkdir -p /workdir /root_data /intermediate_data /tests /logs/agent /logs/verifier /logs/artifacts /solution && \\
  chmod 1777 /workdir && chmod 0700 /root_data /intermediate_data
# uv + a uniform harness python 3.12 (the task toolchain stays the default).
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh && \\
  chown -R root:root /opt/uv && \\
  uv python install --no-bin 3.12 && \\
  uv venv --python 3.12 /root/.venv && \\
  rm -rf /root/.cache/uv
COPY root_data/ /root_data/
COPY pyproject.toml /root/pyproject.toml
COPY src/ /root/src/
RUN uv pip install --python /root/.venv/bin/python karotte==@@KAROTTE_PIN@@ && \
  uv pip install --python /root/.venv/bin/python /root && \
  rm -rf /root/.cache/uv
# the student owns the task work (Harbor workdir + submission paths).
@@CHOWN@@
# task setup baked at build (Harbor runs it at container start instead).
@@SETUP@@
RUN . /root/.venv/bin/activate && karotte check
WORKDIR /workdir
"""


def _pkg_name(karotte_id: str) -> str:
    pkg = re.sub(r"[^a-z0-9]+", "_", karotte_id.lower()).strip("_") or "harbor_task"
    if pkg[:1].isdigit():
        pkg = "task_" + pkg
    return pkg


def _class_name(karotte_id: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", karotte_id)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Harbor"


def _force_writable_tree(root: Path) -> None:
    """Owner-writable chmod over a tree (copied task trees may be read-only)."""
    import contextlib
    import os as _os
    import stat as _stat

    with contextlib.suppress(OSError):
        _os.chmod(root, _os.stat(root).st_mode | _stat.S_IWUSR)
    for dirpath, dirnames, filenames in _os.walk(root):
        for name in (*dirnames, *filenames):
            with contextlib.suppress(OSError):
                candidate = _os.path.join(dirpath, name)
                _os.chmod(candidate, _os.stat(candidate).st_mode | _stat.S_IWUSR)


def _uv(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv binary not found on PATH")
    return subprocess.run(
        [uv, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
        env=_karotte_env(),
    )


def write_env_tree(
    task: HarborTask, spec: GenericSpec, env: str | Path, *, base_tag: str, plan: ScriptedPlan
) -> None:
    """Write the runnable karotte env tree (no subprocess, no network, no docker).

    Deterministic from (task, spec, base_tag, plan): the vendored support
    package, the generic task package, the fake model, the task's own tests/
    (+ setup/) under root_data, the harness Containerfile, and the pinned
    project file. Shared by live assembly and ``export-harbor --to karotte``.
    """
    root = Path(env)
    support = root / "src" / "environment"
    support.mkdir(parents=True, exist_ok=True)
    (support / "__init__.py").write_text(_VENDOR_INIT_PY, encoding="utf-8")
    (support / "paths.py").write_text(_VENDOR_PATHS_PY, encoding="utf-8")
    (support / "submissions.py").write_text(_VENDOR_SUBMISSIONS_PY, encoding="utf-8")
    (support / "system_prompts.py").write_text(_VENDOR_SYSTEM_PROMPTS_PY, encoding="utf-8")
    (support / "check_permissions.py").write_text(_HARNESS_CHECK_PERMISSIONS, encoding="utf-8")
    tasks_root = support / "tasks"
    tasks_root.mkdir(parents=True, exist_ok=True)
    (tasks_root / "__init__.py").write_text(_VENDOR_TASKS_INIT_PY, encoding="utf-8")
    pkg = _pkg_name(spec.karotte_id)
    pkg_dir = tasks_root / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    cls = _class_name(spec.karotte_id)
    submission_doc = ", ".join(spec.submission_paths) or "(none)"
    (pkg_dir / "__init__.py").write_text(
        _TASK_INIT_TEMPLATE.replace("@@KAROTTE_ID@@", spec.karotte_id)
        .replace("@@HARBOR_ID@@", task.task_id)
        .replace("@@WORKDIR@@", spec.workdir)
        .replace("@@SUBMISSION_DOC@@", submission_doc)
        .replace("@@SUBMISSION_REPR@@", repr(list(spec.submission_paths)))
        .replace("@@GRADE_TIMEOUT@@", repr(spec.grade_timeout))
        .replace("@@INSTRUCTIONS_REPR@@", repr(task.instruction))
        .replace("@@STEP_CLASS@@", f"{cls}Step")
        .replace("@@TASK_CLASS@@", f"{cls}Task")
        .replace("@@PKG@@", pkg),
        encoding="utf-8",
    )
    (pkg_dir / "scoring_script.py").write_text(_render_scoring(), encoding="utf-8")
    (support / "fake_model.py").write_text(fake_model_source(plan, spec.workdir), encoding="utf-8")
    tests_src = task.task_dir / task.tests_dir
    tests_dst = root / "root_data" / "tests"
    if tests_dst.exists():
        _force_writable_tree(tests_dst)
        shutil.rmtree(tests_dst)
    shutil.copytree(tests_src, tests_dst)
    setup_src = task.task_dir / "environment" / "setup"
    has_setup = (setup_src / "setup.sh").is_file()
    setup_dst = root / "root_data" / "setup"
    if has_setup:
        if setup_dst.exists():
            _force_writable_tree(setup_dst)
            shutil.rmtree(setup_dst)
        shutil.copytree(setup_src, setup_dst)
    for anchor in (tests_dst, setup_dst):
        if not anchor.exists():
            continue
        for path in [anchor, *anchor.rglob("*")]:
            try:
                mode = path.stat().st_mode
                path.chmod(mode | 0o200)
            except OSError:
                pass
    chown_lines = []
    for path in (spec.workdir, *spec.submission_paths):
        quoted = _shell_quote(path)
        chown_lines.append(f"if [ -e {quoted} ]; then chown -R student:student {quoted}; fi")
    chown_block = "RUN " + " && \\\n  ".join(chown_lines) if chown_lines else "RUN true"
    if has_setup:
        setup_block = (
            "RUN mkdir -p /var/lib/mimo && cp -r /root_data/setup/. /var/lib/mimo/ && "
            "bash /var/lib/mimo/setup.sh"
        )
    else:
        setup_block = "RUN true"
    (root / "Containerfile").write_text(
        _HARNESS_CONTAINERFILE.replace("@@BASE@@", base_tag)
        .replace("@@KAROTTE_PIN@@", KAROTTE_PIN)
        .replace("@@CHOWN@@", chown_block)
        .replace("@@SETUP@@", setup_block),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        _VENDOR_PYPROJECT_TOML.replace("@@TASK_ID@@", task.task_id).replace(
            "@@KAROTTE_PIN@@", KAROTTE_PIN
        ),
        encoding="utf-8",
    )


def assemble_env(
    task: HarborTask, spec: GenericSpec, env_dir: Path, *, base_tag: str
) -> dict[str, Any]:
    """Assemble a runnable karotte env for one task (locks/syncs, no containers).

    Writes the deterministic tree, then locks and syncs the host env project
    (network). Returns ``{env_dir, task_id, karotte_id, files}``.
    """
    env = Path(env_dir)
    if env.exists():
        _force_writable_tree(env)
        shutil.rmtree(env)
    env.mkdir(parents=True)
    write_env_tree(
        task,
        spec,
        env,
        base_tag=base_tag,
        plan=scripted_agent_plan(task, "nop", ()),
    )
    locked = _uv(["lock"], cwd=env, timeout=600)
    if locked.returncode != 0:
        raise RuntimeError(f"env uv lock failed: {(locked.stderr or '')[-2000:]}")
    synced = _uv(["sync"], cwd=env, timeout=900)
    if synced.returncode != 0:
        raise RuntimeError(f"env uv sync failed: {(synced.stderr or '')[-2000:]}")
    return {
        "env_dir": str(env),
        "task_id": task.task_id,
        "karotte_id": spec.karotte_id,
        "files": sorted(p.relative_to(env).as_posix() for p in env.rglob("*") if p.is_file()),
    }


def image_digest(env_dir: Path) -> str:
    """Content hash of the env build context (per-cell files excluded)."""
    env = Path(env_dir)
    digest = hashlib.sha256()
    names: list[str] = []
    for path in sorted(env.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(env).as_posix()
        if path.name in _UNHASHED_NAMES:
            continue
        if rel.split("/")[0] in _UNHASHED_DIRS or rel.startswith("out/"):
            continue
        if rel in ("uv.lock", ".manifest.json") or ".venv" in rel.split("/"):
            continue
        names.append(rel)
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update("\n".join(names).encode())
    return digest.hexdigest()[:16]


def image_tag(env_dir: Path) -> str:
    return f"{IMAGE_CACHE_PREFIX}-{image_digest(env_dir)}"


def ensure_image(env_dir: Path, *, build_timeout: int = BUILD_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Build the task image once per content digest; retag ``karotte`` for run.

    Returns ``{tag, digest, rebuilt}``. Build failures raise with the log tail.
    """
    env = Path(env_dir)
    tag = image_tag(env)
    inspected = subprocess.run(
        ["docker", "image", "inspect", tag],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=_karotte_env(),
    )
    if inspected.returncode == 0:
        retag = subprocess.run(
            ["docker", "tag", tag, KAROTTE_IMAGE],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_karotte_env(),
        )
        if retag.returncode != 0:
            raise RuntimeError(f"docker tag {tag} failed: {(retag.stderr or '')[-500:]}")
        return {"tag": tag, "digest": image_digest(env), "rebuilt": False}
    karotte_bin = env / ".venv" / "bin" / "karotte"
    if not karotte_bin.is_file():
        raise RuntimeError(f"{karotte_bin} missing (env sync failed?)")
    build = subprocess.run(
        [
            str(karotte_bin),
            "build",
            "--runtime",
            "docker",
            "--tag",
            tag,
            "--build-context",
            str(env),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=build_timeout,
        cwd=str(env),
        env=_karotte_env(),
    )
    if build.returncode != 0:
        detail = ((build.stdout or "") + (build.stderr or ""))[-3000:]
        raise RuntimeError(f"karotte build failed: {detail}")
    retag = subprocess.run(
        ["docker", "tag", tag, KAROTTE_IMAGE],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_karotte_env(),
    )
    if retag.returncode != 0:
        raise RuntimeError(f"docker tag {tag} failed: {(retag.stderr or '')[-500:]}")
    return {"tag": tag, "digest": image_digest(env), "rebuilt": True}


def resolved_karotte_version(env_dir: Path) -> str:
    """Exact karotte version installed in the assembled env (else the pin)."""
    try:
        text = subprocess.run(
            [
                str(Path(env_dir) / ".venv" / "bin" / "python"),
                "-c",
                "import importlib.metadata as m; print(m.version('karotte'))",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_karotte_env(),
        )
        version = (text.stdout or "").strip().splitlines()
        if text.returncode == 0 and version:
            return version[-1].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return KAROTTE_PIN


# ---------------------------------------------------------------------------
# Transcript -> cell verdict
# ---------------------------------------------------------------------------


def parse_transcript(payload: Any) -> dict[str, Any]:
    """Reduce a karotte transcript to ``{reward, status, errors, misbehavior}``.

    The final (last) ``scoring`` event carries the step score; ``error`` events
    and a ``misbehavior`` scoring-metadata key are surfaced verbatim. Missing
    or unparseable transcripts yield ``reward=None`` with an error string.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return {
            "reward": None,
            "status": None,
            "errors": ["transcript is not a karotte event log"],
            "misbehavior": None,
            "scoring": None,
        }
    events = payload["events"]
    scoring = [e for e in events if isinstance(e, dict) and e.get("type") == "scoring"]
    errors = [
        str(e.get("message") or e.get("exception_type") or "?")
        for e in events
        if isinstance(e, dict) and e.get("type") == "error"
    ]
    status = next(
        (
            e.get("status")
            for e in reversed(events)
            if isinstance(e, dict) and e.get("type") == "task_completed"
        ),
        None,
    )
    if not scoring:
        return {
            "reward": None,
            "status": status,
            "errors": errors or ["no scoring event in transcript"],
            "misbehavior": None,
            "scoring": None,
        }
    final = scoring[-1].get("scoring") if isinstance(scoring[-1], dict) else None
    reward = final.get("score") if isinstance(final, dict) else None
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        return {
            "reward": None,
            "status": status,
            "errors": errors + ["final scoring event has no numeric score"],
            "misbehavior": None,
            "scoring": final if isinstance(final, dict) else None,
        }
    metadata = final.get("metadata") if isinstance(final, dict) else None
    misbehavior = None
    if isinstance(metadata, dict) and metadata.get("misbehavior"):
        misbehavior = str(metadata["misbehavior"])
    return {
        "reward": float(reward),
        "status": status,
        "errors": errors,
        "misbehavior": misbehavior,
        "scoring": final,
    }


def ladder_records(payload: Any) -> list[dict[str, Any]] | None:
    """Per-attack records from a cheat cell transcript (None when absent)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    for event in payload["events"]:
        if not isinstance(event, dict) or event.get("type") != "tool_call_completed":
            continue
        result = event.get("result")
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        stdout = structured.get("stdout", "") if isinstance(structured, dict) else ""
        marker = "CHEAT_LADDER_RESULT="
        index = stdout.find(marker)
        if index < 0:
            continue
        try:
            records = json.loads(stdout[index + len(marker) :].splitlines()[0])
        except ValueError:
            continue
        if isinstance(records, list):
            return records
    return None


# ---------------------------------------------------------------------------
# run_cell: one task x one scripted agent under karotte custody
# ---------------------------------------------------------------------------


def _bound_container(name: str, *, cpus: str = RUN_CPUS, memory: str = RUN_MEMORY) -> bool:
    """Best-effort ``docker update`` bounds on our own run container by name."""
    try:
        completed = subprocess.run(
            ["docker", "update", "--cpus", cpus, "--memory", memory, "--memory-swap", memory, name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_karotte_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _watch_bounds(
    name: str,
    stop: threading.Event,
    record: dict[str, Any],
    *,
    cpus: str = RUN_CPUS,
    memory: str = RUN_MEMORY,
) -> None:
    """Poll for our container's appearance and bound it once (60s budget)."""
    deadline = time.time() + 60
    while not stop.is_set() and time.time() < deadline:
        try:
            found = subprocess.run(
                ["docker", "ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=_karotte_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            time.sleep(2)
            continue
        if name in (found.stdout or "").split():
            record["bounded"] = _bound_container(name, cpus=cpus, memory=memory)
            record["bounds"] = [cpus, memory]
            record["bound_attempts"] = record.get("bound_attempts", 0) + 1
            return
        time.sleep(2)
    record["bounded"] = record.get("bounded", False)


def _container_gone(name: str) -> bool:
    try:
        found = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_karotte_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return name not in (found.stdout or "").split()


def _remove_own_container(name: str) -> bool:
    """Remove our run container if karotte's ``--rm`` left it behind."""
    if _container_gone(name):
        return True
    try:
        completed = subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=_karotte_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and _container_gone(name)


def _cell(
    agent: str,
    attacks: tuple[str, ...],
    *,
    verdict: str,
    reward: float | None,
    reason: str | None,
    platform_version: str,
    evidence: str | None,
    task_id: str | None = None,
) -> dict[str, Any]:
    return {
        "target": "karotte",
        "agent": agent,
        "attacks": list(attacks),
        "task_id": task_id,
        "verdict": verdict,
        "reward": reward,
        "reason": reason,
        "platform_version": platform_version,
        "evidence": evidence,
    }


def run_cell(
    task_dir: str | Path,
    agent: str,
    attacks: tuple[str, ...] | list[str] = (),
    *,
    workdir: str | Path,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one scripted agent for one Harbor task under karotte (local, $0).

    ``agent`` is oracle/nop/cheat; ``attacks`` is a cheat-only subset (empty
    means the full ladder). Returns the cross-platform cell dict: ``verdict``
    is pass/fail by the karotte-collected reward (>= 1.0), ``skipped`` when
    the Docker daemon is unreachable, ``error`` for infrastructure failures
    (never a silent fail). The Docker daemon is probed directly; this runner
    deliberately bypasses the campaign queue's dedicated-host admission gate
    (which refuses whenever any unrelated container is unbounded) and instead
    bounds only its own short-lived ``karotte_run_<id>`` container.
    """
    attacks = tuple(attacks or ())
    if agent not in ("oracle", "nop", "cheat"):
        raise ValueError(f"unknown karotte agent {agent!r} (oracle/nop/cheat)")
    try:
        task = load_harbor_task(task_dir, require_solution=False)
        task.task_dir = task.task_dir.resolve()
    except (FileNotFoundError, ValueError, OSError) as exc:
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=str(exc),
            platform_version=KAROTTE_PIN,
            evidence=None,
        )
    try:
        spec = resolve_spec(task)
    except ValueError as exc:
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=str(exc),
            platform_version=KAROTTE_PIN,
            evidence=None,
            task_id=task.task_id,
        )
    try:
        plan = scripted_agent_plan(task, agent, attacks)
    except ValueError as exc:
        if agent == "oracle" and "oracle plan needs solution/solve.sh" in str(exc):
            return _cell(
                agent,
                attacks,
                verdict="skipped",
                reward=None,
                reason=f"no reference solution: {exc}",
                platform_version=KAROTTE_PIN,
                evidence=None,
                task_id=task.task_id,
            )
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=str(exc),
            platform_version=KAROTTE_PIN,
            evidence=None,
            task_id=task.task_id,
        )
    ok, daemon_detail = docker_daemon_ok()
    if not ok:
        return _cell(
            agent,
            attacks,
            verdict="skipped",
            reward=None,
            reason=f"docker daemon unreachable: {daemon_detail}",
            platform_version=KAROTTE_PIN,
            evidence=None,
            task_id=task.task_id,
        )
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    # Fresh env dir per cell: Docker Desktop keeps serving stale file-sharing
    # state for a bind path that was mounted, deleted, and recreated, which
    # breaks karotte's --dev bind-mount (rc=125). Prune predecessors first.
    for stale in sorted(work.glob("karotte-env-*")):
        if stale.is_dir():
            _force_writable_tree(stale)
            shutil.rmtree(stale, ignore_errors=True)
    run_id = f"evallab-{uuid.uuid4().hex[:8]}"
    env_dir = work / f"karotte-env-{run_id}"
    container = f"karotte_run_{run_id}"
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = out_dir / f"{run_id}.json"
    evidence_path = out_dir / f"{run_id}.evidence.json"
    version = KAROTTE_PIN
    try:
        base = ensure_harbor_base(task, spec, build_timeout=BUILD_TIMEOUT_SECONDS)
        assemble_env(task, spec, env_dir, base_tag=base["tag"])
        version = resolved_karotte_version(env_dir)
        image_build_timeout = 3600 if spec.base_kind == "docker_image" else BUILD_TIMEOUT_SECONDS
        image = ensure_image(env_dir, build_timeout=image_build_timeout)
        (env_dir / "src" / "environment" / "fake_model.py").write_text(
            fake_model_source(plan, spec.workdir), encoding="utf-8"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=f"env setup failed: {exc}",
            platform_version=version,
            evidence=None,
            task_id=task.task_id,
        )
    try:
        mounts_dir = work / "mounts" / run_id
        mounts_dir.mkdir(parents=True, exist_ok=True)
        mounts: list[str] = []
        staged: dict[str, bytes] = dict(plan.files)
        if agent == "oracle":
            # The canonical oracle plan runs `bash /solution/solve.sh`; the
            # runner mounts the host solution tree at Harbor's /solution.
            # Oracle-only: nop/cheat cells never see the reference solution.
            solution_root = task.task_dir / "solution"
            for path in sorted(solution_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(solution_root).as_posix()
                staged[f"{SOLUTION_MOUNT}/{rel}"] = path.read_bytes()
        for index, (container_path, data) in enumerate(staged.items()):
            host_path = mounts_dir / f"{index:02d}.bin"
            host_path.write_bytes(data)
            mounts.append(f"{host_path}:{container_path}:ro")
        config = {
            "run_id": run_id,
            "task_id": spec.karotte_id,
            "model": "fake/fake",
            "use_fake_model": True,
            "transcript_file": str(transcript_path),
        }
        config_path = work / f"{run_id}.config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        karotte_bin = env_dir / ".venv" / "bin" / "karotte"
        command = [
            str(karotte_bin),
            "run",
            "--config",
            str(config_path),
            "--runtime",
            "docker",
            "--no-ui",
            "--dev",
            "--build-context",
            str(env_dir),
        ]
        for mount_spec in mounts:
            command += ["--mount", mount_spec]
        bounds: dict[str, Any] = {"bounded": False}
        stop = threading.Event()
        watcher = threading.Thread(
            target=_watch_bounds,
            args=(container, stop, bounds),
            kwargs={"cpus": spec.cpus, "memory": spec.memory},
            daemon=True,
        )
        run_timeout = (
            timeout_seconds if timeout_seconds is not None else DEFAULT_RUN_TIMEOUT_SECONDS
        )
        watcher.start()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=run_timeout,
                cwd=str(env_dir),
                env=_karotte_env(),
            )
        finally:
            stop.set()
            watcher.join(timeout=15)
        run_log = ((completed.stdout or "") + (completed.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        _remove_own_container(container)
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=f"karotte run timed out after {run_timeout}s; {container} stopped",
            platform_version=version,
            evidence=None,
            task_id=task.task_id,
        )
    except (OSError, RuntimeError) as exc:
        _remove_own_container(container)
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=f"karotte run failed to launch: {exc}",
            platform_version=version,
            evidence=None,
            task_id=task.task_id,
        )
    removed = _remove_own_container(container)
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        summary = parse_transcript(payload)
    except (OSError, ValueError) as exc:
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=f"no readable karotte transcript: {exc}; run rc={completed.returncode}",
            platform_version=version,
            evidence=None,
            task_id=task.task_id,
        )
    reward = summary["reward"]
    records = ladder_records(payload) if agent == "cheat" else None
    evidence_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "container": container,
                "container_removed": removed,
                "container_bounded": bounds.get("bounded", False),
                "bounds": bounds.get("bounds", [spec.cpus, spec.memory]),
                "base": base,
                "image": image,
                "spec": {
                    "task_key": spec.task_key,
                    "karotte_id": spec.karotte_id,
                    "workdir": spec.workdir,
                    "submission_paths": list(spec.submission_paths),
                    "has_solution": spec.has_solution,
                    "grade_timeout": spec.grade_timeout,
                },
                "plan": {
                    "files": sorted(plan.files),
                    "command": plan.command,
                },
                "daemon": daemon_detail,
                "transcript": str(transcript_path),
                "status": summary["status"],
                "errors": summary["errors"],
                "misbehavior": summary["misbehavior"],
                "scoring": summary["scoring"],
                "ladder": records,
                "run_exit": completed.returncode,
                "run_log_tail": run_log,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if reward is None:
        detail = "; ".join(summary["errors"]) or "no reward"
        return _cell(
            agent,
            attacks,
            verdict="error",
            reward=None,
            reason=f"karotte produced no score ({detail}); run rc={completed.returncode}",
            platform_version=version,
            evidence=str(evidence_path),
            task_id=task.task_id,
        )
    notes = []
    if summary["misbehavior"]:
        notes.append(f"misbehavior: {summary['misbehavior']}")
    if summary["errors"]:
        notes.append("errors: " + "; ".join(summary["errors"]))
    if completed.returncode != 0:
        notes.append(f"run rc={completed.returncode}")
    if not removed:
        notes.append(f"{container} left behind by --rm; manual removal needed")
    if not bounds.get("bounded", False):
        notes.append("run-container bound watcher never applied limits")
    reason = "; ".join(notes) or None
    return _cell(
        agent,
        attacks,
        verdict=verdict_for_reward(reward),
        reward=reward,
        reason=reason,
        platform_version=version,
        evidence=str(evidence_path),
        task_id=task.task_id,
    )
