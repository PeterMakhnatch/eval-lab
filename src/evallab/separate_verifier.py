"""Separate-verifier transforms for MiMo task packages.

The canonical producer is ``separate-verifier@10``; its shell fragments live
under ``data/separate_verifier_v10/``. Earlier generations keep identifiers
and template components used by @10, not duplicate receipt evaluators.
The native ``har169-separate-verifier`` experiment still requires the @1 producer.

``separate-verifier@1`` converts a shared-mode MiMo task package
(``task.toml``, ``environment/``, ``tests/``) so hidden tests are bundled
into the verifier environment only, and the agent's workspace reaches the
verifier as a declared artifact.

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
@1 snapshot hook captures that state too.

``separate-verifier@2`` ("patch-only verifier") closes the grading hole @1
leaves open: the agent is root in its container (and @1 additionally
restores the agent's own ``.git`` and base sha into the verifier), so
tracked-conftest edits, ``sitecustomize``/``PATH`` tamper, background
writers, and in-source ``atexit``/``pytest`` monkeypatches all grade 1
without fixing the bug. The @2 verifier never trusts anything from the
agent environment except repo file bytes: it reruns the bundled clean setup
itself, computes ``BASE`` there, diffs the snapshot files with its own git
under the BASE tree's ignore rules, drops test-infra paths, gates tamper
signatures, then applies the hidden tests and grades with a structured
junit check.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shlex
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.purge_installed_copies import MARKER_V2 as PURGE_INSTALLED_COPIES_MARKER_V2
from evallab.purge_installed_copies import SETUP_PYTHON_PATH
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

#: Verifier entry point (must stay the discovered test path).


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
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
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
    changes: dict[str, bytes | None] = {
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


# --------------------------------------------------------------------------- #
# separate-verifier@2 ("patch-only verifier")
# --------------------------------------------------------------------------- #

#: Transform id recorded in lineage.

#: Subdirectory of the bundled tests holding the readable copy of the
#: parent's clean setup chain (``environment/setup/*``). The @2 grader reruns
#: it in the verifier container so BASE and the pristine checkout never come
#: from the agent environment.
V2_SETUP_SUBDIR = "tests/_verifier-setup"

#: Scratch dir for the @2 grader. The junit report lands here (outside the
#: repo, where the code under test cannot reach it); ``/var/lib/mimo`` itself
#: is setup state (``chmod 700``, rewritten by setup.sh).
V2_GRADE_DIR = "/var/lib/mimo-grade"

#: Setup's system interpreter need not have pip. The existing cache-disabled
#: cleanup branch removes cache directories directly; neither override belongs
#: in the task's persistent environment or its test runner.
SETUP_HELPER_ENV = {"PATH": SETUP_PYTHON_PATH, "PIP_NO_CACHE_DIR": "1"}


def render_verifier_setup_bootstrap(*, tests_dir: str, state_dir: str, grade_dir: str) -> str:
    """Run bundled setup before artifact seeding and restore its tests on exit.

    Setup wipes /tests and /logs. The native backend invokes this only in the
    dedicated verifier, under SETUP_HELPER_ENV, before Harbor uploads submissions.
    """
    bundle_name = PurePosixPath(V2_SETUP_SUBDIR).name
    return f"""set -eu
