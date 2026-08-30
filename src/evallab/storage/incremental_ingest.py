"""Digest-indexed incremental ingestion of promoted ATIF bundles.

Invariants:
1. Digest Indexing: Tracks content digests from PROMOTION.json manifests with total-order tie-breaks
   to skip unchanged bundles.
2. Atomicity & Idempotence: Bundle parquet writes are staged and swapped using a rollback-safe adjacent
   backup; the digest index is committed only after bundle writes succeed; reruns produce identical
   logical tables.
3. Provenance & Omission Preservation: Lineage (source->promoted mapping) and omission records are
   captured into derived Parquet tables.
4. Security Enforcement: Rejects any bundle carrying physical symlinks or unredacted raw-log paths,
   while admitting repository-supported promotion schema versions and valid R4 quota sidecars.
5. Compact Performance Ledger: Records scanned, changed, skipped, and rejected counts without
   wall-clock claims.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa

from evallab.evidence.atif import PARQUET_SCHEMAS, ExportedTable, project_trial
from evallab.evidence.parquet_io import write_table_atomic
from evallab.interpretation.trajectory_judgment import canonical_json_digest
from evallab.results import load_job, sha256_file
from evallab.schemas import ContractModel

SCHEMA_VERSION = "promoted-atif-incremental-ingest/v1"
PERF_SCHEMA_VERSION = "incremental-ingest-perf/v1"
SUPPORTED_PROMOTION_VERSIONS = frozenset({1, 2, "1", "2", "v1", "v2"})
MANIFEST_NAME = "PROMOTION.json"
DIGEST_INDEX_FILENAME = "promoted_ingest_index.json"
PERF_LEDGER_FILENAME = "promoted_ingest_perf.json"
PROMOTED_BUNDLES_DIRNAME = "promoted_bundles"

# --------------------------------------------------------------------------- #
# Parquet Schemas for Lineage and Omissions
# --------------------------------------------------------------------------- #

LINEAGE_SCHEMA = pa.schema(
    [
        pa.field("bundle_name", pa.string(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
        pa.field("promoted_path", pa.string()),
        pa.field("action", pa.string(), nullable=False),
        pa.field("rule", pa.string()),
        pa.field("source_bytes", pa.int64(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("promoted_bytes", pa.int64()),
        pa.field("promoted_sha256", pa.string()),
    ]
)

OMISSIONS_SCHEMA = pa.schema(
    [
        pa.field("bundle_name", pa.string(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
        pa.field("rule", pa.string(), nullable=False),
        pa.field("entry_type", pa.string(), nullable=False),
        pa.field("link_target", pa.string()),
        pa.field("source_bytes", pa.int64(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
    ]
)


# --------------------------------------------------------------------------- #
# Security Inspection
# --------------------------------------------------------------------------- #


def is_raw_log_path(relative: Path) -> bool:
    """Whether a path is raw model I/O or runtime state that R2 must omit."""
    parts = relative.parts
    if relative.name == "job.log" and len(parts) == 1:
        return True
    if relative.name == "trial.log":
        return True
    if "agent" not in parts:
        return False
    # R4 quota sidecars under agent/quota/ are whitelisted derivatives, not raw logs
    if "quota" in parts or relative.name.endswith(".rate-limits.json"):
        return False
    return (
        "sessions" in parts or "opencode" in parts or relative.name in {"codex.txt", "opencode.txt"}
    )


def validate_bundle_security(bundle_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """Fail-closed security validation of a promoted bundle directory.

    Rejects:
    1. Unsupported promotion manifest schema versions.
    2. Physical symlinks anywhere in the bundle tree.
    3. Physical raw-log files present on disk.
    4. Manifest entries promoting raw-log paths (except valid R4 quota sidecars).
    """
    rejections: list[str] = []

    # 1. Schema version: accept all repository-supported promotion versions
    manifest_version = manifest.get("schema_version")
    if manifest_version not in SUPPORTED_PROMOTION_VERSIONS:
        rejections.append(f"unsupported_manifest_schema_version:{manifest_version}")

    # 2. Check filesystem for symlinks and forbidden raw files
    try:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_symlink():
                rel = path.relative_to(bundle_dir).as_posix()
                rejections.append(f"security_symlink_detected:{rel}")
            elif path.is_file():
                rel_path = path.relative_to(bundle_dir)
                if is_raw_log_path(rel_path):
                    rejections.append(f"security_raw_log_file_present:{rel_path.as_posix()}")
    except OSError as exc:
        rejections.append(f"filesystem_scan_error:{exc}")

    # 3. Check manifest entries for bad promotions
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        source_path = entry.get("source_path", "")
        action = entry.get("action")
        rule = entry.get("rule")
        promoted_path = entry.get("promoted_path")

        # Allow valid R4 quota sidecars derived from omitted rollouts
        is_r4_sidecar = (
            rule == "R4"
            and action == "redacted"
            and isinstance(promoted_path, str)
            and (
                "quota" in Path(promoted_path).parts or promoted_path.endswith(".rate-limits.json")
            )
        )

        if is_raw_log_path(Path(source_path)) and action != "omitted" and not is_r4_sidecar:
            rejections.append(f"security_unomitted_raw_log_manifest:{source_path}")
        if action == "omitted" and promoted_path is not None:
            rejections.append(f"security_omission_promoted_path_not_null:{source_path}")

    return rejections


# --------------------------------------------------------------------------- #
# Digest Index & Performance Ledger Models
# --------------------------------------------------------------------------- #

DispositionOutcome = Literal["changed", "skipped", "rejected", "failed"]


class BundleDisposition(ContractModel):
    """Outcome of processing one promoted bundle."""

    bundle_name: str
    outcome: DispositionOutcome
    digest: str
    reason: str | None = None
    promoted_files: int = 0
    omitted_files: int = 0


class IngestPerformanceLedger(ContractModel):
    """Compact run-level performance ledger without wall-clock claims."""

    schema_version: Literal["incremental-ingest-perf/v1"] = PERF_SCHEMA_VERSION
    run_id: str
    scanned_bundles: int
    changed_bundles: int
    skipped_bundles: int
    rejected_bundles: int
    failed_bundles: int
    promoted_files_scanned: int
    promoted_files_ingested: int
    promoted_files_skipped: int
    content_digest: str


class DigestIndex(ContractModel):
    """Persistent content-digest index tracking ingested bundle versions."""

    schema_version: Literal["promoted-atif-incremental-ingest/v1"] = SCHEMA_VERSION
    entries: dict[str, str] = field(default_factory=dict)
    content_digest: str = ""


@dataclass(frozen=True)
class IncrementalIngestResult:
    """Complete outcome of an incremental ingestion run."""

    derived_root: Path
    index_path: Path
    perf_ledger_path: Path
    performance: IngestPerformanceLedger
    dispositions: tuple[BundleDisposition, ...]
    tables: tuple[ExportedTable, ...]


# --------------------------------------------------------------------------- #
# Digest Computation & Discovery
# --------------------------------------------------------------------------- #


def compute_bundle_digest(manifest: dict[str, Any]) -> str:
    """Deterministic content digest for a promoted bundle from its manifest.

    Applies a complete total-order tie-break across all entry fields so duplicate
    source_paths (e.g. omitted rollout vs R4 sidecar) produce identical deterministic digests.
    """
    files = manifest.get("files", [])
    valid_entries = [f for f in files if isinstance(f, dict)]

    def _sort_key(f: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
        return (
            str(f.get("source_path") or ""),
            str(f.get("promoted_path") or ""),
            str(f.get("action") or ""),
            str(f.get("rule") or ""),
            str(f.get("source_sha256") or ""),
            str(f.get("promoted_sha256") or ""),
            str(f.get("entry_type") or ""),
        )

    normalized_files = []
    for f in sorted(valid_entries, key=_sort_key):
        normalized_files.append(
            {
                "source_path": f.get("source_path"),
                "promoted_path": f.get("promoted_path"),
                "action": f.get("action"),
                "rule": f.get("rule"),
                "source_bytes": f.get("source_bytes"),
                "source_sha256": f.get("source_sha256"),
                "promoted_bytes": f.get("promoted_bytes"),
                "promoted_sha256": f.get("promoted_sha256"),
                "entry_type": f.get("entry_type"),
                "link_target": f.get("link_target"),
            }
        )

    body = {
        "schema_version": manifest.get("schema_version"),
        "bundle": manifest.get("bundle"),
        "source_job_result_sha256": manifest.get("source_job_result_sha256"),
        "files": normalized_files,
    }
    return canonical_json_digest(body)


def discover_promoted_bundles(runs_root: Path) -> list[Path]:
    """Find all candidate promoted bundle directories under runs_root."""
    if not runs_root.is_dir():
        return []
    candidates = []
    for manifest_path in sorted(runs_root.glob(f"*/{MANIFEST_NAME}")):
        bundle_dir = manifest_path.parent
        if bundle_dir.is_dir() and not bundle_dir.name.startswith("."):
            candidates.append(bundle_dir)
    return sorted(candidates, key=lambda p: p.name)


# --------------------------------------------------------------------------- #
# Lineage & Omission Extraction
# --------------------------------------------------------------------------- #


def extract_lineage_and_omissions(
    bundle_name: str, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract structured lineage and omission rows from PROMOTION.json."""
    lineage_rows: list[dict[str, Any]] = []
    omission_rows: list[dict[str, Any]] = []

    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            continue
        source_path = str(entry.get("source_path") or "")
        action = str(entry.get("action") or "verbatim")
        rule = entry.get("rule")
        source_bytes = int(entry.get("source_bytes") or 0)
        source_sha256 = str(entry.get("source_sha256") or "")
        promoted_path = entry.get("promoted_path")
        promoted_bytes = entry.get("promoted_bytes")
        promoted_sha256 = entry.get("promoted_sha256")
        entry_type = str(entry.get("entry_type") or "file")
        link_target = entry.get("link_target")

        lineage_rows.append(
            {
                "bundle_name": bundle_name,
                "source_path": source_path,
                "promoted_path": str(promoted_path) if promoted_path is not None else None,
                "action": action,
                "rule": str(rule) if rule is not None else None,
                "source_bytes": source_bytes,
                "source_sha256": source_sha256,
                "promoted_bytes": int(promoted_bytes) if promoted_bytes is not None else None,
                "promoted_sha256": str(promoted_sha256) if promoted_sha256 is not None else None,
            }
        )

        if action == "omitted":
            omission_rows.append(
                {
                    "bundle_name": bundle_name,
                    "source_path": source_path,
                    "rule": str(rule or "R2"),
                    "entry_type": entry_type,
                    "link_target": str(link_target) if link_target is not None else None,
                    "source_bytes": source_bytes,
                    "source_sha256": source_sha256,
                }
            )

    return lineage_rows, omission_rows


