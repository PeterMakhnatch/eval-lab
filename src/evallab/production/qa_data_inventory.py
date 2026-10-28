"""Admission-first QA listings and source-snapshot counts, never blind-reader inputs."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import _json_text, _sealed_pattern
from evallab.production.cloud_trace_export import CHEATBENCH_DATASET, cheatbench_item_admitted
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError
from evallab.production.run_atlas import outcome_from_reward
from evallab.production.run_atlas_clusters import load_clusters


def _reserve(ctx: QaContext, payload: dict[str, Any]) -> None:
    """Charge keys/counts and a small allowance for separators and growing omission counts."""
    if not ctx.budget.take({**payload, "_framing": " " * 64}):
        raise QaDataError("this call's summary does not fit; raise --budget")


def _tasks_and_traces(ctx: QaContext, owner: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Caller owns the snapshot: admission and both identity projections are inseparable."""
    admitted, _sealed = ctx.retriever._admitted_ids()
    if owner is not None and owner not in admitted:
        raise QaDataError(UNAVAILABLE)
    task_parameters: list[Any] = []
    task_scope = ctx.database._task_id_scope("c.task_id", admitted, task_parameters)
    tasks = ctx.database._rows(
        "SELECT c.task_id, c.dataset_id FROM corpus_tasks c "
        f"WHERE {task_scope} ORDER BY c.task_id", task_parameters,
    )
    trace_parameters: list[Any] = []
    trace_scope = ctx.database._task_id_scope("a.task_id", admitted, trace_parameters)
    rows = ctx.database._rows(
        "SELECT a.trace_id, a.task_id, c.dataset_id, a.model_id, a.reward, a.step_count, "
        "CASE WHEN f.run_id IS NULL THEN 0 ELSE 1 END AS faceted FROM agent_traces a "
        "JOIN corpus_tasks c ON c.task_id=a.task_id "
        "LEFT JOIN run_facets f ON f.run_id=a.trace_id AND f.task_id=a.task_id "
        f"WHERE {trace_scope} "
        "ORDER BY c.dataset_id, a.task_id, a.model_id, a.trace_id", trace_parameters,
    )
    return tasks, rows


