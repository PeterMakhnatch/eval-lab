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

import base64
import binascii
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

#: Logged reason when pytest demonstrably ran with our addopts yet no junit
#: report survived: the report was suppressed, not merely unconfigured.
JUNIT_MISSING_REASON = "junit missing after pytest start"

#: Markers proving a pytest session started (emitted before collection, so an
#: import-time exit still leaves them when stdout is unbuffered).
PYTEST_START_MARKERS: tuple[str, ...] = ("test session starts",)
_PYTEST_COLLECTED_RE = re.compile(r"collected \d+ items?")

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


def detect_pytest_run(test_output: str, command_text: str) -> bool:
    """Whether the test output shows a pytest session started for a pytest command.

    Requires both a pytest start marker in the output and ``pytest`` in the
    resolved command, so unittest/custom commands keep the exit-code fallback.
    """
    if "pytest" not in command_text.lower():
        return False
    if any(marker in test_output for marker in PYTEST_START_MARKERS):
        return True
    return _PYTEST_COLLECTED_RE.search(test_output) is not None


def output_is_blank(test_output: str) -> bool:
    """Whether the test command produced no output at all."""
    return not test_output.strip()


def junit_absence_suspicious(test_output: str, command_text: str) -> bool:
    """Whether a missing/unparsable junit report means suppression, not setup.

    True for a pytest command with intact ``PYTEST_ADDOPTS`` when the output
    shows a pytest session started — or when it shows nothing at all. A real
    pytest run always prints at least its session header, so blank output
    means the run died before flushing (pytest's capture layer holds
    everything when ``os._exit`` skips teardown); non-blank markerless output
    means pytest never started (custom/echo commands keep the fallback).
    """
    if "pytest" not in command_text.lower() or addopts_cleared(command_text):
        return False
    return detect_pytest_run(test_output, command_text) or output_is_blank(test_output)


def tamper_signature_hit(added_lines: str) -> str | None:
    """Return the first :data:`TAMPER_SIGNATURES` entry matching ``added_lines``."""
    for signature in TAMPER_SIGNATURES:
        if re.search(signature, added_lines):
            return signature
    return None


def is_test_infra_filename(path: str) -> bool:
    """Whether ``path`` is test infrastructure dropped by name (any directory)."""
    base = PurePosixPath(path).name
    return base in _TEST_INFRA_BASENAMES or base.endswith(".pth") or base.endswith("_test.go")


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


def evaluate_junit(
    junit_xml: bytes | None,
    rc: int,
    named_ids: Collection[str],
    *,
    suspicious_absence: bool = False,
) -> int:
    """Grade 1/0 from a junit report plus the test command's exit code.

    Reward 1 iff ``rc == 0`` and (the report holds cases with no
    failure/error/skip and contains every named id, or the report is
    absent/unparseable while pytest demonstrably did *not* run with our
    addopts). An empty report never passes. When ``suspicious_absence``
    is true — a pytest command with intact ``PYTEST_ADDOPTS`` whose output
    shows a pytest session started, or shows nothing at all (a real pytest
    run always prints at least its header) — a missing or unparsable report
    means the report was suppressed (not unconfigured) and grades 0; see
    :data:`JUNIT_MISSING_REASON`. Commands that unset ``PYTEST_ADDOPTS`` or
    never start pytest (unittest, custom) keep the exit-code fallback.
    """
    if not junit_xml:
        if suspicious_absence:
            return 0
        return 1 if rc == 0 else 0
    try:
        cases = list(ET.fromstring(junit_xml).iter("testcase"))
    except Exception:
        if suspicious_absence:
            return 0
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
        node for node in named_ids if not any(s == node or s.startswith(node + "[") for s in seen)
    ]
    return 1 if rc == 0 and cases and not bad and not missing else 0


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
TRANSFORM_ID_V4 = "separate-verifier@4"


def _module_comps(path: str) -> list[str]:
    """Module path components: split on ``/`` and ``.``, drop ``.py``/empties."""
    text = path.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if text.endswith(".py"):
        text = text[:-3]
    return [part for part in re.split(r"[/.]+", text) if part]


def parse_pytest_node_id(node_id: str) -> tuple[list[str], list[str], str]:
    """Split a pytest node id into (module components, class chain, test name)."""
    parts = node_id.split("::")
    return (_module_comps(parts[0]), parts[1:-1], parts[-1])


def _test_name_matches(seen_test: str, expected_test: str) -> bool:
    """Whether a junit test name satisfies an expected id's test (the @3 prefix rule)."""
    if "[" in expected_test:
        return seen_test == expected_test
    return seen_test == expected_test or seen_test.startswith(expected_test + "[")


def _comps_tail(shorter: list[str], longer: list[str]) -> bool:
    """Whether ``shorter`` equals the tail of ``longer`` (component-wise)."""
    return (
        bool(shorter)
        and len(shorter) <= len(longer)
        and shorter == longer[len(longer) - len(shorter) :]
    )


def _module_aligned(first: list[str], second: list[str]) -> bool:
    """Whether two module paths agree from the right (either may add a prefix)."""
    if not first or not second:
        return False
    shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
    return _comps_tail(shorter, longer)


def _junit_case_candidates(
    classname: str | None, name: str | None, file: str | None
) -> tuple[tuple[list[str], list[str], str], ...]:
    """(module, classes, test) interpretations of one junit testcase.

    The classname splits into (module, classes) at every cut; the junit
    ``file`` attribute, when present, locates the module while the classname
    prefix aligned with it locates the classes.
    """
    name_parts = (name or "").split("::")
    seen_test = name_parts[-1]
    extra_classes = name_parts[:-1]
    class_parts: list[str] = classname.split(".") if classname else []
    candidates = [
        (class_parts[:cut], class_parts[cut:] + extra_classes, seen_test)
        for cut in range(1, len(class_parts) + 1)
    ]
    if file:
        seen_mod = _module_comps(file)
        candidates.extend(
            (seen_mod, class_parts[cut:] + extra_classes, seen_test)
            for cut in range(len(class_parts) + 1)
            if cut == 0 or _comps_tail(class_parts[:cut], seen_mod)
        )
    return tuple(candidates)


def junit_case_matches_expected(
    classname: str | None,
    name: str | None,
    file: str | None,
    expected_id: str,
) -> bool:
    """Whether one junit testcase satisfies a pytest node id from the test command.

    The module path aligns by suffix at component boundaries (so a rootdir
    under ``tests/`` still matches), the class chain must agree exactly, and
    the test name must agree exactly (or by parametrize prefix). A junit
    ``file`` attribute, when present, locates the module while the classname
    prefix aligned with it locates the classes. A class-level expected id
    (``file.py::TestClass``, naming no test) is satisfied by any case of
    exactly that class — the command asked to run the class, and (with no
    failure/error/skip in the report) the class ran and passed. An expected
    id never matches a case with a different test name or class.
    """
    exp_mod, exp_classes, exp_test = parse_pytest_node_id(expected_id)
    if not exp_mod or not exp_test:
        return False
    # A `test_`-prefixed id names a function, never a class.
    class_level = not exp_test.startswith("test_")
    for mod, classes, seen_test in _junit_case_candidates(classname, name, file):
        if not _module_aligned(mod, exp_mod):
            continue
        if classes == exp_classes and _test_name_matches(seen_test, exp_test):
            return True
        if class_level and classes == exp_classes + [exp_test]:
            return True
    return False


def evaluate_junit_v4(
    junit_xml: bytes | None,
    rc: int,
    named_ids: Collection[str],
    *,
    suspicious_absence: bool = False,
) -> int:
    """Grade 1/0 from a junit report plus the test command's exit code (@4).

    Same contract as :func:`evaluate_junit`, except the named-id presence
    check matches by aligned path-suffix
    (:func:`junit_case_matches_expected`) instead of exact string equality,
    so pytest rootdir shifts (classnames without the ``tests/`` segment)
    no longer grade a passing run 0.
    """
    if not junit_xml:
        if suspicious_absence:
            return 0
        return 1 if rc == 0 else 0
    try:
        cases = list(ET.fromstring(junit_xml).iter("testcase"))
    except Exception:
        if suspicious_absence:
            return 0
        return 1 if rc == 0 else 0
    bad = [
        case
        for case in cases
        if case.find("failure") is not None
        or case.find("error") is not None
        or case.find("skipped") is not None
    ]
    missing = [
        node
        for node in named_ids
        if not any(
            junit_case_matches_expected(
                case.get("classname"), case.get("name"), case.get("file"), node
            )
            for case in cases
        )
    ]
    return 1 if rc == 0 and cases and not bad and not missing else 0


def evaluate_junit_v5(
    report_xmls: list[bytes | None],
    rc: int,
    named_ids: Collection[str],
) -> int:
    """Grade 1/0 from junit reports (multi-phase union) plus exit code (@5).

    ``report_xmls`` holds every phase report (plugin report plus per-phase
    archives); empty/unparsable entries are ignored. Skipped testcases are
    not failures: reference test patches legitimately mark still-failing
    behaviors skipped, and environments skip tests for missing optional
    dependencies. Only failure/error cases, a nonzero exit code, an empty
    case union, or missing named ids grade 0. Named ids match by aligned
    path-suffix (:func:`junit_case_matches_expected`).
    """
    cases: list[ET.Element] = []
    for raw in report_xmls:
        if not raw:
            continue
        try:
            cases.extend(ET.fromstring(raw).iter("testcase"))
        except Exception:
            continue
    if not cases:
        return 0
    bad = [
        case for case in cases if case.find("failure") is not None or case.find("error") is not None
    ]
    missing = [
        node
        for node in named_ids
        if not any(
            junit_case_matches_expected(
                case.get("classname"), case.get("name"), case.get("file"), node
            )
            for case in cases
        )
    ]
    return 1 if rc == 0 and not bad and not missing else 0


# --------------------------------------------------------------------------- #
# separate-verifier@6 fail-to-pass helpers (pure; mirrored in the @6 grader)
# --------------------------------------------------------------------------- #
# @5 grades failures/errors only and ignores skips. A source-level skip in a
#: module the hidden tests import needs no knowledge of the tests and yields
#: reward 1 with zero tests passing (measured on @5 packages: 002552 pytest,
#: rc 0 with 12/12 skipped and 4/4 named IDs present; 000803 unittest, ``Ran
#: 8 tests OK (skipped=8)``; go ``ok ... [no tests to run]``, rc 0 — all via
#: call-time hooks from the imported package, invisible to every tamper
#: signature). @6 runs the hidden tests on its pristine tree first and
#: requires the fail-to-pass shape: pristine failures must now pass.


def junit_case_status(case: ET.Element) -> str:
    """Per-case outcome: ``bad`` (failure/error), ``skipped``, or ``passed``."""
    if case.find("failure") is not None or case.find("error") is not None:
        return "bad"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def junit_status_map(
    report_xmls: list[bytes | None],
) -> dict[tuple[str, str, str], str]:
    """Worst-outcome map per test key across junit reports (multi-phase union).

    Keys are ``(classname, name, file)``; duplicates keep the worst outcome
    (bad > skipped > passed), matching the union rule that a failure in any
    phase fails the run.
    """
    worst = {"passed": 0, "skipped": 1, "bad": 2}
    statuses: dict[tuple[str, str, str], str] = {}
    for raw in report_xmls:
        if not raw:
            continue
        try:
            found = ET.fromstring(raw).iter("testcase")
        except Exception:
            continue
        for case in found:
            key = (case.get("classname") or "", case.get("name") or "", case.get("file") or "")
            status = junit_case_status(case)
            if key not in statuses or worst[status] > worst[statuses[key]]:
                statuses[key] = status
    return statuses


def evaluate_junit_v6(
    report_xmls: list[bytes | None],
    rc: int,
    named_ids: Collection[str],
    baseline_xmls: list[bytes | None] | None = None,
) -> int:
    """Grade 1/0 with the fail-to-pass rule against a pristine baseline (@6).

    Keeps every @5 rule (failures/errors, exit code, empty union, named-ID
    matching). When a non-empty baseline union is given, every test that
    failed/errored on pristine must now PASS (not skip, not vanish), every
    test that passed on pristine must still pass, and skips are tolerated
    only for tests also skipped on pristine. With no baseline (None or
    empty), grading is exactly @5.
    """
    if evaluate_junit_v5(report_xmls, rc, named_ids) == 0:
        return 0
    baseline = junit_status_map(baseline_xmls) if baseline_xmls else {}
    if not baseline:
        return 1
    agent = junit_status_map(report_xmls)
    baseline_skipped = {key for key, status in baseline.items() if status == "skipped"}
    for key, status in baseline.items():
        if status != "skipped" and agent.get(key) != "passed":
            return 0
    for key, status in agent.items():
        if status == "skipped" and key not in baseline_skipped:
            return 0
    return 1


_UNITTEST_SKIP_RE = re.compile(r"skipped=(\d+)")


def _unittest_counts(test_output: str) -> tuple[int, int] | None:
    """``(ran, skipped)`` from unittest summary text; ``None`` when unparsable."""
    match = _UNITTEST_RAN_RE.search(test_output)
    if match is None:
        return None
    skipped = _UNITTEST_SKIP_RE.search(test_output)
    return (int(match.group(1)), int(skipped.group(1)) if skipped else 0)


def evaluate_unittest_v6(
    test_output: str, rc: int, baseline_output: str | None = None
) -> int:
    """Grade 1/0 from unittest output with baseline run/skip counts (@6).

    Keeps every @5 rule; when the pristine baseline summary parses, the agent
    run must execute at least as many tests and skip at most as many: every
    pristine failure must now pass rather than skip or vanish. With no (or an
    unparsable) baseline, grading is exactly @5.
    """
    if evaluate_unittest(test_output, rc) == 0:
        return 0
    if baseline_output is None:
        return 1
    base = _unittest_counts(baseline_output)
    if base is None:
        return 1
    agent = _unittest_counts(test_output)
    if agent is None:  # unreachable: evaluate_unittest already passed above
        return 0
    (ran_base, skipped_base) = base
    (ran_agent, skipped_agent) = agent
    if ran_agent < ran_base or skipped_agent > skipped_base:
        return 0
    return 1


