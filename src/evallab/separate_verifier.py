"""Separate-verifier transform for MiMo task packages (``separate-verifier@1``).

Converts a shared-mode MiMo task package (``task.toml``, ``environment/``,
``tests/``) so hidden tests are bundled into the verifier environment only,
and the agent's workspace reaches the verifier as a declared artifact.

Mechanism (all Harbor 0.24 behavior cited inline):

* ``[verifier] environment_mode = "separate"`` plus a ``tests/Dockerfile``
  makes Harbor build the verifier image from ``tests/`` and never upload
  ``/tests`` into the agent container (``verifier.py:124-131``
  ``skip_tests_upload`` driven by ``bundled_tests``;
  ``verifier_mode.py:96-116``).
* The task-level ``artifacts`` entry plus ``[[verifier.collect]]`` hooks
  snapshot the agent workspace after the agent phase (``trial.py:1443-1474``,
  ``1475+``). The convention entry (``/logs/artifacts``) is implicit; the
  snapshot lives outside it at :data:`SNAP_DIR` so the explicit declaration
  never collides with the convention entry (a recorded ``skipped`` entry
  would fail regrade coverage in ``regrade.py:430-452``).
* ``tests/test.sh`` becomes a wrapper that restores the snapshot onto the
  pristine checkout in the verifier image and then ``exec``s the original
  grading script (renamed to ``tests/test-orig.sh``) byte-for-byte, so
  grading logic is identical by construction.

The original grading script depends on setup state (``/var/lib/mimo/base``
and the hidden git dir) that only exists in the agent environment, so the
snapshot hook captures that state too.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "separate-verifier@1"

#: Absolute container path of the agent snapshot. Declared in task-level
#: ``artifacts``; re-materialized at the same path in the verifier env
#: ("no translation", ``artifact_handler.py``).
SNAP_DIR = "/var/tmp/mimo-separate"

#: Agent trajectory consumed by verifier-side integrity criteria. Declared in
#: task-level ``artifacts`` so Harbor carries it from the agent environment
#: into the separate verifier at the same absolute path, which is also the
#: integrity runner's default in-image location
#: (``integrity_core.Roots.trajectory_path`` = ``/logs/agent/trajectory.json``).
#: Collection is best-effort (``artifact_handler.py:287-341``): when the agent
#: writes no trajectory (e.g. ``nop``) the manifest records ``failed`` and the
#: trial still grades; the integrity runner then scores from empty inputs.
TRAJECTORY_ARTIFACT = "/logs/agent/trajectory.json"

#: Where the agent-setup state lives in these task images.
MIMO_STATE_DIR = "/var/lib/mimo"

#: Original grading script name after the rename.
ORIG_TEST_SCRIPT = "tests/test-orig.sh"

#: Verifier entry point (must stay the discovered test path).
WRAPPER_TEST_SCRIPT = "tests/test.sh"


@dataclass(frozen=True)
class ParentInfo:
    """Validated facts about the parent package."""

    task_name: str
    workdir: str
    docker_image: str
    has_solution: bool


def read_parent_info(parent_dir: Path | str) -> ParentInfo:
    """Read and validate the parent package; raise :class:`VariantInvalid`."""
    parent = Path(parent_dir)
    try:
        config = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc

    verifier = config.get("verifier")
    if not isinstance(verifier, dict):
        raise VariantInvalid("parent task.toml has no [verifier] table")
    if verifier.get("environment_mode") == "separate" or "environment" in verifier:
        raise VariantInvalid("parent is already in separate-verifier mode")
    if (parent / "tests" / "Dockerfile").exists():
        raise VariantInvalid("parent already has tests/Dockerfile")
    if not (parent / "tests" / "test.sh").is_file():
        raise VariantInvalid("parent has no tests/test.sh")

    environment = config.get("environment")
    if not isinstance(environment, dict):
        raise VariantInvalid("parent task.toml has no [environment] table")
    workdir = environment.get("workdir")
    docker_image = environment.get("docker_image")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise VariantInvalid("parent [environment].workdir must be an absolute path")
    if not isinstance(docker_image, str) or not docker_image:
        raise VariantInvalid("parent [environment].docker_image must be set")

    task = config.get("task")
    if not isinstance(task, dict) or not task.get("name"):
        raise VariantInvalid("parent task.toml has no [task].name")
    return ParentInfo(
        task_name=str(task["name"]),
        workdir=workdir,
        docker_image=docker_image,
        has_solution=(parent / "solution" / "solve.sh").is_file(),
    )


def render_tests_dockerfile(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests.

    The ``--platform`` pin keeps Apple Silicon hosts on the same amd64 image
    the agent runs (Harbor builds with ``--platform`` of the daemon;
    without the pin an amd64-only base fails to build for arm64).
    """
    return (
        "# Separate-verifier image (separate-verifier@1): pristine repo checkout\n"
        "# plus the hidden tests bundled in. The agent image never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh /tests/test-orig.sh\n"
    )


