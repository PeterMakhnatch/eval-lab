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
bounds each run container to 2 CPUs / 2 GiB via ``docker update`` without ever
touching containers it did not start.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.interop import KAROTTE_PIN, HarborTask, load_harbor_task, verdict_for_reward

#: Image tag karotte's ``run`` reads (hardcoded upstream); content-keyed tags
#: below let cells skip rebuilds and retag this name.
KAROTTE_IMAGE = "karotte"
#: Cache tag prefix for built task images (``evallab-karotte-<digest>``).
IMAGE_CACHE_PREFIX = "evallab-karotte"
#: Bounds applied to each run container (cpus, memory).
RUN_CPUS = "2"
RUN_MEMORY = "2g"
#: Build budget per task image; the run phase uses ``timeout_seconds``.
BUILD_TIMEOUT_SECONDS = 1800
DEFAULT_RUN_TIMEOUT_SECONDS = 1200
#: Per-command timeout (seconds) for scripted bash tool calls.
BASH_TIMEOUT_S = 300
#: Timeout for the cheat-ladder tool call (full ladder worst case ~7 min).
CHEAT_TIMEOUT_S = 900
#: Files the cell runner never hashes into the image digest (per-cell inputs).
_UNHASHED_NAMES = {"fake_model.py"}
_UNHASHED_DIRS = {"out"}
#: Ladder source, staged into the student container for cheat cells.
CHEAT_LADDER_MOUNT = "/tmp/cheat_ladder.py"


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