tests={shlex.quote(tests_dir)}
state={shlex.quote(state_dir)}
grade={shlex.quote(grade_dir)}
mkdir -p "$state" "$grade"
backup=$(mktemp -d "$grade/native-tests.XXXXXX")
cp -a "$tests/." "$backup/"
trap 'status=$?; rm -rf "$tests"; cp -a "$backup/." "$tests" || exit 1; rm -rf "$backup"; exit "$status"' EXIT
cp -a "$backup/{bundle_name}/." "$state/"
bash "$state/setup.sh"
test -f "$state/ready"
echo "native setup: ready marker observed"
"""


#: Tamper signatures gated on added lines of the kept agent change. One
#: Python constant so the list is testable; the rendered grader embeds the
#: same alternation in ``grep -E`` (entries are valid both as Python ``re``
#: and as ERE). Measured: 0 hits on added lines of all 192 HAR-191
#: oracle-pass reference fixes.
TAMPER_SIGNATURES: tuple[str, ...] = (
    r"\b_pytest\b",
    r"\bpytest_(runtest|configure|collection|sessionfinish|sessionstart|report|terminal|unconfigure)",
    r"\bTestReport\b",
    r"junitxml",
    r"\bos\._exit\b",
    r"\batexit\b",
)

#: Exact test-infra filenames dropped from the change (a legitimate fix never
#: touches these).
_TEST_INFRA_BASENAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py"}
)

#: Logged reason when pytest demonstrably ran with our addopts yet no junit
#: report survived: the report was suppressed, not merely unconfigured.
JUNIT_MISSING_REASON = "junit missing after pytest start"

#: How a task command can clear our ``PYTEST_ADDOPTS`` (and with it the
#: junit report): shell unset, direct assignment/override, or ``env -u``.
_ADDOPTS_CLEAR_RE = re.compile(
    r"unset\s+[^\n]*PYTEST_ADDOPTS|PYTEST_ADDOPTS\s*=|env\s+[^\n]*-u\s+PYTEST_ADDOPTS"
)

#: Base64 blobs that may hide the real test command (encoded shell commands
#: are rarely shorter; hex SHAs decode to inert garbage either way).
_B64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _decode_b64_blobs(text: str) -> str:
    """Append decoded base64 blobs found in ``text`` (best-effort)."""
    decoded = [text]
    for token in _B64_BLOB_RE.findall(text):
        try:
            decoded.append(base64.b64decode(token, validate=True).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            continue
    return "\n".join(decoded)


def resolve_command_text(test_command_sh: str, mimo_script: str | None, patch_text: str) -> str:
    """Resolvable text of the task's test command for addopts/pytest checks.

    Concatenates ``test_command.sh``, the ``mimo_test_command.sh`` it points
    at (when available post-patch), and the command section of the hidden
    test patch — decoding base64 blobs so encoded commands are inspected
    too. All inputs are trusted verifier-side bundles, never agent bytes.
    """
    parts = [test_command_sh, mimo_script or "", patch_text.split("mimo_test_command.sh")[-1]]
    return _decode_b64_blobs("\n".join(parts))


def addopts_cleared(command_text: str) -> bool:
    """Whether the resolved test command clears our ``PYTEST_ADDOPTS``."""
    return _ADDOPTS_CLEAR_RE.search(command_text) is not None


def is_test_infra_filename(path: str) -> bool:
    """Whether ``path`` is test infrastructure dropped by name (any directory)."""
    base = PurePosixPath(path).name
    return base in _TEST_INFRA_BASENAMES or base.endswith(".pth") or base.endswith("_test.go")


def drop_reason(path: str, hidden_paths: Collection[str]) -> str | None:
    """Name-based drop reason for ``path`` (``None`` keeps it).

    Covers test-infra filenames and every path named in the hidden test
    patch. Content-based drops (pytest-config hunks, Go ``TestMain``) need
    file bytes; see :func:`is_pytest_config_tamper` and
    :func:`declares_testmain`.
    """
    if is_test_infra_filename(path):
        return "test-infra"
    if path in hidden_paths:
        return "hidden-test path"
    return None


# --------------------------------------------------------------------------- #
# separate-verifier@4 ("rootdir-robust junit matching")
# --------------------------------------------------------------------------- #
# @4 keeps every @3 guarantee and changes only the pytest named-id presence
# check: @3 exact-matches expected node ids (from test.patch/command, e.g.
# ``tests/unit/test_x.py::test_y``) against junit-classname-derived ids, so a
# pytest rootdir under ``tests/`` (e.g. the cloud-sql-connector family with
# rootdir=/testbed/tests) yields classnames without the ``tests/`` segment
# and grades a fully passing oracle 0 (missing=all). @4 matches expected ids
# to junit cases by aligned path-suffix instead: module path components
# compared from the right at '/' or '.' boundaries (either side may carry an
# extra prefix), plus exact class-chain and test-name agreement (parametrize
# ids keep the @3 prefix rule). No @2/@3 symbol or template byte changes, so
# @2/@3 records stay valid. See docs/mimo/separate-verifier.md.

#: Transform id recorded in lineage.


def render_snapshot_hook_v2(workdir: str, *, snap_dir: str = SNAP_DIR) -> str:
    """Collect-hook command snapshotting workspace files only (``sh -c``; POSIX
    only, no single quotes so it fits a TOML literal string).

    Unlike @1 this captures neither ``/var/lib/mimo/base`` nor the git dir:
    the @2 verifier reruns the clean setup itself and computes BASE there.
    Only repo file bytes cross the boundary.

    The tarball covers exactly the files the grader can see: tracked files
    plus untracked files the worktree does not ignore (``git ls-files -c -o
    --exclude-standard``). Regenerable dependency and cache trees
    (``node_modules``, ``.venv``, ``.yarn/cache``, ``__pycache__``, build
    outputs — anything the repo ignores) never enter the archive. This is
    grading-neutral by construction: the @2+ grader diffs the snapshot with
    ``git add -A`` under the BASE tree ignore rules, which never stages
    ignored files either, and it reinstalls dependencies by rerunning the
    bundled clean setup. Source trees that are tracked or simply not
    ignored (``packages/``, ``vendor/``, new files the agent adds) still
    cross intact. The @1 hook keeps its full-worktree tar: its grader
    restores the snapshot verbatim and never reruns setup, so it genuinely
    needs the installed dependencies.

    Outside a git worktree (no ``.git``) the listing fails and the hook
    falls back to the full-worktree tar minus ``.git``.
    """
    return (
        f'SNAP="{snap_dir}"; CWD="{workdir}"; '
        'mkdir -p "$SNAP"; '
        'LIST="$SNAP/files"; '
        'git -C "$CWD" ls-files -c -o --exclude-standard -z > "$LIST" 2>/dev/null; '
        'if [ -s "$LIST" ]; then '
        'tar --null --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" --files-from="$LIST"; '
        'else '
        'tar --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" .; '
        'fi; '
        'ls -la "$SNAP"; true'
    )


def collect_verifier_setup_files(parent_dir: Path | str) -> dict[str, bytes]:
    """Bundle the parent's clean setup chain (``environment/setup/*``) for the verifier.

    Returns ``tests/_verifier-setup/<relpath>`` bytes for ``derive_task``.
    Fail-closed: a missing ``setup.sh`` or a symlinked entry refuses, since
    the grader could not reproduce the agent's setup.
    """
    setup_dir = Path(parent_dir) / "environment" / "setup"
    if not (setup_dir / "setup.sh").is_file() or (setup_dir / "setup.sh").is_symlink():
        raise VariantInvalid("parent has no environment/setup/setup.sh for the @2 bundle")
    if (Path(parent_dir) / V2_SETUP_SUBDIR).exists():
        raise VariantInvalid(f"parent already has {V2_SETUP_SUBDIR}; refusing overwrite")
    bundled: dict[str, bytes] = {}
    for path in sorted(setup_dir.rglob("*")):
        if path.is_symlink():
            raise VariantInvalid(f"setup bundle entry is a symlink: {path.name}")
        if path.is_file():
            bundled[f"{V2_SETUP_SUBDIR}/{path.relative_to(setup_dir).as_posix()}"] = (
                path.read_bytes()
            )
    if not bundled:
        raise VariantInvalid("parent environment/setup/ is empty; nothing to bundle")
    return bundled


# --------------------------------------------------------------------------- #
# separate-verifier@3 ("multi-runner patch-only verifier")
# --------------------------------------------------------------------------- #
# @3 keeps every @2 guarantee for Python/pytest tasks and extends the same
# patch-only shape to the non-Python runners measured over the 1,519
# non-Python code tasks (task store
# ``derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks``;
# census: go-test 718 incl. opaque-wrap, jest 135, mocha 113, usecase-sh 81,
# node 50+12, vitest 37, tap/ava/karma/jasmine ~40, pytest 14, make/ctest 11,
# phpunit 7+13 opaque, rspec 4+16 opaque, junit-mvn/gradle 3+21 opaque,
# cargo 1+5 opaque, forge 3, long tail ~120 incl. 532 fully opaque
# build-env wrappers whose real command resolves only at grade time).
#
# Semantics change vs @2, so this is a new transform id; no @2 symbol or
# template byte changes, and @2 records stay valid. See
# docs/mimo/separate-verifier.md for the per-language table.

#: Transform id recorded in lineage.

#: New-file runner configs dropped from the kept agent change when the path
#: is absent at BASE (an honest fix never adds runner configuration). The
#: rendered grader embeds the same list as a shell ``case`` (see
#: :func:`render_wrapper_test_sh_v3`); keep the two in sync (pinned by
#: ``test_v3_new_infra_list_matches_grader``).
V3_NEW_INFRA_BASENAMES = frozenset(
    {
        "jest.config.js",
        "jest.config.cjs",
        "jest.config.mjs",
        "jest.config.ts",
        "jest.config.json",
        "jest.setup.js",
        "jest.setup.cjs",
        "jest.setup.mjs",
        "jest.setup.ts",
        "jest.preset.js",
        "vitest.config.js",
        "vitest.config.ts",
        "vitest.config.mts",
        "vitest.setup.js",
        "vitest.setup.ts",
        "vitest.workspace.js",
        "vitest.workspace.ts",
        "vitest.shared.js",
        "vitest.shared.ts",
        "ava.config.js",
        "ava.config.cjs",
        "ava.config.mjs",
        "karma.conf.js",
        ".mocharc.js",
        ".mocharc.cjs",
        ".mocharc.mjs",
        ".mocharc.json",
        ".mocharc.yml",
        ".mocharc.yaml",
        ".taprc",
        ".nycrc",
        ".nycrc.json",
        "jasmine.json",
        "build.rs",
        "phpunit.xml",
        "phpunit.xml.dist",
        "phpunit.dist.xml",
        ".rspec",
    }
)

#: Build-output dirs dropped from the kept change when absent at BASE (never
#: fix content; may carry pre-planted structured reports).
V3_BUILD_OUTPUT_DIR_RES = (
    r"(^|/)target/surefire-reports/",
    r"(^|/)target/failsafe-reports/",
    r"(^|/)build/test-results/",
    r"(^|/)\.nyc_output/",
    r"(^|/)coverage/",
)
_V3_BUILD_OUTPUT_RES_COMPILED = tuple(re.compile(p) for p in V3_BUILD_OUTPUT_DIR_RES)


def is_v3_new_infra(path: str, *, in_base: bool) -> bool:
    """Whether ``path`` is @3 test infrastructure dropped as a new file.

    Only new files (absent at BASE) are dropped: tracked runner configs are
    handled by :func:`v3_config_revert_reason` so honest tracked edits
    survive unless their changed lines touch runner keys.
    """
    if in_base:
        return False
    if PurePosixPath(path).name in V3_NEW_INFRA_BASENAMES:
        return True
    return any(rx.search(path) for rx in _V3_BUILD_OUTPUT_RES_COMPILED)


#: Tracked configs reverted to BASE when the kept change's changed lines
#: touch runner keys: (rule label, basenames, key regex). An honest fix
#: never flips these keys (false positives measured against reference
#: fixes; see docs/mimo/separate-verifier.md).
V3_CONFIG_HUNK_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "package.json runner key",
        ("package.json",),
        r'"(jest|mocha|vitest|ava|tap|karma|jasmine|nyc|c8)"\s*:',
    ),
    ("pom.xml surefire", ("pom.xml",), r"surefire|failsafe|skipTests|<excludes?>"),
    (
        "gradle test block",
        ("build.gradle", "build.gradle.kts"),
        r"exclude|ignoreFailures|useJUnit|test\s*\{",
    ),
    ("rspec options", (".rspec",), r"--require|--format|--tag|--exclude-pattern"),
    (
        "phpunit config",
        ("phpunit.xml", "phpunit.xml.dist", "phpunit.dist.xml"),
        r"bootstrap|suffix=|exclude|executionOrder|printer",
    ),
    ("go toolchain", ("go.mod",), r"toolchain"),
    (
        "mocha options",
        (
            ".mocharc.js",
            ".mocharc.cjs",
            ".mocharc.mjs",
            ".mocharc.json",
            ".mocharc.yml",
            ".mocharc.yaml",
        ),
        r"spec|require|ignore|exclude|grep|invert|reporter",
    ),
)
_V3_CONFIG_HUNK_COMPILED = tuple(
    (label, frozenset(names), re.compile(pattern, re.IGNORECASE))
    for label, names, pattern in V3_CONFIG_HUNK_RULES
)


def v3_config_revert_reason(path: str, changed_lines: str) -> str | None:
    """Rule label when a tracked config change touches runner keys (revert to BASE)."""
    base = PurePosixPath(path).name
    for label, names, pattern in _V3_CONFIG_HUNK_COMPILED:
        if base in names and pattern.search(changed_lines):
            return label
    return None


#: Cross-language tamper signatures gated on added lines of the kept agent
#: change (universal: any path). Extends :data:`TAMPER_SIGNATURES`; every
#: entry is valid both as Python ``re`` and as ERE for the grader's
#: embedded python gate. False positives measured against reference fixes
#: (docs/mimo/separate-verifier.md).
V3_TAMPER_SIGNATURES: tuple[str, ...] = TAMPER_SIGNATURES + (
    r"\bos\.Exit\s*\(",
    r"\bprocess\.exit\s*\(",
    r"\bprocess\.exitCode\s*=",
    r"\bDeno\.exit\s*\(",
    r"\bSystem\.exit\s*\(",
    r"\bRuntime\.getRuntime\(\)\.halt\s*\(",
    r"process::exit\s*\(",
    r"\bstd::process::exit",
    r"\bKernel\.exit\b",
    r"\bglobal\.(it|test|describe|fit)\s*=",
)

#: Scoped signatures: ``exit(0)`` is legitimate C/shell fix content, so it
#: only gates Ruby/PHP paths (added lines of files with these extensions).
V3_SCOPED_SIGNATURES: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".rb",), r"\bexit\s*\(\s*0\s*\)|\bexit!\s*\(\s*0?\s*\)"),
    ((".php",), r"\bexit\s*\(\s*0\s*\)|\bdie\s*\(\s*0\s*\)"),
)
_V3_SCOPED_COMPILED = tuple(
    (frozenset(exts), re.compile(pattern)) for exts, pattern in V3_SCOPED_SIGNATURES
)


def v3_tamper_hit_for_file(path: str, added_lines: str) -> str | None:
    """First @3 tamper signature matching a file's added lines (``None`` keeps)."""
    for signature in V3_TAMPER_SIGNATURES:
        if re.search(signature, added_lines):
            return signature
    ext = PurePosixPath(path).suffix.lower()
    for exts, pattern in _V3_SCOPED_COMPILED:
        if ext in exts and pattern.search(added_lines):
            return pattern.pattern
    return None


