"""Freeze metadata-only MiMo train/held-out inputs; never execute task payloads.

Run once to create split.json and heldout.json. Once package-pairs.csv arrives,
run --manifest-only. --check is read-only; drift requires a new --output-dir.
--assemble-successor publishes a separate, scoring-closed v7 candidate without
regenerating the historical split or transferring its scoring authorization.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import runpy
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SHARED_ROOT = ROOT.parents[1] if ROOT.parent.name == ".worktrees" else ROOT
SEED = 20261016
HELDOUT_SIZE = 30
NAMESPACE = "mimo-v2.6-rl/"
RESEARCH_PINS = ["format-code-task-000657", "format-code-task-000371", "format-code-task-001686"]
REFERENCE_BROKEN = {
    task_id: "Research reports own-v6-reference failure; exclude active training/evaluation, retain historical training exposure"
    for task_id in ("format-code-task-000838", "format-code-task-001373")
}
RESEARCH_PROVENANCE = {
    "authority": "Research", "relay": "Main", "decision_date": "2026-10-16",
    "directive": "Preserve the frozen 30 and add 000657/000371/001686 without calling them clean; exclude 000838/001373 from active train/heldout but retain all 42 historical training exposures.",
    "authorization": "Explicit pre-seal correction of our unpublished draft; no scoring has occurred.",
    "reported_reference_failures": "Accepted as reported; no task execution or confirming rerun.",
}
PAIR_COLUMNS = (
    "task_id", "original_package_digest", "original_package_path",
    "v6_package_digest", "v6_package_path", "test_repaired_package_digest",
    "test_repaired_package_path", "repair_record_path", "repair_record_sha256",
    "repair_disposition", "training_exclusion",
)
EXPLICIT_EXCLUSIONS = {
    **{f"format-code-task-{n}": "explicit control hold" for n in ("000792", "000803", "000838")},
    "format-code-task-001647": "reported setup-broken",
    **{f"format-code-task-{n}": "completed line-only concern" for n in (
        "000219", "000682", "001258", "001616", "001790", "002609",
    )},
    **{f"format-code-task-{n}": "uncertain completed evidence" for n in (
        "000336", "002135", "002402",
    )},
}
SUCCESSOR_SIZE = 33
SUCCESSOR_MEMBERSHIP_SHA256 = "ce90659538cac139177d1e2af5c643a51f48d5f40af2f2a7efcc03ddbad285da"
SUCCESSOR_ORDER_SHA256 = "48c02dd2b16ae29d7d5d6df11bd5d3f3057fe654afacccf37d089dbf0ddbbb9b"


def canonical_id(value: Any, known: set[str] | None = None) -> str:
    """Only a bare source_id or the exact MiMo namespace is accepted."""
    if not isinstance(value, str):
        raise ValueError(f"task ID must be a string: {value!r}")
    task_id = value.removeprefix(NAMESPACE)
    if not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", task_id):
        raise ValueError(f"unexpected namespace or ambiguous task ID: {value!r}")
    if known is not None and task_id not in known:
        raise ValueError(f"unknown task ID: {task_id}")
    return task_id


def fingerprint(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def source_bytes(path: Path, sources: dict[str, dict[str, Any]]) -> bytes:
    data = path.read_bytes()
    key = str(path.resolve())
    item = {"path": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if key in sources and sources[key] != item:
        raise ValueError(f"input changed while freezing: {key}")
    sources[key] = item
    return data


def csv_rows(path: Path, sources: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(source_bytes(path, sources).decode("utf-8"))))


def rows_by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    result = {}
    for row in rows:
        task_id = canonical_id(row["task_id"])
        if task_id in result:
            raise ValueError(f"duplicate task ID: {task_id}")
        result[task_id] = row | {"task_id": task_id}
    return result


def jsonl_metadata(data: bytes):
    # Do not inspect messages, command/completion payloads, or trajectory files.
    for line in data.splitlines():
        if line.strip():
            row = json.loads(line)
            yield {key: row[key] for key in ("task", "execution_id") if key in row}


def training_paths(training_root: Path) -> list[Path]:
    if not training_root.is_dir():
        raise ValueError(f"training input directory missing: {training_root}")
    datasets = sorted(p for p in training_root.rglob("*.jsonl") if p.name in {"A.jsonl", "B.jsonl", "C.jsonl"})
    if not datasets:
        raise ValueError(f"no actual A/B/C datasets in {training_root}")
    paths = set(datasets)
    for directory in {p.parent for p in datasets}:
        required = {directory / f"{arm}.jsonl" for arm in "ABC"} | {directory / "manifest.json"}
        missing = sorted(str(p) for p in required if not p.is_file())
        if missing:
            raise ValueError(f"incomplete training dataset directory: {missing}")
        paths.update(required)
    return sorted(paths)


def training_metadata(training_root: Path, sources: dict[str, dict[str, Any]]) -> tuple[set[str], list[dict[str, Any]]]:
    task_ids: set[str] = set()
    inventory = []
    for path in training_paths(training_root):
        data = source_bytes(path, sources)
        entry = dict(sources[str(path.resolve())])
        if path.suffix == ".jsonl":
            ids: set[str] = set()
            count = 0
            for row in jsonl_metadata(data):
                ids.add(canonical_id(row.get("task")))
                count += 1
            entry.update(kind="dataset", arm=path.stem, records=count, task_ids=sorted(ids))
        else:
            metadata = json.loads(data)
            if not isinstance(metadata.get("tasks"), list):
                raise ValueError(f"training manifest lacks task metadata: {path}")
            ids = {canonical_id(value) for value in metadata["tasks"]}
            entry.update(kind="dataset_manifest", task_ids=sorted(ids))
            hashes = metadata.get("arm_sha256", {})
            for arm in "ABC":
                arm_path = path.parent / f"{arm}.jsonl"
                source_bytes(arm_path, sources)
                actual = sources[str(arm_path.resolve())]["sha256"]
                if hashes.get(arm) != actual:
                    raise ValueError(f"training manifest/arm hash mismatch: {path.parent / f'{arm}.jsonl'}")
        task_ids.update(ids)
        inventory.append(entry)
    return task_ids, inventory


def select_heldout(candidates: set[str], train: set[str]) -> list[str]:
    eligible = candidates - train
    if len(eligible) < HELDOUT_SIZE:
        raise ValueError(
            f"heldout minimum {HELDOUT_SIZE}: only {len(eligible)} eligible completed "
            f"pass/refuted tasks; gap {HELDOUT_SIZE - len(eligible)}; no unknown fill"
        )
    return sorted(eligible, key=lambda task_id: (rank(task_id), task_id))[:HELDOUT_SIZE]


def rank(task_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{task_id}".encode()).hexdigest()


def validate_split(split: dict[str, Any]) -> None:
    train = split["train"]
    heldout = split["heldout"]
    exposure = split["training_exposure"]
    for values in (train, heldout, exposure):
        if len(values) != len(set(values)) or any(canonical_id(value) != value for value in values):
            raise ValueError("split IDs must be unique, canonical bare source IDs")
    if len(heldout) < HELDOUT_SIZE:
        raise ValueError(f"heldout minimum is {HELDOUT_SIZE}, got {len(heldout)}")
    if not set(train) <= set(exposure) or set(train) & REFERENCE_BROKEN.keys():
        raise ValueError("active train must be within historical exposure and exclude reported reference failures")
    overlap = set(exposure) & set(heldout)
    if overlap:
        raise ValueError(f"training leakage: {sorted(overlap)}")
    blocked = set(heldout) & (EXPLICIT_EXCLUSIONS.keys() | REFERENCE_BROKEN.keys())
    if blocked:
        raise ValueError(f"explicitly excluded heldout tasks: {sorted(blocked)}")


def build_split(config: dict[str, str], *, revision: dict[str, Any] | None = None) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    repo = Path(config["repo_root"])
    package_root = Path(config["package_root"])
    v6 = rows_by_id(csv_rows(Path(config["v6_manifest"]), sources))
    references = rows_by_id(csv_rows(Path(config["reference_index"]), sources))
    observations = list(jsonl_metadata(source_bytes(Path(config["ordinary"]), sources)))
    observed_ids = {canonical_id(row.get("task")) for row in observations}
    dataset_ids, training_inputs = training_metadata(Path(config["training_root"]), sources)
    exposure = observed_ids | dataset_ids | set(revision.get("prior_training_exposure", []) if revision else [])
    train = exposure - REFERENCE_BROKEN.keys()
    known = set(v6)
    # Keep terminal/training-only IDs; require their existing first-party identity record.
    for task_id in sorted(exposure - known):
        records = sorted((repo / "library/task-variants" / f"mimo-v2.6-rl__{task_id}").glob("*.json"))
        if not records:
            raise ValueError(f"unknown task ID (no manifest or identity record): {task_id}")
        record = json.loads(source_bytes(records[0], sources))
        if canonical_id(record.get("task_name")) != task_id:
            raise ValueError(f"ambiguous identity record for {task_id}: {records[0]}")
        known.add(task_id)
    for task_id in exposure:
        canonical_id(task_id, known)

    published = {}
    for line in source_bytes(Path(config["findings"]), sources).splitlines():
        if not line.strip():
            continue
        finding = json.loads(line)
        subject = finding["subject"]
        task_id = canonical_id(subject["task_id"], set(v6))
        if task_id in published:
            raise ValueError(f"ambiguous repeated published finding: {task_id}")
        verdict = finding["verdict"]
        refutation = finding["refutation"]["status"]
        if verdict not in {"pass", "fail", "unknown"} or refutation not in {"verified", "refuted", "unverified"}:
            raise ValueError(f"unknown completed finding disposition: {task_id}")
        published[task_id] = {
            "verdict": verdict, "refutation": refutation,
            "source_task_digest": subject["task_digest"], "stage": finding["stage"],
        }

    reference_pool = set()
    pool_metadata = {}
    exclusions = {task_id: [reason] for task_id, reason in EXPLICIT_EXCLUSIONS.items()}
    for task_id, reason in REFERENCE_BROKEN.items():
        exclusions.setdefault(task_id, []).append(reason)
    for task_id in sorted(exposure):
        exclusions.setdefault(task_id, []).append("ordinary observation or current A/B/C training metadata")
    for task_id, finding in published.items():
        if finding["verdict"] != "pass" and finding["refutation"] != "refuted":
            exclusions.setdefault(task_id, []).append("completed supported or unresolved finding")
    for task_id, row in v6.items():
        ref = references.get(task_id)
        if row["language"] != "python" or row["status"] != "built" or not ref:
            continue
        if ref["label"] != "oracle:pass+nop:fail" or not ref["patch_path"] or not ref["patch_sha256"]:
            continue
        package_path = Path(row["package_path"])
        if not package_path.is_absolute():
            package_path = package_root / package_path
        solve = package_path / "solution/solve.sh"
        if not solve.is_file():
            raise ValueError(f"indexed built Python task lacks packaged reference: {task_id}: {solve}")
        if not source_bytes(solve, sources).strip():
            raise ValueError(f"indexed built Python task has empty packaged reference: {task_id}")
        reference_pool.add(task_id)
        pool_metadata[task_id] = {
            "task_id": task_id, "v6_package_digest": row["final_digest"],
            "v6_package_path": row["package_path"],
            "reference_patch_path": ref["patch_path"], "reference_patch_sha256": ref["patch_sha256"],
            "packaged_solve": dict(sources[str(solve.resolve())]),
        }
        if task_id not in published:
            exclusions.setdefault(task_id, []).append("not measured in completed published findings; not clean")
    positive = {
        task_id for task_id, finding in published.items()
        if finding["verdict"] == "pass" or finding["refutation"] == "refuted"
    }
    eligible = reference_pool & positive - exclusions.keys()
    prior = revision.get("prior_heldout") if revision else None
    base_heldout = list(prior) if prior else select_heldout(eligible, exposure)
    additions = [canonical_id(value, reference_pool) for value in revision.get("added_heldout", [])] if revision else []
    heldout = base_heldout + [task_id for task_id in additions if task_id not in base_heldout]
    if set(heldout) - reference_pool:
        raise ValueError(f"pinned heldout lacks built Python/indexed reference: {sorted(set(heldout) - reference_pool)}")
    selected = []
    for task_id in heldout:
        row = pool_metadata[task_id]
        patch = Path(row["reference_patch_path"])
        source_bytes(patch, sources)
        if sources[str(patch.resolve())]["sha256"] != row["reference_patch_sha256"].removeprefix("sha256:"):
            raise ValueError(f"reference patch digest mismatch: {task_id}")
        selected.append(row | {
            "selection_rank_sha256": rank(task_id), "published_evidence": published.get(task_id),
            "selection_basis": "Research-pinned addition" if task_id in additions else "frozen deterministic base",
            "readiness": "repair/control-pending; not certified clean" if task_id in additions else "source evidence only; exact-digest controls unproven",
        })
    split = {
        "schema": "mimo.training-split/v1", "seed": SEED,
        "train": sorted(train), "heldout": heldout,
        "training_exposure": sorted(exposure), "revision": revision,
        "excluded_from_active_training": [{"task_id": task_id, "reason": reason} for task_id, reason in REFERENCE_BROKEN.items()],
        "historical_training_audit": {
            "status": "required", "arm_A": "already authorized, possibly already trained",
            "action": "Downstream audit/exclusion of reported broken tasks is required; never rewrite historical A or its 42-ID exposure history.",
        },
        "ordinary_training_tasks": sorted(observed_ids),
        "dataset_metadata_training_tasks": sorted(dataset_ids),
        "train_outside_reference_pool": sorted(train - reference_pool),
        "reference_pool": sorted(reference_pool), "heldout_task_metadata": selected,
        "training_inputs": training_inputs,
        "sources": sorted(sources.values(), key=lambda item: item["path"]), "config": config,
        "counts": {
            "ordinary_observations": len(observations),
            "ordinary_executions": len({row["execution_id"] for row in observations}),
            "ordinary_unique_tasks": len(observed_ids), "training_unique_tasks": len(train),
            "training_dataset_files": sum(item["kind"] == "dataset" for item in training_inputs),
            "built_python_v6": sum(row["status"] == "built" and row["language"] == "python" for row in v6.values()),
            "indexed_nonempty_python_references": len(reference_pool),
            "completed_published_findings": len(published), "eligible_completed_pass_or_refuted": len(eligible),
            "heldout": len(heldout), "heldout_training_overlap": 0,
            "training_exposure_unique_tasks": len(exposure),
            "active_training_reference_failure_exclusions": len(exposure & REFERENCE_BROKEN.keys()),
            "frozen_base_heldout": len(base_heldout), "research_pinned_additions": len(set(heldout) - set(base_heldout)),
            "training_outside_reference_pool": len(train - reference_pool),
        },
        "selection_rule": {
            "size": len(heldout), "minimum_size": HELDOUT_SIZE, "base_size": len(base_heldout), "seed": SEED,
            "research_pinned_additions": additions, "research_pin_provenance": revision.get("provenance") if revision else None,
            "rank": "ascending hex SHA256(UTF-8(str(seed) + ':' + bare source_id)); source_id tie-break",
            "eligibility": "built Python v6; index oracle:pass+nop:fail with recovered patch; nonempty packaged solve.sh; completed published verdict=pass OR refutation=refuted",
            "exclusions": "all training metadata/observations, explicit holds, supported/unresolved findings, line-only and uncertain concerns; never pad with unmeasured tasks",
            "eligible": sorted(eligible),
        },
        "exclusions": [{"task_id": task_id, "reasons": reasons} for task_id, reasons in sorted(exclusions.items())],
        "limits": [
            "Metadata-only selection; no new model outcomes, task execution, command replay, or paid calls.",
            "Published evidence is source-digest evidence, not transferred certification of v6 or repaired digests.",
            "Indexed reference labels do not establish current exact-digest native control readiness.",
            "The sweep is paused; only completed published findings are consumed; unmeasured is not pass.",
            "Train preserves all ordinary and current A/B/C metadata IDs, including training-only tasks outside the reference pool.",
            "Current training bytes and directory membership are pinned; additions or drift require an explicit successor, never overwrite.",
            "Repaired-target policy and written QA Trace/Trainer schema agreement remain unresolved; scoring is not allowed.",
            "Research-pinned additions are explicit coverage-repair/control-pending tasks, not newly declared clean outcomes.",
            "Reported own-v6-reference failures are excluded from active train/heldout without a confirming rerun; historical training exposure remains in the denylist.",
        ],
    }
    validate_split(split)
    return split


def encoded(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def heldout_payload(split: dict[str, Any], split_path: Path, release_path: Path | None = None) -> dict[str, Any]:
    return {
        "schema": "mimo.heldout/v1", "heldout": split["heldout"],
        "split": fingerprint(split_path), "scoring_allowed": False,
        "freeze_ready": release_path is not None,
        "validity_check_allowed": release_path is not None,
        "validity_release": fingerprint(release_path) if release_path is not None else None,
    }


def manifest_payload(split: dict[str, Any], output_dir: Path, pairs_path: Path, *, policy_revision: dict[str, Any] | None = None, release_path: Path | None = None) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    data = source_bytes(pairs_path, sources)
    release = json.loads(release_path.read_text(encoding="utf-8")) if release_path is not None else None
    heldout_data = encoded(heldout_payload(split, output_dir / "split.json", release_path)).encode("utf-8")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != PAIR_COLUMNS:
        raise ValueError(f"unexpected package-pairs columns: {reader.fieldnames}")
    pairs = rows_by_id(list(reader))
    known_v6 = rows_by_id(csv_rows(Path(split["config"]["v6_manifest"]), sources))
    known = set(known_v6) | set(split["train"])
    for task_id in pairs:
        canonical_id(task_id, known)
    required = set(split["heldout"])
    if required - pairs.keys():
        raise ValueError(f"package-pairs lacks heldout tasks: {sorted(required - pairs.keys())}")
    pinned_v6 = {row["task_id"]: row["v6_package_digest"] for row in split["heldout_task_metadata"]}
    tasks = []
    for task_id, row in sorted(pairs.items()):
        controls = {}
        for version in ("original", "v6", "test_repaired"):
            digest, path = row[f"{version}_package_digest"], row[f"{version}_package_path"]
            if bool(digest) != bool(path) or (digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
                raise ValueError(f"invalid concrete {version} version: {task_id}")
            if digest:
                controls[version] = {"status": "unproven", "package_digest": digest, "evidence": []}
        if task_id in pinned_v6 and (row["v6_package_digest"] != pinned_v6[task_id] or not row["original_package_digest"]):
            raise ValueError(f"heldout package-pair differs from frozen v6 or lacks original: {task_id}")
        repaired = row["test_repaired_package_digest"]
        if repaired:
            if repaired == row["v6_package_digest"] or not row["repair_record_path"] or not row["repair_record_sha256"]:
                raise ValueError(f"unchanged/unbound target is not a test repair: {task_id}")
            record_path = Path(row["repair_record_path"])
            record = json.loads(source_bytes(record_path, sources))
            if sources[str(record_path.resolve())]["sha256"] != row["repair_record_sha256"].removeprefix("sha256:"):
                raise ValueError(f"repair record hash mismatch: {task_id}")
            if (canonical_id(record["task_name"]) != task_id or record["variant_digest"] != repaired
                    or record["parent"]["digest"] != row["v6_package_digest"]
                    or record["transform"] != "verifier-repair@1"):
                raise ValueError(f"repair is not derived from the exact v6 parent: {task_id}")
        # Research explicitly rejected 001425's partial, unverified repair.
        use_repair = bool(repaired) and task_id != "format-code-task-001425"
        target_column = "test_repaired" if use_repair else "v6"
        if not row["v6_package_digest"]:
            raise ValueError(f"missing actual v6 evaluation package: {task_id}")
        tasks.append(row | {
            "split": "excluded" if task_id in REFERENCE_BROKEN else "heldout" if task_id in split["heldout"] else "train" if task_id in split["train"] else "unselected",
            "exclusion_reason": REFERENCE_BROKEN.get(task_id),
            "source_controls": controls,
            "evaluation_target": {
                "package_digest": row[f"{target_column}_package_digest"],
                "package_path": row[f"{target_column}_package_path"],
                "version_column": target_column,
                "repair_scope": "task-specific-v6-verifier-repair" if use_repair else "global-v6-hardening-only",
                "test_repair_claim": use_repair,
            },
        })
    return {
        "schema": "mimo.training-manifest/v1", "scoring_allowed": False,
        "freeze_ready": release is not None,
        "validity_check_allowed": release is not None,
        "validity_release": fingerprint(release_path) if release_path is not None else None,
        "canonical_id_form": "bare-source-id", "task_family": "mimo-v2.6-rl",
        "selection_rule": split["selection_rule"],
        "split": fingerprint(output_dir / "split.json"),
        "heldout_manifest": {
            "path": str((output_dir / "heldout.json").resolve()),
            "bytes": len(heldout_data), "sha256": hashlib.sha256(heldout_data).hexdigest(),
        },
        "package_pairs": dict(sources[str(pairs_path.resolve())]),
        "sources": sorted(sources.values(), key=lambda item: item["path"]),
        "split_source_bindings": split["sources"],
        "training_inputs": split["training_inputs"],
        "train": split["train"], "heldout": split["heldout"], "tasks": tasks,
        "training_exposure": split["training_exposure"], "revision": split["revision"],
        "excluded_from_active_training": split["excluded_from_active_training"],
        "historical_training_audit": split["historical_training_audit"],
        "training_tasks_without_reference_pairs": sorted(set(split["train"]) - pairs.keys()),
        "counts": split["counts"] | {
            "package_pair_rows": len(tasks),
            "distinct_test_repaired_rows": sum(bool(row["test_repaired_package_digest"]) for row in tasks),
            "heldout_distinct_test_repaired_rows": sum(bool(pairs[task_id]["test_repaired_package_digest"]) for task_id in split["heldout"]),
            "heldout_evaluation_target_v6_rows": sum(row["split"] == "heldout" and row["evaluation_target"]["version_column"] == "v6" for row in tasks),
        },
        "schema_agreement": release["schema_agreement"] if release is not None else {"status": "pending written agreement", "required_from": ["QA Trace", "Trainer"], "evidence": []},
        "control_readiness": {"status": "unproven", "exact_digest_evidence": []},
        "policy_revision": policy_revision,
        "evaluation_target_policy": {
            "status": "resolved", "scoring_allowed": False,
            "rule": "Unflagged completed-evidence heldout tasks use v6@10 hardening. Additional test repair is required only for known defects. 001425 uses plain v6, not its partial unverified repair. RH validity findings require Research repair-or-drop disposition before scoring.",
            "authority": "Research", "relay": "Main", "decision_date": "2026-10-16",
            "repair_scope": "Per task, explicit in evaluation_target; never transfer repair labels across digests.",
        },
        "limits": [limit for limit in split["limits"] if not limit.startswith(("Repaired-target policy and written", "Current training bytes and directory"))] + [
            "Research's current target policy and written schema acknowledgements supersede the earlier ID-freeze snapshot's pending notes.",
            "Frozen training hashes are historical input observations; current A/B/C membership must remain within the entire frozen exposure denylist.",
            "Validity checking is a separate permission from scoring. scoring_allowed remains false until Research dispositions the RH validity gate.",
            "Original means frozen current v5, not a claim about historical trajectory-source digests.",
            "No exact-digest native control evidence was supplied to this manifest; all concrete source controls remain unproven.",
        ],
    }


def validate_release_inputs(split: dict[str, Any], output_dir: Path, release_path: Path) -> None:
    """Check current exclusion coverage without reselection from changing evidence."""
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release["schema"] != "mimo.validity-release/v1":
        raise ValueError("unsupported validity release schema")
    if release["split"] != fingerprint(output_dir / "split.json"):
        raise ValueError("validity release does not bind the frozen split")
    agreement = release["schema_agreement"]
    if agreement["status"] != "accepted" or {row["role"] for row in agreement["evidence"]} != {"QA Trace", "Trainer"}:
        raise ValueError("both written schema acknowledgements are required")
    findings_path = Path(split["config"]["findings"])
    dynamic_sources = {item["path"] for item in split["training_inputs"]} | {str(findings_path.resolve())}
    for item in split["sources"]:
        if item["path"] not in dynamic_sources and fingerprint(Path(item["path"])) != item:
            raise ValueError(f"frozen input drift: {item['path']}")
    training_ids, _ = training_metadata(Path(split["config"]["training_root"]), {})
    if training_ids & set(split["heldout"]) or not training_ids <= set(split["training_exposure"]):
        raise ValueError("current training inventory escapes the frozen exposure denylist")
    published = {
        canonical_id(row["subject"]["task_id"]): row
        for line in findings_path.read_text(encoding="utf-8").splitlines()
        if line.strip() for row in [json.loads(line)]
    }
    for selected in split["heldout_task_metadata"]:
        row = published.get(selected["task_id"])
        evidence = None if row is None else {
            "refutation": (row.get("refutation") or {}).get("status", "unverified"),
            "source_task_digest": row["subject"].get("task_digest"),
            "stage": row.get("stage"), "verdict": row.get("verdict"),
        }
        if evidence != selected["published_evidence"]:
            raise ValueError(f"heldout evidence changed; Research disposition required: {selected['task_id']}")


def payload_fingerprint(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = encoded(payload).encode("utf-8")
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def validate_scoring_authority(actor: Any, approval: Any) -> None:
    if actor != "research-harbor" or not isinstance(approval, str) or not approval.strip():
        raise ValueError("scoring release requires research-harbor and an explicit approval")


def scoring_payloads(output_dir: Path, heldout: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    heldout = heldout | {"scoring_allowed": True}
    manifest = manifest | {
        "scoring_allowed": True,
        "heldout_manifest": payload_fingerprint(output_dir / "heldout.json", heldout),
    }
    return heldout, manifest


def check_frozen(output_dir: Path) -> dict[str, Any]:
    split_path = output_dir / "split.json"
    split = json.loads(split_path.read_text(encoding="utf-8"))
    validate_split(split)
    heldout = json.loads((output_dir / "heldout.json").read_text(encoding="utf-8"))
    release_binding = heldout.get("validity_release")
    release_path = Path(release_binding["path"]) if release_binding is not None else None
    if release_path is not None:
        if fingerprint(release_path) != release_binding:
            raise ValueError("validity release receipt drift")
        validate_release_inputs(split, output_dir, release_path)
    else:
        for item in split["sources"]:
            if fingerprint(Path(item["path"])) != item:
                raise ValueError(f"frozen input drift: {item['path']}; create an explicit successor")
        current = {str(path.resolve()) for path in training_paths(Path(split["config"]["training_root"]))}
        frozen = {item["path"] for item in split["training_inputs"]}
        if current != frozen:
            raise ValueError(f"frozen training inventory drift: added={sorted(current - frozen)}, removed={sorted(frozen - current)}; create an explicit successor")
        if build_split(split["config"], revision=split.get("revision")) != split:
            raise ValueError("frozen split metadata differs from deterministic regeneration; create an explicit successor")
    expected_heldout = heldout_payload(split, split_path, release_path)
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    expected_manifest = None
    if manifest is not None:
        pairs_path = Path(manifest["package_pairs"]["path"])
        expected_manifest = manifest_payload(split, output_dir, pairs_path, policy_revision=manifest.get("policy_revision"), release_path=release_path)
    scoring_path = output_dir / "scoring-release.json"
    if scoring_path.exists():
        if expected_manifest is None or release_path is None:
            raise ValueError("scoring release requires the validity-released package manifest")
        scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
        validate_scoring_authority(scoring.get("actor"), scoring.get("approval"))
        if scoring.get("schema") != "mimo.scoring-release/v1":
            raise ValueError("unsupported scoring release schema")
        predecessors = {
            "predecessor_heldout_manifest": payload_fingerprint(output_dir / "heldout.json", expected_heldout),
            "predecessor_package_manifest": payload_fingerprint(manifest_path, expected_manifest),
        }
        if any(scoring.get(key) != value for key, value in predecessors.items()):
            raise ValueError("scoring release predecessor binding drift")
        expected_heldout, expected_manifest = scoring_payloads(output_dir, expected_heldout, expected_manifest)
        released = {
            "released_heldout_manifest": payload_fingerprint(output_dir / "heldout.json", expected_heldout),
            "released_package_manifest": payload_fingerprint(manifest_path, expected_manifest),
        }
        if any(scoring.get(key) != value or fingerprint(Path(value["path"])) != value for key, value in released.items()):
            raise ValueError("scoring release output binding drift")
    if heldout != expected_heldout:
        raise ValueError("frozen heldout manifest drift")
    if manifest is not None and expected_manifest != manifest:
        raise ValueError("frozen package manifest/input drift; create an explicit successor")
    return split


def create_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded(payload))


def replace_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(encoded(payload))
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def release_scoring(output_dir: Path, actor: str | None, approval: str | None) -> dict[str, Any]:
    validate_scoring_authority(actor, approval)
    split = check_frozen(output_dir)
    heldout_path, manifest_path = output_dir / "heldout.json", output_dir / "manifest.json"
    heldout = json.loads(heldout_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(payload.get("scoring_allowed") is not False
           or payload.get("freeze_ready") is not True
           or payload.get("validity_check_allowed") is not True
           for payload in (heldout, manifest)):
        raise ValueError("scoring release requires both closed scoring gates and completed validity release")
    released_heldout, released_manifest = scoring_payloads(output_dir, heldout, manifest)
    create_json(output_dir / "scoring-release.json", {
        "schema": "mimo.scoring-release/v1",
        "actor": actor, "approval": approval, "applied_at": datetime.now(UTC).isoformat(),
        "predecessor_heldout_manifest": fingerprint(heldout_path),
        "predecessor_package_manifest": fingerprint(manifest_path),
        "released_heldout_manifest": payload_fingerprint(heldout_path, released_heldout),
        "released_package_manifest": payload_fingerprint(manifest_path, released_manifest),
        "allowed_changes": ["heldout.scoring_allowed", "manifest.scoring_allowed", "manifest.heldout_manifest"],
    })
    replace_json(manifest_path, released_manifest)
    # Keep the consumer's starting gun last, after its package-manifest binding.
    replace_json(heldout_path, released_heldout)
    return split


def _successor_json(path: Path, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = json.loads(source_bytes(path, sources))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _successor_binding(path: Path, binding: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Check bytes, not historical absolute locations, when using a frozen copy."""
    key = str(path.resolve())
    if key not in sources:
        source_bytes(path, sources)
    actual = sources[key]
    expected_sha256 = binding.get("sha256")
    if (not isinstance(expected_sha256, str) or expected_sha256.removeprefix("sha256:") != actual["sha256"]
            or ("bytes" in binding and binding["bytes"] != actual["bytes"])):
        raise ValueError(f"source hash/digest mismatch: {path}")
    return dict(actual)


