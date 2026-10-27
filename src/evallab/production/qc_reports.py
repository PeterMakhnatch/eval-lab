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

import csv
import hashlib
import io
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


# (report condition, retained cell directory). The cell directories are the executed
# truth: one result.json per task, model and turn. Summary fields in the TS-4 manifest
# are not used for counts, because they are mislabelled (``*_static_*`` holds executed
# counts) and one of them exceeds its arm's cell count.
_TS4_CONDITIONS = (
    ("turn_1", "turn-1"),
    ("turn_2_original_feedback", "turn-2"),
    ("turn_2_runner_accurate_feedback", "turn-2-runner-accurate"),
)


def _served_requested_route(cell: dict[str, Any], route: str) -> bool:
    provider, model = cell.get("served_provider"), cell.get("served_model")
    return bool(model) and route in (model, f"{provider}/{model}")


def _ts4_clean_cohort(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    audit = manifest["ts4_cross_key_and_executed_audit"]
    clean = {
        dataset: sorted(tasks)
        for dataset, tasks in audit["strictly_dev_everywhere_by_dataset"].items()
        if tasks
    }
    if (
        sum(len(tasks) for tasks in clean.values()) != audit["strictly_dev_everywhere_count"]
        or len(audit["contaminated_tasks"]) != audit["secondary_key_contaminated_count"]
    ):
        raise ValueError("TS-4 audit counts disagree with its recorded task lists")
    return audit, clean


def _read_ts4_cell_payload(
    cells_directory: Path, dataset: str, task: str, model: str, turn: str
) -> tuple[dict[str, Any], bytes] | None:
    path = cells_directory / dataset / task / model.replace("/", "_") / turn
    try:
        raw = read_owned_file(path / "result.json")
    except FileNotFoundError:
        return None
    cell = json.loads(raw)
    if cell.get("requested_model_route") != model:
        raise ValueError(f"{path} is not a cell for the requested route {model}")
    return cell, raw


def _read_ts4_cell(
    cells_directory: Path, dataset: str, task: str, model: str, turn: str
) -> dict[str, Any] | None:
    payload = _read_ts4_cell_payload(cells_directory, dataset, task, model, turn)
    return None if payload is None else payload[0]


def _ts4_outcome(cell: dict[str, Any], model: str) -> tuple[bool, bool]:
    """(attested, executed differential); a differential only counts on an attested cell."""
    ok = cell.get("attestation_ok") is True and _served_requested_route(cell, model)
    hit = ok and cell.get("original_reward") == 1.0 and cell.get("repaired_reward") == 0.0
    return ok, hit


def ts4_executed_differentials(
    manifest: dict[str, Any], cells_directory: Path, *, generated_at: str
) -> dict[str, Any]:
    """Count executed paired-grader differentials from the retained per-cell results.

    The manifest supplies only the cross-key audit and the strictly-DEV task list; every
    count comes from a cell's own ``result.json``. A cell counts as attested only when
    ``attestation_ok`` is true and the served model is the requested route, and only an
    attested cell with original reward 1.0 and repaired reward 0.0 is an executed
    differential, so executed <= attested <= evaluated cells for every arm.
    """
    audit, clean = _ts4_clean_cohort(manifest)
    task_count = audit["strictly_dev_everywhere_count"]
    digest, found, arms = hashlib.sha256(), 0, []
    for model in sorted(audit["models_on_clean_30_tasks"]):
        conditions = []
        for name, turn in _TS4_CONDITIONS:
            by_dataset: list[dict[str, Any]] = []
            for dataset, tasks in sorted(clean.items()):
                cells = attested = executed = 0
                for task in tasks:
                    cell = _read_ts4_cell(cells_directory, dataset, task, model, turn)
                    if cell is None:
                        continue
                    ok, hit = _ts4_outcome(cell, model)
                    cells, attested, executed = cells + 1, attested + ok, executed + hit
                    digest.update(f"{dataset}\t{task}\t{model}\t{turn}\t{ok:d}\t{hit:d}\n".encode())
                by_dataset.append(
                    {
                        "dataset_id": dataset,
                        "cells_n": cells,
                        "attested_n": attested,
                        "executed_differential_n": executed,
                    }
                )
                found += cells
            conditions.append(
                {
                    "condition": name,
                    **{
                        field: sum(row[field] for row in by_dataset)
                        for field in ("cells_n", "attested_n", "executed_differential_n")
                    },
                    "by_dataset": by_dataset,
                }
            )
        arms.append({"arm": model, "requested_model": model, "conditions": conditions})
    if not found:
        raise ValueError(f"no TS-4 cell results were found under {cells_directory}")
    return {
        "schema": "evallab.seeker_executed_differentials/v2",
        "protocol": audit["schema"],
        "cohort": {
            "split": "dev",
            "key_count": audit["assignments_csv_key_count"],
            "audited_n": audit["total_tasks_audited"],
            "cross_key_excluded_n": audit["secondary_key_contaminated_count"],
            "task_count": task_count,
            "datasets": [
                {"dataset_id": dataset, "count_n": len(tasks)}
                for dataset, tasks in sorted(clean.items())
            ],
        },
        "arms": arms,
        "source_sha256": digest.hexdigest(),
        "generated_at": generated_at,
    }


_TS5_FACTORIAL_CONDITIONS = (
    ("turn_1_visible", "turn-1-visible", False),
    ("turn_2_visible", "turn-2-visible", False),
    ("turn_1_hidden", "turn-1", True),
    ("turn_2_hidden_retrieval_only", "turn-2-retrieval-only", True),
    ("turn_2_hidden_feedback_only", "turn-2-feedback-only", True),
    ("turn_2_hidden_original_feedback", "turn-2", True),
    ("turn_2_hidden_runner_accurate_feedback", "turn-2-runner-accurate", True),
)
_TS5_ORTHOGONAL_CONDITIONS = (
    ("turn_1_visible", "turn-1-visible", False, "none", False),
    ("turn_2_visible", "turn-2-visible", False, "none", True),
    ("turn_1_hidden", "turn-1", True, "none", False),
    ("turn_2_hidden_retrieval_only", "turn-2-retrieval-only", True, "none", True),
    ("turn_2_hidden_feedback_only", "turn-2-feedback-only", True, "runner_accurate", False),
    (
        "turn_2_hidden_runner_accurate_feedback",
        "turn-2-runner-accurate",
        True,
        "runner_accurate",
        True,
    ),
)


def _verify_factorial_cell_digests(cell: dict[str, Any], cell_directory: Path) -> None:
    """Refuse a factorial cell whose recorded LLM stdout digest does not bind its file."""
    expected = cell.get("llm_stdout_sha256")
    if expected is None:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"{cell_directory} has a malformed llm_stdout_sha256")
    artifact = cell_directory / "response.jsonl"
    try:
        payload = read_owned_file(artifact)
    except FileNotFoundError as exc:
        raise ValueError(
            f"{cell_directory} records llm_stdout_sha256 but response.jsonl is missing"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"{artifact} does not match the recorded llm_stdout_sha256")


