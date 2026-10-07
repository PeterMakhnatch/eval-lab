"""Sandbox-side file-access observer for HAR-180.

Uploaded into the agent sandbox by ``evallab.harbor_file_access`` and run
there (never on the evaluator host, never wrapping agent commands). Three
subcommands:

* ``probe`` -- report interpreter/tooling availability, resolve actual git
  storage (worktree ``.git``, ``git rev-parse`` resolution, the MiMo
  ``git-hidden`` locations), record the pre-agent git ancestry baseline
  (``git_history``: recorded ``/var/lib/mimo/base`` preferred, else HEAD,
  with bounded ancestor enumeration), and check the known grader root plus
  any evaluator-configured candidates. Prints one JSON document to stdout.
* ``snapshot`` -- walk protected roots and hash file content (chunked,
  size-capped). Prints one JSON manifest to stdout; only digests cross the
  sandbox boundary, never file content.
* ``capture`` -- supervise ``inotifywait -m`` over the selected watch dirs
  with NUL framing and a terminal field, seal framed
  bytes into ``chunks/cNNNNNN.bin`` files as they arrive (bounded new
  bytes per transfer, never whole-file re-reads), write a READY file once
  ``Watches established.`` appears on stderr, and finalize a
  ``capture-status.json`` with chunk/loss accounting on shutdown.

Python 3.7 compatible at runtime: only stdlib modules available since 3.7,
no ``match``, no ``removeprefix``, no ``missing_ok``, no ``zoneinfo``.
Builtin generic annotations are safe because ``from __future__ import
annotations`` keeps them unevaluated on 3.7+.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

SCHEMA_PROBE = "evallab.file_access_probe/v1"
SCHEMA_SNAPSHOT = "evallab.file_access_snapshot/v1"
SCHEMA_CAPTURE_STATUS = "evallab.file_access_capture_status/v1"

BASE_EVENTS = ("open", "access")
LOSS_EVENTS = ("q_overflow", "ignored", "unmount")
FRAME_FORMAT = "%e%0%w%f%0EVALLAB_END%0"
READINESS_MARKER = "Watches established."
SPAWN_GRACE_SEC = 5.0
STOP_GRACE_SEC = 10.0
MAX_HASH_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20000
STDERR_TAIL_CHARS = 2000
CHUNK_BYTES = 1024 * 1024
#: A sealed chunk is at most this big; the controller downloads sealed chunks
#: only, so every transfer is bounded new bytes, never a whole-file re-read.
SEAL_BYTES = 64 * 1024
#: Seal a non-empty chunk at least this often, so quiet windows still publish.
SEAL_SEC = 5.0
#: Sandbox-side cap on the framed stream per window; past it the observer
#: stops watching and reports truncation instead of filling the disk.
MAX_STREAM_BYTES = 32 * 1024 * 1024

DISCOVERY_CANDIDATES = ("/testbed", "/app", "/repo", "/workspace")
MIMO_GIT_HIDDEN = "/var/lib/mimo/git-hidden"
#: Recorded base commit written by the MiMo setup (``echo "$BASE" > "$M/base"``).
#: Preferred over HEAD when it names a commit object present in the repository;
#: dataset revisions and task metadata are never consulted for the code base.
MIMO_BASE_FILE = "/var/lib/mimo/base"
TESTS_DIR = "/tests"
#: Bound on stored ancestry per repository; enumeration stops incomplete
#: rather than ingesting an unbounded object graph.
GIT_HISTORY_MAX_COMMITS = 100000
#: Bound on any single read-only git query used for ancestry capture.
GIT_HISTORY_TIMEOUT_SEC = 10
#: Bound on the whole ancestry collection across all repositories in one probe.
#: Queries share this monotonic deadline so ancestry work can never push the
#: probe past the plugin's sandbox exec envelope; repos unvisited when it
#: lapses are recorded incomplete/unknown, never blocking capture.
GIT_HISTORY_TOTAL_TIMEOUT_SEC = 3.0


def utc_now_iso() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000000):06d}Z"


@contextlib.contextmanager
def _chunk_output(path: str):
    with open(path, "wb") as output:
        yield output


def build_inotifywait_argv(
    watch_paths: list[str],
    event_names: list[str],
    *,
    stdbuf: bool,
) -> list[str]:
    """Argv for one ``inotifywait -m`` run piped to the chunk writer.

    ``stdbuf -o0 -e0`` (when present) defeats libc block buffering so quiet
    windows still publish promptly; without it, delivery waits on buffer
    fills and the status says so.
    """
    argv = [
        "inotifywait",
        "-m",
        "-r",
        "--format",
        FRAME_FORMAT,
        "--no-newline",
    ]
    for name in event_names:
        argv.extend(["-e", name])
    argv.extend(sorted(watch_paths))
    if stdbuf:
        return ["stdbuf", "-o0", "-e0"] + argv
    return argv


def stderr_ready(lines: list[str]) -> bool:
    """True once ``inotifywait`` has registered the initial watch set."""
    return any(line.strip() == READINESS_MARKER for line in lines)


def stderr_watch_errors(lines: list[str]) -> list[str]:
    """Watch-registration failures (limits, permissions) worth surfacing."""
    errors = []
    for line in lines:
        lowered = line.lower()
        if (
            "failed to watch" in lowered
            or "upper limit" in lowered
            or "permission denied" in lowered
        ):
            errors.append(line.strip()[:200])
    return errors


def stderr_overflow(lines: list[str]) -> bool:
    """Best-effort overflow sighting independent of ``-e`` selection."""
    return any("overflow" in line.lower() for line in lines)


def entry_for(path: str, category: str, max_hash_bytes: int) -> dict[str, Any] | None:
    """Describe one sandbox path. Absent paths return None (caller records)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    entry: dict[str, Any] = {"path": path, "category": category}
    mode = info.st_mode
    if stat.S_ISREG(mode):
        entry["type"] = "file"
        entry["size_bytes"] = info.st_size
        if info.st_size > max_hash_bytes:
            entry["sha256"] = None
            entry["hash_status"] = "size_limit"
        else:
            try:
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    while True:
                        chunk = handle.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                # Bare lowercase hex: the contract and the watch parser
                # accept ``[0-9a-f]{64}`` or null, never prefixed digests.
                entry["sha256"] = digest.hexdigest()
                entry["hash_status"] = "complete"
            except OSError:
                entry["sha256"] = None
                entry["hash_status"] = "unreadable"
    elif stat.S_ISDIR(mode):
        entry["type"] = "directory"
    elif stat.S_ISLNK(mode):
        entry["type"] = "symlink"
        with contextlib.suppress(OSError):
            entry["target"] = os.readlink(path)
        entry.setdefault("target", None)
    else:
        entry["type"] = "other"
    return entry


