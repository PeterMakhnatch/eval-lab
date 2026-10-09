"""Separate-verifier transforms for MiMo task packages.

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

import hashlib
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
TRANSFORM_ID_V2 = "separate-verifier@2"

#: Subdirectory of the bundled tests holding the readable copy of the
#: parent's clean setup chain (``environment/setup/*``). The @2 grader reruns
#: it in the verifier container so BASE and the pristine checkout never come
#: from the agent environment.
V2_SETUP_SUBDIR = "tests/_verifier-setup"

#: Scratch dir for the @2 grader. The junit report lands here (outside the
#: repo, where the code under test cannot reach it); ``/var/lib/mimo`` itself
#: is setup state (``chmod 700``, rewritten by setup.sh).
V2_GRADE_DIR = "/var/lib/mimo-grade"

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

#: Config files dropped only when their changed lines touch runner config.
_PYTEST_CONFIG_BASENAMES = frozenset({"setup.cfg", "pyproject.toml"})

_PYTEST_CONFIG_RE = re.compile(r"pytest|addopts|plugins", re.IGNORECASE)
_TESTMAIN_RE = re.compile(r"func TestMain\s*\(")
_NAMED_ID_RE = re.compile(r"(\S+\.py::[^\s\"']+)")


def tamper_signature_hit(added_lines: str) -> str | None:
    """Return the first :data:`TAMPER_SIGNATURES` entry matching ``added_lines``."""
    for signature in TAMPER_SIGNATURES:
        if re.search(signature, added_lines):
            return signature
    return None


def is_test_infra_filename(path: str) -> bool:
    """Whether ``path`` is test infrastructure dropped by name (any directory)."""
    base = PurePosixPath(path).name
    return (
        base in _TEST_INFRA_BASENAMES or base.endswith(".pth") or base.endswith("_test.go")
    )


def is_pytest_config_tamper(path: str, changed_lines: str) -> bool:
    """Whether a ``setup.cfg``/``pyproject.toml`` change touches runner config."""
    if PurePosixPath(path).name not in _PYTEST_CONFIG_BASENAMES:
        return False
    return _PYTEST_CONFIG_RE.search(changed_lines) is not None


def declares_testmain(go_source: str) -> bool:
    """Whether a Go source declares ``TestMain`` (dropped: it wraps the test binary)."""
    return _TESTMAIN_RE.search(go_source) is not None


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


def parse_named_pytest_ids(patch_text: str) -> set[str]:
    """pytest node ids named by the task's test command (``mimo_test_command.sh`` section).

    Empty when no id is parseable (e.g. base64-encoded commands): the
    named-id presence check then does not apply.
    """
    return set(_NAMED_ID_RE.findall(patch_text.split("mimo_test_command.sh")[-1]))


def evaluate_junit(junit_xml: bytes | None, rc: int, named_ids: Collection[str]) -> int:
    """Grade 1/0 from a junit report plus the test command's exit code.

    Reward 1 iff ``rc == 0`` and (the report is absent/unparseable, or it
    holds cases with no failure/error/skip and contains every named id). An
    empty report never passes; a task that unsets ``PYTEST_ADDOPTS`` falls
    back to the exit code instead.
    """
    if not junit_xml:
        return 1 if rc == 0 else 0
    try:
        cases = list(ET.fromstring(junit_xml).iter("testcase"))
    except Exception:
        return 1 if rc == 0 else 0
    bad = [
        case
        for case in cases
        if case.find("failure") is not None
        or case.find("error") is not None
        or case.find("skipped") is not None
    ]
    seen = {
        f"{(case.get('classname') or '').replace('.', '/')}.py::{case.get('name') or ''}"
        for case in cases
    }
    missing = [
        node
        for node in named_ids
        if not any(s == node or s.startswith(node + "[") for s in seen)
    ]
    return 1 if rc == 0 and cases and not bad and not missing else 0


def render_snapshot_hook_v2(workdir: str) -> str:
    """Collect-hook command snapshotting workspace files only (``sh -c``; POSIX
    only, no single quotes so it fits a TOML literal string).

    Unlike @1 this captures neither ``/var/lib/mimo/base`` nor the git dir:
    the @2 verifier reruns the clean setup itself and computes BASE there.
    Only repo file bytes cross the boundary.
    """
    return (
        f'SNAP="{SNAP_DIR}"; CWD="{workdir}"; '
        'mkdir -p "$SNAP"; '
        'tar --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" .; '
        'ls -la "$SNAP"; true'
    )


def render_tests_dockerfile_v2(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup.

    The ``--platform`` pin keeps Apple Silicon hosts on the same amd64 image
    the agent runs. No setup runs at build time: the grader reruns the
    bundled clean setup at grade time (``tests/test.sh``), matching the
    agent's runtime environment exactly.
    """
    return (
        "# Separate-verifier image (separate-verifier@2): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
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


#: @2 grader entry point. ``@@WORKDIR@@`` and ``@@TAMPER_ALTERNATION@@`` are
#: filled by :func:`render_wrapper_test_sh_v2` (token replacement, since the
#: embedded python heredoc is brace-heavy).
_V2_WRAPPER_TEMPLATE = """#!/bin/bash
# separate-verifier@2 entry: patch-only grading in a pristine verifier checkout.
# Only repo file bytes cross from the agent (workspace.tgz). The verifier runs
# its own clean setup, computes BASE itself, diffs with its own git under the
# BASE tree's ignore rules, drops test-infra paths, gates tamper signatures,
# then applies the hidden tests and grades with a structured junit check.
# Exit 0 whenever grading completes (reward in /logs/verifier/reward.txt);
# exit 1 only for testbed problems (setup/patch failures: not scored).
set -u
SNAP="/var/tmp/mimo-separate"
M="/var/lib/mimo"
CWD="@@WORKDIR@@"
V=/logs/verifier
GRADE=/var/lib/mimo-grade
TS_BAK="$GRADE/tests"
mkdir -p "$V" "$GRADE"
# 0. Pristine checkout: rerun the bundled clean setup (the same chain the
# agent got). setup.sh wipes /tests and /logs, so stash /tests first
# ($GRADE is untouched by setup) and run before writing any verifier logs.
rm -rf "$TS_BAK"; cp -a /tests "$TS_BAK"
mkdir -p "$M"; cp -r "$TS_BAK/_verifier-setup/." "$M/"
if ! bash "$M/setup.sh" > "$GRADE/setup.log" 2>&1; then
  echo "verifier setup failed (testbed problem, not scored)" >&2; exit 1
fi
rm -rf /tests; cp -a "$TS_BAK" /tests; rm -rf "$TS_BAK"
mkdir -p "$V"; cp "$GRADE/setup.log" "$V/setup.log"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "verifier setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if [ -d "$M/git-hidden" ]; then rm -rf "$CWD/.git"; mv "$M/git-hidden" "$CWD/.git"; fi
test -f /tests/test.patch || { echo "bundled test.patch missing (testbed problem)" >&2; exit 1; }
test -f /tests/test_command.sh || { echo "bundled test_command.sh missing (testbed)" >&2; exit 1; }
# 1. The agent's change, computed by the verifier's own git against the clean
# base. The agent's .git and .gitignore edits are ignored: the BASE tree's
# ignore rules apply.
test -f "$SNAP/workspace.tgz" || { echo "snapshot missing: $SNAP/workspace.tgz" >&2; exit 1; }
rm -rf /tmp/agentcopy && mkdir -p /tmp/agentcopy
tar -xzf "$SNAP/workspace.tgz" -C /tmp/agentcopy || { echo "snapshot unreadable" >&2; exit 1; }
rm -rf /tmp/agentcopy/.git
git --git-dir="$CWD/.git" show "$BASE:.gitignore" > /tmp/base.gitignore 2>/dev/null || : > /tmp/base.gitignore
cp /tmp/base.gitignore /tmp/agentcopy/.gitignore
export GIT_INDEX_FILE=/tmp/agent.index; rm -f "$GIT_INDEX_FILE"
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy read-tree "$BASE" || { echo "clean-tree read failed" >&2; exit 1; }
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy add -A
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy diff --cached --binary "$BASE" > "$V/agent.full.diff"
CHANGED=$(git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy diff --cached --name-only "$BASE")
unset GIT_INDEX_FILE
# 2. Drop test infrastructure and hidden-test paths from the change.
TESTFILES=$(grep '^diff --git' /tests/test.patch | sed 's#.* b/##')
KEEP=/tmp/keep.list; : > "$KEEP"; : > "$V/dropped.log"
for f in $CHANGED; do
  b=${f##*/}; drop=""
  case "$b" in conftest.py|pytest.ini|tox.ini|sitecustomize.py|usercustomize.py|*.pth|*_test.go) drop="test-infra";; esac
  if [ -z "$drop" ] && printf '%s\\n' "$TESTFILES" | grep -qxF -- "$f"; then drop="hidden-test path"; fi
  if [ -z "$drop" ]; then
    case "$b" in setup.cfg|pyproject.toml)
      if diff <(git --git-dir="$CWD/.git" show "$BASE:$f" 2>/dev/null) "/tmp/agentcopy/$f" 2>/dev/null | grep -E '^[<>]' | grep -qiE 'pytest|addopts|plugins'; then drop="pytest config"; fi;;
    esac
  fi
  if [ -z "$drop" ]; then
    case "$f" in *.go)
      if grep -qE 'func TestMain[[:space:]]*\\(' "/tmp/agentcopy/$f" 2>/dev/null; then drop="go TestMain"; fi;;
    esac
  fi
  if [ -n "$drop" ]; then echo "dropped $f ($drop)" >> "$V/dropped.log"; else echo "$f" >> "$KEEP"; fi
done
# 3. Copy the kept files over the pristine tree (deletions too).
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if [ -e "/tmp/agentcopy/$f" ] || [ -L "/tmp/agentcopy/$f" ]; then
    mkdir -p "$(dirname "$f")"
    cp -a "/tmp/agentcopy/$f" "$f"
  else rm -f "$f"; fi
done < "$KEEP"
# 3b. Source that reaches into the test runner or forces the exit code is not a fix.
git add -A >/dev/null 2>&1 && git diff --cached "$BASE" > "$V/agent.kept.diff"; git reset -q
if grep -E '^\\+[^+]' "$V/agent.kept.diff" | grep -qE '@@TAMPER_ALTERNATION@@'; then
  echo 0 > "$V/reward.txt"; echo "REWARD=0 tamper signature in agent diff"; exit 0
fi
# 4. Hidden tests, then the named tests with a structured report the code under test cannot reach.
printf '%s\\n' "$TESTFILES" | while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e "$BASE:$tf" 2>/dev/null; then
    git checkout -q "$BASE" -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done
if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then
  cat "$V/apply.log" >&2
  echo "the hidden tests could not be applied (testbed problem, not scored)" >&2
  exit 1
fi
JUNIT=$GRADE/junit.xml; mkdir -p "${JUNIT%/*}"
PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
python3 - "$JUNIT" "$RC" /tests/test.patch <<'PYEOF' > "$V/reward.txt" 2> "$V/junit-grade.log"
import sys, re, xml.etree.ElementTree as ET
junit_path, rc, patch_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
patch = open(patch_path).read()
ids = set(re.findall(r'(\\S+\\.py::[^\\s"\\']+)', patch.split("mimo_test_command.sh")[-1]))
try:
    cases = list(ET.parse(junit_path).iter("testcase"))
except Exception:
    print(1 if rc == 0 else 0)
    sys.stderr.write(f"rc={rc} no junit: exit-code grading\\n")
    sys.exit()
bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None]
seen = {c.get("classname", "").replace(".", "/") + ".py::" + c.get("name", "") for c in cases}
missing = [i for i in ids if not any(s == i or s.startswith(i + "[") for s in seen)]
print(1 if rc == 0 and cases and not bad and not missing else 0)
sys.stderr.write(f"rc={rc} cases={len(cases)} bad={len(bad)} named={len(ids)} missing={missing}\\n")
PYEOF
echo "REWARD=$(cat "$V/reward.txt") rc=$RC" | tee -a "$V/test_output.log"
"""


def render_wrapper_test_sh_v2(workdir: str) -> str:
    """Verifier entry point: patch-only grading (never trusts agent state)."""
    if not workdir.startswith("/"):
        raise VariantInvalid("workdir must be an absolute path")
    return (
        _V2_WRAPPER_TEMPLATE.replace("@@WORKDIR@@", workdir).replace(
            "@@TAMPER_ALTERNATION@@", "|".join(TAMPER_SIGNATURES)
        )
    )


def build_changes_v2(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @2.

    Bundles the parent's clean setup chain into ``tests/_verifier-setup/``
    and replaces ``tests/test.sh`` with the patch-only grader. The parent's
    original grading script is superseded by design (its history stays in
    the lineage record); no ``tests/test-orig.sh`` is kept.

    ``solution_sh`` adds an oracle-control reference solution when the parent
    has none. Refuses to overwrite an existing solution.
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
    changes: dict[str, bytes | None] = {
        "task.toml": render_task_toml(
            parent_toml_text, snapshot_hook=snapshot_hook, probe_hook=probe_hook
        ).encode("utf-8"),
        "tests/test.sh": render_wrapper_test_sh_v2(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v2(info.docker_image).encode("utf-8"),
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
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v2(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and a structured junit check.",
    created_by: str = "patch-only-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@2`` variant of a MiMo task package."""
    changes, inputs = build_changes_v2(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V2,
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
    "TAMPER_SIGNATURES",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "TRAJECTORY_ARTIFACT",
    "V2_GRADE_DIR",
    "V2_SETUP_SUBDIR",
    "WRAPPER_TEST_SCRIPT",
    "ParentInfo",
    "build_changes",
    "build_changes_v2",
    "collect_verifier_setup_files",
    "declares_testmain",
    "derive_separate_verifier",
    "derive_separate_verifier_v2",
    "drop_reason",
    "evaluate_junit",
    "is_pytest_config_tamper",
    "is_test_infra_filename",
    "parse_named_pytest_ids",
    "read_parent_info",
    "render_probe_hook",
    "render_snapshot_hook",
    "render_snapshot_hook_v2",
    "render_task_toml",
    "render_tests_dockerfile",
    "render_tests_dockerfile_v2",
    "render_wrapper_test_sh",
    "render_wrapper_test_sh_v2",
    "tamper_signature_hit",
]
