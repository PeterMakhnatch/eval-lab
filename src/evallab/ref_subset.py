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

Grading defaults to local Docker; Modal is explicit and spend-gated. Each grade
starts fresh from a snapshot taken after task setup, so build output never
carries between grades and setup runs once per task. Raw MiMo code packages
grade their working tree in place. Separate-verifier Harbor packages derive
the reference from their oracle and upload only artifacts into fresh verifiers.
``--plan`` inspects local package bytes without constructing either backend.

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
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab.execution_contracts import MODAL_PATH_PIN
from evallab.regrade import CloudSpendGate, SpendCapReached, lab_harbor_problem
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


def unified_patch(before: dict[str, bytes | None], after: dict[str, bytes | None]) -> str:
    """A ``git apply``-able patch from ``before`` to ``after`` (git's own diff)."""
    with tempfile.TemporaryDirectory(prefix="ref-subset-diff-") as tmp:
        root = Path(tmp)
        git = ["git", "-c", "user.name=ref-subset", "-c", "user.email=ref-subset@localhost"]
        subprocess.run([*git, "init", "-q"], cwd=root, check=True)
        for state in (before, after):
            for path, data in state.items():
                target = root / path
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
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


def _modal_resources_problem(path: Path) -> str | None:
    config = tomllib.loads((path / "task.toml").read_text())
    environment = config.get("environment") or {}
    verifier_environment = (config.get("verifier") or {}).get("environment") or {}
    for role, values in (("agent", environment), ("verifier", verifier_environment)):
        if values.get("gpus") or values.get("tpu"):
            return f"{role} GPU/TPU Modal resources have no approved price estimate"
    return None


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
        "evidence_refs": [str(out_dir / "report.json"), *(str(out_dir / "mutants" / f"{s['id']}.patch") for s in survivors)],
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
        for name in ("minimal.patch", "dropped.patch")
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
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Probe one task and write report.json, finding.json and the patches to ``out_dir``.

    A MiMo package needs ``patch_path``; a separate-verifier package takes its reference from
    its own oracle and records it as ``reference.patch`` (over the artifacts, relative to ``/``).
    ``mutants`` > 0 adds the line-level pass (:func:`mutation_pass`): ``report["mutants"]``,
    ``finding-mutants.json`` and one reproducer per survivor in ``mutants/``.
    """
    _check_environment(environment, spend_gate)
    if environment == "modal" and mutants < 0:
        raise ValueError("Modal mutant count must be nonnegative")
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
            problem = _modal_resources_problem(package_dir)
            if problem is not None:
                raise RuntimeError(problem)
            from evallab.ref_subset_modal import ModalSandbox

            sandbox = ModalSandbox(
                package,
                spend_gate=spend_gate,
                out_dir=out_dir / "native",
                memory=memory,
                oracle_timeout=oracle_timeout,
                grade_timeout=grade_timeout,
                build_timeout=min(1_800, oracle_timeout),
            )
        else:
            sandbox = (
                SeparateSandbox(package, memory=memory, oracle_timeout=oracle_timeout)
                if package.separate
                else Sandbox(package, memory=memory)
            )
            head["image"] = sandbox.image if package.separate else package.image
            if package.gpus:
                raise RuntimeError(f"the task needs {package.gpus} GPU(s); local Docker has none")
        try:
            sandbox.prepare()
            if package.separate:
                base, fixed = sandbox.reference()
                paths = list(base)
                patch = unified_patch(base, fixed)
                (out_dir / "reference.patch").write_text(patch)
                head["reference"]["sha256"] = hashlib.sha256(patch.encode()).hexdigest()
            else:
                paths = patch_paths(patch)
                base = sandbox.read(paths)
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
                    changes, grade_files, grades, limit=mutants, seed=package.task_id,
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
        report["execution"] = sandbox.evidence() if sandbox is not None else {"events": []}
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
        default=0.05,
        help="Positive finite reservation estimate per image work/native creation attempt (default: 0.05)",
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
    """Read only local package/index/report bytes. No runtime/backend imports."""
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
                problem = _modal_resources_problem(job.package)
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
    estimate = round(units * args.usd_per_execution, 4)
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
    del root, harbor
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
        help="Probe one task: a raw MiMo package against --patch, or a Harbor separate-verifier "
        "task against its own oracle's artifacts",
    )
    run.add_argument("package", type=Path)
    run.add_argument("--patch", type=Path, help="Reference patch (MiMo packages only)")
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
