"""NP23: SB-5 retrieval-leak audit, redone with distinct examples per target.

SB-5 ran one query ("weak_check") for all 44 targets, so its 132 Atlas examples were
3 distinct examples repeated 44 times and could not test same-task rejection. This
audit gives every target its own query (the words of its task name) and adds a second
panel of store-resident DEV tasks that DO have traces, so a retriever that fails to
exclude the target would visibly return it (the positive control).

Panels (44 targets each):
  cohort  the SB-1 cohort tasks, as in SB-5
  store   DEV tasks with an admitted trace in the store, chosen by sha256 order
Modes, each with the target excluded (production use) and without (control):
  L lexical, no Atlas argument      G facets=FILTER      F facets={}
  C cluster + like_run anchor on a level-2 cluster not owned by the target
The rendered text of every example is checked with ``rendered_leaks`` under both
profiles (``retrieval_leak_check``); the blind profile is applied to the tree's own
``seeker_blind_examples.render_blind`` when that module exists.

Read-only; no model calls; counts only, ids are recorded as short hashes.

usage (PYTHONPATH selects the source tree under test):
    uv run --no-sync python audit.py STORE.sqlite OUT.json [LEAK_CHECK.py]
"""

from __future__ import annotations

import hashlib
import importlib.util
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
TOP_K = 8
PANEL = 44
TRAILING = re.compile(r"(?:-v?\d+)+$")


