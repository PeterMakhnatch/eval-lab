"""Scan newly admitted DEV traces locally on a periodic, credential-free timer.

Existing result records at the current actor projection are the completion
ledger. A run is skipped only after all four deterministic scanners produced
values at the current Scout and projection versions. Partial/error/stale-version
scans remain pending; informational null flags are not failures.

Only an admitted id that is also present in the current actor key map is
scanned. An admitted run whose source retains no actor history has no transcript
by construction: it is reported as explicitly unknown/quarantined (``actor_missing``
plus the sync ``quarantined`` reasons) and never counted as scanned or clean.

An unversioned results ledger, or a key map or transcript cache of another
projection, is refused unchanged and never extended. No hosted reader, model
provider, upload or trial execution is invoked.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Collection, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _records(path: Path) -> Iterator[dict[str, Any]]:
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def completed_runs(
    results_path: Path, *, scanners: Collection[str], scout_version: str,
    projection_version: str,
) -> set[str]:
    """Return complete current-version CloudDB coverage, not merely imported ids.

    Rows from another actor projection are ignored, so a migrated or older
    measurement never marks a run complete for the current one.
    """
    coverage: dict[str, set[str]] = defaultdict(set)
    required = set(scanners)
    if not required:
        raise ValueError("At least one deterministic scanner is required")
    prefix = f"inspect-scout/{scout_version}:"
    for row in _records(results_path):
        if (
            row.get("tool") == "scout" and row.get("source") == "cloud-trace-db"
            and row.get("projection_version") == projection_version
            and str(row.get("detector_version", "")).startswith(prefix)
            and row.get("value") is not None
        ):
            scanner = str(row.get("detector", "")).partition(":")[0]
            if scanner in required:
                coverage[row["run_id"]].add(scanner)
    return {run_id for run_id, seen in coverage.items() if required <= seen}


def _receipt(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    """Counts for stdout; per-run identity and reasons stay in the receipt."""
    summary = dict(report)
    summary["quarantined_count"] = len(summary.pop("quarantined", []) or [])
    summary["actor_missing_count"] = len(summary.pop("actor_missing", []) or [])
    return summary


def run_once(
    *, db_path: Path, repo_root: Path, transcripts_dir: Path, scans_dir: Path,
    keymap_path: Path, results_path: Path, state_dir: Path, batch_size: int = 128,
) -> dict[str, Any]:
    """Import new runs, scan only unscanned admitted ids, and retain useful health."""
    from evallab.readers import ACTOR_PROJECTION_VERSION
    from evallab.readers.clouddb_blind import (
        dev_trace_refs,
        opaque_id,
    )
    from evallab.readers.scout_clouddb import (
        SCANNERS,
        SCOUT_VERSION,
        collect,
        require_current_results,
        scan,
        sync,
    )

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    state_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (state_dir / "watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "at": datetime.now(UTC).isoformat()}
        try:
            # Another projection's ledger is refused before any import or append.
            require_current_results(results_path)
            report: dict[str, Any] = sync(
                db_path, transcripts_dir, keymap_path, repo_root=repo_root,
            )
            # Admission is checked again before scanning persisted transcripts: a
            # task may have acquired a cross-key TEST seal since its initial import.
            uri = db_path.expanduser().resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as con:
                refs = dev_trace_refs(con, repo_root=repo_root)
            # Only an admitted id with a current actor transcript is scannable.
            keyed = {row["opaque_id"] for row in _records(keymap_path)}
            available = [ref for ref in refs if opaque_id(ref.trace_id) in keyed]
            missing = [ref.trace_id for ref in refs if opaque_id(ref.trace_id) not in keyed]
            completed = completed_runs(
                results_path, scanners=SCANNERS, scout_version=SCOUT_VERSION,
                projection_version=ACTOR_PROJECTION_VERSION,
            )
            pending = [ref.trace_id for ref in available if ref.trace_id not in completed]
            selected = pending[:batch_size]
            report.update({
                "at": datetime.now(UTC).isoformat(), "status": "idle",
                "admitted": len(refs), "actor_available": len(available),
                "actor_missing": missing, "pending": len(pending),
                "scanned": 0, "result_lines": 0, "usd": 0, "model_calls": 0,
            })
            if selected:
                status = scan(
                    transcripts_dir, scans_dir,
                    transcript_ids=[opaque_id(run_id) for run_id in selected],
                )
                written = collect(status.location, keymap_path, results_path)
                report.update({
                    "status": "scanned" if status.complete and not status.errors else "incomplete",
                    "scanned": len(selected), "result_lines": written,
                    "scan": str(status.location),
                    "errors": [error.model_dump() for error in status.errors],
                })
            report["wall_seconds"] = round(time.monotonic() - started, 3)
            _receipt(state_dir / "watch-last.json", report)
            return report
        except Exception as error:
            _receipt(state_dir / "watch-last.json", {
                "status": "error", "at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__, "error": str(error),
                "wall_seconds": round(time.monotonic() - started, 3),
            })
            raise


def watch_arguments(
    *, repo_root: Path, db_path: Path, trace_tools: Path, viewer: Path, batch_size: int,
) -> dict[str, Any]:
    """One projection's watch paths, shared by the defaults and the installer.

    The installer renders these into its LaunchAgent from the committed snapshot
    it just installed, so a scheduled run never reads or extends another
    projection's caches.
    """
    from evallab.readers.scout_clouddb import scout_paths

    paths = scout_paths(Path(trace_tools), Path(viewer))
    return {
        "db_path": Path(db_path),
        "repo_root": Path(repo_root),
        "transcripts_dir": paths.transcripts,
        "scans_dir": paths.scans / "watch",
        "keymap_path": paths.keymap,
        "results_path": paths.results,
        "state_dir": paths.state,
        "batch_size": int(batch_size),
    }


def argument_argv(arguments: dict[str, Any]) -> list[str]:
    """Render ``watch_arguments`` as LaunchAgent arguments (paths as absolute)."""
    return [
        "--repo-root", str(arguments["repo_root"]), "--db-path", str(arguments["db_path"]),
        "--state-dir", str(arguments["state_dir"]),
        "--keymap-path", str(arguments["keymap_path"]),
        "--results-path", str(arguments["results_path"]),
        "--transcripts-dir", str(arguments["transcripts_dir"]),
        "--scans-dir", str(arguments["scans_dir"]),
        "--batch-size", str(arguments["batch_size"]),
    ]


def build_parser(defaults: Any) -> argparse.ArgumentParser:
    """CLI parser whose defaults are one projection's locations."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-path", type=Path, default=defaults.DEFAULT_DB)
    parser.add_argument("--repo-root", type=Path, default=defaults.DEFAULT_REPO_ROOT)
    parser.add_argument("--transcripts-dir", type=Path, default=defaults.DEFAULT_TRANSCRIPTS)
    parser.add_argument("--scans-dir", type=Path, default=defaults.DEFAULT_SCANS / "watch")
    parser.add_argument("--keymap-path", type=Path, default=defaults.DEFAULT_KEYMAP)
    parser.add_argument("--results-path", type=Path, default=defaults.DEFAULT_RESULTS)
    parser.add_argument("--state-dir", type=Path, default=defaults.DEFAULT_STATE)
    parser.add_argument("--batch-size", type=int, default=128)
    return parser


def main(argv: list[str] | None = None) -> int:
    from evallab.readers import scout_clouddb as defaults

    args = build_parser(defaults).parse_args(argv)
    report = run_once(**vars(args))
    print(json.dumps(_summary(report)), flush=True)
    return int(report["status"] in {"incomplete", "error"})


if __name__ == "__main__":
    raise SystemExit(main())