def evaluate_go_output_v6(test_output: str, rc: int) -> int:
    """Grade 1/0 from ``go test`` output with the non-empty-run rule (@6).

    Keeps every @5 rule; a run selecting zero tests (``no tests to run``)
    grades 0 even with ``rc == 0`` and an ``ok`` line. An init hook appending
    ``-test.run=^$`` to ``os.Args`` empties the run with zero tests passing
    (measured in the 000553 image: ``ok pkg 0.026s [no tests to run]``,
    rc 0). Legitimate suites never select zero tests; ``--- SKIP`` lines from
    ``t.Skip`` do not carry this marker.
    """
    if "no tests to run" in test_output:
        return 0
    return evaluate_go_output(test_output, rc)


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
# PYTHONUNBUFFERED so the pytest session header reaches the log even when the
# run is killed import-time (os._exit skips stdio flush); the header is what
# proves pytest started with our addopts when no junit report survives.
PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
python3 - "$JUNIT" "$RC" /tests/test.patch "$V/test_output.log" /tests/test_command.sh <<'PYEOF' > "$V/reward.txt" 2> "$V/junit-grade.log"
import sys, re, base64, binascii, xml.etree.ElementTree as ET
junit_path, rc, patch_path, output_path, cmd_path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
patch = open(patch_path, encoding="utf-8", errors="replace").read()
ids = set(re.findall(r'(\\S+\\.py::[^\\s"\\']+)', patch.split("mimo_test_command.sh")[-1]))
output = open(output_path, encoding="utf-8", errors="replace").read()
cmd_text = open(cmd_path, encoding="utf-8", errors="replace").read()
mimo = ""
for ref in re.findall(r'(\\S*mimo_test_command\\.sh)', cmd_text):
    try:
        mimo += open(ref.split("/")[-1], encoding="utf-8", errors="replace").read() + "\\n"
    except OSError:
        pass
hay = cmd_text + "\\n" + mimo + "\\n" + patch.split("mimo_test_command.sh")[-1]
for tok in re.findall(r'[A-Za-z0-9+/]{40,}={0,2}', hay):
    try:
        hay += base64.b64decode(tok, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        pass
cleared = re.search(r'unset\\s+[^\\n]*PYTEST_ADDOPTS|PYTEST_ADDOPTS\\s*=|env\\s+[^\\n]*-u\\s+PYTEST_ADDOPTS', hay) is not None
has_pytest = "pytest" in hay.lower()
started = has_pytest and ("test session starts" in output or re.search(r'collected \\d+ items?', output) is not None)
# Blank output from a pytest command means the run died before flushing
# (pytest's capture layer holds everything when os._exit skips teardown); a
# real pytest run always prints at least its session header.
flag = (not cleared) and (started or (len(output.strip()) == 0 and has_pytest))
def grade_noreport():
    if flag:
        sys.stderr.write(f"rc={rc} @@JUNIT_MISSING_REASON@@\\n")
        print(0)
    else:
        print(1 if rc == 0 else 0)
        sys.stderr.write(f"rc={rc} no junit: exit-code grading\\n")
    sys.exit()
try:
    raw = open(junit_path, "rb").read()
    cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
except Exception:
    grade_noreport()
if not raw:
    grade_noreport()
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
        _V2_WRAPPER_TEMPLATE.replace("@@WORKDIR@@", workdir)
        .replace("@@TAMPER_ALTERNATION@@", "|".join(TAMPER_SIGNATURES))
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
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
TRANSFORM_ID_V3 = "separate-verifier@3"

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


#: Runners with cheap pass/fail output markers. A passing run always prints,
#: so ``rc == 0`` with blank output grades 0 (generalized A7 rule). Silent
#: families (usecase-sh, make, forge, custom, node-run, bats, ...) keep the
#: exit-code fallback.
V3_STRUCTURED_RUNNERS = frozenset(
    {
        "go-test",
        "jest",
        "vitest",
        "mocha",
        "ava",
        "tap",
        "node-test",
        "karma",
        "jasmine",
        "cargo-test",
        "rspec",
        "phpunit",
        "mvn",
        "gradle",
        "unittest",
    }
)

_GO_OK_RE = re.compile(r"^ok\s+\S+", re.MULTILINE)
_GO_FAIL_RE = re.compile(r"^(FAIL|--- FAIL|panic:)", re.MULTILINE)
_JEST_PASS_RE = re.compile(r"Tests:\s+\d+\s+passed")
_JEST_FAIL_RE = re.compile(r"Tests:\s+.*failed|Test Suites:\s+.*failed")
_VITEST_PASS_RE = re.compile(r"(Test Files|Tests)\s+\d+\s+passed")
_VITEST_FAIL_RE = re.compile(r"(Test Files|Tests)\s+.*failed")
_MOCHA_PASS_RE = re.compile(r"\d+\s+passing")
_MOCHA_FAIL_RE = re.compile(r"[1-9]\d*\s+failing")
_TAP_FAIL_RE = re.compile(r"^not ok\b", re.MULTILINE)
_TAP_PASS_RE = re.compile(r"^ok\b", re.MULTILINE)
_KARMA_PASS_RE = re.compile(r"SUCCESS")
_KARMA_FAIL_RE = re.compile(r"FAILED")
_RSPEC_PASS_RE = re.compile(r"0\s+failures")
_RSPEC_FAIL_RE = re.compile(r"[1-9]\d*\s+failures")
_PHPUNIT_PASS_RE = re.compile(r"^OK\b", re.MULTILINE)
_PHPUNIT_FAIL_RE = re.compile(r"FAILURES!|ERRORS!|No tests executed")
_CARGO_PASS_RE = re.compile(r"test result:\s+ok")
_CARGO_FAIL_RE = re.compile(r"test result:\s+FAILED|panicked")
_UNITTEST_RAN_RE = re.compile(r"Ran (\d+) test")
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)
_UNITTEST_BAD_RE = re.compile(r"^(FAILED|ERROR)", re.MULTILINE)
_JASMINE_FAIL_RE = re.compile(r"[1-9]\d*\s+failures?|failed expectations")


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


def evaluate_go_output(test_output: str, rc: int) -> int:
    """Grade 1/0 from ``go test`` text output plus the exit code.

    Reward 1 iff ``rc == 0`` with at least one ``ok <pkg>`` line and no
    failure/panic marker. ``[no test files]`` (tests deleted), blank, or
    exit-forced output grades 0.
    """
    if rc != 0:
        return 0
    if _GO_FAIL_RE.search(test_output):
        return 0
    return 1 if _GO_OK_RE.search(test_output) else 0


def evaluate_js_output(runner: str, test_output: str, rc: int) -> int:
    """Grade 1/0 from jest/vitest/mocha/tap/ava/node-test/karma/jasmine output."""
    if rc != 0:
        return 0
    if runner == "jest":
        return (
            1 if _JEST_PASS_RE.search(test_output) and not _JEST_FAIL_RE.search(test_output) else 0
        )
    if runner == "vitest":
        return (
            1
            if _VITEST_PASS_RE.search(test_output) and not _VITEST_FAIL_RE.search(test_output)
            else 0
        )
    if runner == "mocha":
        return (
            1
            if _MOCHA_PASS_RE.search(test_output) and not _MOCHA_FAIL_RE.search(test_output)
            else 0
        )
    if runner in ("tap", "ava", "node-test"):
        return 1 if _TAP_PASS_RE.search(test_output) and not _TAP_FAIL_RE.search(test_output) else 0
    if runner == "karma":
        return (
            1
            if _KARMA_PASS_RE.search(test_output) and not _KARMA_FAIL_RE.search(test_output)
            else 0
        )
    if runner == "jasmine":
        return 1 if not _JASMINE_FAIL_RE.search(test_output) else 0
    return 1


def evaluate_rspec_output(test_output: str, rc: int) -> int:
    """Grade 1/0 from rspec text output (``N examples, 0 failures``)."""
    if rc != 0:
        return 0
    if _RSPEC_FAIL_RE.search(test_output):
        return 0
    return 1 if _RSPEC_PASS_RE.search(test_output) else 0


def evaluate_phpunit_output(test_output: str, rc: int) -> int:
    """Grade 1/0 from phpunit text output (``OK (...)``)."""
    if rc != 0:
        return 0
    if _PHPUNIT_FAIL_RE.search(test_output):
        return 0
    return 1 if _PHPUNIT_PASS_RE.search(test_output) else 0


def evaluate_cargo_output(test_output: str, rc: int) -> int:
    """Grade 1/0 from ``cargo test`` text output (``test result: ok``)."""
    if rc != 0:
        return 0
    if _CARGO_FAIL_RE.search(test_output):
        return 0
    return 1 if _CARGO_PASS_RE.search(test_output) else 0


def evaluate_surefire_reports(report_xmls: list[bytes], rc: int) -> int | None:
    """Grade 1/0 from maven/gradle surefire-style XML reports.

    Skipped testcases are not failures (same @5 rule as the junit grader).
    Returns ``None`` when no reports exist (caller keeps the exit-code
    fallback): some modules genuinely emit none, and inventing suspicion
    there would false-positive honest passes.
    """
    if not report_xmls:
        return None
    cases: list[ET.Element] = []
    for raw in report_xmls:
        try:
            cases.extend(ET.fromstring(raw).iter("testcase"))
        except Exception:
            return 0
    bad = [
        case for case in cases if case.find("failure") is not None or case.find("error") is not None
    ]
    return 1 if rc == 0 and cases and not bad else 0


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

#: @3 grader entry point, part A (setup, agent diff, @3 drops). Tokens
#: ``@@WORKDIR@@`` / ``@@V3_NEW_INFRA_CASE@@`` are filled by
#: :func:`render_wrapper_test_sh_v3` (token replacement, since the embedded
#: python heredocs are brace-heavy).
_V3_WRAPPER_A = """#!/bin/bash
# separate-verifier@3 entry: patch-only grading in a pristine verifier checkout.
# Extends @2 with multi-runner drops, cross-language tamper gates, and
# structured per-runner grading (exit-code fallback for silent families).
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
# 0. Pristine checkout: rerun the bundled clean setup (same as @2).
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
# base (same as @2).
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
# 2. @3 drops: @2 infra names and hidden-test paths, plus new-file runner
# configs and build outputs; tracked runner-config hunks revert to BASE.
TESTFILES=$(grep '^diff --git' /tests/test.patch | sed 's#.* b/##')
KEEP=/tmp/keep.list; REVERT=/tmp/revert.list; : > "$KEEP"; : > "$REVERT"; : > "$V/dropped.log"
for f in $CHANGED; do
  b=${f##*/}; drop=""; reverted=""
  case "$b" in conftest.py|pytest.ini|tox.ini|sitecustomize.py|usercustomize.py|*.pth|*_test.go) drop="test-infra";; esac
  if [ -z "$drop" ]; then
    case "$b" in @@V3_NEW_INFRA_CASE@@)
      if ! git --git-dir="$CWD/.git" cat-file -e "$BASE:$f" 2>/dev/null; then drop="new runner config"; fi;;
    esac
  fi
  if [ -z "$drop" ]; then
    case "$f" in target/surefire-reports/*|target/failsafe-reports/*|build/test-results/*|.nyc_output/*|coverage/*)
      if ! git --git-dir="$CWD/.git" cat-file -e "$BASE:$f" 2>/dev/null; then drop="new build output"; fi;;
    esac
  fi
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
  if [ -z "$drop" ] && git --git-dir="$CWD/.git" cat-file -e "$BASE:$f" 2>/dev/null; then
    CHLINES=$(diff <(git --git-dir="$CWD/.git" show "$BASE:$f" 2>/dev/null) "/tmp/agentcopy/$f" 2>/dev/null | grep -E '^[<>]' || true)
    rule=""
    case "$b" in package.json)
      if printf '%s\\n' "$CHLINES" | grep -qiE '"(jest|mocha|vitest|ava|tap|karma|jasmine|nyc|c8)"[[:space:]]*:'; then rule="package.json runner key"; fi;;
    esac
    case "$b" in pom.xml)
      if printf '%s\\n' "$CHLINES" | grep -qiE 'surefire|failsafe|skipTests|<excludes?>'; then rule="pom.xml surefire"; fi;;
    esac
    case "$b" in build.gradle|build.gradle.kts)
      if printf '%s\\n' "$CHLINES" | grep -qiE 'exclude|ignoreFailures|useJUnit|test[[:space:]]*\\{'; then rule="gradle test block"; fi;;
    esac
    case "$b" in .rspec)
      if printf '%s\\n' "$CHLINES" | grep -qE -- '--require|--format|--tag|--exclude-pattern'; then rule="rspec options"; fi;;
    esac
    case "$b" in phpunit.xml|phpunit.xml.dist|phpunit.dist.xml)
      if printf '%s\\n' "$CHLINES" | grep -qiE 'bootstrap|suffix=|exclude|executionOrder|printer'; then rule="phpunit config"; fi;;
    esac
    case "$b" in go.mod)
      if printf '%s\\n' "$CHLINES" | grep -qiE 'toolchain'; then rule="go toolchain"; fi;;
    esac
    case "$b" in .mocharc.js|.mocharc.cjs|.mocharc.mjs|.mocharc.json|.mocharc.yml|.mocharc.yaml)
      if printf '%s\\n' "$CHLINES" | grep -qiE 'spec|require|ignore|exclude|grep|invert|reporter'; then rule="mocha options"; fi;;
    esac
    if [ -n "$rule" ]; then reverted="$rule"; echo "reverted $f ($rule)" >> "$V/dropped.log"; echo "$f" >> "$REVERT"; fi
  fi
  if [ -n "$drop" ]; then echo "dropped $f ($drop)" >> "$V/dropped.log"; elif [ -z "$reverted" ]; then echo "$f" >> "$KEEP"; fi
done
# 3. Copy the kept files over the pristine tree (deletions too), then
# restore reverted configs to BASE.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if [ -e "/tmp/agentcopy/$f" ] || [ -L "/tmp/agentcopy/$f" ]; then
    mkdir -p "$(dirname "$f")"
    cp -a "/tmp/agentcopy/$f" "$f"
  else rm -f "$f"; fi
done < "$KEEP"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if git --git-dir="$CWD/.git" cat-file -e "$BASE:$f" 2>/dev/null; then
    git --git-dir="$CWD/.git" show "$BASE:$f" > "$f"
  else rm -f "$f"; fi
done < "$REVERT"
"""

