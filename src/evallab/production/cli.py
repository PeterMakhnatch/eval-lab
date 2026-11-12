"""Public DEV evidence exports and admitted trace imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_CONNECTION_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|nats|tls|ws|wss)://[^\s\"'<>]+")
_LIVE_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"


def _queue_options(parser: argparse.ArgumentParser) -> None:
    # Suppressed child defaults preserve options supplied before the subcommand.
    parser.add_argument(
        "--repo-root", type=Path, default=argparse.SUPPRESS,
        help="Corpus/source root (default: the current Eval Lab workspace)",
    )


def register_parser(commands: Any) -> None:
    """Register deterministically without opening databases, brokers, or models."""
    production = commands.add_parser(
        "prod", help="Export evidence and import admitted DEV trace records",
        description=(
            "Public DEV production lane. TEST/heldout records remain sealed. "
            "All receipts are JSON."
        ),
    )
    production.set_defaults(repo_root=None)
    _queue_options(production)
    actions = production.add_subparsers(dest="prod_command", required=True)


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
        else:
            from evallab.production.cloud_trace_db import CloudTraceDatabase, import_run_clusters
            from evallab.production.corpus_queue import CorpusRegistry

            with CloudTraceDatabase(_database(root, args.cloud_database)) as database:
                receipt = import_run_clusters(
                    database, _resolve(root, args.jsonl), registry=CorpusRegistry(root),
                    drop_unfaceted=args.drop_unfaceted,
                )
            _emit({"command": args.prod_command, **receipt}, args)
            return 0
    except KeyboardInterrupt:
        _emit({"command": args.prod_command, "event": "interrupted"}, args, error=True)
        return 130
    except Exception as exc:
        _emit({
            "command": args.prod_command,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, args, error=True)
        return 2
