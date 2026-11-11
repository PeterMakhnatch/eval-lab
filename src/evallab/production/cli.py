"""Public DEV queue admission, read-only status, and evidence exports."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry

_CONNECTION_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|nats|tls|ws|wss)://[^\s\"'<>]+")
_LIVE_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"


Backend = Literal["sqlite", "postgresql"]


@dataclass
class QueueConnection:
    """Own one queue without permitting a later switch between durable stores."""

    queue: ContinuousWorkQueue
    backend: Backend
    fallback_reason: str | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if not self._closed:
            self.queue.close()
            self._closed = True

    def __enter__(self) -> QueueConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _sqlite_path(database: str | Path) -> Path:
    path = str(database)
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///") :]
    result = Path(path).expanduser()
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def open_production_queue(
    database: str | Path,
    *,
    registry: CorpusRegistry,
    sqlite_fallback: Path | None = None,
    connect_timeout: int = 3,
) -> QueueConnection:
    """Open the requested store, falling back only before PostgreSQL connects.

    A malformed DSN, schema error, corrupt database, or any later disconnect is
    not an availability fallback. The dedicated connection error contains no
    driver exception text or connection credentials.
    """
    from evallab.production.corpus_queue import ContinuousWorkQueue, ProductionQueueConnectionError

    if type(connect_timeout) is not int or connect_timeout <= 0:
        raise ValueError("connect_timeout must be a positive integer")
    if not str(database).startswith(("postgresql://", "postgres://")):
        return QueueConnection(ContinuousWorkQueue(_sqlite_path(database), registry=registry), "sqlite")
    try:
        queue = ContinuousWorkQueue(database, registry=registry, connect_timeout=connect_timeout)
    except ProductionQueueConnectionError:
        if sqlite_fallback is None:
            raise
        queue = ContinuousWorkQueue(_sqlite_path(sqlite_fallback), registry=registry)
        return QueueConnection(
            queue,
            "sqlite",
            "PostgreSQL initial connection unavailable; using the explicit SQLite fallback.",
        )
    return QueueConnection(queue, "postgresql")


def _queue_options(parser: argparse.ArgumentParser) -> None:
    # Suppressed child defaults preserve options supplied before the subcommand.
    parser.add_argument(
        "--repo-root", type=Path, default=argparse.SUPPRESS,
        help="Corpus/source root (default: the current Eval Lab workspace)",
    )
    parser.add_argument(
        "--database", default=argparse.SUPPRESS,
        help="PostgreSQL DSN or SQLite path (default: derived/production/prod-queue.sqlite)",
    )
    parser.add_argument(
        "--sqlite-fallback", type=Path, default=argparse.SUPPRESS,
        help="Initial PostgreSQL connection fallback (default: the root-derived SQLite queue)",
    )


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def register_parser(commands: Any) -> None:
    """Register deterministically without opening databases, brokers, or models."""
    production = commands.add_parser(
        "prod", help="Admit and inspect the durable production queue; export evidence",
        description=(
            "Public DEV production lane. TEST/heldout records remain sealed. "
            "Queue status is read-only. All receipts are JSON."
        ),
    )
    production.set_defaults(repo_root=None, database=None, sqlite_fallback=None)
    _queue_options(production)
    actions = production.add_subparsers(dest="prod_command", required=True)
    ingest = actions.add_parser("ingest", help="Admit and seed public DEV corpora idempotently")
    _queue_options(ingest)
    ingest.add_argument(
        "--dataset", dest="dataset_ids", action="append",
        help="Registry dataset ID; repeat to select several (default: all public DEV corpora)",
    )
    ingest.add_argument("--run-id", default="production-seed")
    ingest.set_defaults(func=_command)

    status = actions.add_parser("status", help="Read an existing queue strictly read-only; never create or migrate it")
    _queue_options(status)
    status.set_defaults(func=_command)

    project = actions.add_parser(
        "project-actor",
        help="Stage a separate admitted SQLite/CAS bundle; never dispatch or upload",
    )
    project.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    project.add_argument("--source-database", type=Path, required=True)
    project.add_argument("--source-cas", type=Path, required=True)
    project.add_argument(
        "--cas-allowlist", type=Path, required=True,
        help="Operator-approved JSON array of direct CAS URIs; not inferred rights clearance",
    )
    project.add_argument("--output-directory", type=Path, required=True)
    project.add_argument(
        "--max-bytes", type=int, required=True,
        help="Hard byte cap for staged CAS objects (SQLite bytes reported separately)",
    )
    project.add_argument(
        "--task-id", dest="task_ids", action="append",
        help="Further restrict registry-admitted tasks; repeat to select several",
    )
    project.set_defaults(func=_command)

    runs = actions.add_parser(
        "export-runs",
        help="Export blinded, registry-admitted DEV traces locally; never dispatch or upload",
    )
    runs.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    runs.add_argument("--source-database", type=Path, required=True)
    runs.add_argument("--output-directory", type=Path, required=True)
    runs.add_argument("--format", choices=("harbor", "atif", "scout"), required=True)
    runs.add_argument(
        "--manifest", type=Path, required=True,
        help="New sealed identity/step join file outside the export directory",
    )
    runs.add_argument(
        "--trace-id", dest="trace_ids", action="append",
        help="Further restrict admitted traces; repeat to select several",
    )
    runs.add_argument(
        "--trace-ids-file", type=Path, metavar="FILE",
        help="One trace id per line, merged with --trace-id; blank lines and # comments ignored",
    )
    runs.add_argument(
        "--strict", action="store_true",
        help="Fail before publication if any requested trace id is missing or not admitted",
    )
    runs.add_argument("--limit", type=int, help="Maximum admitted traces to export")
    runs.set_defaults(func=_command)

    scope = actions.add_parser(
        "export-cluster-scope",
        help="Write a metadata-only scope file for one Run Atlas cluster; never reads run bodies",
        description=(
            "One row per DEV-admitted run in the cluster (trace id, task ids, assignments.csv digest, "
            "facet build/producer/model) plus counts of refused members. No trace bodies, labels, "
            "rewards, facet text or cluster names; the manifest must not already exist."
        ),
    )
    scope.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    scope.add_argument(
        "--source-database", type=Path, default=_LIVE_DATABASE,
        help="SQLite cloud trace database (default: the live store)",
    )
    scope.add_argument("--cluster-id", required=True)
    scope.add_argument("--manifest", type=Path, required=True, help="New scope file to publish")
    scope.set_defaults(func=_command)

    cheatbench = actions.add_parser(
        "export-cheatbench-runs",
        help="Export blinded, registry-admitted DEV CheatBench runs that have no cloud trace; never uploads",
        description=(
            "Resolves facet run ids (cheatbench-actions-v1 item ids) through the answer-key registry's "
            "pinned published trajectories, applies the per-item DEV admission, and writes the same "
            "blinded formats as export-runs. The trial_ref and the expected-verdict fields stay in the "
            "sealed manifest outside the export."
        ),
    )
    cheatbench.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    cheatbench.add_argument("--output-directory", type=Path, required=True)
    cheatbench.add_argument("--format", choices=("harbor", "atif", "scout"), required=True)
    cheatbench.add_argument(
        "--manifest", type=Path, required=True, help="New sealed join file outside the export directory",
    )
    cheatbench.add_argument(
        "--run-id", dest="run_ids", action="append",
        help="Facet run id (item id) to export; repeat to select several (default: every candidate)",
    )
    cheatbench.add_argument("--limit", type=int, help="Maximum admitted runs to export")
    cheatbench.add_argument(
        "--view", choices=("full", "actions_only"), default="full",
        help="full = the trajectory the facets were produced from (default)",
    )
    cheatbench.set_defaults(func=_command)


    facets = actions.add_parser(
        "import-run-facets",
        help="Import Run Atlas run-facets/v1 JSONL into the cloud trace database (DEV-admitted runs only)",
    )
    facets.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    facets.add_argument(
        "--cloud-database", required=True,
        help="Cloud trace database: SQLite path/URL or PostgreSQL URL",
    )
    facets.add_argument("--jsonl", type=Path, required=True, help="Append-only facets JSONL keyed by run_id")
    facets.set_defaults(func=_command)

    clusters = actions.add_parser(
        "import-run-clusters",
        help="Import the final complete Run Atlas run-clusters/v1 build (DEV-admitted, faceted runs only)",
    )
    clusters.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    clusters.add_argument(
        "--cloud-database", required=True,
        help="Cloud trace database: SQLite path/URL or PostgreSQL URL",
    )
    clusters.add_argument("--jsonl", type=Path, required=True, help="Append-only clusters JSONL")
    clusters.add_argument(
        "--drop-unfaceted", action="store_true",
        help="Drop (and count) clustered runs with no stored admitted facets instead of refusing the build",
    )
    clusters.set_defaults(func=_command)

    findings = actions.add_parser(
        "import-trace-findings",
        help="Import trace-findings/v1 JSONL (one detector verdict per line) into the cloud trace database (DEV-admitted runs only)",
    )
    findings.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    findings.add_argument(
        "--cloud-database", required=True,
        help="Cloud trace database: SQLite path/URL or PostgreSQL URL",
    )
    findings.add_argument("--jsonl", type=Path, required=True, help="Append-only findings JSONL")
    findings.set_defaults(func=_command)


    sb1 = actions.add_parser(
        "seeker-error-first",
        help="SB-1 two-turn error-first Reward Seeker campaign; --plan (default) is free",
        description=(
            "One cell is one cohort task x one model: a blind turn 1, then turn 2 with the "
            "real ORIGINAL-grader runner error placed first plus DEV-only trace retrieval. "
            "Plan and summarize make no model call and no grader run. --execute needs "
            "--approval, --max-usd, --spend-ledger and --modal-app-name; local Docker is "
            "always disabled."
        ),
    )
    sb1.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    sb1.add_argument(
        "--cohort", type=Path,
        default=Path("research/experiments/sb1-clean-seeker-set-20261027/cohort.json"),
        help="evallab.sb1_clean_seeker_set/v1 manifest",
    )
    sb1.add_argument("--out", type=Path, required=True, help="Retained cell/summary directory")
    sb1.add_argument(
        "--models", nargs="+", metavar="ROUTE",
        help="OMP routes (default: the four SB-1 routes)",
    )
    sb1.add_argument("--workers", type=int, default=6, help="Concurrent cells (1-8, default 6)")
    sb1.add_argument(
        "--per-model-concurrency", type=int, default=2,
        help="Concurrent model calls per route, within --workers (default 2)",
    )
    sb1.add_argument("--resume", action="store_true", help="Skip turns whose result passes the QC predicate")
    mode = sb1.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Default: counts and cost bound only")
    mode.add_argument("--execute", action="store_true", help="Call models and Modal graders")
    mode.add_argument("--summarize", action="store_true", help="Rebuild summary from retained cells")
    sb1.add_argument("--approval", help="Recorded Peter authorization text for the Modal spend")
    sb1.add_argument("--max-usd", type=float, help="Spend cap enforced by the native ledger")
    sb1.add_argument("--spend-ledger", type=Path, help="Native Modal reservation ledger path")
    sb1.add_argument("--modal-app-name", help="Modal app that owns the sandboxes")
    sb1.add_argument("--billing-tag", help="Modal execution cost attribution")
    sb1.add_argument(
        "--cloud-trace-db", type=Path,
        help="Local cloud-trace SQLite for turn-2 retrieval (opened read-only, DEV only)",
    )
    sb1.add_argument("--assignments-csv", type=Path, help="Split ledger (default: repository copy)")
    sb1.add_argument("--omp-timeout", type=_positive_seconds, default=300.0)
    sb1.add_argument("--grader-timeout", type=int, default=600)
    sb1.add_argument("--omp-config", type=Path, help="OMP --config file")
    sb1.add_argument(
        "--spend-log", type=Path,
        help="Failure-library spend.jsonl (default: ~/.local/state/evallab-failure-library)",
    )
    sb1.add_argument(
        "--visible-tests", action="store_true",
        help="SK-4 visible-tests arm: the prompt includes the task's tests instead of "
        "hiding them. Cells record hide_tests=false and only read back under this mode",
    )
    sb1.add_argument(
        "--no-probe-routes", action="store_true",
        help="Skip the pre-campaign route probe (a route that is fallback-served then "
        "costs a grader run per cell instead of being reported unavailable)",
    )
    sb1.set_defaults(func=_command)




def _resolve(root: Path, path: Path) -> Path:
    path = path.expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _database(root: Path, value: str | None) -> str | Path:
    if value is None:
        return root / "derived/production/prod-queue.sqlite"
    if value.startswith(("postgresql://", "postgres://")):
        return value
    if value == ":memory:":
        return value
    if value.startswith("sqlite:///"):
        value = value[len("sqlite:///"):]
    return _resolve(root, Path(value))


def _open_queue(args: argparse.Namespace, root: Path) -> Any:
    from evallab.production.corpus_queue import CorpusRegistry

    fallback = _resolve(root, args.sqlite_fallback or Path("derived/production/prod-queue.sqlite"))
    return open_production_queue(
        _database(root, args.database), registry=CorpusRegistry(root), sqlite_fallback=fallback,
    )


def _open_status_queue(args: argparse.Namespace, root: Path) -> Any:
    """Reuse the queue's snapshot reader without invoking its migrating writer constructor."""
    import sqlite3

    from evallab.production.actor_projection import source_read_uri
    from evallab.production.corpus_queue import (
        ContinuousWorkQueue,
        ProductionQueueConnectionError,
    )

    class StatusQueue(ContinuousWorkQueue):
        def __init__(self, database: str | Path) -> None:
            self._lock = threading.RLock()
            self._postgres = str(database).startswith(("postgresql://", "postgres://"))
            if self._postgres:
                import psycopg

                try:
                    self._connection = psycopg.connect(
                        str(database), autocommit=True, connect_timeout=3,
                        options="-c default_transaction_read_only=on",
                    )
                except psycopg.OperationalError:
                    raise ProductionQueueConnectionError(
                        "PostgreSQL production queue connection unavailable",
                    ) from None
            else:
                path = Path(database)
                if not path.is_file():
                    raise ValueError("status requires an existing queue database")
                sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
                if all(sidecar.is_file() for sidecar in sidecars):
                    uri = path.as_uri() + "?mode=ro"
                else:
                    try:
                        uri = source_read_uri(path)
                    except ValueError as exc:
                        raise ValueError(str(exc).replace("before projection", "before reading queue status")) from exc
                self._connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            try:
                if not self._postgres:
                    self._connection.execute("PRAGMA query_only=ON")
                tables = self._execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                    if self._postgres else "SELECT name FROM sqlite_schema WHERE type = 'table'",
                ).fetchall()
                if not {"queue_state", "events", "outbox", "consumer_cursors"} <= {row[0] for row in tables}:
                    raise ValueError("status requires a queue database (missing queue tables)")
            except BaseException:
                self._connection.close()
                raise

    database = _database(root, args.database)
    try:
        queue = StatusQueue(database)
    except ProductionQueueConnectionError:
        fallback = _resolve(root, args.sqlite_fallback or Path("derived/production/prod-queue.sqlite"))
        return QueueConnection(
            StatusQueue(fallback), "sqlite",
            "PostgreSQL initial connection unavailable; using the explicit SQLite fallback.",
        )
    return QueueConnection(queue, "postgresql" if queue._postgres else "sqlite")