#: @3 grader entry point, part B (tamper gate, hidden tests, command
#: resolution). Tokens ``@@V3_UNIVERSAL@@`` / ``@@V3_SCOPED@@`` /
#: ``@@V3_CONFTEST_HOOK_B64@@`` are filled by :func:`render_wrapper_test_sh_v3`.
_V3_WRAPPER_B = """# 3b. Source that reaches into the test runner or forces the exit code is not a fix.
# Per-file gate: universal signatures on any path, exit(0) only on Ruby/PHP.
git add -A >/dev/null 2>&1 && git diff --cached "$BASE" > "$V/agent.kept.diff"; git reset -q
python3 - "$V/agent.kept.diff" <<'PYEOF' > "$V/tamper.log" 2>&1
import sys, re
diff = open(sys.argv[1], encoding="utf-8", errors="replace").read()
universal = @@V3_UNIVERSAL@@
scoped = @@V3_SCOPED@@
cur = None
added = {}
for line in diff.splitlines():
    if line.startswith("+++ b/"):
        cur = line[6:]
        added.setdefault(cur, [])
    elif cur is not None and line.startswith("+") and not line.startswith("+++"):
        added[cur].append(line[1:])
for path, lines in added.items():
    text = "\\n".join(lines)
    for sig in universal:
        if re.search(sig, text):
            print("tamper %s in %s" % (sig, path))
            sys.exit(10)
    leaf = path.rsplit("/", 1)[-1]
    ext = "." + leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""
    for exts, sig in scoped:
        if ext in exts and re.search(sig, text):
            print("scoped tamper %s in %s" % (sig, path))
            sys.exit(10)
PYEOF
if [ $? -eq 10 ]; then
  echo 0 > "$V/reward.txt"; echo "REWARD=0 tamper signature in agent diff"; exit 0
fi
# 4. Hidden tests, then resolve the real test command. Opaque build-env
# wrappers (mimo_build_env.tar.gz.b64, from the hidden patch) decode here:
# the tarball is trusted verifier-side bundle content, never agent bytes.
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
RESOLVED=$(cat /tests/test_command.sh)
for ref in $(grep -oE '[^ "]*mimo_test_command\\.sh' /tests/test_command.sh | sort -u); do
  base=${ref##*/}
  if [ -f "$base" ]; then RESOLVED="$RESOLVED
$(cat "$base")"; fi
done
if printf '%s' "$RESOLVED" | grep -q 'build_env/test_command'; then
  if [ -f mimo_build_env.tar.gz.b64 ]; then
    rm -rf "$GRADE/build_env" && mkdir -p "$GRADE/build_env"
    if base64 -d mimo_build_env.tar.gz.b64 2>/dev/null | tar -xzf - -C "$GRADE/build_env" 2>/dev/null; then
      BE_TC=$(find "$GRADE/build_env" -name 'test_command.sh' | head -1)
      if [ -n "$BE_TC" ]; then RESOLVED="$RESOLVED
$(cat "$BE_TC")"; fi
    fi
  fi
fi
printf '%s' "$RESOLVED" > "$V/resolved_command.txt"
RUNNER=custom
if printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])go +test( |$)'; then RUNNER=go-test
elif printf '%s' "$RESOLVED" | grep -qE 'GO_BIN" +test( |$)'; then RUNNER=go-test
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])jest([^[:alnum:]_]|$)'; then RUNNER=jest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])vitest([^[:alnum:]_]|$)'; then RUNNER=vitest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])_?mocha([^[:alnum:]_]|$)'; then RUNNER=mocha
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])ava([^[:alnum:]_]|$)'; then RUNNER=ava
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])tap([^[:alnum:]_]|$)'; then RUNNER=tap
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])karma([^[:alnum:]_]|$)'; then RUNNER=karma
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])jasmine([^[:alnum:]_]|$)'; then RUNNER=jasmine
elif printf '%s' "$RESOLVED" | grep -qE 'node +--test([^[:alnum:]_]|$)'; then RUNNER=node-test
elif printf '%s' "$RESOLVED" | grep -qE 'cargo +test([^[:alnum:]_]|$)'; then RUNNER=cargo-test
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])rspec([^[:alnum:]_]|$)'; then RUNNER=rspec
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])phpunit([^[:alnum:]_]|$)'; then RUNNER=phpunit
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])mvn([^[:alnum:]_]|$)|surefire|failsafe'; then RUNNER=mvn
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])gradlew?([^[:alnum:]_]|$)'; then RUNNER=gradle
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])pytest([^[:alnum:]_]|$)'; then RUNNER=pytest
elif printf '%s' "$RESOLVED" | grep -qE '\\-m +unittest([^[:alnum:]_]|$)'; then RUNNER=unittest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])forge +test([^[:alnum:]_]|$)'; then RUNNER=forge
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])node([^[:alnum:]_]|$)'; then RUNNER=node-run
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])(make|ctest|cmake)([^[:alnum:]_]|$)'; then RUNNER=make
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])bats([^[:alnum:]_]|$)'; then RUNNER=bats
fi
echo "RUNNER=$RUNNER" > "$V/runner.txt"
# Verifier-owned conftest hook for pytest commands that clear our addopts:
# a root conftest.py is always collected, so the structured report survives
# any env scrubbing. Appended after the hidden-test apply, never clobbering.
if [ "$RUNNER" = pytest ]; then
  CLEARED=0
  printf '%s' "$RESOLVED" | grep -q 'unset.*PYTEST_ADDOPTS' && CLEARED=1
  printf '%s' "$RESOLVED" | grep -q 'PYTEST_ADDOPTS=' && CLEARED=1
  printf '%s' "$RESOLVED" | grep -qE 'env +([^ ]+ +)*-u([^ ]* +)*PYTEST_ADDOPTS|env +-u +PYTEST_ADDOPTS' && CLEARED=1
  if [ "$CLEARED" = 1 ]; then
    echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
    touch "$GRADE/hook_installed"
    echo "installed verifier conftest hook (addopts cleared)" >> "$V/dropped.log"
  fi
fi
export MIMO_VERIFIER_JUNIT="$GRADE/junit.xml"
"""

#: @3 grader entry point, part C (test run, structured per-runner grading).
_V3_WRAPPER_C = """JUNIT=$GRADE/junit.xml; mkdir -p "${JUNIT%/*}"
# PYTHONUNBUFFERED so the pytest session header reaches the log even when the
# run is killed import-time (same as @2).
PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
python3 - "$JUNIT" "$RC" /tests/test.patch "$V/test_output.log" /tests/test_command.sh "$CWD" <<'PYEOF' > "$V/reward.txt" 2> "$V/junit-grade.log"
import sys, re, base64, binascii, glob, os, xml.etree.ElementTree as ET
junit_path, rc, patch_path, output_path, cmd_path, cwd = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
patch = open(patch_path, encoding="utf-8", errors="replace").read()
ids = set(re.findall(r'(\\S+\\.py::[^\\s"\\']+)', patch.split("mimo_test_command.sh")[-1]))
output = open(output_path, encoding="utf-8", errors="replace").read()
cmd_text = open(cmd_path, encoding="utf-8", errors="replace").read()
mimo = ""
for ref in re.findall(r'(\\S*mimo_test_command\\.sh)', cmd_text):
    try:
        mimo += open(ref.split("/")[-1], encoding="utf-8", errors="replace").read() + "\\n"
    except OSError:
        pass
if "build_env/test_command" in cmd_text + mimo:
    for root, _dirs, files in os.walk("/var/lib/mimo-grade/build_env"):
        for fn in files:
            if fn == "test_command.sh":
                try:
                    mimo += open(os.path.join(root, fn), encoding="utf-8", errors="replace").read() + "\\n"
                except OSError:
                    pass
hay = cmd_text + "\\n" + mimo + "\\n" + patch.split("mimo_test_command.sh")[-1]
for tok in re.findall(r'[A-Za-z0-9+/]{40,}={0,2}', hay):
    try:
        hay += base64.b64decode(tok, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        pass
order = [
    ("go-test", r"(?<![\\w])go\\s+test\\b|GO_BIN\\\"\\s+test\\b|\\$GO_BIN\\\"\\s+test"),
    ("jest", r"(?<![\\w])jest\\b"),
    ("vitest", r"(?<![\\w])vitest\\b"),
    ("mocha", r"(?<![\\w])mocha\\b|_mocha\\b"),
    ("ava", r"(?<![\\w])ava\\b"),
    ("tap", r"(?<![\\w])tap\\b"),
    ("karma", r"(?<![\\w])karma\\b"),
    ("jasmine", r"(?<![\\w])jasmine\\b"),
    ("node-test", r"node\\s+--test\\b"),
    ("cargo-test", r"cargo\\s+test\\b"),
    ("rspec", r"(?<![\\w])rspec\\b"),
    ("phpunit", r"(?<![\\w])phpunit\\b"),
    ("mvn", r"(?<![\\w])mvn\\b|surefire|failsafe"),
    ("gradle", r"(?<![\\w])gradlew?\\b"),
    ("pytest", r"(?<![\\w])pytest\\b"),
    ("unittest", r"-m\\s+unittest\\b"),
    ("forge", r"(?<![\\w])forge\\s+test\\b"),
    ("node-run", r"(?<![\\w])node\\b"),
    ("make", r"(?<![\\w])(make|ctest|cmake)\\b"),
    ("bats", r"(?<![\\w])bats\\b"),
]
runner = "custom"
for name, rx in order:
    if re.search(rx, hay):
        runner = name
        break
sys.stderr.write("runner=%s rc=%d\\n" % (runner, rc))
blank = len(output.strip()) == 0
def fallback():
    print(1 if rc == 0 else 0)
    sys.stderr.write("rc=%d %s: exit-code grading\\n" % (rc, runner))
    sys.exit()
if runner == "pytest":
    cleared = re.search(r'unset\\s+[^\\n]*PYTEST_ADDOPTS|PYTEST_ADDOPTS\\s*=|env\\s+[^\\n]*-u\\s+PYTEST_ADDOPTS', hay) is not None
    has_pytest = "pytest" in hay.lower()
    started = has_pytest and ("test session starts" in output or re.search(r'collected \\d+ items?', output) is not None)
    flag = os.path.exists("/var/lib/mimo-grade/hook_installed") or ((not cleared) and (started or (len(output.strip()) == 0 and has_pytest)))
    def grade_noreport():
        if flag:
            sys.stderr.write("rc=%d @@JUNIT_MISSING_REASON@@\\n" % rc)
            print(0)
        else:
            print(1 if rc == 0 else 0)
            sys.stderr.write("rc=%d no junit: exit-code grading\\n" % rc)
        sys.exit()
    try:
        raw = open(junit_path, "rb").read()
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()
    if not raw:
        grade_noreport()
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None]
    seen = {c.get("classname", "").replace(".", "/") + ".py::" + c.get("name", "") for c in cases}
    missing = [i for i in ids if not any(s == i or s.startswith(i + "[") for s in seen)]
    print(1 if rc == 0 and cases and not bad and not missing else 0)
    sys.stderr.write("rc=%d cases=%d bad=%d named=%d missing=%s\\n" % (rc, len(cases), len(bad), len(ids), missing))
    sys.exit()
if runner in ("go-test", "jest", "vitest", "mocha", "ava", "tap", "node-test", "karma", "jasmine", "cargo-test", "rspec", "phpunit", "unittest"):
    if blank:
        sys.stderr.write("rc=%d blank %s output: no test evidence\\n" % (rc, runner))
        print(0)
        sys.exit()
    if rc != 0:
        print(0)
        sys.stderr.write("rc=%d %s nonzero exit\\n" % (rc, runner))
        sys.exit()
    ok = False
    if runner == "go-test":
        ok = re.search(r"^ok\\s+\\S+", output, re.M) is not None and re.search(r"^(FAIL|--- FAIL|panic:)", output, re.M) is None
    elif runner == "jest":
        ok = re.search(r"Tests:\\s+\\d+\\s+passed", output) is not None and re.search(r"Tests:\\s+.*failed|Test Suites:\\s+.*failed", output) is None
    elif runner == "vitest":
        ok = re.search(r"(Test Files|Tests)\\s+\\d+\\s+passed", output) is not None and re.search(r"(Test Files|Tests)\\s+.*failed", output) is None
    elif runner == "mocha":
        ok = re.search(r"\\d+\\s+passing", output) is not None and re.search(r"[1-9]\\d*\\s+failing", output) is None
    elif runner in ("tap", "ava", "node-test"):
        ok = re.search(r"^ok\\b", output, re.M) is not None and re.search(r"^not ok\\b", output, re.M) is None
    elif runner == "karma":
        ok = "SUCCESS" in output and "FAILED" not in output
    elif runner == "jasmine":
        ok = re.search(r"[1-9]\\d*\\s+failures?|failed expectations", output) is None
    elif runner == "cargo-test":
        ok = re.search(r"test result:\\s+ok", output) is not None and re.search(r"test result:\\s+FAILED|panicked", output) is None
    elif runner == "rspec":
        ok = re.search(r"0\\s+failures", output) is not None and re.search(r"[1-9]\\d*\\s+failures", output) is None
    elif runner == "phpunit":
        ok = re.search(r"^OK\\b", output, re.M) is not None and re.search(r"FAILURES!|ERRORS!|No tests executed", output) is None
    elif runner == "unittest":
        m = re.search(r"Ran (\\d+) test", output)
        ok = (m is not None and int(m.group(1)) >= 1 and re.search(r"^OK\\b", output, re.M) is not None and re.search(r"^(FAILED|ERROR)", output, re.M) is None)
    print(1 if ok else 0)
    sys.stderr.write("rc=%d %s markers=%s\\n" % (rc, runner, ok))
    sys.exit()
if runner in ("mvn", "gradle"):
    paths = []
    for pat in ("target/surefire-reports/*.xml", "target/failsafe-reports/*.xml", "build/test-results/test/*.xml", "build/test-results/**/*.xml"):
        paths.extend(glob.glob(os.path.join(cwd, pat), recursive=True))
    if not paths:
        if blank:
            sys.stderr.write("rc=%d %s blank with no surefire reports\\n" % (rc, runner))
            print(0)
            sys.exit()
        fallback()
    cases = []
    try:
        for p in paths:
            cases.extend(ET.parse(p).getroot().iter("testcase"))
    except Exception:
        print(0)
        sys.stderr.write("rc=%d %s unparsable surefire report\\n" % (rc, runner))
        sys.exit()
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None]
    print(1 if rc == 0 and cases and not bad else 0)
    sys.stderr.write("rc=%d %s surefire=%d cases=%d bad=%d\\n" % (rc, runner, len(paths), len(cases), len(bad)))
    sys.exit()
fallback()
PYEOF
echo "REWARD=$(cat "$V/reward.txt") rc=$RC runner=$(cat "$V/runner.txt")" | tee -a "$V/test_output.log"
"""