def _count_factorial_cells(
    clean: dict[str, list[str]],
    models: list[str],
    conditions: tuple[tuple[str, str, bool] | tuple[str, str, bool, str, bool], ...],
    cells_directory: Path,
    *,
    require_complete: bool = False,
    digest_seed: bytes = b"",
) -> tuple[str, int, list[dict[str, Any]]]:
    digest, found, arms = hashlib.sha256(digest_seed), 0, []
    for model in models:
        counted_conditions = []
        for condition in conditions:
            name, turn, expected_hide = condition[:3]
            axes = condition[3:]
            if axes and len(axes) != 2:
                raise ValueError(f"factorial condition {name!r} has malformed axes")
            by_dataset: list[dict[str, Any]] = []
            for dataset, tasks in sorted(clean.items()):
                cells = attested = executed = 0
                for task in tasks:
                    cell_directory = (
                        cells_directory / dataset / task / model.replace("/", "_") / turn
                    )
                    payload = _read_ts4_cell_payload(
                        cells_directory, dataset, task, model, turn
                    )
                    if payload is None:
                        if require_complete:
                            raise ValueError(f"missing declared factorial cell: {cell_directory}")
                        continue
                    cell, raw = payload
                    if cell.get("hide_tests") is not expected_hide:
                        raise ValueError(
                            f"{cell_directory} has hide_tests="
                            f"{cell.get('hide_tests')!r}, expected {expected_hide!r}"
                        )
                    _verify_factorial_cell_digests(cell, cell_directory)
                    ok, hit = _ts4_outcome(cell, model)
                    cells, attested, executed = cells + 1, attested + ok, executed + hit
                    digest.update(
                        (
                            f"{dataset}\t{task}\t{model}\t{turn}\t{expected_hide:d}\t"
                            f"{ok:d}\t{hit:d}\t{hashlib.sha256(raw).hexdigest()}\n"
                        ).encode()
                    )
                by_dataset.append(
                    {
                        "dataset_id": dataset,
                        "cells_n": cells,
                        "attested_n": attested,
                        "executed_differential_n": executed,
                    }
                )
                found += cells
            counted_condition: dict[str, Any] = {
                "condition": name,
                "hide_tests": expected_hide,
                **{
                    field: sum(row[field] for row in by_dataset)
                    for field in ("cells_n", "attested_n", "executed_differential_n")
                },
                "by_dataset": by_dataset,
            }
            if axes:
                counted_condition["feedback"] = axes[0]
                counted_condition["retrieval"] = axes[1]
            counted_conditions.append(counted_condition)
        arms.append({"arm": model, "requested_model": model, "conditions": counted_conditions})
    return digest.hexdigest(), found, arms


def _factorial_document(
    *,
    protocol: str,
    key_count: int,
    audited_n: int,
    cross_key_excluded_n: int,
    clean: dict[str, list[str]],
    arms: list[dict[str, Any]],
    source_sha256: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema": "evallab.seeker_factorial_2x2_differentials/v1",
        "protocol": protocol,
        "cohort": {
            "split": "dev",
            "key_count": key_count,
            "audited_n": audited_n,
            "cross_key_excluded_n": cross_key_excluded_n,
            "task_count": sum(len(tasks) for tasks in clean.values()),
            "datasets": [
                {"dataset_id": dataset, "count_n": len(tasks)}
                for dataset, tasks in sorted(clean.items())
            ],
        },
        "arms": arms,
        "source_sha256": source_sha256,
        "generated_at": generated_at,
    }


