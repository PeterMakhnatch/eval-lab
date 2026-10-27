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
    table, key = _UNITS[unit]
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "trust_tier" not in columns:
        raise ValueError(f"{table} has no trust_tier column; the database predates evidence tiers")
    dataset_of = dict(connection.execute("SELECT task_id, dataset_id FROM corpus_tasks"))
    counts: Counter[tuple[str, str]] = Counter()
    unclassified = 0
    # The digest identifies exactly which admitted rows were counted and at which tier,
    # so a reader holding the same database can reproduce it.
    digest = hashlib.sha256()
    for row_id, task_id, tier in connection.execute(
        f"SELECT {key}, task_id, trust_tier FROM {table} ORDER BY {key}"
    ):
        if task_id not in admitted:
            continue
        digest.update(f"{row_id}\t{task_id}\t{tier}\n".encode())
        if tier in TRUST_TIERS:
            counts[(tier, dataset_of[task_id])] += 1
        else:
            unclassified += 1
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
        "source_sha256": digest.hexdigest(),
        "generated_at": generated_at,
    }


_TS4_CONDITIONS = (
    # (condition, static key, executed total key, executed MiMo key, executed TB4 key)
    (
        "turn_1",
        "turn_1_static_30",
        "turn_1_executed_30",
        "turn_1_executed_mimo_22",
        "turn_1_executed_tb4_8",
    ),
    (
        "turn_2_original_feedback",
        "turn_2_static_30",
        "turn_2_executed_pr1241_30",
        "turn_2_executed_pr1241_mimo_22",
        "turn_2_executed_pr1241_tb4_8",
    ),
    (
        "turn_2_runner_accurate_feedback",
        None,
        "turn_2_executed_runner_accurate_30",
        "turn_2_executed_runner_accurate_mimo_22",
        None,
    ),
    (
        "cumulative_runner_accurate_feedback",
        None,
        "cumul_executed_runner_accurate_30",
        "cumul_executed_runner_accurate_mimo_22",
        "cumul_executed_tb4_8",
    ),
)
_TS4_DATASETS = ("mimo-defect-map-v1", "tb4-audit-v1")


