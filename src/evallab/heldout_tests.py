"""Held-out upstream-test extraction for HAR-197 regrade (offline, CPU-only).

A held-out suite captures the *additional* upstream tests a fix commit adds, so
a later regrade can overlay those tests onto an agent patch applied at the task
base and score a separate numeric ``holdout_pass``. Original trial evidence and
rewards are never touched here; this module only reads a local Git directory
and a hidden-patch file, and writes one strict JSON suite document.

Pipeline (local, no repository code is executed)::

    fix_parent -> fix_commit      candidate added/modified Python test defs
    base_commit + hidden_patch    hidden postimage; base/hidden fingerprint sets
    candidates - hidden - base    novel held-out nodes + test/support overlays

Source-immutability contract: only read-only Git commands (``rev-parse``,
``rev-list``, ``cat-file``, ``diff-tree``, ``ls-tree``) run against the source
``git_dir``. Every invocation passes ``--no-replace-objects`` and
``-c protocol.allow=never``, and runs with ``GIT_CONFIG_NOSYSTEM=1``,
``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_SYSTEM=/dev/null`` and
``GIT_OPTIONAL_LOCKS=0`` so inherited config, replacement objects, transports
(including lazy fetch, which then fails closed) and lock files cannot reach or
alter the source repository. The hidden patch is applied in memory to blob
bytes from ``cat-file``; Git's writable index and object store are never used.

Test/support heuristic: a changed path is test/support when its basename is
``conftest.py``, matches ``test_*.py`` / ``*_test.py``, or lives under a
``test``/``tests``/``testing``/``fixtures``/``fixture``/``testdata``/
``test-data``/``test_data`` directory. Everything else is production code and
is never exported (skipped paths are counted in ``notes``). Collection
heuristic: module-level ``test*`` functions plus ``test*`` methods of classes
named ``Test*`` or subclassing something named ``*TestCase``.

Normalization and its limits: a test fingerprint hashes normalized AST
(decorators, signature, returns annotation, body minus docstring; comments
never reach the AST; def/class names are dropped so renames and
comment/docstring-only copies subtract as duplicates). Decorator, parameter
and body changes stay significant. Exact-AST novelty is **not** proof of
behavioral or semantic independence -- a renamed test with identical
normalized AST, or a body change no stronger than the hidden verifier, can
still be coupled to leaked behavior. Suites qualify their method in ``notes``
and must be gated by later functional controls; consumers must not read
``ready`` as independence.

``HeldoutSuite.model_dump(mode='json')`` is the runtime wire format. Blob
bytes are hashed and base64-encoded as bytes, never re-encoded text.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.benchmark_program_contracts import (
    compute_sha256,
    validate_safe_relative_path,
)
from evallab.schemas import ContractModel

__all__ = [
    "SCHEMA_VERSION",
    "ZERO_PARENT",
    "HeldoutExclusion",
    "HeldoutFile",
    "HeldoutSuite",
    "HeldoutTest",
    "build_parser",
    "extract_suite",
    "load_suite",
    "main",
    "validate_suite_path",
    "write_suite",
]


#: Wire schema marker for every suite document this module writes.
SCHEMA_VERSION = "heldout-suite/v1"

#: Sentinel ``fix_parent`` for suites whose single first parent is unknown
#: (missing commit) or ambiguous (merge with several parents). It is only
#: ever emitted with ``status == "unavailable"`` and an explicit refusal.
ZERO_PARENT = "0" * 40

_FULL_SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUALNAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"

#: Directory names that mark a path as test/support context.
_SUPPORT_DIR_NAMES = frozenset(
    {
        "test",
        "tests",
        "testing",
        "fixtures",
        "fixture",
        "testdata",
        "test-data",
        "test_data",
    }
)

#: Git commands this module may run against the source repository. Anything
#: else is refused internally, so extraction cannot fetch, mutate or execute.
_READONLY_GIT_COMMANDS = frozenset(
    {"rev-parse", "rev-list", "cat-file", "diff-tree", "ls-tree"}
)

_GIT_TIMEOUT_SECONDS = 120


def validate_suite_path(path_str: str) -> str:
    """Validate a repository-relative suite path, reusing the lab convention.

    Starts from ``validate_safe_relative_path`` (relative, POSIX, no
    traversal/backslash/redundant slashes) and adds the held-out scope rules:
    canonical spelling, no ``.git`` component, no ``:`` (it would break
    ``path::qualname`` node IDs), no control characters, and no leading ``-``
    (flag injection when node IDs reach a test command).
    """
    if not isinstance(path_str, str) or not path_str:
        raise ValueError("suite path must be a non-empty string")
    clean = validate_safe_relative_path(path_str)
    if clean != path_str:
        raise ValueError(f"suite path is not in canonical form: {path_str!r}")
    parts = PurePosixPath(clean).parts
    if any(part == ".git" for part in parts):
        raise ValueError(f"suite path must not enter .git: {path_str!r}")
    if ":" in clean:
        raise ValueError(f"suite path must not contain ':': {path_str!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in clean):
        raise ValueError(f"suite path must not contain control chars: {path_str!r}")
    if parts[0].startswith("-"):
        raise ValueError(f"suite path must not start with '-': {path_str!r}")
    return clean


class HeldoutTest(ContractModel):
    """One selected held-out test node."""

    node_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    qualname: str = Field(min_length=1, pattern=_QUALNAME_PATTERN)
    fingerprint: str = Field(pattern=_SHA256_PATTERN)
    change: Literal["added", "modified"]

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_suite_path(value)


class HeldoutFile(ContractModel):
    """One authoritative test/support overlay payload.

    Present files carry a git-style mode plus exact content bytes as base64
    with their SHA-256 digest. Deletions null all three fields.
    """

    path: str = Field(min_length=1)
    mode: Literal["100644", "100755"] | None = None
    content_base64: str | None = None
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_suite_path(value)

    @model_validator(mode="after")
    def _check_payload_terms(self) -> HeldoutFile:
        present = (
            self.mode is not None,
            self.content_base64 is not None,
            self.sha256 is not None,
        )
        if any(present) and not all(present):
            raise ValueError(
                "file payload must set mode/content_base64/sha256 together, "
                "or null all three for a deletion"
            )
        if all(present):
            assert self.content_base64 is not None and self.sha256 is not None
            try:
                raw = base64.b64decode(self.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 is not valid base64") from exc
            if compute_sha256(raw) != self.sha256:
                raise ValueError("sha256 does not match content_base64 bytes")
        return self


class HeldoutExclusion(ContractModel):
    """One candidate test subtracted from the suite, with an explicit reason."""

    node_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class HeldoutSuite(ContractModel):
    """Complete held-out suite document; ``model_dump(mode='json')`` is wire."""

    schema_version: Literal["heldout-suite/v1"] = "heldout-suite/v1"
    task_name: str = Field(min_length=1)
    image: str = Field(min_length=1)
    workdir: str = Field(min_length=1)
    base_commit: str = Field(pattern=_FULL_SHA_PATTERN)
    fix_commit: str = Field(pattern=_FULL_SHA_PATTERN)
    fix_parent: str = Field(pattern=_FULL_SHA_PATTERN)
    hidden_patch_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["ready", "no_extra_tests", "unavailable"]
    tests: list[HeldoutTest] = Field(default_factory=list)
    files: list[HeldoutFile] = Field(default_factory=list)
    exclusions: list[HeldoutExclusion] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("task_name")
    @classmethod
    def _check_task_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_name must not be blank")
        return value

    @field_validator("image")
    @classmethod
    def _check_image(cls, value: str) -> str:
        if not re.search(r"@sha256:[0-9a-f]{64}(?![0-9a-f])", value):
            raise ValueError("image must pin a digest (@sha256:<64 hex>)")
        return value

    @field_validator("workdir")
    @classmethod
    def _check_workdir(cls, value: str) -> str:
        if "\\" in value or not value.startswith("/"):
            raise ValueError("workdir must be an absolute POSIX path")
        if any(part == ".." for part in PurePosixPath(value).parts):
            raise ValueError("workdir must not contain '..'")
        return value

    @model_validator(mode="after")
    def _check_status_terms(self) -> HeldoutSuite:
        if self.status == "ready":
            if not self.tests or not self.files:
                raise ValueError("ready suite requires selected tests and payload files")
            if self.refusals:
                raise ValueError("ready suite must not carry refusals")
        elif self.status == "no_extra_tests":
            if self.tests or self.files:
                raise ValueError("no_extra_tests suite must not carry tests or files")
            if self.refusals:
                raise ValueError(
                    "no_extra_tests is completed subtraction; refused input is unavailable"
                )
        else:
            if self.tests or self.files:
                raise ValueError("unavailable suite must not carry tests or files")
            if not self.refusals:
                raise ValueError("unavailable suite requires explicit refusals")
        node_ids = [item.node_id for item in self.tests]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("test node_id values must be unique")
        file_paths = [item.path for item in self.files]
        if len(set(file_paths)) != len(file_paths):
            raise ValueError("file path values must be unique")
        for item in self.tests:
            expected = item.path + "::" + item.qualname.replace(".", "::")
            if item.node_id != expected:
                raise ValueError(f"node_id {item.node_id!r} disagrees with path/qualname")
        return self


class _GitError(RuntimeError):
    """A read-only source-Git probe failed; callers turn this into refusals."""


class _PatchError(ValueError):
    """A hidden patch is missing, ambiguous or unsupported; becomes refusals."""


def _git_env() -> dict[str, str]:
    """Sanitized environment for read-only source-Git probes."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
    )
    return env