def walk_root(
    root: str, category: str, max_hash_bytes: int, budget: list[int]
) -> tuple[list[dict[str, Any]], bool]:
    """Depth-first snapshot of one root without following symlinks."""
    entries: list[dict[str, Any]] = []
    truncated = False
    first = entry_for(root, category, max_hash_bytes)
    if first is None:
        return entries, truncated
    entries.append(first)
    budget[0] -= 1
    if first.get("type") != "directory":
        return entries, truncated
    stack = [root]
    while stack:
        if budget[0] <= 0:
            truncated = True
            break
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if budget[0] <= 0:
                truncated = True
                break
            described = entry_for(child.path, category, max_hash_bytes)
            if described is None:
                continue
            entries.append(described)
            budget[0] -= 1
            if described.get("type") == "directory" and not os.path.islink(child.path):
                stack.append(child.path)
    return entries, truncated


def split_root_spec(spec: str) -> tuple[str, str]:
    """Split a ``PATH:CATEGORY`` root spec on its last colon."""
    path, _, category = spec.rpartition(":")
    if not path or not category:
        raise ValueError(f"root spec must look like PATH:CATEGORY, got {spec!r}")
    return path, category


def cmd_snapshot(args: argparse.Namespace) -> int:
    max_hash_bytes = args.max_bytes
    budget = [args.max_files]
    entries: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    truncated = False
    specs: list[tuple[str, str, bool]] = []
    for spec in args.root or []:
        path, category = split_root_spec(spec)
        specs.append((path, category, False))
    for spec in args.shallow_root or []:
        path, category = split_root_spec(spec)
        specs.append((path, category, True))
    for path, category, shallow in sorted(specs):
        if not os.path.lexists(path):
            roots.append({"path": path, "category": category, "status": "absent"})
            continue
        if shallow:
            described = entry_for(path, category, max_hash_bytes)
            roots.append(
                {"path": path, "category": category, "status": "present", "mode": "shallow"}
            )
            if described is not None:
                entries.append(described)
                budget[0] -= 1
            continue
        roots.append({"path": path, "category": category, "status": "present", "mode": "full"})
        found, cut = walk_root(path, category, max_hash_bytes, budget)
        entries.extend(found)
        truncated = truncated or cut
    manifest = {
        "schema": SCHEMA_SNAPSHOT,
        "captured_at": utc_now_iso(),
        "max_bytes": max_hash_bytes,
        "max_files": args.max_files,
        "truncated": truncated or budget[0] <= 0,
        "roots": roots,
        "entries": entries,
    }
    sys.stdout.write(json.dumps(manifest, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


def resolve_git_dir(candidate: str, git_binary: str | None) -> tuple[str | None, str]:
    """Resolve the git dir for a worktree candidate. Returns (git_dir, method)."""
    dotgit = os.path.join(candidate, ".git")
    if os.path.isdir(dotgit) and not os.path.islink(dotgit):
        return dotgit, "dotgit"
    if os.path.lexists(dotgit) or os.path.isdir(candidate):
        # A ``.git`` file (worktree/submodule pointer), a symlink, or a bare
        # candidate root: resolve with git when available.
        if git_binary is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc = subprocess.run(
                    [git_binary, "-C", candidate, "rev-parse", "--absolute-git-dir"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                resolved = (proc.stdout or "").strip()
                if proc.returncode == 0 and resolved and os.path.lexists(resolved):
                    return resolved, "git-cli"
        if os.path.lexists(dotgit):
            return dotgit, "git-pointer"
    return None, "absent"


def describe_repo(candidate: str, git_binary: str | None) -> dict[str, Any]:
    """Describe one worktree candidate: existence, git dir, objects/refs."""
    report: dict[str, Any] = {"candidate": candidate}
    if not os.path.isdir(candidate):
        report.update({"exists": False, "git_dir": None, "method": "absent"})
        return report
    report["exists"] = True
    git_dir, method = resolve_git_dir(candidate, git_binary)
    report["git_dir"] = git_dir
    report["method"] = method
    if git_dir is not None:
        report["objects"] = (
            "present" if os.path.isdir(os.path.join(git_dir, "objects")) else "absent"
        )
        report["refs"] = "present" if os.path.lexists(os.path.join(git_dir, "refs")) else "absent"
    else:
        report["objects"] = "absent"
        report["refs"] = "absent"
    return report


def is_full_commit_hash(value: object) -> bool:
    """True for a full lowercase 40/64-hex commit name; nothing else qualifies."""
    if not isinstance(value, str) or len(value) not in (40, 64):
        return False
    return all(char in "0123456789abcdef" for char in value)


def read_mimo_base(path: str = MIMO_BASE_FILE) -> str | None:
    """Recorded base commit from the MiMo setup state, or None when absent/invalid."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return None
    token = text.strip().lower()
    return token if is_full_commit_hash(token) else None


def _run_git(
    git_binary: str | None,
    git_dir: str,
    argv: list[str],
    timeout: float,
    deadline: float | None = None,
) -> tuple[Any | None, bool]:
    """Read-only git bound to one git dir. Returns (proc or None, timed_out)."""
    if not git_binary:
        return None, False
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, True
        timeout = min(timeout, remaining)
    # Explicit ``--git-dir`` pins the repository: git never walks up from the
    # current directory to a parent repo. ``GIT_NO_REPLACE_OBJECTS`` keeps
    # grafted history out; ``GIT_NO_LAZY_FETCH`` asks modern git not to
    # implicitly fetch missing promisor objects -- but partial clones predate
    # that guard, so ``GIT_ALLOW_PROTOCOL`` (empty: no transport allowed)
    # denies every fetch transport independent of repo config. Together they
    # bound ancestry reads to local objects even on older supported git. The
    # prompt/lock knobs keep local reads non-interactive.
    cmd = [git_binary, "--git-dir", git_dir] + list(argv)
    env = dict(os.environ)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = ""
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return None, True
    except (OSError, subprocess.SubprocessError):
        return None, False
    return proc, False


def _git_head(
    git_binary: str | None,
    git_dir: str,
    timeout: float,
    deadline: float | None = None,
) -> str | None:
    """Pre-agent HEAD as a full hash, or None when unresolvable."""
    proc, _ = _run_git(
        git_binary, git_dir, ["rev-parse", "--verify", "HEAD"], timeout, deadline
    )
    if proc is None or proc.returncode != 0:
        return None
    parts = (proc.stdout or "").strip().split()
    if not parts:
        return None
    token = parts[0].lower()
    return token if is_full_commit_hash(token) else None


def _git_object_is_commit(
    git_binary: str | None,
    git_dir: str,
    commit: str,
    timeout: float,
    deadline: float | None = None,
) -> bool:
    """True when ``commit`` names a commit object present in this repository."""
    proc, _ = _run_git(git_binary, git_dir, ["cat-file", "-t", commit], timeout, deadline)
    return (
        proc is not None
        and proc.returncode == 0
        and (proc.stdout or "").strip() == "commit"
    )


def _git_is_shallow(
    git_dir: str,
    git_binary: str | None,
    timeout: float,
    deadline: float | None = None,
) -> bool | None:
    """Shallow boundary present (True), absent (False), or unknowable (None)."""
    try:
        if os.path.isfile(os.path.join(git_dir, "shallow")) and os.path.getsize(
            os.path.join(git_dir, "shallow")
        ) > 0:
            return True
    except OSError:
        pass
    proc, _ = _run_git(
        git_binary, git_dir, ["rev-parse", "--is-shallow-repository"], timeout, deadline
    )
    if proc is None or proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip().lower()
    return value == "true" if value in ("true", "false") else None


def _git_ancestors(
    git_binary: str | None,
    git_dir: str,
    base: str,
    timeout: float,
    max_commits: int,
    deadline: float | None = None,
) -> tuple[list[str], str]:
    """Ancestors of ``base`` (base first) plus a status: ok/capped/timeout/failed."""
    limit = max(int(max_commits), 0)
    proc, timed_out = _run_git(
        git_binary,
        git_dir,
        ["rev-list", "--max-count", str(limit + 1), base],
        timeout,
        deadline,
    )
    if timed_out:
        return [], "timeout"
    if proc is None or proc.returncode != 0:
        return [], "failed"
    commits = []
    for line in (proc.stdout or "").splitlines():
        token = line.strip().lower()
        if not token:
            continue
        if not is_full_commit_hash(token):
            return [], "failed"
        commits.append(token)
    if base not in commits:
        return [], "failed"
    if len(commits) > limit:
        return commits[:limit], "capped"
    return commits, "ok"


def describe_git_history(
    repository: str,
    git_dir: str,
    git_binary: str | None,
    *,
    mimo_base: str | None = None,
    timeout: float = GIT_HISTORY_TIMEOUT_SEC,
    max_commits: int = GIT_HISTORY_MAX_COMMITS,
    deadline: float | None = None,
) -> dict[str, Any]:
    """One ``git_history`` entry: base plus bounded ancestors, never raising."""
    entry: dict[str, Any] = {
        "repository": repository,
        "git_dir": git_dir,
        "base_commit": None,
        "base_source": "unknown",
        "ancestor_commits": [],
        "complete": False,
        "reason": None,
    }
    if not git_binary:
        entry["reason"] = "git-unavailable"
        return entry
    if deadline is not None and deadline - time.monotonic() <= 0:
        entry["reason"] = "timeout"
        return entry
    base = None
    source = "unknown"
    if mimo_base is not None and is_full_commit_hash(mimo_base) and _git_object_is_commit(
        git_binary, git_dir, mimo_base, timeout, deadline
    ):
        base, source = mimo_base, "mimo_base"
    else:
        head = _git_head(git_binary, git_dir, timeout, deadline)
        if head is not None:
            base, source = head, "pre_agent_head"
    if base is None:
        entry["reason"] = "base-unresolvable"
        return entry
    entry["base_commit"] = base
    entry["base_source"] = source
    commits, status = _git_ancestors(
        git_binary, git_dir, base, timeout, max_commits, deadline
    )
    if status == "timeout":
        entry["reason"] = "timeout"
    elif status == "failed":
        entry["reason"] = "rev-list-failed"
    elif status == "capped":
        entry["ancestor_commits"] = commits
        entry["reason"] = "capped"
    else:
        entry["ancestor_commits"] = commits
        shallow = _git_is_shallow(git_dir, git_binary, timeout, deadline)
        if shallow:
            entry["reason"] = "shallow"
        elif shallow is None:
            # Shallowness itself is unknowable: claiming complete would promote
            # a possibly truncated graph, so this stays unknown.
            entry["reason"] = "timeout"
        else:
            entry["complete"] = True
    return entry


def collect_git_history(
    reports: list[dict[str, Any]] | None,
    git_binary: str | None,
    *,
    mimo_base_path: str = MIMO_BASE_FILE,
    timeout: float = GIT_HISTORY_TIMEOUT_SEC,
    max_commits: int = GIT_HISTORY_MAX_COMMITS,
    total_timeout: float = GIT_HISTORY_TOTAL_TIMEOUT_SEC,
) -> list[dict[str, Any]]:
    """Probe-level ``git_history``: one entry per distinct git dir, never raising."""
    mimo_base = read_mimo_base(mimo_base_path)
    deadline = time.monotonic() + max(float(total_timeout), 0.0)
    history: list[dict[str, Any]] = []
    seen: set[str] = set()
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        git_dir = report.get("git_dir")
        repository = report.get("candidate") or report.get("repository")
        if not git_dir or not repository:
            continue
        try:
            key = os.path.realpath(git_dir)
        except OSError:
            key = os.path.normpath(git_dir)
        if key in seen:
            continue
        seen.add(key)
        if deadline - time.monotonic() <= 0:
            # Budget lapsed: later graphs stay unknown rather than stalling
            # the probe past the sandbox exec envelope.
            history.append(
                {
                    "repository": repository,
                    "git_dir": git_dir,
                    "base_commit": None,
                    "base_source": "unknown",
                    "ancestor_commits": [],
                    "complete": False,
                    "reason": "timeout",
                }
            )
            continue
        try:
            history.append(
                describe_git_history(
                    repository,
                    git_dir,
                    git_binary,
                    mimo_base=mimo_base,
                    timeout=timeout,
                    max_commits=max_commits,
                    deadline=deadline,
                )
            )
        except Exception:
            history.append(
                {
                    "repository": repository,
                    "git_dir": git_dir,
                    "base_commit": None,
                    "base_source": "unknown",
                    "ancestor_commits": [],
                    "complete": False,
                    "reason": "rev-list-failed",
                }
            )
    return history


def cmd_probe(args: argparse.Namespace) -> int:
    git_binary = shutil.which("git")
    candidates = list(DISCOVERY_CANDIDATES) + [os.getcwd()]
    seen = set()
    repos = []
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        repos.append(describe_repo(normalized, git_binary))
    mimo = describe_repo(MIMO_GIT_HIDDEN, git_binary)
    mimo_pointer = os.path.join(MIMO_GIT_HIDDEN, ".git")
    if mimo.get("git_dir") is None and os.path.lexists(mimo_pointer):
        nested = describe_repo(mimo_pointer, git_binary)
        if nested.get("git_dir"):
            mimo = nested
            mimo["candidate"] = MIMO_GIT_HIDDEN
    history_reports = list(repos)
    if isinstance(mimo, dict) and mimo.get("git_dir"):
        history_reports.append(mimo)
    try:
        git_history = collect_git_history(history_reports, git_binary)
    except Exception:
        # Ancestry capture never fails the probe; unknown stays unknown.
        git_history = []
    tests: dict[str, Any] = {"path": TESTS_DIR}
    if os.path.isdir(TESTS_DIR) and not os.path.islink(TESTS_DIR):
        tests["status"] = "present"
    elif os.path.lexists(TESTS_DIR):
        tests["status"] = "not-a-directory"
    else:
        tests["status"] = "absent"
    extras = []
    for extra in args.candidate or []:
        extras.append(
            {
                "path": extra,
                "exists": os.path.lexists(extra),
                "is_dir": os.path.isdir(extra) and not os.path.islink(extra),
            }
        )
    report = {
        "schema": SCHEMA_PROBE,
        "captured_at": utc_now_iso(),
        "cwd": os.getcwd(),
        "python": sys.version.split()[0],
        "inotifywait": shutil.which("inotifywait"),
        "git": git_binary,
        "repos": repos,
        "mimo_git_hidden": mimo,
        "git_history": git_history,
        "tests": tests,
        "extras": extras,
    }
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


class CaptureSupervisor:
    """Spawn ``inotifywait -m`` and publish sealed chunks while it runs.

    Framed bytes stream over a pipe into ``chunks/cNNNNNN.part``; the file is
    sealed (flushed, fsynced, renamed to ``cNNNNNN.bin``) once it reaches
    ``seal_bytes`` or ``seal_sec`` of age. The controller downloads sealed
    chunks only, so every transfer is bounded new bytes and live alerts
    precede the end of the window. A ``.part`` file is never complete and
    must not be downloaded.
    """

    def __init__(
        self,
        out_dir: str,
        watch_paths: list[str],
        loss_events: bool,
        seal_bytes: int = SEAL_BYTES,
        seal_sec: float = SEAL_SEC,
        max_bytes: int = MAX_STREAM_BYTES,
    ) -> None:
        self.out_dir = out_dir
        self.chunks_dir = os.path.join(out_dir, "chunks")
        self.watch_paths = watch_paths
        self.loss_events = loss_events
        self.seal_bytes = seal_bytes
        self.seal_sec = seal_sec
        self.max_bytes = max_bytes
        self.ready_path = os.path.join(out_dir, "READY")
        self.status_path = os.path.join(out_dir, "capture-status.json")
        self.stderr_lines: list[str] = []
        self.child: subprocess.Popen[bytes] | None = None
        self.stdbuf = shutil.which("stdbuf") is not None
        self.stop_requested = False
        self.stream_bytes = 0
        self.sealed = 0
        self.truncated = False
        self._part: Any = None
        self._part_index = 0
        self._part_bytes = 0
        self._part_opened = 0.0
        self._write_lock = threading.Lock()
        self._readers: list[threading.Thread] = []

    def event_names(self) -> list[str]:
        """Active ``-e`` selection: reads plus loss events where supported."""
        names = list(BASE_EVENTS)
        if self.loss_events:
            names.extend(LOSS_EVENTS)
        return names

    def _part_path(self) -> str:
        return os.path.join(self.chunks_dir, f"c{self._part_index:06d}.part")

    def _sealed_path(self) -> str:
        return os.path.join(self.chunks_dir, f"c{self._part_index:06d}.bin")

    def _open_part(self) -> None:
        self._part_stack = contextlib.ExitStack()
        self._part = self._part_stack.enter_context(_chunk_output(self._part_path()))
        self._part_bytes = 0
        self._part_opened = time.time()

    def _seal_part(self) -> None:
        part, self._part = self._part, None
        if part is None:
            return
        with contextlib.suppress(OSError):
            try:
                part.flush()
                os.fsync(part.fileno())
            finally:
                self._part_stack.close()
            if self._part_bytes:
                os.rename(self._part_path(), self._sealed_path())
                self.sealed += 1
            else:
                os.unlink(self._part_path())
        self._part_index += 1

    def _append_stream(self, payload: bytes) -> None:
        with self._write_lock:
            if self._part is None:
                return
            room = self.max_bytes - self.stream_bytes
            if room <= 0:
                self.truncated = True
                return
            if len(payload) > room:
                payload = payload[:room]
                self.truncated = True
            self._part.write(payload)
            self._part_bytes += len(payload)
            self.stream_bytes += len(payload)

    def write_status(self, state: str, reason: str) -> None:
        """Persist the bounded capture status; never raises."""
        tail = "".join(self.stderr_lines)[-STDERR_TAIL_CHARS:]
        status = {
            "schema": SCHEMA_CAPTURE_STATUS,
            "state": state,
            "reason": reason[:500],
            "finished_at": utc_now_iso(),
            "watch_paths": sorted(self.watch_paths),
            "events": self.event_names(),
            "loss_events": "explicit" if self.loss_events else "fallback-unverified",
            "format": FRAME_FORMAT,
            "ready": os.path.exists(self.ready_path),
            "event_bytes": self.stream_bytes,
            "chunks": self.sealed,
            "truncated": self.truncated,
            "stdbuf": "active" if self.stdbuf else "unavailable",
            "overflow_seen": stderr_overflow(self.stderr_lines),
            "watch_errors": stderr_watch_errors(self.stderr_lines),
            "stderr_tail": tail,
        }
        with contextlib.suppress(OSError), open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump(status, handle, sort_keys=True)
            handle.write("\n")

    def mark_ready(self) -> None:
        """Record that the initial watch set is registered."""
        with contextlib.suppress(OSError), open(self.ready_path, "w", encoding="utf-8") as handle:
            handle.write("ready\n")

    def _drain_stderr(self) -> None:
        child = self.child
        if child is None or child.stderr is None:
            return
        for line in iter(child.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace")
            self.stderr_lines.append(text)
            if stderr_ready(self.stderr_lines):
                self.mark_ready()
        with contextlib.suppress(Exception):
            child.stderr.close()

    def _drain_stdout(self) -> None:
        child = self.child
        if child is None or child.stdout is None:
            return
        while True:
            try:
                payload = os.read(child.stdout.fileno(), CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not payload:
                break
            self._append_stream(payload)
        with contextlib.suppress(Exception):
            child.stdout.close()

    def _spawn(self, event_names: list[str]) -> tuple[bool, str]:
        argv = build_inotifywait_argv(self.watch_paths, event_names, stdbuf=self.stdbuf)
        try:
            self.child = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        for target in (self._drain_stderr, self._drain_stdout):
            worker = threading.Thread(target=target, daemon=True)
            worker.start()
            self._readers.append(worker)
        deadline = time.time() + SPAWN_GRACE_SEC
        while time.time() < deadline:
            if stderr_ready(self.stderr_lines):
                return True, ""
            if self.child.poll() is not None:
                time.sleep(0.2)  # Let the drain threads land trailing output.
                detail = "".join(self.stderr_lines)[-STDERR_TAIL_CHARS:]
                return False, f"inotifywait exited during setup: {detail.strip()}"
            time.sleep(0.05)
        return True, ""

    def _terminate_child(self) -> None:
        child = self.child
        if child is None:
            return
        with contextlib.suppress(Exception):
            if child.poll() is None:
                child.terminate()
        with contextlib.suppress(Exception):
            child.wait(timeout=STOP_GRACE_SEC)
        with contextlib.suppress(Exception):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=STOP_GRACE_SEC)
        for worker in self._readers:
            worker.join(timeout=STOP_GRACE_SEC)
        self._readers.clear()
        self.child = None

    def request_stop(self, _signum: int, _frame: Any) -> None:
        """Handle SIGTERM/SIGINT: stop watching, seal, then finalize."""
        self.stop_requested = True

    def _rotate(self) -> None:
        with self._write_lock:
            if self._part is None:
                return
            aged = time.time() - self._part_opened >= self.seal_sec
            if self._part_bytes >= self.seal_bytes or (aged and self._part_bytes):
                self._seal_part()
                if self.stop_requested or self.truncated:
                    return
                try:
                    self._open_part()
                except OSError:
                    self.truncated = True

    def run(self) -> int:
        """Supervise one capture window; always leaves a status file behind."""
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        try:
            os.makedirs(self.chunks_dir, exist_ok=True)
        except OSError as exc:
            sys.stderr.write(f"cannot create output dir: {exc}\n")
            return 2
        if shutil.which("inotifywait") is None:
            self.write_status("error", "inotifywait is not installed in the sandbox")
            return 0
        if not self.watch_paths:
            self.write_status("error", "no watch paths selected")
            return 0
        for missing in [path for path in self.watch_paths if not os.path.lexists(path)]:
            self.stderr_lines.append(f"selected watch path absent at start: {missing}\n")
        self._open_part()
        names = list(BASE_EVENTS) + list(LOSS_EVENTS)
        ok, detail = self._spawn(names)
        if not ok:
            self._terminate_child()
        if not ok and any(
            word in detail.lower()
            for word in ("invalid", "unknown", "q_overflow", "ignored", "unmount")
        ):
            # Older inotifywait builds may reject the explicit loss events;
            # retry narrow and keep the gap visible in the status record.
            self.loss_events = False
            self.stderr_lines.append(f"loss events unsupported, retrying narrow: {detail.strip()}\n")
            ok, detail = self._spawn(list(BASE_EVENTS))
        if not ok:
            with self._write_lock:
                self._seal_part()
            self.write_status("error", detail or "inotifywait failed to start")
            return 0
        while not self.stop_requested:
            child = self.child
            if child is not None and child.poll() is not None:
                # The observer died on its own: seal, report honestly, and do
                # not restart (a restart would silently split the window).
                self._terminate_child()
                with self._write_lock:
                    self._seal_part()
                tail = "".join(self.stderr_lines)[-STDERR_TAIL_CHARS:]
                self.write_status(
                    "error",
                    f"inotifywait died during capture: {tail.strip() or 'no output'}",
                )
                return 0
            if self.truncated and child is not None:
                self._terminate_child()
                self.stop_requested = True
            self._rotate()
            time.sleep(0.2)
        self._terminate_child()
        with self._write_lock:
            self._seal_part()
        self.write_status("complete", "observer stopped after agent window")
        return 0


def cmd_capture(args: argparse.Namespace) -> int:
    supervisor = CaptureSupervisor(
        args.out,
        args.watch or [],
        not args.no_loss_events,
        seal_bytes=args.chunk_bytes,
        seal_sec=args.chunk_sec,
        max_bytes=args.max_bytes,
    )
    return supervisor.run()


def build_parser() -> argparse.ArgumentParser:
    """CLI parser: ``probe``/``snapshot``/``capture`` subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="report sandbox discovery as JSON")
    probe.add_argument("--candidate", action="append", default=[],
                       help="extra path to check")
    probe.set_defaults(func=cmd_probe)
    snapshot = sub.add_parser("snapshot", help="hash protected roots as JSON")
    snapshot.add_argument("--root", action="append", default=[],
                          help="PATH:CATEGORY full walk")
    snapshot.add_argument("--shallow-root", action="append", default=[],
                          help="PATH:CATEGORY presence-only")
    snapshot.add_argument("--max-bytes", type=int, default=MAX_HASH_BYTES)
    snapshot.add_argument("--max-files", type=int, default=MAX_SNAPSHOT_FILES)
    snapshot.set_defaults(func=cmd_snapshot)
    capture = sub.add_parser("capture", help="supervise inotifywait for one window")
    capture.add_argument("--out", required=True, help="status/READY/chunks directory")
    capture.add_argument("--watch", action="append", default=[], help="directory to watch")
    capture.add_argument("--no-loss-events", action="store_true",
                         help="skip explicit q_overflow/ignored/unmount selection")
    capture.add_argument("--chunk-bytes", type=int, default=SEAL_BYTES,
                         help="seal a chunk once it reaches this many bytes")
    capture.add_argument("--chunk-sec", type=float, default=SEAL_SEC,
                         help="seal a non-empty chunk at least this often")
    capture.add_argument("--max-bytes", type=int, default=MAX_STREAM_BYTES,
                         help="sandbox-side cap on the framed stream per window")
    capture.set_defaults(func=cmd_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch to the requested subcommand."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
