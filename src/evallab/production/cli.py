"""Public DEV evidence exports."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_CONNECTION_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|nats|tls|ws|wss)://[^\s\"'<>]+")
_LIVE_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"






def _resolve(root: Path, path: Path) -> Path:
    path = path.expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _emit(payload: dict[str, Any], *, error: bool = False) -> None:
    from evallab.explorer import redact_mapping, redact_text

    secrets = {
        value for key, value in os.environ.items()
        if value and any(marker in key.upper() for marker in (
            "API_KEY", "ACCESS_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
        ))
    }

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


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    """Honor the existing CLI's integer return convention, with redacted JSON errors."""
    try:
        root = _resolve(root, args.repo_root) if args.repo_root is not None else root.resolve()
        if args.prod_command == "export-runs":
            _emit({"command": args.prod_command, **_export_runs(args, root)})
            return 0
        if args.prod_command == "export-cheatbench-runs":
            _emit({"command": args.prod_command, **_export_cheatbench_runs(args, root)})
            return 0
        _emit({"command": args.prod_command, **_export_cluster_scope(args, root)})
        return 0
    except Exception as exc:
        _emit({
            "command": args.prod_command,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, error=True)
        return 2
