"""One always-on viewer for every published run (``evallab results-viewer``).

``evallab view`` builds a one-off snapshot of the jobs you name. This command
keeps a persistent viewer root in step with the results home instead, and
serves it with Harbor 0.24's viewer app at one fixed URL:

* each pass mirrors jobs published since the last pass, using the same
  overlay as ``evallab view`` (``reward`` unchanged, plus ``integrity`` and
  ``reward_gated``), so the viewer shows the same dims;
* a republished job (its ``result.json`` changed) is rebuilt; a job whose
  source is gone is dropped from the root;
* a job is mirrored only after its tree has been quiet for ``settle``
  seconds, because publishing copies files in place;
* each viewer job is staged beside the root and renamed in, so the viewer
  never lists a half-built job.

Sources are only read. The root holds hard links (see ``harbor_view._link``)
and small rewritten ``result.json`` / ``reward-details.json`` files; removing
a viewer job only unlinks the root's names, never a source's.

The served app is guarded: only GET/HEAD requests with a loopback ``Host``
header reach Harbor. The 0.24 viewer also exposes POST endpoints that launch
``harbor run``, run ``harbor analyze`` (model calls), upload to Harbor Hub and
delete jobs; an always-on unauthenticated server must not offer those to any
web page that can reach loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.harbor_view import (
    VIEWER_MIN_VERSION,
    installed_harbor_version,
    is_job_dir,
    mirror_job,
)
from evallab.results_home import results_root
from evallab.task_pages import (
    DEFAULT_TRUSTED_GLOBS,
    PAGE_RECORD,
    TaskPages,
    default_store,
)

SOURCE_RECORD = ".evallab-results-viewer.json"
#: v2: hard-linked mirrors. A v1 (symlinked) viewer job is dropped and rebuilt.
SOURCE_RECORD_SCHEMA = "results_viewer/source/v2"
DEFAULT_ROOT = (
    Path.home() / "Library" / "Application Support" / "evallab" / "results-viewer" / "jobs"
)
DEFAULT_PORT = 8100
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_SETTLE_SECONDS = 60.0
#: Search depth below each source: the results home is ``<date>/<job>/``.
DISCOVERY_DEPTH = 2

Signature = tuple[int, int]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def discover_sources(sources: list[Path], *, depth: int = DISCOVERY_DEPTH) -> list[Path]:
    """Job dirs at or below each source, at most ``depth`` levels down."""
    found: dict[str, Path] = {}

    def walk(path: Path, remaining: int) -> None:
        if is_job_dir(path):
            found.setdefault(str(path.resolve()), path.resolve())
            return
        if remaining == 0 or not path.is_dir():
            return
        for child in path.iterdir():
            if child.name.startswith(".") or not child.is_dir():
                continue
            walk(child, remaining - 1)

    for source in sources:
        walk(source, depth)
    return [found[key] for key in sorted(found)]


def source_signature(job_dir: Path) -> Signature:
    """Change marker for a published job: its ``result.json`` stat.

    Publishing replaces the whole tree, so a republished job always gets a
    fresh ``result.json``.
    """
    stat = (job_dir / "result.json").stat()
    return (stat.st_mtime_ns, stat.st_size)


def newest_mtime(job_dir: Path) -> float:
    """Newest modification time anywhere in the job tree (links not followed)."""
    newest = job_dir.lstat().st_mtime
    for dirpath, dirnames, filenames in os.walk(job_dir, followlinks=False):
        for name in (*dirnames, *filenames):
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except FileNotFoundError:
                continue
    return newest


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unsettled: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    jobs: int = 0
    task_pages: dict[str, list[str]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.added
            or self.rebuilt
            or self.removed
            or self.failed
            or any(self.task_pages.values())
        )

    def summary(self) -> str:
        pages = (
            " task_pages=" + ",".join(f"{k}:{len(v)}" for k, v in sorted(self.task_pages.items()))
            if self.task_pages
            else ""
        )
        return (
            f"jobs={self.jobs} added={len(self.added)} rebuilt={len(self.rebuilt)} "
            f"removed={len(self.removed)} unsettled={len(self.unsettled)} "
            f"failed={len(self.failed)}{pages}"
        )


def _read_record(job: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((job / SOURCE_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SOURCE_RECORD_SCHEMA:
        return None
    return record


def _unique_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    idx = 2
    while f"{name}-{idx}" in taken:
        idx += 1
    return f"{name}-{idx}"


class ResultsViewerRoot:
    """A persistent viewer root mirrored from one or more source trees.

    Each viewer job carries ``SOURCE_RECORD`` naming its source job dir and
    the source signature it was built from, so the root itself is the state:
    a restart resumes without rebuilding.
    """

    def __init__(
        self,
        root: Path,
        sources: list[Path],
        *,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
        task_pages: TaskPages | None = None,
    ) -> None:
        self.root = root
        self.sources = sources
        self.settle_seconds = settle_seconds
        self.clock = clock
        self.log = log or (lambda _msg: None)
        self.staging = root.parent / f".{root.name}.staging"
        #: Signatures that failed to mirror; retried only once they change.
        self._failed: dict[str, Signature] = {}
        #: Per-task pages (``task-<id>``) built beside the mirrored jobs.
        self.task_pages = task_pages

    def _index(self) -> dict[str, tuple[str, Signature | None]]:
        """``source -> (viewer job name, signature)`` read from the root."""
        index: dict[str, tuple[str, Signature | None]] = {}
        for job in self.root.iterdir():
            if not job.is_dir() or job.is_symlink() or (job / PAGE_RECORD).is_file():
                continue  # task pages are kept by TaskPages
            record = _read_record(job)
            if record is None or not isinstance(record.get("source"), str):
                # Not ours (or a pre-record leftover): derived, drop and rebuild.
                self._discard(job)
                continue
            signature = record.get("signature")
            sig = (
                (int(signature[0]), int(signature[1]))
                if isinstance(signature, list) and len(signature) == 2
                else None
            )
            if record["source"] in index:
                self._discard(job)
                continue
            index[record["source"]] = (job.name, sig)
        return index

    def _discard(self, job: Path) -> None:
        trash = self.staging / f"trash-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        job.rename(trash)
        # rmtree unlinks names (hard links, symlinks) without touching sources.
        shutil.rmtree(trash, ignore_errors=True)

    def _build(self, source: Path, name: str, signature: Signature) -> Path:
        staged = self.staging / f"build-{uuid.uuid4().hex}" / name
        try:
            mirror_job(source, staged)
            record_path = staged / SOURCE_RECORD
            record_path.unlink(missing_ok=True)  # never write through a hard link
            record_path.write_text(
                json.dumps(
                    {
                        "schema": SOURCE_RECORD_SCHEMA,
                        "source": str(source),
                        "signature": list(signature),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except BaseException:
            shutil.rmtree(staged.parent, ignore_errors=True)
            raise
        return staged

    def sync(self) -> SyncReport:
        """One pass: add new jobs, rebuild republished ones, drop vanished ones."""
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.staging, ignore_errors=True)
        report = SyncReport()
        index = self._index()
        jobs = discover_sources(self.sources)
        live = {str(job) for job in jobs}

        for source, (name, _sig) in sorted(index.items()):
            if source not in live:
                self._discard(self.root / name)
                del index[source]
                report.removed.append(name)

        candidates: list[tuple[Signature, Path]] = []
        for job in jobs:
            try:
                signature = source_signature(job)
            except FileNotFoundError:
                continue  # vanished mid-pass; next pass decides
            current = index.get(str(job))
            if current is not None and current[1] == signature:
                continue
            if self._failed.get(str(job)) == signature:
                continue
            candidates.append((signature, job))
        # Newest first, so a fresh root shows today's runs before old ones.
        candidates.sort(key=lambda item: item[0][0], reverse=True)

        taken = {name for name, _sig in index.values()}
        now = self.clock()
        for signature, job in candidates:
            key = str(job)
            try:
                if now - newest_mtime(job) < self.settle_seconds:
                    report.unsettled.append(job.name)
                    continue
                existing = index.get(key)
                name = existing[0] if existing else _unique_name(job.name, taken)
                staged = self._build(job, name, signature)
                target = self.root / name
                if existing:
                    self._discard(target)
                staged.rename(target)
                shutil.rmtree(staged.parent, ignore_errors=True)
            except Exception as exc:  # one bad job never stops the pass
                self._failed[key] = signature
                report.failed[key] = f"{type(exc).__name__}: {exc}"
                self.log(f"results-viewer: failed {key}: {type(exc).__name__}: {exc}")
                continue
            self._failed.pop(key, None)
            taken.add(name)
            index[key] = (name, signature)
            (report.rebuilt if existing else report.added).append(name)
        shutil.rmtree(self.staging, ignore_errors=True)
        report.jobs = len(index)
        if self.task_pages is not None:
            report.task_pages = self.task_pages.sync(jobs)
        return report


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

READ_METHODS = frozenset({"GET", "HEAD"})


def allowed_hosts(host: str, port: int) -> frozenset[str]:
    names = {host, "127.0.0.1", "localhost"}
    return frozenset({f"{name}:{port}" for name in names} | names)


def read_only(app: ASGIApp, hosts: frozenset[str]) -> ASGIApp:
    """Pass only GET/HEAD requests whose ``Host`` is loopback.

    The method check removes every state-changing viewer endpoint (run
    launch, analyze, upload, delete). The ``Host`` check stops DNS rebinding,
    where a foreign site resolves its own name to 127.0.0.1.
    """

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1")
            if host not in hosts:
                await _reject(send, 403, "forbidden host")
                return
            if scope["method"] not in READ_METHODS:
                await _reject(send, 405, "read-only results viewer")
                return
        await app(scope, receive, send)

    return guarded


async def _reject(send: Send, status: int, message: str) -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _viewer_app(root: Path) -> ASGIApp:
    version = installed_harbor_version()
    if version is None or version < VIEWER_MIN_VERSION:
        raise RuntimeError(
            "results-viewer needs harbor>=0.24 in this environment "
            "(uv sync --extra laminar, or scripts/ops/launchd/install-results-viewer.sh)"
        )
    from harbor.cli.view import STATIC_DIR  # ty: ignore[unresolved-import]
    from harbor.viewer.server import create_app  # ty: ignore[unresolved-import]

    if not (STATIC_DIR / "index.html").is_file():
        raise RuntimeError(f"harbor viewer static files missing: {STATIC_DIR}")
    return create_app(root, mode="jobs", static_dir=STATIC_DIR)


def _sync_forever(viewer: ResultsViewerRoot, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            report = viewer.sync()
        except Exception as exc:
            print(f"results-viewer: sync error: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            if report.changed:
                print(
                    f"results-viewer: {report.summary()} ({time.monotonic() - started:.1f}s)",
                    flush=True,
                )
        stop.wait(interval)


def serve(viewer: ResultsViewerRoot, *, host: str, port: int, interval: float) -> int:
    import uvicorn  # ty: ignore[unresolved-import]

    viewer.root.mkdir(parents=True, exist_ok=True)
    app = read_only(_viewer_app(viewer.root), allowed_hosts(host, port))
    stop = threading.Event()
    syncer = threading.Thread(
        target=_sync_forever, args=(viewer, interval, stop), name="results-sync", daemon=True
    )
    syncer.start()
    print(f"results-viewer: serving {viewer.root} at http://{host}:{port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    try:
        server.run()
    finally:
        stop.set()
    return 0 if server.started else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    del root
    sources = [path.expanduser().resolve() for path in args.sources] or [results_root()]
    variants = args.variant_records.expanduser() if args.variant_records else None
    viewer = ResultsViewerRoot(
        args.root.expanduser(),
        sources,
        settle_seconds=args.settle,
        log=lambda msg: print(msg, file=sys.stderr),
        task_pages=None
        if args.no_task_pages
        else TaskPages(
            args.root.expanduser(),
            store=(args.readers_store or default_store()).expanduser(),
            variants_dir=variants if variants is not None and variants.is_dir() else None,
            trusted_globs=args.trusted_job or DEFAULT_TRUSTED_GLOBS,
            laminar_api_key=os.environ.get("LMNR_PROJECT_API_KEY") or None,
        ),
    )
    if args.once:
        report = viewer.sync()
        print(f"root: {viewer.root}")
        print(report.summary())
        for key, error in sorted(report.failed.items()):
            print(f"  failed {key}: {error}")
        return 1 if report.failed else 0
    try:
        return serve(viewer, host=args.host, port=args.port, interval=args.interval)
    except RuntimeError as exc:
        print(f"evallab results-viewer: {exc}", file=sys.stderr)
        return 2


def build_results_viewer_parser(commands: argparse._SubParsersAction) -> None:
    """Register ``evallab results-viewer``."""
    parser = commands.add_parser(
        "results-viewer",
        help="Serve every published run at one fixed URL, kept in sync (read-only)",
    )
    parser.add_argument(
        "sources",
        type=Path,
        nargs="*",
        help="Results roots to mirror (default: the results home, $EVALLAB_RESULTS_HOME)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Persistent viewer root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Seconds between sync passes (default: 60)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="Mirror a job only after its tree has been quiet this long (default: 60)",
    )
    parser.add_argument(
        "--readers-store",
        type=Path,
        help="Reader verdict store <store>/<job>/<trial>/<reader>.json "
        "(default: $EVALLAB_READERS_STORE or ~/Library/Application Support/evallab/readers)",
    )
    parser.add_argument(
        "--trusted-job",
        action="append",
        default=[],
        metavar="GLOB",
        help="Job-name glob whose trials get task pages (repeatable; default: HAR-168-*)",
    )
    parser.add_argument(
        "--variant-records",
        type=Path,
        help="Variant-record tree for health/solve tags (the installer passes a snapshot of "
        "library/task-variants; without it pages show 'no record')",
    )
    parser.add_argument("--no-task-pages", action="store_true", help="Do not build task-<id> pages")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one sync pass, print the summary and exit (no server)",
    )
    parser.set_defaults(func=_command)