def inventory(ctx: QaContext, *, ids_out: Path | None) -> dict[str, Any]:
    """Count every admitted task/run, including unfaceted runs; optionally create an exclusive id file."""
    with ctx.database._transaction():
        tasks, rows = _tasks_and_traces(ctx)
    task_counts = Counter(row["dataset_id"] for row in tasks)
    run_counts = Counter(row["dataset_id"] for row in rows)
    outcomes = Counter(outcome_from_reward(row["reward"]) for row in rows)
    models = Counter(row["model_id"] for row in rows)
    capped_models = max(0, len(models) - 12)
    payload: dict[str, Any] = {
        "admitted_tasks": len(tasks), "admitted_runs": len(rows), "by_dataset": {},
        "run_outcomes": {key: outcomes[key] for key in ("pass", "fail", "unknown")},
        "models": {}, "models_total": len(models), "omitted": capped_models, "truncated": bool(capped_models),
    }
    if ids_out is not None:
        payload["ids_file"] = {"path": str(ids_out), "lines": len(rows)}
    _reserve(ctx, payload)
    ctx.budget.omitted += capped_models
    for dataset in sorted(task_counts.keys() | run_counts.keys()):
        value = {"tasks": task_counts[dataset], "runs": run_counts[dataset]}
        if ctx.budget.take({dataset: value}):
            payload["by_dataset"][dataset] = value
        else:
            payload["omitted"] += 1
    for model, count in sorted(models.items(), key=lambda pair: (-pair[1], pair[0]))[:12]:
        if ctx.budget.take({model: count}):
            payload["models"][model] = count
        else:
            payload["omitted"] += 1
    payload["truncated"] = payload["omitted"] > 0
    if ids_out is not None:
        try:
            with ids_out.open("x", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(row["trace_id"] + "\n")
        except OSError as exc:
            raise QaDataError("cannot create ids file: it must not exist and its parent must exist") from exc
    return payload


def traces(
    ctx: QaContext, *, task_id: str | None, dataset: str | None, model: str | None,
    outcome: str | None, limit: int, ids_only: bool,
) -> dict[str, Any]:
    """List all admitted runs, faceted or not. Records expose dataset/model for QA, NOT blind readers."""
    if limit < 1:
        raise QaDataError("--limit must be at least 1")
    if outcome is not None and outcome not in ("pass", "fail", "unknown"):
        raise QaDataError("--outcome must be pass, fail or unknown")
    owner = ctx.require_task(task_id)["task_id"] if task_id is not None else None
    with ctx.database._transaction():
        _tasks, rows = _tasks_and_traces(ctx, owner)
    matched = []
    for row in rows:
        derived = outcome_from_reward(row["reward"])
        if owner is not None and row["task_id"] != owner:
            continue
        if dataset is not None and row["dataset_id"] != dataset:
            continue
        if model is not None and row["model_id"] != model:
            continue
        if outcome is not None and derived != outcome:
            continue
        matched.append(row["trace_id"] if ids_only else {
            key: row[key] for key in ("trace_id", "task_id", "dataset_id", "model_id", "step_count")
        } | {"outcome": derived, "faceted": bool(row["faceted"])})
    payload: dict[str, Any] = {"traces": [], "matched": len(matched), "omitted": len(matched), "truncated": bool(matched)}
    if owner is not None:
        payload["task_id"] = owner
    _reserve(ctx, payload)
    payload["traces"] = [item for item in matched[:limit] if ctx.budget.take([item] if ids_only else item)]
    ctx.budget.omitted += max(0, len(matched) - limit)
    payload["omitted"] = len(matched) - len(payload["traces"])
    payload["truncated"] = payload["omitted"] > 0
    return payload


def _registry_dev_ids(ctx: QaContext) -> set[str]:
    registry = ctx.retriever.registry
    return {
        item.item_id for item in registry.items(dataset_id=CHEATBENCH_DATASET, split="dev")
        if cheatbench_item_admitted(registry, item)
    } if registry is not None else set()


def _members(ctx: QaContext, payload: dict[str, Any], groups: dict[str, list[str]]) -> dict[str, Any]:
    total = sum(map(len, groups.values()))
    payload.update({key: [] for key in groups})
    payload.update(omitted=total, truncated=bool(total))
    _reserve(ctx, payload)
    for key, values in groups.items():
        payload[key] = [value for value in values if ctx.budget.take([value])]
    payload["omitted"] = total - sum(len(payload[key]) for key in groups)
    payload["truncated"] = payload["omitted"] > 0
    return payload


def clusters(ctx: QaContext, *, members: str | None, source: str, level: int | None = None) -> dict[str, Any]:
    """Project admitted members; listings sort by level, descending admitted size, then id, optionally at one level."""
    if source not in ("db", "file"):
        raise QaDataError("--source must be db or file")
    if level is not None and level not in (1, 2, 3):
        raise QaDataError("--level must be 1, 2 or 3")
    with ctx.database._transaction():
        _tasks, traces_ = _tasks_and_traces(ctx)
        cloud = {row["trace_id"] for row in traces_}
        all_cloud = {row[0] for row in ctx.database._execute("SELECT trace_id FROM agent_traces").fetchall()}
        registry_only = _registry_dev_ids(ctx) - all_cloud
        nodes = ctx.database._rows(
            "SELECT cluster_id, level, parent, run_ids, provenance_json "
            "FROM run_clusters ORDER BY level, cluster_id",
        ) if source == "db" else []
    allowed = cloud | registry_only
    if source == "file":
        try:
            nodes = load_clusters(ctx.state_dir / "run-atlas" / "clusters-v1.jsonl")
        except (OSError, ValueError) as exc:
            raise QaDataError("the cluster build file cannot be read") from exc
    selected = next((node for node in nodes if node["cluster_id"] == members), None) if members is not None else None
    if members is not None:
        if selected is None:
            raise QaDataError(UNAVAILABLE)
        ids = set(selected["run_ids"])
        if not ids & (cloud if source == "db" else allowed):
            raise QaDataError(UNAVAILABLE)
        build_id = selected.get("build_id") if source == "file" else selected["provenance_json"].get("build_id")
        payload: dict[str, Any] = {"build_id": build_id, "cluster_id": selected["cluster_id"]}
        if source == "db":
            return _members(ctx, payload, {"admitted_members": sorted(ids & cloud)})
        cloud_run_ids = sorted(ids & cloud)
        registry_only_dev_ids = sorted(ids & registry_only)
        payload.update(
            cloud_runs=len(cloud_run_ids), registry_only_dev=len(registry_only_dev_ids),
            admitted_members=len(cloud_run_ids) + len(registry_only_dev_ids),
        )
        return _members(ctx, payload, {
            "cloud_run_ids": cloud_run_ids, "registry_only_dev_ids": registry_only_dev_ids,
        })
    projected = []
    for node in nodes:
        ids = set(node["run_ids"])
        visible = ids & (cloud if source == "db" else allowed)
        if not visible:
            continue
        item = {key: node[key] for key in ("cluster_id", "level", "parent")}
        item["build_id"] = node.get("build_id") if source == "file" else node["provenance_json"].get("build_id")
        if source == "db":
            item["admitted_size"] = len(visible)
        else:
            cloud_runs = len(ids & cloud)
            item.update(
                admitted_members=len(visible), cloud_runs=cloud_runs,
                registry_only_dev=len(visible) - cloud_runs,
            )
        projected.append(item)
    builds = {item["build_id"] for item in projected}
    size_key = "admitted_size" if source == "db" else "admitted_members"
    projected.sort(key=lambda item: (item["level"], -item[size_key], item["cluster_id"]))
    if level is not None:
        projected = [item for item in projected if item["level"] == level]
    if len(builds) == 1:
        for item in projected:
            del item["build_id"]
    payload = {
        "build_id": next(iter(builds)) if len(builds) == 1 else None,
        "clusters": [], "omitted": len(projected), "truncated": bool(projected),
    }
    if level is not None:
        payload["level"] = level
    if source == "file":
        payload["build_id"] = nodes[0]["build_id"] if nodes else None
        payload["facet_count"] = nodes[0]["facet_count"] if nodes else 0
        payload["levels"] = dict(sorted(Counter(item["level"] for item in projected).items()))
    _reserve(ctx, payload)
    payload["clusters"] = [item for item in projected if ctx.budget.take(item)]
    payload["omitted"] = len(projected) - len(payload["clusters"])
    payload["truncated"] = payload["omitted"] > 0
    return payload


def _facet_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "admitted_cloud_runs": len(rows),
        "answer_material_accessed": sum(bool(row["answer_material_accessed"]) for row in rows),
        "suspicion": dict(sorted(Counter(row["suspicion"] for row in rows).items())),
        "tests": dict(sorted(Counter(row["tests"] for row in rows).items())),
    }


