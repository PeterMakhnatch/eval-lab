#!/usr/bin/env python3
"""Rebuild the TB4 audit key offline from its frozen, reviewed source metadata.

Raw PM prose, port code, original task packages and actor views stay outside Git.
``source.json`` retains factual metadata, short titles, source identities, every
historical auditor row, and the reviewed class decisions; it is scorer-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any

from views import KEY_ID, SPLIT_SEED, materialize_agent_view, task_splits

from evallab.qa.models import AnswerKeyItem

HERE = Path(__file__).resolve().parent
BENCHMARK_CONTEXT_ID = "__benchmark__/terminal-bench-4"
CATEGORY = {
    "weak_check": "weak",
    "grader_logic_bug": "broken",
    "answer_reachable": "reachable",
    "grader_tamperable": "tamperable",
    "unstated_requirement": "prompt_misaligned",
    "instruction_grader_contradiction": "prompt_misaligned",
    "overstrict_oracle": "broken",
    "wrong_answer_key": "broken",
    "env_mismatch_or_flaky": "broken",
    "infeasible_as_written": "broken",
    "exploitable_llm_judge": "weak",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def _counts(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


WITNESS_SCHEMA = "tb4.audit.witnesses/v1"
KEY_PATH = "research/answer-keys/tb4-audit-v1"


def load_witnesses(path: Path = HERE / "witnesses.json") -> dict[str, Any] | None:
    """Load the scorer-only witness registry, if one has been registered."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_witnesses(
    registry: dict[str, Any] | None,
    splits: dict[str, str],
    finding_ids: set[str],
    root: Path,
) -> list[dict[str, Any]]:
    """Check dev-only placement, file digests and replay rewards of every witness."""
    if registry is None:
        return []
    if registry.get("schema") != WITNESS_SCHEMA:
        raise ValueError("not a TB4 audit witness registry")
    witnesses = registry["witnesses"]
    if len({w["witness_id"] for w in witnesses}) != len(witnesses):
        raise ValueError("witness ids must be unique")
    if len({w["finding_id"] for w in witnesses}) != len(witnesses):
        raise ValueError("each finding may carry at most one witness")
    for w in witnesses:
        wid = w["witness_id"]
        if w["finding_id"] not in finding_ids:
            raise ValueError(f"witness {wid} names an unknown finding")
        if splits.get(w["task"]) != "dev" or w.get("split") != "dev":
            raise ValueError(f"witness {wid} must target a dev-split task")
        directory = (root / w["solution_dir"]).resolve()
        if not directory.is_relative_to((root / "witnesses").resolve()):
            raise ValueError(f"witness {wid} leaves the witnesses directory")
        if not (directory / "solve.sh").is_file():
            raise ValueError(f"witness {wid} has no solve.sh")
        actual = {
            path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError(f"witness {wid} must not contain symlinks")
        if actual != w["files"]:
            raise ValueError(f"witness {wid} files do not match their registered sha256 digests")
        if w["confirmed"]:
            upstream = w.get("upstream_replay") or {}
            port = w.get("port_replay") or {}
            if upstream.get("reward") != 1.0 or port.get("reward") != 0.0:
                raise ValueError(
                    f"confirmed witness {wid} needs upstream reward 1.0 and port reward 0.0"
                )
    return witnesses


EXPOSURE_PATH = HERE.parent / "splits/exposure.jsonl"
EXPOSURE_STRATA = ("unexposed", "inspected_only", "tuned")


def _split_guard() -> Any:
    spec = importlib.util.spec_from_file_location("_splits_guard", HERE.parent / "splits/guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def historical_exposures(names: list[str], path: Path = EXPOSURE_PATH) -> dict[str, dict[str, Any]]:
    """Exposure ids and stratum per task, from the shared historical exposure log.

    Touched slugs follow ``splits/guard.py``: explicit ``tasks`` plus every item
    selected by ``items``/``items_file``/``where`` in another registered key.
    This key's own rows are excluded so the stratum never depends on itself.
    """
    guard = _split_guard()
    keys = {k: v for k, v in guard.load_keys().items() if k != KEY_ID}
    exposures = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    touched: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for exposure in exposures:
        record = exposure | {
            "items": {k: v for k, v in (exposure.get("items") or {}).items() if k in keys},
            "items_file": {
                k: v for k, v in (exposure.get("items_file") or {}).items() if k in keys
            },
            "where": {k: v for k, v in (exposure.get("where") or {}).items() if k in keys},
        }
        by_key = {k: set(v) for k, v in guard.exposed_items(record, keys).items()}
        slugs = set(exposure.get("tasks") or [])
        for key_id, ids in by_key.items():
            slugs.update(
                guard.task_name(item["subject"]["task_id"])
                for item in keys.get(key_id, {"items": []})["items"]
                if item["item_id"] in ids
            )
        for slug in slugs & touched.keys():
            touched[slug].append(exposure)
    out = {}
    for name, matches in touched.items():
        kinds = {e["kind"] for e in matches}
        stratum = (
            "tuned"
            if "tuned" in kinds
            else "inspected_only"
            if "inspected" in kinds
            else "unexposed"
        )
        out[name] = {
            "historical_exposures": sorted({e["exposure_id"] for e in matches}),
            "exposure_stratum": stratum,
        }
    return out


def build_outputs(
    source: dict[str, Any],
    witnesses: dict[str, Any] | None = None,
    *,
    witness_root: Path = HERE,
) -> dict[str, str]:
    """Build all registered labels without reading a task package or using a model.

    ``witnesses`` defaults to the scorer-only ``witnesses.json`` beside this file.
    """
    if witnesses is None:
        witnesses = load_witnesses(witness_root / "witnesses.json")
    if source.get("schema") != "tb4.audit.sources/v1":
        raise ValueError("not a frozen TB4 audit source manifest")
    findings = source["findings"]
    if len(findings) != 241 or len({row["finding_id"] for row in findings}) != 241:
        raise ValueError("tb4-audit-v1 must preserve all 241 unique PM findings")
    tasks = source["tasks"]
    by_name = {row["task"]: row for row in tasks}
    if len(by_name) != len(tasks):
        raise ValueError("the source cohort contains duplicate task identities")
    splits = task_splits(by_name, seed=SPLIT_SEED, dev_count=25)
    exposure = historical_exposures(sorted(by_name))
    taxonomy = source["taxonomy"]
    class_ids = {row["key"]: row["id"] for row in taxonomy["task_side"]}
    if set(class_ids) != set(CATEGORY):
        raise ValueError("the key and pinned task-side taxonomy disagree")
    items = []
    for finding in findings:
        name = finding["task"]
        scope = finding["scope"]
        if scope not in {"task", "benchmark"} or (scope == "benchmark") != (name is None):
            raise ValueError("the PM task and benchmark scopes are inconsistent")
        review = finding["taxonomy_review"]
        cls = review["final_class"]
        if cls not in class_ids or review["evidence_status"] != "suspected_source_claim":
            raise ValueError("class labels must be explicit, known, and not native-confirmed")
        if review["decision"] in {"changed", "uncertain"} and not review.get("parent_decision"):
            raise ValueError(
                "changed or uncertain first-pass labels require a recorded final review"
            )
        if scope == "task":
            task = by_name[name]
            subject = task["subject"]
            package = task["package"]
            split = splits[name]
        else:
            # The legacy answer-key subject schema is task|trace. This explicit
            # context carrier is NOT a native task, has no package or digest,
            # is excluded from actor views/task metrics, and is never held out.
            subject = {"kind": "task", "task_id": BENCHMARK_CONTEXT_ID}
            package = None
            split = "dev"
        tests = finding["port_tests"]
        item = {
            "schema": "qa.answer_key/v1",
            "key_id": KEY_ID,
            "item_id": finding["finding_id"],
            "subject": subject,
            "scope": scope,
            "task": name,
            "role": "finding" if scope == "task" else "benchmark_context",
            "package": package,
            "split": split,
            "title": finding["title"],
            "expected_category": CATEGORY[cls],
            "expected_verdict": "fail",
            "expected_fault": "eval",
            "expected_root_causes": [finding["finding_id"]],
            "root_cause": [
                {
                    "id": finding["finding_id"],
                    "category": CATEGORY[cls],
                    "source_class": cls,
                    "summary": finding["title"],
                }
            ],
            "label_source": source["pm"]["source"]["url"]
            + "@sha256:"
            + source["pm"]["source"]["sha256"],
            "label_strength": "pm_reported",
            "pm_types": finding["pm_types"],
            "taxonomy_class": cls,
            "taxonomy_class_id": class_ids[cls],
            "taxonomy_review": review,
            "port": {
                "status": finding["port_status"],
                "status_label": finding["port_status_label"],
                "revision": source["pm"]["port"]["revision"],
                "mapping_status": finding["port_mapping_status"],
                "tests": tests,
                "mapping_review": finding["port_mapping_review"],
            },
            "sources": finding["attribution"],
            "github_refs": finding["github_refs"],
            "pm_execution": finding["pm_execution"],
            "harbor_trial_urls": finding["harbor_trial_urls"],
            "auditor_evidence": finding["auditor_evidence"],
            "source_url": finding["source_url"],
            "label_binding": "auditor report on the named task; not re-executed on the input digest",
            **(
                exposure[name]
                if scope == "task"
                else {"historical_exposures": [], "exposure_stratum": "unexposed"}
            ),
        }
        AnswerKeyItem.model_validate(item)
        items.append(item)
    items.sort(key=lambda row: row["item_id"])
    registered = _verify_witnesses(
        witnesses, splits, {row["item_id"] for row in items}, witness_root
    )
    by_item = {row["item_id"]: row for row in items}
    for w in registered:
        item = by_item[w["finding_id"]]
        if item["task"] != w["task"]:
            raise ValueError(f"witness {w['witness_id']} names the wrong task")
        item["reproducer"] = {
            "kind": "command",
            "path": f"{KEY_PATH}/{w['solution_dir']}/solve.sh",
            "expected_reward": 1.0,
        }
        item["witness"] = {
            key: w.get(key)
            for key in (
                "witness_id",
                "solution_dir",
                "files",
                "port_test",
                "upstream_replay",
                "port_replay",
                "confirmed",
            )
        }
        if w["confirmed"]:
            item["label_strength"] = "confirmed"
            item["label_binding"] = (
                "witness program replayed: upstream TB4 grader reward 1.0 in a fresh "
                "container and fixed port grader reward 0.0"
            )
        AnswerKeyItem.model_validate(item)
    confirmed = [w for w in registered if w["confirmed"]]
    cohort = []
    for name, task in sorted(by_name.items()):
        # Keep unmatched Epoch/Tokenless flags and PM's no-issue/not-reviewed
        # statements, but do not invent additional PM findings or clean labels.
        cohort.append(
            task
            | {
                "schema": "tb4.audit.task/v1",
                "scope": "task",
                "split": splits[name],
            }
            | exposure[name]
        )
    for item in items:
        if item["scope"] == "task" and item["split"] != splits[item["task"]]:
            raise ValueError("a finding straddles its task's split")
    agreement = source["agreement"]
    for kind in ("counted_source_count", "direct_source_count", "attributed_source_count"):
        if sum(agreement["overall"][kind].values()) != len(items):
            raise ValueError("finding-level source agreement does not sum to the finding cohort")
    if sum(agreement["task_level_overlap"]["source_count"].values()) != len(cohort):
        raise ValueError("task-level source overlap does not sum to the task cohort")
    summary = {
        "schema": "tb4.audit.summary/v1",
        "key_id": KEY_ID,
        "split_seed": SPLIT_SEED,
        "split_method": "ascending sha256(key_id:seed:task), first 25 tasks dev, remainder test",
        "native_tasks": len(cohort),
        "native_task_splits": _counts(row["split"] for row in cohort),
        "pm_findings": len(items),
        "finding_scope": _counts(row["scope"] for row in items),
        "finding_splits": _counts(row["split"] for row in items),
        "task_scoped_finding_splits": _counts(
            row["split"] for row in items if row["scope"] == "task"
        ),
        "taxonomy_classes": _counts(row["taxonomy_class"] for row in items),
        "taxonomy_review_decisions": _counts(row["taxonomy_review"]["decision"] for row in items),
        "pm_type_labels": _counts(cls for row in items for cls in row["pm_types"]),
        "port_status": _counts(row["port"]["status"] for row in items),
        "port_mapping_status": _counts(row["port"]["mapping_status"] for row in items),
        "pm_execution_claims": _counts(row["pm_execution"]["execution_claim"] for row in items),
        "agreement": agreement,
        "source_discrepancies": source["discrepancies"],
        "source_manifest_sha256": hashlib.sha256(_json(source).encode()).hexdigest(),
        "witness_replay": {
            "registered": len(registered),
            "confirmed": len(confirmed),
            "splits": _counts(w["split"] for w in registered),
            "tasks": _counts(w["task"] for w in registered),
        },
        "native_reproductions_performed": len(confirmed),
        "paid_cost_usd": round(
            sum((w.get("upstream_replay") or {}).get("cost_usd", 0) for w in registered), 6
        ),
        "exposure_stratification": {
            "source": "research/answer-keys/splits/exposure.jsonl",
            "rule": "tuned if any matching exposure is tuned, else inspected_only if any is "
            "inspected, else unexposed",
            "tasks": {
                split: {
                    stratum: sum(
                        1
                        for row in cohort
                        if row["split"] == split and row["exposure_stratum"] == stratum
                    )
                    for stratum in EXPOSURE_STRATA
                }
                for split in ("dev", "test")
            },
            "task_findings": {
                split: {
                    stratum: sum(
                        1
                        for row in items
                        if row["scope"] == "task"
                        and row["split"] == split
                        and row["exposure_stratum"] == stratum
                    )
                    for stratum in EXPOSURE_STRATA
                }
                for split in ("dev", "test")
            },
            "exposures_by_task": {
                row["task"]: row["historical_exposures"]
                for row in cohort
                if row["historical_exposures"]
            },
        },
    }
    return {
        "items.jsonl": _jsonl(items),
        "tasks.jsonl": _jsonl(cohort),
        "summary.json": _json(summary),
        "taxonomy.json": _json(taxonomy),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on stale registered outputs")
    parser.add_argument("--agent-view", type=Path, help="new, label-free output directory")
    parser.add_argument("--split", choices=("dev", "test"))
    parser.add_argument(
        "--task-root", type=Path, help="original upstream package root, not the fixed port"
    )
    args = parser.parse_args()
    if args.agent_view is not None and (args.split is None or args.task_root is None):
        parser.error("--agent-view requires --split and --task-root")
    if args.agent_view is not None and args.check:
        parser.error("--check and --agent-view are separate operations")
    source = json.loads((HERE / "source.json").read_text(encoding="utf-8"))
    outputs = build_outputs(source)
    stale = []
    for name, content in outputs.items():
        path = HERE / name
        if args.check or args.agent_view is not None:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print("stale TB4 audit outputs: " + ", ".join(stale))
        return 1
    if args.agent_view is not None:
        tasks = [json.loads(line) for line in outputs["tasks.jsonl"].splitlines()]
        manifest = materialize_agent_view(
            tasks,
            args.agent_view,
            split=args.split,
            task_root=args.task_root,
            taxonomy=source["taxonomy"],
        )
        print(
            json.dumps(
                {
                    "agent_view": str(args.agent_view),
                    "split": args.split,
                    "tasks": manifest["task_count"],
                    "files": len(manifest["files"]),
                }
            )
        )
    else:
        summary = json.loads(outputs["summary.json"])
        print(
            json.dumps(
                {
                    "key_id": KEY_ID,
                    "checked": args.check,
                    "findings": summary["pm_findings"],
                    "native_tasks": summary["native_tasks"],
                    "task_splits": summary["native_task_splits"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
