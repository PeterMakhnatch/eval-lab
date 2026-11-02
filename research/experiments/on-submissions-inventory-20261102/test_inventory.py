"""Unit tests for A0 submissions inventory."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from evallab.production.corpus_queue import CorpusRegistry

_spec = importlib.util.spec_from_file_location("inventory", Path(__file__).parent / "inventory.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

fmt = _mod.fmt
normalize_task_id = _mod.normalize_task_id
wilson = _mod.wilson

def test_wilson_interval() -> None:
    lo, hi = wilson(2, 32)
    assert 0.01 < lo < 0.03
    assert 0.18 < hi < 0.22
    assert "2/32" in fmt(2, 32)


def test_task_id_normalization() -> None:
    assert normalize_task_id("format-code-task-000240") == "format-code-task-000240"
    assert normalize_task_id("har104-d-002391") == "format-code-task-002391"
    assert normalize_task_id("000783-pressure-a1") == "format-code-task-000783"
    assert normalize_task_id("chrr1-l-honest-002391") == "format-code-task-002391"


def test_counts_json_contract() -> None:
    counts_path = Path(__file__).parent / "counts.json"
    assert counts_path.is_file()
    data = json.loads(counts_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "submissions-inventory-counts/v1"
    assert data["counts"]["total_trials"] == 691
    assert data["counts"]["trials_accepted_with_patch"] == 202
    assert data["gold_universe_join"]["gold_ok_tasks"] == 32
    assert data["gold_universe_join"]["gold_ok_with_ge_3_accepted"] == 2


def test_registry_integration() -> None:
    cr = CorpusRegistry()
    assert cr.has_assignment_authority()
    assert cr.sealed_identity(task_id="format-code-task-002391") is None
    assert cr.sealed_identity(task_id="format-code-task-000657") is not None