def render_snapshot_hook(workdir: str) -> str:
    """Collect-hook command snapshotting workspace + setup state to SNAP_DIR.

    Runs in the agent environment after the agent phase (``sh -c``; POSIX
    only, no single quotes so it fits a TOML literal string). Captures the
    base commit sha, the git dir (hidden or live), and the full worktree
    except ``.git``. Always exits 0; the verifier wrapper fails loudly when
    files are missing.
    """
    return (
        f'SNAP="{SNAP_DIR}"; CWD="{workdir}"; M="{MIMO_STATE_DIR}"; '
        'mkdir -p "$SNAP"; '
        'cp "$M/base" "$SNAP/base"; '
        'rm -rf "$SNAP/stage"; '
        'if [ -d "$M/git-hidden" ]; then '
        'tar -czf "$SNAP/git-hidden.tgz" -C "$M" git-hidden; '
        f'elif [ -d "$CWD/.git" ]; then '
        'mkdir -p "$SNAP/stage/git-hidden"; '
        'cp -r "$CWD/.git/." "$SNAP/stage/git-hidden/"; '
        'tar -czf "$SNAP/git-hidden.tgz" -C "$SNAP/stage" git-hidden; '
        'rm -rf "$SNAP/stage"; fi; '
        'tar --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" .; '
        'ls -la "$SNAP"; true'
    )


def render_probe_hook(workdir: str, marker: str) -> str:
    """Collect-hook command proving hidden tests are absent from the agent env.

    Lists ``/tests`` and greps the worktree for the hidden-test marker. Runs
    BEFORE the snapshot hook so the snapshot tarballs are not scanned. Output
    lands in the declared snapshot dir, hence in the trial artifacts.
    """
    return (
        f'SNAP="{SNAP_DIR}"; CWD="{workdir}"; '
        'mkdir -p "$SNAP"; '
        '{ echo "== ls /tests =="; ls -la /tests 2>&1; '
        f'echo "== grep marker {marker} =="; '
        f'grep -rl "{marker}" "$CWD" /tmp '
        "--exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv "
        "--exclude-dir=node_modules 2>/dev/null; "
        'echo "grep_rc=$?"; } > "$SNAP/probe-agent-env.txt" 2>&1; '
        'cat "$SNAP/probe-agent-env.txt"; true'
    )


def render_wrapper_test_sh(workdir: str, marker: str) -> str:
    """Verifier entry point: restore the snapshot, run the original grading."""
    return (
        "#!/bin/bash\n"
        "# separate-verifier@1 entry: restore the agent snapshot captured by the\n"
        "# [[verifier.collect]] snapshot hook, then run the original grading\n"
        "# script (tests/test-orig.sh) unmodified.\n"
        "set -u\n"
        f'SNAP="{SNAP_DIR}"\n'
        f'M="{MIMO_STATE_DIR}"\n'
        f'CWD="{workdir}"\n'
        "V=/logs/verifier\n"
        'mkdir -p "$V" "$M"\n'
        "# Verifier-side probe: the marker must be present here (bundled tests).\n"
        "{\n"
        'echo "== marker in /tests ==";\n'
        f'grep -rl "{marker}" /tests 2>/dev/null || echo "MARKER NOT FOUND IN /tests";\n'
        '} > "$V/separate-probe.txt" 2>&1\n'
        'test -f "$SNAP/base" || { echo "snapshot missing: $SNAP/base" >&2; exit 1; }\n'
        'test -f "$SNAP/workspace.tgz" || '
        '{ echo "snapshot missing: $SNAP/workspace.tgz" >&2; exit 1; }\n'
        'cp "$SNAP/base" "$M/base"\n'
        'if [ -f "$SNAP/git-hidden.tgz" ]; then\n'
        '  rm -rf "$CWD/.git" "$M/git-hidden";\n'
        '  tar -xzf "$SNAP/git-hidden.tgz" -C "$M";\n'
        "fi\n"
        'cd "$CWD"\n'
        'find "$CWD" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +\n'
        'tar -xzf "$SNAP/workspace.tgz" -C "$CWD"\n'
        "exec /tests/test-orig.sh\n"
    )


def render_task_toml(parent_text: str, *, snapshot_hook: str, probe_hook: str) -> str:
    """Insert artifacts, separate mode, and collect hooks into task.toml text."""
    lines = parent_text.splitlines(keepends=True)
    try:
        schema_idx = next(i for i, line in enumerate(lines) if line.startswith("schema_version"))
    except StopIteration as exc:
        raise VariantInvalid("parent task.toml has no schema_version line") from exc
    try:
        verifier_idx = next(i for i, line in enumerate(lines) if line.strip() == "[verifier]")
    except StopIteration as exc:
        raise VariantInvalid("parent task.toml has no [verifier] header line") from exc

    lines.insert(
        schema_idx + 1, f'\nartifacts = [\n    "{SNAP_DIR}",\n    "{TRAJECTORY_ARTIFACT}",\n]\n'
    )
    # Indices after verifier_idx shifted by the one insertion before them.
    verifier_idx += 1
    lines.insert(verifier_idx + 1, 'environment_mode = "separate"\n')

    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    text += (
        "\n# separate-verifier@1: snapshot the agent workspace for the verifier.\n"
        "# The probe hook runs first so the snapshot tarballs are not scanned.\n"
        "[[verifier.collect]]\n"
        'service = "main"\n'
        f"command = '{probe_hook}'\n"
        "timeout_sec = 300.0\n"
        'user = "root"\n'
        "\n"
        "[[verifier.collect]]\n"
        'service = "main"\n'
        f"command = '{snapshot_hook}'\n"
        "timeout_sec = 600.0\n"
        'user = "root"\n'
    )
    return text


def build_changes(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    ``solution_sh`` adds an oracle-control reference solution when the parent
    has none. Refuses to overwrite an existing solution.
    """
    if not marker or not marker.strip():
        raise VariantInvalid("marker must be a nonempty hidden-test identifier")
    parent = Path(parent_dir)
    info = read_parent_info(parent)
    if info.has_solution and solution_sh is not None:
        raise VariantInvalid("parent already has solution/solve.sh; refusing overwrite")

    original_test_sh = (parent / "tests" / "test.sh").read_bytes()
    snapshot_hook = render_snapshot_hook(info.workdir)
    probe_hook = render_probe_hook(info.workdir, marker)
    parent_toml_text = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes] = {
        "task.toml": render_task_toml(
            parent_toml_text, snapshot_hook=snapshot_hook, probe_hook=probe_hook
        ).encode("utf-8"),
        "tests/test.sh": render_wrapper_test_sh(info.workdir, marker).encode("utf-8"),
        "tests/test-orig.sh": original_test_sh,
        "tests/Dockerfile": render_tests_dockerfile(info.docker_image).encode("utf-8"),
    }
    if solution_sh is not None:
        changes["solution/solve.sh"] = solution_sh

    inputs: dict[str, Any] = {
        "parent_task": info.task_name,
        "workdir": info.workdir,
        "docker_image": info.docker_image,
        "marker": marker,
        "snapshot_dir": SNAP_DIR,
        "trajectory_artifact": TRAJECTORY_ARTIFACT,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Bundle hidden tests into a separate verifier environment; "
    "pass the agent workspace as a declared artifact.",
    created_by: str = "har169-separate-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@1`` variant of a MiMo task package."""
    changes, inputs = build_changes(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


__all__ = [
    "MIMO_STATE_DIR",
    "ORIG_TEST_SCRIPT",
    "SNAP_DIR",
    "TRANSFORM_ID",
    "TRAJECTORY_ARTIFACT",
    "WRAPPER_TEST_SCRIPT",
    "ParentInfo",
    "build_changes",
    "derive_separate_verifier",
    "read_parent_info",
    "render_probe_hook",
    "render_snapshot_hook",
    "render_task_toml",
    "render_tests_dockerfile",
    "render_wrapper_test_sh",
]