# --------------------------------------------------------------------------- #
# @3 runner detection and structured output grading
# --------------------------------------------------------------------------- #

#: Runner ids detected from the resolved test command (first match wins).
#: ``custom`` and silent families keep the exit-code fallback.
V3_RUNNER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("go-test", re.compile(r"(?<![\w])go\s+test\b|GO_BIN\"\s+test\b|\$GO_BIN\"\s+test")),
    ("jest", re.compile(r"(?<![\w])jest\b")),
    ("vitest", re.compile(r"(?<![\w])vitest\b")),
    ("mocha", re.compile(r"(?<![\w])mocha\b|_mocha\b")),
    ("ava", re.compile(r"(?<![\w])ava\b")),
    ("tap", re.compile(r"(?<![\w])tap\b")),
    ("karma", re.compile(r"(?<![\w])karma\b")),
    ("jasmine", re.compile(r"(?<![\w])jasmine\b")),
    ("node-test", re.compile(r"node\s+--test\b")),
    ("cargo-test", re.compile(r"cargo\s+test\b")),
    ("rspec", re.compile(r"(?<![\w])rspec\b")),
    ("phpunit", re.compile(r"(?<![\w])phpunit\b")),
    ("mvn", re.compile(r"(?<![\w])mvn\b|surefire|failsafe")),
    ("gradle", re.compile(r"(?<![\w])gradlew?\b")),
    ("pytest", re.compile(r"(?<![\w])pytest\b")),
    ("unittest", re.compile(r"-m\s+unittest\b")),
    ("forge", re.compile(r"(?<![\w])forge\s+test\b")),
    ("node-run", re.compile(r"(?<![\w])node\b")),
    ("make", re.compile(r"(?<![\w])(make|ctest|cmake)\b")),
    ("bats", re.compile(r"(?<![\w])bats\b")),
)