def _successor_identity(path: Path, package_digest: str, harbor_digest: str) -> dict[str, str]:
    from evallab.registry import compute_task_digests, harbor_task_digest

    digests = compute_task_digests(path)
    if digests.package != package_digest or harbor_task_digest(path) != harbor_digest:
        raise ValueError(f"materialized package/Harbor digest mismatch: {path}")
    if not digests.verifier:
        raise ValueError(f"missing concrete verifier identity: {path}")
    return {
        "package_path": str(path.resolve()), "package_digest": digests.package,
        "harbor_digest": harbor_digest, "verifier_digest": digests.verifier,
    }


def _successor_lineage(task_id: str, identity: dict[str, Any], record_path: str, record_sha256: str,
                       repo_root: Path, sources: dict[str, dict[str, Any]], *,
                       records_root: Path | None = None) -> dict[str, Any]:
    from evallab.task_variants import VariantRecord

    records = []
    digest, harbor = identity["package_digest"], identity["harbor_digest"]
    seen: set[str] = set()
    while True:
        path = (repo_root / record_path).resolve()
        storage = records_root if not records and records_root is not None else repo_root / "library/task-variants"
        canonical = storage / f"mimo-v2.6-rl__{task_id}" / f"{digest.removeprefix('sha256:')[:12]}.json"
        if path != canonical.resolve() or digest in seen:
            raise ValueError(f"noncanonical or cyclic grader lineage: {task_id}")
        seen.add(digest)
        record = VariantRecord.model_validate(_successor_json(path, sources))
        binding = dict(sources[str(path)])
        if not records:
            _successor_binding(path, {"sha256": record_sha256}, sources)
        if (record.task_name != NAMESPACE + task_id or record.variant_digest != digest
                or record.variant_harbor_digest != harbor):
            raise ValueError(f"grader lineage task/package/Harbor digest mismatch: {task_id}")
        records.append({
            "record_path": str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path),
            "record": binding,
            "package_digest": digest, "harbor_digest": harbor,
            "transform": record.transform, "components_changed": record.components_changed,
            "parent": record.parent.model_dump(mode="json"),
        })
        if record.parent.source.kind != "variant":
            break
        record_path = record.parent.source.record
        digest, harbor = record.parent.digest, record.parent.harbor_digest
    return {
        "record_path": records[0]["record_path"], "record_sha256": records[0]["record"]["sha256"],
        "records_newest_first": records,
        "full_transform_chain_newest_first": [record["transform"] for record in records],
        "terminal_parent": records[-1]["parent"],
        "scope": "Canonical record custody and endpoint digests; not a replay or semantic/native qualification.",
    }


