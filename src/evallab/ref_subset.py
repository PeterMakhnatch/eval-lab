"""Reference-subset probe: does part of the reference fix already pass the hidden tests?

QA Agent - Task, S2 executed probe (a sibling of ``evallab hack mutate``). For a
task with a known reference fix (the MiMo ``mimo-reference-fixes/index.csv``),
the reference diff is split into *change blocks*: contiguous runs of edited
lines per file, computed on the base and fixed file contents. Each grade puts a
subset of the blocks on the task's base checkout and runs the task's own hidden
tests (``tests/test.sh``). A greedy minimisation drops what the tests do not
need:

1. baselines: the full reference must earn reward 1 and the base (no block) 0;
2. drop every documentation- or comment-only block at once;
3. drop each file's code blocks, largest file first;
4. drop each remaining code block, largest first;
5. grade the final subset again (a flake guard).

A dropped code block is reference behaviour no hidden test needs. When the kept
subset is a small part of the reference, a partial fix earns full reward: the
task is weakly tested. Every finding carries the kept subset as a
``git apply``-able reproducer with expected reward 1. A dropped block is a
hypothesis, not a proven defect: it can be a refactor, a log line or a
defensive branch the instruction never asks for, so read ``dropped.patch``.
Blocks are the smallest unit: a single block that the tests only half need is
``hack mutate``'s job.

Grading defaults to local Docker; Modal is explicit and spend-gated. Raw MiMo
and separate-verifier grades start from prepared snapshots. Ordinary shared
Harbor tasks instead use native, host-isolated one-worker matrices with their
unchanged manifest and explicit ``--target`` files (or declared artifacts).
Every candidate applies only the complete selected-file mapping to fresh base,
without replaying the reference. An independently passing full-target
reconstruction is required. ``--plan`` constructs no backend.

QA Agent - Trace, T0: ``trace`` compares a passing trial's
``verifier/agent.diff`` with the task report. A pass whose diff leaves every
untested reference block alone, while touching the tested ones, is a suspected
partial fix (``false_positive``, unverified until someone runs stronger tests).
"""

from __future__ import annotations

import argparse
import ast
import csv
import difflib
import hashlib
import io
import json
import math
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab.execution_contracts import MODAL_PATH_PIN
from evallab.regrade import (
    MODAL_USD_PER_ALLOCATION_UNIT,
    CloudSpendGate,
    SpendCapReached,
    lab_harbor_problem,
    modal_resource_blocker,
)
from evallab.verifier_mutation import Mutant, generate_mutants, select_mutants

SCHEMA_REPORT = "evallab.ref_subset.report/v1"
SCHEMA_FINDING = "qa.finding/v1"
STAGE_TASK = "S2.ref_subset"
STAGE_TRACE = "T0.ref_coverage"
STAGE_MUTANTS = "S2.ref_mutants"
REVIEWER = {"agent": "evallab hack ref-subset", "model": None, "tokens": 0, "cost_usd": 0.0}

#: Kept share of the reference's code lines at or below which a passing subset is
#: "trivial" (high severity) or "half" (medium); anything dropped above is low.
HIGH_KEEP = 0.25
MEDIUM_KEEP = 0.5
#: Grades per task including both baselines and the confirmation.
DEFAULT_MAX_GRADES = 24
#: Lines of slack when matching a trial's edits to a reference block.
TRACE_SLACK = 3

BlockKind = Literal["code", "comment", "noncode", "test"]
Status = Literal[
    "measured", "baseline_mismatch", "flaky", "not_applicable", "unavailable", "budget_exhausted"
]

_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".rdoc"})
_DOC_NAMES = ("changelog", "changes", "history", "news", "authors", "contributors")
#: Directories whose files are documentation, examples or CI plumbing, not graded code.
_NONCODE_DIRS = frozenset(
    {"doc", "docs", "example", "examples", "sample", "samples", "benchmark", "benchmarks",
     ".github", ".circleci"}
)  # fmt: skip
#: Top-level release-note fragment directories (towncrier and friends); only at the root,
#: because a package may well be called ``news`` or ``changes``.
_RELEASE_NOTE_DIRS = frozenset({"news", "changes", "changelog.d", "newsfragments"})
#: Packaging manifests, lock files and repository dotfiles: never what a hidden test exercises.
_NONCODE_NAMES = frozenset(
    {"manifest.in", "go.sum", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock",
     "poetry.lock", "pipfile.lock", "uv.lock", "composer.lock", "gemfile.lock", ".gitignore",
     ".gitattributes", ".editorconfig", ".mailmap", ".pre-commit-config.yaml", ".travis.yml",
     "appveyor.yml", ".readthedocs.yml", ".readthedocs.yaml"}
)  # fmt: skip
_HASH_COMMENT = frozenset({".py", ".sh", ".bash", ".rb", ".pl", ".r", ".yaml", ".yml", ".toml"})
_SLASH_COMMENT = frozenset(
    {
        ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c",
        ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".php", ".dart",
    }
)  # fmt: skip
_RESET_LIST = re.compile(r"<<'EOF_RESET_TEST_FILES'\n(.*?)\nEOF_RESET_TEST_FILES", re.S)


# ---------------------------------------------------------------------------
# Change blocks


@dataclass(frozen=True)
class Block:
    id: str
    path: str
    kind: BlockKind
    base_start: int  # 0-based, half-open line range in the base file
    base_end: int
    removed: int
    added: int

    @property
    def size(self) -> int:
        return self.removed + self.added


@dataclass
class FileChange:
    path: str
    base: bytes | None
    fixed: bytes | None
    blocks: list[Block] = field(default_factory=list)
    # (tag, i1, i2, j1, j2, block_id|None) over base/fixed lines; None for opaque files
    ops: list[tuple[str, int, int, int, int, str | None]] | None = None


def _noncode_path(path: str) -> bool:
    """Docs, release notes, packaging and repo plumbing, and interpreter byte-code caches."""
    pure = PurePosixPath(path)
    name = pure.name.lower()
    suffix = pure.suffix.lower()
    return (
        suffix in _DOC_SUFFIXES
        or suffix in {".pyc", ".pyo"}
        or (suffix == "" and name.startswith(_DOC_NAMES))
        or name in _NONCODE_NAMES
        or any(part.lower() in _NONCODE_DIRS for part in pure.parts[:-1])
        or "__pycache__" in pure.parts[:-1]
        or (len(pure.parts) > 1 and pure.parts[0].lower() in _RELEASE_NOTE_DIRS)
    )


def block_kind(
    path: str, removed: Sequence[str], added: Sequence[str], tests: set[str]
) -> BlockKind:
    if path in tests:
        return "test"
    if _noncode_path(path):
        return "noncode"
    suffix = PurePosixPath(path).suffix.lower()
    prefixes: tuple[str, ...] = ()
    if suffix in _HASH_COMMENT:
        prefixes = ("#",)
    elif suffix in _SLASH_COMMENT:
        prefixes = ("//", "/*", "*", "*/") + (("#",) if suffix == ".php" else ())
    lines = [line.strip() for line in (*removed, *added)]
    if all(not line or (prefixes and line.startswith(prefixes)) for line in lines):
        return "comment"
    return "code"


