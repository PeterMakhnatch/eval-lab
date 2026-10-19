"""Publish paired internal QA inputs using the external trace builder's projection tools.

Frozen labels/custody and small receipts are committed; actor inputs stay outside
Git. This command makes no model calls and never executes submitted code.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from external_traces import (  # noqa: E402
    REPO,
    actions_only,
    baseline,
    blind_atif,
    canonical,
    digest,
    file_digest,
    fired_rules,
    leakage,
    opaque,
    results_root,
    write_json,
)

from evallab.cheatbench_fidelity import blind_atif_step_origins  # noqa: E402
from evallab.qa.answer_key import load_answer_key  # noqa: E402
from evallab.qa.trace import tj_atif  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

STEP_FIELDS = frozenset(
    {
        "step_id",
        "source",
        "message",
        "tool_calls",
        "observation",
        "timestamp",
        "reasoning",
        "reasoning_content",
        "analysis",
        "thought",
        "thinking",
    }
)


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def resolve(ref: dict[str, str], roots: dict[str, Path]) -> Path:
    return roots[ref["root"]] / ref["path"]


def canonical_item(row: dict[str, Any]) -> dict[str, Any]:
    """The accepted 70-pass population is distinct from the older 33-pair key."""
    if (
        row["population"] != "official"
        or row["label"] not in {"wrong_pass", "real_pass"}
        or row["original_pass"] is not True
    ):
        raise ValueError("not an accepted canonical original pass")
    positive = row["label"] == "wrong_pass"
    key_id = "mimo-v7-canonical-pass70"
    return {
        "schema": "qa.answer_key/v1",
        "key_id": key_id,
        "item_id": row["item_id"],
        "subject": {
            "kind": "trace",
            "task_id": row["task_id"],
            "task_digest": None,
            "trial_id": Path(row["source_trial_dir"]).name,
        },
        "expected_verdict": "fail" if positive else "pass",
        "expected_category": "weak" if positive else None,
        "expected_fault": "eval" if positive else None,
        "label_source": "heldout-v7.pass-labels/v1: independently accepted canonical original v6 passes, regrade-v7.json",
        "source_label": row["label"],
        "split": "test",
        "source_uid": row["source_uid"],
        "arm": row["arm"],
        "attempt_index": row["attempt_index"],
        "excluded_from_primary": row["excluded_from_primary"],
        "population": row["population"],
    }


def project_internal(raw: dict[str, Any], identity: str) -> dict[str, Any]:
    """Full actor-step evidence, not evaluator headers/metrics/source identifiers.

    Keep the ATIF action/observation payloads exactly. Per-step non-actor
    bookkeeping is excluded before blind_atif; explicit reasoning remains in the
    full view. Natural-language reasoning inside code/tool output is not erased.
    """
    doc = {
        "schema_version": "ATIF-v1.6",
        "steps": [
            {k: copy.deepcopy(v) for k, v in step.items() if k in STEP_FIELDS}
            for step in raw["steps"]
        ],
    }
    out = blind_atif(doc, item_id=identity)
    out["session_id"] = identity
    out["agent"] = {"name": "trace-agent", "version": "1"}
    if "tool_definitions" in raw.get("agent", {}):
        out["agent"]["tool_definitions"] = copy.deepcopy(raw["agent"]["tool_definitions"])
    return out


def counterfactual(raw: dict[str, Any]) -> dict[str, Any]:
    changed = copy.deepcopy(raw)
    for field in ("metadata", "extra", "final_metrics", "labels", "verification"):
        changed[field] = {"expected_verdict": "EVALUATOR_ONLY_SENTINEL"}
    changed["expected_verdict"] = "EVALUATOR_ONLY_SENTINEL"
    changed["label"] = "EVALUATOR_ONLY_SENTINEL"
    for step in changed["steps"]:
        step["extra"] = {"label": "EVALUATOR_ONLY_SENTINEL"}
        step["metrics"] = {"reward": "EVALUATOR_ONLY_SENTINEL"}
    return changed


def validate_population(
    config: dict[str, Any], rows: list[dict[str, Any]], roots: dict[str, Path]
) -> None:
    actual: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        actual[row["dataset"]].append(row["item"])
    if set(actual) != {s["dataset"] for s in config["sources"]}:
        raise ValueError("custody datasets disagree with the frozen population")
    for source in config["sources"]:
        path = resolve(source["labels"], roots)
        if file_digest(path) != source["labels_sha256"]:
            raise ValueError(f"{path}: label source SHA256 mismatch")
        if source["kind"] == "canonical-mimo":
            doc = json.loads(path.read_bytes())
            if not all(
                doc.get(k) is True
                for k in ("canonical_scoring_allowed", "citable", "scoring_allowed")
            ):
                raise ValueError("canonical MiMo release is not admitted for scoring")
            if (
                doc.get("measurement_mode") != "canonical"
                or doc.get("release_status") != "independently_accepted"
            ):
                raise ValueError("canonical MiMo release is not independently accepted")
            expected = [canonical_item(r) for r in doc["labels"]]
        else:
            expected = [r for r in jsonl(path) if r["split"] == "test"]
        by_id = {r["item_id"]: r for r in expected}
        selected = actual[source["dataset"]]
        if any(r["expected_verdict"] not in {"pass", "fail"} for r in selected):
            raise ValueError("non-binary gold must not enter the false-alarm denominator")
        if len(by_id) != len(expected) or len(selected) != source["n_items"]:
            raise ValueError("duplicate labels or incomplete frozen population")
        if {r["item_id"]: r for r in selected} != by_id or len(selected) != len(by_id):
            raise ValueError("custody changed the selected labels or item identities")
        if dict(Counter(r["expected_verdict"] for r in selected)) != source["verdict_counts"]:
            raise ValueError("label balance differs from the frozen population")
        if source.get("pairs"):
            pairs = resolve(source["pairs"], roots)
            if file_digest(pairs) != source["pairs_sha256"]:
                raise ValueError("pair source SHA256 mismatch")
        selected_rows = [row for row in rows if row["dataset"] == source["dataset"]]
        available = sum(row["input"] is not None for row in selected_rows)
        if available != source.get("n_available", source["n_items"]):
            raise ValueError("availability differs from the frozen coverage decision")
        if available < len(selected_rows) and not source.get("allow_unavailable", False):
            raise ValueError("unavailable actor inputs require an explicit coverage decision")
        if any(row["input"] is None and not row.get("unavailable") for row in selected_rows):
            raise ValueError("unavailable actor input lacks its custody explanation")


def build(metadata: Path, artifact_root: Path, roots: dict[str, Path]) -> dict[str, Any]:
    config = json.loads((metadata / "source.json").read_text())
    inputs_path = metadata / "inputs.jsonl"
    if file_digest(inputs_path) != config["inputs_sha256"]:
        raise ValueError("input custody SHA256 mismatch")
    rows = jsonl(inputs_path)
    validate_population(config, rows, roots)
    source_by_id = {s["dataset"]: s for s in config["sources"]}
    verified: dict[Path, str] = {}
    upstream: dict[Path, dict[str, dict[str, Any]]] = {}
    indexes, predictions, coverage = [], [], []
    manifests: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    action_counts: Counter[str] = Counter()
    for row in sorted(rows, key=lambda r: (r["dataset"], r["item"]["item_id"])):
        dataset, item, ref = row["dataset"], row["item"], row["input"]
        coverage.append(
            {
                "dataset": dataset,
                "item_id": item["item_id"],
                "expected_verdict": item["expected_verdict"],
                "subject": item["subject"],
                "status": "available" if ref is not None else "unavailable",
                "source_input": ref,
                "unavailable": row.get("unavailable"),
            }
        )
        if ref is None:
            continue
        source_path = resolve(ref, roots)
        if source_path not in verified:
            verified[source_path] = file_digest(source_path)
        if verified[source_path] != ref["sha256"]:
            raise ValueError(f"{source_path}: agent source SHA256 mismatch")
        identity = opaque(f"internal:{dataset}", item["item_id"])
        if ref["format"] == "trajectory-judge":
            if source_path not in upstream:
                upstream[source_path] = {r["trajectory_id"]: r for r in jsonl(source_path)}
            source_row = upstream[source_path][ref["trajectory_id"]]
            raw = tj_atif(source_row)
            changed = counterfactual(source_row)
            changed["trajectory_id"] = changed["instance_id"] = "EVALUATOR_ONLY_SENTINEL"
            if project_internal(raw, identity) != project_internal(tj_atif(changed), identity):
                raise ValueError("Trajectory Judge adapter retained annotations or semantic IDs")
        elif ref["format"] == "atif":
            raw = json.loads(source_path.read_bytes())
        else:
            raise ValueError(f"unsupported source format: {ref['format']}")
        full = project_internal(raw, identity)
        if full != project_internal(counterfactual(raw), identity):
            raise ValueError("actor bytes depend on evaluator metadata")
        actions = actions_only(full)
        for before, after in zip(full["steps"], actions["steps"], strict=True):
            if any(before.get(k) != after.get(k) for k in ("tool_calls", "observation")):
                raise ValueError("actions projection changed an action or observation")
            if after["source"] == "agent" and after["message"] != "":
                raise ValueError("actions projection retained agent prose")
        collection = opaque("internal-collection", dataset)
        refs, hashes = {}, {}
        for view, doc in (("full", full), ("actions", actions)):
            relative = f"{collection}/{view}/{identity}/agent/trajectory.json"
            if hits := leakage(doc, relative, {}):
                raise ValueError(f"{dataset}/{identity}/{view}: evaluator leak: {hits}")
            target = artifact_root / relative
            payload = canonical(doc)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != payload:
                raise ValueError(f"{target}: refusing to replace a frozen actor input")
            if not target.exists():
                target.write_bytes(payload)
            refs[view] = {
                "kind": "published",
                "path": str(target.parent.parent.relative_to(roots["results"])),
            }
            hashes[view] = digest(payload)
        action_counts[dataset] += sum(len(s.get("tool_calls") or []) for s in full["steps"])
        index = {
            "dataset": dataset,
            "source_item_id": item["item_id"],
            "opaque_id": identity,
            "full_ref": refs["full"],
            "actions_only_ref": refs["actions"],
            "trajectory_sha256": hashes,
            "source_sha256": ref["sha256"],
            "source_step_ids": blind_atif_step_origins(raw),
            "source_origin": ref,
            "expected_verdict": item["expected_verdict"],
        }
        indexes.append(index)
        predictions.append(
            {
                "dataset": dataset,
                "item_id": item["item_id"],
                "split": "test",
                "positive": item["expected_verdict"] == "fail",
                "full": fired_rules(full),
                "actions": fired_rules(actions),
            }
        )
        for view in ("full", "actions"):
            published_item = copy.deepcopy(item)
            if "trial_ref" in published_item:
                published_item["source_trial_ref"] = published_item.pop("trial_ref")
            published_item.update(
                trial_ref=refs[view],
                full_ref=refs["full"],
                actions_only_ref=refs["actions"],
                trajectory_sha256=hashes,
            )
            manifests[(dataset, view)].append(published_item)
    keys = {}
    for (dataset, view), items in sorted(manifests.items()):
        directory = artifact_root / "keys" / view / dataset
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "items.jsonl").write_bytes(b"".join(canonical(i) for i in items))
        (directory / "README.md").write_text(
            f"# {dataset}: {view} actor view\n\n"
            "Paired actor inputs from the immutable internal-trace custody manifest.\n"
            "Labels are driver-only; no evaluator files live in actor trial directories.\n"
            "The original source key/regrade remains authoritative; no labels were re-adjudicated.\n"
            "Actions remove agent prose/explicit reasoning, not code comments or tool observations.\n"
        )
        source = source_by_id[dataset]
        if source.get("pairs"):
            ids = {i["item_id"] for i in items}
            pairs = [
                p
                for p in jsonl(resolve(source["pairs"], roots))
                if p["parent"] in ids and p["child"] in ids
            ]
            (directory / "pairs.jsonl").write_bytes(b"".join(canonical(p) for p in pairs))
        loaded = load_answer_key(directory)
        if len(loaded.items) != len(items):
            raise ValueError("published key loader lost items")
        keys.setdefault(dataset, {})[view] = {
            "path": str(directory),
            "n_items": len(items),
            "n_pairs": len(loaded.pairs),
            "items_sha256": file_digest(directory / "items.jsonl"),
        }
    baselines = {}
    for dataset in sorted(source_by_id):
        selected = [p for p in predictions if p["dataset"] == dataset]
        result = baseline(selected)
        available = len(selected)
        total = source_by_id[dataset]["n_items"]
        result["coverage"] = {
            "source_items": total,
            "measured_items": available,
            "unavailable_items": total - available,
        }
        result["interval_caveat"] = (
            "Wilson intervals are descriptive, not cluster-adjusted. "
            "This is the frozen actor-available subset; unavailable items are neither benign nor misses."
            if available != total
            else "Wilson intervals are descriptive, not cluster-adjusted; this is a census of the frozen selected key population."
        )
        baselines[dataset] = result
    write_json(metadata / "baseline.json", baselines)
    (metadata / "views.jsonl").write_bytes(b"".join(canonical(i) for i in indexes))
    (metadata / "predictions.jsonl").write_bytes(b"".join(canonical(i) for i in predictions))
    (metadata / "coverage.jsonl").write_bytes(b"".join(canonical(i) for i in coverage))
    measured = len(indexes)
    audit = {
        "schema": "evallab.internal_trace_blinding/v1",
        "items": measured,
        "source_items": len(rows),
        "unavailable_items": len(rows) - measured,
        "trajectories_scanned": 2 * measured,
        "label_leaks": 0,
        "label_mutation_checks": measured,
        "label_mutation_failures": 0,
        "actions_view_fidelity_checks": measured,
        "actions_view_fidelity_failures": 0,
        "retained_actions": dict(action_counts),
        "inputs_sha256": file_digest(inputs_path),
        "views_sha256": file_digest(metadata / "views.jsonl"),
        "scope": "Evaluator-label leakage; genuine action/observation evidence is not lexically censored.",
    }
    write_json(metadata / "leak-audit.json", audit)
    receipt = {
        "schema": "evallab.internal_trace_views/v1",
        "keys": keys,
        "artifact_root": str(artifact_root),
        "audit": audit,
        "llm_calls": 0,
        "model_cost_usd": 0.0,
    }
    write_json(metadata / "manifest.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-root", type=Path, default=REPO / "research/answer-keys/internal-views"
    )
    parser.add_argument(
        "--artifact-root", type=Path, default=results_root() / "2026-10-19/internal-traces"
    )
    parser.add_argument(
        "--research-context-root", type=Path, default=Path.home() / "Developer/research-context"
    )
    args = parser.parse_args()
    roots = {
        "repo": REPO,
        "results": results_root(),
        "checkout": shared_checkout_root(REPO),
        "context": args.research_context_root,
    }
    print(
        json.dumps(build(args.metadata_root, args.artifact_root.resolve(), roots), sort_keys=True)
    )


if __name__ == "__main__":
    main()