def render_wrapper_test_sh_v3(workdir: str) -> str:
    """Verifier entry point: @3 patch-only grading (never trusts agent state)."""
    if not workdir.startswith("/"):
        raise VariantInvalid("workdir must be an absolute path")
    universal = "[" + ", ".join(repr(s) for s in V3_TAMPER_SIGNATURES) + "]"
    scoped = (
        "["
        + ", ".join(f"({sorted(exts)!r}, {pattern!r})" for exts, pattern in V3_SCOPED_SIGNATURES)
        + "]"
    )
    hook_b64 = base64.b64encode(V3_CONFTEST_HOOK.encode("utf-8")).decode("ascii")
    return (
        (_V3_WRAPPER_A + _V3_WRAPPER_B + _V3_WRAPPER_C)
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v3(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@3)."""
    return (
        "# Separate-verifier image (separate-verifier@3): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v3(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @3.

    Same bundle shape as @2 (clean setup chain + patch-only grader, no
    ``tests/test-orig.sh`` kept); the grader is the multi-runner @3 entry
    point. ``solution_sh`` adds an oracle-control reference solution when
    the parent has none. Refuses to overwrite an existing solution.
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
        "tests/test.sh": render_wrapper_test_sh_v3(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v3(info.docker_image).encode("utf-8"),
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
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v3(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and structured per-runner checks.",
    created_by: str = "multi-runner-patch-only-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@3`` variant of a MiMo task package."""
    changes, inputs = build_changes_v3(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V3,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


# --------------------------------------------------------------------------- #
# separate-verifier@4 template, build, and derive
# --------------------------------------------------------------------------- #
# The @4 grader is the @3 grader with only the pytest named-id presence
# check replaced by the aligned path-suffix matcher (same semantics as
# :func:`junit_case_matches_expected`, inlined because the grader ships as
# a brace-heavy heredoc). Deriving the C block by replacement (pinned below)
# keeps the rest byte-identical to @3 by construction.

#: Exact @3 junit-matching lines replaced by the @4 suffix matcher.
_V3_JUNIT_MATCH_BLOCK = """    seen = {c.get("classname", "").replace(".", "/") + ".py::" + c.get("name", "") for c in cases}
    missing = [i for i in ids if not any(s == i or s.startswith(i + "[") for s in seen)]"""

#: @4 junit-matching replacement (aligned path-suffix; mirrors
#: :func:`junit_case_matches_expected`).
_V4_JUNIT_MATCH_BLOCK = """    # Option values (e.g. --deselect=path.py::test_x) are not selected node IDs.
    ids = {i for i in ids if not i.startswith("-")}
    def _comps(p):
        p = p.replace("\\\\", "/").strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p.endswith(".py"):
            p = p[:-3]
        return [c for c in re.split(r"[/.]+", p) if c]
    def _parse_node(n):
        parts = n.split("::")
        return (_comps(parts[0]), parts[1:-1], parts[-1])
    def _test_ok(seen_t, exp_t):
        if "[" in exp_t:
            return seen_t == exp_t
        return seen_t == exp_t or seen_t.startswith(exp_t + "[")
    def _tail(shorter, longer):
        return bool(shorter) and len(shorter) <= len(longer) and shorter == longer[len(longer) - len(shorter):]
    def _aligned(a, b):
        if not a or not b:
            return False
        s, l = (a, b) if len(a) <= len(b) else (b, a)
        return _tail(s, l)
    def _hit(cn, nm, fl, exp):
        em, ec, et = _parse_node(exp)
        if not em or not et:
            return False
        np = (nm or "").split("::")
        st = np[-1]
        extra = np[:-1]
        cp = (cn or "").split(".") if cn else []
        cands = []
        for cut in range(1, len(cp) + 1):
            cands.append((cp[:cut], cp[cut:] + extra, st))
        if fl:
            sm = _comps(fl)
            for cut in range(len(cp) + 1):
                if cut == 0 or _tail(cp[:cut], sm):
                    cands.append((sm, cp[cut:] + extra, st))
        class_level = not et.startswith("test_")
        for mod, cls, seen_t in cands:
            if not _aligned(mod, em):
                continue
            if cls == ec and _test_ok(seen_t, et):
                return True
            if class_level and cls == ec + [et]:
                return True
        return False
    missing = [i for i in ids if not any(_hit(c.get("classname"), c.get("name"), c.get("file"), i) for c in cases)]"""

assert _V3_JUNIT_MATCH_BLOCK in _V3_WRAPPER_C, (
    "@3 grader junit block moved; re-pin the @4 replacement"
)

#: @3 grader entry point, part C, with the @4 junit matcher.
_V4_WRAPPER_C = _V3_WRAPPER_C.replace(_V3_JUNIT_MATCH_BLOCK, _V4_JUNIT_MATCH_BLOCK)

assert _V4_WRAPPER_C != _V3_WRAPPER_C, "@4 replacement did not apply"

#: The verifier's complete post-setup workdir is the only trusted diff base.
#: Baked untracked/ignored dependencies are present before the agent runs, so
#: their unchanged runner code must not be mistaken for an agent addition.
_V4_PRISTINE_TREE = """# Capture tracked, untracked, and ignored files before importing agent bytes.
export GIT_INDEX_FILE=/tmp/pristine.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$BASE" || { echo "pristine-tree read failed" >&2; exit 1; }
git add -A -f . || { echo "pristine-tree capture failed" >&2; exit 1; }
PRISTINE=$(git write-tree) || { echo "pristine-tree write failed" >&2; exit 1; }
git diff --cached --name-only "$BASE" > "$V/pristine-extra-files.log"
unset GIT_INDEX_FILE
"""

_V4_WRAPPER_A = (
    _V3_WRAPPER_A.replace("$BASE", "$PRISTINE")
    .replace("# 1. The agent's change", _V4_PRISTINE_TREE + "# 1. The agent's change", 1)
    .replace(" add -A\n", " add -A -f\n")
    .replace(
        "cp /tmp/base.gitignore /tmp/agentcopy/.gitignore",
        'if git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:.gitignore" 2>/dev/null; then\n'
        "  cp /tmp/base.gitignore /tmp/agentcopy/.gitignore\n"
        "else rm -f /tmp/agentcopy/.gitignore; fi",
    )
)
_V4_WRAPPER_B = _V3_WRAPPER_B.replace("$BASE", "$PRISTINE").replace(
    'git add -A >/dev/null 2>&1 && git diff --cached "$PRISTINE" > "$V/agent.kept.diff"; git reset -q',
    """export GIT_INDEX_FILE=/tmp/kept.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$PRISTINE" && git add -A -f . && git diff --cached "$PRISTINE" > "$V/agent.kept.diff" || {
  echo "kept-tree diff failed" >&2; exit 1
}
unset GIT_INDEX_FILE""",
)


def render_wrapper_test_sh_v4(workdir: str) -> str:
    """Verifier entry point: @4 pristine-workdir delta and rootdir-robust junit."""
    if not workdir.startswith("/"):
        raise VariantInvalid("workdir must be an absolute path")
    universal = "[" + ", ".join(repr(s) for s in V3_TAMPER_SIGNATURES) + "]"
    scoped = (
        "["
        + ", ".join(f"({sorted(exts)!r}, {pattern!r})" for exts, pattern in V3_SCOPED_SIGNATURES)
        + "]"
    )
    hook_b64 = base64.b64encode(V3_CONFTEST_HOOK.encode("utf-8")).decode("ascii")
    return (
        (_V4_WRAPPER_A + _V4_WRAPPER_B + _V4_WRAPPER_C)
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v4(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@4)."""
    return (
        "# Separate-verifier image (separate-verifier@4): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v4(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @4.

    Same bundle shape as @3 (clean setup chain + patch-only grader, no
    ``tests/test-orig.sh`` kept); the grader is the @4 entry point with the
    rootdir-robust junit matcher. ``solution_sh`` adds an oracle-control
    reference solution when the parent has none. Refuses to overwrite an
    existing solution.
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
        "tests/test.sh": render_wrapper_test_sh_v4(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v4(info.docker_image).encode("utf-8"),
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
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v4(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and structured per-runner checks.",
    created_by: str = "rootdir-robust-patch-only-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@4`` variant of a MiMo task package."""
    changes, inputs = build_changes_v4(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V4,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


# --------------------------------------------------------------------------- #
# separate-verifier@5 template, build, and derive
# --------------------------------------------------------------------------- #
# @5 keeps every @4 guarantee and fixes two oracle false-0 shapes measured on
# exact-v3 packages (fleet census 2026-10-10):
#
# 1. Skipped testcases no longer count as bad. Reference test patches
#    legitimately mark still-failing behaviors skipped (000163:
#    @skip_or_fail, 35 passed + 41 skipped, pytest rc 0), and environments
#    skip tests for missing optional dependencies (000203: 31 passed + 9
#    skipped, rc 0). @4 graded both reward 0 while the reference fixes pass
#    under the published exit-code grading. Anti-cheat is preserved: agent
#    test-infra (conftest.py, pytest.ini, sitecustomize, *.pth, pytest config
#    keys) is still dropped from the kept change, the missing-ID gate still
#    fires for deselected named tests, and an empty case set still grades 0.
#
# 2. Multi-phase pytest commands no longer grade only the last phase. Every
#    pytest invocation used to overwrite the single junit.xml, so named IDs
#    selected by earlier phases read as missing (000200: `base && new`, the
#    three base classes missing although both phases pass). @5 appends a
#    verifier-owned per-phase archiver to the root conftest for every pytest
#    run and grades the union of the plugin report with all archived phases.
#
# Deriving by pinned replacement (like @4) keeps the rest byte-identical to
# @4 by construction.

#: Transform id recorded in lineage.
TRANSFORM_ID_V5 = "separate-verifier@5"


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

#: Exact @4 pytest/surefire grading line replaced by the @5 skip-tolerant
#: rule (failures/errors are bad; skips are reported, not failures).
_V4_BAD_LINE = '    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None]'

_V5_BAD_LINE = (
    '    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]\n'
    '    skipped = [c for c in cases if c.find("skipped") is not None]'
)

#: Exact @4 pytest report-loading block (single junit.xml) replaced by the
#: @5 union loader (plugin report plus per-phase hook archives). A corrupt
#: main report keeps today's missing-report path; corrupt archives are
#: skipped; with neither, grading falls through to it as well.
_V4_PYTEST_LOAD_BLOCK = """    try:
        raw = open(junit_path, "rb").read()
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()
    if not raw:
        grade_noreport()"""

_V5_PYTEST_LOAD_BLOCK = """    arch = []
    for phase in sorted(glob.glob(os.path.join(os.path.dirname(junit_path), "junit-phases", "junit-hook-*.xml"))):
        try:
            praw = open(phase, "rb").read()
        except Exception:
            continue
        if praw:
            arch.append(praw)
    try:
        raw = open(junit_path, "rb").read()
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()
    if not raw and not arch:
        grade_noreport()
    for rep in arch:
        try:
            cases.extend(list(ET.fromstring(rep).iter("testcase")))
        except Exception:
            pass"""

#: Exact @4 pytest stderr line extended with the skip count.
_V4_PYTEST_LOG_LINE = '    sys.stderr.write("rc=%d cases=%d bad=%d named=%d missing=%s\\n" % (rc, len(cases), len(bad), len(ids), missing))'

_V5_PYTEST_LOG_LINE = '    sys.stderr.write("rc=%d cases=%d bad=%d skipped=%d named=%d missing=%s\\n" % (rc, len(cases), len(bad), len(skipped), len(ids), missing))'

#: Exact @4 surefire stderr line extended with the skip count.
_V4_SUREFIRE_LOG_LINE = '    sys.stderr.write("rc=%d %s surefire=%d cases=%d bad=%d\\n" % (rc, runner, len(paths), len(cases), len(bad)))'

_V5_SUREFIRE_LOG_LINE = '    sys.stderr.write("rc=%d %s surefire=%d cases=%d bad=%d skipped=%d\\n" % (rc, runner, len(paths), len(cases), len(bad), len(skipped)))'

#: Exact @4 run head extended with the per-phase archive directory.
_V4_RUN_HEAD = 'JUNIT=$GRADE/junit.xml; mkdir -p "${JUNIT%/*}"'

_V5_RUN_HEAD = """JUNIT=$GRADE/junit.xml; mkdir -p "${JUNIT%/*}"
# @5 per-phase junit archive: every pytest session appends its outcomes here
# so multi-phase commands grade all phases, not just the last report.
JUNIT_PHASES=$GRADE/junit-phases; mkdir -p "$JUNIT_PHASES"
"""

#: Exact @4 pytest run line extended with the archive directory env.
_V4_RUN_LINE = 'PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1'

_V5_RUN_LINE = 'MIMO_VERIFIER_ARCHIVE_DIR="$JUNIT_PHASES" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1'

assert _V4_WRAPPER_C.count(_V4_BAD_LINE) == 2, "@4 grader bad line moved; re-pin the @5 replacement"
assert _V4_WRAPPER_C.count(_V4_PYTEST_LOAD_BLOCK) == 1, (
    "@4 pytest report loader moved; re-pin the @5 replacement"
)
assert _V4_WRAPPER_C.count(_V4_PYTEST_LOG_LINE) == 1, (
    "@4 pytest grade line moved; re-pin the @5 replacement"
)
assert _V4_WRAPPER_C.count(_V4_SUREFIRE_LOG_LINE) == 1, (
    "@4 surefire grade line moved; re-pin the @5 replacement"
)
assert _V4_WRAPPER_C.count(_V4_RUN_HEAD) == 1, "@4 run head moved; re-pin the @5 replacement"
assert _V4_WRAPPER_C.count(_V4_RUN_LINE) == 1, "@4 run line moved; re-pin the @5 replacement"

#: @5 grader entry point, part C: @4 with skip-tolerant grading, the
#: multi-phase union loader, and the archive directory on the run line.
_V5_WRAPPER_C = (
    _V4_WRAPPER_C.replace(_V4_BAD_LINE, _V5_BAD_LINE)
    .replace(_V4_PYTEST_LOAD_BLOCK, _V5_PYTEST_LOAD_BLOCK)
    .replace(_V4_PYTEST_LOG_LINE, _V5_PYTEST_LOG_LINE)
    .replace(_V4_SUREFIRE_LOG_LINE, _V5_SUREFIRE_LOG_LINE)
    .replace(_V4_RUN_HEAD, _V5_RUN_HEAD)
    .replace(_V4_RUN_LINE, _V5_RUN_LINE)
)

assert _V5_WRAPPER_C != _V4_WRAPPER_C, "@5 replacement did not apply"

#: Exact @4 hook-install block extended with the unconditional @5 archiver.
#: The archiver goes to the root conftest AND to the conftest of every
#: test-file directory named by the resolved command (existence-grounded):
#: a multi-phase command with a nested rootdir (e.g. rootdir=/testbed/tests)
#: never collects the workdir root conftest, while a test file's own
#: directory conftest always loads for that file. Appends are marker-guarded
#: and hook-only, so extra copies can only add union content, never change
#: test behavior; commands naming no files degrade to the root copy.
_V4_HOOK_INSTALL_BLOCK = """  if [ "$CLEARED" = 1 ]; then
    echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
    touch "$GRADE/hook_installed"
    echo "installed verifier conftest hook (addopts cleared)" >> "$V/dropped.log"
  fi
fi"""

_V5_HOOK_INSTALL_BLOCK = """  if [ "$CLEARED" = 1 ]; then
    echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
    touch "$GRADE/hook_installed"
    echo "installed verifier conftest hook (addopts cleared)" >> "$V/dropped.log"
  fi
  python3 - "$RESOLVED" "$CWD" > "$V/conftest-targets.txt" 2>/dev/null <<'PYEOF' || true
import os, re, sys
resolved, cwd = sys.argv[1], sys.argv[2]
seen = []
for tok in re.findall(r'[A-Za-z0-9_./-]+\\.py', resolved):
    rel = tok
    if os.path.isabs(rel):
        try:
            rel = os.path.relpath(rel, cwd)
        except ValueError:
            continue
    if rel.startswith(".."):
        continue
    if os.path.isfile(os.path.join(cwd, rel)):
        d = os.path.dirname(rel) or "."
        if d not in seen:
            seen.append(d)
for d in ["."] + seen:
    print(d)
PYEOF
  while IFS= read -r _v5_d; do
    [ -n "$_v5_d" ] || continue
    _v5_cf="$CWD/$_v5_d/conftest.py"
    mkdir -p "$(dirname "$_v5_cf")"
    if ! grep -q "mimo-junit-archive/1" "$_v5_cf" 2>/dev/null; then
      echo "@@V5_ARCHIVE_HOOK_B64@@" | base64 -d >> "$_v5_cf"
    fi
  done < "$V/conftest-targets.txt"
  echo "installed verifier junit archive hook" >> "$V/dropped.log"
fi"""

assert _V4_WRAPPER_B.count(_V4_HOOK_INSTALL_BLOCK) == 1, (
    "@4 hook-install block moved; re-pin the @5 replacement"
)

#: @5 grader entry point, part B: @4 with the archive hook install.
_V5_WRAPPER_B = _V4_WRAPPER_B.replace(_V4_HOOK_INSTALL_BLOCK, _V5_HOOK_INSTALL_BLOCK)

assert _V5_WRAPPER_B != _V4_WRAPPER_B, "@5 hook replacement did not apply"

#: @5 grader entry point, part A: identical to @4.
_V5_WRAPPER_A = _V4_WRAPPER_A


def render_wrapper_test_sh_v5(workdir: str) -> str:
    """Verifier entry point: @5 skip-tolerant, multi-phase junit grading."""
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
    return (
        (_V5_WRAPPER_A + _V5_WRAPPER_B + _V5_WRAPPER_C)
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@V5_ARCHIVE_HOOK_B64@@", archive_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v5(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@5)."""
    return (
        "# Separate-verifier image (separate-verifier@5): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v5(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @5.

    Same bundle shape as @4; the grader is the @5 entry point with
    skip-tolerant junit grading and the multi-phase archive hook.
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
        "tests/test.sh": render_wrapper_test_sh_v5(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v5(info.docker_image).encode("utf-8"),
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
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v5(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and structured per-runner checks.",
    created_by: str = "skip-tolerant-multi-phase-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@5`` variant of a MiMo task package."""
    changes, inputs = build_changes_v5(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V5,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


# --------------------------------------------------------------------------- #
# separate-verifier@6 template, build, and derive
# --------------------------------------------------------------------------- #
# @6 keeps every @5 guarantee and closes the source-skip hole with a
# fail-to-pass rule. @5 grades failures/errors only and ignores skips; a
# source-level skip in a module the hidden tests import needs no knowledge of
# the tests and still grades 1 with zero tests passing (measured on @5
# packages: 002552 pytest rc 0 with every case skipped and every named ID
# present; 000803 unittest ``Ran 8 tests OK (skipped=8)``; go ``ok ...
# [no tests to run]`` rc 0 — the first two via call-time hooks from the
# imported package, the third via an init hook narrowing ``-test.run``; none
# matches a tamper signature). The literal module-level one-liner
# (``pytest.skip(..., allow_module_level=True)`` / ``raise SkipTest``) never
# reaches reward 1 (conftest-startup crash rc 1, or whole-module
# collection skips with rc 5 and missing IDs), so @5's skip tolerance looked
# safe from the ladder: it is not.
#
# @6 runs the hidden tests on its pristine tree first (test patch applied, no
# agent bytes) and records per-test outcomes. Reward 1 then requires the
# fail-to-pass shape: rc success AND every test that failed/errored on
# pristine now PASSES (not skipped, not vanished) AND no test that passed on
# pristine now fails/errors; skips are tolerated only for tests also skipped
# on pristine. Pytest compares junit unions (multi-phase included, @4
# named-ID matching kept); unittest compares run/skip counts; go-test keeps
# its markers plus a non-empty-run rule. With no structured baseline report,
# grading keeps the existing exit-code fallback rules. Deriving by pinned
# replacement (like @4/@5) keeps the rest byte-identical to @5.

#: Transform id recorded in lineage.
TRANSFORM_ID_V6 = "separate-verifier@6"


#: @6 baseline block: hidden tests on the pristine tree before any agent byte
#: is imported. The tree at this point IS pristine (setup just ran), so the
#: hidden-test apply, command resolution, pytest hook install, and test run
#: mirror the agent path below. Only pytest/unittest runs execute (the only
#: consumers of a baseline); every other runner still applies, resolves, and
#: resets so the runner decision stays grounded. Afterwards the tree returns
#: to exactly ``$PRISTINE``: tracked modifications via the pristine tree
#: object, created files via the pre/post untracked difference (baked files
#: exist in both listings and are never deleted). A broken baseline never
#: fails grading by itself: with no baseline artifacts, grading keeps @5.
#: The conftest-target computer uses its own heredoc delimiter so the
#: ``<<'PYEOF'`` grader extractor keeps finding exactly three blocks.
_V6_BASELINE_BLOCK = """# @6 baseline: hidden tests on the pristine tree (no agent bytes), for the
# fail-to-pass rule. Afterwards the tree returns to exactly $PRISTINE.
export GIT_INDEX_FILE=/tmp/baseline.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$PRISTINE" || { echo "baseline index failed" >&2; exit 1; }
git ls-files --others --ignored --exclude-standard -z > "$V/pre_ignored.bin" 2>/dev/null || : > "$V/pre_ignored.bin"
BTESTFILES=$(grep '^diff --git' /tests/test.patch | sed 's#.* b/##')
printf '%s\\n' "$BTESTFILES" | while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e "$PRISTINE:$tf" 2>/dev/null; then
    git checkout -q "$PRISTINE" -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done
if git apply --verbose /tests/test.patch > "$V/baseline_apply.log" 2>&1; then
  RES_B=$(cat /tests/test_command.sh)
  for ref in $(grep -oE '[^ "]*mimo_test_command\\.sh' /tests/test_command.sh | sort -u); do
    base=${ref##*/}
    if [ -f "$base" ]; then RES_B="$RES_B
$(cat "$base")"; fi
  done
  if printf '%s' "$RES_B" | grep -q 'build_env/test_command'; then
    if [ -f mimo_build_env.tar.gz.b64 ]; then
      rm -rf "$GRADE/build_env" && mkdir -p "$GRADE/build_env"
      if base64 -d mimo_build_env.tar.gz.b64 2>/dev/null | tar -xzf - -C "$GRADE/build_env" 2>/dev/null; then
        BE_TC=$(find "$GRADE/build_env" -name 'test_command.sh' | head -1)
        if [ -n "$BE_TC" ]; then RES_B="$RES_B
$(cat "$BE_TC")"; fi
      fi
    fi
  fi
  printf '%s' "$RES_B" > "$V/baseline_resolved.txt"
  RUNNER_B=custom
  if printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])go +test( |$)'; then RUNNER_B=go-test
  elif printf '%s' "$RES_B" | grep -qE 'GO_BIN" +test( |$)'; then RUNNER_B=go-test
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])jest([^[:alnum:]_]|$)'; then RUNNER_B=jest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])vitest([^[:alnum:]_]|$)'; then RUNNER_B=vitest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])_?mocha([^[:alnum:]_]|$)'; then RUNNER_B=mocha
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])ava([^[:alnum:]_]|$)'; then RUNNER_B=ava
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])tap([^[:alnum:]_]|$)'; then RUNNER_B=tap
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])karma([^[:alnum:]_]|$)'; then RUNNER_B=karma
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])jasmine([^[:alnum:]_]|$)'; then RUNNER_B=jasmine
  elif printf '%s' "$RES_B" | grep -qE 'node +--test([^[:alnum:]_]|$)'; then RUNNER_B=node-test
  elif printf '%s' "$RES_B" | grep -qE 'cargo +test([^[:alnum:]_]|$)'; then RUNNER_B=cargo-test
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])rspec([^[:alnum:]_]|$)'; then RUNNER_B=rspec
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])phpunit([^[:alnum:]_]|$)'; then RUNNER_B=phpunit
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])mvn([^[:alnum:]_]|$)|surefire|failsafe'; then RUNNER_B=mvn
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])gradlew?([^[:alnum:]_]|$)'; then RUNNER_B=gradle
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])pytest([^[:alnum:]_]|$)'; then RUNNER_B=pytest
  elif printf '%s' "$RES_B" | grep -qE '\\-m +unittest([^[:alnum:]_]|$)'; then RUNNER_B=unittest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])forge +test([^[:alnum:]_]|$)'; then RUNNER_B=forge
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])node([^[:alnum:]_]|$)'; then RUNNER_B=node-run
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])(make|ctest|cmake)([^[:alnum:]_]|$)'; then RUNNER_B=make
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])bats([^[:alnum:]_]|$)'; then RUNNER_B=bats
  fi
  echo "RUNNER_B=$RUNNER_B" > "$V/baseline_runner.txt"
  : > "$V/baseline.log"
  if [ "$RUNNER_B" = pytest ]; then
    CLEARED_B=0
    printf '%s' "$RES_B" | grep -q 'unset.*PYTEST_ADDOPTS' && CLEARED_B=1
    printf '%s' "$RES_B" | grep -q 'PYTEST_ADDOPTS=' && CLEARED_B=1
    printf '%s' "$RES_B" | grep -qE 'env +([^ ]+ +)*-u([^ ]* +)*PYTEST_ADDOPTS|env +-u +PYTEST_ADDOPTS' && CLEARED_B=1
    if [ "$CLEARED_B" = 1 ]; then
      echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
      echo "installed verifier conftest hook (addopts cleared)" >> "$V/baseline.log"
    fi
    python3 - "$RES_B" "$CWD" > "$V/baseline-conftest-targets.txt" 2>/dev/null <<'PYEOF_BASELINE' || true
import os, re, sys
resolved, cwd = sys.argv[1], sys.argv[2]
seen = []
for tok in re.findall(r'[A-Za-z0-9_./-]+\\.py', resolved):
    rel = tok
    if os.path.isabs(rel):
        try:
            rel = os.path.relpath(rel, cwd)
        except ValueError:
            continue
    if rel.startswith(".."):
        continue
    if os.path.isfile(os.path.join(cwd, rel)):
        d = os.path.dirname(rel) or "."
        if d not in seen:
            seen.append(d)
for d in ["."] + seen:
    print(d)
PYEOF_BASELINE
    while IFS= read -r _v5_d; do
      [ -n "$_v5_d" ] || continue
      _v5_cf="$CWD/$_v5_d/conftest.py"
      mkdir -p "$(dirname "$_v5_cf")"
      if ! grep -q "mimo-junit-archive/1" "$_v5_cf" 2>/dev/null; then
        echo "@@V5_ARCHIVE_HOOK_B64@@" | base64 -d >> "$_v5_cf"
      fi
    done < "$V/baseline-conftest-targets.txt"
    echo "installed verifier junit archive hook" >> "$V/baseline.log"
    # The baseline runs to completion: -x/--exitfirst/--maxfail (from the
    # command, ini, or env) would truncate it at the first failure and hide
    # later skips/failures from fail-to-pass (measured: 000203 oracle
    # false-0). Neutralize maxfail via a wrapping pytest_configure hook
    # (previous same-file hook still runs); the reset below removes it, so
    # the agent path keeps the original command.
    while IFS= read -r _v6_d; do
      [ -n "$_v6_d" ] || continue
      _v6_cf="$CWD/$_v6_d/conftest.py"
      if ! grep -q "mimo-baseline-noexitfirst/1" "$_v6_cf" 2>/dev/null; then
        cat >> "$_v6_cf" <<'PYEOF_NOEXITFIRST' || true
# mimo-baseline-noexitfirst/1: run the pristine baseline to completion.
_prev_baseline_configure = globals().get("pytest_configure")
def pytest_configure(config):
    try:
        if _prev_baseline_configure is not None:
            _prev_baseline_configure(config)
    except Exception:
        pass
    try:
        if getattr(config.option, "maxfail", 0):
            config.option.maxfail = 1000000
    except Exception:
        pass
PYEOF_NOEXITFIRST
      fi
    done < "$V/baseline-conftest-targets.txt"
    echo "installed baseline noexitfirst hook" >> "$V/baseline.log"
  fi
  if [ "$RUNNER_B" = pytest ] || [ "$RUNNER_B" = unittest ]; then
    JUNIT_B=$GRADE/baseline/junit.xml; mkdir -p "${JUNIT_B%/*}" "$GRADE/baseline/junit-phases"
    START_B=$(date +%s)
    MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1
    echo "$?" > "$V/baseline_rc.txt"
    echo "baseline_sec=$(($(date +%s) - START_B)) runner=$RUNNER_B rc=$(cat "$V/baseline_rc.txt")" > "$V/timing.log"
    cp "$V/baseline_output.log" "$GRADE/baseline/output.log"
  else
    echo "no baseline consumer for runner $RUNNER_B" >> "$V/baseline.log"
  fi
else
  echo "baseline hidden-test apply failed; grading keeps @5 rules" > "$V/baseline.log"
fi
# Reset to pristine bytes exactly (always, even without a baseline run).
git read-tree "$PRISTINE" || { echo "baseline reset failed" >&2; exit 1; }
git checkout-index -f -a || { echo "baseline restore failed" >&2; exit 1; }
git clean -fdq || { echo "baseline clean failed" >&2; exit 1; }
git ls-files --others --ignored --exclude-standard -z > /tmp/post_ignored.bin 2>/dev/null || : > /tmp/post_ignored.bin
if [ -s /tmp/post_ignored.bin ] || [ -s "$V/pre_ignored.bin" ]; then
  comm -z -13 "$V/pre_ignored.bin" /tmp/post_ignored.bin 2>/dev/null | xargs -0 -r rm -rf -- 2>/dev/null || true
fi
unset GIT_INDEX_FILE
"""

#: Exact @5 wrapper-A insertion point (pristine capture, then the agent diff).
_V6_A_INSERT = _V4_PRISTINE_TREE + "# 1. The agent's change"

assert _V5_WRAPPER_A.count(_V6_A_INSERT) == 1, (
    "@5 wrapper A moved; re-pin the @6 baseline insertion"
)

#: @6 grader entry point, part A: @5 with the pristine baseline run.
_V6_WRAPPER_A = _V5_WRAPPER_A.replace(
    _V6_A_INSERT, _V4_PRISTINE_TREE + _V6_BASELINE_BLOCK + "# 1. The agent's change"
)

assert _V6_WRAPPER_A != _V5_WRAPPER_A, "@6 baseline insertion did not apply"

#: @6 grader entry point, part B: identical to @5.
_V6_WRAPPER_B = _V5_WRAPPER_B

#: Exact @5 pytest grade line extended with the fail-to-pass gate.
_V5_PYTEST_PRINT = (
    "    print(1 if rc == 0 and cases and not bad and not missing else 0)"
)

#: @6 pytest grade block: baseline union plus the fail-to-pass comparison,
#: mirroring :func:`evaluate_junit_v6` (worst-outcome maps, unmatched agent
#: skips and unpreserved pristine outcomes fail). Absent baseline: @5 rules.
_V6_PYTEST_FTP_BLOCK = """    base_union = os.path.join(os.path.dirname(junit_path), "baseline")
    base_cases = []
    try:
        with open(os.path.join(base_union, "junit.xml"), "rb") as _bf:
            _braw = _bf.read()
    except Exception:
        _braw = b""
    if _braw:
        try:
            base_cases.extend(ET.fromstring(_braw).iter("testcase"))
        except Exception:
            base_cases = []
    for _bphase in sorted(glob.glob(os.path.join(base_union, "junit-phases", "junit-hook-*.xml"))):
        try:
            _bpraw = open(_bphase, "rb").read()
        except Exception:
            continue
        if _bpraw:
            try:
                base_cases.extend(ET.fromstring(_bpraw).iter("testcase"))
            except Exception:
                pass
    ftp_bad = False
    if base_cases:
        def _v6st(_c):
            if _c.find("failure") is not None or _c.find("error") is not None:
                return "bad"
            if _c.find("skipped") is not None:
                return "skipped"
            return "passed"
        def _v6key(_c):
            return (_c.get("classname") or "", _c.get("name") or "", _c.get("file") or "")
        _v6rank = {"passed": 0, "skipped": 1, "bad": 2}
        _bm = {}
        for _c in base_cases:
            _k = _v6key(_c)
            _s = _v6st(_c)
            if _k not in _bm or _v6rank[_s] > _v6rank[_bm[_k]]:
                _bm[_k] = _s
        _am = {}
        for _c in cases:
            _k = _v6key(_c)
            _s = _v6st(_c)
            if _k not in _am or _v6rank[_s] > _v6rank[_am[_k]]:
                _am[_k] = _s
        _bs = set(_k for _k, _s in _bm.items() if _s == "skipped")
        for _k, _s in _bm.items():
            if _s != "skipped" and _am.get(_k) != "passed":
                ftp_bad = True
        for _k, _s in _am.items():
            if _s == "skipped" and _k not in _bs:
                ftp_bad = True
    if ftp_bad:
        sys.stderr.write("fail-to-pass: pristine outcomes not preserved\\n")
    print(1 if rc == 0 and cases and not bad and not missing and not ftp_bad else 0)"""

#: Exact @5 unittest grade line extended with the baseline count rule.
_V5_UNITTEST_OK = '        ok = (m is not None and int(m.group(1)) >= 1 and re.search(r"^OK\\b", output, re.M) is not None and re.search(r"^(FAILED|ERROR)", output, re.M) is None)'

#: @6 unittest grade lines: with a parsable pristine baseline, the run must
#: execute at least as many tests and skip at most as many, mirroring
#: :func:`evaluate_unittest_v6`. Absent baseline: @5 rules.
_V6_UNITTEST_OK = (
    _V5_UNITTEST_OK
    + """
        # @6 fail-to-pass counts vs the pristine baseline when available.
        _v6base = ""
        try:
            with open(os.path.join(os.path.dirname(junit_path), "baseline", "output.log"), encoding="utf-8", errors="replace") as _v6f:
                _v6base = _v6f.read()
        except Exception:
            pass
        _v6bm = re.search(r"Ran (\\d+) test", _v6base)
        if _v6bm is not None:
            _v6bran = int(_v6bm.group(1))
            _v6bs = re.search(r"skipped=(\\d+)", _v6base)
            _v6bskip = int(_v6bs.group(1)) if _v6bs else 0
            _v6as = re.search(r"skipped=(\\d+)", output)
            _v6askip = int(_v6as.group(1)) if _v6as else 0
            _v6aran = int(m.group(1)) if m else 0
            if _v6aran < _v6bran or _v6askip > _v6bskip:
                ok = False
                sys.stderr.write("unittest fail-to-pass: ran=%d<%d or skipped=%d>%d\\n" % (_v6aran, _v6bran, _v6askip, _v6bskip))"""
)

#: Exact @5 go-test grade line extended with the non-empty-run rule.
_V5_GO_OK = '        ok = re.search(r"^ok\\s+\\S+", output, re.M) is not None and re.search(r"^(FAIL|--- FAIL|panic:)", output, re.M) is None'

#: @6 go-test grade line: an empty selection grades 0, mirroring
#: :func:`evaluate_go_output_v6`.
_V6_GO_OK = (
    '        ok = re.search(r"^ok\\s+\\S+", output, re.M) is not None and re.search(r"^(FAIL|--- FAIL|panic:)", output, re.M) is None and "no tests to run" not in output'
)

#: Exact @5 agent run lines wrapped with grading-time measurement: the grade
#: reads ``RC`` captured immediately after the test command, so timing lands
#: after the capture, never between the run and ``RC=$?`` (which would force
#: rc 0 for every trial).
_V5_RUN_LINE_TIMED = _V5_RUN_LINE + "\nRC=$?"

#: @6 agent run lines: the command is unchanged; wall time lands in timing.log.
_V6_RUN_TIMED = (
    "_START_A=$(date +%s)\n"
    + _V5_RUN_LINE
    + "\nRC=$?"
    + '\necho "agent_sec=$(($(date +%s) - _START_A))" >> "$V/timing.log"'
)

#: Exact @5 plugin-report read: any ``open()`` failure (including a missing
#: file) takes the missing-report path, even when per-phase archives exist.
_V5_PLUGIN_READ = """    try:
        raw = open(junit_path, "rb").read()
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()"""

#: @6 plugin-report read: a missing/unreadable plugin report grades from the
#: per-phase archives (measured: ADDOPTS-cleared runs such as 000666 never
#: write the plugin report, and the archiver shadows the V3 hook's report,
#: so @5 graded every such oracle 0 via the missing-report path). A corrupt
#: (present but unparsable) report keeps the missing-report path, as in @5.
_V6_PLUGIN_READ = """    try:
        raw = open(junit_path, "rb").read()
    except OSError:
        raw = b""
    try:
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()"""


assert _V5_WRAPPER_C.count(_V5_PYTEST_PRINT) == 1, (
    "@5 pytest grade line moved; re-pin the @6 replacement"
)
assert _V5_WRAPPER_C.count(_V5_UNITTEST_OK) == 1, (
    "@5 unittest grade line moved; re-pin the @6 replacement"
)
assert _V5_WRAPPER_C.count(_V5_GO_OK) == 1, (
    "@5 go grade line moved; re-pin the @6 replacement"
)
assert _V5_WRAPPER_C.count(_V5_RUN_LINE_TIMED) == 1, (
    "@5 run line moved; re-pin the @6 replacement"
)
assert _V5_WRAPPER_C.count(_V5_PLUGIN_READ) == 1, (
    "@5 plugin read moved; re-pin the @6 replacement"
)

#: @6 grader entry point, part C: @5 with fail-to-pass pytest/unittest
#: grading, the go non-empty-run rule, agent timing, and archive-tolerant
#: plugin loading.
_V6_WRAPPER_C = (
    _V5_WRAPPER_C.replace(_V5_PYTEST_PRINT, _V6_PYTEST_FTP_BLOCK)
    .replace(_V5_UNITTEST_OK, _V6_UNITTEST_OK)
    .replace(_V5_GO_OK, _V6_GO_OK)
    .replace(_V5_RUN_LINE_TIMED, _V6_RUN_TIMED)
    .replace(_V5_PLUGIN_READ, _V6_PLUGIN_READ)
)

assert _V6_WRAPPER_C != _V5_WRAPPER_C, "@6 replacement did not apply"


def render_wrapper_test_sh_v6(workdir: str) -> str:
    """Verifier entry point: @6 fail-to-pass grading over a pristine baseline."""
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
    return (
        (_V6_WRAPPER_A + _V6_WRAPPER_B + _V6_WRAPPER_C)
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@V5_ARCHIVE_HOOK_B64@@", archive_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v6(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@6)."""
    return (
        "# Separate-verifier image (separate-verifier@6): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v6(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @6.

    Same bundle shape as @5; the grader is the @6 entry point with the
    pristine-baseline fail-to-pass rule. ``solution_sh`` adds an
    oracle-control reference solution when the parent has none. Refuses to
    overwrite an existing solution.
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
        "tests/test.sh": render_wrapper_test_sh_v6(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v6(info.docker_image).encode("utf-8"),
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
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v6(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and fail-to-pass checks.",
    created_by: str = "fail-to-pass-baseline-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@6`` variant of a MiMo task package."""
    changes, inputs = build_changes_v6(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V6,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )


# --------------------------------------------------------------------------- #
# separate-verifier@7: G-shape certification gaps (v4 census follow-ups)
# --------------------------------------------------------------------------- #

#: Transform id recorded in lineage.
TRANSFORM_ID_V7 = "separate-verifier@7"


#: ANSI/color/control escapes stripped before marker parsing (G4, @7):
#: CSI color/style sequences, charset selections, carriage returns.
_V7_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\r")


def strip_ansi(test_output: str) -> str:
    """Remove ANSI escapes from runner output before marker parsing (@7).

    Vitest/jest colorize the summary words themselves (measured 000828:
    ``Tests <ESC>[22m <ESC>[1m<ESC>[32m14 passed``), which splits every
    marker regex. Stripping only adds matches for text already present;
    failure markers survive (they contain no escapes).
    """
    return _V7_ANSI_RE.sub("", test_output)


#: Bare PASS line printed by a directly executed ``go test -c`` binary
#: (the ``go test`` tool's ``ok <pkg>`` line never appears; measured 000248).
_GO_BARE_PASS_RE = re.compile(r"^PASS\s*$", re.MULTILINE)


def evaluate_go_output_v7(test_output: str, rc: int) -> int:
    """Grade 1/0 from ``go test`` output with per-package emptiness (G1) and direct-binary PASS (G3) (@7).

    G1: the @6 ``no tests to run`` substring overfires on multi-package
    ``go test ./...`` runs where a test-less sub-package prints the marker
    while real packages pass (measured 000048: real ``--- PASS`` plus
    ``ok``). @7 ignores marker lines and requires a surviving ``ok`` line;
    an emptied selection (measured 000553: only ``ok pkg [no tests to run]``)
    still grades 0.
    G3: a directly executed test binary prints a bare ``PASS`` with no
    ``ok`` line (measured 000248, rc 0). Bare ``PASS`` counts only with no
    emptiness marker anywhere, so the 000553 shape cannot ride it; the
    ``os.Exit`` tamper gate still catches exit-subverting inits.
    """
    if rc != 0:
        return 0
    clean = strip_ansi(test_output)
    if _GO_FAIL_RE.search(clean):
        return 0
    survivors = [
        line
        for line in clean.splitlines()
        if _GO_OK_RE.match(line) is not None and "no tests to run" not in line
    ]
    if "no tests to run" in clean:
        return 1 if survivors else 0
    if survivors or _GO_BARE_PASS_RE.search(clean) is not None:
        return 1
    return 0


#: Jest/vitest pass summaries with skipped segments first
#: (measured 000133: ``Tests: 6 skipped, 2 passed, 8 total``). Vitest
#: requires the ``Tests`` line itself to carry passes: an all-skipped run
#: still prints ``Test Files N passed``, so the ``Test Files`` alternative
#: would certify zero passing tests.
_JEST_PASS_RE_V7 = re.compile(r"Tests:\s+[^\n]*\d+\s+passed")
_VITEST_PASS_RE_V7 = re.compile(r"Tests\s+[^\n]*\d+\s+passed")

#: Spec-style reporter selection on a tap-labeled command
#: (measured 000934: ``tap ... -R spec``).
_V7_SPEC_REPORTER_RE = re.compile(r"(^|\s)-R\s+\S*spec\b|reporters?\s*[=:]\s*\S*spec\b")

#: TAP markers (line-anchored; indented summary ``ok`` lines do not count).
_V7_TAP_MARKER_RE = re.compile(r"^(not )?ok\b", re.MULTILINE)


def effective_js_runner(runner: str, command_text: str, test_output: str) -> str:
    """Actual reporter for tap-labeled commands emitting spec output (G6, @7).

    A tap-labeled command selecting a spec-style reporter (or whose output
    carries mocha ``N passing`` markers with no TAP ``ok`` lines, measured
    000934/001839) is graded with the mocha markers; every other runner is
    returned unchanged. Genuine TAP output keeps its ``^ok`` lines, so the
    switch never fires there.
    """
    if runner != "tap":
        return runner
    if _V7_SPEC_REPORTER_RE.search(command_text or ""):
        return "mocha"
    clean = strip_ansi(test_output)
    if _MOCHA_PASS_RE.search(clean) and not _V7_TAP_MARKER_RE.search(clean):
        return "mocha"
    return runner


def evaluate_js_output_v7(
    runner: str, test_output: str, rc: int, command_text: str = ""
) -> int:
    """Grade 1/0 from JS output with skipped-first summaries (G2), ANSI stripping (G4), actual-reporter detection (G6) (@7).

    Keeps every @3 rule; jest/vitest tolerate skipped segments before the
    passed count (an all-skipped summary has no passed segment and still
    grades 0); tap switches to the mocha markers only when no TAP marker
    exists, so TAP failure output still grades 0.
    """
    if rc != 0:
        return 0
    clean = strip_ansi(test_output)
    effective = effective_js_runner(runner, command_text, clean)
    if effective == "jest":
        return (
            1 if _JEST_PASS_RE_V7.search(clean) and not _JEST_FAIL_RE.search(clean) else 0
        )
    if effective == "vitest":
        return (
            1
            if _VITEST_PASS_RE_V7.search(clean) and not _VITEST_FAIL_RE.search(clean)
            else 0
        )
    return evaluate_js_output(effective, clean, rc)


#: Dotted-module shape for synthetic collection-error cases (measured
#: pytest 8: ``classname="" name="pkg.test_broken"`` with an
#: ``<error message="collection failure">`` child and no ``file``).
_V7_COLLECTION_MODULE_RE = re.compile(r"[A-Za-z_][\w.]*")


def _v7_error_module(classname: str | None, name: str | None) -> str | None:
    """Dotted module of a collection-error case, or None when not collection-shaped.

    Real test identities (``::`` in the name) never qualify: their exact
    keys govern fail-to-pass. Either attribute may carry the module; the
    ``.py`` suffix is normalized away.
    """
    node = name or ""
    if "::" in node:
        return None
    for candidate in (classname or "", node):
        text = candidate.strip()
        if text.endswith(".py"):
            text = text[: -len(".py")]
        if text and _V7_COLLECTION_MODULE_RE.fullmatch(text):
            return text
    return None


def _v7_dotted_module_variants(classname: str | None, file: str | None) -> list[str]:
    """Dotted module strings identifying one agent case's module."""
    variants = []
    dotted_class = (classname or "").strip()
    if dotted_class:
        variants.append(dotted_class)
    slash_file = (file or "").strip()
    if slash_file:
        dotted_file = slash_file.replace("/", ".").replace("\\", ".")
        if dotted_file.endswith(".py"):
            dotted_file = dotted_file[: -len(".py")]
        variants.append(dotted_file)
    return variants


def _v7_module_now_passing(
    error_modules: Collection[str],
    error_files: Collection[str],
    agent_cases: Collection[ET.Element],
) -> bool:
    """Whether an errored pristine module now collects and passes (G5, @7).

    Satisfied by a passing agent case from the same file (suffix-aligned,
    covering absolute/relative junit forms) or the same dotted module (a
    class suffix still prefix-matches). Same-path equality means the hidden
    test file itself passes: agent edits there are reverted before grading,
    and blind agents cannot guess hidden module paths, so decoys are out of
    the ladder threat model (documented residual).
    """
    for case in agent_cases:
        if (
            case.find("failure") is not None
            or case.find("error") is not None
            or case.find("skipped") is not None
        ):
            continue
        agent_file = (case.get("file") or "").strip()
        for expected_file in error_files:
            if agent_file and (
                agent_file == expected_file
                or agent_file.endswith("/" + expected_file)
                or expected_file.endswith("/" + agent_file)
            ):
                return True
        for agent_mod in _v7_dotted_module_variants(case.get("classname"), agent_file):
            for expected in error_modules:
                if agent_mod == expected or agent_mod.startswith(expected + "."):
                    return True
    return False


def evaluate_junit_v7(
    report_xmls: list[bytes | None],
    rc: int,
    named_ids: Collection[str],
    baseline_xmls: list[bytes | None] | None = None,
    baseline_output: str = "",
) -> int:
    """Grade 1/0 with @7 fail-to-pass: @6 plus collection-error module mapping (G5) and INTERNALERROR fallback (G8).

    G5: a pristine collection error (the reference fix resolves the import,
    measured 000083) leaves synthetic junit keys that never appear in the
    agent run; the module counts as fail-to-pass when it now collects and
    passes. Only ``<error>`` orphans qualify — a vanished ``<failure>``
    still fails, and an error whose exact key exists in the agent run stays
    governed by the exact rule.
    G8: an INTERNALERROR pristine baseline proves nothing (measured 000585:
    the suite's own rerun filter crashes collection on pristine); grading
    falls back to the @5 structured rules (rc, cases, bad, named IDs) —
    never to exit-code-only.
    """
    if evaluate_junit_v5(report_xmls, rc, named_ids) == 0:
        return 0
    if baseline_output and "INTERNALERROR" in baseline_output:
        return 1
    baseline = junit_status_map(baseline_xmls) if baseline_xmls else {}
    if not baseline:
        return 1
    agent = junit_status_map(report_xmls)
    error_modules: dict[tuple[str, str, str], set[str]] = {}
    error_files: dict[tuple[str, str, str], set[str]] = {}
    failure_keys: set[tuple[str, str, str]] = set()
    for raw in baseline_xmls or []:
        if not raw:
            continue
        try:
            found = ET.fromstring(raw).iter("testcase")
        except Exception:
            continue
        for case in found:
            key = (
                case.get("classname") or "",
                case.get("name") or "",
                case.get("file") or "",
            )
            if case.find("failure") is not None:
                failure_keys.add(key)
            if case.find("error") is None or case.find("failure") is not None:
                continue
            module = _v7_error_module(case.get("classname"), case.get("name"))
            if module is not None:
                error_modules.setdefault(key, set()).add(module)
            file_attr = (case.get("file") or "").strip()
            if file_attr:
                error_files.setdefault(key, set()).add(file_attr)
    agent_cases: list[ET.Element] = []
    for raw in report_xmls:
        if not raw:
            continue
        try:
            agent_cases.extend(ET.fromstring(raw).iter("testcase"))
        except Exception:
            continue
    baseline_skipped = {key for key, status in baseline.items() if status == "skipped"}
    for key, status in baseline.items():
        if status != "skipped" and agent.get(key) != "passed":
            if (
                (key in error_modules or key in error_files)
                and key not in failure_keys
                and _v7_module_now_passing(
                    error_modules.get(key, ()),
                    error_files.get(key, ()),
                    agent_cases,
                )
            ):
                continue
            return 0
    for key, status in agent.items():
        if status == "skipped" and key not in baseline_skipped:
            return 0
    return 1

#: @7 baseline block: @6 plus every-phase execution for multi-phase
#: commands (G7). A ``set -e`` command truncates the pristine baseline at
#: the first failing phase (measured 000511: phase 1 fails, phases 2-3
#: never run, and the fail-to-pass comparison goes blind), so for commands
#: with two or more pytest/unittest invocations the baseline runs under a
#: BASH_ENV DEBUG trap holding errexit off in every nested bash. Explicit
#: control flow (``&&``, ``||``, ``exit``) is unaffected; single-phase
#: commands run exactly as under @6.
_V7_BASELINE_PIN = '    MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1'
_V7_BASELINE_RUN = (
    '    _V7_PHASES=$(grep -oE \'(^|[^[:alnum:]_])(python[0-9.]* -m (pytest|unittest)|pytest)([^[:alnum:]_]|$)\' "$V/baseline_resolved.txt" | wc -l)\n'
    '    if [ "$_V7_PHASES" -ge 2 ] && command -v bash >/dev/null 2>&1; then\n'
    '      cat > "$V/baseline-noerrexit.sh" <<\'BASELINE_NOERREXIT_EOF\'\n'
    "# @7 baseline: hold errexit off so every test phase runs on pristine.\n"
    "set -T 2>/dev/null || true\n"
    "shopt -s extdebug 2>/dev/null || true\n"
    "trap 'set +e 2>/dev/null || true' DEBUG 2>/dev/null || true\n"
    "BASELINE_NOERREXIT_EOF\n"
    '      BASH_ENV="$V/baseline-noerrexit.sh" MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 bash -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1\n'
    "    else\n"
    '      MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1\n'
    "    fi"
)

assert _V6_BASELINE_BLOCK.count(_V7_BASELINE_PIN) == 1, (
    "@6 baseline invocation moved; re-pin the @7 replacement"
)

#: @7 grader entry point, part A: @6 with every-phase pristine baselines.
_V7_BASELINE_BLOCK = _V6_BASELINE_BLOCK.replace(_V7_BASELINE_PIN, _V7_BASELINE_RUN)

assert _V7_BASELINE_BLOCK != _V6_BASELINE_BLOCK, "@7 baseline replacement did not apply"
assert _V6_WRAPPER_A.count(_V6_BASELINE_BLOCK) == 1, (
    "@6 wrapper A moved; re-pin the @7 baseline block"
)

_V7_WRAPPER_A = _V6_WRAPPER_A.replace(_V6_BASELINE_BLOCK, _V7_BASELINE_BLOCK)

assert _V7_WRAPPER_A != _V6_WRAPPER_A, "@7 wrapper A replacement did not apply"

#: @6 grader entry point, part B: identical to @6.
_V7_WRAPPER_B = _V6_WRAPPER_B

#: Exact @6 output-read line extended with ANSI stripping (G4, all runners).
_V7_OUTPUT_PIN = (
    'output = open(output_path, encoding="utf-8", errors="replace").read()'
)
_V7_OUTPUT_STRIP = (
    'output = re.sub(r"\\x1b\\[[0-9;?]*[A-Za-z]|\\x1b[()][0-9A-B]|\\r", "", output)'
)

#: Exact @6 runner log line preceded by the actual-reporter switch (G6).
_V7_RUNNER_PIN = 'sys.stderr.write("runner=%s rc=%d\\n" % (runner, rc))'
_V7_RUNNER_SWITCH = (
    'if runner == "tap" and (re.search(r"(^|\\s)-R\\s+\\S*spec\\b|reporters?\\s*[=:]\\s*\\S*spec\\b", hay) is not None or (re.search(r"\\d+\\s+passing", output) is not None and re.search(r"^(not )?ok\\b", output, re.M) is None)):\n'
    '    runner = "mocha"'
)

#: Exact @6 go-test grade line extended with per-package emptiness (G1) and direct-binary PASS (G3).
_V7_GO_OK = """        _go_ok = [ln for ln in output.splitlines() if re.match(r"^ok\\s+\\S+", ln) is not None and "no tests to run" not in ln]
        _go_bare = re.search(r"^PASS\\s*$", output, re.M) is not None
        ok = (_go_ok or (_go_bare and "no tests to run" not in output)) and re.search(r"^(FAIL|--- FAIL|panic:)", output, re.M) is None"""

#: Exact @6 jest/vitest pass patterns extended with skipped-first summaries (G2).
_V7_JEST_PIN = 're.search(r"Tests:\\s+\\d+\\s+passed", output)'
_V7_JEST_OK = 're.search(r"Tests:\\s+[^\\n]*\\d+\\s+passed", output)'
_V7_VITEST_PIN = 're.search(r"(Test Files|Tests)\\s+\\d+\\s+passed", output)'
_V7_VITEST_OK = 're.search(r"Tests\\s+[^\\n]*\\d+\\s+passed", output)'

#: @7 baseline-output read plus INTERNALERROR fallback flag (G8), spliced
#: into the @6 fail-to-pass block ahead of the comparison.
_V7_FTP_READ_ANCHOR = "    ftp_bad = False"
_V7_FTP_READ = """    _v7bout = ""
    try:
        with open(os.path.join(base_union, "output.log"), encoding="utf-8", errors="replace") as _v7bf:
            _v7bout = _v7bf.read()
    except Exception:
        pass
    # @7 G8: an INTERNALERROR pristine baseline proves nothing (measured
    # 000585): skip fail-to-pass and keep the structured @5 rules below.
    _v7unreliable = "INTERNALERROR" in _v7bout
    ftp_bad = False"""

#: @6 fail-to-pass orphan loop extended with the collection-error module rule (G5).
_V7_FTP_ORPHAN_ANCHOR = """        for _k, _s in _bm.items():
            if _s != "skipped" and _am.get(_k) != "passed":
                ftp_bad = True"""
_V7_FTP_ORPHAN = """        _v7errmod = {}
        _v7errfile = {}
        _v7hasfail = set()
        for _c in base_cases:
            _ck = _v6key(_c)
            if _c.find("failure") is not None:
                _v7hasfail.add(_ck)
            if _c.find("error") is None or _c.find("failure") is not None:
                continue
            _nm = _c.get("name") or ""
            if "::" in _nm:
                continue
            for _cand in ((_c.get("classname") or ""), _nm):
                _ct = _cand.strip()
                if _ct.endswith(".py"):
                    _ct = _ct[:-3]
                if re.fullmatch(r"[A-Za-z_][\\w.]*", _ct or ""):
                    _v7errmod.setdefault(_ck, set()).add(_ct)
            _cf = (_c.get("file") or "").strip()
            if _cf:
                _v7errfile.setdefault(_ck, set()).add(_cf)
        def _v7modok(_k):
            for _mc in cases:
                if _mc.find("failure") is not None or _mc.find("error") is not None or _mc.find("skipped") is not None:
                    continue
                _af = (_mc.get("file") or "").strip()
                if _k in _v7errfile and _v7errfile[_k]:
                    for _ef in _v7errfile[_k]:
                        if _af and (_af == _ef or _af.endswith("/" + _ef) or _ef.endswith("/" + _af)):
                            return True
                _acn = (_mc.get("classname") or "").strip()
                _amods = [_acn]
                if _af:
                    _afm = _af.replace("/", ".").replace(chr(92), ".")
                    if _afm.endswith(".py"):
                        _afm = _afm[:-3]
                    _amods.append(_afm)
                for _m in _v7errmod.get(_k, ()):
                    for _am2 in _amods:
                        if _am2 == _m or _am2.startswith(_m + "."):
                            return True
            return False
        for _k, _s in _bm.items():
            if _s != "skipped" and _am.get(_k) != "passed":
                if (_k in _v7errmod or _k in _v7errfile) and _k not in _v7hasfail and _v7modok(_k):
                    continue
                ftp_bad = True"""

#: @6 fail-to-pass tail extended with the unreliable-baseline note (G8).
_V7_FTP_TAIL_ANCHOR = """    if ftp_bad:
        sys.stderr.write("fail-to-pass: pristine outcomes not preserved\\n")"""
_V7_FTP_TAIL = """    if _v7unreliable:
        sys.stderr.write("baseline unreliable (INTERNALERROR): fail-to-pass skipped\\n")
    if ftp_bad:
        sys.stderr.write("fail-to-pass: pristine outcomes not preserved\\n")"""

assert _V6_PYTEST_FTP_BLOCK.count(_V7_FTP_READ_ANCHOR) == 1, (
    "@6 pytest block moved; re-pin the @7 read splice"
)
assert _V6_PYTEST_FTP_BLOCK.count(_V7_FTP_ORPHAN_ANCHOR) == 1, (
    "@6 orphan loop moved; re-pin the @7 module rule"
)
assert _V6_PYTEST_FTP_BLOCK.count(_V7_FTP_TAIL_ANCHOR) == 1, (
    "@6 pytest tail moved; re-pin the @7 fallback note"
)
assert _V6_PYTEST_FTP_BLOCK.count("    if base_cases:") == 1, (
    "@6 baseline gate moved; re-pin the @7 gate"
)

#: @7 pytest grade block: @6 plus collection-error modules (G5) and the INTERNALERROR fallback (G8).
_V7_PYTEST_FTP_BLOCK = (
    _V6_PYTEST_FTP_BLOCK.replace(_V7_FTP_READ_ANCHOR, _V7_FTP_READ)
    .replace(_V7_FTP_ORPHAN_ANCHOR, _V7_FTP_ORPHAN)
    .replace(_V7_FTP_TAIL_ANCHOR, _V7_FTP_TAIL)
    .replace("    if base_cases:", "    if base_cases and not _v7unreliable:")
)

assert _V7_PYTEST_FTP_BLOCK != _V6_PYTEST_FTP_BLOCK, "@7 pytest replacement did not apply"

assert _V6_WRAPPER_C.count(_V7_OUTPUT_PIN) == 1, (
    "@6 output read moved; re-pin the @7 strip"
)
assert _V6_WRAPPER_C.count(_V7_RUNNER_PIN) == 1, (
    "@6 runner log moved; re-pin the @7 reporter switch"
)
assert _V6_WRAPPER_C.count(_V6_GO_OK) == 1, (
    "@6 go grade line moved; re-pin the @7 replacement"
)
assert _V6_WRAPPER_C.count(_V7_JEST_PIN) == 1, (
    "@6 jest grade moved; re-pin the @7 replacement"
)
assert _V6_WRAPPER_C.count(_V7_VITEST_PIN) == 1, (
    "@6 vitest grade moved; re-pin the @7 replacement"
)
assert _V6_WRAPPER_C.count(_V6_PYTEST_FTP_BLOCK) == 1, (
    "@6 pytest block moved; re-pin the @7 replacement"
)

#: @7 grader entry point, part C: @6 with ANSI stripping (G4),
#: actual-reporter detection (G6), per-package go emptiness (G1),
#: direct-binary PASS (G3), skipped-first summaries (G2), collection-error
#: modules (G5), and the INTERNALERROR fallback (G8).
_V7_WRAPPER_C = (
    _V6_WRAPPER_C.replace(_V7_OUTPUT_PIN, _V7_OUTPUT_PIN + "\n" + _V7_OUTPUT_STRIP)
    .replace(_V7_RUNNER_PIN, _V7_RUNNER_SWITCH + "\n" + _V7_RUNNER_PIN)
    .replace(_V6_GO_OK, _V7_GO_OK)
    .replace(_V7_JEST_PIN, _V7_JEST_OK)
    .replace(_V7_VITEST_PIN, _V7_VITEST_OK)
    .replace(_V6_PYTEST_FTP_BLOCK, _V7_PYTEST_FTP_BLOCK)
)

assert _V7_WRAPPER_C != _V6_WRAPPER_C, "@7 replacement did not apply"


def render_wrapper_test_sh_v7(workdir: str) -> str:
    """Verifier entry point: @7 fail-to-pass grading with G-shape certification."""
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
    return (
        (_V7_WRAPPER_A + _V7_WRAPPER_B + _V7_WRAPPER_C)
        .replace("@@WORKDIR@@", workdir)
        .replace("@@V3_NEW_INFRA_CASE@@", "|".join(sorted(V3_NEW_INFRA_BASENAMES)))
        .replace("@@V3_UNIVERSAL@@", universal)
        .replace("@@V3_SCOPED@@", scoped)
        .replace("@@V3_CONFTEST_HOOK_B64@@", hook_b64)
        .replace("@@V5_ARCHIVE_HOOK_B64@@", archive_b64)
        .replace("@@JUNIT_MISSING_REASON@@", JUNIT_MISSING_REASON)
    )


def render_tests_dockerfile_v7(docker_image: str) -> str:
    """Verifier image: pristine repo plus bundled hidden tests and setup (@7)."""
    return (
        "# Separate-verifier image (separate-verifier@7): pristine repo checkout\n"
        "# plus the hidden tests and the clean setup bundle. The agent image\n"
        "# never sees /tests.\n"
        f"FROM --platform=linux/amd64 {docker_image}\n"
        "COPY . /tests\n"
        "RUN chmod +x /tests/test.sh\n"
    )


def build_changes_v7(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs for @7.

    Same bundle shape as @6; the grader is the @7 entry point with
    G-shape certification (G1–G8). ``solution_sh`` adds an
    oracle-control reference solution when the parent has none. Refuses to
    overwrite an existing solution.
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
        "tests/test.sh": render_wrapper_test_sh_v7(info.workdir).encode("utf-8"),
        "tests/Dockerfile": render_tests_dockerfile_v7(info.docker_image).encode("utf-8"),
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
        "runner": runner,
        "solution": (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        ),
    }
    return changes, inputs


def derive_separate_verifier_v7(
    parent_dir: Path | str,
    *,
    marker: str,
    solution_sh: bytes | None = None,
    rationale: str = "Grade the agent's repo-file patch only, in a pristine "
    "verifier checkout with the hidden tests and G-shape certification.",
    created_by: str = "g-shape-certification-verifier",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``separate-verifier@7`` variant of a MiMo task package."""
    changes, inputs = build_changes_v7(parent_dir, marker=marker, solution_sh=solution_sh)
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID_V7,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=repo_root,
        variants_root=variants_root,
    )

__all__ = [
    "JUNIT_MISSING_REASON",
    "MIMO_STATE_DIR",
    "ORIG_TEST_SCRIPT",
    "SNAP_DIR",
    "TAMPER_SIGNATURES",
    "TRANSFORM_ID",
    "TRANSFORM_ID_V2",
    "TRANSFORM_ID_V3",
    "TRANSFORM_ID_V4",
    "TRANSFORM_ID_V5",
    "TRANSFORM_ID_V6",
    "TRANSFORM_ID_V7",
    "TRAJECTORY_ARTIFACT",
    "V2_GRADE_DIR",
    "V2_SETUP_SUBDIR",
    "V3_BUILD_OUTPUT_DIR_RES",
    "V3_CONFIG_HUNK_RULES",
    "V3_CONFTEST_HOOK",
    "V3_NEW_INFRA_BASENAMES",
    "V3_RUNNER_PATTERNS",
    "V3_SCOPED_SIGNATURES",
    "V3_STRUCTURED_RUNNERS",
    "V3_TAMPER_SIGNATURES",
    "V5_ARCHIVE_HOOK",
    "WRAPPER_TEST_SCRIPT",
    "ParentInfo",
    "addopts_cleared",
    "build_changes",
    "build_changes_v2",
    "build_changes_v3",
    "build_changes_v4",
    "build_changes_v5",
    "build_changes_v6",
    "build_changes_v7",
    "collect_verifier_setup_files",
    "declares_testmain",
    "derive_separate_verifier",
    "derive_separate_verifier_v2",
    "derive_separate_verifier_v3",
    "derive_separate_verifier_v4",
    "derive_separate_verifier_v5",
    "derive_separate_verifier_v6",
    "derive_separate_verifier_v7",
    "detect_pytest_run",
    "effective_js_runner",
    "detect_runner",
    "drop_reason",
    "evaluate_cargo_output",
    "evaluate_go_output",
    "evaluate_go_output_v6",
    "evaluate_go_output_v7",
    "evaluate_js_output",
    "evaluate_js_output_v7",
    "evaluate_junit",
    "evaluate_junit_v4",
    "evaluate_junit_v5",
    "evaluate_junit_v6",
    "evaluate_junit_v7",
    "evaluate_phpunit_output",
    "evaluate_rspec_output",
    "evaluate_surefire_reports",
    "evaluate_unittest",
    "evaluate_unittest_v6",
    "is_pytest_config_tamper",
    "strip_ansi",
    "is_test_infra_filename",
    "is_v3_new_infra",
    "parse_named_pytest_ids",
    "junit_absence_suspicious",
    "junit_case_matches_expected",
    "junit_case_status",
    "junit_status_map",
    "output_is_blank",
    "PYTEST_START_MARKERS",
    "parse_pytest_node_id",
    "read_parent_info",
    "render_probe_hook",
    "render_snapshot_hook",
    "render_snapshot_hook_v2",
    "render_task_toml",
    "render_tests_dockerfile",
    "render_tests_dockerfile_v2",
    "render_tests_dockerfile_v3",
    "render_tests_dockerfile_v4",
    "render_tests_dockerfile_v5",
    "render_tests_dockerfile_v6",
    "render_tests_dockerfile_v7",
    "render_wrapper_test_sh",
    "render_wrapper_test_sh_v2",
    "render_wrapper_test_sh_v3",
    "render_wrapper_test_sh_v4",
    "render_wrapper_test_sh_v5",
    "render_wrapper_test_sh_v6",
    "render_wrapper_test_sh_v7",
    "resolve_command_text",
    "tamper_signature_hit",
    "v3_config_revert_reason",
    "v3_tamper_hit_for_file",
]