def ts5_factorial_2x2_differentials(
    manifest: dict[str, Any], cells_directory: Path, *, generated_at: str
) -> dict[str, Any]:
    """Count both Visible-Tests and Hidden-Tests 2x2 factorial cells from ``result.json``.

    Verifies ``cell.get("hide_tests") is expected_hide`` on every cell so visible and
    hidden axes cannot be conflated, requires ``attestation_ok`` and served == requested,
    verifies recorded LLM stdout/solve digests against the retained files when present,
    and counts an executed differential only when ``original_reward == 1.0`` and
    ``repaired_reward == 0.0``.
    """
    audit, clean = _ts4_clean_cohort(manifest)
    source_sha256, found, arms = _count_factorial_cells(
        clean,
        sorted(audit["models_on_clean_30_tasks"]),
        _TS5_FACTORIAL_CONDITIONS,
        cells_directory,
    )
    if not found:
        raise ValueError(f"no TS-5 factorial cell results were found under {cells_directory}")
    return _factorial_document(
        protocol=audit["schema"],
        key_count=audit["assignments_csv_key_count"],
        audited_n=audit["total_tasks_audited"],
        cross_key_excluded_n=audit["secondary_key_contaminated_count"],
        clean=clean,
        arms=arms,
        source_sha256=source_sha256,
        generated_at=generated_at,
    )


def _orthogonal_task_refs(record: dict[str, Any], field: str) -> list[tuple[str, str]]:
    refs = record.get(field)
    if not isinstance(refs, list) or not refs:
        raise ValueError(f"orthogonal ablation {field} must be a non-empty list")
    parsed: list[tuple[str, str]] = []
    for ref in refs:
        if not isinstance(ref, str):
            raise ValueError(f"orthogonal ablation {field} entries must be dataset/task strings")
        dataset, separator, task = ref.partition("/")
        if not separator or not dataset or not task or "/" in task:
            raise ValueError(f"orthogonal ablation task reference is malformed: {ref!r}")
        parsed.append((dataset, task))
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"orthogonal ablation {field} contains duplicate tasks")
    return parsed