# --------------------------------------------------------------------------- #
# Atomic Bundle Ingestion
# --------------------------------------------------------------------------- #


def ingest_bundle_atomic(
    bundle_dir: Path,
    manifest: dict[str, Any],
    target_partition_dir: Path,
) -> list[ExportedTable]:
    """Atomically project one bundle's ATIF tables, lineage, and omissions.

    Writes to a staging directory first, then performs a rollback-safe adjacent swap.
    """
    bundle_name = bundle_dir.name
    staging_dir = target_partition_dir.parent / f"{target_partition_dir.name}.staging"
    backup_dir = target_partition_dir.parent / f"{target_partition_dir.name}.backup"

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    staging_dir.mkdir(parents=True, exist_ok=True)

    exported_tables: list[ExportedTable] = []
    try:
        job = load_job(bundle_dir)

        # 1. Project ATIF trial trajectories
        trajectories_rows: list[dict[str, Any]] = []
        steps_rows: list[dict[str, Any]] = []
        tool_calls_rows: list[dict[str, Any]] = []
        observations_rows: list[dict[str, Any]] = []

        for trial in sorted(job.trials, key=lambda t: t.id):
            projection = project_trial(job, trial)
            trajectories_rows.extend(asdict(item) for item in projection.trajectories)
            steps_rows.extend(asdict(item) for item in projection.steps)
            tool_calls_rows.extend(asdict(item) for item in projection.tool_calls)
            observations_rows.extend(asdict(item) for item in projection.observations)

        # Write core ATIF Parquet tables
        table_data = [
            ("jobs", [{"job_id": job.id, "job_name": job.name, "trial_count": len(job.trials)}]),
            ("trajectories", trajectories_rows),
            ("steps", steps_rows),
            ("tool_calls", tool_calls_rows),
            ("observations", observations_rows),
        ]
        for table_name, rows in table_data:
            path = staging_dir / f"{table_name}.parquet"
            write_table_atomic(path, rows, PARQUET_SCHEMAS[table_name])
            exported_tables.append(
                ExportedTable(
                    table=table_name,
                    path=target_partition_dir / f"{table_name}.parquet",
                    rows=len(rows),
                    sha256=f"sha256:{sha256_file(path)}",
                )
            )

        # 2. Preserve Lineage and Omissions
        lineage_rows, omission_rows = extract_lineage_and_omissions(bundle_name, manifest)

        lineage_path = staging_dir / "promotion_lineage.parquet"
        write_table_atomic(lineage_path, lineage_rows, LINEAGE_SCHEMA)
        exported_tables.append(
            ExportedTable(
                table="promotion_lineage",
                path=target_partition_dir / "promotion_lineage.parquet",
                rows=len(lineage_rows),
                sha256=f"sha256:{sha256_file(lineage_path)}",
            )
        )

        omissions_path = staging_dir / "promotion_omissions.parquet"
        write_table_atomic(omissions_path, omission_rows, OMISSIONS_SCHEMA)
        exported_tables.append(
            ExportedTable(
                table="promotion_omissions",
                path=target_partition_dir / "promotion_omissions.parquet",
                rows=len(omission_rows),
                sha256=f"sha256:{sha256_file(omissions_path)}",
            )
        )

        # 3. Rollback-safe adjacent swap
        target_existed = target_partition_dir.exists()
        if target_existed:
            target_partition_dir.rename(backup_dir)

        try:
            staging_dir.rename(target_partition_dir)
        except Exception:
            if target_existed and backup_dir.exists() and not target_partition_dir.exists():
                backup_dir.rename(target_partition_dir)
            raise
        else:
            if backup_dir.exists():
                shutil.rmtree(backup_dir)

    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if backup_dir.exists() and not target_partition_dir.exists():
            backup_dir.rename(target_partition_dir)
        raise

    return exported_tables