def detect_runner(command_text: str) -> str:
    """Detect the test runner from resolved command text (``custom`` fallback)."""
    for runner, pattern in V3_RUNNER_PATTERNS:
        if pattern.search(command_text):
            return runner
    return "custom"


_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) test")
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)
_UNITTEST_BAD_RE = re.compile(r"^(FAILED|ERROR)", re.MULTILINE)


def evaluate_unittest(test_output: str, rc: int) -> int:
    """Grade 1/0 from unittest text output plus the exit code.

    A real unittest run always prints ``Ran N tests`` and ``OK``/``FAILED``
    to stderr, so reward 1 iff ``rc == 0`` with ``N >= 1``, an ``OK`` line,
    and no failure marker. Blank or exit-forced output grades 0.
    """
    match = _UNITTEST_RAN_RE.search(test_output)
    ran = int(match.group(1)) if match else 0
    if rc != 0:
        return 0
    if ran < 1 or not _UNITTEST_OK_RE.search(test_output):
        return 0
    if _UNITTEST_BAD_RE.search(test_output):
        return 0
    return 1


#: Verifier-owned pytest hook for commands that clear PYTEST_ADDOPTS (8
#: Python tasks, e.g. 000666, all of which also set
#: ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` and clear ``PYTEST_PLUGINS``). A root
#: ``conftest.py`` is always collected (not a plugin: autoload switches do
#: not affect it), so appending this hook after the hidden-test apply gives
#: a junit report no env scrubbing can remove. Appended, never clobbering a
#: hidden-test conftest.py. Writes outside the repo where code under test
#: cannot reach. Embedded base64-encoded in the grader.
V3_CONFTEST_HOOK = (
    "# separate-verifier@3 hook: structured report for addopts-cleared runs.\n"
    "import os as _v3_os\n"
    "_V3_JUNIT_PATH = _v3_os.environ.get("
    '"MIMO_VERIFIER_JUNIT", "/var/lib/mimo-grade/junit.xml")\n'
    "_V3_COLLECTED = []\n"
    "def pytest_runtest_logreport(report):\n"
    '    if report.when == "call" or (report.when == "setup" and report.skipped):\n'
    "        _V3_COLLECTED.append((report.nodeid, report.outcome))\n"
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        import xml.etree.ElementTree as ET\n"
    '        suite = ET.Element("testsuite", name="verifier", tests=str(len(_V3_COLLECTED)))\n'
    "        for nodeid, outcome in _V3_COLLECTED:\n"
    '            name = nodeid.split("::")[-1]\n'
    '            case = ET.SubElement(suite, "testcase", classname=nodeid, name=name)\n'
    '            if outcome == "failed":\n'
    '                ET.SubElement(case, "failure", message="failed")\n'
    '            elif outcome == "skipped":\n'
    '                ET.SubElement(case, "skipped", message="skipped")\n'
    "        ET.ElementTree(suite).write(_V3_JUNIT_PATH)\n"
    "    except Exception:\n"
    "        pass\n"
)