def selected_cheat_attacks(attacks: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate a cheat attack subset against the ladder; empty means full."""
    from evallab.cheat_ladder import parse_attack_selection

    raw = ",".join(attacks) if attacks else None
    return parse_attack_selection(raw)


# ---------------------------------------------------------------------------
# Task adapters: Harbor task -> karotte student data + oracle + scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KarotteTaskAdapter:
    """Everything karotte needs that is Harbor-task-specific."""

    #: library dir name this adapter handles.
    task_key: str
    #: karotte ``Task.id`` (also the ``environment.tasks`` package name seed).
    karotte_id: str
    #: absolute container paths the student must hand in.
    submission_paths: tuple[str, ...]
    #: ``student_data/`` overlay: relative path -> file bytes.
    student_files: dict[str, bytes]
    #: ``root_data/`` overlay: relative path -> file bytes.
    root_files: dict[str, bytes]
    #: bash commands the oracle fake-model runs in the student workdir.
    oracle_commands: tuple[str, ...]
    #: scoring module source (``scoring_script.py``): argv = saved copies...,
    #: last argv = output JSON path; writes ``{"score", "metadata"}``.
    scoring_source: str


_TXN_SCORING = '''"""Score the collected ledger copy with the Harbor verifier (root-only)."""
import json
import subprocess
import sys
from pathlib import Path

STAGE = Path("/app/data/ledger.db")
TESTS = Path("/root_data/task-tests")


def main() -> int:
    saved = sys.argv[1:-1]
    output = Path(sys.argv[-1])
    db = Path(saved[0]) if saved else None
    if db is None or not db.is_file():
        output.write_text(json.dumps({
            "score": 0.0,
            "metadata": {"error": "no ledger database handed in"},
        }))
        return 0
    STAGE.parent.mkdir(parents=True, exist_ok=True)
    if STAGE.exists():
        STAGE.unlink()
    STAGE.write_bytes(db.read_bytes())
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "test_outputs.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(TESTS),
    )
    tail = ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
    if completed.returncode == 0:
        payload = {"score": 1.0, "metadata": {"exit": 0}}
    else:
        payload = {"score": 0.0,
                   "metadata": {"exit": completed.returncode, "output_tail": tail}}
    output.write_text(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_EVSUM_SCORING = '''"""Score the collected summary with the Harbor verifier (root-only)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

APP_IN = Path("/app/input/events.jsonl")
APP_OUT = Path("/app/output/summary.json")
FIX_SRC = Path("/root_data/fixtures/events.jsonl")
FIX_DST = Path("/tests/fixtures/events.jsonl")
VERIFY = Path("/root_data/task-tests/verify.py")
LOG_REWARD = Path("/logs/verifier/reward.json")


def main() -> int:
    saved = sys.argv[1:-1]
    output = Path(sys.argv[-1])
    if len(saved) < 2:
        output.write_text(json.dumps({
            "score": 0.0, "metadata": {"error": "expected input+output copies"},
        }))
        return 0
    src_in, src_out = Path(saved[0]), Path(saved[1])
    if not src_out.is_file():
        output.write_text(json.dumps({
            "score": 0.0, "metadata": {"error": "summary.json is missing"},
        }))
        return 0
    for path in (APP_IN, APP_OUT, FIX_DST):
        path.parent.mkdir(parents=True, exist_ok=True)
    if src_in.is_file():
        APP_IN.write_bytes(src_in.read_bytes())
    elif APP_IN.exists():
        APP_IN.unlink()
    APP_OUT.write_bytes(src_out.read_bytes())
    FIX_DST.write_bytes(FIX_SRC.read_bytes())
    if LOG_REWARD.exists():
        LOG_REWARD.unlink()
    completed = subprocess.run(
        [sys.executable, str(VERIFY)],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(VERIFY.parent),
    )
    reward: float | None = None
    error: str | None = None
    if LOG_REWARD.is_file():
        try:
            payload = json.loads(LOG_REWARD.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            error = f"reward.json unreadable: {exc}"
        else:
            for key in ("score", "reward"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    reward = float(value)
                    break
            if reward is None:
                error = "reward.json has no numeric score/reward key"
    else:
        error = "verifier wrote no reward.json"
    if error is not None:
        tail = ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
        output.write_text(json.dumps({
            "score": 0.0,
            "metadata": {"error": error, "exit": completed.returncode,
                         "output_tail": tail},
        }))
        return 0
    assert reward is not None
    output.write_text(json.dumps({
        "score": reward,
        "metadata": {"exit": completed.returncode,
                     "verdict": "pass" if reward >= 1.0 else "fail"},
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _txn_oracle_commands() -> tuple[str, ...]:
    script = (
        "import sqlite3\n"
        "db = sqlite3.connect('/workdir/data/ledger.db')\n"
        "db.execute(\"UPDATE ledger_entries SET settled_amount_cents = "
        "(SELECT amount_cents FROM settlement_feed WHERE "
        "settlement_feed.external_id = ledger_entries.external_id), "
        "reconciliation_status = 'reconciled' WHERE EXISTS (SELECT 1 FROM "
        "settlement_feed WHERE settlement_feed.external_id = "
        "ledger_entries.external_id AND settlement_feed.amount_cents != "
        "ledger_entries.settled_amount_cents)\")\n"
        "db.commit()\n"
        "print('reconciled', db.total_changes)\n"
        "db.close()\n"
    )
    return (f"python3 - <<'PYEOF'\n{script}PYEOF",)


def _evsum_oracle_commands() -> tuple[str, ...]:
    script = (
        "import json, math\n"
        "from collections import Counter\n"
        "from pathlib import Path\n"
        "events = [json.loads(l) for l in "
        "Path('/workdir/data/input/events.jsonl').read_text().splitlines() if l.strip()]\n"
        "durs = sorted(e['duration_ms'] for e in events)\n"
        "counts = Counter(e['kind'] for e in events)\n"
        "summary = {'schema_version': 1, 'total_events': len(events), "
        "'counts': {k: counts[k] for k in sorted(counts)}, "
        "'total_duration_ms': sum(durs), "
        "'p95_duration_ms': durs[math.ceil(0.95 * len(durs)) - 1]}\n"
        "out = Path('/workdir/data/output/summary.json')\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text(json.dumps(summary, separators=(',', ':')) + chr(10))\n"
        "print('wrote', out)\n"
    )
    return (f"python3 - <<'PYEOF'\n{script}PYEOF",)


def _seeded_ledger_db(task: HarborTask) -> bytes:
    """Seed the starting ledger with the task's own seed script (host-side)."""
    import sqlite3

    seed = (task.task_dir / "environment" / "seed_db.py").read_text(encoding="utf-8")
    marker = 'database_path = Path("/app/data/ledger.db")'
    if marker not in seed:
        raise ValueError("seed_db.py no longer declares the /app/data/ledger.db path")
    with tempfile.TemporaryDirectory(prefix="karotte-seed-") as tmp:
        target = Path(tmp) / "ledger.db"
        patched = seed.replace(marker, f"database_path = Path({str(target)!r})", 1)
        namespace: dict[str, Any] = {}
        exec(compile(patched, "seed_db.py", "exec"), namespace)  # noqa: S102
        connection = sqlite3.connect(target)
        try:
            (rows,) = connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()
        finally:
            connection.close()
        if rows != 6:
            raise ValueError(f"seeded ledger has {rows} entries, expected 6")
        return target.read_bytes()


def txn_adapter(task: HarborTask) -> KarotteTaskAdapter:
    tests = task.task_dir / task.tests_dir / "test_outputs.py"
    return KarotteTaskAdapter(
        task_key="transaction-reconciliation",
        karotte_id="transaction-reconciliation",
        submission_paths=("/workdir/data/ledger.db",),
        student_files={"ledger.db": _seeded_ledger_db(task)},
        root_files={"task-tests/test_outputs.py": tests.read_bytes()},
        oracle_commands=_txn_oracle_commands(),
        scoring_source=_TXN_SCORING,
    )


def evsum_adapter(task: HarborTask) -> KarotteTaskAdapter:
    tests = task.task_dir / task.tests_dir
    env = task.task_dir / "environment"
    return KarotteTaskAdapter(
        task_key="event-summary",
        karotte_id="event-summary",
        submission_paths=(
            "/workdir/data/input/events.jsonl",
            "/workdir/data/output/summary.json",
        ),
        student_files={
            "input/events.jsonl": (env / "events.jsonl").read_bytes(),
        },
        root_files={
            "task-tests/verify.py": (tests / "verify.py").read_bytes(),
            "fixtures/events.jsonl": (tests / "fixtures" / "events.jsonl").read_bytes(),
        },
        oracle_commands=_evsum_oracle_commands(),
        scoring_source=_EVSUM_SCORING,
    )


ADAPTERS = {
    "transaction-reconciliation": txn_adapter,
    "event-summary": evsum_adapter,
}
"""Task-dir names with a real karotte runner adapter (no generic fallback)."""


def adapter_for(task: HarborTask) -> KarotteTaskAdapter:
    try:
        build = ADAPTERS[task.task_dir.name]
    except KeyError:
        raise ValueError(
            f"no karotte runner adapter for task dir {task.task_dir.name!r} "
            f"(have: {sorted(ADAPTERS)})"
        ) from None
    return build(task)


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


def cheat_command(attacks: tuple[str, ...]) -> str:
    """Shell command running the staged ladder in the student workdir."""
    parts = [f"python3 {_shell_quote(CHEAT_LADDER_MOUNT)}", "--cwd /workdir"]
    if attacks:
        parts += ["--attacks", ",".join(attacks)]
    parts += ["--evidence-dir /tmp/cheat-evidence"]
    return " ".join(parts)


def fake_model_source(
    agent: str,
    *,
    adapter: KarotteTaskAdapter,
    attacks: tuple[str, ...] = (),
) -> str:
    """Render ``environment/fake_model.py`` for one cell.

    oracle replays the task's oracle commands; nop sends a single text-only
    message (no tool calls); cheat replays the staged ``cheat_ladder.py``
    (``evallab.cheat_ladder``, same ladder ``evallab cheat run`` executes).
    """
    if agent == "oracle":
        messages = plan_messages(adapter.oracle_commands)
    elif agent == "nop":
        messages = plan_messages((), done_text="No changes; grading the initial state.")
    elif agent == "cheat":
        selection = selected_cheat_attacks(tuple(attacks))
        messages = plan_messages(
            (cheat_command(selection),),
            command_timeout_s=CHEAT_TIMEOUT_S,
            done_text="Cheat ladder finished.",
        )
    else:
        raise ValueError(f"unknown karotte agent {agent!r} (oracle/nop/cheat)")
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
# Environment assembly (karotte's own template + task overlay)
# ---------------------------------------------------------------------------

_POPULATE_SNIPPET = """
import json
from pathlib import Path
from karotte.create_env import _populate, resolve_template_deps
from karotte.templates import discover_templates, TEMPLATES_DIR
out = Path(@@OUT@@)
installed = discover_templates(TEMPLATES_DIR)
existing = {id_: t.template for id_, t in installed.items()}
resolved = resolve_template_deps(["default"], existing)
out.mkdir(parents=True, exist_ok=True)
_populate(out, resolved, installed, agents=[], vendor_karotte=False, no_lock=True)
print(json.dumps({"populated": str(out), "templates": resolved}))
"""

_TASK_INIT_TEMPLATE = '''"""Karotte task @@KAROTTE_ID@@ bridged from Harbor task @@HARBOR_ID@@.

Student-writable: @@SUBMISSION_DOC@@. The judge grades karotte's own
collected submission copies (root-only) with the Harbor verifier logic;
workspace reward-claim files are never read.
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

SUBMISSION_PATHS = @@SUBMISSION_REPR@@

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
        return ExecutableJudge(
            [
                sys.executable,
                "-m",
                "environment.tasks.@@PKG@@.scoring_script",
                *(str(p) for p in self.saved_submissions),
                "score_output.json",
            ],
            continue_threshold=1.0,
        )

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


def _pkg_name(karotte_id: str) -> str:
    pkg = re.sub(r"[^a-z0-9]+", "_", karotte_id.lower()).strip("_") or "harbor_task"
    if pkg[:1].isdigit():
        pkg = "task_" + pkg
    return pkg


def _class_name(karotte_id: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", karotte_id)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Harbor"


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


def assemble_env(
    task: HarborTask, adapter: KarotteTaskAdapter, env_dir: Path
) -> dict[str, Any]:
    """Assemble a runnable karotte env for one task (no containers, no run).

    Populates karotte's own ``default`` template, overlays the single task
    package + data + a pinned ``karotte==`` dependency, and locks/syncs the
    env project. Returns ``{env_dir, task_id, karotte_id, files}``.
    """
    env = Path(env_dir)
    if env.exists():
        shutil.rmtree(env)
    env.mkdir(parents=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv binary not found on PATH")
    completed = subprocess.run(
        [
            uv,
            "run",
            "--no-project",
            "--with",
            f"karotte=={KAROTTE_PIN}",
            "python",
            "-c",
            _POPULATE_SNIPPET.replace("@@OUT@@", repr(str(env))),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(env),
        env=_karotte_env(),
    )
    if completed.returncode != 0 and not (env / "pyproject.toml").is_file():
        detail = ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
        raise RuntimeError(f"karotte template populate failed: {detail}")
    post_create_note = None
    if completed.returncode != 0:
        # _populate renders every template file before its post_create hook;
        # that hook only locks the student venvs and fails on this laptop
        # because karotte passes `--exclude-newer-package=karotte=false`, a
        # flag form uv 0.9.24 rejects. The venv locks run below instead.
        post_create_note = (
            "template post_create.py failed (uv --exclude-newer-package flag "
            "form); venv locks run manually: "
            + ((completed.stdout or "") + (completed.stderr or ""))[-500:]
        )
    tasks_root = env / "src" / "environment" / "tasks"
    for stale in ("example_task", "_template", "_template_suite"):
        shutil.rmtree(tasks_root / stale, ignore_errors=True)
    pkg = _pkg_name(adapter.karotte_id)
    pkg_dir = tasks_root / pkg
    pkg_dir.mkdir(parents=True)
    cls = _class_name(adapter.karotte_id)
    submission_doc = ", ".join(adapter.submission_paths) or "(none)"
    (pkg_dir / "__init__.py").write_text(
        _TASK_INIT_TEMPLATE.replace("@@KAROTTE_ID@@", adapter.karotte_id)
        .replace("@@HARBOR_ID@@", task.task_id)
        .replace("@@SUBMISSION_DOC@@", submission_doc)
        .replace("@@SUBMISSION_REPR@@", repr(list(adapter.submission_paths)))
        .replace("@@INSTRUCTIONS_REPR@@", repr(task.instruction))
        .replace("@@STEP_CLASS@@", f"{cls}Step")
        .replace("@@TASK_CLASS@@", f"{cls}Task")
        .replace("@@PKG@@", pkg),
        encoding="utf-8",
    )
    (pkg_dir / "scoring_script.py").write_text(adapter.scoring_source, encoding="utf-8")
    # Per-cell fake model lands here in run_cell (excluded from image digest).
    (env / "src" / "environment" / "fake_model.py").write_text(
        fake_model_source("nop", adapter=adapter), encoding="utf-8"
    )
    student_data = env / "student_data"
    for rel, data in adapter.student_files.items():
        dest = student_data / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    # Drop template placeholders so only task data ships in the image.
    for placeholder in student_data.rglob(".gitkeep"):
        placeholder.unlink(missing_ok=True)
    root_data = env / "root_data"
    for rel, data in adapter.root_files.items():
        dest = root_data / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    for placeholder in root_data.rglob(".gitkeep"):
        placeholder.unlink(missing_ok=True)
    pyproject = env / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pinned = text.replace('"karotte",', f'"karotte=={KAROTTE_PIN}",\n    "pytest==8.4.1",')
    if pinned == text:
        raise RuntimeError("env pyproject.toml has no karotte dependency to pin")
    pyproject.write_text(pinned, encoding="utf-8")
    locked = _uv(["lock"], cwd=env, timeout=600)
    if locked.returncode != 0:
        raise RuntimeError(f"env uv lock failed: {(locked.stderr or '')[-2000:]}")
    for venv_dir in sorted((env / "venvs").iterdir()):
        if (venv_dir / "pyproject.toml").is_file():
            venv_lock = _uv(["lock"], cwd=venv_dir, timeout=600)
            if venv_lock.returncode != 0:
                raise RuntimeError(
                    f"env venv uv lock failed in {venv_dir.name}: "
                    f"{(venv_lock.stderr or '')[-2000:]}"
                )
    synced = _uv(["sync", "--extra", "dev"], cwd=env, timeout=900)
    if synced.returncode != 0:
        raise RuntimeError(f"env uv sync failed: {(synced.stderr or '')[-2000:]}")
    return {
        "env_dir": str(env),
        "task_id": task.task_id,
        "karotte_id": adapter.karotte_id,
        "post_create_note": post_create_note,
        "files": sorted(
            p.relative_to(env).as_posix() for p in env.rglob("*") if p.is_file()
        ),
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


# ---------------------------------------------------------------------------
# run_cell: one task x one scripted agent under karotte custody
# ---------------------------------------------------------------------------


def _bound_container(name: str, *, cpus: str = RUN_CPUS, memory: str = RUN_MEMORY) -> bool:
    """Best-effort ``docker update`` bounds on our own run container by name."""
    try:
        completed = subprocess.run(
            ["docker", "update", "--cpus", cpus, "--memory", memory,
             "--memory-swap", memory, name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_karotte_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _watch_bounds(name: str, stop: threading.Event, record: dict[str, Any]) -> None:
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
            record["bounded"] = _bound_container(name)
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
    if agent in ("oracle", "nop") and attacks:
        return _cell(
            agent, attacks, verdict="error", reward=None,
            reason="attacks only apply to the cheat agent",
            platform_version=KAROTTE_PIN, evidence=None,
        )
    if agent == "cheat":
        try:
            attacks = selected_cheat_attacks(attacks)
        except ValueError as exc:
            return _cell(
                agent, tuple(attacks), verdict="error", reward=None,
                reason=str(exc), platform_version=KAROTTE_PIN, evidence=None,
            )
    try:
        task = load_harbor_task(task_dir)
    except (FileNotFoundError, ValueError, OSError) as exc:
        return _cell(
            agent, attacks, verdict="error", reward=None, reason=str(exc),
            platform_version=KAROTTE_PIN, evidence=None,
        )
    try:
        adapter = adapter_for(task)
    except ValueError as exc:
        return _cell(
            agent, attacks, verdict="error", reward=None, reason=str(exc),
            platform_version=KAROTTE_PIN, evidence=None, task_id=task.task_id,
        )
    ok, daemon_detail = docker_daemon_ok()
    if not ok:
        return _cell(
            agent, attacks, verdict="skipped", reward=None,
            reason=f"docker daemon unreachable: {daemon_detail}",
            platform_version=KAROTTE_PIN, evidence=None, task_id=task.task_id,
        )
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    env_dir = work / "karotte-env"
    run_id = f"evallab-{uuid.uuid4().hex[:8]}"
    container = f"karotte_run_{run_id}"
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = out_dir / f"{run_id}.json"
    evidence_path = out_dir / f"{run_id}.evidence.json"
    version = KAROTTE_PIN
    try:
        assemble_env(task, adapter, env_dir)
        version = resolved_karotte_version(env_dir)
        image = ensure_image(env_dir)
        (env_dir / "src" / "environment" / "fake_model.py").write_text(
            fake_model_source(agent, adapter=adapter, attacks=attacks), encoding="utf-8"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _cell(
            agent, attacks, verdict="error", reward=None,
            reason=f"env setup failed: {exc}", platform_version=version,
            evidence=None, task_id=task.task_id,
        )
    try:
        ladder_src = Path(__file__).resolve().parent / "cheat_ladder.py"
        mounts: list[str] = []
        if agent == "cheat":
            if not ladder_src.is_file():
                return _cell(
                    agent, attacks, verdict="error", reward=None,
                    reason="cheat_ladder.py missing from src/evallab",
                    platform_version=version, evidence=None, task_id=task.task_id,
                )
            staged = work / "cheat_ladder.py"
            staged.write_bytes(ladder_src.read_bytes())
            mounts.append(f"{staged}:{CHEAT_LADDER_MOUNT}:ro")
        config = {
            "run_id": run_id,
            "task_id": adapter.karotte_id,
            "model": "fake/fake",
            "use_fake_model": True,
            "transcript_file": str(transcript_path),
        }
        config_path = work / f"{run_id}.config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        karotte_bin = env_dir / ".venv" / "bin" / "karotte"
        command = [
            str(karotte_bin), "run", "--config", str(config_path),
            "--runtime", "docker", "--no-ui", "--dev",
            "--build-context", str(env_dir),
        ]
        for spec in mounts:
            command += ["--mount", spec]
        bounds: dict[str, Any] = {"bounded": False}
        stop = threading.Event()
        watcher = threading.Thread(
            target=_watch_bounds, args=(container, stop, bounds), daemon=True
        )
        run_timeout = timeout_seconds if timeout_seconds is not None else DEFAULT_RUN_TIMEOUT_SECONDS
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
            agent, attacks, verdict="error", reward=None,
            reason=f"karotte run timed out after {run_timeout}s; {container} stopped",
            platform_version=version, evidence=None, task_id=task.task_id,
        )
    except (OSError, RuntimeError) as exc:
        _remove_own_container(container)
        return _cell(
            agent, attacks, verdict="error", reward=None,
            reason=f"karotte run failed to launch: {exc}",
            platform_version=version, evidence=None, task_id=task.task_id,
        )
    removed = _remove_own_container(container)
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        summary = parse_transcript(payload)
    except (OSError, ValueError) as exc:
        return _cell(
            agent, attacks, verdict="error", reward=None,
            reason=f"no readable karotte transcript: {exc}; run rc={completed.returncode}",
            platform_version=version, evidence=None, task_id=task.task_id,
        )
    reward = summary["reward"]
    evidence_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "container": container,
                "container_removed": removed,
                "container_bounded_2cpu_2g": bounds.get("bounded", False),
                "image": image,
                "daemon": daemon_detail,
                "transcript": str(transcript_path),
                "status": summary["status"],
                "errors": summary["errors"],
                "misbehavior": summary["misbehavior"],
                "scoring": summary["scoring"],
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
            agent, attacks, verdict="error", reward=None,
            reason=f"karotte produced no score ({detail}); run rc={completed.returncode}",
            platform_version=version, evidence=str(evidence_path), task_id=task.task_id,
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
        agent, attacks, verdict=verdict_for_reward(reward), reward=reward,
        reason=reason, platform_version=version, evidence=str(evidence_path),
        task_id=task.task_id,
    )
