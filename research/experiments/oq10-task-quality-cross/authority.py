"""Project complete split/package identity metadata without loading key item bodies.

Only the canonical assignments CSV, TB4 task *metadata index*, and MiMo package
inventory are inputs. Their producer code is pinned with the study. The projection
contains no finding narrative, witness, instruction, grader, or cross-study row.
The existing CorpusRegistry and admitted_cloud_task_ids remain the sole policy
implementation; this module neither registers external cases nor grants access.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import study

CANONICAL = ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1")


def project(*, ledger: dict[str, str], tb4_metadata: dict[str, str],
            mimo_metadata: dict[str, str], output: Path, registry_type: Any) -> dict[str, Any]:
    """Keep ALL ledger identities and ALL package/digest/heldout links, not eight cases."""
    raw = study.verify_pin(ledger).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    if reader.fieldnames is None or not {"key_id", "item_id", "task", "split"}.issubset(reader.fieldnames):
        raise ValueError("canonical split metadata has an invalid header")
    assignments = list(reader)
    if any(None in row for row in assignments):
        raise ValueError("canonical split metadata has an invalid row")
    task_rows = [json.loads(line) for line in study.verify_pin(tb4_metadata).read_text(encoding="utf-8").splitlines() if line.strip()]
    inventory = json.loads(study.verify_pin(mimo_metadata).read_bytes())
    if inventory.get("dataset_id") != "mimo-defect-map-v1":
        raise ValueError("identity inventory has an unexpected dataset")
    normalize = registry_type.task_name
    subjects: dict[str, dict[str, Any]] = {}
    task_index = []
    for row in task_rows:
        original = row["subject"]
        subject = {"task_id": original["task_id"], "task_digest": original.get("task_digest")}
        name = normalize(subject["task_id"])
        if name in subjects and subjects[name] != subject:
            raise ValueError("task metadata has ambiguous package identities")
        subjects[name] = subject
        task_index.append({"subject": subject, "split": row["split"]})
    items: dict[str, list[dict[str, Any]]] = {key: [] for key in CANONICAL}
    seen: set[tuple[str, str]] = set()
    for row in assignments:
        key = row["key_id"]
        if key not in items or (key, row["item_id"]) in seen:
            continue
        seen.add((key, row["item_id"]))
        task = row["task"] or None
        subject = subjects.get(normalize(task or row["item_id"])) if key == "tb4-audit-v1" else None
        # external_traces.py's canonical trace subjects explicitly have no task_digest.
        subject = subject or {"task_id": task, "task_digest": None}
        items[key].append({"item_id": row["item_id"], "split": row["split"], "subject": subject})
    task_index = [row for row in task_index if ("tb4-audit-v1", row["subject"]["task_id"]) not in seen]
    minimal_inventory = {
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": inventory["heldout_task_ids_excluded_from_training_all_versions"],
        "tasks": [{key: row[key] for key in ("task_id", "record_id", "package_digest", "heldout_excluded_from_training_all_versions")}
                  for row in inventory["tasks"]],
    }
    names = [row["record_id"] for row in minimal_inventory["tasks"]]
    if len(names) != len(set(names)):
        raise ValueError("package inventory repeats an exact record identity")
    output.mkdir(parents=True, exist_ok=False)
    keys = output / "research/answer-keys"
    destination = keys / "splits/assignments.csv"
    destination.parent.mkdir(parents=True)
    with destination.open("xb") as handle:
        handle.write(raw)
    def jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    for key, rows in items.items():
        jsonl(keys / key / "items.jsonl", rows)
    jsonl(keys / "tb4-audit-v1/tasks.jsonl", task_index)
    study.dump_new(output / "research/experiments/mimo-defect-map-v1/inventory.json", minimal_inventory)
    # Native construction validates the full ledger, creates every-key TEST
    # precedence, and seals all task names, item ids, aliases and package digests.
    registry = registry_type(repo_root=output)
    if registry.split_assignments_digest != "sha256:" + ledger["sha256"]:
        raise ValueError("native registry did not consume the complete canonical ledger")
    receipt = {
        "schema": "oq10.full-metadata-registry-projection/v1",
        "root": str(output.resolve()), "projector": study.pin(Path(__file__)),
        "registry_consumer": study.pin(Path(sys.modules[registry_type.__module__].__file__)),
        "sources": {"ledger": ledger, "tb4_task_metadata": tb4_metadata, "mimo_inventory_metadata": mimo_metadata},
        "all_key_ledger_rows": len(assignments),
        "ledger_rows_by_key": dict(Counter(row["key_id"] for row in assignments)),
        "canonical_identity_rows": {key: len(rows) for key, rows in items.items()},
        "tb4_package_subjects": len(subjects), "mimo_exact_packages": len(names),
        "mimo_permanent_heldout_identities": len(minimal_inventory["heldout_task_ids_excluded_from_training_all_versions"]),
        "canonical_item_body_files_opened": 0, "protected_task_bodies_opened": 0,
        "cross_records_registered": 0, "admission_policy": "unchanged CorpusRegistry + admitted_cloud_task_ids",
        "projection_files": [study.pin(path) for path in sorted(output.rglob("*")) if path.is_file()],
    }
    study.dump_new(output / "projection-receipt.json", receipt)
    return {"root": str(output.resolve()), "receipt": study.pin(output / "projection-receipt.json"), "files": receipt["projection_files"]}