def _emit(payload: dict[str, Any], args: argparse.Namespace, *, error: bool = False) -> None:
    from evallab.explorer import redact_mapping, redact_text

    secrets = {
        value for key, value in os.environ.items()
        if value and any(marker in key.upper() for marker in (
            "API_KEY", "ACCESS_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
        ))
    }
    value = args.database
    if value and "://" in value:
        secrets.add(value)
        try:
            parsed = urlsplit(value)
            if parsed.password:
                secrets.add(unquote(parsed.password))
            elif parsed.scheme in {"nats", "tls"} and parsed.username:
                secrets.add(unquote(parsed.username))
        except ValueError:
            pass

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return redact_text(_CONNECTION_URL.sub("[redacted-connection]", value))
        if isinstance(value, Mapping):
            return {str(key): scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    print(
        json.dumps(scrub(redact_mapping(payload)), sort_keys=True, allow_nan=False),
        file=sys.stderr if error else sys.stdout, flush=True,
    )


def _snapshot(connection: Any) -> dict[str, Any]:
    return {
        "backend": connection.backend,
        "fallback_reason": connection.fallback_reason,
        "queue": connection.queue.queue_snapshot(),
    }


def _read_allowlist(path: Path) -> bytes:
    """Read the approval document without following any path alias."""
    from evallab.production.actor_cas import read_owned_file

    return read_owned_file(path)


def _local_path(root: Path, path: Path) -> Path:
    path = path.expanduser()
    path = path if path.is_absolute() else root / path
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Operator paths must not contain symlinks or '..'")
    return path


def _export_runs(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    from evallab.production.cloud_trace_export import export_runs
    from evallab.production.corpus_queue import CorpusRegistry

    trace_ids = args.trace_ids
    if args.trace_ids_file is not None:
        lines = _local_path(root, args.trace_ids_file).read_text(encoding="utf-8").splitlines()
        file_ids = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        if not file_ids:
            raise ValueError("no admitted runs selected")
        trace_ids = [*(trace_ids or ()), *file_ids]
    return export_runs(
        _local_path(root, args.source_database),
        _local_path(root, args.output_directory),
        format=args.format,
        registry=CorpusRegistry(root),
        trace_ids=trace_ids,
        limit=args.limit,
        manifest=_local_path(root, args.manifest),
        strict=args.strict,
    )


def _export_cheatbench_runs(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    from evallab.production.cloud_trace_export import export_cheatbench_runs
    from evallab.production.corpus_queue import CorpusRegistry

    return export_cheatbench_runs(
        _local_path(root, args.output_directory),
        format=args.format,
        registry=CorpusRegistry(root),
        run_ids=args.run_ids,
        limit=args.limit,
        manifest=_local_path(root, args.manifest),
        view=args.view,
    )


def _export_cluster_scope(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    from evallab.production.cloud_trace_export import export_cluster_scope
    from evallab.production.corpus_queue import CorpusRegistry

    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False,
        )
        revision = head.stdout.strip() if head.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        revision = None
    return export_cluster_scope(
        _local_path(root, args.source_database), args.cluster_id, _local_path(root, args.manifest),
        registry=CorpusRegistry(root), revision=revision or None,
    )




def _seeker_error_first(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], int]:
    from evallab.production.seeker_error_first import run_command

    return run_command(
        cohort=_local_path(root, args.cohort),
        out=_local_path(root, args.out),
        models=args.models,
        workers=args.workers,
        per_model_concurrency=args.per_model_concurrency,
        resume=args.resume,
        mode="execute" if args.execute else "summarize" if args.summarize else "plan",
        assignments_csv=_optional_path(root, args.assignments_csv),
        cloud_trace_db=_optional_path(root, args.cloud_trace_db),
        approval=args.approval,
        max_usd=args.max_usd,
        spend_ledger=_optional_path(root, args.spend_ledger),
        modal_app_name=args.modal_app_name,
        billing_tag=args.billing_tag,
        omp_timeout=args.omp_timeout,
        grader_timeout=args.grader_timeout,
        omp_config=_optional_path(root, args.omp_config),
        spend_log=_optional_path(root, args.spend_log),
        probe_routes=not args.no_probe_routes,
        hide_tests=not args.visible_tests,
    )


def _optional_path(root: Path, path: Path | None) -> Path | None:
    return None if path is None else _local_path(root, path)


def _project_actor(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    """Stage an operator-approved subset, never the raw database or its free pages."""
    import sqlite3

    from evallab.production.actor_cas import (
        PublicationUncertainError,
        directory_status,
        discard_owned_directory,
        preflight_destination,
        publish_staged_directory,
        stage_projection_cas,
    )
    from evallab.production.actor_projection import project_actor_database, source_read_uri
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids
    from evallab.production.corpus_queue import CorpusRegistry

    def local_path(path: Path) -> Path:
        return _local_path(root, path)

    source = local_path(args.source_database)
    source_cas = local_path(args.source_cas)
    output = local_path(args.output_directory)
    if output.exists():
        raise FileExistsError("Actor bundle destination already exists")
    allowlist_bytes = _read_allowlist(local_path(args.cas_allowlist))
    allowlist = json.loads(allowlist_bytes)
    if not isinstance(allowlist, list) or any(not isinstance(uri, str) for uri in allowlist):
        raise ValueError("CAS allowlist must be a JSON array of URI strings")
    with closing(sqlite3.connect(source_read_uri(source), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        identities = [
            dict(row) for row in connection.execute(
                "SELECT task_id, dataset_id, split, original_grader_digest,"
                " repaired_grader_digest,"
                " CASE WHEN json_valid(metadata_json)"
                " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
                " CASE WHEN json_valid(metadata_json)"
                " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
                " FROM corpus_tasks ORDER BY task_id"
            )
        ]
    admitted = admitted_cloud_task_ids(identities, registry=CorpusRegistry(root))
    selected = frozenset(args.task_ids) if args.task_ids is not None else admitted
    if not selected or not selected <= admitted:
        raise ValueError("No tasks selected, or a requested task lacks canonical actor admission")
    expected = {row["task_id"]: row for row in identities if row["task_id"] in selected}
    preflight_destination(source_cas, output)
    staging = Path(tempfile.mkdtemp(prefix=".actor-bundle-", dir=output.parent))
    staging_status = directory_status(staging)
    published = False
    try:
        projection = project_actor_database(
            source, staging / "cloud-trace-db.sqlite",
            task_ids=selected, expected_identities=expected,
        )
        cas = stage_projection_cas(
            staging / "cloud-trace-db.sqlite", source_cas, staging / "cas",
            approved_uris=allowlist, max_bytes=args.max_bytes,
        )
        receipt = {
            "schema": "evallab.actor_bundle/v1",
            "database": "cloud-trace-db.sqlite",
            "cas_directory": "cas",
            "projection": projection,
            "cas": cas,
            "cas_allowlist_sha256": hashlib.sha256(allowlist_bytes).hexdigest(),
            "approval_kind": "operator-supplied CAS allowlist; not independent rights adjudication",
            "labels_independently_verified": False,
            "reference_scope": "approved direct CAS URI columns only; embedded references not followed",
        }
        with (staging / "manifest.json").open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(receipt, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            publish_staged_directory(staging, output, expected=staging_status)
        except PublicationUncertainError:
            # The rename succeeded, so this bundle may be complete at the output
            # path. Report the uncertainty and never delete what may be published.
            published = True
            raise
        published = True
        return {"output_directory": str(output), **receipt}
    finally:
        # Before publication this pathname is ours, but only while it still
        # resolves to the directory we created: a competitor's entry is left alone.
        if not published:
            for candidate in (staging, output):
                discard_owned_directory(candidate, staging_status)


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    """Honor the existing CLI's integer return convention, with redacted JSON errors."""
    try:
        root = _resolve(root, args.repo_root) if args.repo_root is not None else root.resolve()
        exit_code = 0
        if args.prod_command == "project-actor":
            _emit({"command": args.prod_command, **_project_actor(args, root)}, args)
            return 0
        if args.prod_command == "export-runs":
            _emit({"command": args.prod_command, **_export_runs(args, root)}, args)
            return 0
        if args.prod_command == "export-cheatbench-runs":
            _emit({"command": args.prod_command, **_export_cheatbench_runs(args, root)}, args)
            return 0
        if args.prod_command == "export-cluster-scope":
            _emit({"command": args.prod_command, **_export_cluster_scope(args, root)}, args)
            return 0
        if args.prod_command == "import-run-facets":
            from evallab.production.cloud_trace_db import CloudTraceDatabase, import_run_facets
            from evallab.production.corpus_queue import CorpusRegistry

            with CloudTraceDatabase(_database(root, args.cloud_database)) as database:
                receipt = import_run_facets(
                    database, _resolve(root, args.jsonl), registry=CorpusRegistry(root),
                )
            _emit({"command": args.prod_command, **receipt}, args)
            return 0
        if args.prod_command == "import-trace-findings":
            from evallab.production.cloud_trace_db import CloudTraceDatabase
            from evallab.production.corpus_queue import CorpusRegistry
            from evallab.production.trace_findings import import_trace_findings

            with CloudTraceDatabase(_database(root, args.cloud_database)) as database:
                receipt = import_trace_findings(
                    database, _resolve(root, args.jsonl), registry=CorpusRegistry(root),
                )
            _emit({"command": args.prod_command, **receipt}, args)
            return 0
        if args.prod_command == "import-run-clusters":
            from evallab.production.cloud_trace_db import CloudTraceDatabase, import_run_clusters
            from evallab.production.corpus_queue import CorpusRegistry

            with CloudTraceDatabase(_database(root, args.cloud_database)) as database:
                receipt = import_run_clusters(
                    database, _resolve(root, args.jsonl), registry=CorpusRegistry(root),
                    drop_unfaceted=args.drop_unfaceted,
                )
            _emit({"command": args.prod_command, **receipt}, args)
            return 0
        if args.prod_command == "seeker-error-first":
            receipt, exit_code = _seeker_error_first(args, root)
            _emit({"command": args.prod_command, **receipt}, args)
            return exit_code
        if args.prod_command == "status":
            with _open_status_queue(args, root) as connection:
                _emit({"command": args.prod_command, **_snapshot(connection)}, args)
            return 0
        with _open_queue(args, root) as connection:
            if args.prod_command == "ingest":
                from evallab.production.ingest import ingest_corpus

                result = ingest_corpus(
                    connection.queue, dataset_ids=args.dataset_ids, run_id=args.run_id,
                )
                receipt: dict[str, Any] = {"ingest": result, **_snapshot(connection)}
            else:
                raise ValueError("Unknown production command")
            _emit({"command": args.prod_command, **receipt}, args)
            return exit_code
    except KeyboardInterrupt:
        _emit({"command": args.prod_command, "event": "interrupted"}, args, error=True)
        return 130
    except Exception as exc:
        _emit({
            "command": args.prod_command,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, args, error=True)
        return 2
