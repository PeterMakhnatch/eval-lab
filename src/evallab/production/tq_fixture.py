"""Separate, provenance-bound DEV fixtures for defensive Task Quality studies.

The canonical actor projector owns relational copying. This builder subsequently
keeps only the exported retriever's bounded failed-run diagnostics and explicitly
supplied source-labelled issues. It neither changes the source nor grants corpus
admission, upgrades labels, executes task code, or hydrates CAS objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.actor_projection import (
    _paths,
    project_actor_database,
    source_read_uri,
)
from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    _bounded_quality_evidence,
    admitted_cloud_task_ids,
)
from evallab.production.corpus_queue import CorpusRegistry


@dataclass(frozen=True)
class FixtureIssue:
    """A caller-reviewed issue and its source/diff custody, not inferred truth.

    ``issue`` uses CloudTraceDatabase's existing issue contract. Unconfirmed
    source claims must stay provisional; the normal retriever omits them.
    ``evidence`` supplies summary, source_sha256, source_locator, and either an
    attested retained repair_diff or an explicit repair_missing_reason.
    """

    issue: Mapping[str, Any]
    evidence: Mapping[str, Any]


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def build_task_quality_fixture(
    source_database: Path,
    destination_database: Path,
    *,
    registry: CorpusRegistry,
    task_ids: Iterable[str],
    expected_source_sha256: str,
    load_issues: Callable[[str], Iterable[FixtureIssue]],
) -> dict[str, Any]:
    """Publish one new SQLite fixture; return its per-row provenance manifest.

    Every requested identity must pass the canonical cross-key gate BEFORE the
    issue loader or actor projector can read content. The exact identity set is
    rechecked by the projector in its read-only source transaction. Only known
    additive legacy trust columns can default, always to provisional/empty.

    The source hash is checked before and after preparation. Publication is a
    no-clobber hard link on the destination filesystem, after database closure.
    The caller persists this returned manifest alongside its immutable fixture.
    No existing destination is replaced, including concurrent creations.
    """
    if not registry.has_assignment_authority():
        raise ValueError("fixture requires canonical split assignment authority")
    if isinstance(task_ids, (str, bytes)):
        raise TypeError("task_ids must be an explicit collection")
    requested = sorted(set(task_ids))
    if not requested or any(not isinstance(task, str) or not task.strip() for task in requested):
        raise ValueError("fixture requires explicit nonblank task identities")
    source, destination = _paths(source_database, destination_database)
    if _digest(source) != expected_source_sha256:
        raise ValueError("source snapshot digest differs from its pinned authority")
    with closing(sqlite3.connect(source_read_uri(source), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in requested)
        identities = [dict(row) for row in connection.execute(
            "SELECT task_id,dataset_id,split,original_grader_digest,repaired_grader_digest,"
            "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json,'$.item_id') END AS item_id,"
            "CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json,'$.item_id') ELSE 'invalid' END AS item_type "
            f"FROM corpus_tasks WHERE task_id IN ({marks}) ORDER BY task_id", requested,
        )]
    admitted = admitted_cloud_task_ids(identities, registry=registry)
    if admitted != frozenset(requested):
        raise ValueError("fixture identities are missing, unadmitted, sealed or TEST in another key")
    expected = {row["task_id"]: row for row in identities}
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.tq-", dir=destination.parent) as directory:
        staged = Path(directory) / "fixture.sqlite"
        projection = project_actor_database(
            source, staged, task_ids=requested, expected_identities=expected,
            allow_legacy_trust_defaults=True,
        )
        seed_sources: dict[str, dict[str, Any]] = {}
        with CloudTraceDatabase(staged) as database:
            retriever = CloudTraceRetriever(database, registry=registry)
            summaries = {task: retriever.failed_run_summary(task) for task in requested}
            tasks = {row["task_id"]: row for row in database._rows("SELECT * FROM corpus_tasks")}
            # Only this private derived file is reduced. No scripts/actions or
            # unbounded raw trace content survive in the published fixture.
            with database._transaction(write=True):
                database._execute("DELETE FROM trace_steps")
                database._execute("DELETE FROM agent_traces")
                database._execute("DELETE FROM task_issues")
            for task in requested:
                task_row = tasks[task]
                supplements: dict[str, Any] = {}
                for seed in load_issues(task):
                    issue, evidence = dict(seed.issue), dict(seed.evidence)
                    if issue.get("task_id") != task or not isinstance(issue.get("issue_id"), str):
                        raise ValueError("seed issue identity does not match its admitted task")
                    issue_id = issue["issue_id"]
                    if issue_id in seed_sources:
                        raise ValueError("seed issue identities must be unique")
                    bounded = _bounded_quality_evidence(evidence)
                    if bounded is None:
                        raise ValueError("seed evidence has invalid source/diff attestation")
                    # Existing labels and reward/trust fields are never inferred
                    # or upgraded here; the caller owns those source assertions.
                    issue["reproducer_script"] = None
                    database.upsert_task_issue(issue)
                    repair = evidence.get("repair_diff")
                    supplements[issue_id] = {
                        key: bounded[key] for key in (
                            "summary", "source_sha256", "source_locator", "repair_missing_reason",
                        )
                    } | {"repair_diff": None if repair is None else {
                        key: repair[key] for key in (
                            "text", "sha256", "original_files_sha256", "repaired_files_sha256", "kind",
                        )
                    }}
                    seed_sources[issue_id] = {
                        "source_sha256": evidence["source_sha256"],
                        "source_locator": evidence["source_locator"],
                        "evidence_sha256": hashlib.sha256(_canonical(evidence)).hexdigest(),
                    }
                metadata: dict[str, Any] = {"task_quality_evidence": supplements}
                if expected[task]["item_id"] is not None:
                    metadata["item_id"] = expected[task]["item_id"]
                task_row["metadata_json"] = metadata
                database.upsert_task(task_row)
                summary = summaries[task]
                if summary is not None:
                    database.upsert_trace({
                        key: summary[key] for key in (
                            "trace_id", "run_id", "task_id", "model_id", "outcome", "reward", "raw_cas_uri",
                        )
                    } | {"is_task_defect_fail": bool(summary["is_task_defect_fail"])}, steps=[
                        {"step_index": row["step_index"], "role": "tool", "observation_preview": row["observation"]}
                        for row in summary["observations"]
                    ])
            rows = []
            keys = {
                "corpus_tasks": ("task_id",), "task_issues": ("issue_id",),
                "agent_traces": ("trace_id",), "trace_steps": ("trace_id", "step_index"),
            }
            for table, primary in keys.items():
                for row in database._rows(f"SELECT * FROM {table} ORDER BY {','.join(primary)}"):
                    identity = {key: row[key] for key in primary}
                    provenance = seed_sources.get(row.get("issue_id"), {
                        "source_snapshot_sha256": expected_source_sha256,
                        "table": table, "identity": identity,
                    })
                    rows.append({
                        "table": table, "identity": identity, "provenance": provenance,
                        "row_sha256": hashlib.sha256(_canonical(row)).hexdigest(),
                    })
        with closing(sqlite3.connect(staged)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
        # DELETE does not erase SQLite free pages or old FTS segments. A second
        # canonical projection copies only the reduced live rows into a new
        # file, so excluded source content cannot survive physically.
        published = Path(directory) / "published.sqlite"
        reduced_identities = {
            task: identity | {"item_type": "text" if identity["item_id"] is not None else None}
            for task, identity in expected.items()
        }
        publication = project_actor_database(
            staged, published, task_ids=requested, expected_identities=reduced_identities,
        )
        if _digest(source) != expected_source_sha256:
            raise ValueError("source snapshot changed during fixture preparation")
        with published.open("rb") as stream:
            fixture_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            os.fsync(stream.fileno())
        manifest = {
            "schema": "evallab.task_quality_fixture/v1", "source_snapshot_sha256": expected_source_sha256,
            "split_assignments_sha256": registry.split_assignments_digest,
            "admitted_task_ids": requested, "database_sha256": fixture_digest,
            "source_projection": projection, "publication_projection": publication, "rows": rows,
            "failed_run_summaries": sum(value is not None for value in summaries.values()),
            "issues": len(seed_sources), "new_executions": 0,
        }
        os.link(published, destination, follow_symlinks=False)
    return manifest
