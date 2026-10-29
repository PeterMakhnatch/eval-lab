"""Synthetic identity metadata only; never deserialize repository key bodies."""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from evallab.production.cloud_trace_db import admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry

SOURCE = Path(__file__).resolve().parents[1] / "research/experiments/oq10-task-quality-cross"
sys.path.insert(0, str(SOURCE))
try:
    spec = importlib.util.spec_from_file_location("oq10_authority_test_module", SOURCE / "authority.py")
    authority = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(authority)
finally:
    sys.path.pop(0)


def sources(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    ledger = root / "assignments.csv"
    rows = [
        ("tb4-audit-v1", "f-safe", "safe", "dev"),
        ("tb4-audit-v1", "f-alias", "alias", "dev"),
        ("tb4-audit-v1", "f-digest-alias", "digest-alias", "dev"),
        ("tb4-audit-v1", "terminal-bench/pkg-sealed", "pkg-sealed", "test"),
        ("secondary-key", "secondary-test", "alias", "test"),
        ("cheatbench-actions-v1", "trace-test", "trace-unit", "test"),
    ]
    with ledger.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["key_id", "item_id", "task", "split"])
        writer.writerows(rows)
    task_metadata = root / "tasks.jsonl"
    task_metadata.write_text("".join(json.dumps({
        "subject": {"task_id": "terminal-bench/" + task,
                    "task_digest": "sha256:" + ("a" if task in {"pkg-sealed", "digest-alias"} else "b") * 64},
        "split": "test" if task == "pkg-sealed" else "dev",
        "pm_finding_ids": ["synthetic-metadata-id"],
    }) + "\n" for task in ("safe", "alias", "digest-alias", "pkg-sealed")))
    inventory = root / "inventory.json"
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
        "tasks": [{"task_id": "mimo-" + suffix, "record_id": "mimo-" + suffix + "@" + letter * 64,
                   "package_digest": "sha256:" + letter * 64,
                   "heldout_excluded_from_training_all_versions": suffix == "held"}
                  for suffix, letter in (("held", "c"), ("safe", "d"))],
    }))
    # A body file exists, so an accidental eager load fails this test rather than
    # looking harmless because a missing source was silently skipped.
    body = root / "items.jsonl"
    body.write_text("THIS SYNTHETIC KEY BODY MUST NEVER BE OPENED")
    return ledger, task_metadata, inventory, body


def candidate(task, *, dataset="tb4-audit-v1", item=None, digest=None):
    return {"task_id": task, "dataset_id": dataset, "split": "dev", "item_id": item,
            "item_type": "text" if item is not None else None,
            "original_grader_digest": digest, "repaired_grader_digest": None}


def test_complete_metadata_projection_preserves_native_every_key_alias_and_digest_seals(tmp_path, monkeypatch):
    ledger, tasks, inventory, body = sources(tmp_path)
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path.resolve() == body.resolve():
            pytest.fail("canonical item bodies were opened")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    result = authority.project(ledger=authority.study.pin(ledger), tb4_metadata=authority.study.pin(tasks),
                               mimo_metadata=authority.study.pin(inventory), output=tmp_path / "projection",
                               registry_type=CorpusRegistry)
    root = Path(result["root"])
    registry = CorpusRegistry(repo_root=root)
    rows = [
        candidate("safe", item="f-safe", digest="sha256:" + "b" * 64),
        candidate("alias", item="f-alias"),
        candidate("digest-alias", item="f-digest-alias", digest="sha256:" + "a" * 64),
        candidate("mimo-held", dataset="mimo-defect-map-v1", item="mimo-held@" + "c" * 64),
        candidate("mimo-safe", dataset="mimo-defect-map-v1", item="mimo-safe@" + "d" * 64),
    ]
    assert admitted_cloud_task_ids(rows, registry=registry) == {"safe", "mimo-safe"}
    assert (root / "research/answer-keys/splits/assignments.csv").read_bytes() == ledger.read_bytes()
    receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert receipt["all_key_ledger_rows"] == 6
    assert receipt["ledger_rows_by_key"]["secondary-key"] == 1
    assert receipt["tb4_package_subjects"] == 4 and receipt["mimo_exact_packages"] == 2
    assert receipt["mimo_permanent_heldout_identities"] == 1
    assert receipt["canonical_item_body_files_opened"] == receipt["cross_records_registered"] == 0


def test_metadata_drift_and_ambiguous_package_links_stop_before_registry_construction(tmp_path):
    ledger, tasks, inventory, _ = sources(tmp_path)
    expected = authority.study.pin(tasks)
    tasks.write_text(tasks.read_text() + json.dumps({
        "subject": {"task_id": "other-prefix/safe", "task_digest": "sha256:" + "e" * 64}, "split": "dev",
    }) + "\n")
    with pytest.raises(ValueError):
        authority.project(ledger=authority.study.pin(ledger), tb4_metadata=expected,
                          mimo_metadata=authority.study.pin(inventory), output=tmp_path / "stale", registry_type=CorpusRegistry)
    with pytest.raises(ValueError, match="ambiguous package identities"):
        authority.project(ledger=authority.study.pin(ledger), tb4_metadata=authority.study.pin(tasks),
                          mimo_metadata=authority.study.pin(inventory), output=tmp_path / "ambiguous", registry_type=CorpusRegistry)
    assert not (tmp_path / "stale").exists() and not (tmp_path / "ambiguous").exists()
