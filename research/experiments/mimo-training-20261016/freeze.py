"""Freeze metadata-only MiMo train/held-out inputs; never execute task payloads.

Run once to create split.json and heldout.json. Once package-pairs.csv arrives,
run --manifest-only. --check is read-only; drift requires a new --output-dir.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--package-root", type=Path, default=SHARED_ROOT)
    parser.add_argument("--training-root", type=Path)
    parser.add_argument("--findings", type=Path)
    parser.add_argument("--pairs", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--manifest-only", action="store_true")
    mode.add_argument("--preseal-research-correction", action="store_true")
    mode.add_argument("--preseal-target-policy", action="store_true")
    mode.add_argument("--release-validity", type=Path)
    mode.add_argument("--release-scoring", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--approval")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    pairs_path = (args.pairs or output_dir / "package-pairs.csv").resolve()
    try:
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
