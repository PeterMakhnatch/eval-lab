"""NP22: SB-5 leak audit re-run on a store copy, with the rendered prompt measured.

Same 44 cohort targets, same modes, same FILTER and same label-key regex as the
preregistered SB-5 audit (``raw/sb5-leak-audit-20261028/preregistration.json``).
The audit counted *struct* fields (``provenance`` keys, ``instruction``); this run
also counts what the rendered block (``example.prompt_text``) shows a model,
because the struct is kept for structured consumers and only the renderer is
changed. Read-only; no model calls; counts only, never retrieved content.

usage (from the tree under test, so ``evallab`` imports that tree):
    uv run --no-sync python research/evidence/runs/np22-prompt-lines-20261028/tools/measure.py \
        STORE.sqlite OUT.json
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import evallab
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

REPO = Path(__file__).resolve().parents[5]
COHORT = REPO / "research/experiments/sb1-clean-seeker-set-20261027/cohort.json"
FILTER = {"outcome": "pass", "grader_or_harness_touched": True}
LABEL_KEY = re.compile(r"reward|verdict|label|passage|expected|answer|solution|gold", re.IGNORECASE)
ANSWER_TOKEN = re.compile(r"\b(?:expected|answer|solution|gold|reward|verdict|passage)\b", re.IGNORECASE)
TRAILING = re.compile(r"(?:-v?\d+)+$")
#: The four fields NP22 withholds from the rendered block.
WITHHELD = ("dataset_id", "split", "label_categories", "receipt_status")


def label_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if LABEL_KEY.search(str(key)):
                found.append(path)
            found.extend(label_keys(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(label_keys(item, f"{prefix}[{index}]"))
    return found


def _identifiers(example: Any) -> list[str]:
    values = [example.task_id, example.trace_id, example.issue_id, example.model_id]
    values += [example.provenance.get(key) for key in ("run_id", "trace_id", "issue_id", "task_id", "model_id")]
    return sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True)


def _without_identifiers(example: Any, text: str) -> str:
    """``text`` with every identifier of the example blanked: ids may embed a dataset name."""
    for value in _identifiers(example):
        text = text.replace(value, "\x00")
    return text


def rendered_channels(example: Any, text: str | None = None) -> dict[str, bool]:
    """Which withheld values a model reading the rendered text can see, outside identifiers."""
    text = _without_identifiers(example, example.prompt_text if text is None else text)
    categories = [str(c) for c in example.provenance.get("label_categories") or []]
    status = example.provenance.get("receipt_status")
    return {
        "dataset_id": bool(example.dataset_id) and example.dataset_id in text,
        "split": '"split"' in text or "split dev" in text.lower(),
        "label_categories": '"label_categories"' in text or any(c in text for c in categories),
        "receipt_status": '"receipt_status"' in text or (isinstance(status, str) and status in text),
    }


def dataset_in_identifiers(example: Any) -> bool:
    """The dataset name is part of the task/trace id itself (a namespaced id), a separate channel."""
    return bool(example.dataset_id) and any(example.dataset_id in value for value in _identifiers(example))


def fields_of(example: Any) -> dict[str, Any]:
    channels = rendered_channels(example)
    return {
        "task_id": example.task_id,
        "task_name": CorpusRegistry.task_name(example.task_id),
        "differential": bool(example.differential),
        "reproducer": bool(example.reproducer),
        "instruction_field": bool(example.instruction),
        "provenance_label_keys": bool(label_keys(example.provenance)),
        "struct_withheld_present": sorted(
            key for key in WITHHELD
            if getattr(example, key, None) or example.provenance.get(key)
        ),
        "rendered_channels": sorted(key for key, seen in channels.items() if seen),
        "rendered_task_asks": "- task asks:" in (example.prompt_text or ""),
        "rendered_answer_tokens": bool(ANSWER_TOKEN.search(example.prompt_text or "")),
        "dataset_in_identifiers": dataset_in_identifiers(example),
    }


def summarize(mode: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [cell for cell in cells if cell["status"] == "ok"]
    examples = [example for cell in ok for example in cell["examples"]]
    latencies = [cell["latency_s"] for cell in ok if cell["latency_s"]]

    def count(predicate: Any) -> int:
        return sum(1 for example in examples if predicate(example))

    return {
        "mode": mode,
        "targets": len(cells),
        "completed": len(ok),
        "refused": sum(1 for cell in cells if cell["status"].startswith("refused")),
        "errors": sum(1 for cell in cells if cell["status"].startswith("error")),
        "not_applicable": sum(1 for cell in cells if cell["status"].startswith("not_applicable")),
        "zero_eligible_targets": sum(1 for cell in ok if not cell["examples"]),
        "examples": len(examples),
        "same_task": sum(cell["same_task"] for cell in ok),
        "struct_instruction_field": count(lambda e: e["instruction_field"]),
        "struct_provenance_label_keys": count(lambda e: e["provenance_label_keys"]),
        "struct_differential_or_reproducer": count(lambda e: e["differential"] or e["reproducer"]),
        "rendered_any_withheld_channel": count(lambda e: bool(e["rendered_channels"])),
        "rendered_by_channel": {
            key: count(lambda e, key=key: key in e["rendered_channels"]) for key in WITHHELD
        },
        "rendered_task_asks_line": count(lambda e: e["rendered_task_asks"]),
        "rendered_answer_token_examples": count(lambda e: e["rendered_answer_tokens"]),
        "dataset_name_inside_identifiers": count(lambda e: e["dataset_in_identifiers"]),
        "latency_median_s": round(statistics.median(latencies), 3) if latencies else None,
    }


def main() -> None:
    store, out = Path(sys.argv[1]), Path(sys.argv[2])
    cohort = json.loads(COHORT.read_text())
    targets = [task["task_id"] for task in cohort["tasks"]]
    registry = CorpusRegistry(REPO)
    db = CloudTraceDatabase(store, read_only=True)
    retriever = CloudTraceRetriever(db, registry=registry)
    cluster_rows = db._rows(
        "SELECT cluster_id, run_ids FROM run_clusters WHERE level = 2 ORDER BY size DESC, cluster_id",
    )
    run_task = {row["trace_id"]: row["task_id"] for row in db._rows("SELECT trace_id, task_id FROM agent_traces")}
    started = time.time()
    cells: dict[str, list[dict[str, Any]]] = {}

    def run_cell(mode: str, target: str, **kwargs: Any) -> None:
        cell: dict[str, Any] = {"target": target, "status": "ok", "examples": [], "same_task": 0, "latency_s": 0.0}
        begin = time.time()
        try:
            found = retriever.retrieve_similar_examples("weak_check", **kwargs)
            cell["latency_s"] = round(time.time() - begin, 3)
            name = CorpusRegistry.task_name(target)
            cell["examples"] = [fields_of(example) for example in found]
            cell["same_task"] = sum(
                1 for example in found
                if example.task_id == target or CorpusRegistry.task_name(example.task_id) == name
            )
        except ValueError as exc:
            cell["status"] = "refused:" + ("named_exclusion" if "exclude_task_ids" in str(exc) else "other")
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            cell["status"] = f"error:{type(exc).__name__}"
        cells.setdefault(mode, []).append(cell)

    for index, target in enumerate(targets, start=1):
        name = CorpusRegistry.task_name(target)
        cluster_id = like_run_id = None
        for row in cluster_rows:
            for run_id in list(row["run_ids"] or ()):
                owner = run_task.get(run_id)
                if owner and owner != target and CorpusRegistry.task_name(owner) != name:
                    cluster_id, like_run_id = row["cluster_id"], run_id
                    break
            if cluster_id:
                break
        run_cell("M2_facets_no_exclusion", target, facets=FILTER)
        run_cell("M2s_facets_excluding_target", target, facets=FILTER, exclude_task_ids=(target,))
        if cluster_id is None:
            for mode in ("M3_cluster_no_exclusion", "M3s_cluster_excluding_target"):
                cells.setdefault(mode, []).append(
                    {"target": target, "status": "not_applicable_no_cluster", "examples": [], "same_task": 0, "latency_s": 0.0},
                )
        else:
            anchor = {"facets": FILTER, "cluster_id": cluster_id, "like_run_id": like_run_id}
            run_cell("M3_cluster_no_exclusion", target, **anchor)
            run_cell("M3s_cluster_excluding_target", target, exclude_task_ids=(target,), **anchor)
        print(f"{index}/{len(targets)}", flush=True)
    db.close()

    summary = {mode: summarize(mode, rows) for mode, rows in cells.items()}
    result = {
        "schema": "evallab.np22_leak_measurement/v1",
        "store": str(store),
        "tree_head": _head(),
        "evallab_module": evallab.__file__,
        "assignments_digest": registry.split_assignments_digest,
        "targets": len(targets),
        "wall_seconds": round(time.time() - started, 1),
        "model_calls": 0,
        "summary": summary,
    }
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=1, sort_keys=True))


def _head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout.strip()


if __name__ == "__main__":
    main()