#: Verifier-owned pytest hook archiving each session's collected outcomes to
#: a counter-suffixed report under ``MIMO_VERIFIER_ARCHIVE_DIR``. Appended to
#: the root conftest.py for every pytest run after the hidden-test apply
#: (never clobbering); a root conftest is always collected, so env scrubbing
#: cannot remove it. Best effort: any failure is swallowed so a hook problem
#: can never break grading. Embedded base64-encoded in the grader.
V5_ARCHIVE_HOOK = (
    "# mimo-junit-archive/1: per-phase junit archive.\n"
    "import os as _v5_os\n"
    '_V5_DIR = _v5_os.environ.get("MIMO_VERIFIER_ARCHIVE_DIR", "")\n'
    "_V5_COLLECTED = []\n"
    "def pytest_runtest_logreport(report):\n"
    '    if report.when == "call" or (report.when == "setup" and report.skipped):\n'
    "        _V5_COLLECTED.append((report.nodeid, report.outcome))\n"
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        if not _V5_DIR:\n"
    "            return\n"
    "        _v5_os.makedirs(_V5_DIR, exist_ok=True)\n"
    '        _v5_count_path = _v5_os.path.join(_V5_DIR, "count")\n'
    "        try:\n"
    '            with open(_v5_count_path, encoding="utf-8") as _v5_f:\n'
    '                _v5_n = int((_v5_f.read() or "0").strip())\n'
    "        except Exception:\n"
    "            _v5_n = 0\n"
    "        import xml.etree.ElementTree as _v5_ET\n"
    '        _v5_suite = _v5_ET.Element("testsuite", name="verifier-archive", tests=str(len(_V5_COLLECTED)))\n'
    "        for _v5_nodeid, _v5_outcome in _V5_COLLECTED:\n"
    '            _v5_parts = _v5_nodeid.split("::")\n'
    "            _v5_file = _v5_parts[0]\n"
    "            _v5_rest = _v5_parts[1:]\n"
    '            _v5_mod = _v5_file.replace("/", ".").replace(chr(92), ".")\n'
    '            if _v5_mod.endswith(".py"):\n'
    "                _v5_mod = _v5_mod[:-3]\n"
    "            if len(_v5_rest) > 1:\n"
    '                _v5_cn = (_v5_mod + "." + ".".join(_v5_rest[:-1])) if _v5_mod else ".".join(_v5_rest[:-1])\n'
    "                _v5_name = _v5_rest[-1]\n"
    "            elif _v5_rest:\n"
    "                _v5_cn = _v5_mod\n"
    "                _v5_name = _v5_rest[0]\n"
    "            else:\n"
    "                _v5_cn = _v5_mod\n"
    "                _v5_name = _v5_nodeid\n"
    '            _v5_case = _v5_ET.SubElement(_v5_suite, "testcase", classname=_v5_cn, name=_v5_name, file=_v5_file)\n'
    '            if _v5_outcome == "failed":\n'
    '                _v5_ET.SubElement(_v5_case, "failure", message="failed")\n'
    '            elif _v5_outcome == "skipped":\n'
    '                _v5_ET.SubElement(_v5_case, "skipped", message="skipped")\n'
    '        _v5_ET.ElementTree(_v5_suite).write(_v5_os.path.join(_V5_DIR, "junit-hook-%d.xml" % _v5_n))\n'
    '        with open(_v5_count_path, "w", encoding="utf-8") as _v5_f:\n'
    "            _v5_f.write(str(_v5_n + 1))\n"
    "    except Exception:\n"
    "        pass\n"
)

#: Transform id recorded in lineage.
TRANSFORM_ID_V6 = "separate-verifier@6"


#: Transform id recorded in lineage.
TRANSFORM_ID_V8 = "separate-verifier@8"


# --------------------------------------------------------------------------- #
# separate-verifier@10: binary-artifact strip, runtime integrity, junit unittest
# --------------------------------------------------------------------------- #
# Closes the three holes Traces proved on canonical @8 packages
# (2026-10-14 own-runs scan, finding 1):
# 1. An unchecked-hash ``.pyc`` in ``__pycache__`` rode the kept snapshot
#    unscanned (the tamper gate reads only text diff lines) and shadowed
#    pristine source at grade time. @10 drops binary artifacts from the
#    kept change, purges ``__pycache__``/``*.pyc`` before the agent run,
#    and grades with ``PYTHONDONTWRITEBYTECODE=1`` plus a verifier-owned
#    ``PYTHONPYCACHEPREFIX``.
# 2. Appending ``pytest.Function.runtest = lambda self: None`` (plain or
#    obfuscated) to an imported package ``__init__`` turned every test
#    body into a no-op; junit then honestly reported all-pass. @10
#    installs a verifier conftest hook that captures
#    ``pytest.Function.runtest`` / ``_pytest.python.Function.runtest`` /
#    ``unittest.TestCase.run`` at conftest import and, at session finish,
#    records whether any identity changed; a change grades 0.
# 3. Unittest was graded from stdout text, so printing ``Ran 1 test`` /
#    ``OK`` and exiting 0 at import scored 1 with no test run. @10 grades
#    unittest only from a structured junit report written by a
#    verifier-owned ``sitecustomize`` shim (loaded via ``PYTHONPATH`` at
#    interpreter startup, before test modules import the package), with
#    the @6 fail-to-pass comparison over junit status maps. No junit
#    cases, no reward. The shim also performs the ``TestCase.run``
#    integrity check for pure-unittest commands.
# No @2-@9 symbol or template byte changes. See
# docs/mimo/separate-verifier.md.