def _run_git(
    git_dir: Path, *args: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run one allow-listed read-only Git command against ``git_dir``."""
    if not args or args[0] not in _READONLY_GIT_COMMANDS:
        raise _GitError(f"refusing non-read-only git command: {args[:1]!r}")
    if input_bytes is not None and args[0] != "cat-file":
        raise _GitError("stdin is only supported for batched cat-file reads")
    cmd = [
        "git",
        "-c",
        "protocol.allow=never",
        "--no-replace-objects",
        "--git-dir",
        str(git_dir),
        *args,
    ]
    try:
        return subprocess.run(
            cmd,
            input=input_bytes,
            env=_git_env(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitError(f"git {' '.join(args)} did not complete: {exc}") from exc


def _git_stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode("utf-8", errors="replace").strip()[:300]


def _resolve_git_dir(git_dir: Path) -> Path | None:
    """Return the effective ``--git-dir`` for a bare dir or checkout root."""
    if (git_dir / "objects").is_dir() and (git_dir / "HEAD").is_file():
        return git_dir
    nested = git_dir / ".git"
    if nested.is_dir() and (nested / "objects").is_dir():
        return nested
    return None


def _rev_parse(git_dir: Path, arg: str) -> str | None:
    """Resolve ``arg`` to a full SHA, or ``None`` when unknown."""
    proc = _run_git(git_dir, "rev-parse", "--verify", arg)
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return None


def _read_blob(git_dir: Path, sha: str) -> bytes:
    """Read blob bytes for ``sha``; raises ``_GitError`` for anything else."""
    kind = _run_git(git_dir, "cat-file", "-t", sha)
    if kind.returncode != 0 or kind.stdout.strip() != b"blob":
        raise _GitError(f"object {sha} is not a blob")
    proc = _run_git(git_dir, "cat-file", "-p", sha)
    if proc.returncode != 0:
        raise _GitError(f"cannot read blob {sha}: {_git_stderr(proc)}")
    return proc.stdout


def _tree_entry(git_dir: Path, rev: str, path: str) -> tuple[str, str] | None:
    """Return ``(mode, sha)`` for ``path`` at ``rev``, or ``None`` if absent."""
    proc = _run_git(git_dir, "ls-tree", "-z", rev, "--", path)
    if proc.returncode != 0:
        raise _GitError(f"git ls-tree failed for {path!r}: {_git_stderr(proc)}")
    records = [chunk for chunk in proc.stdout.split(b"\0") if chunk]
    if not records:
        return None
    if len(records) != 1:
        raise _GitError(f"ambiguous tree entry for {path!r} at {rev}")
    head, tab, _name = records[0].partition(b"\t")
    if not tab:
        raise _GitError(f"unparseable ls-tree entry for {path!r}")
    try:
        mode, kind, sha = head.decode("ascii").split(" ")
    except ValueError as exc:
        raise _GitError(f"unparseable ls-tree entry for {path!r}") from exc
    if kind != "blob":
        raise _GitError(f"tree entry for {path!r} is {kind}, not a blob")
    return mode, sha


def _list_tree_py_shas(git_dir: Path, rev: str) -> dict[str, str]:
    """Map every test/support ``.py`` path at ``rev`` to its blob SHA."""
    proc = _run_git(git_dir, "ls-tree", "-r", "-z", rev, "--")
    if proc.returncode != 0:
        raise _GitError(f"git ls-tree -r failed: {_git_stderr(proc)}")
    found: dict[str, str] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        head, tab, name = record.partition(b"\t")
        if not tab:
            raise _GitError("unparseable ls-tree -r entry")
        try:
            _mode, kind, sha = head.decode("ascii").split(" ")
            path = name.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _GitError("unparseable ls-tree -r entry") from exc
        if kind != "blob" or not path.endswith(".py"):
            continue
        try:
            validate_suite_path(path)
        except ValueError:
            continue
        if _is_test_or_support(path):
            found[path] = sha
    return found


def _read_blobs_batch(git_dir: Path, shas: list[str]) -> dict[str, bytes]:
    """Read many blobs with one ``cat-file --batch`` probe (read-only)."""
    if not shas:
        return {}
    proc = _run_git(
        git_dir,
        "cat-file",
        "--batch",
        input_bytes="".join(f"{sha}\n" for sha in shas).encode("ascii"),
    )
    if proc.returncode != 0:
        raise _GitError(f"git cat-file --batch failed: {_git_stderr(proc)}")
    out: dict[str, bytes] = {}
    view = proc.stdout
    pos = 0
        try:
            end = view.index(b"\n", pos)
            header = view[pos:end].decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _GitError(f"batched cat-file desynced at {sha}") from exc
        pos = end + 1
        parts = header.split(" ")
        if len(parts) != 3 or parts[0] != sha:
            raise _GitError(f"batched cat-file desynced at {sha}")
        _found, kind, size_text = parts
        if kind == "missing":
            raise _GitError(f"batched cat-file missing object {sha}")
        if kind != "blob":
            raise _GitError(f"batched object {sha} is {kind}, not a blob")
        try:
            size = int(size_text)
        except ValueError as exc:
            raise _GitError(f"batched cat-file bad size for {sha}") from exc
        if size < 0 or pos + size > len(view):
            raise _GitError(f"batched cat-file truncated at {sha}")
        out[sha] = view[pos : pos + size]
        pos += size + 1
    return out

# --------------------------------------------------------------------------- #
# Minimal unified-patch handling (hidden postimage reconstruction only).
# --------------------------------------------------------------------------- #


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class _FilePatch:
    old_path: str | None
    new_path: str | None
    is_new: bool = False
    is_delete: bool = False
    is_rename: bool = False
    is_binary: bool = False
    hunks: list[_Hunk] = field(default_factory=list)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_ab_prefix(token: str) -> str:
    if token in ("/dev/null", "dev/null"):
        return "/dev/null"
    for prefix in ("a/", "b/"):
        if token.startswith(prefix):
            return token[2:]
    return token


def _unquote_git_path(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        try:
            return bytes(token[1:-1], "utf-8").decode("unicode_escape")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _PatchError(f"unparseable quoted patch path: {token!r}") from exc
    return token


def _split_git_path_token(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        return _unquote_git_path(raw)
    return raw.split("\t")[0].strip()


def _parse_unified_patch(text: str) -> list[_FilePatch]:
    """Parse a git unified diff into per-file patches.

    Only the constructs hidden test patches need are supported: new/delete/
    modify/rename with context hunks, plus ``no newline`` markers. Copies,
    binary diffs and malformed input raise ``_PatchError`` with an explicit
    reason instead of guessing.
    """
    if not text.strip():
        return []
    raw_lines = text.split("\n")
    # A trailing newline is framing, not an empty body line.
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    current_hunk: _Hunk | None = None
    headers_seen = False
    skipping_binary = False

    def _finish_file() -> None:
        nonlocal current, current_hunk, headers_seen, skipping_binary
        if current is None:
            return
        if not current.is_binary:
            if current.old_path is None or current.new_path is None:
                raise _PatchError("patch file entry is missing ---/+++ headers")
        files.append(current)
        current = None
        current_hunk = None
        headers_seen = False
        skipping_binary = False

    for lineno, line in enumerate(raw_lines, start=1):
        if line.startswith("diff --git "):
            _finish_file()
            try:
                tokens = shlex.split(line[len("diff --git "):])
            except ValueError as exc:
                raise _PatchError(f"line {lineno}: unparseable diff --git header") from exc
            if len(tokens) != 2:
                raise _PatchError(f"line {lineno}: diff --git header needs two paths")
            old_raw = _strip_ab_prefix(_unquote_git_path(tokens[0]))
            new_raw = _strip_ab_prefix(_unquote_git_path(tokens[1]))
            current = _FilePatch(
                old_path=None if old_raw == "/dev/null" else old_raw,
                new_path=None if new_raw == "/dev/null" else new_raw,
            )
            continue
        if current is None:
            if line.strip() == "":
                continue
            raise _PatchError(f"line {lineno}: content outside any diff --git entry")
        if skipping_binary:
            continue
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            current.is_binary = True
            skipping_binary = True
            continue
        if line.startswith("copy from ") or line.startswith("copy to "):
            raise _PatchError("patch copies are unsupported; expand them explicitly")
        if line.startswith("rename from "):
            current.is_rename = True
            current.old_path = _strip_ab_prefix(_split_git_path_token(line[len("rename from "):]))
            continue
        if line.startswith("rename to "):
            current.is_rename = True
            current.new_path = _strip_ab_prefix(_split_git_path_token(line[len("rename to "):]))
            continue
        if line.startswith("new file mode "):
            current.is_new = True
            continue
        if line.startswith("deleted file mode "):
            current.is_delete = True
            continue
        if line.startswith(
            ("old mode", "new mode", "index ", "similarity index ", "dissimilarity index ")
        ):
            continue
        if line.startswith("--- "):
            token = _split_git_path_token(line[4:])
            current.old_path = None if token == "/dev/null" else _strip_ab_prefix(token)
            if current.old_path is None:
                current.is_new = True
            continue
        if line.startswith("+++ "):
            token = _split_git_path_token(line[4:])
            current.new_path = None if token == "/dev/null" else _strip_ab_prefix(token)
            if current.new_path is None:
                current.is_delete = True
            headers_seen = True
            continue
        match = _HUNK_RE.match(line)
        if match is not None:
            if not headers_seen:
                raise _PatchError(f"line {lineno}: hunk before ---/+++ headers")
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current_hunk = _Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )
            current.hunks.append(current_hunk)
            continue
        if current_hunk is None:
            if line.strip() == "":
                continue
            raise _PatchError(f"line {lineno}: unexpected patch line outside a hunk")
        if line[:1] in (" ", "-", "+") or line == "" or line.startswith("\\"):
            current_hunk.lines.append(line)
            continue
        raise _PatchError(f"line {lineno}: invalid hunk body line")
    _finish_file()
    non_binary = [item for item in files if not item.is_binary]
    if not non_binary and text.strip():
        # A non-empty patch with no usable file entries (binary-only or
        # unrecognised) must not silently count as "no hidden tests".
        raise _PatchError("patch has no usable file diffs")
    return files


def _compose_postimage(base_text: str, hunks: list[_Hunk], path: str) -> str:
    """Apply hunks with strict context, count and trailing-newline checks.

    Unchanged regions between hunks are copied through; the final trailing
    newline is inherited from the base unless the patch rewrites the last
    line (with or without a ``No newline`` marker).
    """
    if base_text == "":
        base_lines: list[str] = []
        base_trailing = True
    else:
        base_lines = base_text.split("\n")
        if base_text.endswith("\n"):
            base_trailing = True
            base_lines.pop()
        else:
            base_trailing = False
    out: list[str] = []
    # Bareness of each hunk-emitted line; the new file is bare iff its last
    # line was emitted bare. Lines copied from the untouched tail inherit
    # the base trailing state instead.
    emit_bare: list[bool] = []
    cursor = 0

    for hunk in hunks:
        want = hunk.old_start - 1
        if hunk.old_count == 0:
            want = cursor
        if want < 0 or want < cursor:
            raise _PatchError(
                f"{path}: hunk overlaps earlier hunks at old line {hunk.old_start}"
            )
        while cursor < want:
            if cursor >= len(base_lines):
                raise _PatchError(f"{path}: hunk starts beyond end of file")
            out.append(base_lines[cursor])
            emit_bare.append(False)
            cursor += 1
        consumed = 0
        emitted = 0
        body = hunk.lines
        index = 0
        while index < len(body):
            raw = body[index]
            if raw.startswith("\\"):
                raise _PatchError(f"{path}: no-newline marker without a preceding line")
            if raw == "":
                prefix, text = " ", ""
            else:
                prefix, text = raw[0], raw[1:]
            if prefix not in (" ", "-", "+"):
                raise _PatchError(f"{path}: invalid hunk body prefix: {prefix!r}")
            bare_next = index + 1 < len(body) and body[index + 1].startswith("\\")
            if bare_next:
                marker = body[index + 1].strip()
                if marker != "\\ No newline at end of file":
                    raise _PatchError(f"{path}: unsupported patch marker: {body[index + 1]!r}")
            if prefix == " ":
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    raise _PatchError(f"{path}: context mismatch at old line {cursor + 1}")
                out.append(text)
                emit_bare.append(bare_next)
                cursor += 1
                consumed += 1
                emitted += 1
                if bare_next:
                    if cursor != len(base_lines):
                        raise _PatchError(f"{path}: no-newline marker mid-file")
                    if base_trailing:
                        raise _PatchError(
                            f"{path}: patch claims a bare final line but base ends with newline"
                        )
                    index += 1
            elif prefix == "-":
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    raise _PatchError(f"{path}: removal mismatch at old line {cursor + 1}")
                cursor += 1
                consumed += 1
                if bare_next:
                    if cursor != len(base_lines):
                        raise _PatchError(f"{path}: no-newline marker mid-file")
                    if base_trailing:
                        raise _PatchError(
                            f"{path}: patch claims a bare final line but base ends with newline"
                        )
                    index += 1
            else:
                out.append(text)
                emit_bare.append(bare_next)
                emitted += 1
                if bare_next:
                    index += 1
            index += 1
        if consumed != hunk.old_count:
            raise _PatchError(
                f"{path}: hunk old count {hunk.old_count} does not match {consumed} lines"
            )
        if emitted != hunk.new_count:
            raise _PatchError(
                f"{path}: hunk new count {hunk.new_count} does not match {emitted} lines"
            )
    tail = base_lines[cursor:]
    out.extend(tail)
    if not out:
        return ""
    trailing = base_trailing if tail else not emit_bare[-1]
    return "\n".join(out) + ("" if not trailing else "\n")


# --------------------------------------------------------------------------- #
# AST test discovery and normalized fingerprints.
# --------------------------------------------------------------------------- #


@dataclass
class _TestDef:
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef


def _is_testcase_base(expr: ast.expr) -> bool:
    name: str | None = None
    if isinstance(expr, ast.Name):
        name = expr.id
    elif isinstance(expr, ast.Attribute):
        name = expr.attr
    return name is not None and (name == "TestCase" or name.endswith("TestCase"))


def _is_test_class(node: ast.ClassDef) -> bool:
    short = node.name.split(".")[-1]
    return short.startswith("Test") or any(_is_testcase_base(base) for base in node.bases)


def _collect_tests(tree: ast.Module) -> dict[str, _TestDef]:
    """Collect module ``test*`` funcs plus ``test*`` methods of Test classes.

    Walks nested classes and compound statements (``if``/``try`` blocks often
    guard version-specific tests); anything defined inside a function body is
    a helper, never a collected node.
    """
    found: dict[str, _TestDef] = {}

    def _methods(stmts: list[ast.stmt], prefix: str) -> None:
        for entry in stmts:
            if isinstance(entry, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if entry.name.startswith("test"):
                    qual = f"{prefix}.{entry.name}"
                    found[qual] = _TestDef(qual, entry)
            elif isinstance(entry, ast.ClassDef):
                if _is_test_class(entry):
                    _methods(entry.body, f"{prefix}.{entry.name}")
            else:
                for child in ast.iter_child_nodes(entry):
                    if isinstance(child, ast.stmt):
                        _methods([child], prefix)

    def _module(stmts: list[ast.stmt], prefix: str) -> None:
        for entry in stmts:
            if isinstance(entry, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not prefix and entry.name.startswith("test"):
                    found[entry.name] = _TestDef(entry.name, entry)
            elif isinstance(entry, ast.ClassDef):
                qual = f"{prefix}.{entry.name}" if prefix else entry.name
                if _is_test_class(entry):
                    _methods(entry.body, qual)
                _module(entry.body, qual)
            else:
                for child in ast.iter_child_nodes(entry):
                    if isinstance(child, ast.stmt):
                        _module([child], prefix)

    _module(tree.body, "")
    return found


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _fingerprint_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Normalized AST digest: decorators, signature and body matter.

    Comments never reach the AST, docstrings are dropped, and def/class names
    are excluded so renames and comment-only copies subtract as duplicates.
    """
    payload = {
        "async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": [
            ast.dump(item, annotate_fields=True, include_attributes=False)
            for item in node.decorator_list
        ],
        "args": ast.dump(node.args, annotate_fields=True, include_attributes=False),
        "returns": (
            ast.dump(node.returns, annotate_fields=True, include_attributes=False)
            if node.returns is not None
            else None
        ),
        "type_params": [
            ast.dump(item, annotate_fields=True, include_attributes=False)
            for item in getattr(node, "type_params", [])
        ],
        "body": [
            ast.dump(item, annotate_fields=True, include_attributes=False)
            for item in _strip_docstring(list(node.body))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return compute_sha256(encoded)


def _parse_tests(source: str, path: str) -> dict[str, str]:
    """Map qualname to normalized fingerprint; syntax failure is explicit."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise _PatchError(f"{path}: cannot parse Python source: {exc}") from exc
    return {qual: _fingerprint_of(entry.node) for qual, entry in _collect_tests(tree).items()}


def _is_test_or_support(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return any(part in _SUPPORT_DIR_NAMES for part in parts[:-1])


# --------------------------------------------------------------------------- #
# Suite construction.
# --------------------------------------------------------------------------- #


@dataclass
class _ChangedFile:
    path: str
    status: str
    old_bytes: bytes | None
    new_bytes: bytes | None
    new_mode: str | None


def _unavailable_suite(
    *,
    task_name: str,
    image: str,
    workdir: str,
    base_commit: str,
    fix_commit: str,
    fix_parent: str,
    hidden_patch_sha256: str,
    refusals: list[str],
    notes: list[str],
    exclusions: list[HeldoutExclusion] | None = None,
) -> HeldoutSuite:
    return HeldoutSuite(
        task_name=task_name,
        image=image,
        workdir=workdir,
        base_commit=base_commit,
        fix_commit=fix_commit,
        fix_parent=fix_parent,
        hidden_patch_sha256=hidden_patch_sha256,
        status="unavailable",
        tests=[],
        files=[],
        exclusions=exclusions or [],
        refusals=refusals,
        notes=notes,
    )


def _overlay_mode(new_mode: str, path: str) -> str:
    if new_mode in ("100644", "100755"):
        return new_mode
    raise _PatchError(f"{path}: unsupported file mode {new_mode}")


_METHOD_NOTES = [
    "source git accessed read-only (rev-parse/rev-list/cat-file/diff-tree/ls-tree "
    "with --no-replace-objects, protocol.allow=never, no inherited config, no locks); "
    "hidden patch applied in memory to cat-file bytes; source index/objects untouched",
    "test/support scope: conftest.py, test_*.py, *_test.py, or under test|tests|testing|"
    "fixtures|fixture|testdata|test-data|test_data; production paths never exported",
    "fingerprint: normalized AST ignoring comments, docstrings and def/class names; "
    "decorators, signature, returns and body significant; exact-AST novelty is not proof "
    "of behavioral independence and must be qualified by later functional controls",
]


def extract_suite(
    *,
    git_dir: Path,
    base_commit: str,
    fix_commit: str,
    hidden_patch: Path,
    task_name: str,
    image: str,
    workdir: str,
) -> HeldoutSuite:
    """Extract the held-out upstream-test suite for one local Git input.

    Raises ``FileNotFoundError``/``ValueError`` for malformed caller
    arguments (missing paths, bad SHA shapes, unpinned image, relative
    workdir). Returns a suite with ``status == "unavailable"`` and explicit
    ``refusals`` for missing/ambiguous/unsupported source data instead of
    counting it as ``no_extra_tests``.
    """
    git_dir = Path(git_dir)
    hidden_patch = Path(hidden_patch)
    if not git_dir.exists():
        raise FileNotFoundError(f"git_dir does not exist: {git_dir}")
    if not hidden_patch.is_file():
        raise FileNotFoundError(f"hidden_patch is not a file: {hidden_patch}")
    for label, sha in (("base_commit", base_commit), ("fix_commit", fix_commit)):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):
            raise ValueError(f"{label} must be a full 40/64-hex repository SHA")
    if not isinstance(task_name, str) or not task_name.strip():
        raise ValueError("task_name must be a non-empty string")
    if not isinstance(image, str) or not re.search(r"@sha256:[0-9a-f]{64}(?![0-9a-f])", image):
        raise ValueError("image must pin a digest (@sha256:<64 hex>)")
    if not isinstance(workdir, str) or "\\" in workdir or not workdir.startswith("/"):
        raise ValueError("workdir must be an absolute POSIX path")
    if any(part == ".." for part in PurePosixPath(workdir).parts):
        raise ValueError("workdir must not contain '..'")

    try:
        hidden_bytes = hidden_patch.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"cannot read hidden_patch {hidden_patch}: {exc}") from exc
    hidden_patch_sha256 = compute_sha256(hidden_bytes)

    resolved_git = _resolve_git_dir(git_dir)
    if resolved_git is None:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=ZERO_PARENT,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"not a git directory: {git_dir}"],
            notes=list(_METHOD_NOTES),
        )
    base_full = _rev_parse(resolved_git, base_commit + "^{commit}")
    if base_full is None or base_full.lower() != base_commit.lower():
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=ZERO_PARENT,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"base commit not present in git_dir: {base_commit}"],
            notes=list(_METHOD_NOTES),
        )
    fix_full = _rev_parse(resolved_git, fix_commit + "^{commit}")
    if fix_full is None or fix_full.lower() != fix_commit.lower():
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=ZERO_PARENT,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"fix commit not present in git_dir: {fix_commit}"],
            notes=list(_METHOD_NOTES),
        )
    parent_full = _rev_parse(resolved_git, fix_commit + "^1")
    second_parent = _rev_parse(resolved_git, fix_commit + "^2")
    if parent_full is None:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=ZERO_PARENT,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"fix commit has no first parent: {fix_commit}"],
            notes=list(_METHOD_NOTES),
        )
    if second_parent is not None:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=ZERO_PARENT,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[
                f"fix commit is a merge with several parents; refusing to select one: {fix_commit}"
            ],
            notes=list(_METHOD_NOTES),
        )

    diff_proc = _run_git(
        resolved_git,
        "diff-tree",
        "--no-commit-id",
        "--raw",
        "-r",
        "-z",
        "--no-renames",
        parent_full,
        fix_full,
        "--",
    )
    if diff_proc.returncode != 0:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"git diff-tree failed: {_git_stderr(diff_proc)}"],
            notes=list(_METHOD_NOTES),
        )

    refusals: list[str] = []
    notes = list(_METHOD_NOTES)
    exclusions: list[HeldoutExclusion] = []
    changed: list[_ChangedFile] = []
    skipped_production = 0

    chunks = [chunk for chunk in diff_proc.stdout.split(b"\0") if chunk != b""]
    if len(chunks) % 2 != 0:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=["git diff-tree output has an unpaired record"],
            notes=notes,
        )
    for index in range(0, len(chunks), 2):
        info_raw, path_raw = chunks[index], chunks[index + 1]
        try:
            path = path_raw.decode("utf-8")
        except UnicodeDecodeError:
            refusals.append(f"fix entry with undecodable path bytes: {path_raw!r}")
            continue
        try:
            fields = info_raw.decode("ascii")[1:].split(" ")
            old_mode, new_mode, old_sha, new_sha, status = fields
        except (UnicodeDecodeError, ValueError):
            refusals.append(f"unparseable diff-tree record for {path!r}")
            continue
        if status not in ("A", "M", "D", "T"):
            refusals.append(f"unsupported change kind {status!r} for {path!r}")
            continue
        try:
            validate_suite_path(path)
        except ValueError as exc:
            refusals.append(f"unsafe fix path {path!r}: {exc}")
            continue
        if not _is_test_or_support(path):
            skipped_production += 1
            continue
        if old_mode in ("120000", "160000") or new_mode in ("120000", "160000"):
            refusals.append(f"symlink/submodule entry unsupported: {path!r}")
            continue
        try:
            old_bytes = _read_blob(resolved_git, old_sha) if old_sha != "0" * 40 else None
            new_bytes = _read_blob(resolved_git, new_sha) if new_sha != "0" * 40 else None
        except _GitError as exc:
            refusals.append(f"{path}: {exc}")
            continue
        if status == "D" and new_bytes is not None:
            refusals.append(f"{path}: deletion still resolves content")
            continue
        if status == "A" and old_bytes is not None:
            refusals.append(f"{path}: addition already has old content")
            continue
        changed.append(
            _ChangedFile(
                path=path,
                status=status,
                old_bytes=old_bytes,
                new_bytes=new_bytes,
                new_mode=new_mode if new_bytes is not None else None,
            )
        )

    try:
        patch_text = hidden_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=["hidden patch is not utf-8 text (possible binary patch)"],
            notes=notes,
            exclusions=exclusions,
        )
    try:
        file_patches = _parse_unified_patch(patch_text)
    except _PatchError as exc:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"hidden patch unsupported: {exc}"],
            notes=notes,
            exclusions=exclusions,
        )

    postimage: dict[str, bytes | None] = {}
    skipped_hidden_production = 0
    for entry in file_patches:
        for candidate in (entry.old_path, entry.new_path):
            if candidate is None or candidate == "/dev/null":
                continue
            try:
                validate_suite_path(candidate)
            except ValueError as exc:
                refusals.append(f"unsafe hidden patch path {candidate!r}: {exc}")
        if refusals:
            continue
        touched = [p for p in (entry.old_path, entry.new_path) if p is not None]
        if touched and not any(_is_test_or_support(p) for p in touched):
            # Hidden production/fixture bytes are out of scope: only hidden
            # *test* postimages feed duplicate subtraction, so production
            # entries (including binary fixtures) are counted, not applied.
            skipped_hidden_production += 1
            continue
        if entry.is_binary:
            target = entry.new_path if entry.new_path is not None else entry.old_path
            refusals.append(f"hidden patch binary content unsupported: {target!r}")
            continue
        old_path = entry.old_path
        new_path = entry.new_path
        if entry.is_delete:
            if old_path is None:
                refusals.append("hidden patch deletion without an old path")
                continue
            try:
                anchor = _tree_entry(resolved_git, base_full, old_path)
            except _GitError as exc:
                refusals.append(f"hidden base lookup failed for {old_path!r}: {exc}")
                continue
            if anchor is None:
                refusals.append(f"hidden patch deletes absent base file: {old_path!r}")
                continue
            if new_path is not None and new_path != old_path:
                refusals.append(f"hidden patch ambiguous deletion target: {old_path!r}")
                continue
            postimage[old_path] = None
            continue
        if entry.is_new or (old_path is None and new_path is not None):
            if new_path is None:
                refusals.append("hidden patch addition without a new path")
                continue
            try:
                anchor = _tree_entry(resolved_git, base_full, new_path)
            except _GitError as exc:
                refusals.append(f"hidden base lookup failed for {new_path!r}: {exc}")
                continue
            if anchor is not None:
                refusals.append(f"hidden patch creates existing base file: {new_path!r}")
                continue
            try:
                postimage[new_path] = _compose_postimage("", entry.hunks, new_path).encode(
                    "utf-8"
                )
            except _PatchError as exc:
                refusals.append(f"hidden patch does not apply for {new_path!r}: {exc}")
            continue
        assert old_path is not None and new_path is not None
        try:
            anchor = _tree_entry(resolved_git, base_full, old_path)
        except _GitError as exc:
            refusals.append(f"hidden base lookup failed for {old_path!r}: {exc}")
            continue
        if anchor is None:
            refusals.append(f"hidden patch modifies absent base file: {old_path!r}")
            continue
        try:
            anchor_bytes = _read_blob(resolved_git, anchor[1])
        except _GitError as exc:
            refusals.append(f"hidden base blob unreadable for {old_path!r}: {exc}")
            continue
        try:
            anchor_text = anchor_bytes.decode("utf-8")
        except UnicodeDecodeError:
            refusals.append(f"hidden base file is not utf-8 text: {old_path!r}")
            continue
        try:
            postimage[new_path] = _compose_postimage(anchor_text, entry.hunks, new_path).encode(
                "utf-8"
            )
        except _PatchError as exc:
            refusals.append(f"hidden patch does not apply for {old_path!r}: {exc}")
            continue
        if entry.is_rename and new_path != old_path:
            postimage.setdefault(old_path, None)

    if refusals:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=refusals,
            notes=notes,
            exclusions=exclusions,
        )

    # Base duplicates span the whole base tree: a fix test copied from an
    # untouched file is still a duplicate. Hidden duplicates come only from
    # the reconstructed hidden postimages.
    try:
        base_py_shas = _list_tree_py_shas(resolved_git, base_full)
    except _GitError as exc:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"base tree listing failed: {exc}"],
            notes=notes,
            exclusions=exclusions,
        )
    try:
        base_blobs = _read_blobs_batch(resolved_git, sorted(set(base_py_shas.values())))
    except _GitError as exc:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=[f"base blob read failed: {exc}"],
            notes=notes,
            exclusions=exclusions,
        )
    base_fingerprints: set[str] = set()
    scope_paths = {item.path for item in changed} | set(postimage)
    skipped_base_unparsed = 0
    for path in sorted(base_py_shas):
        raw = base_blobs[base_py_shas[path]]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if path in scope_paths:
                refusals.append(f"base file is not utf-8 text: {path!r}")
            else:
                skipped_base_unparsed += 1
            continue
        try:
            base_fingerprints.update(_parse_tests(text, path).values())
        except _PatchError as exc:
            if path in scope_paths:
                refusals.append(f"{exc}")
            else:
                skipped_base_unparsed += 1
    hidden_py = sorted(
        path
        for path, content in postimage.items()
        if path.endswith(".py") and content is not None and _is_test_or_support(path)
    )
    hidden_fingerprints: set[str] = set()
    for path in hidden_py:
        hidden_bytes_for_path = postimage[path]
        assert hidden_bytes_for_path is not None
        try:
            hidden_fingerprints.update(
                _parse_tests(hidden_bytes_for_path.decode("utf-8"), path).values()
            )
        except UnicodeDecodeError:
            refusals.append(f"hidden postimage is not utf-8 text: {path!r}")
        except _PatchError as exc:
            refusals.append(f"{exc}")
    if refusals:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=refusals,
            notes=notes,
            exclusions=exclusions,
        )

    novel: list[HeldoutTest] = []
    for item in sorted(changed, key=lambda entry: entry.path):
        if item.new_bytes is None:
            continue
        if not item.path.endswith(".py"):
            continue
        try:
            new_text = item.new_bytes.decode("utf-8")
        except UnicodeDecodeError:
            refusals.append(f"fix file is not utf-8 text: {item.path!r}")
            continue
        try:
            new_tests = _parse_tests(new_text, item.path)
        except _PatchError as exc:
            refusals.append(f"{exc}")
            continue
        old_tests: dict[str, str] = {}
        if item.old_bytes is not None:
            try:
                old_tests = _parse_tests(item.old_bytes.decode("utf-8"), item.path)
            except UnicodeDecodeError:
                refusals.append(f"fix parent file is not utf-8 text: {item.path!r}")
                continue
            except _PatchError as exc:
                refusals.append(f"{exc}")
                continue
        for qual in sorted(new_tests):
            fingerprint = new_tests[qual]
            node_id = item.path + "::" + qual.replace(".", "::")
            if qual not in old_tests:
                change: Literal["added", "modified"] = "added"
            elif old_tests[qual] != fingerprint:
                change = "modified"
            else:
                exclusions.append(HeldoutExclusion(node_id=node_id, reason="unchanged"))
                continue
            if fingerprint in hidden_fingerprints:
                exclusions.append(HeldoutExclusion(node_id=node_id, reason="hidden_duplicate"))
            elif fingerprint in base_fingerprints:
                exclusions.append(HeldoutExclusion(node_id=node_id, reason="base_duplicate"))
            else:
                novel.append(
                    HeldoutTest(
                        node_id=node_id,
                        path=item.path,
                        qualname=qual,
                        fingerprint=fingerprint,
                        change=change,
                    )
                )
    if refusals:
        return _unavailable_suite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            refusals=refusals,
            notes=notes,
            exclusions=exclusions,
        )

    exclusions.sort(key=lambda entry: entry.node_id)
    notes.append(
        f"fix changes {len(changed)} test/support paths; "
        f"{skipped_production} production paths excluded without export; "
        f"{skipped_hidden_production} non-test hidden entries out of scope; "
        f"base duplicates scanned over {len(base_py_shas)} test/support .py files "
        f"({skipped_base_unparsed} unscanned outside fix/hidden scope)"
    )
    if not novel:
        return HeldoutSuite(
            task_name=task_name,
            image=image,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_parent=parent_full,
            hidden_patch_sha256=hidden_patch_sha256,
            status="no_extra_tests",
            tests=[],
            files=[],
            exclusions=exclusions,
            refusals=[],
            notes=notes,
        )
    novel.sort(key=lambda item: item.node_id)
    payloads: list[HeldoutFile] = []
    seen_paths: set[str] = set()
    for item in sorted(changed, key=lambda entry: entry.path):
        if item.path in seen_paths:
            return _unavailable_suite(
                task_name=task_name,
                image=image,
                workdir=workdir,
                base_commit=base_commit,
                fix_commit=fix_commit,
                fix_parent=parent_full,
                hidden_patch_sha256=hidden_patch_sha256,
                refusals=[f"duplicate fix entry for {item.path!r}"],
                notes=notes,
                exclusions=exclusions,
            )
        seen_paths.add(item.path)
        if item.new_bytes is None:
            payloads.append(
                HeldoutFile(path=item.path, mode=None, content_base64=None, sha256=None)
            )
            continue
        assert item.new_mode is not None
        try:
            overlay_mode = _overlay_mode(item.new_mode, item.path)
        except _PatchError as exc:
            return _unavailable_suite(
                task_name=task_name,
                image=image,
                workdir=workdir,
                base_commit=base_commit,
                fix_commit=fix_commit,
                fix_parent=parent_full,
                hidden_patch_sha256=hidden_patch_sha256,
                refusals=[f"{exc}"],
                notes=notes,
                exclusions=exclusions,
            )
        payloads.append(
            HeldoutFile(
                path=item.path,
                mode=overlay_mode,  # type: ignore[arg-type]
                content_base64=base64.b64encode(item.new_bytes).decode("ascii"),
                sha256=compute_sha256(item.new_bytes),
            )
        )
    return HeldoutSuite(
        task_name=task_name,
        image=image,
        workdir=workdir,
        base_commit=base_commit,
        fix_commit=fix_commit,
        fix_parent=parent_full,
        hidden_patch_sha256=hidden_patch_sha256,
        status="ready",
        tests=novel,
        files=payloads,
        exclusions=exclusions,
        refusals=[],
        notes=notes,
    )


def load_suite(path: Path) -> HeldoutSuite:
    """Load and strictly validate a suite document."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"suite file is not valid JSON: {path}: {exc}") from exc
    return HeldoutSuite.model_validate(payload)


def write_suite(suite: HeldoutSuite, path: Path) -> None:
    """Write a suite document; refuses to overwrite an existing path."""
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to overwrite suite file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(suite.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def build_parser() -> argparse.ArgumentParser:
    """Build the small single-input extraction CLI parser."""
    parser = argparse.ArgumentParser(
        prog="evallab.heldout_tests",
        description="Extract one held-out upstream-test suite from a local Git directory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract", help="extract one suite and write it as JSON")
    extract.add_argument("--git-dir", type=Path, required=True)
    extract.add_argument("--base-commit", required=True)
    extract.add_argument("--fix-commit", required=True)
    extract.add_argument("--hidden-patch", type=Path, required=True)
    extract.add_argument("--task-name", required=True)
    extract.add_argument("--image", required=True)
    extract.add_argument("--workdir", required=True)
    extract.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m evallab.heldout_tests extract ...``."""
    args = build_parser().parse_args(argv)
    if args.command == "extract":
        suite = extract_suite(
            git_dir=args.git_dir,
            base_commit=args.base_commit,
            fix_commit=args.fix_commit,
            hidden_patch=args.hidden_patch,
            task_name=args.task_name,
            image=args.image,
            workdir=args.workdir,
        )
        write_suite(suite, args.out)
        print(
            json.dumps(
                {
                    "status": suite.status,
                    "tests": len(suite.tests),
                    "files": len(suite.files),
                    "exclusions": len(suite.exclusions),
                    "refusals": len(suite.refusals),
                    "out": str(args.out),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