def load_leak_check(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("np23_leak_check", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["np23_leak_check"] = module  # dataclasses and typing resolve the module by name
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def short(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:10]


def family(task_id: str) -> str:
    return TRAILING.sub("", CorpusRegistry.task_name(task_id))


def kinds(leaks: list[str]) -> list[str]:
    """Leak kinds without the leaked values: counts only, never content."""
    return sorted({" ".join(item.split(" ")[:2]) for item in leaks})


def query_for(task_id: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", CorpusRegistry.task_name(task_id)))


def main() -> None:
    store, out = Path(sys.argv[1]), Path(sys.argv[2])
    check = load_leak_check(Path(sys.argv[3]) if len(sys.argv) > 3 else REPO / "src/evallab/production/retrieval_leak_check.py")
    try:
        from evallab.production import seeker_blind_examples as adapter
    except ImportError:
        adapter = None
    registry = CorpusRegistry(REPO)
    db = CloudTraceDatabase(store, read_only=True)
    retriever = CloudTraceRetriever(db, registry=registry)
    cohort = [task["task_id"] for task in json.loads(COHORT.read_text())["tasks"]]
    with db._transaction():
        admitted = set(retriever._admitted_ids()[0])
    traced = sorted(
        {row["task_id"] for row in db._rows("SELECT DISTINCT task_id FROM agent_traces")} & admitted,
        key=lambda task_id: hashlib.sha256(task_id.encode()).hexdigest(),
    )
    cohort_set = set(cohort)
    panels = {
        "cohort": cohort,
        "store": [task_id for task_id in traced if task_id not in cohort_set][:PANEL],
    }
    cluster_rows = db._rows(
        "SELECT cluster_id, run_ids FROM run_clusters WHERE level = 2 ORDER BY size DESC, cluster_id",
    )
    run_task = {row["trace_id"]: row["task_id"] for row in db._rows("SELECT trace_id, task_id FROM agent_traces")}
    started = time.time()
    cells: list[dict[str, Any]] = []

    def anchor_for(target: str) -> tuple[str, str] | None:
        name = CorpusRegistry.task_name(target)
        for row in cluster_rows:
            for run_id in list(row["run_ids"] or ()):
                owner = run_task.get(run_id)
                if owner and owner != target and CorpusRegistry.task_name(owner) != name:
                    return row["cluster_id"], run_id
        return None

    def run_cell(panel: str, mode: str, excluded: bool, target: str, **kwargs: Any) -> None:
        cell: dict[str, Any] = {
            "panel": panel, "mode": mode, "excluded": excluded, "target": short(target),
            "status": "ok", "examples": [], "latency_s": 0.0,
        }
        if excluded:
            kwargs["exclude_task_ids"] = (target,)
        begin = time.time()
        try:
            found = retriever.retrieve_similar_examples(query_for(target), top_k=TOP_K, **kwargs)
            cell["latency_s"] = round(time.time() - begin, 3)
            name, fam = CorpusRegistry.task_name(target), family(target)
            for example in found:
                shared = check.rendered_leaks([example], example.prompt_text, "shared")
                shared_content = check.rendered_leaks([example], example.prompt_text, "shared", include_content=True)
                record = {
                    "id": short(example.trace_id or example.issue_id),
                    "task": short(example.task_id),
                    "same_task": example.task_id == target or CorpusRegistry.task_name(example.task_id) == name,
                    "same_family": family(example.task_id) == fam,
                    "shared_leaks": kinds(shared),
                    "shared_leaks_incl_step_bodies": kinds(shared_content),
                    "blind_leaks_on_shared_block": kinds(check.rendered_leaks([example], example.prompt_text, "blind")),
                }
                if adapter:
                    record["blind_leaks_on_adapter"] = kinds(
                        check.rendered_leaks([example], adapter.render_blind([example]), "blind")
                    )
                cell["examples"].append(record)
        except ValueError as exc:
            cell["status"] = "refused:" + ("named_exclusion" if "exclude_task_ids" in str(exc) else "other")
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            cell["status"] = f"error:{type(exc).__name__}"
        cells.append(cell)

    for panel, targets in panels.items():
        for index, target in enumerate(targets, start=1):
            anchor = anchor_for(target)
            for excluded in (True, False):
                run_cell(panel, "L_lexical", excluded, target)
                run_cell(panel, "G_facets_filter", excluded, target, facets=FILTER)
                run_cell(panel, "F_facets_any", excluded, target, facets={})
                if anchor is None:
                    cells.append({"panel": panel, "mode": "C_cluster_like", "excluded": excluded,
                                  "target": short(target), "status": "not_applicable_no_cluster",
                                  "examples": [], "latency_s": 0.0})
                else:
                    run_cell(panel, "C_cluster_like", excluded, target, facets=FILTER,
                             cluster_id=anchor[0], like_run_id=anchor[1])
            print(f"{panel} {index}/{len(targets)}", flush=True)
    db.close()

    def summarize(selected: list[dict[str, Any]]) -> dict[str, Any]:
        ok = [cell for cell in selected if cell["status"] == "ok"]
        examples = [example for cell in ok for example in cell["examples"]]
        latencies = [cell["latency_s"] for cell in ok if cell["latency_s"]]

        def with_leak(key: str) -> int:
            return sum(1 for example in examples if example.get(key))

        def by_kind(key: str) -> dict[str, int]:
            counts: dict[str, int] = {}
            for example in examples:
                for kind in example.get(key) or []:
                    counts[kind] = counts.get(kind, 0) + 1
            return dict(sorted(counts.items()))

        return {
            "cells": len(selected), "completed": len(ok),
            "refused": sum(1 for cell in selected if cell["status"].startswith("refused")),
            "errors": sum(1 for cell in selected if cell["status"].startswith("error")),
            "not_applicable": sum(1 for cell in selected if cell["status"].startswith("not_applicable")),
            "zero_example_cells": sum(1 for cell in ok if not cell["examples"]),
            "examples": len(examples),
            "distinct_examples": len({example["id"] for example in examples}),
            "distinct_tasks": len({example["task"] for example in examples}),
            "same_task_examples": sum(1 for example in examples if example["same_task"]),
            "same_family_examples": sum(1 for example in examples if example["same_family"]),
            "examples_failing_shared_profile": with_leak("shared_leaks"),
            "shared_profile_failures_by_kind": by_kind("shared_leaks"),
            "examples_hitting_shared_profile_incl_step_bodies": with_leak("shared_leaks_incl_step_bodies"),
            "examples_failing_blind_profile_on_shared_block": with_leak("blind_leaks_on_shared_block"),
            "blind_failures_on_shared_block_by_kind": by_kind("blind_leaks_on_shared_block"),
            "examples_failing_blind_profile_on_adapter_output": with_leak("blind_leaks_on_adapter"),
            "adapter_examples_checked": sum(1 for example in examples if "blind_leaks_on_adapter" in example),
            "blind_failures_on_adapter_by_kind": by_kind("blind_leaks_on_adapter"),
            "latency_median_s": round(statistics.median(latencies), 3) if latencies else None,
        }

    summary: dict[str, Any] = {"all": summarize(cells)}
    for panel in panels:
        for excluded in (True, False):
            for mode in ("L_lexical", "G_facets_filter", "F_facets_any", "C_cluster_like"):
                key = f"{panel}/{mode}/{'excluded' if excluded else 'unexcluded'}"
                summary[key] = summarize(
                    [cell for cell in cells if (cell["panel"], cell["mode"], cell["excluded"]) == (panel, mode, excluded)]
                )
    for excluded in (True, False):
        summary[f"all/{'excluded' if excluded else 'unexcluded'}"] = summarize(
            [cell for cell in cells if cell["excluded"] == excluded]
        )
    result = {
        "schema": "evallab.np23_leak_audit/v1",
        "store": str(store),
        "store_bytes": store.stat().st_size,
        "evallab_module": evallab.__file__,
        "assignments_digest": registry.split_assignments_digest,
        "panels": {name: len(targets) for name, targets in panels.items()},
        "top_k": TOP_K,
        "wall_seconds": round(time.time() - started, 1),
        "model_calls": 0,
        "adapter_present": adapter is not None,
        "summary": summary,
        "cells": cells,
    }
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(json.dumps({key: summary[key] for key in ("all", "all/excluded", "all/unexcluded")}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