def ts4_executed_differentials(manifest: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    """Map the recorded TS-4 cross-key audit and per-model counts to the report schema.

    Every number is copied from the manifest; the only arithmetic is the TB4 share of a
    total whose MiMo share is recorded (the strictly-DEV cohort has no other dataset).
    """
    audit = manifest["ts4_cross_key_and_executed_audit"]
    clean = audit["strictly_dev_everywhere_by_dataset"]
    shares = {name: len(tasks) for name, tasks in clean.items() if tasks}
    if set(shares) != set(_TS4_DATASETS):
        raise ValueError(f"TS-4 cohort datasets are not {_TS4_DATASETS}: {sorted(shares)}")
    task_count = audit["strictly_dev_everywhere_count"]
    if (
        sum(shares.values()) != task_count
        or len(audit["contaminated_tasks"]) != audit["secondary_key_contaminated_count"]
    ):
        raise ValueError("TS-4 audit counts disagree with its recorded task lists")
    arms = []
    for model, counts in sorted(audit["models_on_clean_30_tasks"].items()):
        conditions = []
        for name, static_key, total_key, mimo_key, tb4_key in _TS4_CONDITIONS:
            total, mimo = counts[total_key], counts[mimo_key]
            tb4 = counts[tb4_key] if tb4_key else total - mimo
            if mimo + tb4 != total:
                raise ValueError(f"TS-4 {model} {name}: dataset shares do not sum to the total")
            conditions.append(
                {
                    "condition": name,
                    "static_match_n": counts[static_key] if static_key else None,
                    "executed_differential_n": total,
                    "executed_by_dataset": [
                        {"dataset_id": "mimo-defect-map-v1", "count_n": mimo},
                        {"dataset_id": "tb4-audit-v1", "count_n": tb4},
                    ],
                }
            )
        arms.append({"arm": model, "requested_model": model, "conditions": conditions})
    return {
        "schema": "evallab.seeker_executed_differentials/v1",
        "protocol": audit["schema"],
        "cohort": {
            "split": "dev",
            "key_count": audit["assignments_csv_key_count"],
            "audited_n": audit["total_tasks_audited"],
            "cross_key_excluded_n": audit["secondary_key_contaminated_count"],
            "task_count": task_count,
            "datasets": [{"dataset_id": name, "count_n": shares[name]} for name in _TS4_DATASETS],
        },
        "arms": arms,
        "generated_at": generated_at,
    }


# CM-4 corpus -> (role, dataset id, cross-key record in crosskey.json). The producer froze
# the cascade on Goodfire DEV before any CheatBench result (freeze.json, frozen_arm).
_CM4_COHORTS = {
    "goodfire": ("tune", "goodfire-reward-hack-data", "goodfire_sample"),
    "cheatbench": ("cross_test", "cheatbench-actions-v1", "cheatbench-actions-v1"),
}


def _cm4_cohort(
    corpus: str, summary_file: dict[str, Any], crosskey: dict[str, Any]
) -> dict[str, Any]:
    role, dataset_id, key = _CM4_COHORTS[corpus]
    summary, rows = summary_file["summary"], summary_file["traces"]
    positives = [row for row in rows if row["positive"]]
    unscored = [
        row for row in rows if not row["positive"] and row["stratum"].startswith("considered")
    ]
    negatives = [row for row in rows if not row["positive"] and row not in unscored]
    cascade, stage1 = summary["cascade"], summary["stage1"]
    counted = {
        "positives": (len(positives), summary["positives"]),
        "negatives": (len(negatives), summary["negatives"]),
        "traces": (len(rows), summary["traces"]),
        "cascade tp": (sum(1 for row in positives if row["alarmed"]), cascade["tp"]),
        "cascade fp": (sum(1 for row in negatives if row["alarmed"]), cascade["fp"]),
    }
    for label, (found, recorded) in counted.items():
        if found != recorded:
            raise ValueError(
                f"CM-4 {corpus}: {label} {found} disagrees with the summary {recorded}"
            )
    stage1_pos = sum(1 for row in positives if row["stage1"])
    stage1_neg = sum(1 for row in negatives if row["stage1"])
    if (
        round(100 * stage1_pos / len(positives), 1) != stage1["recall"]
        or round(100 * stage1_neg / len(negatives), 1) != stage1["far"]
    ):
        raise ValueError(f"CM-4 {corpus}: Stage-1 counts disagree with the summary rates")
    checked = crosskey[key]
    if checked["identities"] != len(rows):
        raise ValueError(
            f"CM-4 {corpus}: cross-key check covers {checked['identities']} identities, not {len(rows)}"
        )
    if checked["assigned_test_anywhere"] or checked["sealed_identity"]:
        raise ValueError(f"CM-4 {corpus}: cross-key check found non-DEV assignments")
    return {
        "role": role,
        "dataset_id": dataset_id,
        "split": "dev",
        "positive_n": len(positives),
        "clean_n": len(negatives),
        "unscored_n": len(unscored),
        "identities_checked_n": checked["identities"],
        "non_dev_assignment_n": 0,
        "stage1_positive_hit_n": stage1_pos,
        "stage1_clean_hit_n": stage1_neg,
        "true_positive_n": cascade["tp"],
        "false_positive_n": cascade["fp"],
        "judge_calls_n": summary["judge_calls"],
        "input_tokens_n": summary["input_tokens"],
        "output_tokens_n": summary["output_tokens"],
    }


def cm4_monitor_cascade(directory: Path, *, generated_at: str) -> dict[str, Any]:
    """Build the cascade report from CM-4's recorded freeze, scores, receipts and cross-key audit."""
    freeze_raw = read_owned_file(directory / "freeze.json")
    freeze = json.loads(freeze_raw)
    arm = freeze["frozen_arm"].split()[0].rstrip(",")
    crosskey = json.loads(read_owned_file(directory / "crosskey.json"))
    cohorts = []
    for corpus in _CM4_COHORTS:
        scored = json.loads(read_owned_file(directory / f"score2-{corpus}-{arm}.json"))
        if scored["summary"]["arm"] != arm:
            raise ValueError(f"CM-4 {corpus}: score file is not the frozen {arm!r} arm")
        cohorts.append(_cm4_cohort(corpus, scored, crosskey))
    receipts = [
        json.loads(line)
        for line in read_owned_file(directory / "judge-receipts.jsonl").decode().splitlines()
        if line.strip()
    ]
    served = sorted({row["served"] for row in receipts if row["arm"] == arm})
    if not served:
        raise ValueError("CM-4 has no judge receipts for the frozen arm")
    return {
        "schema": "evallab.monitor_cascade/v1",
        "system": "window-cascade",
        "cascade_version": freeze["cascade_version"],
        "arm": arm,
        "judge_models": served,
        "frozen_sha256": hashlib.sha256(freeze_raw).hexdigest(),
        "frozen_recorded_at": freeze["recorded_utc"],
        "freeze_statement": freeze["frozen_arm"],
        "cohorts": cohorts,
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
    ts4_manifest: Path | None = None,
    cm4_directory: Path | None = None,
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
    if ts4_manifest is not None:
        document = ts4_executed_differentials(
            json.loads(read_owned_file(ts4_manifest)), generated_at=stamp
        )
        artifacts.append(
            (
                "seeker-executed-differentials-ts4.json",
                _encode(document),
                f"built from the recorded TS-4 audit in {ts4_manifest.name}",
            )
        )
    if cm4_directory is not None:
        artifacts.append(
            (
                "monitor-cascade-cm4.json",
                _encode(cm4_monitor_cascade(cm4_directory, generated_at=stamp)),
                f"built from the recorded CM-4 results in {cm4_directory.name}",
            )
        )
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