#: Transform id recorded in lineage.
TRANSFORM_ID_V10 = "separate-verifier@10"

#: Binary/build-artifact suffixes dropped from the kept agent change (@10).
BINARY_ARTIFACT_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib", ".o", ".a", ".class"}
)


def is_binary_artifact_path(path: str) -> bool:
    """Whether a kept-change path is a binary artifact dropped at @10.

    Covers ``__pycache__`` components and compiled-object suffixes; an
    honest source fix never needs to ship precompiled bytes, and a
    tracked pristine binary simply stays at its BASE bytes.
    """
    pure = PurePosixPath(path)
    if "__pycache__" in pure.parts:
        return True
    return pure.suffix.lower() in BINARY_ARTIFACT_SUFFIXES




#: Verifier-owned conftest hook (@10): captures test-runner internals at
#: conftest import and records identity changes at session finish to
#: ``$MIMO_VERIFIER_INTEGRITY_DIR/integrity.txt``. Chained after any
#: previously appended ``pytest_sessionfinish`` (the @5 archiver) so both
#: run. Best effort like the other hooks: a hook failure never breaks the
#: test session; the grader treats a missing file as "not changed" and
#: the structured junit rules still apply.
V10_INTEGRITY_HOOK = (
    "# mimo-integrity/1: runner-internal integrity check.\n"
    "import os as _v10_os\n"
    '_V10_DIR = _v10_os.environ.get("MIMO_VERIFIER_INTEGRITY_DIR", "")\n'
    "_V10_ORIG = {}\n"
    "try:\n"
    "    import pytest as _v10_pytest\n"
    '    _V10_ORIG["pytest.Function.runtest"] = _v10_pytest.Function.runtest\n'
    "except Exception:\n"
    "    _v10_pytest = None\n"
    "try:\n"
    "    import _pytest.python as _v10_pp\n"
    '    _V10_ORIG["_pytest.python.Function.runtest"] = _v10_pp.Function.runtest\n'
    "except Exception:\n"
    "    _v10_pp = None\n"
    "try:\n"
    "    import unittest as _v10_ut\n"
    '    _V10_ORIG["unittest.TestCase.run"] = _v10_ut.TestCase.run\n'
    "except Exception:\n"
    "    _v10_ut = None\n"
    '_v10_prev_sessionfinish = globals().get("pytest_sessionfinish")\n'
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    try:\n"
    "        if _v10_prev_sessionfinish is not None:\n"
    "            _v10_prev_sessionfinish(session, exitstatus)\n"
    "    except Exception:\n"
    "        pass\n"
    "    try:\n"
    "        if not _V10_DIR:\n"
    "            return\n"
    "        _v10_changed = []\n"
    '        if _v10_pytest is not None and _v10_pytest.Function.runtest is not _V10_ORIG.get("pytest.Function.runtest"):\n'
    '            _v10_changed.append("pytest.Function.runtest")\n'
    '        if _v10_pp is not None and _v10_pp.Function.runtest is not _V10_ORIG.get("_pytest.python.Function.runtest"):\n'
    '            _v10_changed.append("_pytest.python.Function.runtest")\n'
    '        if _v10_ut is not None and _v10_ut.TestCase.run is not _V10_ORIG.get("unittest.TestCase.run"):\n'
    '            _v10_changed.append("unittest.TestCase.run")\n'
    "        _v10_os.makedirs(_V10_DIR, exist_ok=True)\n"
    '        _v10_text = ("CHANGED " + ",".join(_v10_changed)) if _v10_changed else "OK"\n'
    '        with open(_v10_os.path.join(_V10_DIR, "integrity.txt"), "w", encoding="utf-8") as _v10_f:\n'
    '            _v10_f.write(_v10_text + "\\n")\n'
    "    except Exception:\n"
    "        pass\n"
)