def _successor_repeat_proof(native: dict[str, Any], sources: dict[str, dict[str, Any]]) -> bool:
    if len(native["rewards"]) == 1:
        return True
    path = Path(native["result_path"]).parent / "verifier/stability.json"
    if not path.is_file():
        return False
    stability = _successor_json(path, sources)
    runs = stability.get("runs")
    return (
        stability.get("schema") == "evallab.verifier_stability/v1"
        and stability.get("repeat_n_requested") == 3 and stability.get("n_runs") == 3
        and isinstance(runs, list) and len(runs) == 3 and all(isinstance(run, dict) for run in runs)
        and [run.get("index") for run in runs] == [0, 1, 2]
        and stability.get("first_reward") == native["rewards"][0]
    )


def _successor_setup_controls(selected: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if selected.get("setup_only") is not True:
        return None
    handoff = selected.get("selection_handoff")
    if handoff is None:
        return None
    declared = handoff["receipt"]["controls"]
    if not declared.get("evidence_path"):
        return {"status": "pending", "source": None, "receipt": None}
    path = Path(handoff["source"]["path"]).parent / declared["evidence_path"]
    if not path.is_file():
        return {"status": "pending", "source": None, "receipt": None, "recorded_path": str(path)}
    receipt = _successor_json(path, sources)
    if "evidence_sha256" in declared:
        _successor_binding(path, {"sha256": declared["evidence_sha256"]}, sources)
    if (receipt.get("schema") != "mimo-v7-native-controls/v1"
            or canonical_id(receipt["task_id"]) != "format-code-task-001948"
            or receipt["package_digest"] != selected["package_digest"]
            or receipt["harbor_digest"] != selected["harbor_digest"]
            or receipt.get("scoring_allowed") is not False):
        raise ValueError("setup native controls task/package/Harbor digest mismatch")
    native_reader = runpy.run_path(str(ROOT / "research/experiments/mimo-v6-full-fleet/census.py"))["native_observations"]
    actual_native = {
        observation["execution_id"]: observation
        for observation in native_reader({Path(item["metadata_path"]) for item in receipt["observations"]})
    }
    rewards: dict[str, list[float]] = {"oracle": [], "nop": []}
    seen = set()
    observations = []
    for observation in receipt["observations"]:
        binding = observation["task_binding"]
        agent, execution_id = observation["agent"], observation["execution_id"]
        if (agent not in rewards or execution_id in seen
                or canonical_id(observation["task_id"]) != "format-code-task-001948"
                or observation["source_package_digest"] != selected["package_digest"]
                or binding["source_package_digest"] != selected["package_digest"]
                or any(observation[key] != selected["harbor_digest"] for key in (
                    "source_harbor_digest", "staged_harbor_digest", "executed_harbor_digest"))
                or binding["task_digest"] != selected["harbor_digest"]):
            raise ValueError("setup native observation has duplicate execution or mismatched selected identity")
        seen.add(execution_id)
        raw_sources = []
        for owner, name in ((observation, "metadata"), (observation, "result"), (binding, "config"), (binding, "lock")):
            raw = Path(owner[f"{name}_path"])
            raw_sources.append({
                "kind": name,
                "status": "available_hash_verified" if raw.is_file() else "raw_evidence_unavailable",
                "source": _successor_binding(raw, {"sha256": owner[f"{name}_sha256"]}, sources) if raw.is_file() else {
                    "path": str(raw), "sha256": owner[f"{name}_sha256"],
                },
            })
        native = actual_native.get(execution_id)
        raw_available = all(item["status"] == "available_hash_verified" for item in raw_sources)
        if raw_available and (native is None or native["task_binding"] != binding or any(
                native[key] != observation[key] for key in (
                    "agent", "metadata_path", "metadata_sha256", "result_path", "result_sha256", "rewards",
                    "source_package_digest", "source_harbor_digest", "staged_harbor_digest", "executed_harbor_digest",
                ))):
            raise ValueError("setup native observation does not match its actual physical execution identity or score")
        case_sources = []
        if native is not None:
            for raw in sorted((Path(native["result_path"]).parent / "verifier").rglob("*")):
                if raw.is_file():
                    source_bytes(raw, sources)
                    case_sources.append(dict(sources[str(raw.resolve())]))
        values = observation["rewards"]
        measured = (
            native is not None and native["scored"] is True
            and _successor_repeat_proof(native, sources)
            and type(observation["returncode"]) is int and observation["returncode"] == 0
            and observation["timed_out"] is False
            and observation["process_ok"] is True and observation["process_failed"] is False
            and observation["exception_present"] is False and observation["scored"] is True
            and observation["native_identity_verified"] is True and binding["verified"] is True
            and binding["error"] is None and bool(values)
            and all(type(value) in (int, float) and math.isfinite(value) for value in values)
            and all(
                len(observation[key]) == len(values) and all(value is flag for value in observation[key])
                for key, flag in (("repeat_scored", True), ("test_execution", True), ("run_error_present", False))
            )
            and observation["run_errors"] == [None] * len(values)
            and raw_available
        )
        observations.append({
            "execution_id": execution_id, "measured": measured,
            "native_scored": native is not None and native["scored"] is True,
            "raw_sources": raw_sources, "case_sources": case_sources,
        })
        if measured:
            rewards[agent].extend(values)
    return {
        "status": "reported_mechanical_evidence", "source": dict(sources[str(path.resolve())]),
        "handoff_hash_bound": "evidence_sha256" in declared, "receipt": receipt,
        "observations": observations, "measured_rewards": rewards,
        "wrong_and_valid_alternate_gate": receipt["wrong_and_valid_alternate_gate"],
        "scope": "Mechanical native controls only; not an independent Verifier PASS or wrong/correct qualification.",
    }


def _successor_baseline_controls(
    metadata_paths: tuple[Path, ...], sources: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Read complete native repeat3 pairs without rewriting frozen x1 evidence."""
    if not metadata_paths:
        return {}
    from evallab.runner import load_job, matrix_run_outcome
    from evallab.schemas import MatrixRun

    paths = {path.resolve() for path in metadata_paths}
    if len(paths) != len(metadata_paths) or any(path.name != "lab-metadata.json" for path in paths):
        raise ValueError("baseline controls require distinct native lab-metadata.json paths")
    for path in sorted(paths):
        _successor_json(path, sources)
    native_reader = runpy.run_path(str(ROOT / "research/experiments/mimo-v6-full-fleet/census.py"))["native_observations"]
    observations = native_reader(paths)
    if len(observations) != len(paths):
        raise ValueError("baseline controls require one distinct physical execution per metadata file")
    controls: dict[str, dict[str, Any]] = {}
    for native in observations:
        task_id, agent = canonical_id(native["task_id"]), native["agent"]
        expected = 1.0 if agent == "oracle" else 0.0
        job = Path(native["metadata_path"]).parent
        loaded = load_job(job)
        outcome = matrix_run_outcome(loaded, MatrixRun(
            name=f"baseline-{agent}", agent=agent, attempts=1, expect_reward=expected,
        ))
        if (native["scored"] is not True or native["rewards"] != [expected] * 3
                or not _successor_repeat_proof(native, sources)
                or outcome["status"] != "ok"
                or any((trial.result.get("config", {}).get("agent") or {}).get("model_name") is not None
                       for trial in loaded.trials)):
            raise ValueError(f"baseline control has absent, errored or conflicting native outcomes: {task_id} {agent}")
        control = controls.setdefault(task_id, {
            "status": "native_custody_verified_not_semantically_certified",
            "observations": {}, "measured_rewards": {},
            "scope": "Exact unchanged-v6 reference/nop repeat3 only; not wrong/correct qualification or an independent Verifier PASS.",
        })
        if agent in control["observations"]:
            raise ValueError(f"baseline controls contain multiple physical executions for one role: {task_id} {agent}")
        binding = native["task_binding"]
        raw_paths = {
            Path(native["metadata_path"]), Path(native["result_path"]),
            Path(binding["config_path"]), Path(binding["lock_path"]),
        }
        for owner, name in ((native, "metadata"), (native, "result"), (binding, "config"), (binding, "lock")):
            _successor_binding(Path(owner[f"{name}_path"]), {"sha256": owner[f"{name}_sha256"]}, sources)
        raw_paths.update(path for name in ("result.json", "config.json", "lock.json")
                         if (path := job / name).is_file())
        trial = Path(native["result_path"]).parent
        raw_paths.update(path for directory in ("agent", "verifier")
                         for path in (trial / directory).rglob("*") if path.is_file())
        for path in sorted(raw_paths):
            source_bytes(path, sources)
        control["observations"][agent] = {
            "native": native, "matrix_outcome": outcome,
            "environment": loaded.trials[0].result.get("config", {}).get("environment"),
            "raw_sources": [dict(sources[str(path.resolve())]) for path in sorted(raw_paths)],
        }
        control["measured_rewards"][agent] = native["rewards"]
    if any(set(control["observations"]) != {"oracle", "nop"} for control in controls.values()):
        raise ValueError("baseline controls require a complete reference/nop repeat3 pair per task")
    return controls


def _successor_controls(task_id: str, parent: dict[str, Any], selected: dict[str, Any],
                        controls: dict[str, Any], pairs: dict[str, Any], source: dict[str, Any],
                        sources: dict[str, dict[str, Any]],
                        baseline_controls: dict[str, Any] | None = None) -> dict[str, Any]:
    pair = pairs.get(task_id)
    retained = []
    rewards: dict[str, list[float]] = {"oracle": [], "nop": []}
    indexes = []
    if pair is not None:
        if pair["package_digest"] != parent["package_digest"]:
            raise ValueError(f"native controls bind a different frozen package: {task_id}")
        for agent in ("oracle", "nop"):
            index = pair[f"{agent}_observation"]
            if type(index) is not int or not 0 <= index < len(controls["observations"]):
                raise ValueError(f"invalid native observation index: {task_id}")
            observation = controls["observations"][index]
            receipt = observation["receipt"]
            if (canonical_id(receipt["task_id"]) != task_id or receipt["agent"] != agent
                    or Path(receipt["package"]).resolve() != Path(parent["package_path"])
                    or observation["source_package_digest"] != parent["package_digest"]
                    or observation["source_harbor_digest"] != parent["harbor_digest"]
                    or observation["staged_harbor_digest"] != parent["harbor_digest"]):
                raise ValueError(f"native observation task/package/Harbor digest mismatch: {task_id}")
            metadata = observation["native_metadata"]
            registered = parent["registered_endpoint"]["controls"][agent]
            if (registered["observation_index"] != index or registered["reward"] != receipt["reward"]
                    or registered["native_metadata"] != metadata
                    or registered["receipt_source"] != observation["receipt_source"]
                    or registered["receipt_line"] != observation["receipt_line"]):
                raise ValueError(f"registered native-control custody mismatch: {task_id}")
            metadata_path = Path(metadata["path"])
            if metadata_path.is_file():
                custody = {"status": "available_hash_verified", "source": _successor_binding(metadata_path, metadata, sources)}
            else:
                custody = {"status": "raw_metadata_unavailable", "recorded_source": metadata}
            reward = receipt["reward"]
            measured = (
                type(observation["native_exit_code"]) is int and observation["native_exit_code"] == 0
                and observation["timed_out"] is False and receipt["error"] is None
                and type(reward) in (int, float) and math.isfinite(reward)
            )
            retained.append({
                "observation_index": index, "observation": observation,
                "measured": measured, "raw_metadata_custody": custody,
            })
            if selected["package_digest"] == parent["package_digest"]:
                indexes.append(index)
                if measured:
                    rewards[agent].append(reward)
    if baseline_controls is not None:
        if selected["package_digest"] != parent["package_digest"] or any(
            observation["native"]["source_package_digest"] != selected["package_digest"]
            or any(observation["native"][key] != selected["harbor_digest"] for key in (
                "source_harbor_digest", "staged_harbor_digest", "executed_harbor_digest"))
            for observation in baseline_controls["observations"].values()
        ):
            raise ValueError(f"baseline controls do not bind the selected unchanged-v6 package: {task_id}")
        rewards = baseline_controls["measured_rewards"]
        indexes = []
    setup_controls = _successor_setup_controls(selected, sources)
    if setup_controls is not None and "measured_rewards" in setup_controls:
        rewards = setup_controls["measured_rewards"]
    semantic_controls = selected.get("semantic_repair_controls")
    if semantic_controls is not None:
        rewards = semantic_controls["measured_rewards"]
    return {
        "source": source, "backend": controls["backend"],
        "retained_frozen_v6": {
            "status": "retained" if pair is not None else "missing",
            "selected_pair": pair, "observations": retained,
            "scope": controls["qualification_scope"],
        },
        "reported_setup_controls": setup_controls,
        **({"reported_baseline_controls": baseline_controls} if baseline_controls is not None else {}),
        "selected_package": {
            "package_digest": selected["package_digest"], "harbor_digest": selected["harbor_digest"],
            **({"measurement_source": "reported_baseline_controls"} if baseline_controls is not None else {}),
            "observation_indexes": indexes, "measured_reference_rewards": rewards["oracle"],
            "measured_nop_rewards": rewards["nop"], "status": "pending",
            "missing": [
                *([] if rewards["oracle"] == [1, 1, 1] else ["native_reference_1_1_1"]),
                *([] if rewards["nop"] == [0, 0, 0] else ["native_nop_0_0_0"]),
                "adjudicated_wrong_inventory_and_exact_digest_zero_outcomes",
                "admitted_correct_inventory_and_exact_digest_accepted_outcomes",
                "independent_verifier_pass",
            ],
        },
    }


def _successor_repairs(path: Path | None, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if path is None:
        return {"status": "pending", "source": None, "handoff": None, "selections": {},
                "reason": "EnvCheck handoff not supplied"}
    handoff = _successor_json(path, sources)
    if handoff.get("schema") != "envcheck.repairs-v7/v1":
        raise ValueError("unsupported EnvCheck repair handoff schema")
    rows = handoff.get("repairs")
    if (not isinstance(rows, list)
            or type(handoff.get("complete_count")) is not int or handoff["complete_count"] < 0
            or type(handoff.get("required_task_count")) is not int or handoff["required_task_count"] < 0
            or type(handoff.get("recorded_reproducer_count")) is not int or handoff["recorded_reproducer_count"] < 0
            or handoff.get("training_allowed") is not False
            or handoff.get("scoring_allowed", False) is not False):
        raise ValueError("invalid EnvCheck repair handoff counts or closed gates")
    selections = {}
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("task_id"), str)
                or type(row.get("qualified_for_v7")) is not bool
                or row.get("status") not in {"partial", "complete"}):
            raise ValueError("unrecognized nonempty EnvCheck repair handoff row")
        task_id = canonical_id(row["task_id"])
        if task_id in seen:
            raise ValueError(f"duplicate EnvCheck repair task: {task_id}")
        seen.add(task_id)
        if row["qualified_for_v7"]:
            if (row["status"] != "complete" or row.get("reference") != [1, 1, 1]
                    or row.get("do_nothing") != [0, 0, 0]
                    or row.get("task_control_bindings_unchanged") is not True
                    or row.get("no_original_assertion_weakened") is not True
                    or row.get("training_allowed") is not False or row.get("unrepairable") != []):
                raise ValueError(f"incomplete EnvCheck repair advertised as complete: {task_id}")
            selections[task_id] = row
    if len(selections) != handoff["complete_count"]:
        raise ValueError("EnvCheck complete count differs from selected repair rows")
    for item in handoff["source_inputs"]:
        _successor_binding(path.parent / item["path"], item, sources)
    return {
        "status": "pending", "source": dict(sources[str(path.resolve())]), "handoff": handoff,
        "selections": selections,
        "reason": "Bound repair candidates still require exact native custody and independent semantic acceptance",
    }


def _successor_repair_controls(candidate: dict[str, Any], qualification: dict[str, Any],
                               selected: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from evallab.registry import compute_task_digests
    from evallab.runner import load_job, load_matrix, matrix_run_outcome, staged_matrix_request
    from evallab.task_variants import harbor_task_digest

    matrix_path = Path(candidate["matrix"]).resolve()
    _successor_binding(matrix_path, {"sha256": candidate["matrix_sha256"]}, sources)
    matrix = load_matrix(matrix_path)
    producer_root = next((path for path in matrix_path.parents if (path / "pyproject.toml").is_file()), None)
    if producer_root is None:
        raise ValueError("repair matrix producer repository is unavailable")
    if ((producer_root / matrix.task).resolve() != Path(selected["package_path"])
            or matrix.task_package_digest != selected["package_digest"]
            or matrix.verifier_digest != selected["verifier_digest"]
            or qualification["matrix_sha256"] != candidate["matrix_sha256"]
            or qualification["qualified"] is not True):
        raise ValueError("repair qualification matrix/package binding mismatch")
    checks = {check["run"]: check for check in qualification["checks"]}
    if len(checks) != len(qualification["checks"]) or set(checks) != {run.name for run in matrix.runs}:
        raise ValueError("repair qualification has missing or duplicated native controls")
    wrongs = {control["run"]: control for control in candidate["known_wrong_reproducers"]}
    alternates = {control["run"] for control in candidate["adjudicated_correct_controls"]}
    if (len(matrix.runs) < 3 or matrix.runs[0].agent != "oracle" or matrix.runs[1].agent != "nop"
            or {run.name for run in matrix.runs[2:]} != set(wrongs) | alternates or not wrongs):
        raise ValueError("repair matrix does not cover its complete declared wrong/correct inventory")
    native_reader = runpy.run_path(str(ROOT / "research/experiments/mimo-v6-full-fleet/census.py"))["native_observations"]
    observations = []
    seen = set()
    for index, run in enumerate(matrix.runs):
        check = checks[run.name]
        role = "reference" if index == 0 else "nop" if index == 1 else (
            "prompt-valid-alternate" if run.name in alternates else "known-wrong"
        )
        expected = 0.0 if role in {"nop", "known-wrong"} else 1.0
        repeated = role != "known-wrong"
        if (check["status"] != "ok" or check["role"] != role
                or check["matrix_sha256"] != candidate["matrix_sha256"]
                or run.expect_reward != expected or run.attempts != 1
                or run.agent != ("nop" if role == "nop" else "oracle")):
            raise ValueError(f"repair control role, expectation or completion mismatch: {run.name}")
        job = Path(check["job_dir"]).resolve()
        actual = native_reader({job / "lab-metadata.json"})
        if len(actual) != 1 or actual[0]["execution_id"] in seen:
            raise ValueError("repair control lacks a distinct physical native execution")
        native = actual[0]
        seen.add(native["execution_id"])
        with staged_matrix_request(matrix, run, repo_root=producer_root) as (request, provenance):
            identity = compute_task_digests(request.task)
            harbor = harbor_task_digest(request.task)
            if (identity.verifier != selected["verifier_digest"]
                    or any(check.get(key) != value for key, value in provenance.items())
                    or native["source_package_digest"] != identity.package
                    or any(native[key] != harbor for key in (
                        "source_harbor_digest", "staged_harbor_digest", "executed_harbor_digest",
                    ))):
                raise ValueError(f"repair control executed a different package, grader or submission: {run.name}")
        if (native["agent"] != run.agent or native["scored"] is not True
                or native["rewards"] != [expected] * (3 if repeated else 1)
                or (repeated and not _successor_repeat_proof(native, sources))
                or matrix_run_outcome(load_job(job), run)["status"] != "ok"
                or (repeated and check["repeats"] != native["rewards"])):
            raise ValueError(f"repair control has absent, errored or conflicting native outcomes: {run.name}")
        if run.name in wrongs:
            control = wrongs[run.name]
            for key in ("patch", "script"):
                _successor_binding(Path(control[key]), {"sha256": control[f"{key}_sha256"]}, sources)
            if (run.solution is None or (producer_root / run.solution).resolve() != Path(control["script"]).resolve()
                    or check["solution_sha256"].removeprefix("sha256:") != control["script_sha256"].removeprefix("sha256:")):
                raise ValueError("wrong-submission control is not bound to the recorded script")
        paths = [
            Path(native["metadata_path"]), Path(native["result_path"]),
            Path(native["task_binding"]["config_path"]), Path(native["task_binding"]["lock_path"]),
        ]
        trial = Path(native["result_path"]).parent
        paths.extend(path for directory in ("agent", "verifier") for path in (trial / directory).rglob("*") if path.is_file())
        for path in sorted(set(paths)):
            source_bytes(path, sources)
        observations.append({"role": role, "run": run.name, "native": native,
                             "raw_sources": [dict(sources[str(path.resolve())]) for path in sorted(set(paths))]})
    return {
        "status": "native_custody_verified_not_semantically_certified", "observations": observations,
        "measured_rewards": {
            "oracle": observations[0]["native"]["rewards"], "nop": observations[1]["native"]["rewards"],
        },
        "scope": "Exact package/submission, physical execution and case proof; adjudications remain independent.",
    }


def _successor_repair(task_id: str, parent: dict[str, Any], row: dict[str, Any],
                      repo_root: Path, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from evallab.task_variants import VariantRecord, materialize
    from evallab.verifier_repair import build_command_wrapper, read_task_workdir

    qualification_path = Path(row["qualification"]).resolve()
    producer_root = next((path for path in qualification_path.parents if (path / "pyproject.toml").is_file()), None)
    if producer_root is None:
        raise ValueError("repair publication producer repository is unavailable")
    candidate_path = (producer_root / row["candidate"]).resolve()
    _successor_binding(candidate_path, {"sha256": row["candidate_sha256"]}, sources)
    _successor_binding(qualification_path, {"sha256": row["qualification_sha256"]}, sources)
    candidate = _successor_json(candidate_path, sources)
    qualification = _successor_json(qualification_path, sources)
    if (candidate.get("schema") != "envcheck.v7.candidate/v1"
            or canonical_id(candidate["task_id"]) != task_id or canonical_id(row["task_id"]) != task_id
            or candidate["parent_digest"] != parent["package_digest"]
            or row["parent_digest"] != parent["package_digest"]
            or Path(candidate["parent_package"]).resolve() != Path(parent["package_path"])
            or any(candidate[key] != row[key] for key in (
                "package_path", "package_digest", "harbor_digest", "verifier_digest", "native_record", "native_record_sha256",
            ))
            or qualification["package_digest"] != row["package_digest"]
            or qualification["verifier_digest"] != row["verifier_digest"]):
        raise ValueError(f"repair candidate does not bind the exact selected parent and qualification: {task_id}")
    selected = _successor_identity(Path(row["package_path"]), row["package_digest"], row["harbor_digest"])
    if selected["verifier_digest"] != row["verifier_digest"]:
        raise ValueError("repair grader digest differs from the selected package bytes")
    record_path = Path(row["native_record"]).resolve()
    lineage = _successor_lineage(
        task_id, selected, str(record_path), row["native_record_sha256"], repo_root, sources,
        records_root=record_path.parent.parent,
    )
    record = VariantRecord.model_validate(_successor_json(record_path, sources))
    before, after = Path(parent["package_path"]), Path(selected["package_path"])
    if (record.transform != "verifier-repair@1" or set(record.components_changed) != {"verifier"}
            or record.parent.digest != parent["package_digest"] or record.parent.harbor_digest != parent["harbor_digest"]
            or {item.path for item in record.files} != {"tests/test.patch", "tests/test_command.sh"}
            or record.inputs.get("edits") or not isinstance(record.inputs.get("command"), str)):
        raise ValueError("semantic repair is not an append-only verifier transform of the exact parent")
    if len(lineage["records_newest_first"]) == 1 and record.parent.source.kind == "local":
        if Path(record.parent.source.path).resolve() != before:
            raise ValueError("local repair parent is not the exact selected package")
        lineage["records_newest_first"].extend(parent["lineage"]["records_newest_first"])
        lineage["full_transform_chain_newest_first"].extend(parent["lineage"]["full_transform_chain_newest_first"])
        lineage["terminal_parent"] = parent["lineage"]["terminal_parent"]
    elif lineage["records_newest_first"][1:] != parent["lineage"]["records_newest_first"]:
        raise ValueError("semantic repair does not extend the full exact-parent grader lineage")
    original_patch = (before / "tests/test.patch").read_bytes()
    original_command = (before / "tests/test_command.sh").read_text()
    command, workdir = record.inputs["command"].strip(), read_task_workdir(before)
    actual_command = (after / "tests/test_command.sh").read_bytes()
    command_preserved = actual_command == build_command_wrapper(
        original_command, command, workdir=workdir,
    ).encode() or actual_command == build_command_wrapper(
        original_command, command, workdir=workdir, interpreter=None,
    ).encode()
    if (not (after / "tests/test.patch").read_bytes().startswith(original_patch)
            or (after / "tests/test.patch").read_bytes() == original_patch
            or not command_preserved):
        raise ValueError("semantic repair removed original test bytes or bypassed the original test command")
    with tempfile.TemporaryDirectory(prefix="mimo-v7-repair-replay-") as scratch:
        replay = materialize(record, before, repo_root=repo_root, variants_root=Path(scratch))
        if _successor_identity(replay, row["package_digest"], row["harbor_digest"])["verifier_digest"] != selected["verifier_digest"]:
            raise ValueError("independent repair record replay differs from selected bytes")
    source_path = Path(candidate["source_manifest"])
    _successor_binding(source_path, {"sha256": candidate["source_sha256"]}, sources)
    admitted = _successor_json(source_path, sources)
    expected = {item["finding_id"]: item for item in admitted["rows"] if canonical_id(item["task_id"]) == task_id}
    wrongs = candidate["known_wrong_reproducers"]
    if (set(expected) != {item["finding_id"] for item in wrongs}
            or set(expected) != {item["finding_id"] for item in row["reproducers"]}
            or any(item["patch_sha256"] != expected[item["finding_id"]]["reproducer_sha256"] for item in wrongs)
            or row["recorded_reproducer_count"] != len(wrongs)
            or row["reproducers_killed"] != len(wrongs)
            or row["adjudicated_correct_controls"] != candidate["adjudicated_correct_controls"]):
        raise ValueError("repair omits or changes its admitted wrong-submission inventory")
    native = _successor_repair_controls(candidate, qualification, selected, sources)
    return selected | {
        "version_column": "v7", "repair_scope": "append-only-verifier", "test_repair_claim": True,
        "setup_only": False, "lineage": lineage, "semantic_repair_controls": native,
        "selection_handoff": {
            "receipt": row, "candidate": dict(sources[str(candidate_path.resolve())]),
            "qualification": dict(sources[str(qualification_path.resolve())]),
        },
    }


def _successor_setup(path: Path, parent: dict[str, Any], repo_root: Path,
                     sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from evallab.registry import compute_subpath_digest

    handoff = _successor_json(path, sources)
    task_id = "format-code-task-001948"
    if (handoff.get("schema") != "mimo-v7-setup-derivation/v1"
            or canonical_id(handoff["task_id"]) != task_id
            or handoff.get("scoring_allowed") is not False
            or handoff["parent_package_digest"] != parent["package_digest"]
            or Path(handoff["parent_package"]).resolve() != Path(parent["package_path"])
            or handoff["transform"] != "setup-python-path@1"):
        raise ValueError("setup selection is not derived from the exact frozen 001948 parent")
    selected = _successor_identity(Path(handoff["package"]), handoff["package_digest"], handoff["harbor_digest"])
    lineage = _successor_lineage(task_id, selected, handoff["record"], handoff["record_sha256"], repo_root, sources)
    record = _successor_json(repo_root / lineage["record_path"], sources)
    allowed = {"environment/setup/setup.sh", "task.toml"}
    if (selected["package_digest"] == parent["package_digest"]
            or record["transform"] != "setup-python-path@1"
            or set(record["components_changed"]) != {"task_toml", "environment"}
            or {item["path"] for item in record["files"]} != allowed
            or set(handoff["changed_files"]) != allowed
            or handoff.get("tests_byte_identical") is not True
            or handoff["tests_digest"] != selected["verifier_digest"]
            or selected["verifier_digest"] != parent["verifier_digest"]
            or lineage["records_newest_first"][1:] != parent["lineage"]["records_newest_first"]):
        raise ValueError("setup-only selection changed verifier bytes or does not extend the exact frozen lineage")
    before, after = Path(parent["package_path"]), Path(selected["package_path"])
    for relative in ("instruction.md", "solution", "environment/Dockerfile"):
        if compute_subpath_digest(before / relative) != compute_subpath_digest(after / relative):
            raise ValueError(f"setup-only selection changed {relative}")
    for relative, digest in handoff["unchanged_files"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError(f"invalid setup unchanged-file path: {relative}")
        for package in (before, after):
            _successor_binding(package / relative, {"sha256": digest}, sources)
    return selected | {
        "version_column": "setup_only", "repair_scope": "setup-only-python-path",
        "test_repair_claim": False, "setup_only": True, "lineage": lineage,
        "selection_handoff": {"source": dict(sources[str(path.resolve())]), "receipt": handoff},
    }


def successor_payload(frozen_source_dir: Path, *, repairs_handoff: Path | None = None,
                      setup_handoff: Path | None = None, repo_root: Path = ROOT,
                      native_control_metadata: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Assemble the frozen33 candidate; no ranking, execution, or scoring release."""
    frozen_source_dir, repo_root = frozen_source_dir.resolve(), repo_root.resolve()
    sources: dict[str, dict[str, Any]] = {}
    files = {name: frozen_source_dir / name for name in (
        "heldout.json", "split.json", "manifest.json", "native-controls.json", "lineage-registration.json",
    )}
    heldout, split, manifest, controls, registration = (
        _successor_json(path, sources) for path in files.values()
    )
    schemas = ("mimo.heldout/v1", "mimo.training-split/v1", "mimo.training-manifest/v1",
               "mimo.native-controls/v1", "mimo.frozen-lineage-registration/v1")
    if any(payload.get("schema") != schema for payload, schema in zip(
            (heldout, split, manifest, controls, registration), schemas, strict=True)):
        raise ValueError("unsupported frozen successor source schema")
    validate_split(split)
    membership = heldout["heldout"]
    if (len(membership) != SUCCESSOR_SIZE or len(set(membership)) != SUCCESSOR_SIZE
            or any(canonical_id(task_id) != task_id for task_id in membership)
            or hashlib.sha256(("\n".join(sorted(membership)) + "\n").encode()).hexdigest() != SUCCESSOR_MEMBERSHIP_SHA256
            or hashlib.sha256(("\n".join(membership) + "\n").encode()).hexdigest() != SUCCESSOR_ORDER_SHA256
            or membership != split["heldout"] or membership != manifest["heldout"]):
        raise ValueError("successor requires the exact frozen33 membership and order; no swaps or duplicates")
    if manifest["training_exposure"] != split["training_exposure"] or manifest["train"] != split["train"]:
        raise ValueError("frozen historical training exposure differs between sources")
    _successor_binding(files["split.json"], heldout["split"], sources)
    _successor_binding(files["split.json"], manifest["split"], sources)
    _successor_binding(files["heldout.json"], manifest["heldout_manifest"], sources)
    registered_sources = {Path(item["path"]).name: item for item in registration["frozen_sources"]}
    for name in ("heldout.json", "manifest.json", "native-controls.json"):
        _successor_binding(files[name], registered_sources[name], sources)
    pairs_path = frozen_source_dir / "package-pairs.csv"
    pair_data = source_bytes(pairs_path, sources)
    reader = csv.DictReader(io.StringIO(pair_data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != PAIR_COLUMNS:
        raise ValueError("unexpected frozen package-pairs columns")
    pairs = rows_by_id(list(reader))
    _successor_binding(pairs_path, manifest["package_pairs"], sources)
    frozen_rows = rows_by_id(manifest["tasks"])
    metadata = rows_by_id(split["heldout_task_metadata"])
    endpoints = rows_by_id([row for row in registration["endpoints"] if row["role"] == "headline"])
    if (set(metadata) != set(membership) or set(endpoints) != set(membership)
            or {task_id for task_id, row in frozen_rows.items() if row["split"] == "heldout"} != set(membership)):
        raise ValueError("frozen rich task/metadata/lineage membership differs from frozen33")
    control_pairs = rows_by_id(controls["selected_pairs"])
    repairs = _successor_repairs(repairs_handoff, sources)
    if any(canonical_id(row["task_id"]) not in membership for row in (repairs["handoff"] or {}).get("repairs", [])):
        raise ValueError("EnvCheck repair handoff includes a task outside the frozen33")
    baseline_controls = _successor_baseline_controls(native_control_metadata, sources)
    if set(baseline_controls) - set(membership):
        raise ValueError("baseline controls include a task outside the frozen33")
    tasks = []
    package_root = Path(split["config"]["package_root"])
    for task_id in membership:
        row, endpoint = frozen_rows[task_id], endpoints[task_id]
        digest = row["v6_package_digest"]
        package_path = (package_root / row["v6_package_path"]).resolve()
        target = row["evaluation_target"]
        if (any(row[column] != pairs[task_id][column] for column in PAIR_COLUMNS)
                or metadata[task_id]["v6_package_digest"] != digest
                or (package_root / metadata[task_id]["v6_package_path"]).resolve() != package_path
                or endpoint["package_digest"] != digest or Path(endpoint["package_path"]).resolve() != package_path
                or target["version_column"] != "v6" or target["package_digest"] != digest
                or Path(target["package_path"]).resolve() != package_path or target["test_repair_claim"] is not False):
            raise ValueError(f"frozen parent package identity mismatch: {task_id}")
        parent = _successor_identity(package_path, digest, endpoint["harbor_digest"])
        parent["lineage"] = _successor_lineage(task_id, parent, endpoint["record_path"], endpoint["record_sha256"], repo_root, sources)
        if parent["lineage"]["full_transform_chain_newest_first"] != endpoint["full_transform_chain_newest_first"]:
            raise ValueError(f"registered full grader lineage mismatch: {task_id}")
        _successor_binding(files["native-controls.json"], endpoint["control_source"], sources)
        parent["registered_endpoint"] = endpoint
        selected = target | parent
        # Registration's historical controls remain under frozen_parent, not a
        # selected-child qualification claim.
        selected = {key: value for key, value in selected.items() if key != "registered_endpoint"}
        kind = "unchanged-v6"
        if task_id == "format-code-task-001948" and setup_handoff is not None:
            selected = _successor_setup(setup_handoff, parent, repo_root, sources)
            kind = "setup-only"
        if task_id in repairs["selections"]:
            selected = _successor_repair(task_id, selected, repairs["selections"][task_id], repo_root, sources)
            kind = "semantic-repair"
        tasks.append(row | {
            "frozen_parent": parent, "evaluation_target": selected, "successor_selection": kind,
            "native_controls": _successor_controls(
                task_id, parent, selected, controls, control_pairs,
                dict(sources[str(files["native-controls.json"])]), sources, baseline_controls.get(task_id),
            ),
        })
    # Recheck every consumed source before returning a deterministically encoded
    # candidate, so a concurrently updated handoff cannot be silently mixed in.
    for item in list(sources.values()):
        source_bytes(Path(item["path"]), sources)
    return {
        "schema": "mimo.heldout/v1", "successor_version": "v7", "status": "candidate",
        "canonical_id_form": "bare-source-id", "task_family": "mimo-v2.6-rl",
        "heldout": membership, "train": split["train"], "training_exposure": split["training_exposure"],
        "split": dict(sources[str(files["split.json"])]),
        "frozen_sources": {name: dict(sources[str(path)]) for name, path in files.items()},
        "tasks": tasks, "semantic_repairs": repairs,
        "setup_selection": {"status": "selected" if setup_handoff is not None else "pending"},
        "sources": sorted(sources.values(), key=lambda item: item["path"]),
        "scoring_allowed": False, "freeze_ready": False,
        "validity_check_allowed": True, "validity_release": None,
        "independent_verifier": {"status": "pending", "evidence": None},
        "pending_gates": [
            "semantic_repairs", "native_reference_1_1_1_and_nop_0_0_0_on_each_selected_digest",
            "all_adjudicated_wrong_reproducer_outcomes_zero",
            "no_rejected_adjudicated_correct_case", "independent_verifier_pass",
            *([] if setup_handoff is not None else ["setup_001948_selection"]),
        ],
        "counts": {
            "heldout": len(membership), "heldout_training_overlap": 0,
            "training_exposure_unique_tasks": len(split["training_exposure"]),
            "selected_unchanged_v6": sum(row["successor_selection"] == "unchanged-v6" for row in tasks),
            "selected_setup_only": sum(row["successor_selection"] == "setup-only" for row in tasks),
            "selected_semantic_repairs": sum(row["successor_selection"] == "semantic-repair" for row in tasks),
        },
        "limits": [
            "Historical scoring authorization is not inherited. This API cannot open scoring.",
            "Retained exact-v6 x1 controls do not establish v7 repeat3 or qualify a changed package.",
            "Missing/error/null native outcomes are unmeasured, never successful rejection.",
            "Reproducer adjudications and admitted-correct inventory remain independent evidence; no verdict is inferred from a wrong-only export.",
            "Candidate assembly authorizes no execution or spend and makes no v7 acceptance claim.",
            "Historical frozen-v6 headline grading analyzers do not admit new selected digests; they require a separate consumer integration.",
        ],
    }


def validate_successor(payload: dict[str, Any], frozen_source_dir: Path, *,
                       repairs_handoff: Path | None = None, setup_handoff: Path | None = None,
                       repo_root: Path = ROOT, native_control_metadata: tuple[Path, ...] = ()) -> None:
    """Validate candidate contents and current source/package/record custody."""
    expected = successor_payload(frozen_source_dir, repairs_handoff=repairs_handoff,
                                 setup_handoff=setup_handoff, repo_root=repo_root,
                                 native_control_metadata=native_control_metadata)
    if encoded(payload) != encoded(expected):
        raise ValueError("successor manifest differs from its exact frozen inputs, selected identities, or closed gates")


def assemble_successor(frozen_source_dir: Path, destination: Path, *,
                       repairs_handoff: Path | None = None, setup_handoff: Path | None = None,
                       repo_root: Path = ROOT,
                       native_control_metadata: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Atomically publish a new candidate without replacing any existing file."""
    destination = destination.resolve()
    if destination.is_relative_to(frozen_source_dir.resolve()):
        raise ValueError("successor destination must be outside the frozen source directory")
    payload = successor_payload(frozen_source_dir, repairs_handoff=repairs_handoff,
                                setup_handoff=setup_handoff, repo_root=repo_root,
                                native_control_metadata=native_control_metadata)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(encoded(payload))
            handle.flush()
            os.fsync(handle.fileno())
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--package-root", type=Path, default=SHARED_ROOT)
    parser.add_argument("--training-root", type=Path)
    parser.add_argument("--findings", type=Path)
    parser.add_argument("--pairs", type=Path)
    parser.add_argument("--frozen-source-dir", type=Path)
    parser.add_argument("--repairs-handoff", type=Path)
    parser.add_argument("--setup-handoff", type=Path)
    parser.add_argument("--native-control-metadata", type=Path, action="append", default=[])
    parser.add_argument("--successor-destination", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--manifest-only", action="store_true")
    mode.add_argument("--preseal-research-correction", action="store_true")
    mode.add_argument("--preseal-target-policy", action="store_true")
    mode.add_argument("--release-validity", type=Path)
    mode.add_argument("--release-scoring", action="store_true")
    mode.add_argument("--assemble-successor", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--approval")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    pairs_path = (args.pairs or output_dir / "package-pairs.csv").resolve()
    try:
        if args.assemble_successor:
            if args.frozen_source_dir is None or args.successor_destination is None:
                raise ValueError("--assemble-successor requires --frozen-source-dir and --successor-destination")
            if args.actor is not None or args.approval is not None:
                raise ValueError("successor assembly cannot accept scoring-release authority")
            payload = assemble_successor(
                args.frozen_source_dir, args.successor_destination,
                repairs_handoff=args.repairs_handoff, setup_handoff=args.setup_handoff, repo_root=args.repo_root,
                native_control_metadata=tuple(args.native_control_metadata),
            )
            print(json.dumps({
                "counts": payload["counts"], "manifest": fingerprint(args.successor_destination),
                "scoring_allowed": False, "pending_gates": payload["pending_gates"],
            }, sort_keys=True))
            return
        if any(value is not None for value in (
                args.frozen_source_dir, args.repairs_handoff, args.setup_handoff, args.successor_destination,
                *args.native_control_metadata)):
            raise ValueError("successor input/destination arguments require --assemble-successor")
        if args.release_scoring:
            split = release_scoring(output_dir, args.actor, args.approval)
        elif args.release_validity:
            release_path = args.release_validity.resolve()
            split_path = output_dir / "split.json"
            split = json.loads(split_path.read_text(encoding="utf-8"))
            validate_split(split)
            validate_release_inputs(split, output_dir, release_path)
            old_heldout = json.loads((output_dir / "heldout.json").read_text(encoding="utf-8"))
            if old_heldout["scoring_allowed"] is not False or old_heldout["heldout"] != split["heldout"]:
                raise ValueError("validity release requires an unscored, unchanged heldout membership")
            old_manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            release = json.loads(release_path.read_text(encoding="utf-8"))
            if fingerprint(pairs_path) != release["package_pairs"] or release["package_pairs"] != old_manifest["package_pairs"]:
                raise ValueError("validity release requires the frozen package-pair CSV")
            from evallab.registry import task_directory_digest
            package_root = Path(split["config"]["package_root"])
            rows = rows_by_id(list(csv.DictReader(io.StringIO(pairs_path.read_text(encoding="utf-8")))))
            for task_id in split["heldout"]:
                for version in ("original", "v6"):
                    path = package_root / rows[task_id][f"{version}_package_path"]
                    if task_directory_digest(path) != rows[task_id][f"{version}_package_digest"]:
                        raise ValueError(f"materialized {version} package digest mismatch: {task_id}")
            manifest = manifest_payload(split, output_dir, pairs_path, policy_revision=old_manifest.get("policy_revision"), release_path=release_path)
            replace_json(output_dir / "manifest.json", manifest)
            # The consumer's starting gun is published last, after the pair table.
            replace_json(output_dir / "heldout.json", heldout_payload(split, split_path, release_path))
        elif args.preseal_target_policy:
            manifest_path = output_dir / "manifest.json"
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if old_manifest.get("policy_revision") or old_manifest["evaluation_target_policy"]["status"] != "unresolved":
                raise ValueError("target-policy succession already applied; use an explicit successor")
            for item in old_manifest["sources"] + [old_manifest["split"], old_manifest["heldout_manifest"]]:
                if fingerprint(Path(item["path"])) != item:
                    raise ValueError(f"frozen input drift before target-policy succession: {item['path']}")
            if fingerprint(pairs_path) != old_manifest["package_pairs"]:
                raise ValueError("target-policy succession requires the same frozen package-pair CSV")
            split = json.loads((output_dir / "split.json").read_text(encoding="utf-8"))
            validate_split(split)
            policy_revision = {
                "predecessor_manifest": fingerprint(manifest_path),
                "authority": "Research", "relay": "Main", "decision_date": "2026-10-16",
                "directive": "Held-out grading package is v6 for all, test-repaired variants only where they exist.",
                "authorization": "Explicit pre-seal succession of our unpublished unscored draft; parent owns final scoring gate.",
                "scoring_occurred": False,
            }
            manifest = manifest_payload(split, output_dir, pairs_path, policy_revision=policy_revision)
            manifest_path.write_text(encoded(manifest), encoding="utf-8")
        elif args.preseal_research_correction:
            split_path = output_dir / "split.json"
            old = json.loads(split_path.read_text(encoding="utf-8"))
            if old.get("revision") or (output_dir / "manifest.json").exists():
                raise ValueError("pre-seal correction already applied or manifest sealed; use an explicit successor")
            input_successions = []
            for item in old["sources"]:
                current = fingerprint(Path(item["path"]))
                if current != item:
                    input_successions.append({"previous": item, "current": current})
            revision = {
                "predecessor_split": fingerprint(split_path),
                "predecessor_heldout": fingerprint(output_dir / "heldout.json"),
                "prior_heldout": old["heldout"], "added_heldout": RESEARCH_PINS,
                "prior_training_exposure": old.get("training_exposure", old["train"]),
                "input_successions": input_successions,
                "provenance": RESEARCH_PROVENANCE,
            }
            split = build_split(old["config"], revision=revision)
            split_path.write_text(encoded(split), encoding="utf-8")
            (output_dir / "heldout.json").write_text(encoded(heldout_payload(split, split_path)), encoding="utf-8")
            create_json(output_dir / "manifest.json", manifest_payload(split, output_dir, pairs_path))
        elif args.check or args.manifest_only:
            split = check_frozen(output_dir)
            if args.manifest_only:
                create_json(output_dir / "manifest.json", manifest_payload(split, output_dir, pairs_path))
        else:
            if any((output_dir / name).exists() for name in ("split.json", "heldout.json", "manifest.json")):
                raise ValueError("outputs already frozen; use --check or an explicit successor --output-dir; never overwrite")
            repo, package_root = args.repo_root.resolve(), args.package_root.resolve()
            config = {
                "repo_root": str(repo), "package_root": str(package_root),
                "ordinary": str(repo / "research/experiments/qa-trace/regrade/ordinary-passes.jsonl"),
                "v6_manifest": str(repo / "research/experiments/mimo-clean-v6/manifest.csv"),
                "reference_index": str(repo / "research/experiments/mimo-reference-fixes/index.csv"),
                "findings": str((args.findings or package_root / "derived/qa/ref-subset/mimo-v2.6-rl-code/findings-published.jsonl").resolve()),
                "training_root": str((args.training_root or package_root / "derived/qa-trace/training-arms/2026-10-16").resolve()),
            }
            revision = {"prior_heldout": [], "added_heldout": RESEARCH_PINS, "provenance": RESEARCH_PROVENANCE}
            split = build_split(config, revision=revision)
            create_json(output_dir / "split.json", split)
            create_json(output_dir / "heldout.json", heldout_payload(split, output_dir / "split.json"))
            if pairs_path.is_file():
                create_json(output_dir / "manifest.json", manifest_payload(split, output_dir, pairs_path))
        scoring_allowed = json.loads((output_dir / "heldout.json").read_text(encoding="utf-8"))["scoring_allowed"]
        print(json.dumps({"counts": split["counts"], "manifest_created": (output_dir / "manifest.json").is_file(), "scoring_allowed": scoring_allowed}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"freeze: {exc}\n")


if __name__ == "__main__":
    main()
