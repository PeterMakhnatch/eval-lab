"""Lost study custody must never bootstrap another paid allowance."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1] / "research/experiments/leak-oracle"


@pytest.mark.parametrize("missing", ["budget.json", "run.json"])
def test_missing_custody_half_cannot_initialize_a_fresh_allowance(tmp_path, monkeypatch, missing):
    monkeypatch.syspath_prepend(str(HERE))
    spec = importlib.util.spec_from_file_location("har191_sweep_test", HERE / "sweep.py")
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    metadata = {
        "manifest_sha256": "a" * 64,
        "authority": sweep.AUTHORITY,
        "app_ids": ["ap-already-owned"],
    }
    remaining = "run.json" if missing == "budget.json" else "budget.json"
    content = json.dumps(
        metadata if remaining == "run.json" else {"schema_version": 2, "batches": []}
    )
    (tmp_path / remaining).write_text(content)
    args = SimpleNamespace(
        evidence=tmp_path, manifest=tmp_path / "cohort.json", task=[], max_open=0
    )
    with pytest.raises(ValueError):
        sweep.Sweep(args, {"tasks": [{"task_id": "format-code-task-002552"}]}, "a" * 64)
    assert not (tmp_path / missing).exists()
    assert (tmp_path / remaining).read_text() == content