def _lines(data: bytes) -> list[str] | None:
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def file_change(path: str, base: bytes | None, fixed: bytes | None, tests: set[str]) -> FileChange:
    change = FileChange(path=path, base=base, fixed=fixed)
    base_lines = _lines(base) if base is not None else []
    fixed_lines = _lines(fixed) if fixed is not None else []
    if (base is None) != (fixed is None) and base_lines == [] and fixed_lines == []:
        # Creating/deleting an empty file is still a real filesystem change.
        kind = "test" if path in tests else "noncode" if _noncode_path(path) else "code"
        change.blocks.append(Block(f"{path}#0", path, kind, 0, 0, 1, 1))
        return change
    if base_lines is None or fixed_lines is None:
        kind: BlockKind = "test" if path in tests else "noncode" if _noncode_path(path) else "code"
        size = 1
        change.blocks.append(Block(f"{path}#0", path, kind, 0, 0, size, size))
        return change
    matcher = difflib.SequenceMatcher(None, base_lines, fixed_lines, autojunk=False)
    ops: list[tuple[str, int, int, int, int, str | None]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append((tag, i1, i2, j1, j2, None))
            continue
        block_id = f"{path}#{len(change.blocks)}"
        removed, added = base_lines[i1:i2], fixed_lines[j1:j2]
        kind = block_kind(path, removed, added, tests)
        change.blocks.append(Block(block_id, path, kind, i1, i2, len(removed), len(added)))
        ops.append((tag, i1, i2, j1, j2, block_id))
    change.ops = ops
    if path.endswith(".py"):
        _mark_python_neutral(change)
    return change


def _python_shape(source: bytes | None) -> str | None:
    """The module's AST without docstrings (``None``: absent or unparsable)."""
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def _mark_python_neutral(change: FileChange) -> None:
    """Reclassify code blocks whose removal leaves the AST unchanged (docstrings, formatting)."""
    everything = {block.id for block in change.blocks}
    reference = _python_shape(compose(change, everything))
    if reference is None:
        return
    for i, block in enumerate(change.blocks):
        if (
            block.kind == "code"
            and _python_shape(compose(change, everything - {block.id})) == reference
        ):
            change.blocks[i] = replace(block, kind="comment")


def compose(change: FileChange, keep: set[str]) -> bytes | None:
    """The file with only the kept blocks applied (``None``: the file is absent)."""
    if change.ops is None:
        return change.fixed if change.blocks[0].id in keep else change.base
    applied = [block.id for block in change.blocks if block.id in keep]
    if not applied:
        return change.base
    if len(applied) == len(change.blocks):
        return change.fixed
    base_lines = _lines(change.base) if change.base is not None else []
    fixed_lines = _lines(change.fixed) if change.fixed is not None else []
    assert base_lines is not None and fixed_lines is not None
    out: list[str] = []
    for tag, i1, i2, j1, j2, block_id in change.ops:
        if tag == "equal" or block_id not in keep:
            out.extend(base_lines[i1:i2])
        else:
            out.extend(fixed_lines[j1:j2])
    return "".join(out).encode("utf-8")


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if match:
                for path in match.groups():
                    if path not in paths:
                        paths.append(path)
    return paths


def apply_reference(patch: str, base: dict[str, bytes | None]) -> dict[str, bytes | None]:
    """Apply the reference patch to the base files on the host (``git apply``)."""
    with tempfile.TemporaryDirectory(prefix="ref-subset-") as tmp:
        root = Path(tmp)
        for path, data in base.items():
            if data is not None:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        (root / ".ref.patch").write_text(patch)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", ".ref.patch"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"reference patch does not apply to the base: {result.stderr.strip()}")
        return {
            path: (root / path).read_bytes() if (root / path).is_file() else None for path in base
        }


def unified_patch(
    before: dict[str, bytes | None],
    after: dict[str, bytes | None],
    *,
    before_modes: dict[str, int | None] | None = None,
    after_modes: dict[str, int | None] | None = None,
) -> str:
    """A base-applicable Git patch; optional modes preserve Git's executable bit."""
    with tempfile.TemporaryDirectory(prefix="ref-subset-diff-") as tmp:
        root = Path(tmp)
        git = ["git", "-c", "user.name=ref-subset", "-c", "user.email=ref-subset@localhost"]
        subprocess.run([*git, "init", "-q"], cwd=root, check=True)
        for state, modes in ((before, before_modes), (after, after_modes)):
            for path, data in state.items():
                target = root / path
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    mode = modes.get(path) if modes is not None else None
                    if mode is not None:
                        target.chmod(0o755 if mode & 0o100 else 0o644)
            subprocess.run([*git, "add", "-A"], cwd=root, check=True)
            if state is before:
                subprocess.run(
                    [*git, "commit", "-q", "--allow-empty", "-m", "base"], cwd=root, check=True
                )
        return subprocess.run(
            [*git, "diff", "--cached", "--binary"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout


# ---------------------------------------------------------------------------
# Task packages and Docker grading


class UnsupportedTask(ValueError):
    """A well-formed task this probe cannot reproduce (sidecar or remapped artifacts)."""


@dataclass(frozen=True)
class Package:
    """A raw MiMo code package, or a Harbor task whose verifier runs separately on artifacts.

    MiMo packages come with a reference patch; separate-verifier packages derive the reference
    from their own oracle (``solution/solve.sh``) and are graded on ``artifacts`` alone.
    """

    path: Path
    task_id: str
    image: str
    workdir: str
    tests: frozenset[str]
    artifacts: tuple[str, ...] = ()
    gpus: int = 0
    verifier_timeout: float | None = None
    oracle_network: bool = True
    verifier_network: bool = True
    verifier_user: str | None = None
    verifier_env: tuple[tuple[str, str], ...] = ()

    @property
    def separate(self) -> bool:
        return bool(self.artifacts)

    @classmethod
    def load(cls, path: Path) -> Package:
        config = tomllib.loads((path / "task.toml").read_text())
        environment = config.get("environment") or {}
        verifier = config.get("verifier") or {}
        test_sh = path / "tests" / "test.sh"
        image = environment.get("docker_image")
        setup = path / "environment" / "setup" / "setup.sh"
        if not image or not test_sh.is_file() or not setup.is_file():
            if verifier.get("environment_mode") == "separate" or (
                verifier.get("environment_mode") is None and verifier.get("environment") is not None
            ):
                return cls._load_separate(path, config, environment, verifier)
            raise ValueError(
                f"{path}: neither a raw MiMo code package ([environment].docker_image, "
                "environment/setup/setup.sh, tests/test.sh) nor a separate-verifier task"
            )
        reset = _RESET_LIST.search(test_sh.read_text())
        tests = frozenset(
            line.strip() for line in (reset.group(1) if reset else "").splitlines() if line.strip()
        )
        task_id = str((config.get("metadata") or {}).get("source_id") or path.name)
        return cls(path, task_id, str(image), str(environment.get("workdir") or "/testbed"), tests)

    @classmethod
    def _load_separate(
        cls,
        path: Path,
        config: dict[str, Any],
        environment: dict[str, Any],
        verifier: dict[str, Any],
    ) -> Package:
        artifacts: list[str] = []
        for entry in config.get("artifacts") or []:
            if isinstance(entry, dict):
                if entry.get("service") not in (None, "main") or entry.get("destination"):
                    raise UnsupportedTask(
                        f"{path}: artifact {entry} (sidecar or remapped) is unsupported"
                    )
                entry = entry.get("source")
            if not isinstance(entry, str) or not entry.startswith("/"):
                raise ValueError(f"{path}: artifact {entry!r} is not an absolute path")
            artifacts.append(entry.rstrip("/") or "/")
        needed = (
            "environment/Dockerfile",
            "tests/Dockerfile",
            "tests/test.sh",
            "solution/solve.sh",
        )
        missing = [name for name in needed if not (path / name).is_file()]
        if not artifacts or missing:
            raise ValueError(
                f"{path}: a separate-verifier task needs [[artifacts]] and {', '.join(needed)}"
                + (f" (missing {', '.join(missing)})" if missing else " (no artifacts)")
            )
        name = str((config.get("task") or {}).get("name") or path.name)
        timeout = verifier.get("timeout_sec")
        oracle_network = bool(environment.get("allow_internet", True))
        verifier_environment = verifier.get("environment") or {}
        user = verifier.get("user")
        env = verifier.get("env") or {}
        return cls(
            path, name, "", "/", frozenset(), tuple(artifacts), int(environment.get("gpus") or 0),
            float(timeout) if timeout else None, oracle_network,
            bool(verifier_environment.get("allow_internet", oracle_network)),
            None if user is None else str(user),
            # Host-templated values (``${VAR}``) are secrets Harbor resolves on the host: skipped.
            tuple((str(k), str(v)) for k, v in sorted(env.items()) if "${" not in str(v)),
        )  # fmt: skip


def modal_execution_bound(
    *, max_grades: int = DEFAULT_MAX_GRADES, mutants: int = 0, separate: bool = True
) -> int:
    """Conservative reservation count, including image preparation and reads.

    Native creation retains its two-attempt retry: both attempts reserve before
    entering the provider boundary, even when the second attempt is unused.
    Raw MiMo uses one preparation sandbox; separate tasks use an oracle and a
    pristine verifier template. Each preparation also reserves image work.
    Reads share those sandboxes, and each grade clones the native snapshot.
    """
    return (6 if separate else 3) + 2 * (max(4, max_grades) + max(0, mutants))


def _check_environment(
    environment: str, spend_gate: CloudSpendGate | None, *, lanes: int = 1
) -> None:
    if environment not in {"docker", "modal"}:
        raise ValueError("ref-subset environment must be docker or modal")
    maximum = 16 if environment == "modal" else 2
    if not 1 <= lanes <= maximum:
        raise ValueError(f"{environment} ref-subset lanes must be between 1 and {maximum}")
    if environment == "modal":
        if spend_gate is None:
            raise ValueError("Modal ref-subset requires recorded approval and a USD spend gate")
        problem = lab_harbor_problem()
        if problem is not None:
            raise ValueError(f"Refusing Modal ref-subset: {problem}")


@dataclass
class Grade:
    label: str
    kept: list[str]
    reward: float | None
    rc: int | None
    seconds: float
    status: Literal["scored", "unscored", "timeout", "error"]
    tail: str = ""
    native: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status == "scored" and self.reward == 1.0


def _docker(
    *args: str, input: bytes | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)


#: ``docker exec`` in the bare MiMo images resolves ``python3`` to the
#: image's 3.6 interpreter, whose syntax cannot run the setup heredocs;
#: pin PATH the way Modal execs already do (see
#: execution_contracts.MODAL_PATH_PIN; 001948, measured 2026-10-16).
#: The pinned /usr/bin interpreter has no pip in these images, so the
#: setup exec also sets PIP_NO_CACHE_DIR=1: setup's own cache-disabled
#: branch then purges the on-disk pip cache directories directly.
_PATH_PIN: tuple[str, ...] = ("-e", f"PATH={MODAL_PATH_PIN}", "-e", "PIP_NO_CACHE_DIR=1")


def _check(result: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        raise RuntimeError(
            f"{what} failed: {result.stderr.decode(errors='replace').strip()[-400:]}"
        )
    return result.stdout


def _tar(files: dict[str, tuple[bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Sandbox:
    """One snapshot per task (image + setup); one throwaway container per grade."""

    def __init__(self, package: Package, *, memory: str = "4g", cpus: str = "2") -> None:
        self.package = package
        self.memory, self.cpus = memory, cpus
        nonce = hashlib.sha256(f"{package.path}{time.time()}".encode()).hexdigest()[:10]
        self.name = f"ref-subset-{package.task_id[-6:]}-{nonce}"
        self.snapshot = f"evallab-ref-subset/{package.task_id.lower()}:{nonce}"
        self.modes: dict[str, int] = {}

    def _run(self, image: str, name: str) -> None:
        _check(
            _docker(
                "run", "-d", "--name", name, "--network", "none", "--memory", self.memory,
                "--cpus", self.cpus, "--entrypoint", "/bin/sh", image, "-c", "exec tail -f /dev/null",
            ),
            f"docker run {image}",
        )  # fmt: skip

    def prepare(self, timeout: float = 1_800) -> None:
        self._run(self.package.image, self.name)
        try:
            setup = self.package.path / "environment" / "setup"
            _check(_docker("exec", self.name, "mkdir", "-p", "/var/lib/mimo"), "mkdir")
            _check(_docker("cp", f"{setup}/.", f"{self.name}:/var/lib/mimo/"), "copy setup")
            result = _docker(
                "exec", *_PATH_PIN, self.name, "bash", "/var/lib/mimo/setup.sh", timeout=timeout
            )
            _check(result, "task setup")
            _check(
                _docker("cp", f"{self.package.path / 'tests'}/.", f"{self.name}:/tests/"),
                "copy tests",
            )
            _check(_docker("commit", self.name, self.snapshot), "docker commit")
        finally:
            _docker("rm", "-f", self.name)

    def read(self, paths: Iterable[str]) -> dict[str, bytes | None]:
        name = f"{self.name}-read"
        self._run(self.snapshot, name)
        try:
            wanted = list(paths)
            listing = "\n".join(wanted)
            script = 'cd "$1" && while IFS= read -r p; do if [ -f "$p" ]; then printf "%s\\0" "$p"; fi; done'
            present = _check(
                _docker(
                    "exec",
                    "-i",
                    name,
                    "sh",
                    "-c",
                    script,
                    "sh",
                    self.package.workdir,
                    input=listing.encode() + b"\n",
                ),
                "list base files",
            ).split(b"\0")
            found = [p.decode() for p in present if p]
            out: dict[str, bytes | None] = dict.fromkeys(wanted)
            if found:
                raw = _check(
                    _docker("exec", "-i", name, "tar", "-C", self.package.workdir, "--null", "-T", "-", "-cf", "-",
                            input=b"\0".join(p.encode() for p in found) + b"\0"),
                    "read base files",
                )  # fmt: skip
                with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                    for member in archive.getmembers():
                        handle = archive.extractfile(member)
                        if handle is not None:
                            out[member.name] = handle.read()
                            self.modes[member.name] = member.mode
            return out
        finally:
            _docker("rm", "-f", name)

    def grade(
        self,
        label: str,
        kept: list[str],
        files: dict[str, bytes | None],
        timeout: float,
        tail_chars: int = 2_000,
    ) -> Grade:
        name = f"{self.name}-g"
        started = time.monotonic()
        try:
            self._run(self.snapshot, name)
            workdir = self.package.workdir
            gone = [path for path, data in files.items() if data is None]
            present = {
                path: (data, self.modes.get(path, 0o644))
                for path, data in files.items()
                if data is not None
            }
            if gone:
                _check(
                    _docker("exec", name, "rm", "-f", "--", *[f"{workdir}/{p}" for p in gone]),
                    "remove files",
                )
            if present:
                _check(_docker("cp", "-", f"{name}:{workdir}", input=_tar(present)), "write files")
            stale = [p for p in files if p.endswith(".py")]
            if stale:
                pycache = " ".join(
                    f"'{workdir}/{PurePosixPath(p).parent}/__pycache__/{PurePosixPath(p).stem}'.*.pyc"
                    for p in stale
                )
                _docker("exec", name, "sh", "-c", f"rm -f {pycache}")
            return _verify(name, label, kept, timeout, started, tail_chars=tail_chars)
        except (RuntimeError, OSError) as exc:
            return Grade(
                label, kept, None, None, time.monotonic() - started, "error", str(exc)[-2_000:]
            )
        finally:
            _docker("rm", "-f", name)

    def close(self) -> None:
        _docker("rmi", self.snapshot)


def _verify(
    name: str,
    label: str,
    kept: list[str],
    timeout: float,
    started: float,
    exec_options: Sequence[str] = (),
    *,
    tail_chars: int = 2_000,
) -> Grade:
    """Run ``/tests/test.sh`` in container ``name`` and score ``/logs/verifier/reward.txt``."""
    try:
        result = _docker(
            "exec", *_PATH_PIN, *exec_options, name, "bash", "/tests/test.sh", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return Grade(label, kept, None, None, time.monotonic() - started, "timeout")
    raw = _docker("exec", name, "cat", "/logs/verifier/reward.txt").stdout.decode().strip()
    tail = result.stdout.decode(errors="replace")[-tail_chars:]
    seconds = time.monotonic() - started
    try:
        reward = float(raw)
    except ValueError:
        return Grade(label, kept, None, result.returncode, seconds, "unscored", tail)
    if not 0.0 <= reward <= 1.0:
        return Grade(label, kept, None, result.returncode, seconds, "unscored", tail)
    return Grade(label, kept, reward, result.returncode, seconds, "scored", tail)


def _dockerfile_bases(dockerfile: Path) -> list[str]:
    """Registry images a Dockerfile builds ``FROM`` (stage aliases, ``scratch`` and ARGs skipped)."""
    stages: set[str] = set()
    bases: list[str] = []
    for line in dockerfile.read_text().splitlines():
        match = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if image.lower() != "scratch" and image not in stages and "$" not in image:
            bases.append(image)
        if alias:
            stages.add(alias)
    return list(dict.fromkeys(bases))


class SeparateSandbox:
    """A Harbor separate-verifier task: the oracle runs once in the task environment, and every
    grade is a fresh verifier container that receives only the artifacts, the way Harbor uploads
    them (absent artifacts skipped, directory artifacts emptied first). Unlike the MiMo sandbox,
    containers keep Docker's default network: the oracle and the verifier may install packages.
    """

    def __init__(
        self,
        package: Package,
        *,
        memory: str = "4g",
        cpus: str = "2",
        build_timeout: float = 1_800,
        oracle_timeout: float = 3_600,
    ) -> None:
        self.package = package
        self.memory, self.cpus = memory, cpus
        self.build_timeout, self.oracle_timeout = build_timeout, oracle_timeout
        slug = re.sub(r"[^a-z0-9._-]+", "-", package.task_id.lower()).strip("-.")[-40:] or "task"
        nonce = hashlib.sha256(f"{package.path}{time.time()}".encode()).hexdigest()[:10]
        self.name = f"ref-subset-{slug}-{nonce}"
        self.image = f"evallab-ref-subset-env/{slug}:{nonce}"
        self.verifier_image = f"evallab-ref-subset-verifier/{slug}:{nonce}"
        self.modes: dict[str, int] = {}
        self.dirs: set[str] = set()
        self.pulled: list[str] = []
        # Artifact files the oracle left as they were: Harbor uploads them with every submission.
        self.unchanged: dict[str, bytes] = {}

    def _run(self, image: str, name: str, *, network: bool) -> None:
        _check(
            _docker(
                "run", "-d", "--name", name, "--memory", self.memory, "--cpus", self.cpus,
                *(() if network else ("--network", "none")),
                "--entrypoint", "/bin/sh", image, "-c", "exec tail -f /dev/null",
            ),
            f"docker run {image}",
        )  # fmt: skip

    def prepare(self) -> None:
        environment, tests = self.package.path / "environment", self.package.path / "tests"
        bases = _dockerfile_bases(environment / "Dockerfile") + _dockerfile_bases(
            tests / "Dockerfile"
        )
        self.pulled = [
            base
            for base in dict.fromkeys(bases)
            if _docker("image", "inspect", base).returncode != 0
        ]
        for tag, context, what in (
            (self.image, environment, "environment"),
            (self.verifier_image, tests, "verifier"),
        ):
            try:
                result = _docker("build", "-q", "-t", tag, str(context), timeout=self.build_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"{what} image build exceeded {self.build_timeout:.0f}s"
                ) from exc
            _check(result, f"build the {what} image")

    def _read(self, name: str) -> dict[str, bytes]:
        """Every regular file under the artifact paths, keyed relative to ``/``."""
        script = 'for p in "$@"; do if [ -d "/$p" ]; then echo "D $p"; elif [ -e "/$p" ]; then echo "F $p"; fi; done'
        relative = [artifact.lstrip("/") for artifact in self.package.artifacts]
        listing = _check(
            _docker("exec", name, "sh", "-c", script, "sh", *relative), "list artifacts"
        ).decode()
        present: list[str] = []
        for line in listing.splitlines():
            kind, path = line.split(" ", 1)
            present.append(path)
            if kind == "D":
                self.dirs.add(path)
        out: dict[str, bytes] = {}
        if present:
            raw = _check(
                _docker("exec", name, "tar", "-C", "/", "-cf", "-", "--", *present),
                "read artifacts",
            )
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                for member in archive.getmembers():
                    handle = archive.extractfile(member) if member.isfile() else None
                    if handle is not None:
                        out[member.name] = handle.read()
                        self.modes[member.name] = member.mode
        return out

    def reference(self) -> tuple[dict[str, bytes | None], dict[str, bytes | None]]:
        """The artifacts before and after the task's own oracle (``solution/solve.sh``)."""
        name = f"{self.name}-oracle"
        self._run(self.image, name, network=self.package.oracle_network)
        try:
            before = self._read(name)
            _check(_docker("exec", name, "mkdir", "-p", "/solution"), "mkdir /solution")
            solution = self.package.path / "solution"
            _check(_docker("cp", f"{solution}/.", f"{name}:/solution"), "copy the solution")
            try:
                result = _docker(
                    "exec", name, "bash", "/solution/solve.sh", timeout=self.oracle_timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"the oracle exceeded {self.oracle_timeout:.0f}s") from exc
            _check(result, "the oracle (solution/solve.sh)")
            after = self._read(name)
        finally:
            _docker("rm", "-f", name)
        paths = sorted(set(before) | set(after))
        self.unchanged = {p: before[p] for p in paths if p in before and before[p] == after.get(p)}
        return {p: before.get(p) for p in paths}, {p: after.get(p) for p in paths}

    def grade(
        self, label: str, kept: list[str], files: dict[str, bytes | None], timeout: float
    ) -> Grade:
        name = f"{self.name}-g"
        started = time.monotonic()
        try:
            self._run(self.verifier_image, name, network=self.package.verifier_network)
            submitted = self.unchanged | files
            present = {
                path: (data, self.modes.get(path, 0o644))
                for path, data in submitted.items()
                if data is not None
            }
            emptied = [d for d in sorted(self.dirs) if any(p.startswith(f"{d}/") for p in present)]
            parents = sorted({str(PurePosixPath("/", p).parent) for p in present})
            script = " && ".join(
                ["mkdir -p /logs/verifier /logs/artifacts /tests"]
                + [f"rm -rf '/{d}'" for d in emptied]
                + [f"mkdir -p '{parent}'" for parent in parents]
            )
            _check(_docker("exec", name, "sh", "-c", script), "prepare the verifier")
            if present:
                _check(_docker("cp", "-", f"{name}:/", input=_tar(present)), "upload artifacts")
            tests = self.package.path / "tests"
            _check(_docker("cp", f"{tests}/.", f"{name}:/tests"), "copy the tests")
            options = [f"--env={key}={value}" for key, value in self.package.verifier_env]
            if self.package.verifier_user is not None:
                options.append(f"--user={self.package.verifier_user}")
            return _verify(name, label, kept, timeout, started, options)
        except (RuntimeError, OSError) as exc:
            return Grade(
                label, kept, None, None, time.monotonic() - started, "error", str(exc)[-2_000:]
            )
        finally:
            _docker("rm", "-f", name)

    def close(self) -> None:
        _docker("rmi", self.image, self.verifier_image)
        for base in self.pulled:
            _docker("rmi", base)


# ---------------------------------------------------------------------------
# Minimisation


@dataclass
class Probe:
    changes: list[FileChange]
    grade: Callable[[str, set[str]], Grade]
    max_grades: int = DEFAULT_MAX_GRADES
    grades: list[Grade] = field(default_factory=list)

    @property
    def blocks(self) -> list[Block]:
        return [block for change in self.changes for block in change.blocks if block.kind != "test"]

    def _try(self, label: str, keep: set[str]) -> bool:
        grade = self.grade(label, keep)
        self.grades.append(grade)
        return grade.passed

    def _budget(self) -> bool:
        return (
            len(self.grades) < self.max_grades - 1
        )  # one grade stays reserved for the confirmation

    def run(self) -> tuple[Status, set[str], str]:
        blocks = self.blocks
        code = [block for block in blocks if block.kind == "code"]
        everything = {block.id for block in blocks}
        if len(code) < 2:
            return (
                "not_applicable",
                everything,
                f"{len(code)} code block(s): no proper subset to try",
            )
        full = self._try("full", everything)
        nop = self.grade("nop", set())
        self.grades.append(nop)
        if not full or nop.status != "scored" or nop.reward != 0.0:
            return (
                "baseline_mismatch",
                everything,
                (
                    f"baselines: full={self.grades[0].reward} ({self.grades[0].status}), "
                    f"nop={nop.reward} ({nop.status}); expected 1 and 0"
                ),
            )
        keep = set(everything)
        soft = {block.id for block in blocks if block.kind in {"comment", "noncode"}}
        if soft and self._try("drop-docs-comments", keep - soft):
            keep -= soft
        exhausted = False
        files: dict[str, list[Block]] = {}
        for block in code:
            files.setdefault(block.path, []).append(block)
        if len(files) > 1:
            for path, owned in sorted(
                files.items(), key=lambda item: -sum(b.size for b in item[1])
            ):
                candidate = keep - {block.id for block in owned}
                if not any(block.id in candidate for block in code):
                    continue
                if not self._budget():
                    exhausted = True
                    break
                if self._try(f"drop-file:{path}", candidate):
                    keep = candidate
        for block in sorted(code, key=lambda b: (-b.size, b.id)):
            if block.id not in keep:
                continue
            candidate = keep - {block.id}
            if not any(other.id in candidate for other in code):
                continue
            if not self._budget():
                exhausted = True
                break
            if self._try(f"drop-block:{block.id}", candidate):
                keep = candidate
        dropped_code = [block for block in code if block.id not in keep]
        if dropped_code and not self._try("confirm", keep):
            return (
                "flaky",
                keep,
                "the minimal subset passed once and failed on the confirmation grade",
            )
        if exhausted:
            return (
                "budget_exhausted",
                keep,
                f"stopped at {self.max_grades} grades; later blocks untried",
            )
        return "measured", keep, ""


def severity_of(kept_fraction: float) -> str:
    if kept_fraction <= HIGH_KEEP:
        return "high"
    if kept_fraction <= MEDIUM_KEEP:
        return "medium"
    return "low"


def mutation_pass(
    changes: list[FileChange],
    grade_files: Callable[[str, dict[str, bytes | None]], Grade],
    grades: list[Grade],
    *,
    limit: int,
    seed: str,
    tests: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, bytes | None]]:
    """Line-level pass inside the reference, with ``hack mutate``'s one-edit operators.

    Every mutant is the full reference with one edit on a line the reference wrote (comparison
    boundaries, ``and``/``or``, dropped operands and statements, negated conditions, ...), graded
    like any subset. A survivor (reward 1) is behaviour inside a block the tests need that no test
    pins down: the gap a partial fix slips through and block drops cannot see. Reverting a whole
    hunk is the block probe's job and is left out. The baselines are graded here when the block
    probe never ran them. Returns the summary and each survivor's file bytes by mutant id.
    """
    full = {c.path: c.fixed for c in changes if c.path not in tests}
    base = {c.path: c.base for c in changes if c.path not in tests}
    by_label = {grade.label: grade for grade in grades}
    full_grade = by_label.get("full") or _record(grades, grade_files("full", full))
    nop_grade = by_label.get("nop") or _record(grades, grade_files("nop", base))
    summary: dict[str, Any] = {"stage": STAGE_MUTANTS, "limit": limit}
    if not full_grade.passed or nop_grade.status != "scored" or nop_grade.reward != 0.0:
        reason = f"baselines: full={full_grade.reward} ({full_grade.status}), nop={nop_grade.reward} ({nop_grade.status})"
        return summary | {
            "status": "baseline_mismatch",
            "reason": reason,
            "verdict": "unknown",
            "severity": None,
        }, {}
    candidates: list[Mutant] = []
    mutated_bytes: dict[str, tuple[str, bytes]] = {}
    for change in changes:
        if change.fixed is None or change.path in tests:
            continue
        if not any(block.kind == "code" for block in change.blocks):
            continue
        for mutant, mutated in generate_mutants(change.path, change.fixed, change.base):
            if mutant.changed_line and mutant.operator != "revert-hunk":
                candidates.append(mutant)
                mutated_bytes[mutant.id] = (change.path, mutated)
    chosen = select_mutants(candidates, limit=limit, seed=seed)
    survivors: list[dict[str, Any]] = []
    files: dict[str, bytes | None] = {}
    killed = unscored = 0
    for mutant in chosen:
        path, mutated = mutated_bytes[mutant.id]
        result = _record(grades, grade_files(f"mutant:{mutant.id}", full | {path: mutated}))
        if result.passed:
            survivors.append(
                {
                    "id": mutant.id, "file": path, "line": mutant.line, "end_line": mutant.end_line,
                    "operator": mutant.operator, "original": mutant.original,
                    "replacement": mutant.replacement, "edit": mutant.patch,
                }
            )  # fmt: skip
            files[mutant.id] = mutated
        elif result.status == "scored":
            killed += 1
        else:
            unscored += 1
    scored = killed + len(survivors)
    if survivors:
        verdict, share = "fail", len(survivors) / scored
        severity = "high" if share >= 0.5 else "medium" if share >= 0.2 else "low"
    else:
        verdict = "pass" if chosen and not unscored else "unknown"
        severity = None
    status = "measured" if chosen else "not_applicable"
    return summary | {
        "status": status,
        "reason": "" if chosen else "no mutable code on the lines the reference wrote",
        "verdict": verdict, "severity": severity, "generated": len(candidates), "tried": len(chosen),
        "killed": killed, "unscored": unscored, "survivors": survivors,
    }, files  # fmt: skip


def _record(grades: list[Grade], grade: Grade) -> Grade:
    grades.append(grade)
    return grade


def mutants_finding(report: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """The ``qa.finding/v1`` for the line-level pass of one task."""
    summary = report["mutants"]
    survivors = summary.get("survivors") or []
    if summary["verdict"] == "fail":
        lines = ", ".join(f"{s['file']}:{s['line']} {s['operator']}" for s in survivors[:4])
        text = (
            f"{len(survivors)}/{summary['killed'] + len(survivors)} one-edit mutants of lines the "
            f"reference wrote pass the hidden tests ({lines})"
        )
    elif summary["verdict"] == "pass":
        text = f"the hidden tests kill all {summary['tried']} mutants of lines the reference wrote"
    else:
        text = f"{summary['status']}: {summary.get('reason', '')}"
    first = survivors[0]["id"] if survivors else None
    return {
        "schema": SCHEMA_FINDING,
        "subject": {"kind": "task", "task_id": report["task_id"], "task_digest": report.get("package_digest")},
        "stage": STAGE_MUTANTS,
        "verdict": summary["verdict"],
        "category": "weak",
        "severity": summary["severity"],
        "fault": "eval" if summary["verdict"] == "fail" else None,
        "summary": text,
        "evidence_refs": [
            str(out_dir / "report.json"),
            *(str(out_dir / "mutants" / f"{s['id']}.patch") for s in survivors),
            *(s["witness"] for s in survivors if s.get("witness")),
        ],
        "reproducer": (
            {"kind": "patch", "path": str(out_dir / "mutants" / f"{first}.patch"), "expected_reward": 1}
            if first
            else None
        ),
        "refutation": {
            "status": "unverified",
            "by": None,
            "reason": "a surviving mutant can be behaviour-equivalent or change behaviour the instruction "
            "never asks for; read the edit against the instruction",
        },
        "reviewer": REVIEWER,
    }  # fmt: skip


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def summarize(
    task_id: str,
    changes: list[FileChange],
    status: Status,
    keep: set[str],
    reason: str,
    grades: list[Grade],
) -> dict[str, Any]:
    blocks = [block for change in changes for block in change.blocks]
    code = [block for block in blocks if block.kind == "code"]
    code_lines = sum(block.size for block in code)
    kept_lines = sum(block.size for block in code if block.id in keep)
    dropped = [block.id for block in code if block.id not in keep]
    measured = status in {"measured", "budget_exhausted"}
    verdict = "unknown" if not measured else "fail" if dropped else "pass"
    kept_fraction = kept_lines / code_lines if code_lines else 1.0
    return {
        "task_id": task_id,
        "status": status,
        "reason": reason,
        "verdict": verdict,
        "severity": severity_of(kept_fraction) if verdict == "fail" else None,
        "code_blocks": len(code),
        "code_lines": code_lines,
        "kept_code_lines": kept_lines,
        "kept_fraction": round(kept_fraction, 4),
        "kept": sorted(keep),
        "dropped_code": dropped,
        "blocks": [
            {
                "id": block.id,
                "path": block.path,
                "kind": block.kind,
                "base_lines": [block.base_start, block.base_end],
                "removed": block.removed,
                "added": block.added,
            }
            for block in blocks
        ],  # fmt: skip
        "grades": [
            {
                "label": grade.label,
                "kept": len(grade.kept),
                "reward": grade.reward,
                "rc": grade.rc,
                "status": grade.status,
                "seconds": round(grade.seconds, 1),
            }
            for grade in grades
        ],  # fmt: skip
    }


def finding(report: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    verdict = report["verdict"]
    dropped = len(report.get("dropped_code") or [])
    if verdict == "fail":
        summary = (
            f"{report['kept_code_lines']}/{report['code_lines']} reference code lines "
            f"({report['code_blocks'] - dropped}/{report['code_blocks']} blocks) pass the hidden tests"
        )
    elif verdict == "pass":
        summary = f"every one of {report['code_blocks']} reference code blocks is needed by the hidden tests"
    else:
        summary = f"{report['status']}: {report['reason']}"
    evidence = [str(out_dir / "report.json")]
    evidence += [
        str(out_dir / name)
        for name in ("minimal.patch", "dropped.patch", "minimal.sh", "reference.patch", "reference.sh")
        if (out_dir / name).is_file()
    ]
    return {
        "schema": SCHEMA_FINDING,
        "subject": {
            "kind": "task",
            "task_id": report["task_id"],
            "task_digest": report.get("package_digest"),
        },
        "stage": STAGE_TASK,
        "verdict": verdict,
        "category": "weak",
        "severity": report["severity"],
        "fault": "eval" if verdict == "fail" else None,
        "summary": summary,
        "evidence_refs": evidence,
        "reproducer": (
            {"kind": "patch", "path": str(out_dir / "minimal.patch"), "expected_reward": 1}
            if verdict == "fail"
            else None
        ),
        "refutation": {
            "status": "unverified",
            "by": None,
            "reason": "a dropped block can be behaviour the instruction never asks for (refactor, logging, "
            "defensive branch); read dropped.patch against the instruction",
        },
        "reviewer": REVIEWER,
    }


# ---------------------------------------------------------------------------
# One task


def _shared_config(path: Path) -> dict[str, Any] | None:
    """Select native shared tasks without widening the two legacy Package variants."""
    from evallab.verifier_mutation import _task_config

    config = _task_config(path)
    verifier = config.get("verifier") or {}
    environment = config.get("environment") or {}
    if (
        environment.get("docker_image")
        and (path / "environment/setup/setup.sh").is_file()
        and (path / "tests/test.sh").is_file()
    ):
        return None
    mode = verifier.get("environment_mode")
    if mode is None:
        mode = "separate" if verifier.get("environment") is not None else "shared"
    return config if mode == "shared" else None


def _shared_targets(path: Path, config: dict[str, Any], targets: Sequence[str]) -> list[str]:
    from evallab.campaign_execution import docker_task_resources
    from evallab.verifier_mutation import declared_targets

    if config.get("steps"):
        raise UnsupportedTask("native solution overrides do not support declared task steps")
    if not (path / "solution/solve.sh").is_file() or not (path / "tests/test.sh").is_file():
        raise UnsupportedTask("native shared tasks need solution/solve.sh and tests/test.sh")
    if any((path / "environment" / name).exists() for name in (
        "docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml",
    )):
        raise UnsupportedTask("native ref-subset supports one sandbox, not task Compose services")
    if (config.get("environment") or {}).get("gpus"):
        raise UnsupportedTask("native ref-subset cannot admit a local GPU task")
    docker_task_resources(path)  # Local manifest validation only; never lower its limits.
    timeout = (config.get("agent") or {}).get("timeout_sec")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 28_800
        or int(timeout) != timeout
    ):
        raise UnsupportedTask("native ref-subset needs a declared integral agent timeout of 1–28800 seconds")
    chosen = list(dict.fromkeys(targets or declared_targets(config)))
    if not chosen:
        raise UnsupportedTask("the task declares no artifacts; pass repeated --target absolute FILE paths")
    for target in chosen:
        pure = PurePosixPath(target)
        if (
            not pure.is_absolute()
            or str(pure) != target
            or ".." in pure.parts
            or target == "/"
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
        ):
            raise UnsupportedTask(f"native targets must be normalized absolute file paths: {target!r}")
        if pure.parts[1] in {"logs", "tests", "solution", "proc", "sys", "dev"}:
            raise UnsupportedTask(f"native target overlaps harness/runtime files: {target}")
    return chosen


def _native_path_checks(target: str) -> str:
    """Refuse symlinks (including ancestors), directories and special files."""
    pure = PurePosixPath(target)
    lines = []
    for parent in [*reversed(pure.parents), pure]:
        quoted = shlex.quote(str(parent))
        lines.append(f"if [ -L {quoted} ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi\n")
    quoted = shlex.quote(target)
    lines.append(
        f"if [ -e {quoted} ] && [ ! -f {quoted} ]; then "
        "echo 'ref-subset: target is not a regular file' >&2; exit 97; fi\n"
    )
    return "".join(lines)


def _native_capture_script(solve: str | None, targets: Sequence[str]) -> str:
    from evallab.verifier_mutation import CAPTURE_ROOT, capture_script

    states = f"{CAPTURE_ROOT}/.ref-subset-states"
    lines = [capture_script(solve, ()), "set -e\n", f"mkdir -p {shlex.quote(states)}\n"]
    for index, target in enumerate(targets):
        lines.append(_native_path_checks(target))
        lines.append(
            f"if [ -f {shlex.quote(target)} ]; then printf 'present\\n'; "
            f"else printf 'absent\\n'; fi > {shlex.quote(f'{states}/{index}')}\n"
        )
        lines.append(
            f"if [ -f {shlex.quote(target)} ]; then "
            f"stat -c '%a' -- {shlex.quote(target)} > {shlex.quote(f'{states}/{index}.mode')}; fi\n"
        )
    lines += [
        capture_script(None, targets),
        f"printf 'complete\\n' > {shlex.quote(f'{states}/complete')}\n",
    ]
    return "".join(lines)


def _native_captured_files(
    repo: Path, run: Any, targets: Sequence[str]
) -> tuple[dict[str, bytes | None], dict[str, int | None]]:
    from evallab.verifier_mutation import _captured_files, _captured_root

    root = _captured_root(repo, run)
    complete = root / ".ref-subset-states/complete" if root is not None else None
    if complete is None or not complete.is_file() or complete.read_bytes() != b"complete\n":
        raise RuntimeError(f"{run.name}: native capture did not complete")
    assert root is not None
    files: dict[str, bytes | None] = {}
    modes: dict[str, int | None] = {}
    for index, target in enumerate(targets):
        state = (root / ".ref-subset-states" / str(index)).read_bytes()
        host = root / target.lstrip("/")
        relative = host.relative_to(root)
        if any(root.joinpath(*relative.parts[:end]).is_symlink() for end in range(1, len(relative.parts) + 1)):
            raise UnsupportedTask(f"{target}: captured symlink is not a regular-file witness")
        if state == b"absent\n" and not host.exists():
            files[target.lstrip("/")] = None
            modes[target.lstrip("/")] = None
        elif state == b"present\n" and host.is_file():
            files[target.lstrip("/")] = _captured_files(root, target)[target]
            mode = (root / ".ref-subset-states" / f"{index}.mode").read_text().strip()
            if not re.fullmatch(r"[0-7]{1,4}", mode) or int(mode, 8) & 0o7000:
                raise UnsupportedTask(f"{target}: missing/unsupported native file permission bits")
            modes[target.lstrip("/")] = int(mode, 8)
        else:
            raise RuntimeError(f"{run.name}: incomplete or non-file capture for {target}")
    return files, modes


def _native_overlay_script(
    files: dict[str, bytes | None], modes: dict[str, int | None]
) -> str:
    """Only selected files on fresh base; exact bytes with no interpreter/decoder dependency."""
    lines = ["#!/usr/bin/env bash\n# ref-subset: selected files on fresh base; no reference replay\nset -e\n"]
    for path, data in sorted(files.items()):
        lines.append(_native_path_checks(path))
        quoted = shlex.quote(path)
        if data is None:
            lines.append(f"rm -f -- {quoted}\n")
            continue
        lines += [f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}\n", f": > {quoted}\n"]
        for start in range(0, len(data), 4096):
            escaped = "".join(f"\\0{byte:03o}" for byte in data[start:start + 4096])
            lines.append(f"printf '%b' '{escaped}' >> {quoted}\n")
        mode = modes.get(path)
        if mode is None:
            raise UnsupportedTask(f"{path}: cannot reconstruct a file without captured permission bits")
        lines.append(f"chmod {mode:o} {quoted}\n")
    return "".join(lines)


def _grade_entry(grade: Grade) -> dict[str, Any]:
    result = {
        "label": grade.label, "kept": len(grade.kept), "kept_blocks": grade.kept,
        "reward": grade.reward, "rc": grade.rc, "status": grade.status,
        "seconds": grade.seconds, "tail": grade.tail,
    }
    if grade.native is not None:
        result["native"] = grade.native
    return result


def _probe_shared_task(
    package_dir: Path,
    out_dir: Path,
    config: dict[str, Any],
    targets: Sequence[str],
    *,
    repo_root: Path,
    max_grades: int,
    mutants: int,
    mutation_seed: str | None,
    requested_limits: dict[str, Any],
    log: Callable[[str], None],
) -> dict[str, Any]:
    from evallab.campaign_execution import docker_available_resources, docker_task_resources
    from evallab.docker_lock import docker_slot
    from evallab.execution_contracts import new_ulid
    from evallab.registry import compute_task_digests
    from evallab.runner import run_matrix
    from evallab.verifier_mutation import _Audit, _slug

    task_id = str((config.get("task") or {}).get("name") or package_dir.name)
    try:
        selected = _shared_targets(package_dir, config, targets)
        if max_grades < 4 or mutants < 0:
            raise UnsupportedTask("native ref-subset needs max_grades >= 4 and mutants >= 0")
        repo = repo_root.resolve()
        output = out_dir.resolve()
        if not output.is_relative_to(repo):
            raise UnsupportedTask(f"native output directory must stay inside its repository ({repo})")
    except (ValueError, OSError) as exc:
        return _write_skipped(task_id, package_dir, out_dir, "not_applicable", str(exc))
    output.mkdir(parents=True, exist_ok=True)
    native_dir = output / "native"
    native_dir.mkdir(exist_ok=True)
    timeout = int(config["agent"]["timeout_sec"])
    digests = compute_task_digests(package_dir)
    solve_bytes = (package_dir / "solution/solve.sh").read_bytes()
    try:
        solve = solve_bytes.decode("utf-8")  # Preserve CRLF and the intact reference/helper body.
    except UnicodeDecodeError as exc:
        return _write_skipped(
            task_id, package_dir, out_dir, "not_applicable",
            f"native capture requires an intact UTF-8 reference script; unsupported encoding: {exc}",
        )
    head: dict[str, Any] = {
        "schema": SCHEMA_REPORT, "backend": "docker", "package_type": "native-shared",
        "package": str(package_dir), "package_digest": digests.package,
        "verifier_digest": digests.verifier, "task_id": task_id, "started_at": _now(),
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reference": {
            "oracle": "solution/solve.sh", "oracle_sha256": hashlib.sha256(solve_bytes).hexdigest(),
            "patch": str(output / "reference.patch"), "patch_root": "/",
        },
        "limits": {
            "max_grades": max_grades, "mutants": mutants, "matrix_workers": 1,
            "shared_docker_slots": 2, "matrix_timeout_seconds": timeout,
            "requested_legacy_limits": requested_limits,
            "declared_agent_timeout_sec": config["agent"]["timeout_sec"],
            "effective_agent_timeout_sec": timeout, "agent_timeout_multiplier": 1.0,
            "mutation_seed": task_id if mutation_seed is None else mutation_seed,
            "additional_original_capture_grades": 1,
            "subset_limit_includes": ["full", "nop"],
            "environment": config.get("environment") or {},
            "verifier": config.get("verifier") or {},
            "resource_admission": "live native Docker capacity, including controller/competitor reserve",
            "overrides": "legacy memory/oracle_timeout/grade_timeout do not override native task limits",
        },
        "native_limits": [
            "Selected regular-file bytes and permission bits are captured; absence is distinct from empty.",
            "Every candidate applies ONLY the complete selected-file mapping to fresh base, with no "
            "reference replay. Original oracle=1, actual nop=0 and independent full-target reconstruction=1 "
            "are required. Unselected files, non-file side effects, ownership and compiled outputs are "
            "not reconstructed; failed reconstruction is unsupported/unmeasured, never a replay fallback.",
            "A surviving source edit may be dead or not consumed by the verifier. Instruction/consumer "
            "adjudication is required; these are hypotheses, not accepted weak-test positives.",
            "Targets must be explicit files (or declared file artifacts); directories, symlinks, "
            "special files, task steps, Compose services and GPUs are unsupported.",
        ],
        "targets": selected,
    }
    bindings: dict[str, dict[str, Any]] = {}
    runs: list[Any] = []
    grades: list[Grade] = []
    base_modes: dict[str, int | None] = {}
    fixed_modes: dict[str, int | None] = {}
    base: dict[str, bytes | None] = {}
    fixed: dict[str, bytes | None] = {}
    changes: list[FileChange] = []
    report: dict[str, Any] = dict(head)

    def runner(matrix: Any, root: Path) -> list[dict[str, Any]]:
        matrix_path = native_dir / "matrices" / f"{matrix.name}.json"
        common = {"matrix_id": matrix.matrix_id, "matrix": str(matrix_path.relative_to(repo))}
        for entry in matrix.runs:
            bindings[entry.name] = dict(common)
        # One shared FIFO owner per sequential matrix; Executor does not acquire a nested slot.
        with docker_slot(label=matrix.name, slots=2, timeout=timeout):
            required_cpu, required_mb = docker_task_resources(package_dir)
            try:
                available_cpu, available_mb = docker_available_resources()
                resource_mode = "bounded_containers_subtracted"
            except ValueError as exc:
                if "unbounded CPU or memory" not in str(exc):
                    raise
                # Pre-existing host services (e.g. local Postgres) are unbounded
                # but are not competing benchmark sandboxes; fail-closed
                # subtraction would make every native run impossible on this
                # host. Fall back to daemon capacity minus the same 2 CPU /
                # 2 GiB controller reserve, and disclose the mode in evidence.
                info = json.loads(subprocess.run(
                    ["docker", "info", "--format", "{{json .}}"],
                    capture_output=True, text=True, check=True, timeout=10,
                ).stdout)
                available_cpu = float(info["NCPU"] - 2)
                available_mb = info["MemTotal"] // (1024 * 1024) - 2048
                resource_mode = "capacity_minus_reserve_unbounded_services_present"
            resources = {
                "required_cpus": required_cpu, "required_memory_mb": required_mb,
                "available_cpus": available_cpu, "available_memory_mb": available_mb,
                "mode": resource_mode,
            }
            for binding in (bindings[entry.name] for entry in matrix.runs):
                binding["resources"] = resources
            if required_cpu > available_cpu or required_mb > available_mb:
                raise RuntimeError(f"native task resource-ineligible; no limits lowered: {resources}")
            if compute_task_digests(package_dir).package != digests.package:
                raise RuntimeError("native source package changed since the reference was bound")
            execution = run_matrix(matrix, root=root)
        results = []
        for row in execution.results:
            if any(isinstance(value, float) and not math.isfinite(value) for value in row.get("rewards") or []):
                row = row | {
                    "status": "infra", "rewards": [],
                    "error": "native returned non-finite reward; raw outcome is retained in its receipt",
                }
            results.append(row)
        by_name = {row["name"]: row for row in results}
        for entry in matrix.runs:
            bindings[entry.name].update(
                receipt=str(execution.receipt_path.relative_to(repo)),
                invocations=str(execution.invocation_path.relative_to(repo)),
                result=by_name.get(entry.name),
            )
        return results

    audit = _Audit(
        package=package_dir.resolve(), repo_root=repo, run_dir=native_dir,
        slug=_slug(task_id.rsplit("/", 1)[-1], limit=28), nonce=new_ulid().lower()[-6:],
        solve=solve, digests=digests, timeout_seconds=timeout, workers=1, runner=runner,
    )

    def execute(label: str, script: str, role: Any, kept: Sequence[str] = ()) -> Grade:
        started = time.monotonic()
        stage_label = f"{len(runs):04d}-{_slug(label, limit=40)}"
        run = audit.stage(role, stage_label, script)
        (repo / run.script).chmod(0o755)
        runs.append(run)
        failure = ""
        try:
            audit.execute([run], stage_label)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            run.status, run.error = "infra", str(exc)
            failure = str(exc)
        binding = run.model_dump(mode="json") | bindings.get(run.name, {})
        binding["package_digest"] = digests.package
        binding["trials"] = []
        rc = None
        if run.job_dir is not None:
            for trial in sorted((repo / run.job_dir).iterdir()):
                if trial.is_dir() and (trial / "result.json").is_file():
                    binding["trials"].append(str(trial.relative_to(repo)))
                    exit_path = trial / "agent/exit-code.txt"
                    if exit_path.is_file():
                        with suppress(ValueError):
                            rc = int(exit_path.read_text())
        scored = run.status in {"ok", "mismatch"} and run.reward is not None and math.isfinite(run.reward)
        grade = Grade(
            label, list(kept), run.reward if scored else None, rc, time.monotonic() - started,
            "scored" if scored else "error", failure or run.error or "", binding,
        )
        grades.append(grade)
        log(f"  {task_id} {label}: reward={grade.reward} {grade.status} {grade.seconds:.0f}s")
        if not scored:
            raise RuntimeError(f"{label}: native outcome unavailable; {grade.tail or run.status}")
        return grade

    def candidate_modes(files: dict[str, bytes | None]) -> dict[str, int | None]:
        return {
            path: None if data is None else fixed_modes[path] if fixed[path] is not None else base_modes[path]
            for path, data in files.items()
        }

    def grade_files(label: str, files: dict[str, bytes | None], kept: Sequence[str] = ()) -> Grade:
        if label in controls:
            expected = fixed if label == "full" else base
            if any(expected.get(path) != data for path, data in files.items()):
                raise RuntimeError(f"{label}: baseline does not match its actual native capture")
            controls[label].kept = list(kept)
            return controls[label]
        complete = fixed | files
        modes = candidate_modes(complete)
        script = _native_overlay_script(
            {"/" + path: data for path, data in complete.items()},
            {"/" + path: mode for path, mode in modes.items()},
        )
        witness = None
        patch = None
        if label.startswith("mutant:"):
            ident = label.split(":", 1)[1]
            directory = output / "mutants"
            directory.mkdir(exist_ok=True)
            patch, witness = directory / f"{ident}.patch", directory / f"{ident}.sh"
        elif label == "confirm":
            patch, witness = output / "minimal.patch", output / "minimal.sh"
        if patch is not None and witness is not None:
            patch.write_text(unified_patch(base, complete, before_modes=base_modes, after_modes=modes))
            witness.write_text(script)
            witness.chmod(0o755)
        try:
            return execute(label, script, "mutant" if label.startswith("mutant:") else "equivalent", kept)
        finally:
            if witness is not None and grades and grades[-1].label == label:
                assert grades[-1].native is not None
                grades[-1].native.update(patch=str(patch), witness=str(witness), patch_root="/")

    controls: dict[str, Grade] = {}
    try:
        for label, reference, role in (
            ("oracle", solve, "capture-oracle"), ("nop", None, "capture-base"),
        ):
            controls[label] = execute(label, _native_capture_script(reference, selected), role)
        for label, destination, mode_destination in (
            ("oracle", fixed, fixed_modes), ("nop", base, base_modes),
        ):
            run = runs[0 if label == "oracle" else 1]
            captured, modes = _native_captured_files(repo, run, selected)
            destination.update(captured)
            mode_destination.update(modes)
        for path in base:
            if base[path] is not None and fixed[path] is not None and base_modes[path] != fixed_modes[path]:
                raise UnsupportedTask(f"{path}: existing-file mode changes are not represented by byte change blocks")
        head["captures"] = {
            label: {
                path: None if data is None else {
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": modes[path],
                }
                for path, data in files.items()
            }
            for label, files, modes in (("base", base, base_modes), ("oracle", fixed, fixed_modes))
        }
        reference_patch = unified_patch(base, fixed, before_modes=base_modes, after_modes=fixed_modes)
        (output / "reference.patch").write_text(reference_patch)
        full_script = _native_overlay_script(
            {"/" + path: data for path, data in fixed.items()},
            {"/" + path: mode for path, mode in fixed_modes.items()},
        )
        (output / "reference.sh").write_text(full_script)
        (output / "reference.sh").chmod(0o755)
        head["reference"]["sha256"] = hashlib.sha256(reference_patch.encode()).hexdigest()
        head["reference"]["witness"] = str(output / "reference.sh")
        head["reference"]["oracle_capture_script"] = runs[0].script
        changes = [
            file_change(path, base[path], fixed[path], set())
            for path in base if base[path] != fixed[path]
        ]

        def grade(label: str, keep: set[str]) -> Grade:
            return grade_files(
                label, {change.path: compose(change, keep) for change in changes}, sorted(keep)
            )

        search_grades: list[Grade] = []
        if controls["oracle"].reward != 1.0 or controls["nop"].reward != 0.0:
            status, keep, reason = (
                "baseline_mismatch", {b.id for c in changes for b in c.blocks},
                f"native controls oracle={controls['oracle'].reward}, nop={controls['nop'].reward}; expected 1 and 0",
            )
        else:
            controls["full"] = execute("full", full_script, "equivalent")
            if not controls["full"].passed:
                raise UnsupportedTask(
                    "selected targets cannot reconstruct the passing reference on fresh base: "
                    f"full-target reward={controls['full'].reward}; unsupported/unmeasured, no reference-replay fallback"
                )
            status, keep, reason = Probe(changes, grade, max_grades=max_grades, grades=search_grades).run()
        report = head | summarize(task_id, changes, status, keep, reason, grades)
        report["coverage"] = {
            "subset": "unknown" if status == "budget_exhausted" else status,
            "mutations": "not_requested" if not mutants else "unknown",
        }
        if status == "budget_exhausted":
            report.update(verdict="unknown", severity=None)
        if (output / "minimal.patch").is_file():
            kept_files = {change.path: compose(change, keep) for change in changes}
            kept_complete = base | kept_files
            (output / "dropped.patch").write_text(unified_patch(
                kept_complete, fixed, before_modes=candidate_modes(kept_complete), after_modes=fixed_modes,
            ))
        if mutants and status not in {"baseline_mismatch", "flaky"}:
            summary, _ = mutation_pass(
                changes, grade_files, search_grades, limit=mutants,
                seed=task_id if mutation_seed is None else mutation_seed,
            )
            summary["untried"] = summary.get("generated", 0) - summary.get("tried", 0)
            summary["coverage"] = "unknown" if summary["untried"] or summary.get("unscored") else summary["status"]
            if summary["untried"]:
                summary.update(status="budget_exhausted", reason="mutation limit left generated candidates untried")
                if summary["verdict"] == "pass":
                    summary.update(verdict="unknown", severity=None)
            for survivor in summary.get("survivors") or []:
                survivor.update(
                    patch=str(output / "mutants" / f"{survivor['id']}.patch"),
                    witness=str(output / "mutants" / f"{survivor['id']}.sh"), patch_root="/",
                )
            report["mutants"] = summary
            report["coverage"]["mutations"] = summary["coverage"]
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, ImportError) as exc:
        report = head | report | {
            "status": "not_applicable" if isinstance(exc, UnsupportedTask) else "unavailable",
            "reason": str(exc), "verdict": "unknown", "severity": None,
            "coverage": {"subset": "unknown", "mutations": "unknown"},
        }
        if mutants:
            report["mutants"] = {
                "stage": STAGE_MUTANTS, "limit": mutants, "status": report["status"],
                "reason": str(exc), "verdict": "unknown", "severity": None, "coverage": "unknown",
                "tried": sum(g.label.startswith("mutant:") for g in grades),
                "generated": None, "untried": None,
            }
    if "mutants" in report:
        report["mutants"]["runs"] = [
            _grade_entry(g) | {"id": g.label.split(":", 1)[1],
                              "outcome": "unknown" if g.status != "scored" else "survived" if g.passed else "killed"}
            for g in grades if g.label.startswith("mutant:")
        ]
    report.update(
        finished_at=_now(), grades=[_grade_entry(grade) for grade in grades],
        execution={"transport": "runner.run_matrix/Executor/HostIsolatedDockerEnvironment",
                   "runs": [grade.native for grade in grades]},
    )
    (output / "grades.log").write_text(
        "".join(
            f"== {g.label} reward={g.reward} rc={g.rc} {g.status} {g.seconds:.1f}s\n"
            f"{json.dumps(_grade_entry(g), sort_keys=True)}\n" for g in grades
        )
    )
    (output / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    (output / "finding.json").write_text(json.dumps(finding(report, output), indent=1, sort_keys=True) + "\n")
    if "mutants" in report:
        (output / "finding-mutants.json").write_text(
            json.dumps(mutants_finding(report, output), indent=1, sort_keys=True) + "\n"
        )
    return report


def probe_task(
    package_dir: Path,
    patch_path: Path | None,
    out_dir: Path,
    *,
    max_grades: int = DEFAULT_MAX_GRADES,
    memory: str = "4g",
    grade_timeout: float = 1_800,
    oracle_timeout: float = 3_600,
    mutants: int = 0,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
    targets: Sequence[str] = (),
    repo_root: Path | None = None,
    mutation_seed: str | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Probe one task and write report.json, finding.json and the patches to ``out_dir``.

    A MiMo package needs ``patch_path``; a separate-verifier package takes its reference from
    its own oracle and records it as ``reference.patch`` (over the artifacts, relative to ``/``).
    ``mutants`` > 0 adds the line-level pass (:func:`mutation_pass`): ``report["mutants"]``,
    ``finding-mutants.json`` and one reproducer per survivor in ``mutants/``.
    Shared-verifier tasks use unchanged native matrices and selected ``targets`` (absolute files).
    Native output must be inside ``repo_root`` (defaults to this checkout); the legacy memory
    and timeout arguments do not override native manifest limits. ``mutation_seed`` overrides
    the default task-id seed for deterministic line-mutant selection.
    """
    _check_environment(environment, spend_gate)
    if environment == "modal" and mutants < 0:
        raise ValueError("Modal mutant count must be nonnegative")
    shared = _shared_config(package_dir)
    if shared is not None:
        if patch_path is not None or environment != "docker":
            return _write_skipped(
                package_dir.name, package_dir, out_dir, "not_applicable",
                "native shared tasks require Docker and derive their reference from the intact oracle (no --patch)",
                environment=environment, spend_gate=spend_gate,
            )
        return _probe_shared_task(
            package_dir, out_dir, shared, targets,
            repo_root=repo_root or Path(__file__).resolve().parents[2],
            max_grades=max_grades, mutants=mutants, mutation_seed=mutation_seed,
            requested_limits={"memory": memory, "grade_timeout_s": grade_timeout,
                              "oracle_timeout_s": oracle_timeout},
            log=log,
        )
    if targets:
        raise ValueError("--target is only supported for native shared-verifier tasks")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = _now()
    try:
        package = Package.load(package_dir)
    except UnsupportedTask as exc:
        return _write_skipped(
            package_dir.name,
            package_dir,
            out_dir,
            "not_applicable",
            str(exc),
            environment=environment,
            spend_gate=spend_gate,
        )
    if package.separate == (patch_path is not None):
        raise ValueError(
            f"{package_dir}: a separate-verifier task derives its reference from its oracle (no --patch)"
            if package.separate
            else f"{package_dir}: a MiMo package needs its reference --patch"
        )
    patch = patch_path.read_text() if patch_path is not None else ""
    sandbox: Any | None = None
    modal_sandbox = None
    separate_sandbox: SeparateSandbox | None = None
    raw_sandbox: Sandbox | None = None
    if package.verifier_timeout:
        grade_timeout = min(grade_timeout, package.verifier_timeout)
    head: dict[str, Any] = {
        "schema": SCHEMA_REPORT,
        "backend": environment,
        "package": str(package_dir),
        "package_digest": _package_digest(package_dir),
        "image": package.image,
        "reference": (
            {"oracle": "solution/solve.sh", "patch": str(out_dir / "reference.patch")}
            if package.separate
            else {"patch": str(patch_path), "sha256": hashlib.sha256(patch.encode()).hexdigest()}
        ),
        "limits": {
            "max_grades": max_grades,
            "memory": memory,
            "grade_timeout_s": grade_timeout,
            "network": (
                {
                    "oracle": "default" if package.oracle_network else "none",
                    "verifier": "default" if package.verifier_network else "none",
                }
                if package.separate
                else "none"
            ),
        },
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "started_at": started,
    }
    grades: list[Grade] = []
    try:
        if environment == "modal":
            assert spend_gate is not None
            problem = modal_resource_blocker(package_dir)
            if problem is not None:
                raise RuntimeError(problem)
            from evallab.ref_subset_modal import ModalSandbox

            modal_sandbox = ModalSandbox(
                package,
                spend_gate=spend_gate,
                out_dir=out_dir / "native",
                memory=memory,
                oracle_timeout=oracle_timeout,
                grade_timeout=grade_timeout,
                build_timeout=min(1_800, oracle_timeout),
            )
            sandbox = modal_sandbox
        elif package.separate:
            separate_sandbox = SeparateSandbox(
                package, memory=memory, oracle_timeout=oracle_timeout
            )
            sandbox = separate_sandbox
            head["image"] = separate_sandbox.image
        else:
            raw_sandbox = Sandbox(package, memory=memory)
            sandbox = raw_sandbox
        if environment == "docker" and package.gpus:
            raise RuntimeError(f"the task needs {package.gpus} GPU(s); local Docker has none")
        try:
            sandbox.prepare()
            if package.separate:
                if modal_sandbox is not None:
                    base, fixed = modal_sandbox.reference()
                else:
                    assert separate_sandbox is not None
                    base, fixed = separate_sandbox.reference()
                paths = list(base)
                patch = unified_patch(base, fixed)
                (out_dir / "reference.patch").write_text(patch)
                head["reference"]["sha256"] = hashlib.sha256(patch.encode()).hexdigest()
            else:
                paths = patch_paths(patch)
                reader = modal_sandbox if modal_sandbox is not None else raw_sandbox
                assert reader is not None
                base = reader.read(paths)
                fixed = apply_reference(patch, base)
            changes = [
                file_change(path, base[path], fixed[path], set(package.tests)) for path in paths
            ]
            changes = [change for change in changes if change.base != change.fixed]

            def grade_files(
                label: str, files: dict[str, bytes | None], kept: Sequence[str] = ()
            ) -> Grade:
                timeout = grade_timeout
                if grades and grades[0].status == "scored":
                    timeout = min(grade_timeout, max(300.0, 3 * grades[0].seconds))
                result = sandbox.grade(label, list(kept), files, timeout)
                log(
                    f"  {package.task_id} {label}: reward={result.reward} {result.status} {result.seconds:.0f}s"
                )
                if environment == "modal" and result.status != "scored":
                    grades.append(result)
                    raise RuntimeError(
                        f"{label}: {result.status}; no measured reward: {result.tail[-500:]}"
                    )
                return result

            def grade(label: str, keep: set[str]) -> Grade:
                files = {
                    change.path: compose(change, keep)
                    for change in changes
                    if change.path not in package.tests
                }
                return grade_files(label, files, sorted(keep))

            probe = Probe(changes, grade, max_grades=max_grades, grades=grades)
            status, keep, reason = probe.run()
            report = head | summarize(package.task_id, changes, status, keep, reason, grades)
            if environment == "modal" and status == "budget_exhausted":
                report.update(verdict="unknown", severity=None)
            base_files = {change.path: change.base for change in changes}
            full_files = {change.path: change.fixed for change in changes}
            if report["verdict"] == "fail":
                kept_files = {change.path: compose(change, keep) for change in changes}
                (out_dir / "minimal.patch").write_text(unified_patch(base_files, kept_files))
                (out_dir / "dropped.patch").write_text(unified_patch(kept_files, full_files))
            if mutants > 0:
                summary, survivors = mutation_pass(
                    changes, grade_files, grades, limit=mutants,
                    seed=package.task_id if mutation_seed is None else mutation_seed,
                    tests=package.tests,
                )  # fmt: skip
                report["mutants"] = summary
                for survivor in summary.get("survivors") or []:
                    mutated = full_files | {survivor["file"]: survivors[survivor["id"]]}
                    target = out_dir / "mutants" / f"{survivor['id']}.patch"
                    target.parent.mkdir(exist_ok=True)
                    target.write_text(unified_patch(base_files, mutated))
        finally:
            if sandbox is not None:
                sandbox.close()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, ImportError) as exc:
        report = head | {
            "task_id": package.task_id,
            "status": "budget_exhausted" if isinstance(exc, SpendCapReached) else "unavailable",
            "reason": str(exc)[-1_000:], "verdict": "unknown",
            "severity": None, "grades": [
                {"label": g.label, "reward": g.reward, "status": g.status, "seconds": round(g.seconds, 1)} for g in grades
            ],
        }  # fmt: skip
    report["finished_at"] = _now()
    if environment == "modal":
        assert spend_gate is not None
        report["execution"] = (
            modal_sandbox.evidence() if modal_sandbox is not None else {"events": []}
        )
        report["spend"] = spend_gate.to_json()
        report["reservation_bound"] = modal_execution_bound(
            max_grades=max_grades, mutants=mutants, separate=package.separate
        )
        (out_dir / "run-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "evallab.ref_subset.run/v1",
                    "backend": environment,
                    "tasks": [package.task_id],
                    "spend": report["spend"],
                    "reservation_policy": "estimates include unused native retry attempts; not a provider billing hard kill",
                    "finished_at": report["finished_at"],
                },
                indent=1,
                sort_keys=True,
            )
            + "\n"
        )
    (out_dir / "grades.log").write_text(
        "".join(
            f"== {g.label} reward={g.reward} rc={g.rc} {g.status} {g.seconds:.1f}s\n{g.tail}\n"
            for g in grades
        )
    )
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    (out_dir / "finding.json").write_text(
        json.dumps(finding(report, out_dir), indent=1, sort_keys=True) + "\n"
    )
    if "mutants" in report:
        (out_dir / "finding-mutants.json").write_text(
            json.dumps(mutants_finding(report, out_dir), indent=1, sort_keys=True) + "\n"
        )
    return report


def _package_digest(path: Path) -> str | None:
    try:
        from evallab.registry import task_directory_digest

        return task_directory_digest(path)
    except (ImportError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Sweep over a reference-fix index


@dataclass(frozen=True)
class Job:
    task_id: str
    package: Path
    patch: Path | None
    image: str


def load_jobs(index: Path, tasks_root: Path, order: Sequence[str] = ()) -> list[Job]:
    jobs: list[Job] = []
    for row in csv.DictReader(index.open()):
        if row.get("label") != "oracle:pass+nop:fail":
            continue
        package = tasks_root / row["task_id"]
        try:
            loaded = Package.load(package)
            image, separate = loaded.image, loaded.separate
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            image, separate = "", False
        patch = Path(row["patch_path"]) if row.get("patch_path") else None
        if patch is not None or separate:
            jobs.append(Job(row["task_id"], package, None if separate else patch, image))
    rank = {task: i for i, task in enumerate(order)}
    return sorted(jobs, key=lambda job: (rank.get(job.task_id, len(rank)), job.task_id))


def patch_code_blocks(patch: str, tests: Iterable[str] = ()) -> int:
    """Code change runs in a patch (docs, comments and reset test files excluded); no Docker."""
    skip = set(tests)
    count, path = 0, ""
    removed: list[str] = []
    added: list[str] = []

    def close() -> None:
        nonlocal count
        if (removed or added) and block_kind(path, removed, added, skip) == "code":
            count += 1
        removed.clear()
        added.clear()

    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            close()
            match = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            path = match.group(2) if match else ""
        elif raw.startswith(("--- ", "+++ ")):
            continue
        elif raw.startswith("-"):
            removed.append(raw[1:])
        elif raw.startswith("+"):
            added.append(raw[1:])
        elif not raw.startswith("\\"):
            close()
    close()
    return count


class ImageCache:
    """Pull ahead of the lanes; remove an image this process pulled once its last job ends."""

    def __init__(self, jobs: Sequence[Job], ahead: int) -> None:
        self.images = list(dict.fromkeys(job.image for job in jobs))
        self.users = {image: sum(1 for job in jobs if job.image == image) for image in self.images}
        self.slots = threading.Semaphore(ahead)
        self.ready = {image: threading.Event() for image in self.images}
        self.errors: dict[str, str] = {}
        self.pulled: set[str] = set()
        self.lock = threading.Lock()

    def prefetch(self) -> None:
        for image in self.images:
            self.slots.acquire()
            if _docker("image", "inspect", image).returncode != 0:
                result = _docker("pull", image)
                if result.returncode != 0:
                    self.errors[image] = result.stderr.decode(errors="replace")[-400:]
                else:
                    with self.lock:
                        self.pulled.add(image)
            self.ready[image].set()

    def wait(self, image: str) -> str | None:
        self.ready[image].wait()
        return self.errors.get(image)

    def release(self, image: str) -> None:
        with self.lock:
            self.users[image] -= 1
            last = self.users[image] == 0
            mine = image in self.pulled
        if last:
            if mine:
                _docker("rmi", image)
            self.slots.release()


def sweep(
    jobs: list[Job],
    out_root: Path,
    *,
    lanes: int,
    ahead: int,
    max_grades: int,
    memory: str,
    mutants: int = 0,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Probe each job (resumable). Without the line-level pass, tasks with fewer than two code
    blocks skip Docker; with it, a one-block fix is exactly where it looks."""
    _check_environment(environment, spend_gate, lanes=lanes)
    if environment == "modal" and mutants < 0:
        raise ValueError("Modal mutant count must be nonnegative")
    todo = [job for job in jobs if not (out_root / job.task_id / "report.json").is_file()]
    log(
        f"ref-subset sweep: {len(jobs)} tasks, {len(jobs) - len(todo)} already reported, {len(todo)} to run"
    )
    execution_jobs: list[Job] = []
    reports: list[dict[str, Any]] = []
    for job in todo:
        out_dir = out_root / job.task_id
        try:
            package = Package.load(job.package)
            if not package.separate and job.patch is None:
                raise ValueError("a raw MiMo package needs a reference patch")
            blocks = (
                patch_code_blocks(job.patch.read_text(errors="replace"), package.tests)
                if job.patch is not None and not package.separate
                else None
            )
        except (OSError, ValueError) as exc:
            reports.append(
                _write_skipped(
                    job.task_id,
                    job.package,
                    out_dir,
                    "unavailable",
                    str(exc),
                    environment=environment,
                    spend_gate=spend_gate,
                )
            )
            continue
        if blocks is not None and blocks < 2 and not mutants:
            reason = f"{blocks} code change run(s) in the reference patch: no proper subset to try"
            reports.append(
                _write_skipped(
                    job.task_id,
                    job.package,
                    out_dir,
                    "not_applicable",
                    reason,
                    environment=environment,
                    spend_gate=spend_gate,
                )
            )
        else:
            execution_jobs.append(job)
    log(
        f"ref-subset sweep: {len(execution_jobs)} need {environment}, {len(reports)} recorded without it"
    )
    cache = None
    if environment == "docker":
        cache = ImageCache([job for job in execution_jobs if job.image], lanes + ahead)
        threading.Thread(target=cache.prefetch, daemon=True).start()

    def one(job: Job) -> dict[str, Any]:
        out_dir = out_root / job.task_id
        error = cache.wait(job.image) if cache is not None and job.image else None
        try:
            if error:
                return _write_skipped(
                    job.task_id, job.package, out_dir, "unavailable", f"image pull failed: {error}"
                )
            report = probe_task(
                job.package, job.patch, out_dir, max_grades=max_grades, memory=memory,
                mutants=mutants, environment=environment, spend_gate=spend_gate, log=log,
            )  # fmt: skip
            log(
                f"{job.task_id}: {report['status']} verdict={report['verdict']} kept={report.get('kept_fraction')}"
            )
            return report
        finally:
            if cache is not None and job.image:
                cache.release(job.image)

    with ThreadPoolExecutor(max_workers=lanes) as pool:
        reports += list(pool.map(one, execution_jobs))
    if environment == "modal":
        assert spend_gate is not None
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "run-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "evallab.ref_subset.run/v1",
                    "backend": environment,
                    "tasks": [job.task_id for job in jobs],
                    "lanes": lanes,
                    "spend": spend_gate.to_json(),
                    "reservation_policy": "estimates include unused native retry attempts; not a provider billing hard kill",
                    "finished_at": _now(),
                },
                indent=1,
                sort_keys=True,
            )
            + "\n"
        )
    return reports


def _write_skipped(
    task_id: str,
    package: Path,
    out_dir: Path,
    status: Status,
    reason: str,
    *,
    environment: str = "docker",
    spend_gate: CloudSpendGate | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": SCHEMA_REPORT, "task_id": task_id, "package": str(package), "status": status,
        "reason": reason, "verdict": "unknown", "severity": None, "finished_at": _now(),
        "backend": environment, "package_digest": _package_digest(package),
    }  # fmt: skip
    if spend_gate is not None:
        report["spend"] = spend_gate.to_json()
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    (out_dir / "finding.json").write_text(
        json.dumps(finding(report, out_dir), indent=1, sort_keys=True) + "\n"
    )
    return report


def collect(out_root: Path) -> dict[str, Any]:
    reports = [json.loads(path.read_text()) for path in sorted(out_root.glob("*/report.json"))]
    status: dict[str, int] = {}
    severity: dict[str, int] = {}
    for report in reports:
        status[report["status"]] = status.get(report["status"], 0) + 1
        if report["verdict"] == "fail":
            severity[report["severity"]] = severity.get(report["severity"], 0) + 1
    findings = [
        json.loads(path.read_text())
        for report in sorted(out_root.glob("*/report.json"))
        for path in (report.parent / "finding.json", report.parent / "finding-mutants.json")
        if path.is_file()
    ]
    (out_root / "findings.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in findings)
    )
    summary = {
        "schema": "evallab.ref_subset.summary/v1",
        "tasks": len(reports),
        "status": status,
        "verdict": {
            v: sum(1 for r in reports if r["verdict"] == v) for v in ("pass", "fail", "unknown")
        },
        "weak_by_severity": severity,
        "weak": sorted(
            (
                {
                    "task_id": r["task_id"],
                    "severity": r["severity"],
                    "kept_fraction": r["kept_fraction"],
                    "kept_code_lines": r["kept_code_lines"],
                    "code_lines": r["code_lines"],
                }
                for r in reports
                if r["verdict"] == "fail"
            ),
            key=lambda item: (item["kept_fraction"], item["task_id"]),
        ),  # fmt: skip
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    return summary


# ---------------------------------------------------------------------------
# QA Trace T0: does a passing trial skip the reference blocks no test checks?


def edited_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Base-file line ranges (0-based, half-open) a unified diff edits, per path."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    line = 0
    start: int | None = None

    def flush() -> None:
        nonlocal start
        if path is not None and start is not None:
            ranges.setdefault(path, []).append((start, max(line, start + 1)))
        start = None

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            match = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            path = match.group(2) if match else None
            continue
        if path is None or raw.startswith(("--- ", "+++ ", "\\")):
            continue
        header = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", raw)
        if header:
            flush()
            line = int(header.group(1)) - (0 if header.group(2) == "0" else 1)
            continue
        if raw.startswith(("-", "+")):
            if start is None:
                start = line
            if raw.startswith("-"):
                line += 1
            continue
        flush()
        line += 1
    flush()
    return ranges


def _touches(block: dict[str, Any], ranges: dict[str, list[tuple[int, int]]]) -> bool:
    spans = ranges.get(block["path"])
    if not spans:
        return False
    start, end = block["base_lines"]
    end = max(end, start + 1)
    return any(lo < end + TRACE_SLACK and hi > start - TRACE_SLACK for lo, hi in spans)


def trace_check(trial_dir: Path, report: dict[str, Any] | None) -> dict[str, Any]:
    """One ``qa.finding/v1`` for a trial: is its pass a partial fix the tests cannot see?"""
    result = json.loads((trial_dir / "result.json").read_text())
    task_id = str(result.get("task_name") or "").split("/")[-1]
    reward_file = trial_dir / "verifier" / "reward.txt"
    reward = reward_file.read_text().strip() if reward_file.is_file() else None
    diff_file = trial_dir / "verifier" / "agent.diff"
    base = {
        "schema": SCHEMA_FINDING,
        "subject": {
            "kind": "trace",
            "task_id": task_id,
            "task_digest": None,
            "trial_id": trial_dir.name,
        },
        "stage": STAGE_TRACE,
        "category": "false_positive",
        "fault": None,
        "severity": None,
        "reproducer": None,
        "reviewer": REVIEWER,
    }
    unknown = base | {
        "verdict": "unknown",
        "refutation": {"status": "unverified", "by": None, "reason": ""},
    }
    if reward != "1":
        return unknown | {
            "summary": f"not a pass (reward {reward}); this check judges passes only",
            "evidence_refs": [],
        }
    if report is None or report.get("verdict") not in {"pass", "fail"}:
        status = (
            "no reference-subset report"
            if report is None
            else f"task report {report.get('status')}"
        )
        return unknown | {"summary": f"{status} for {task_id}", "evidence_refs": []}
    if not diff_file.is_file():
        return unknown | {"summary": "trial has no verifier/agent.diff", "evidence_refs": []}
    ranges = edited_ranges(diff_file.read_text(errors="replace"))
    code = [block for block in report["blocks"] if block["kind"] == "code"]
    untested = [block for block in code if block["id"] in set(report["dropped_code"])]
    tested = [block for block in code if block["id"] not in set(report["dropped_code"])]
    covered_tested = [block["id"] for block in tested if _touches(block, ranges)]
    covered_untested = [block["id"] for block in untested if _touches(block, ranges)]
    evidence = [str(diff_file), str(report.get("report_path", ""))]
    metrics = {
        "tested_blocks": len(tested),
        "tested_touched": len(covered_tested),
        "untested_blocks": len(untested),
        "untested_touched": len(covered_untested),
        "untested_lines": sum(block["removed"] + block["added"] for block in untested),
    }
    if not untested:
        return base | {
            "verdict": "pass", "summary": "every reference block is needed by the hidden tests",
            "evidence_refs": evidence, "metrics": metrics,
            "refutation": {"status": "unverified", "by": None, "reason": ""},
        }  # fmt: skip
    suspect = not covered_untested
    lines = metrics["untested_lines"]
    severity = report.get("severity") if suspect else None
    summary = (
        f"pass leaves all {len(untested)} untested reference blocks ({lines} lines) unchanged: "
        "a partial fix would score the same"
        if suspect
        else f"diff touches {len(covered_untested)}/{len(untested)} untested reference blocks"
    )
    return base | {
        "verdict": "fail" if suspect else "pass",
        "severity": severity,
        "fault": "eval" if suspect else None,
        "summary": summary,
        "evidence_refs": evidence,
        "metrics": metrics,
        "refutation": {
            "status": "unverified",
            "by": None,
            "reason": "the agent may implement the untested behaviour elsewhere or the behaviour may be optional; "
            "compare the diff with dropped.patch",
        },
    }


# ---------------------------------------------------------------------------
# CLI


def _reservation_estimate(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("reservation estimate must be positive and finite")
    return number


def _spend_cap(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("USD cap must be nonnegative and finite")
    return number


def _add_environment_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--environment", choices=("docker", "modal"), default="docker")
    parser.add_argument("--plan", action="store_true", help="$0 local plan; construct no backend")
    parser.add_argument(
        "--max-usd", type=_spend_cap, help="Modal reservation cap, not a provider billing hard kill"
    )
    parser.add_argument(
        "--approve", help="Recorded approval text; required for real Modal execution"
    )
    parser.add_argument(
        "--usd-per-execution",
        type=_reservation_estimate,
        default=MODAL_USD_PER_ALLOCATION_UNIT,
        help="Positive finite reservation estimate per image work/native creation attempt "
        "(default: %(default)s)",
    )


def _command_spend_gate(args: argparse.Namespace) -> CloudSpendGate | None:
    maximum = 16 if args.environment == "modal" else 2
    if not 1 <= getattr(args, "lanes", 1) <= maximum:
        raise ValueError(f"{args.environment} ref-subset lanes must be between 1 and {maximum}")
    if args.environment == "docker":
        return None
    if args.mutants < 0:
        raise ValueError("Modal mutant count must be nonnegative")
    if not args.approve or not args.approve.strip() or args.max_usd is None:
        raise ValueError("real Modal execution requires --approve TEXT and --max-usd USD")
    return CloudSpendGate(args.approve, args.max_usd, args.usd_per_execution)


def _plan_command(args: argparse.Namespace, jobs: list[Job], *, sweep_mode: bool) -> int:
    """Read only local package/index/report bytes. No runtime/backend construction."""
    tasks: list[dict[str, Any]] = []
    refusals: list[str] = []
    maximum = 16 if args.environment == "modal" else 2
    lanes = getattr(args, "lanes", 1)
    if not 1 <= lanes <= maximum:
        refusals.append(f"{args.environment} lanes must be between 1 and {maximum}")
    if args.environment == "modal" and args.mutants < 0:
        refusals.append("Modal mutant count must be nonnegative")
    for job in jobs:
        item: dict[str, Any] = {
            "task_id": job.task_id,
            "package": str(job.package),
            "package_digest": _package_digest(job.package),
            "reservation_units_upper_bound": 0,
        }
        try:
            shared = _shared_config(job.package)
            if shared is not None:
                item["package_type"] = "native-shared"
                if sweep_mode:
                    raise UnsupportedTask("native shared tasks use run with per-task --target files, not the legacy index sweep")
                if job.patch is not None or args.environment != "docker":
                    raise UnsupportedTask("native shared tasks require Docker and their own oracle, not --patch")
                item["targets"] = _shared_targets(job.package, shared, getattr(args, "target", ()))
                if args.max_grades < 4 or args.mutants < 0:
                    raise UnsupportedTask("native ref-subset needs max_grades >= 4 and mutants >= 0")
                item.update(
                    reference={"oracle": "solution/solve.sh"},
                    action="probe", matrix_workers=1, shared_docker_slots=2,
                    effective_agent_timeout_sec=int(shared["agent"]["timeout_sec"]),
                    declared_agent_timeout_sec=shared["agent"]["timeout_sec"],
                    agent_timeout_multiplier=1.0,
                    requested_legacy_limits={"memory": args.memory, "oracle_timeout_s": args.oracle_timeout},
                    candidate_state="fresh base; selected files only; no reference replay",
                    required_controls={"oracle": 1, "nop": 0, "full": 1},
                    resources=(shared.get("environment") or {}),
                    live_resource_eligibility="unmeasured; checked under shared admission before each native matrix",
                    mutation_seed=(
                        str((shared.get("task") or {}).get("name") or job.package.name)
                        if getattr(args, "seed", None) is None else args.seed
                    ),
                )
                tasks.append(item)
                continue
            if getattr(args, "target", ()):
                raise UnsupportedTask("--target is only supported for native shared-verifier tasks")
            package = Package.load(job.package)
            item["package_type"] = "separate-verifier" if package.separate else "raw-mimo"
            if package.separate == (job.patch is not None):
                raise ValueError(
                    "separate-verifier reference comes from its oracle, not --patch"
                    if package.separate
                    else "raw MiMo package requires --patch"
                )
            if job.patch is not None:
                raw = job.patch.read_bytes()
                item["reference"] = {
                    "path": str(job.patch),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            else:
                item["reference"] = {"oracle": "solution/solve.sh"}
            if args.environment == "modal":
                problem = modal_resource_blocker(job.package)
                if problem is not None:
                    raise ValueError(problem)
            resumed = sweep_mode and (args.output_dir / job.task_id / "report.json").is_file()
            blocks = (
                patch_code_blocks(raw.decode(errors="replace"), package.tests)
                if sweep_mode and job.patch is not None
                else None
            )
            skip = resumed or (blocks is not None and blocks < 2 and not args.mutants)
            item["action"] = "resume_skip" if resumed else "not_applicable" if skip else "probe"
            if not skip and args.environment == "modal":
                item["reservation_units_upper_bound"] = modal_execution_bound(
                    max_grades=args.max_grades, mutants=args.mutants, separate=package.separate
                )
        except (OSError, ValueError) as exc:
            item.update(action="refused", reason=str(exc))
            refusals.append(f"{job.task_id}: {exc}")
        tasks.append(item)
    units = sum(item["reservation_units_upper_bound"] for item in tasks)
    estimate = units * args.usd_per_execution
    fits = args.max_usd is None or estimate <= args.max_usd + 1e-9
    if args.environment == "modal":
        if args.max_usd is None:
            refusals.append("real Modal execution requires --max-usd")
        elif not fits:
            refusals.append(
                "conservative reservation estimate exceeds --max-usd; execution would stop at the gate"
            )
        if not args.approve or not args.approve.strip():
            refusals.append("real Modal execution requires recorded --approve text")
    plan = {
        "schema": "evallab.ref_subset.plan/v1",
        "backend": args.environment,
        "plan_only": True,
        "plan_cost_usd": 0.0,
        "executed": False,
        "lanes": lanes,
        "tasks": tasks,
        "reservation_units_upper_bound": units,
        "usd_per_execution": args.usd_per_execution,
        "estimated_reservation_usd": estimate,
        "max_usd": args.max_usd,
        "fits_cap": fits if args.max_usd is not None else None,
        "approval": args.approve,
        "execution_refused": bool(refusals),
        "refusals": refusals,
        "reservation_policy": "includes image work and both potential native creation attempts; estimates, not a provider billing hard kill",
    }
    print(json.dumps(plan, indent=1, sort_keys=True))
    return 0


def _run_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    if args.plan:
        return _plan_command(
            args, [Job(args.package.name, args.package, args.patch, "")], sweep_mode=False
        )
    try:
        spend_gate = _command_spend_gate(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = probe_task(
        args.package,
        args.patch,
        args.output_dir,
        max_grades=args.max_grades,
        memory=args.memory,
        oracle_timeout=args.oracle_timeout,
        mutants=args.mutants,
        environment=args.environment,
        spend_gate=spend_gate,
        targets=args.target,
        repo_root=root,
        mutation_seed=args.seed,
    )
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in ("task_id", "status", "verdict", "severity", "kept_fraction", "reason")
            }
        )
    )
    return int(
        report["status"] == "unavailable"
        or (args.environment == "modal" and report["status"] == "budget_exhausted")
    )


def _sweep_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del root, harbor
    order = json.loads(args.order.read_text()) if args.order else ()
    jobs = load_jobs(args.index, args.tasks_root, order)
    if args.task:
        jobs = [job for job in jobs if job.task_id in set(args.task)]
    if args.limit:
        jobs = jobs[: args.limit]
    if args.plan:
        return _plan_command(args, jobs, sweep_mode=True)
    try:
        spend_gate = _command_spend_gate(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    reports = sweep(
        jobs,
        args.output_dir,
        lanes=args.lanes,
        ahead=args.ahead,
        max_grades=args.max_grades,
        memory=args.memory,
        mutants=args.mutants,
        environment=args.environment,
        spend_gate=spend_gate,
    )
    print(json.dumps(collect(args.output_dir), indent=1))
    return int(
        args.environment == "modal"
        and any(report["status"] in {"unavailable", "budget_exhausted"} for report in reports)
    )


def _trace_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del root, harbor
    out: list[dict[str, Any]] = []
    for trial in args.trial:
        result = json.loads((trial / "result.json").read_text())
        task_id = str(result.get("task_name") or "").split("/")[-1]
        report_path = args.reports / task_id / "report.json"
        report = (
            json.loads(report_path.read_text()) | {"report_path": str(report_path)}
            if report_path.is_file()
            else None
        )
        out.append(trace_check(trial, report))
    text = "".join(json.dumps(item, sort_keys=True) + "\n" for item in out)
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def add_ref_subset_parser(hack_commands: Any) -> None:
    parser = hack_commands.add_parser(
        "ref-subset",
        help="Weak-test probe: the smallest part of a reference fix that still passes the hidden "
        "tests (Docker by default; explicitly approved Modal)",
    )
    commands = parser.add_subparsers(dest="ref_subset_command", required=True)
    run = commands.add_parser(
        "run",
        help="Probe raw MiMo against --patch, separate-verifier oracle artifacts, or an unchanged "
        "shared Harbor task through native matrices with --target files",
    )
    run.add_argument("package", type=Path)
    run.add_argument("--patch", type=Path, help="Reference patch (MiMo packages only)")
    run.add_argument("--target", action="append", default=[], help="Absolute native shared target FILE; repeat per file")
    run.add_argument("--seed", help="Deterministic line-mutant selection seed (default: task id)")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--max-grades", type=int, default=DEFAULT_MAX_GRADES)
    run.add_argument("--memory", default="4g")
    run.add_argument(
        "--mutants",
        type=int,
        default=0,
        help="Line-level pass: grade up to N one-edit mutants of the lines the reference wrote",
    )
    run.add_argument(
        "--oracle-timeout",
        type=float,
        default=3_600,
        help="Seconds for solution/solve.sh; also lowers Modal image/setup timeout below 1800s",
    )
    _add_environment_options(run)
    run.set_defaults(func=_run_command)
    many = commands.add_parser(
        "sweep", help="Probe every oracle:pass+nop:fail row of a reference-fix index"
    )
    many.add_argument("--index", type=Path, required=True)
    many.add_argument(
        "--tasks-root",
        type=Path,
        required=True,
        help="Directory holding the raw packages by task id",
    )
    many.add_argument(
        "--output-dir", type=Path, required=True, help="Resumable: tasks with a report are skipped"
    )
    many.add_argument("--order", type=Path, help="JSON list of task ids to run first, in order")
    many.add_argument("--task", action="append", default=[])
    many.add_argument("--limit", type=int)
    many.add_argument(
        "--lanes", type=int, default=1, choices=range(1, 17), help="Docker: 1-2; Modal: 1-16"
    )
    many.add_argument("--ahead", type=int, default=1, help="Images pulled ahead of the lanes")
    many.add_argument("--max-grades", type=int, default=DEFAULT_MAX_GRADES)
    many.add_argument("--memory", default="4g")
    many.add_argument("--mutants", type=int, default=0, help="Line-level pass per task (see run)")
    _add_environment_options(many)
    many.set_defaults(func=_sweep_command)
    trace = commands.add_parser(
        "trace", help="QA Trace T0: flag passes that skip the untested reference blocks"
    )
    trace.add_argument("trial", type=Path, nargs="+")
    trace.add_argument("--reports", type=Path, required=True, help="A sweep output directory")
    trace.add_argument("--output", type=Path)
    trace.set_defaults(func=_trace_command)