# --------------------------------------------------------------------------- #
# Ledger Persistence
# --------------------------------------------------------------------------- #


def load_digest_index(index_path: Path) -> DigestIndex:
    """Load persistent digest index, or return empty."""
    if not index_path.is_file():
        return DigestIndex(schema_version=SCHEMA_VERSION, entries={}, content_digest="")
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            return DigestIndex(schema_version=SCHEMA_VERSION, entries={}, content_digest="")
        entries = raw.get("entries", {})
        if not isinstance(entries, dict):
            entries = {}
        return DigestIndex(
            schema_version=SCHEMA_VERSION,
            entries=entries,
            content_digest=str(raw.get("content_digest", "")),
        )
    except (OSError, json.JSONDecodeError):
        return DigestIndex(schema_version=SCHEMA_VERSION, entries={}, content_digest="")


def save_digest_index(index_path: Path, index: DigestIndex) -> None:
    """Save digest index atomically."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": SCHEMA_VERSION,
        "entries": index.entries,
    }
    content_digest = canonical_json_digest(body)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": index.entries,
        "content_digest": content_digest,
    }
    tmp_path = index_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(index_path)


def save_performance_ledger(perf_path: Path, ledger: IngestPerformanceLedger) -> None:
    """Save run performance ledger atomically."""
    perf_path.parent.mkdir(parents=True, exist_ok=True)
    payload = ledger.model_dump(mode="json")
    tmp_path = perf_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(perf_path)


# --------------------------------------------------------------------------- #
# Main Entry Point
# --------------------------------------------------------------------------- #


def ingest_promoted_bundles(
    runs_root: Path,
    derived_root: Path,
    *,
    force: bool = False,
    run_id: str | None = None,
    index_path: Path | None = None,
    perf_path: Path | None = None,
) -> IncrementalIngestResult:
    """Incremental, digest-indexed ingestion of promoted ATIF bundles.

    Guarantees:
    - Skips bundles whose digest has not changed since the last indexed run.
    - Atomically projects changed bundles into Parquet partitions via rollback-safe swap.
    - Rejects bundles violating security invariants (symlinks, unredacted raw logs).
    - Preserves lineage and omission rows.
    - Writes a compact performance ledger without wall-clock claims.
    """
    derived_root = derived_root.resolve()
    effective_index_path = index_path or (derived_root / DIGEST_INDEX_FILENAME)
    effective_perf_path = perf_path or (derived_root / PERF_LEDGER_FILENAME)
    promoted_bundles_root = derived_root / PROMOTED_BUNDLES_DIRNAME

    digest_index = load_digest_index(effective_index_path)
    updated_entries = dict(digest_index.entries)

    bundle_dirs = discover_promoted_bundles(runs_root)

    dispositions: list[BundleDisposition] = []
    all_exported_tables: list[ExportedTable] = []

    scanned_bundles = len(bundle_dirs)
    changed_bundles = 0
    skipped_bundles = 0
    rejected_bundles = 0
    failed_bundles = 0

    promoted_files_scanned = 0
    promoted_files_ingested = 0
    promoted_files_skipped = 0

    effective_run_id = (
        run_id or f"run-{hashlib.sha256(str(scanned_bundles).encode()).hexdigest()[:16]}"
    )

    for bundle_dir in bundle_dirs:
        bundle_name = bundle_dir.name
        manifest_path = bundle_dir / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            dispositions.append(
                BundleDisposition(
                    bundle_name=bundle_name,
                    outcome="failed",
                    digest="",
                    reason=f"manifest_load_error:{exc}",
                )
            )
            failed_bundles += 1
            continue

        promoted_count = sum(1 for f in manifest.get("files", []) if f.get("promoted_path"))
        omitted_count = sum(1 for f in manifest.get("files", []) if not f.get("promoted_path"))
        promoted_files_scanned += promoted_count

        # 1. Security Check
        security_errors = validate_bundle_security(bundle_dir, manifest)
        if security_errors:
            dispositions.append(
                BundleDisposition(
                    bundle_name=bundle_name,
                    outcome="rejected",
                    digest="",
                    reason="; ".join(security_errors),
                    promoted_files=promoted_count,
                    omitted_files=omitted_count,
                )
            )
            rejected_bundles += 1
            continue

        # 2. Content Digest & Skip Check
        digest = compute_bundle_digest(manifest)
        target_partition = promoted_bundles_root / bundle_name
        is_complete = (
            target_partition.is_dir()
            and (target_partition / "trajectories.parquet").is_file()
            and (target_partition / "promotion_lineage.parquet").is_file()
        )

        if not force and updated_entries.get(bundle_name) == digest and is_complete:
            dispositions.append(
                BundleDisposition(
                    bundle_name=bundle_name,
                    outcome="skipped",
                    digest=digest,
                    promoted_files=promoted_count,
                    omitted_files=omitted_count,
                )
            )
            skipped_bundles += 1
            promoted_files_skipped += promoted_count
            continue

        # 3. Atomic Ingestion with Rollback-Safe Swap
        try:
            tables = ingest_bundle_atomic(bundle_dir, manifest, target_partition)
            all_exported_tables.extend(tables)
            updated_entries[bundle_name] = digest
            dispositions.append(
                BundleDisposition(
                    bundle_name=bundle_name,
                    outcome="changed",
                    digest=digest,
                    promoted_files=promoted_count,
                    omitted_files=omitted_count,
                )
            )
            changed_bundles += 1
            promoted_files_ingested += promoted_count
        except Exception as exc:
            dispositions.append(
                BundleDisposition(
                    bundle_name=bundle_name,
                    outcome="failed",
                    digest=digest,
                    reason=f"ingest_failed:{type(exc).__name__}:{exc}",
                    promoted_files=promoted_count,
                    omitted_files=omitted_count,
                )
            )
            failed_bundles += 1

    # 4. Commit Performance Ledger
    perf_body = {
        "schema_version": PERF_SCHEMA_VERSION,
        "run_id": effective_run_id,
        "scanned_bundles": scanned_bundles,
        "changed_bundles": changed_bundles,
        "skipped_bundles": skipped_bundles,
        "rejected_bundles": rejected_bundles,
        "failed_bundles": failed_bundles,
        "promoted_files_scanned": promoted_files_scanned,
        "promoted_files_ingested": promoted_files_ingested,
        "promoted_files_skipped": promoted_files_skipped,
    }
    perf_digest = canonical_json_digest(perf_body)
    perf_ledger = IngestPerformanceLedger(
        schema_version=PERF_SCHEMA_VERSION,
        run_id=effective_run_id,
        scanned_bundles=scanned_bundles,
        changed_bundles=changed_bundles,
        skipped_bundles=skipped_bundles,
        rejected_bundles=rejected_bundles,
        failed_bundles=failed_bundles,
        promoted_files_scanned=promoted_files_scanned,
        promoted_files_ingested=promoted_files_ingested,
        promoted_files_skipped=promoted_files_skipped,
        content_digest=perf_digest,
    )
    save_performance_ledger(effective_perf_path, perf_ledger)

    # 5. Commit Digest Index
    new_index = DigestIndex(
        schema_version=SCHEMA_VERSION,
        entries=updated_entries,
    )
    save_digest_index(effective_index_path, new_index)

    return IncrementalIngestResult(
        derived_root=derived_root,
        index_path=effective_index_path,
        perf_ledger_path=effective_perf_path,
        performance=perf_ledger,
        dispositions=tuple(dispositions),
        tables=tuple(all_exported_tables),
    )
