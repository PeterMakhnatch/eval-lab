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

An unchanged sweep must not repeat full hydration. The result ledger is read
under a coverage index bound to the SHA-256 of its exact bytes, so untouched
bytes reuse the stored run ids without re-parsing every row while a same-length
rewrite invalidates the index; admission refs are decided once per sweep and
shared with ``sync``; the key map is read after the import so newly inserted
runs scan in the same sweep; and the Scout transcript database is never opened
(never imported) when the unkeyed remainder yields no transcript. Admission is
still recomputed every sweep, and an unavailable actor history is still retried,
because both remain the only authoritative statements about what may be read.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Coverage index beside one projection's state. It is bound to the exact
#: ledger bytes by SHA-256 and to the fields below; bump ``COVERAGE_CONTRACT``
#: whenever the counting rule in ``_coverage`` changes, so a stored index is
#: recomputed rather than trusted.
LEDGER_INDEX_NAME = "coverage-v1.json"
LEDGER_INDEX_SCHEMA = "evallab.scout_coverage/v1"
COVERAGE_CONTRACT = "clouddb-coverage-v1"


def _records(path: Path) -> Iterator[dict[str, Any]]:
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def _coverage(
    rows: Iterable[Mapping[str, Any]], *, scanners: Collection[str], scout_version: str,
    projection_version: str,
) -> set[str]:
    """Runs with a current-projection value from every required scanner."""
    coverage: dict[str, set[str]] = defaultdict(set)
    required = set(scanners)
    if not required:
        raise ValueError("At least one deterministic scanner is required")
    prefix = f"inspect-scout/{scout_version}:"
    for row in rows:
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


def completed_runs(
    results_path: Path, *, scanners: Collection[str], scout_version: str,
    projection_version: str,
) -> set[str]:
    """Return complete current-version CloudDB coverage, not merely imported ids.

    Rows from another actor projection are ignored, so a migrated or older
    measurement never marks a run complete for the current one.
    """
    return _coverage(
        _records(Path(results_path)), scanners=scanners, scout_version=scout_version,
        projection_version=projection_version,
    )


def _cached_coverage(path: Path) -> dict[str, Any] | None:
    """A structurally valid stored index, or ``None`` for anything else."""
    if not path.is_file():
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    runs = index.get("runs") if isinstance(index, dict) else None
    if (
        not isinstance(index, dict) or index.get("schema") != LEDGER_INDEX_SCHEMA
        or not isinstance(runs, list) or not all(isinstance(run, str) for run in runs)
    ):
        return None
    return index


def _write_coverage(path: Path, index: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _file_digest(path: Path) -> str:
    """SHA-256 of the exact ledger bytes, or of nothing when it does not exist."""
    digest = hashlib.sha256()
    if path.exists():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _hashed_rows(path: Path, digest: Any) -> Iterator[dict[str, Any]]:
    """Yield parsed ledger rows while hashing the exact bytes just parsed."""
    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                if line.strip():
                    yield json.loads(line)


def ledger_coverage(
    results_path: Path, *, index_path: Path, scanners: Collection[str], scout_version: str,
    projection_version: str,
) -> set[str]:
    """Current-projection coverage over the exact ledger bytes, indexed by them.

    Unchanged bytes reuse the stored run ids after one streaming hash of the
    ledger; changed bytes are parsed once, bounded in memory, and the stored
    index is bound to the digest of the bytes actually parsed. A same-length
    rewrite therefore invalidates the index, and a ledger holding an unversioned
    Scout row is refused before any index is trusted or replaced.
    """
    from evallab.readers.scout_clouddb import versioned_rows

    results_path, index_path = Path(results_path), Path(index_path)
    required = set(scanners)
    if not required:
        raise ValueError("At least one deterministic scanner is required")
    identity: dict[str, Any] = {
        "schema": LEDGER_INDEX_SCHEMA, "coverage_contract": COVERAGE_CONTRACT,
        "projection_version": projection_version, "scout_version": scout_version,
        "scanners": sorted(required),
    }
    cached = _cached_coverage(index_path)
    if (
        cached is not None
        and {key: cached.get(key) for key in identity} == identity
        and cached.get("ledger_sha256") == _file_digest(results_path)
    ):
        return set(cached["runs"])
    digest = hashlib.sha256()
    runs = _coverage(
        versioned_rows(_hashed_rows(results_path, digest), results_path),
        scanners=required, scout_version=scout_version,
        projection_version=projection_version,
    )
    _write_coverage(
        index_path, {**identity, "ledger_sha256": digest.hexdigest(), "runs": sorted(runs)},
    )
    return runs


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
    from evallab.readers.clouddb_blind import opaque_id
    from evallab.readers.scout_clouddb import (
        SCANNERS,
        SCOUT_VERSION,
        admitted_refs,
        collect,
        read_keymap,
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
            # Unchanged ledger bytes reuse the stored current-projection coverage.
            completed = ledger_coverage(
                results_path, index_path=state_dir / LEDGER_INDEX_NAME, scanners=SCANNERS,
                scout_version=SCOUT_VERSION, projection_version=ACTOR_PROJECTION_VERSION,
            )
            # Admission is recomputed every sweep: a task may have acquired a
            # cross-key TEST seal, or its split registry may have tightened, since
            # the last import. The same decision then drives the import below.
            refs = admitted_refs(db_path, repo_root=repo_root)
            report: dict[str, Any] = sync(
                db_path, transcripts_dir, keymap_path, repo_root=repo_root, refs=refs,
            )
            # The import above may have keyed new actor histories; read the map
            # once, after it, so this sweep can scan what it just inserted.
            keymap = read_keymap(keymap_path)
            # Only an admitted id with a current actor transcript is scannable.
            available = [ref for ref in refs if opaque_id(ref.trace_id) in keymap]
            missing = [ref.trace_id for ref in refs if opaque_id(ref.trace_id) not in keymap]
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