def recount(ctx: QaContext) -> dict[str, Any]:
    """Bind append-only facet counts (last record wins) to a shared-lock SHA256 snapshot.

    Records naming a sealed identity are left out of every count, exactly as ``runs`` withholds
    them, so the two calls agree on which runs exist and no count reveals a withheld one.
    """
    with ctx.database._transaction():
        _tasks, traces_ = _tasks_and_traces(ctx)
        cloud = {row["trace_id"] for row in traces_}
        sealed = _sealed_pattern(frozenset(ctx.retriever._admitted_ids()[1]))

        def shown(record: dict[str, Any]) -> bool:
            return not ctx.retriever._sealed_text(_json_text(record), sealed)

        every = ctx.database._rows("SELECT * FROM run_facets")
        stored = [row for row in every if shown(row)]
        db_ids = {row["run_id"] for row in stored}
        withheld = {row["run_id"] for row in every} - db_ids
        owner = {(row["trace_id"], row["task_id"]) for row in traces_}
        db = [row for row in stored if (row["run_id"], row["task_id"]) in owner]
    try:
        with (ctx.state_dir / "run-atlas" / "facets-v1.jsonl").open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            raw = stream.read()
        lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
        latest = {row["run_id"]: row for row in lines if row["run_id"] not in withheld and shown(row)}
        jsonl = [row for run_id, row in latest.items() if run_id in cloud]
        payload = {
            "jsonl_sha256": hashlib.sha256(raw).hexdigest(), "jsonl_lines": len(lines),
            "jsonl_unique_runs": len(latest), "db_facets": len(db_ids),
            "jsonl_only_runs": len(latest.keys() - db_ids),
            "note": "Source totals include all records; histograms are admitted cloud runs only. "
            "The JSONL is append-only and imported with a lag: quote its digest with counts.",
            "jsonl": _facet_counts(jsonl), "db": _facet_counts(db),
            "omitted": 0, "truncated": False,
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise QaDataError("the facet ledger cannot be recounted") from exc
    _reserve(ctx, payload)
    return payload