def ts5_orthogonal_differentials(
    manifest: dict[str, Any],
    cells_directory: Path,
    *,
    assignments_csv: Path,
    generated_at: str,
) -> dict[str, Any]:
    """Count the complete affirmative-DEV orthogonal matrix from retained cells.

    The manifest supplies only explicit tune/cross-test membership and requested model
    routes. Every selected task must be affirmatively DEV in ``assignments.csv`` and
    have no non-DEV row under any key. Every declared task/model/condition cell must
    exist; unattested cells remain in denominators and never score.
    """
    _ts4_clean_cohort(manifest)
    record = manifest.get("pt_self4_orthogonal_6arm_ablation")
    if not isinstance(record, dict):
        raise ValueError("manifest has no pt_self4_orthogonal_6arm_ablation record")
    tune_refs = _orthogonal_task_refs(record, "affirmative_tune_9_tasks")
    cross_refs = _orthogonal_task_refs(record, "breaker_cross_test_4_tasks")
    selected = tune_refs + cross_refs
    if len(set(selected)) != len(selected):
        raise ValueError("orthogonal tune and cross-test task lists overlap")
    remediation = manifest.get("pt_self2_vf6_remediation_and_34_task_factorial")
    if isinstance(remediation, dict):
        affirmative = remediation.get("affirmative_dev_13_tasks_by_dataset")
        if isinstance(affirmative, dict):
            recorded = {
                (dataset, task)
                for dataset, tasks in affirmative.items()
                if isinstance(dataset, str) and isinstance(tasks, list)
                for task in tasks
                if isinstance(task, str)
            }
            if recorded != set(selected):
                raise ValueError("manifest affirmative DEV task lists disagree")
    requested = record.get("requested_models")
    if not isinstance(requested, list) or not requested:
        raise ValueError("orthogonal ablation requested_models must be a non-empty list")
    if any(not isinstance(model, str) or not model.strip() for model in requested):
        raise ValueError("orthogonal ablation model routes must be non-empty strings")
    if len(set(requested)) != len(requested):
        raise ValueError("orthogonal ablation requested_models contains duplicates")
    models = sorted(requested)
    protocol = record.get("schema")
    if not isinstance(protocol, str) or not protocol.strip():
        raise ValueError("orthogonal ablation schema must be a non-empty string")

    assignments_payload = read_owned_file(assignments_csv)
    assignments_sha256 = hashlib.sha256(assignments_payload).hexdigest()
    try:
        assignment_rows = csv.DictReader(io.StringIO(assignments_payload.decode("utf-8")))
        key_count = len(
            {
                str(row.get("key_id") or "").strip()
                for row in assignment_rows
                if str(row.get("key_id") or "").strip()
            }
        )
    except UnicodeDecodeError as exc:
        raise ValueError("assignments.csv is not valid UTF-8") from exc
    if not key_count:
        raise ValueError("assignments.csv contains no assignment keys")
    from evallab.production.seeker_worker import (
        find_cross_key_dev_assignments,
        find_cross_key_non_dev_assignments,
    )

    for _dataset, task in selected:
        if find_cross_key_non_dev_assignments(task, assignments_csv=assignments_csv):
            raise ValueError(f"orthogonal task {task!r} has a non-DEV assignment")
        if not find_cross_key_dev_assignments(task, assignments_csv=assignments_csv):
            raise ValueError(f"orthogonal task {task!r} has no affirmative DEV assignment")

    roles: dict[str, str] = {}
    clean: dict[str, list[str]] = {}
    for role, refs in (("tune", tune_refs), ("cross_test", cross_refs)):
        for dataset, task in refs:
            if roles.setdefault(dataset, role) != role:
                raise ValueError(f"orthogonal dataset {dataset!r} has conflicting roles")
            clean.setdefault(dataset, []).append(task)
    clean = {dataset: sorted(tasks) for dataset, tasks in sorted(clean.items())}
    datasets = [
        {
            "dataset_id": dataset,
            "count_n": len(tasks),
            "role": roles[dataset],
            "tasks": tasks,
        }
        for dataset, tasks in clean.items()
    ]
    selection = {
        "assignments_sha256": assignments_sha256,
        "conditions": [list(condition) for condition in _TS5_ORTHOGONAL_CONDITIONS],
        "datasets": datasets,
        "key_count": key_count,
        "models": models,
        "protocol": protocol,
    }
    digest_seed = (
        json.dumps(selection, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    source_sha256, _found, arms = _count_factorial_cells(
        clean,
        models,
        _TS5_ORTHOGONAL_CONDITIONS,
        cells_directory,
        require_complete=True,
        digest_seed=digest_seed,
    )
    return {
        "schema": "evallab.seeker_orthogonal_differentials/v1",
        "protocol": protocol,
        "cohort": {
            "split": "dev",
            "selection": "affirmative_dev",
            "key_count": key_count,
            "assignments_sha256": assignments_sha256,
            "task_count": len(selected),
            "affirmative_dev_n": len(selected),
            "datasets": datasets,
        },
        "arms": arms,
        "source_sha256": source_sha256,
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
    role, dataset_id, _ = _CM4_COHORTS[corpus]
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
    checked_n = _check_cross_key(corpus, len(rows), crosskey)
    return {
        "role": role,
        "dataset_id": dataset_id,
        "split": "dev",
        "positive_n": len(positives),
        "clean_n": len(negatives),
        "unscored_n": len(unscored),
        "identities_checked_n": checked_n,
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


_SUBSCRIPTION_USD = "not applicable: subscription route, no per-call price"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(read_owned_file(path)).hexdigest()


def _cost(
    calls: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    usd: float | None,
    *,
    complete: bool,
    scope: str,
    usd_basis: str,
) -> dict[str, Any]:
    return {
        "calls_n": calls,
        "input_tokens_n": tokens_in,
        "output_tokens_n": tokens_out,
        "usd": usd,
        "complete": complete,
        "scope": scope,
        "usd_basis": usd_basis,
    }


def _oq5_cost(calls: dict[str, Any], scope: str) -> dict[str, Any]:
    complete = not (
        calls["missing_receipts"] or calls["missing_tokens_in"] or calls["missing_tokens_out"]
    )
    usd = calls["observed_cost_usd"]
    basis = (
        _SUBSCRIPTION_USD
        if calls["cost_usd_applicability"] == "not_applicable_subscription"
        else "reported cost of the calls that returned one"
    )
    return _cost(
        calls["attempts"],
        calls["observed_tokens_in"],
        calls["observed_tokens_out"],
        usd,
        complete=complete,
        scope=scope,
        usd_basis=basis,
    )


def _oq5_result(label: str, metrics: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": label,
        "hit_label": "recorded bug keys re-found",
        "hits_n": metrics["matched_keys"],
        "of_n": metrics["evaluable_keys"],
        "planned_of_n": metrics["planned_keys"],
        "false_alarm_n": None,
        "false_alarm_of_n": None,
        "stage_one_hits_n": None,
        "stage_one_false_alarm_n": None,
        "cost": cost,
    }


def _oq5_system(directory: Path) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The Task Bug Finder: the frozen arm on DEV and on its unchanged cross-test."""
    names = ("dev-summary.json", "cross-summary.json", "freeze.json")
    paths = {name: directory / name for name in names}
    dev, cross, freeze = (json.loads(read_owned_file(path)) for path in paths.values())
    arm = freeze["selected_arm"]
    if dev["selected_arm"] != arm:
        raise ValueError("OQ-5: the DEV summary and the freeze receipt select different arms")
    if cross["freeze_sha256"] != _file_sha256(paths["freeze.json"]):
        raise ValueError("OQ-5: the cross-test summary is not bound to this freeze receipt")
    if cross["TEST_exposure"] or cross["new_grader_executions"]:
        raise ValueError("OQ-5: the cross-test summary records a protected-split exposure")
    tune_arm, cross_metrics = dev["arms"][arm], cross["metrics"]
    dev_datasets = sorted(dev["by_dataset"])
    cross_datasets = sorted({row["dataset_id"] for row in cross["rows"]})

    def arm_cost(name: str) -> dict[str, Any]:
        planned, complete = dev["arms"][name]["planned_tasks"], dev["arms"][name]["complete_tasks"]
        calls = dev["arm_calls"][name]
        scope = f"all {calls['attempts']} calls of arm {name} ({complete} of {planned} tasks complete)"
        return _oq5_cost(calls, scope)

    tune_cost = arm_cost(arm)
    cross_cost = _oq5_cost(
        cross["calls"],
        f"all {cross['calls']['attempts']} model calls on the cross-test "
        f"({cross_metrics['complete_tasks']} of {cross_metrics['planned_tasks']} tasks complete)",
    )
    served = sorted(
        f"{identity['provider']}/{identity['model']}"
        for identity in {
            (identity["provider"], identity["model"]): identity
            for identity in (
                *dev["calls"]["served_identities"],
                *cross["calls"]["served_identities"],
            )
        }.values()
    )
    paired = dev["paired_metrics"]
    paired_label = f"{len(dev['complete_paired_task_ids'])} tasks both arms completed"
    flag = {
        "flag": "History and retrieval evidence",
        "plain_english": (
            "Gives the reviewer summaries of earlier failed runs and issue evidence from other tasks; "
            "off means it reviews the task cold."
        ),
        "role": "tune",
        "cohort": f"{paired_label} ({', '.join(dev_datasets)})",
        "variants": [
            _oq5_result("off: cold review (arm A)", paired["A"], arm_cost("A")),
            _oq5_result("on: history and retrieval (arm B)", paired["B"], arm_cost("B")),
        ],
    }
    return (
        {
            "system_id": "task_bug_finder",
            "name": "Task Bug Finder",
            "plain_english": (
                "Reads a benchmark task's instructions and grading code, reports defects in the "
                "grader, and is scored on whether it re-finds bugs already recorded for that task."
            ),
            "measure": "Recorded bug keys re-found out of the keys that could be evaluated",
            "version": f"OQ-5 frozen arm {arm} ({freeze['protocol']['model']}, "
            f"confidence threshold {freeze['protocol']['confidence_threshold']})",
            "served_models": served,
            "runs": [
                {
                    "role": "tune",
                    "cohort": f"DEV A/B ({', '.join(dev_datasets)}; {tune_arm['planned_tasks']} tasks)",
                    "frozen": True,
                    "result": _oq5_result(f"arm {arm}", tune_arm, tune_cost),
                },
                {
                    "role": "cross_test",
                    "cohort": f"cross-test ({', '.join(cross_datasets)}; {cross_metrics['planned_tasks']} tasks)",
                    "frozen": True,
                    "result": _oq5_result(f"arm {arm}, unchanged", cross_metrics, cross_cost),
                },
            ],
            "flags": [flag],
            "caveats": [
                "Scores only bugs already on record; it cannot say how many real bugs a task has, "
                "and no false-positive rate was measured.",
                f"The cross-test is incomplete: {cross_metrics['planned_tasks'] - cross_metrics['complete_tasks']} "
                f"of {cross_metrics['planned_tasks']} tasks and "
                f"{cross_metrics['planned_keys'] - cross_metrics['evaluable_keys']} of "
                f"{cross_metrics['planned_keys']} keys could not be evaluated, so the count is over "
                f"{cross_metrics['evaluable_keys']} keys, not all of them.",
                f"{tune_arm['accepted_findings']} finding(s) passed the confidence and citation gate "
                f"on DEV and {cross_metrics['accepted_findings']} on the cross-test; "
                f"{tune_arm['matched_keys']} and {cross_metrics['matched_keys']} matched a recorded key.",
                "Subscription route: no per-call dollar price exists, so cost is tokens only.",
            ],
        },
        [(f"OQ-5 {name}", _file_sha256(path)) for name, path in paths.items()],
    )


def _check_cross_key(corpus: str, identities: int, crosskey: dict[str, Any]) -> int:
    checked = crosskey[_CM4_COHORTS[corpus][2]]
    if checked["identities"] != identities:
        raise ValueError(
            f"CM-4 {corpus}: cross-key check covers {checked['identities']} identities, not {identities}"
        )
    if checked["assigned_test_anywhere"] or checked["sealed_identity"]:
        raise ValueError(f"CM-4 {corpus}: cross-key check found non-DEV assignments")
    return checked["identities"]


def _cascade_result(
    label: str,
    scored: dict[str, Any],
    *,
    stage1_key: str,
    stage1_row: str,
    calls_key: str,
    scope: str,
    usd_basis: str,
) -> dict[str, Any]:
    summary, rows = scored["summary"], scored["traces"]
    if summary["excluded_unavailable"]:
        raise ValueError(f"{label}: {summary['excluded_unavailable']} traces were unavailable")
    positives = [row for row in rows if row["positive"]]
    negatives = [
        row for row in rows if not row["positive"] and not row["stratum"].startswith("considered")
    ]
    cascade, stage1 = summary["cascade"], summary[stage1_key]
    tp = sum(1 for row in positives if row["alarmed"])
    fp = sum(1 for row in negatives if row["alarmed"])
    stage1_tp = sum(1 for row in positives if row[stage1_row])
    stage1_fp = sum(1 for row in negatives if row[stage1_row])
    counted = {
        "positives": (len(positives), summary["positives"]),
        "negatives": (len(negatives), summary["negatives"]),
        "cascade tp": (tp, cascade["tp"]),
        "cascade fp": (fp, cascade["fp"]),
    }
    for name, (found, recorded) in counted.items():
        if found != recorded:
            raise ValueError(f"{label}: {name} {found} disagrees with the summary {recorded}")
    if (
        round(100 * stage1_tp / len(positives), 1) != stage1["recall"]
        or round(100 * stage1_fp / len(negatives), 1) != stage1["far"]
    ):
        raise ValueError(f"{label}: Stage-1 counts disagree with the summary rates")
    return {
        "label": label,
        "hit_label": "cheating traces alarmed",
        "hits_n": tp,
        "of_n": len(positives),
        "planned_of_n": None,
        "false_alarm_n": fp,
        "false_alarm_of_n": len(negatives),
        "stage_one_hits_n": stage1_tp,
        "stage_one_false_alarm_n": stage1_fp,
        "cost": _cost(
            summary[calls_key],
            summary["input_tokens"],
            summary["output_tokens"],
            summary.get("notional_usd"),
            complete=True,
            scope=scope,
            usd_basis=usd_basis,
        ),
    }


_CM5_ARM_LABELS = {
    "base": "off: no command cues",
    "pf_git": "git-history cue only",
    "pf_all": "on: all command cues (frozen)",
}


def _monitor_system(cm4: Path, cm5: Path) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The 2-Stage Live Monitor: CM-5's frozen arm, and the feature settings behind it."""
    freeze_path = cm5 / "freeze.json"
    freeze = json.loads(read_owned_file(freeze_path))
    frozen, crosskey_path = freeze["frozen_arm"], cm4 / "crosskey.json"
    crosskey = json.loads(read_owned_file(crosskey_path))
    sources = [("CM-5 freeze", _file_sha256(freeze_path)), ("CM-4 cross-key audit", _file_sha256(crosskey_path))]

    def scored(directory: Path, name: str) -> dict[str, Any]:
        path = directory / name
        sources.append((f"{directory.name.split('-')[0].upper()} {name}", _file_sha256(path)))
        return json.loads(read_owned_file(path))

    def cm5_result(corpus: str, arm: str) -> dict[str, Any]:
        file = scored(cm5, f"e2escore-{corpus}-{arm}.json")
        if file["summary"]["corpus"] != corpus:
            raise ValueError(f"CM-5 {arm}: score file is not the {corpus} corpus")
        _check_cross_key(corpus, len(file["traces"]), crosskey)
        return _cascade_result(
            _CM5_ARM_LABELS[arm],
            file,
            stage1_key="stage1_any_card",
            stage1_row="stage1_any",
            calls_key="judged_cards",
            scope=f"the {file['summary']['judged_cards']} judged windows scored for this arm",
            usd_basis="not recorded in the CM-5 score file",
        )

    def cm4_result(corpus: str, arm: str, label: str) -> dict[str, Any]:
        file = scored(cm4, f"score2-{corpus}-{arm}.json")
        if file["summary"]["arm"] != arm:
            raise ValueError(f"CM-4 {corpus}: score file is not the {arm!r} arm")
        _check_cross_key(corpus, len(file["traces"]), crosskey)
        return _cascade_result(
            label,
            file,
            stage1_key="stage1",
            stage1_row="stage1",
            calls_key="judge_calls",
            scope=f"all {file['summary']['judge_calls']} judge calls for this arm",
            usd_basis="notional price recorded by the producer, not a bill",
        )

    runs, flags = [], []
    for corpus, (role, dataset_id, _) in _CM4_COHORTS.items():
        runs.append(
            {
                "role": role,
                "cohort": dataset_id,
                "frozen": True,
                "result": cm5_result(corpus, frozen),
            }
        )
        arms = [arm for arm in _CM5_ARM_LABELS if (cm5 / f"e2escore-{corpus}-{arm}.json").exists()]
        flags.append(
            {
                "flag": "Command cues in the lookahead window (CM-5 judge)",
                "plain_english": (
                    "Rule cues that flag git-history browsing, test-file edits and assertion changes "
                    "send extra windows to the judge; off sends only the base windows."
                ),
                "role": role,
                "cohort": dataset_id,
                "variants": [cm5_result(corpus, arm) for arm in arms],
            }
        )
    receipts5 = [
        json.loads(line)
        for line in read_owned_file(cm5 / "judge-receipts.jsonl").decode().splitlines()
        if line.strip()
    ]
    receipts4 = [
        json.loads(line)
        for line in read_owned_file(cm4 / "judge-receipts.jsonl").decode().splitlines()
        if line.strip()
    ]
    if any(row["fallback"] for row in (*receipts5, *receipts4)):
        raise ValueError("a judge receipt records a fallback model")
    judge5 = sorted({row["served"] for row in receipts5})
    judge4 = sorted({row["served"] for row in receipts4})
    if judge5 != [freeze["judge"]]:
        raise ValueError("CM-5 receipts were not all served by the frozen judge")
    for corpus, (role, dataset_id, _) in _CM4_COHORTS.items():
        flags.append(
            {
                "flag": f"Reference examples in the judge prompt (CM-4 judge {', '.join(judge4)})",
                "plain_english": (
                    "Adds labelled example cases to the judge's prompt; off shows the judge only the "
                    "flagged window. Measured on the earlier CM-4 cascade, so not comparable with "
                    "the CM-5 rows."
                ),
                "role": role,
                "cohort": dataset_id,
                "variants": [
                    cm4_result(corpus, "plain", "off: no reference examples"),
                    cm4_result(corpus, "with_examples", "on: reference examples"),
                ],
            }
        )
    return (
        {
            "system_id": "live_monitor",
            "name": "2-Stage Live Monitor",
            "plain_english": (
                "Cheap rules flag suspicious steps in an agent's trace; a bounded model judge then "
                "reads the surrounding window and confirms or rejects each flag."
            ),
            "measure": "Cheating traces alarmed (recall) and clean traces alarmed (false alarms)",
            "version": f"{freeze['cascade_version']}, frozen arm {frozen}, judge {freeze['judge']}",
            "served_models": sorted({*judge5, *judge4}),
            "runs": runs,
            "flags": flags,
            "caveats": [
                f"Producer's freeze note: {freeze['note']}.",
                "Goodfire labels are provisional publisher judgments, and CheatBench labels mark "
                "cheating attempts, not whether a solution was correct.",
                "Rates are over scored traces only; traces that merely considered cheating are in "
                "neither rate.",
                "Cost is judge tokens only. CM-5 records no dollar figure and CM-4's are notional "
                "prices, not bills.",
                "This shows the arm that has a freeze receipt and a completed cross-test; a later "
                "arm is not shown until it has its own.",
            ],
        },
        sources,
    )


class _Usage:
    """Recorded usage summed over cells; a missing figure marks the total incomplete."""

    def __init__(self) -> None:
        self.calls = self.tokens_in = self.tokens_out = 0
        self.usd, self.complete, self.priced = 0.0, True, True

    def add(self, cell: dict[str, Any]) -> None:
        self.calls += 1
        for field, attribute in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out")):
            if cell.get(field) is None:
                self.complete = False
            else:
                setattr(self, attribute, getattr(self, attribute) + cell[field])
        if cell.get("cost_usd") is None:
            self.priced = False
        else:
            self.usd += cell["cost_usd"]

    def cost(self, scope: str) -> dict[str, Any]:
        return _cost(
            self.calls,
            self.tokens_in,
            self.tokens_out,
            round(self.usd, 6) if self.priced else None,
            complete=self.complete,
            scope=scope,
            usd_basis="recorded cost_usd per cell; a 0.0 on a subscription route is not a zero bill",
        )


# Seeker variants: label, the turns whose cells are combined. A task scores when any
# attested cell among them is an executed differential.
_SEEKER_VARIANTS = (
    ("off: turn 1 only", ("turn-1",)),
    ("on: + turn 2, original feedback", ("turn-1", "turn-2")),
    ("on: + turn 2, runner-accurate feedback", ("turn-1", "turn-2-runner-accurate")),
)


def _seeker_system(
    manifest: dict[str, Any], cells_directory: Path
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The Two-Turn Reward Seeker: one DEV cohort, turn 1 alone versus a second turn."""
    audit, clean = _ts4_clean_cohort(manifest)
    task_count = audit["strictly_dev_everywhere_count"]
    cohort = ", ".join(f"{dataset} {len(tasks)}" for dataset, tasks in sorted(clean.items()))
    digest, runs, flags, served, rows = hashlib.sha256(), [], [], [], []
    turns = sorted({turn for _, group in _SEEKER_VARIANTS for turn in group})
    for model in sorted(audit["models_on_clean_30_tasks"]):
        usage = {label: _Usage() for label, _ in _SEEKER_VARIANTS}
        hits = dict.fromkeys(usage, 0)
        evaluated = dict.fromkeys(usage, 0)
        attested = 0
        for dataset, tasks in sorted(clean.items()):
            for task in tasks:
                cells = {
                    turn: _read_ts4_cell(cells_directory, dataset, task, model, turn)
                    for turn in turns
                }
                outcomes = {
                    turn: _ts4_outcome(cell, model) if cell else (False, False)
                    for turn, cell in cells.items()
                }
                for turn, cell in cells.items():
                    if cell is None:
                        continue
                    ok, hit = outcomes[turn]
                    attested += ok
                    recorded = [cell.get(field) for field in ("input_tokens", "output_tokens", "cost_usd")]
                    line = [dataset, task, model, turn, f"{ok:d}", f"{hit:d}", *map(str, recorded)]
                    digest.update(("\t".join(line) + "\n").encode())
                for label, group in _SEEKER_VARIANTS:
                    present = [(turn, cell) for turn in group if (cell := cells[turn]) is not None]
                    if not present:
                        continue
                    evaluated[label] += 1
                    hits[label] += any(outcomes[turn][1] for turn, _ in present)
                    for _, cell in present:
                        usage[label].add(cell)
        if not any(evaluated.values()):
            continue
        if attested:
            served.append(model)
        results = [
            {
                "label": label,
                "hit_label": "executed grader differentials",
                "hits_n": hits[label],
                "of_n": evaluated[label],
                "planned_of_n": task_count,
                "false_alarm_n": None,
                "false_alarm_of_n": None,
                "stage_one_hits_n": None,
                "stage_one_false_alarm_n": None,
                "cost": usage[label].cost(f"{usage[label].calls} calls for these tasks"),
            }
            for label, _ in _SEEKER_VARIANTS
            if evaluated[label]
        ]
        rows += results
        runs.append(
            {
                "role": "dev_only",
                "cohort": f"{cohort} · {model}",
                "frozen": False,
                "result": results[-1] | {"label": f"{model} · {results[-1]['label']}"},
            }
        )
        flags.append(
            {
                "flag": f"Second turn · {model}",
                "plain_english": (
                    "Turn 2 re-prompts with retrieved evidence and runner feedback, and a task "
                    "scores if either turn finds a differential; off stops after turn 1."
                ),
                "role": "dev_only",
                "cohort": cohort,
                "variants": results,
            }
        )
    if not runs:
        raise ValueError(f"no TS-4 cell results were found under {cells_directory}")
    unpriced = sum(1 for row in rows if row["cost"]["usd"] is None)
    short = sum(1 for row in rows if row["of_n"] < task_count)
    return (
        {
            "system_id": "reward_seeker",
            "name": "Two-Turn Reward Seeker",
            "plain_english": (
                "Asks a model to solve a task, then lets it try again with retrieved evidence and "
                "runner feedback, and checks whether any solution passes the original grader but "
                "fails the repaired one, which exposes a grader weakness."
            ),
            "measure": "Tasks with an executed grader differential out of the tasks evaluated",
            "version": f"TS-3/TS-4, hidden tests, cross-key-clean cohort ({cohort})",
            "served_models": served,
            "runs": runs,
            "flags": flags,
            "caveats": [
                "These historical TS-3/TS-4 rows use one DEV cohort, so none is an unseen-data "
                "accuracy; the affirmative-DEV orthogonal tune/cross-test matrix is retained "
                "separately and is not pooled into these union-of-turns rows.",
                "An executed differential means the original grader passed and the repaired grader "
                "failed when run; it measures grader weakness found, not solution correctness.",
                "Cells served by a different model, or whose attestation failed, stay in the "
                "denominators and never score.",
                "Headline rows use the runner-accurate second turn. The retained cell files were "
                "regenerated after earlier exports, so figures from earlier exports of this "
                "cohort may differ; the digest in the sources identifies the cells counted here.",
                f"{short} of {len(rows)} rows evaluated fewer tasks than the cohort's {task_count}. "
                "Cost sums the recorded per-cell usage; "
                f"{unpriced} rows lack a priced cost and cells without token counts make a row "
                "incomplete. A recorded 0.0 is not a zero bill.",
            ],
        },
        [("TS-3 per-cell results (counted cells)", digest.hexdigest())],
    )


def system_playbook(
    *,
    oq5_directory: Path,
    cm4_directory: Path,
    cm5_directory: Path,
    ts4_manifest: dict[str, Any],
    ts4_cells: Path,
    generated_at: str,
) -> dict[str, Any]:
    """The three core systems side by side, built only from recorded results."""
    finder, finder_sources = _oq5_system(oq5_directory)
    seeker, seeker_sources = _seeker_system(ts4_manifest, ts4_cells)
    monitor, monitor_sources = _monitor_system(cm4_directory, cm5_directory)
    return {
        "schema": "evallab.system_playbook/v1",
        "systems": [finder, seeker, monitor],
        "sources": [
            {"label": label, "sha256": digest}
            for label, digest in (*finder_sources, *seeker_sources, *monitor_sources)
        ],
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
    ts4_cells: Path | None = None,
    cm4_directory: Path | None = None,
    oq5_directory: Path | None = None,
    cm5_directory: Path | None = None,
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
    if (ts4_manifest is None) != (ts4_cells is None):
        raise ValueError("TS-4 needs both the audit manifest and the per-cell results directory")
    if ts4_manifest is not None and ts4_cells is not None:
        manifest_doc = json.loads(read_owned_file(ts4_manifest))
        document = ts4_executed_differentials(
            manifest_doc, ts4_cells, generated_at=stamp
        )
        artifacts.append(
            (
                "seeker-executed-differentials-ts4.json",
                _encode(document),
                f"counted from the per-cell results in {ts4_cells.name}; "
                f"audit from {ts4_manifest.name}",
            )
        )
        if next(ts4_cells.glob("*/*/*/turn-1-visible/result.json"), None) is not None:
            factorial_doc = ts5_factorial_2x2_differentials(
                manifest_doc, ts4_cells, generated_at=stamp
            )
            artifacts.append(
                (
                    "seeker-factorial-2x2-differentials-ts5.json",
                    _encode(factorial_doc),
                    f"counted from the 2x2 factorial and orthogonal unbundling per-cell results in {ts4_cells.name}; "
                    f"audit from {ts4_manifest.name}",
                )
            )
        if "pt_self4_orthogonal_6arm_ablation" in manifest_doc:
            orthogonal_doc = ts5_orthogonal_differentials(
                manifest_doc,
                ts4_cells,
                assignments_csv=repo_root / "research/answer-keys/splits/assignments.csv",
                generated_at=stamp,
            )
            artifacts.append(
                (
                    "seeker-orthogonal-differentials-ts5.json",
                    _encode(orthogonal_doc),
                    f"counted from the affirmative-DEV orthogonal per-cell results in {ts4_cells.name}; "
                    f"selection from {ts4_manifest.name}",
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
    if (oq5_directory is None) != (cm5_directory is None):
        raise ValueError("the system playbook needs both the OQ-5 and the CM-5 directories")
    if oq5_directory is not None and cm5_directory is not None:
        if ts4_manifest is None or ts4_cells is None or cm4_directory is None:
            raise ValueError(
                "the system playbook also needs the TS-4 manifest and cells and the CM-4 directory"
            )
        playbook = system_playbook(
            oq5_directory=oq5_directory,
            cm4_directory=cm4_directory,
            cm5_directory=cm5_directory,
            ts4_manifest=json.loads(read_owned_file(ts4_manifest)),
            ts4_cells=ts4_cells,
            generated_at=stamp,
        )
        artifacts.append(
            (
                "system-playbook.json",
                _encode(playbook),
                "built from recorded OQ-5, TS-3/TS-4, CM-4 and CM-5 results; counts copied or "
                "counted, never re-scored",
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