#: Verifier-owned ``sitecustomize`` shim (@10): loaded at interpreter
#: startup via ``PYTHONPATH`` for every test-command python. Records
#: unittest outcomes into a junit report at ``MIMO_VERIFIER_UNITTEST_JUNIT``
#: (written at exit, so an import-time ``sys.exit(0)`` yields an empty
#: report) and checks ``unittest.TestCase.run`` /
#: ``TextTestRunner.run`` identities captured at startup. Wraps only
#: ``TextTestResult`` outcome methods; failures are swallowed so the shim
#: can never break an honest run (the grader then simply sees no cases).
V10_UNITTEST_SHIM = (
    "# mimo-unittest-shim/1: structured unittest outcomes + integrity.\n"
    "import atexit as _v10_atexit\n"
    "import os as _v10_os\n"
    "import re as _v10_re\n"
    "_V10_CASES = []\n"
    "_V10_UT = None\n"
    "_V10_ORIG_TC = None\n"
    "_V10_ORIG_RUNNER = None\n"
    "try:\n"
    "    import unittest as _V10_UT\n"
    "    import unittest.runner as _v10_runner\n"
    "    _V10_ORIG_TC = _V10_UT.TestCase.run\n"
    "    _V10_ORIG_RUNNER = _v10_runner.TextTestRunner.run\n"
    "    _v10_result = _v10_runner.TextTestResult\n"
    "    def _v10_wrap(name, status):\n"
    "        _orig = getattr(_v10_result, name)\n"
    "        def _wrapped(self, test, *args, **kwargs):\n"
    "            _V10_CASES.append((str(test), status))\n"
    "            return _orig(self, test, *args, **kwargs)\n"
    "        setattr(_v10_result, name, _wrapped)\n"
    '    _v10_wrap("addSuccess", "passed")\n'
    '    _v10_wrap("addFailure", "failed")\n'
    '    _v10_wrap("addError", "failed")\n'
    '    _v10_wrap("addSkip", "skipped")\n'
    '    _v10_wrap("addExpectedFailure", "skipped")\n'
    '    _v10_wrap("addUnexpectedSuccess", "failed")\n'
    "except Exception:\n"
    "    _V10_UT = None\n"
    "def _v10_flush():\n"
    "    try:\n"
    '        _idir = _v10_os.environ.get("MIMO_VERIFIER_INTEGRITY_DIR", "")\n'
    "        if _idir and _V10_UT is not None:\n"
    "            _changed = []\n"
    "            if _V10_UT.TestCase.run is not _V10_ORIG_TC:\n"
    '                _changed.append("unittest.TestCase.run")\n'
    "            import unittest.runner as _v10_rr\n"
    "            if _v10_rr.TextTestRunner.run is not _V10_ORIG_RUNNER:\n"
    '                _changed.append("unittest.TextTestRunner.run")\n'
    "            _v10_os.makedirs(_idir, exist_ok=True)\n"
    '            _text = ("CHANGED " + ",".join(_changed)) if _changed else "OK"\n'
    '            with open(_v10_os.path.join(_idir, "integrity-unittest.txt"), "w", encoding="utf-8") as _f:\n'
    '                _f.write(_text + "\\n")\n'
    "    except Exception:\n"
    "        pass\n"
    "    try:\n"
    '        _out = _v10_os.environ.get("MIMO_VERIFIER_UNITTEST_JUNIT", "")\n'
    "        if not _out:\n"
    "            return\n"
    "        import xml.etree.ElementTree as _ET\n"
    '        _suite = _ET.Element("testsuite", name="verifier-unittest", tests=str(len(_V10_CASES)))\n'
    "        for _label, _status in _V10_CASES:\n"
    '            _m = _v10_re.match(r"^(.*?)\\s*\\(([^()]*)\\)$", _label)\n'
    "            if _m:\n"
    "                _name = _m.group(1)\n"
    "                _cn = _m.group(2)\n"
    "            else:\n"
    "                _name = _label\n"
    '                _cn = ""\n'
    '            _case = _ET.SubElement(_suite, "testcase", classname=_cn, name=_name)\n'
    '            if _status == "failed":\n'
    '                _ET.SubElement(_case, "failure", message="failed")\n'
    '            elif _status == "skipped":\n'
    '                _ET.SubElement(_case, "skipped", message="skipped")\n'
    "        _v10_os.makedirs(_v10_os.path.dirname(_out), exist_ok=True)\n"
    "        _ET.ElementTree(_suite).write(_out)\n"
    "    except Exception:\n"
    "        pass\n"
    "_v10_atexit.register(_v10_flush)\n"
)


#: @10 agent run line: bytecode off, verifier pycache prefix, unittest shim.
_V10_RUN_LINE = (
    'MIMO_VERIFIER_ARCHIVE_DIR="$JUNIT_PHASES" '
    'MIMO_VERIFIER_UNITTEST_JUNIT="$GRADE/unittest-junit.xml" '
    'MIMO_VERIFIER_INTEGRITY_DIR="$GRADE" '
    'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$GRADE/pycache" '
    'PYTHONPATH="$GRADE/v10shim${PYTHONPATH:+:$PYTHONPATH}" '
    'PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" '
    'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1'
)

_V10_TEMPLATE_DIR = Path(__file__).resolve().parent / "data" / "separate_verifier_v10"
#: @10 grader entry point fragments (loaded from static shell templates).
_V10_WRAPPER_A = (_V10_TEMPLATE_DIR / "part_a.sh").read_text(encoding="utf-8")
_V10_WRAPPER_B = (_V10_TEMPLATE_DIR / "part_b.sh").read_text(encoding="utf-8")
_V10_WRAPPER_C = (_V10_TEMPLATE_DIR / "part_c.sh").read_text(encoding="utf-8")


#: Supplementary behavioral/mutant-killing phases a repaired package appends after its base run:
#:
#:     timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
#:     RC=$?
#:     if [ "$RC" -eq 0 ]; then
#:       ( cd "$CWD" && cd /testbed && PYTHONPATH=/testbed python3 /tests/test_outputs.py ) >> "$V/test_output.log" 2>&1
#:       RC=$?
#:     fi
#:
#: The separate verifier rebuilds the grading command from ``mimo_build_env``, so without this extraction those extra
#: phases are silently dropped and a mutant survives the behavioral repair.
_SUPPLEMENTARY_BLOCK = re.compile(
    r'if \[ "\$RC" -eq 0 \]; then\n\s*(?P<cmd>[^\n]+?)\s*>>\s*"\$V/test_output\.log"\s*2>&1\n\s*RC=\$\?\nfi'
)


def supplementary_test_blocks(parent_dir: Path | str, *, workdir: str | None = None) -> list[str]:
    """Commands from the parent ``tests/test.sh`` that run after the base test command.

    ``/testbed`` and ``$CWD`` are rewritten to the verifier's own workdir so the block runs against the pristine
    checkout instead of a path that does not exist in the verifier container.
    """
    parent = Path(parent_dir)
    try:
        text = (parent / "tests" / "test.sh").read_text(encoding="utf-8")
    except OSError:
        return []
    target = workdir or _workdir_from_toml(parent)
    blocks = []
    for match in _SUPPLEMENTARY_BLOCK.finditer(text):
        command = match.group("cmd").strip()
        if target:
            command = command.replace("/testbed", target).replace('"$CWD"', f'"{target}"')
        blocks.append(command)
    return blocks


def _workdir_from_toml(parent: Path) -> str | None:
    try:
        config = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    workdir = config.get("environment", {}).get("workdir")
    return workdir if isinstance(workdir, str) and workdir.startswith("/") else None


