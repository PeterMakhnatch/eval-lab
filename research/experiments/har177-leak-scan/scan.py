"""HAR-177 image .git leak scan.

Containment question per task image: does the baked-in repo state retain
commit history past the task base commit (later commits present in the
image's ``.git``, whether on refs or only as unreachable objects)?

Containment rule (binding for this module): report whether post-base
history exists plus identifying metadata -- counts, the first few SHAs
and their subject lines, and whether anything is on a ref. NEVER quote
file contents, blobs, trees, or diffs of those commits. Every git
command used here is metadata-only (see ``_GIT_ALLOW``); the registry
fetch stream-extracts only ``<repo>/.git/**`` from each layer and skips
all worktree files without materializing the image.

Method, cheapest first: ``mirror.gcr.io`` pull-through for Docker Hub
manifests/blobs (200s unauthenticated for these images), otherwise
Docker Hub anonymous, otherwise the row is ``unscanned`` with the
blocker recorded. No ``docker pull`` of full images anywhere.

Usage (local validation, $0)::

    python scan.py validate --snapshot <task-store>/tasks --workers 4
    python scan.py scan --ledger <ledger.csv> --snapshot <tasks> \\
        --variants <variants> --out leak_scan.csv --limit 10

The streamer also records tar-member mtimes for repo-rooted non-``.git``
files and flags minute clusters (≥3 files sharing a minute later than the
bulk): post-fix touches the Xiaomi images bake in. New CSV columns carry
the finding; the git-leak columns are byte-identical to before.

Full sweep (paid; needs the parent's explicit per-slice OK first)::

    python scan.py scan --ledger ... --mode modal --out leak_scan.csv

Staged census (dry-run default; paid launch waits on Peter in chat)::

    python scan.py sweep --ledger <ledger.csv> --snapshot <tasks> \\
        --variants <variants> --out leak_scan.csv
    python scan.py sweep --ledger ... --execute   # only with approval
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime

REPO = "xiaomimimo/mimo-v2.6-rl-oss"
MIRROR = "https://mirror.gcr.io"
DOCKERHUB = "https://registry-1.docker.io"
DOCKER_TOKEN = "https://auth.docker.io/token"

MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.manifest.v1+json"
)

#: The only git subcommands this module may run. All are metadata-only:
#: object names, ref names, counts, and commit subject lines. No blobs,
#: trees, file contents, patches, or diffs are ever requested.
_GIT_ALLOW = frozenset({"rev-parse", "for-each-ref", "log", "fsck", "show", "count-objects"})

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"@sha256:([0-9a-f]{64})")
CWD_RE = re.compile(r"^CWD=(\S+)", re.MULTILINE)

SAMPLE_N = 5
MAX_GIT_BYTES = 8 * 1024**3  # sanity cap on the extracted .git subset

CSV_COLUMNS = (
    "task_id",
    "run",
    "ledger_digest",
    "image_digest",
    "has_future_history",
    "needs_repair",
    "git_present",
    "repo_path",
    "base_commit",
    "beyond_base_refs",
    "unreachable_commits",
    "on_ref",
    "sample_shas",
    "sample_subjects",
    "method",
    "error",
    "scanned_at",
    # Mtime-cluster columns (appended; the git-leak columns above are frozen).
    "mtime_cluster",
    "mtime_cluster_minute",
    "mtime_cluster_files",
    "mtime_cluster_paths",
    "mtime_files_scanned",
)

#: HAR-161 probe tasks (known-positive: all 10 leaked) with the oracle
#: image-check facts to reproduce before scaling. Group "refs": history
#: past base readable on refs; group "unreachable": no ref past base,
#: leak only as unreachable objects.
HAR161_PROBES = (
    "000552",
    "001269",
    "002139",
    "002391",
    "002402",
    "002486",
    "002552",
    "002864",
    "002938",
    "000792",
)
HAR161_REFS = frozenset({"000552", "002139", "002391", "002486", "002938"})
HAR161_UNREACHABLE = frozenset({"000792", "001269", "002402", "002552", "002864"})
#: Exact unreachable-commit counts HAR-161 reported (images are
#: digest-pinned, so these are stable).
HAR161_UNREACHABLE_EXACT = {"001269": 192, "002402": 696, "002552": 12}
#: Unreachable fix-commit prefixes HAR-161's exploit runs read.
HAR161_FIX_PREFIXES = {
    "002402": ("56f63eb6",),
    "002552": ("e88159fb", "edb06c52"),
}


# --------------------------------------------------------------------------
# Pure helpers (no network, no subprocess): unit-tested.
# --------------------------------------------------------------------------


def parse_image_digest(dockerfile_text: str) -> str | None:
    """Image digest named by a task ``environment/Dockerfile``."""
    match = DIGEST_RE.search(dockerfile_text)
    return f"sha256:{match.group(1)}" if match else None


def parse_setup_cwd(setup_text: str) -> str:
    """Working directory the setup script checks out (default ``/testbed``)."""
    match = CWD_RE.search(setup_text)
    return match.group(1) if match else "/testbed"


def parse_manifest(body: bytes) -> dict:
    """``(config, layers)`` from a manifest document; raises when layerless."""
    doc = json.loads(body)
    layers = [entry["digest"] for entry in doc.get("layers", [])]
    if not layers:
        raise ValueError("manifest has no layers")
    return {"config": doc.get("config", {}).get("digest", ""), "layers": layers}


def sanitize_subject(text: str, limit: int = 200) -> str:
    """One-line, separator-safe commit subject (metadata only, never content)."""
    flat = " ".join(text.split())
    flat = flat.replace(";", ",").replace("|", "/")
    return flat[:limit]


def verdict(has_future: bool | None) -> tuple[str, str]:
    """``(has_future_history, needs_repair)`` CSV values from the analysis."""
    if has_future is None:
        return "unscanned", "false"
    return ("yes", "true") if has_future else ("no", "false")


def build_row(
    *,
    task_id: str,
    run: str,
    ledger_digest: str,
    image_digest: str,
    base_commit: str = "",
    beyond: int = 0,
    unreachable: int = 0,
    samples: list[tuple[str, str]] | None = None,
    git_present: bool = True,
    repo_path: str = "",
    method: str = "",
    error: str = "",
    mtime: dict | None = None,
) -> dict:
    """One CSV row from the git analysis. ``samples`` are ``(sha, subject)``.

    ``mtime`` is a :func:`find_mtime_cluster` result (or ``None`` when the
    streamer never ran, e.g. error rows); its fields land in the appended
    mtime-cluster columns without touching the git-leak columns above.
    """
    samples = samples or []
    if error:
        history, repair = verdict(None)
        on_ref = "unknown"
    else:
        history, repair = verdict(git_present and (beyond > 0 or unreachable > 0))
        on_ref = "yes" if beyond > 0 else "no"
    mtime = mtime or {}
    return {
        "task_id": task_id,
        "run": run,
        "ledger_digest": ledger_digest,
        "image_digest": image_digest,
        "has_future_history": history,
        "needs_repair": repair,
        "git_present": "true" if git_present else "false",
        "repo_path": repo_path,
        "base_commit": base_commit,
        "beyond_base_refs": str(beyond),
        "unreachable_commits": str(unreachable),
        "on_ref": on_ref,
        "sample_shas": ";".join(sha for sha, _ in samples[:SAMPLE_N]),
        "sample_subjects": ";".join(sanitize_subject(s) for _, s in samples[:SAMPLE_N]),
        "method": method,
        "error": error,
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mtime_cluster": mtime.get("flag", "unknown"),
        "mtime_cluster_minute": mtime.get("cluster_minute", ""),
        "mtime_cluster_files": str(mtime.get("cluster_files", "")),
        "mtime_cluster_paths": mtime.get("cluster_paths", ""),
        "mtime_files_scanned": str(mtime.get("files_scanned", "")),
    }


# --------------------------------------------------------------------------
# Registry client (mirror.gcr.io first, Docker Hub anonymous fallback).
# --------------------------------------------------------------------------


@dataclass
class Registry:
    """Minimal pull-through client that streams blobs without materializing."""

    repo: str = REPO
    mirror: str = MIRROR
    timeout: int = 60
    tries: int = 3
    _hub_token: str | None = field(default=None, repr=False)

    def _get(self, url: str, headers: dict | None = None) -> tuple[bytes, int]:
        last: Exception | None = None
        for attempt in range(self.tries):
            try:
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read(), resp.status
            except Exception as exc:
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"GET {url} failed after {self.tries} tries: {last}")

    def manifest(self, digest: str) -> tuple[dict, str]:
        """Manifest document; returns ``(parsed, method)``."""
        path = f"/v2/{self.repo}/manifests/{digest}"
        try:
            body, _ = self._get(self.mirror + path, {"Accept": MANIFEST_ACCEPT})
            return parse_manifest(body), "mirror.gcr.io-stream"
        except Exception as mirror_err:
            token = self._hub_auth()
            body, _ = self._get(
                DOCKERHUB + path,
                {"Accept": MANIFEST_ACCEPT, "Authorization": f"Bearer {token}"},
            )
            return parse_manifest(body), f"dockerhub-stream (mirror failed: {mirror_err})"

    def _hub_auth(self) -> str:
        if self._hub_token is None:
            scope = f"repository:{self.repo}:pull"
            url = f"{DOCKER_TOKEN}?service=registry.docker.io&scope={scope}"
            body, _ = self._get(url)
            self._hub_token = json.loads(body)["token"]
        return self._hub_token

    def open_blob(self, digest: str) -> tuple:
        """Open a blob stream; the caller reads and closes it."""
        last: Exception | None = None
        for attempt in range(self.tries):
            try:
                url = f"{self.mirror}/v2/{self.repo}/blobs/{digest}"
                req = urllib.request.Request(url)
                return urllib.request.urlopen(req, timeout=self.timeout), ("mirror.gcr.io-stream")
            except Exception as exc:
                last = exc
                time.sleep(2**attempt)
        try:
            token = self._hub_auth()
            url = f"{DOCKERHUB}/v2/{self.repo}/blobs/{digest}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            return urllib.request.urlopen(req, timeout=self.timeout), (
                f"dockerhub-stream (mirror failed: {last})"
            )
        except Exception as exc:
            raise RuntimeError(
                f"blob {digest}: mirror failed ({last}); hub failed ({exc})"
            ) from exc


# --------------------------------------------------------------------------
# Layer streaming: extract only <repo>/.git/** to a directory.
# --------------------------------------------------------------------------


def tar_git_root(name: str) -> tuple[str, str] | None:
    """``(repo_root, relpath)`` for a ``<root>/.git/<rel>`` tar member."""
    clean = name
    while clean.startswith("./"):
        clean = clean[len("./") :]
    clean = clean.lstrip("/")
    if clean.startswith(".git/"):
        return "", clean[len(".git/") :]
    marker = "/.git/"
    idx = clean.find(marker)
    if idx <= 0:
        return None
    return clean[:idx], clean[idx + len(marker) :]


def _root_slug(root: str) -> str:
    return root.replace("/", "_") if root else "_root"


def extract_git_subset(
    stream: io.RawIOBase | io.BufferedIOBase,
    dest_dir: str,
    *,
    mtime_sink: list | None = None,
) -> tuple[dict[str, tuple[int, int]], int]:
    """Stream one gzip layer; write every ``<root>/.git/**`` subset to disk.

    Returns ``(roots, git_bytes)`` where ``roots`` maps each repo root to
    ``(files, bytes)`` in first-seen order, so callers can analyze the
    setup working-directory root while still reporting independent repos
    elsewhere in the image. Each root lands in ``dest_dir/<slug>/``; later
    entries overwrite earlier ones, matching image-layer overlay order.
    Raises ``OverflowError`` past ``MAX_GIT_BYTES`` across all roots.

    When ``mtime_sink`` is a list, every regular-file tar member that is
    NOT a ``.git`` member is appended as ``(cleaned_name, mtime_epoch)``.
    Only header metadata is recorded: no worktree file is read or written,
    so the return value and the extracted subset stay byte-identical to a
    sink-less call. Callers collapse repeats per path (later layers win)
    and filter to the analyzed repo root before clustering.
    """
    roots: dict[str, list[int]] = {}
    git_bytes = 0
    with gzip.GzipFile(fileobj=stream) as gunzip, tarfile.open(fileobj=gunzip, mode="r|*") as tar:
        for member in tar:
            found = tar_git_root(member.name)
            if found is None:
                if mtime_sink is not None and member.isfile():
                    mtime_sink.append((_clean_tar_name(member.name), int(member.mtime)))
                continue
            member_root, rel = found
            if not rel:
                continue
            base = os.path.join(dest_dir, _root_slug(member_root))
            target = os.path.normpath(os.path.join(base, rel))
            if target != base and not target.startswith(base + os.sep):
                continue  # path traversal outside the scratch dir; skip
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                parent = os.path.dirname(target)
                os.makedirs(parent or base, exist_ok=True)
                size = 0
                with open(target, "wb") as handle:
                    while chunk := extracted.read(1024 * 1024):
                        size += len(chunk)
                        git_bytes += len(chunk)
                        if git_bytes > MAX_GIT_BYTES:
                            raise OverflowError(f".git subset exceeds {MAX_GIT_BYTES} bytes")
                        handle.write(chunk)
                entry = roots.setdefault(member_root, [0, 0])
                entry[0] += 1
                entry[1] += size
    return {root: (files, nbytes) for root, (files, nbytes) in roots.items()}, git_bytes

# --------------------------------------------------------------------------
# Mtime-cluster detection: post-fix touches baked into image worktrees.
# --------------------------------------------------------------------------

#: A later-than-bulk shared minute needs this many files to flag. The
#: confirmed 002402 fix touch is 5 files; 3 keeps smaller touches visible
#: while ignoring one-off stragglers.
MTIME_CLUSTER_MIN_FILES = 3
#: Cap on the ``;``-joined cluster paths in the CSV (column stays readable).
MTIME_SAMPLE_PATHS = 10
#: Directory components whose mtimes reflect build/test residue, not source.
MTIME_EXCLUDE_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        ".git",
    }
)
#: File endings whose mtimes reflect build residue, not source.
MTIME_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".egg-info")


def _clean_tar_name(name: str) -> str:
    """Tar member name without ``./`` prefixes or a leading ``/``."""
    clean = name
    while clean.startswith("./"):
        clean = clean[len("./") :]
    return clean.lstrip("/")


def is_mtime_candidate(rel: str) -> bool:
    """Whether a repo-relative path counts for clustering (no caches)."""
    if not rel or rel.startswith(".git/"):
        return False
    if rel.endswith(MTIME_EXCLUDE_SUFFIXES):
        return False
    return not (
        MTIME_EXCLUDE_DIRS.intersection(rel.split("/"))
        or any(part.endswith(".egg-info") for part in rel.split("/"))
    )


def collapse_mtimes(entries: list[tuple[str, int]], repo_root: str) -> dict[str, int]:
    """Last-seen mtime per repo-relative path under ``repo_root``.

    ``entries`` are raw ``(cleaned_name, epoch)`` sink records across all
    layers; later layers overwrite earlier ones, so repeats collapse to
    the last observation, matching image overlay order.
    """
    prefix = repo_root.strip("/")
    collapsed: dict[str, int] = {}
    for name, epoch in entries:
        if prefix:
            if name != prefix and not name.startswith(prefix + "/"):
                continue
            rel = name[len(prefix) :].lstrip("/")
        else:
            rel = name
        if rel and is_mtime_candidate(rel):
            collapsed[rel] = epoch
    return collapsed


def _minute_iso(epoch_minute: int) -> str:
    """UTC ``YYYY-MM-DDTHH:MMZ`` for a minute-floored epoch."""
    return datetime.fromtimestamp(epoch_minute * 60, UTC).strftime("%Y-%m-%dT%H:%MZ")


def find_mtime_cluster(mtimes: dict[str, int]) -> dict:
    """Flag a late shared-minute cluster among per-path mtimes.

    The bulk minute is the one holding the most files (ties go to the
    earliest); any strictly later minute with at least
    ``MTIME_CLUSTER_MIN_FILES`` files flags, reporting the latest such
    minute. Uniform worktrees (e.g. the 002552 control) never flag.
    """
    files_scanned = len(mtimes)
    if not mtimes:
        return {
            "flag": "unknown",
            "cluster_minute": "",
            "cluster_files": "",
            "cluster_paths": "",
            "files_scanned": files_scanned,
        }
    by_minute: dict[int, list[str]] = {}
    for rel, epoch in mtimes.items():
        by_minute.setdefault(int(epoch // 60), []).append(rel)
    biggest = max(len(paths) for paths in by_minute.values())
    bulk = min(minute for minute, paths in by_minute.items() if len(paths) == biggest)
    late = sorted(
        minute
        for minute, paths in by_minute.items()
        if minute > bulk and len(paths) >= MTIME_CLUSTER_MIN_FILES
    )
    if not late:
        return {
            "flag": "no",
            "cluster_minute": "",
            "cluster_files": "",
            "cluster_paths": "",
            "files_scanned": files_scanned,
        }
    cluster = late[-1]
    paths = sorted(by_minute[cluster])
    return {
        "flag": "yes",
        "cluster_minute": _minute_iso(cluster),
        "cluster_files": len(paths),
        "cluster_paths": ";".join(paths[:MTIME_SAMPLE_PATHS]),
        "files_scanned": files_scanned,
    }


_GIT_SLOW_TIMEOUT = 600  # log/fsck/show over large packs on 1-CPU workers


# --------------------------------------------------------------------------
# Git analysis (metadata-only commands; see _GIT_ALLOW).
# --------------------------------------------------------------------------


def _git_env(git_dir: str) -> dict:
    return {
        **os.environ,
        "GIT_DIR": git_dir,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }


def _git(git_dir: str, *args: str, timeout: int = 120, check: bool = True) -> str:
    if not args or args[0] not in _GIT_ALLOW:
        raise ValueError(f"git command not on the metadata allowlist: {args[:1]}")
    if args[0] in ("show", "log"):
        # Metadata only: no patches, no <rev>:<path> or <rev> -- <path> reads.
        if "-p" in args or "--patch" in args or "--" in args:
            raise ValueError(f"git {args[0]} with content flags is refused: {args[1:]}")
        if any(":" in arg and not arg.startswith("--format=") for arg in args[1:]):
            raise ValueError(f"git {args[0]} with a path read is refused: {args[1:]}")
    if args[0] == "show":
        # Commit subjects only: suppress the diff and name SHAs explicitly.
        if "-s" not in args:
            raise ValueError(f"git show without -s is refused: {args[1:]}")
        for arg in args[1:]:
            if not arg.startswith("-") and not SHA_RE.match(arg):
                raise ValueError(f"git show of a non-SHA is refused: {arg!r}")
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=timeout, env=_git_env(git_dir)
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def drop_packed_refs(text: str, bad: set[str]) -> str:
    """``packed-refs`` content with unresolvable refs (and peel lines) removed."""
    kept: list[str] = []
    skipping_peeled = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("^"):
            if skipping_peeled:
                skipping_peeled = False
                continue
            kept.append(line)
            continue
        skipping_peeled = False
        parts = stripped.split()
        if len(parts) == 2 and SHA_RE.match(parts[0]) and parts[1] in bad:
            skipping_peeled = True
            continue
        kept.append(line)
    return "".join(kept)


def _filesystem_refs(git_dir: str) -> tuple[list[str], set[str]]:
    """Refnames from loose files and ``packed-refs``.

    ``for-each-ref`` silently hides dangling symrefs while ``fsck`` still
    dies on them, so quarantine enumerates the filesystem directly.
    Returns ``(loose_refs, packed_refs)``.
    """
    loose: list[str] = []
    refs_root = os.path.join(git_dir, "refs")
    for root, dirs, files in os.walk(refs_root):
        dirs[:] = [d for d in dirs if d != ".leakscan-quarantine"]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), git_dir)
            loose.append(rel.replace(os.sep, "/"))
    packed: set[str] = set()
    packed_path = os.path.join(git_dir, "packed-refs")
    if os.path.isfile(packed_path):
        for line in read_text(packed_path).splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and SHA_RE.match(parts[0]) and not parts[1].startswith("^"):
                packed.add(parts[1])
    return loose, packed


def quarantine_dangling_refs(git_dir: str) -> list[str]:
    """Move refs ``rev-parse`` cannot resolve aside; return their names.

    Dangling symrefs and zero pointers (e.g. ``refs/remotes/origin/HEAD``
    aimed at a branch that was never fetched) make ``git fsck`` exit
    nonzero while still printing valid object data. They point at no
    commit, so quarantining them cannot change the beyond-base count;
    resolvable refs are never touched. Operates on the extracted scratch
    copy only, never on an image.
    """
    loose_refs, packed_refs = _filesystem_refs(git_dir)
    quarantine = os.path.join(git_dir, ".leakscan-quarantine")
    bad: list[str] = []
    packed_bad: set[str] = set()
    for ref in loose_refs:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(git_dir),
        )
        if proc.returncode == 0 and SHA_RE.match(proc.stdout.strip()):
            continue
        bad.append(ref)
        loose = os.path.join(git_dir, *ref.split("/"))
        dest = os.path.join(quarantine, *ref.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(loose, dest)
    for ref in sorted(packed_refs):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(git_dir),
        )
        if proc.returncode == 0 and SHA_RE.match(proc.stdout.strip()):
            continue
        bad.append(ref)
        packed_bad.add(ref)
    if packed_bad:
        packed_path = os.path.join(git_dir, "packed-refs")
        if os.path.isfile(packed_path):
            os.makedirs(quarantine, exist_ok=True)
            original = read_text(packed_path)
            with open(os.path.join(quarantine, "packed-refs.orig"), "w") as handle:
                handle.write(original)
            with open(packed_path, "w") as handle:
                handle.write(drop_packed_refs(original, packed_bad))
    return bad


def _nonempty_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _nonempty_dir(path: str) -> bool:
    return os.path.isdir(path) and bool(os.listdir(path))


def analyze_git_dir(git_dir: str, *, sample_n: int = SAMPLE_N) -> dict:
    """Metadata-only analysis of an extracted ``.git`` directory.

    Mirrors ``setup.sh`` semantics: ``BASE`` is ``git rev-parse HEAD`` and
    ``beyond`` counts ``git log --all --not BASE``. Returns counts plus
    ``(sha, subject)`` samples; never file contents or diffs.
    """
    base = _git(git_dir, "rev-parse", "HEAD").strip()
    if not SHA_RE.match(base):
        raise RuntimeError(f"could not resolve the base commit: {base!r}")
    quarantined = quarantine_dangling_refs(git_dir)
    refs_out = _git(git_dir, "for-each-ref", "--format=%(objectname) %(refname)")
    ref_count = sum(1 for line in refs_out.splitlines() if line.strip())
    beyond: list[tuple[str, str]] = []
    log_out = _git(
        git_dir, "log", "--all", "--not", base, "--format=%H%x00%s", timeout=_GIT_SLOW_TIMEOUT
    )
    for line in log_out.splitlines():
        sha, _, subject = line.partition("\x00")
        if SHA_RE.match(sha):
            beyond.append((sha, subject))
    unreachable_shas: list[str] = []
    fsck_out = _git(git_dir, "fsck", "--unreachable", "--no-reflogs", timeout=_GIT_SLOW_TIMEOUT)
    for line in fsck_out.splitlines():
        parts = line.split()
        if (
            line.startswith("unreachable commit ")
            and len(parts) >= 3
            and SHA_RE.match(parts[2])
            and parts[2] not in unreachable_shas
        ):
            unreachable_shas.append(parts[2])
    # Beyond-ref commits sample first (they sit on a ref), then unreachable.
    samples = list(beyond[:sample_n])
    need = sample_n - len(samples)
    if need > 0 and unreachable_shas:
        subjects: dict[str, str] = {}
        batch = unreachable_shas[:need]
        for start in range(0, len(batch), 200):
            out = _git(
                git_dir,
                "show",
                "-s",
                "--format=%H%x00%s",
                *batch[start : start + 200],
                timeout=_GIT_SLOW_TIMEOUT,
            )
            for line in out.splitlines():
                sha, _, subject = line.partition("\x00")
                if SHA_RE.match(sha):
                    subjects[sha] = subject
        samples += [(sha, subjects.get(sha, "")) for sha in batch]
    try:
        counts = _git(git_dir, "count-objects", "-v")
    except RuntimeError:
        counts = ""
    return {
        "base": base,
        "refs": ref_count,
        "quarantined": quarantined,
        "beyond": len(beyond),
        "unreachable": len(unreachable_shas),
        "unreachable_shas": unreachable_shas,
        "samples": samples,
        "counts": counts,
        "shallow": os.path.exists(os.path.join(git_dir, "shallow")),
        "has_alternates": _nonempty_file(os.path.join(git_dir, "objects", "info", "alternates")),
        "has_worktrees": _nonempty_dir(os.path.join(git_dir, "worktrees"))
        or os.path.exists(os.path.join(git_dir, "commondir")),
    }


# --------------------------------------------------------------------------
# Per-task scan.
# --------------------------------------------------------------------------


def read_text(path: str) -> str:
    with open(path, errors="replace") as handle:
        return handle.read()


def resolve_package(row: dict, *, snapshot_dir: str, variants_dir: str) -> str:
    """Local package dir for a ledger row (snapshot task or variant package)."""
    if row["run"] == "original":
        return os.path.join(snapshot_dir, row["task_id"])
    short = row["run_digest"].removeprefix("sha256:")[:12]
    return os.path.join(variants_dir, f"mimo-v2.6-rl__{row['task_id']}", short)


def resolve_image(
    row: dict, *, snapshot_dir: str, variants_dir: str
) -> tuple[str | None, str, str]:
    """``(image_digest|None, expect_root, resolve_error)`` for a ledger row."""
    package = resolve_package(row, snapshot_dir=snapshot_dir, variants_dir=variants_dir)
    try:
        dockerfile = read_text(os.path.join(package, "environment", "Dockerfile"))
    except OSError as exc:
        return None, "testbed", f"no package Dockerfile: {exc}"
    image = parse_image_digest(dockerfile)
    if image is None:
        return None, "testbed", "Dockerfile names no digest"
    try:
        setup_text = read_text(os.path.join(package, "environment", "setup", "setup.sh"))
        expect_root = parse_setup_cwd(setup_text).lstrip("/")
    except OSError:
        expect_root = "testbed"
    return image, expect_root, ""


def scan_image(
    *,
    task_id: str,
    run: str,
    ledger_digest: str,
    image_digest: str,
    registry: Registry,
    work_root: str,
    sample_n: int = SAMPLE_N,
    prefer_root: str = "",
) -> dict:
    """Scan one image by digest; always returns a CSV-ready row dict.

    Portable: needs only the registry and a scratch dir, so the same
    function runs locally and on Modal workers. ``prefer_root`` is the
    setup working directory (e.g. ``testbed``): when the image holds
    several repos, that one is analyzed and the rest are reported as
    ``+other-roots`` evidence the strip repair does not cover.
    """
    started = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="leakscan-", dir=work_root)
    methods: list[str] = []
    repo_root = ""
    others: list[str] = []
    try:
        manifest, manifest_method = registry.manifest(image_digest)
        methods.append(manifest_method)
        roots: dict[str, list[int]] = {}
        mtime_entries: list[tuple[str, int]] = []
        for layer in manifest["layers"]:
            resp, blob_method = registry.open_blob(layer)
            try:
                layer_roots, _layer_bytes = extract_git_subset(resp, tmp, mtime_sink=mtime_entries)
            finally:
                resp.close()
            if blob_method not in methods:
                methods.append(blob_method)
            for root, (files, nbytes) in layer_roots.items():
                entry = roots.setdefault(root, [0, 0])
                entry[0] += files
                entry[1] += nbytes
        if not roots:
            result = build_row(
                task_id=task_id,
                run=run,
                ledger_digest=ledger_digest,
                image_digest=image_digest,
                git_present=False,
                method="+".join(methods),
                error="",
                mtime=find_mtime_cluster(collapse_mtimes(mtime_entries, prefer_root)),
            )
            result["_elapsed_s"] = round(time.monotonic() - started, 1)
            return result
        if prefer_root in roots:
            repo_root = prefer_root
        else:
            repo_root = max(roots, key=lambda root: roots[root][0])
        others = sorted(root for root in roots if root != repo_root)
        if others:
            methods.append(f"other-roots:{len(others)}")
        mtime = find_mtime_cluster(collapse_mtimes(mtime_entries, repo_root))
        analysis = analyze_git_dir(os.path.join(tmp, _root_slug(repo_root)), sample_n=sample_n)
    except Exception as exc:
        result = build_row(
            task_id=task_id,
            run=run,
            ledger_digest=ledger_digest,
            image_digest=image_digest,
            repo_path=repo_root,
            method="+".join(methods),
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        result["_elapsed_s"] = round(time.monotonic() - started, 1)
        shutil.rmtree(tmp, ignore_errors=True)
        return result
    if analysis["shallow"]:
        methods.append("shallow-clone")
    if analysis["quarantined"]:
        methods.append(f"quarantined-refs:{len(analysis['quarantined'])}")
    if analysis["has_alternates"]:
        methods.append("alternates")
    if analysis["has_worktrees"]:
        methods.append("worktrees")
    result = build_row(
        task_id=task_id,
        run=run,
        ledger_digest=ledger_digest,
        image_digest=image_digest,
        base_commit=analysis["base"],
        beyond=analysis["beyond"],
        unreachable=analysis["unreachable"],
        samples=analysis["samples"],
        repo_path=repo_root or "/",
        method="+".join(methods),
        error="",
        mtime=mtime,
    )
    result["_elapsed_s"] = round(time.monotonic() - started, 1)
    result["_unreachable_shas"] = analysis["unreachable_shas"]
    result["_quarantined"] = analysis["quarantined"]
    result["_other_roots"] = others
    shutil.rmtree(tmp, ignore_errors=True)
    return result


def scan_task(
    row: dict,
    *,
    snapshot_dir: str,
    variants_dir: str,
    registry: Registry,
    work_root: str,
) -> dict:
    """Scan one ledger row's image; always returns a CSV-ready row dict."""
    image, expect_root, resolve_error = resolve_image(
        row, snapshot_dir=snapshot_dir, variants_dir=variants_dir
    )
    if image is None:
        return build_row(
            task_id=row["task_id"],
            run=row["run"],
            ledger_digest=row["run_digest"],
            image_digest="",
            method="",
            error=resolve_error,
        )
    return scan_image(
        task_id=row["task_id"],
        run=row["run"],
        ledger_digest=row["run_digest"],
        image_digest=image,
        registry=registry,
        work_root=work_root,
        prefer_root=expect_root,
    )


def load_usable_rows(ledger_path: str) -> list[dict]:
    with open(ledger_path, newline="") as handle:
        return [row for row in csv.DictReader(handle) if row["status"] == "usable"]


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})


def append_spend_log(path: str, event: dict) -> None:
    event = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}
    with open(path, "a") as handle:
        handle.write(json.dumps(event) + "\n")


# --------------------------------------------------------------------------
# Random-sample sweep (HAR-177 option C): prevalence with a Wilson bound.
# --------------------------------------------------------------------------


def draw_sample(task_ids: list[str], n: int, seed: int) -> list[str]:
    """Seeded random sample of task ids, returned in sorted order."""
    rng = random.Random(seed)
    if n > len(task_ids):
        raise ValueError(f"sample {n} exceeds population {len(task_ids)}")
    return sorted(rng.sample(sorted(task_ids), n))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n <= 0:
        return (0.0, 0.0)
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside [0, {n}]")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_reuse(paths: list[str]) -> dict[str, dict]:
    """Prior scan rows keyed by task_id (later files win; unscanned never reused)."""
    reused: dict[str, dict] = {}
    for path in paths:
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("has_future_history") in ("yes", "no"):
                    reused[row["task_id"]] = row
    return reused


def prevalence(rows: list[dict]) -> dict:
    """Counts plus the Wilson interval over scanned (non-unscanned) rows."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["has_future_history"]] = counts.get(row["has_future_history"], 0) + 1
    scanned = counts.get("yes", 0) + counts.get("no", 0)
    lo, hi = wilson(counts.get("yes", 0), scanned)
    return {"counts": counts, "scanned": scanned, "yes": counts.get("yes", 0), "lo": lo, "hi": hi}


# --------------------------------------------------------------------------
# HAR-161 validation (10 probe tasks): stop and report on any disagreement.
# --------------------------------------------------------------------------


def validate_result(short_id: str, result: dict) -> list[str]:
    """Disagreements between a scan result and the HAR-161 known facts."""
    problems: list[str] = []
    if result["has_future_history"] != "yes":
        problems.append(
            f"{short_id}: expected has_future_history=yes, "
            f"got {result['has_future_history']} ({result['error']})"
        )
        return problems
    beyond = int(result["beyond_base_refs"])
    unreachable = int(result["unreachable_commits"])
    if short_id in HAR161_REFS and beyond <= 0:
        problems.append(f"{short_id}: HAR-161 saw history on refs, scan found beyond=0")
    if short_id in HAR161_UNREACHABLE:
        if beyond != 0:
            problems.append(
                f"{short_id}: HAR-161 saw unreachable-only leak, scan found beyond={beyond}"
            )
        if unreachable <= 0:
            problems.append(f"{short_id}: HAR-161 saw unreachable commits, scan found 0")
    if short_id in HAR161_UNREACHABLE_EXACT:
        want = HAR161_UNREACHABLE_EXACT[short_id]
        if unreachable != want:
            problems.append(
                f"{short_id}: HAR-161 reported {want} unreachable commits, scan found {unreachable}"
            )
    for prefix in HAR161_FIX_PREFIXES.get(short_id, ()):
        shas = result.get("_unreachable_shas", [])
        if not any(str(sha).startswith(prefix) for sha in shas):
            problems.append(f"{short_id}: HAR-161 fix commit {prefix}... not among unreachable")
    return problems


def run_validation(
    *,
    snapshot_dir: str,
    registry: Registry,
    work_root: str,
    workers: int = 4,
    out_csv: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Scan the 10 HAR-161 probe images; return ``(rows, problems)``."""
    rows = [
        {
            "task_id": f"format-code-task-{short}",
            "run": "original",
            "run_digest": "",
            "status": "usable",
        }
        for short in HAR161_PROBES
    ]
    # Validation scans the original snapshot image HAR-161 probed.
    results = _scan_concurrent(
        rows,
        snapshot_dir=snapshot_dir,
        variants_dir="",
        registry=registry,
        work_root=work_root,
        workers=workers,
    )
    problems: list[str] = []
    for row in results:
        short = row["task_id"].removeprefix("format-code-task-")
        problems.extend(validate_result(short, row))
    if out_csv is not None:
        write_csv(out_csv, results)
    return results, problems


def _scan_concurrent(
    rows: list[dict],
    *,
    snapshot_dir: str,
    variants_dir: str,
    registry: Registry,
    work_root: str,
    workers: int,
) -> list[dict]:
    def one(row: dict) -> dict:
        return scan_task(
            row,
            snapshot_dir=snapshot_dir,
            variants_dir=variants_dir,
            registry=registry,
            work_root=work_root,
        )

    ordered: list[dict | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(one, row): idx for idx, row in enumerate(rows)}
        for future in as_completed(future_to_idx):
            ordered[future_to_idx[future]] = future.result()
    return [row for row in ordered if row is not None]


# --------------------------------------------------------------------------
# Modal batch (paid; only with the parent's explicit per-slice OK).
# --------------------------------------------------------------------------

MODAL_APP_NAME = "har177-leak-scan"


def run_modal_batch(
    rows: list[dict],
    *,
    out_csv: str,
    spend_log: str,
    snapshot_dir: str,
    variants_dir: str,
    concurrency: int = 32,
    cpu: float = 1.0,
    memory: int = 1024,
) -> None:
    """Fan the sweep out to Modal; each task streams its own layers.

    MUST only be called after the parent confirms billing auth for the
    $0.60 slice budget. Image digests are resolved locally first, so
    workers need no task-store access. Records start/stop in the spend
    log; the authoritative cost comes from the Modal dashboard.
    """
    import modal  # lazy: tests and local runs never need it

    payloads: list[dict] = []
    for row in rows:
        image, expect_root, resolve_error = resolve_image(
            row, snapshot_dir=snapshot_dir, variants_dir=variants_dir
        )
        if image is None:
            payloads.append({"row": row, "image": None, "error": resolve_error})
        else:
            payloads.append(
                {
                    "row": {
                        "task_id": row["task_id"],
                        "run": row["run"],
                        "run_digest": row["run_digest"],
                    },
                    "image": image,
                    "prefer": expect_root,
                    "error": "",
                }
            )

    app = modal.App(MODAL_APP_NAME)
    this_file = os.path.abspath(__file__)
    modal_image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("git")
        .add_local_file(this_file, "/root/scan.py")
    )

    @app.function(
        image=modal_image,
        timeout=1800,
        cpu=cpu,
        memory=memory,
        serialized=True,
        max_containers=concurrency,
    )
    def scan_one(payload: dict) -> dict:
        sys.path.insert(0, "/root")
        import scan as worker

        if not payload["image"]:
            return worker.build_row(
                task_id=payload["row"]["task_id"],
                run=payload["row"]["run"],
                ledger_digest=payload["row"]["run_digest"],
                image_digest="",
                method="",
                error=payload["error"],
            )
        reg = worker.Registry()
        with tempfile.TemporaryDirectory(prefix="leakscan-") as work:
            return worker.scan_image(
                task_id=payload["row"]["task_id"],
                run=payload["row"]["run"],
                ledger_digest=payload["row"]["run_digest"],
                image_digest=payload["image"],
                registry=reg,
                work_root=work,
                prefer_root=payload.get("prefer", ""),
            )

    append_spend_log(
        spend_log,
        {
            "event": "modal_batch_start",
            "tasks": len(payloads),
            "concurrency": concurrency,
            "app": MODAL_APP_NAME,
        },
    )
    started = time.monotonic()
    results: list[dict] = []
    with app.run():
        for result in scan_one.map(payloads):
            results.append(result)
    append_spend_log(
        spend_log,
        {
            "event": "modal_batch_stop",
            "tasks": len(results),
            "wall_s": round(time.monotonic() - started, 1),
            "note": "verify cost in the Modal dashboard against the $0.60 slice budget",
        },
    )
    write_csv(out_csv, results)


# --------------------------------------------------------------------------
# Staged full census (HAR-191 receipt pattern: plan by default, fenced batches).
# --------------------------------------------------------------------------

#: Hard worst-case ceiling for the whole 1,146-task census, cold pulls
#: included. Anchored to posted pilot evidence (spend.log
#: ``modal_billing_posted``): $0.03047 for 20 tasks / 24,517 MiB, i.e.
#: $1.2428e-6/MiB; the 2,594,600 MiB census projects to $3.23 byte-ratio.
#: The per-task worst case below ($0.0030 ≈ 2x the pilot mean, above the
#: $0.00286/task sample100 upper tail) reserves $3.44 worst case, inside
#: the cap with margin for one-time cold image builds (~$0.00015 posted).
SWEEP_CAP_USD = 3.50
SWEEP_BATCH_TASKS = 100
SWEEP_WORST_PER_TASK_USD = 0.0030


def plan_sweep(task_ids: list[str], *, batch_size: int = SWEEP_BATCH_TASKS) -> list[list[str]]:
    """Task ids chunked into sequential batches (sorted input, stable plan)."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    ordered = sorted(task_ids)
    return [ordered[start : start + batch_size] for start in range(0, len(ordered), batch_size)]


def sweep_reservation(n_tasks: int) -> float:
    """Worst-case dollar reservation for a batch of ``n_tasks``."""
    return round(n_tasks * SWEEP_WORST_PER_TASK_USD, 4)


def sweep_fence_ok(spent_worst: float, next_batch_tasks: int, *, cap: float = SWEEP_CAP_USD) -> bool:
    """Whether the next batch fits under the hard cap.

    ``spent_worst`` is the conservative spend so far (posted dashboard
    actuals where available, else the sum of prior batch reservations).
    Refusing here is the per-batch cost check: a breached fence stops
    the census before another paid batch launches.
    """
    return spent_worst + sweep_reservation(next_batch_tasks) <= cap + 1e-9


def run_sweep(
    args: argparse.Namespace, *, execute: bool = False
) -> int:
    """Print the staged census plan (default) or run it batch by batch.

    Dry-run prints every batch command plus the cost math and stages
    nothing. ``--execute`` runs the batches sequentially through the
    existing :func:`run_modal_batch` path, checking the spend fence
    before each batch and stopping (leaving prior batch CSVs in place)
    the moment the fence refuses. Paid launch still waits on Peter's
    in-chat approval; this function never grants it.
    """
    usable = load_usable_rows(args.ledger)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        usable = [row for row in usable if row["task_id"] in wanted]
    if args.limit:
        usable = usable[: args.limit]
    batches = plan_sweep([row["task_id"] for row in usable], batch_size=args.batch_size)
    total_worst = round(sum(sweep_reservation(len(batch)) for batch in batches), 4)
    plan = {
        "event": "sweep_plan",
        "tasks": len(usable),
        "batches": len(batches),
        "batch_size": args.batch_size,
        "cap_usd": args.cap_usd,
        "total_worst_usd": total_worst,
        "per_task_worst_usd": SWEEP_WORST_PER_TASK_USD,
        "basis": "posted pilot $0.03047/20 tasks; sample100 upper tail $0.00286/task",
        "execute": execute,
    }
    print(
        f"census plan: {len(usable)} tasks in {len(batches)} batches "
        f"(≤{args.batch_size}/batch), worst ${total_worst:.2f} vs cap ${args.cap_usd:.2f}"
    )
    for idx, batch in enumerate(batches):
        print(
            f"  batch {idx + 1}/{len(batches)}: {len(batch)} tasks "
            f"(worst ${sweep_reservation(len(batch)):.2f}) "
            f"{batch[0]}..{batch[-1]}"
        )
    if total_worst > args.cap_usd + 1e-9:
        print(f"STOP: plan worst ${total_worst:.2f} exceeds cap ${args.cap_usd:.2f}; not staged")
        append_spend_log(args.spend_log, plan | {"verdict": "over_cap"})
        return 1
    base_cmd = (
        f"python scan.py sweep --ledger {args.ledger} --snapshot {args.snapshot} "
        f"--variants {args.variants} --out {args.out} --batch-size {args.batch_size} "
        f"--cap-usd {args.cap_usd} --execute"
    )
    print(f"staged command (after approval, runs all batches with per-batch fence): {base_cmd}")
    append_spend_log(args.spend_log, plan | {"verdict": "staged" if execute else "dry_run"})
    if not execute:
        print("dry-run: nothing launched (paid launch waits on Peter in chat)")
        return 0
    by_id = {row["task_id"]: row for row in usable}
    spent_worst = 0.0
    done: list[dict] = []
    for idx, batch in enumerate(batches):
        if not sweep_fence_ok(spent_worst, len(batch), cap=args.cap_usd):
            append_spend_log(
                args.spend_log,
                {
                    "event": "sweep_abort_over_cap",
                    "batch": idx + 1,
                    "spent_worst_usd": round(spent_worst, 4),
                    "next_reservation_usd": sweep_reservation(len(batch)),
                    "cap_usd": args.cap_usd,
                    "completed_tasks": len(done),
                },
            )
            print(f"STOP: fence refused batch {idx + 1}; {len(done)} rows kept")
            break
        append_spend_log(
            args.spend_log,
            {
                "event": "sweep_batch_start",
                "batch": idx + 1,
                "batches": len(batches),
                "tasks": len(batch),
                "reservation_usd": sweep_reservation(len(batch)),
                "spent_worst_usd": round(spent_worst, 4),
                "cap_usd": args.cap_usd,
            },
        )
        part_out = f"{args.out}.batch{idx + 1:02d}.tmp"
        run_modal_batch(
            [by_id[tid] for tid in batch],
            out_csv=part_out,
            spend_log=args.spend_log,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            concurrency=args.concurrency,
            cpu=args.cpu,
            memory=args.memory,
        )
        with open(part_out, newline="") as handle:
            done += list(csv.DictReader(handle))
        os.remove(part_out)
        spent_worst = round(spent_worst + sweep_reservation(len(batch)), 4)
        append_spend_log(
            args.spend_log,
            {
                "event": "sweep_batch_stop",
                "batch": idx + 1,
                "completed_tasks": len(done),
                "spent_worst_usd": spent_worst,
                "note": "replace reservation with the dashboard actual before the next batch",
            },
        )
    done.sort(key=lambda row: row["task_id"])
    write_csv(args.out, done)
    print(f"census rows={len(done)} worst-spend-so-far=${spent_worst:.2f}")
    return 0


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------


def run_sample(args: argparse.Namespace, registry: Registry) -> int:
    """Seeded sample sweep: draw, reuse prior rows, scan the rest, report prevalence."""
    usable = load_usable_rows(args.ledger)
    by_id = {row["task_id"]: row for row in usable}
    drawn = draw_sample(list(by_id), args.n, args.seed)
    append_spend_log(
        args.spend_log,
        {
            "event": "sample_draw",
            "n": args.n,
            "seed": args.seed,
            "population": len(by_id),
            "tasks": drawn,
        },
    )
    reuse = load_reuse([path for path in args.reuse.split(",") if path]) if args.reuse else {}
    reused = {tid: reuse[tid] for tid in drawn if tid in reuse}
    need = [by_id[tid] for tid in drawn if tid not in reused]
    append_spend_log(
        args.spend_log,
        {"event": "sample_reuse", "reused": len(reused), "to_scan": len(need)},
    )
    results: list[dict] = list(reused.values())
    if need and args.mode == "modal":
        fresh_out = args.out + ".fresh.tmp"
        run_modal_batch(
            need,
            out_csv=fresh_out,
            spend_log=args.spend_log,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            concurrency=args.concurrency,
            cpu=args.cpu,
            memory=args.memory,
        )
        with open(fresh_out, newline="") as handle:
            results += list(csv.DictReader(handle))
        os.remove(fresh_out)
    elif need:
        results += _scan_concurrent(
            need,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            registry=registry,
            work_root=args.work_root,
            workers=args.workers,
        )
    results.sort(key=lambda row: row["task_id"])
    write_csv(args.out, results)
    report = prevalence(results)
    print(
        f"sample n={len(results)} seed={args.seed} "
        f"yes={report['yes']}/{report['scanned']} "
        f"prevalence={report['yes'] / report['scanned'] if report['scanned'] else 0:.3f} "
        f"wilson95=({report['lo']:.3f},{report['hi']:.3f}) "
        f"counts={report['counts']} reused={len(reused)}"
    )
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", required=True, help="snapshot tasks dir")
    parser.add_argument("--work-root", default=tempfile.gettempdir())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--spend-log", default="spend.log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    valid = sub.add_parser("validate", help="scan the 10 HAR-161 probe images")
    _common(valid)
    valid.add_argument("--out-csv", default=None)

    scan = sub.add_parser("scan", help="scan ledger rows to a CSV")
    _common(scan)
    scan.add_argument("--ledger", required=True)
    scan.add_argument("--variants", required=True)
    scan.add_argument("--out", required=True)
    scan.add_argument("--tasks", default="", help="comma-separated task_ids")
    scan.add_argument("--limit", type=int, default=0)
    scan.add_argument("--mode", choices=("local", "modal"), default="local")
    scan.add_argument("--concurrency", type=int, default=32)
    scan.add_argument("--cpu", type=float, default=1.0)
    scan.add_argument("--memory", type=int, default=1024)

    sample = sub.add_parser("sample", help="seeded sample sweep with Wilson prevalence")
    _common(sample)
    sample.add_argument("--ledger", required=True)
    sample.add_argument("--variants", required=True)
    sample.add_argument("--out", required=True)
    sample.add_argument("--n", type=int, default=100)
    sample.add_argument("--seed", type=int, required=True)
    sample.add_argument("--reuse", default="", help="comma-separated prior CSVs to reuse")
    sample.add_argument("--mode", choices=("local", "modal"), default="modal")
    sample.add_argument("--concurrency", type=int, default=32)
    sample.add_argument("--cpu", type=float, default=1.0)
    sample.add_argument("--memory", type=int, default=1024)

    sweep = sub.add_parser("sweep", help="staged full census: dry-run plan unless --execute")
    _common(sweep)
    sweep.add_argument("--ledger", required=True)
    sweep.add_argument("--variants", required=True)
    sweep.add_argument("--out", required=True)
    sweep.add_argument("--tasks", default="", help="comma-separated task_ids")
    sweep.add_argument("--limit", type=int, default=0)
    sweep.add_argument("--batch-size", type=int, default=SWEEP_BATCH_TASKS)
    sweep.add_argument("--cap-usd", type=float, default=SWEEP_CAP_USD)
    sweep.add_argument("--concurrency", type=int, default=32)
    sweep.add_argument("--cpu", type=float, default=1.0)
    sweep.add_argument("--memory", type=int, default=1024)
    sweep.add_argument(
        "--execute",
        action="store_true",
        help="run fenced Modal batches (only with Peter's in-chat approval)",
    )

    args = parser.parse_args(argv)
    os.makedirs(args.work_root, exist_ok=True)
    registry = Registry()

    if args.command == "validate":
        started = time.monotonic()
        append_spend_log(
            args.spend_log,
            {
                "event": "validation_start",
                "tasks": list(HAR161_PROBES),
                "mode": "local",
            },
        )
        results, problems = run_validation(
            snapshot_dir=args.snapshot,
            registry=registry,
            work_root=args.work_root,
            workers=args.workers,
            out_csv=args.out_csv,
        )
        append_spend_log(
            args.spend_log,
            {
                "event": "validation_stop",
                "seconds": round(time.monotonic() - started, 1),
                "problems": len(problems),
                "modal_spend_usd": 0,
            },
        )
        for row in results:
            print(
                f"{row['task_id']} {row['has_future_history']} "
                f"beyond={row['beyond_base_refs']} "
                f"unreachable={row['unreachable_commits']} "
                f"on_ref={row['on_ref']} {row.get('_elapsed_s', '?')}s "
                f"repo={row['repo_path']} {row['method']} {row['error']}"
            )
        if problems:
            print("\nVALIDATION DISAGREEMENTS (stop and report):")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("\nvalidation: all 10 agree with HAR-161")
        return 0

    if args.command == "sample":
        return run_sample(args, registry)

    if args.command == "sweep":
        return run_sweep(args, execute=args.execute)

    rows = load_usable_rows(args.ledger)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        rows = [row for row in rows if row["task_id"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    append_spend_log(
        args.spend_log,
        {
            "event": "scan_start",
            "tasks": len(rows),
            "mode": args.mode,
        },
    )
    started = time.monotonic()
    if args.mode == "modal":
        run_modal_batch(
            rows,
            out_csv=args.out,
            spend_log=args.spend_log,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            concurrency=args.concurrency,
            cpu=args.cpu,
            memory=args.memory,
        )
    else:
        results = _scan_concurrent(
            rows,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            registry=registry,
            work_root=args.work_root,
            workers=args.workers,
        )
        write_csv(args.out, results)
        counts: dict[str, int] = {}
        for row in results:
            counts[row["has_future_history"]] = counts.get(row["has_future_history"], 0) + 1
        print(f"rows={len(results)} {counts}")
    append_spend_log(
        args.spend_log,
        {
            "event": "scan_stop",
            "seconds": round(time.monotonic() - started, 1),
            "modal_spend_usd": (0 if args.mode == "local" else "see modal_batch_stop + dashboard"),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
