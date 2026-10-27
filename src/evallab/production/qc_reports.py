"""Export the Trust & QC retained reports as exact-byte, validated artifacts.

``evallab prod export-qc-reports`` writes documents the dashboard's
``/api/cloud-db/benchmarks`` listing accepts, never uploads them, and never
changes the source database:

* ``evallab.trust_tier_census/v1`` is *generated* from the live
  ``CloudTraceDatabase`` (opened read-only). Only rows on tasks the shared
  ``admitted_cloud_task_ids`` gate admits are counted, so the census describes the
  evidence retrieval can actually use.
* ``evallab.hackbook_qc/v1`` (``evallab hackbook qc --out``) and
  ``evallab.tune_cross_test/v1`` reports are *recorded* measurements. They are
  validated with the same strict validator as the API and then copied byte for
  byte; the exporter never recomputes, rounds or rewrites a measured value.

Every artifact is validated as the exact bytes that are written, staged in a
sibling directory and published by one rename, so a partial export is never left
behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.actor_cas import read_owned_file
from evallab.production.actor_projection import source_read_uri
from evallab.production.benchmark_reports import (
    MAX_BENCHMARK_BYTES,
    validate_benchmark_document,
)
from evallab.production.cloud_trace_db import TRUST_TIERS, admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry

_TIER_BASIS = {
    "tier1_container_verified": (
        "container-verified: qualified repairs, replayed differentials, maintainer fixes"
    ),
    "tier2_consensus_labeled": (
        "consensus-labelled: unanimous judge votes with passage spans, human-verified runs"
    ),
    "tier3_provisional": (
        "provisional: unverified leads, and the default for rows whose writer declared no tier"
    ),
}
_UNITS = {"traces": ("agent_traces", "trace_id"), "issues": ("task_issues", "issue_id")}
_SLUG = re.compile(r"[^a-z0-9._-]+")
_IDENTITY_SQL = (
    "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
    " FROM corpus_tasks ORDER BY task_id"
)


def trust_tier_census(
    connection: sqlite3.Connection,
    admitted: frozenset[str],
    *,
    unit: str,
    generated_at: str,
) -> dict[str, Any]:
    """Count admitted DEV rows per (tier, dataset) from a read-only connection."""
    table, _ = _UNITS[unit]
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "trust_tier" not in columns:
        raise ValueError(f"{table} has no trust_tier column; the database predates evidence tiers")
    dataset_of = dict(connection.execute("SELECT task_id, dataset_id FROM corpus_tasks"))
    counts: Counter[tuple[str, str]] = Counter()
    unclassified = 0
    for task_id, tier, rows in connection.execute(
        f"SELECT task_id, trust_tier, COUNT(*) FROM {table} GROUP BY task_id, trust_tier"
    ):
        if task_id not in admitted:
            continue
        if tier in TRUST_TIERS:
            counts[(tier, dataset_of[task_id])] += rows
        else:
            unclassified += rows
    rank = {tier: index for index, tier in enumerate(TRUST_TIERS)}
    tiers = [
        {
            "tier": tier,
            "source": source,
            "basis": _TIER_BASIS[tier],
            "rows_n": rows,
            # Retrieval serves tiers 1 and 2 by default and omits tier 3 unless asked.
            "prompt_eligible_n": 0 if tier == TRUST_TIERS[-1] else rows,
        }
        for (tier, source), rows in sorted(
            counts.items(), key=lambda item: (rank[item[0][0]], item[0][1])
        )
    ]
    return {
        "schema": "evallab.trust_tier_census/v1",
        "split": "dev",
        "unit": unit,
        "admitted_rows_n": counts.total() + unclassified,
        "unclassified_n": unclassified,
        "tiers": tiers,
        "source_sha256": None,
        "generated_at": generated_at,
    }


def _encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _slug(path: Path) -> str:
    slug = _SLUG.sub("-", path.stem.casefold()).strip("-.")
    if not slug:
        raise ValueError(f"cannot derive an artifact name from {path.name!r}")
    return slug


def _recorded(paths: tuple[Path, ...], schema: str, prefix: str) -> list[tuple[str, bytes, str]]:
    artifacts = []
    for path in paths:
        raw = read_owned_file(path)
        if len(raw) > MAX_BENCHMARK_BYTES:
            raise ValueError(f"{path.name} exceeds the {MAX_BENCHMARK_BYTES} byte report limit")
        found = validate_benchmark_document(raw).get("schema")
        if found != schema:
            raise ValueError(f"{path.name} is {found!r}, not {schema}")
        artifacts.append((f"{prefix}-{_slug(path)}.json", raw, f"recorded input {path.name}"))
    return artifacts


def export_qc_reports(
    *,
    source_database: Path,
    output_directory: Path,
    repo_root: Path,
    hackbook_qc: tuple[Path, ...] = (),
    tune_cross_test: tuple[Path, ...] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Stage, validate and atomically publish a directory of retained reports."""
    if output_directory.exists() or output_directory.is_symlink():
        raise FileExistsError("QC report destination already exists; refusing overwrite")
    stamp = generated_at or datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(source_read_uri(source_database), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        identities = [dict(row) for row in connection.execute(_IDENTITY_SQL)]
        registry = CorpusRegistry(repo_root)
        if not registry.has_assignment_authority():
            raise ValueError("No split assignment authority is loaded; nothing can be admitted")
        admitted = admitted_cloud_task_ids(identities, registry=registry)
        connection.row_factory = None
        artifacts = [
            (
                f"trust-tier-census-{unit}.json",
                _encode(trust_tier_census(connection, admitted, unit=unit, generated_at=stamp)),
                "generated from the live database (read-only, gate-admitted rows only)",
            )
            for unit in _UNITS
        ]
    artifacts += _recorded(hackbook_qc, "evallab.hackbook_qc/v1", "hackbook-qc")
    artifacts += _recorded(tune_cross_test, "evallab.tune_cross_test/v1", "tune-cross-test")
    names = [name for name, _, _ in artifacts]
    if len(set(names)) != len(names):
        raise ValueError("two inputs map to the same artifact name; rename one source file")

    staging = Path(tempfile.mkdtemp(prefix=".qc-reports-", dir=output_directory.parent))
    try:
        receipt = []
        for name, raw, origin in artifacts:
            document = validate_benchmark_document(raw)
            with (staging / name).open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            written = (staging / name).read_bytes()
            if written != raw or validate_benchmark_document(written) != document:
                raise RuntimeError(f"{name} changed between validation and publication")
            receipt.append(
                {
                    "file": name,
                    "schema": document["schema"],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "origin": origin,
                }
            )
        os.rename(staging, output_directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "output_directory": str(output_directory),
        "generated_at": stamp,
        "artifacts": receipt,
        "uploaded": False,
        "note": (
            "Counts are source claims copied or counted, not re-verified. Copy the directory "
            "to the cloud benchmarks directory only under an approved transfer."
        ),
    }