def render_wrapper_test_sh_v10(
    workdir: str, *, python_setup: bool = False, extra_blocks: tuple[str, ...] = ()
) -> str:
    """Verifier entry point: @10 binary strip, integrity gate, junit unittest.

    Only a bundled Python purge setup needs system-Python helper lookup.
    Keep that PATH local to the setup subprocess: project tests may need the
    image's configured Python 3.6 interpreter.
    """
    if not workdir.startswith("/"):
        raise VariantInvalid("workdir must be an absolute path")
    universal = "[" + ", ".join(repr(s) for s in V3_TAMPER_SIGNATURES) + "]"
    scoped = (
        "["
        + ", ".join(f"({sorted(exts)!r}, {pattern!r})" for exts, pattern in V3_SCOPED_SIGNATURES)
        + "]"
    )
    hook_b64 = base64.b64encode(V3_CONFTEST_HOOK.encode("utf-8")).decode("ascii")
    archive_b64 = base64.b64encode(V5_ARCHIVE_HOOK.encode("utf-8")).decode("ascii")
    integrity_b64 = base64.b64encode(V10_INTEGRITY_HOOK.encode("utf-8")).decode("ascii")
    shim_b64 = base64.b64encode(V10_UNITTEST_SHIM.encode("utf-8")).decode("ascii")
    wrapper_a = _V10_WRAPPER_A
    if python_setup:
        wrapper_a = wrapper_a.replace(
            'if ! bash "$M/setup.sh"',
            f'if ! PATH="{SETUP_PYTHON_PATH}" bash "$M/setup.sh"',
            1,
        )
    text = (wrapper_a + _V10_WRAPPER_B + _V10_WRAPPER_C)
    if extra_blocks:
        # @10 composition: the repaired package's own behavioral/mutant-killing phases run in the verifier too, and
        # any failure there zeroes the reward exactly like a base-phase failure.
        appended = "\n".join(
            f'if [ "$RC" -eq 0 ]; then\n'
            f'  {block} >> "$V/test_output.log" 2>&1\n'
            f'  RC=$?\n'
            f"fi"
            for block in extra_blocks
        )
        anchor = _V10_RUN_LINE + "\nRC=$?\n"
        if anchor not in text:
            raise VariantInvalid("@10 wrapper does not carry the expected run line and RC assignment")
        # splice after RC=$? so the appended phases read the base phase's status
        text = text.replace(anchor, anchor + appended + "\n", 1)
    return (
        text
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@V5_ARCHIVE_HOOK_B64@@", archive_b64)
        .replace("@@V10_INTEGRITY_HOOK_B64@@", integrity_b64)
        .replace("@@V10_UNITTEST_SHIM_B64@@", shim_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v10(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@10)."""
    return (
        "# Separate-verifier image (separate-verifier@10): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v10(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @10.

    Same bundle shape as @9; the grader is the @10 entry point with the
    binary-artifact strip, runtime integrity gate, and junit-only
    unittest grading. ``solution_sh`` adds an oracle-control reference
    solution when the parent has none. Refuses to overwrite a solution.
    """
    if not marker or not marker.strip():
        raise VariantInvalid("marker must be a nonempty hidden-test identifier")
    parent = Path(parent_dir)
    info = read_parent_info(parent)
    if info.has_solution and solution_sh is not None:
        raise VariantInvalid("parent already has solution/solve.sh; refusing overwrite")

    snapshot_hook = render_snapshot_hook_v2(info.workdir)
    probe_hook = render_probe_hook(info.workdir, marker)
    parent_toml_text = (parent / "task.toml").read_text(encoding="utf-8")
    setup_files = collect_verifier_setup_files(parent)
    python_setup = (
        PURGE_INSTALLED_COPIES_MARKER_V2.encode("utf-8")
        in setup_files[f"{V2_SETUP_SUBDIR}/setup.sh"]
    )
    extra_blocks = tuple(supplementary_test_blocks(parent, workdir=info.workdir))
    wrapper = render_wrapper_test_sh_v10(
        info.workdir, python_setup=python_setup, extra_blocks=extra_blocks
    ).encode("utf-8")
    try:
        patch_text = (parent / "tests" / "test.patch").read_text(encoding="utf-8")
        command_sh = (parent / "tests" / "test_command.sh").read_text(encoding="utf-8")
        runner = detect_runner(resolve_command_text(command_sh, None, patch_text))
    except OSError:
        runner = "unknown"
    changes: dict[str, bytes | None] = {
        "task.toml": render_task_toml(
            parent_toml_text, snapshot_hook=snapshot_hook, probe_hook=probe_hook
        ).encode("utf-8"),
        "tests/test.sh": wrapper,
        "tests/Dockerfile": render_tests_dockerfile_v10(info.docker_image).encode("utf-8"),
        **setup_files,
    }
    if solution_sh is not None:
        changes["solution/solve.sh"] = solution_sh

    setup_digest = hashlib.sha256(b"".join(setup_files[key] for key in sorted(setup_files)))
    inputs: dict[str, Any] = {
        "parent_task": info.task_name,
        "workdir": info.workdir,
        "docker_image": info.docker_image,
        "marker": marker,
        "snapshot_dir": SNAP_DIR,
        "trajectory_artifact": TRAJECTORY_ARTIFACT,
        "setup_files": sorted(setup_files),
        "setup_sha256": setup_digest.hexdigest(),
        "setup_python_path": SETUP_PYTHON_PATH if python_setup else "",
        "wrapper_sha256": hashlib.sha256(wrapper).hexdigest(),
        "supplementary_blocks": list(extra_blocks),
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v10(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with binary artifacts stripped, runner-integrity "
    "checks, and structured junit grading for pytest and unittest.",
    created_by: str = "integrity-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@10`` variant of a MiMo task package."""
    changes, inputs = build_changes_v10(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V10,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )
