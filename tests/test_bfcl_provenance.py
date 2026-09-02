from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LOCK = ROOT / "library/benchmarks/bfcl-parity/source-lock.json"
PREFLIGHT = ROOT / "scripts/bfcl_preflight.py"
FIXTURE_FILES = {
    "instruction.md": "Write /app/result.json.\n",
    "task.toml": 'version = "1.0"\n',
}
FIXTURE_DIGEST = "sha256:4373a55c45dfe1957e8270da17c0263810e63aef28f8c9b79ab529e4791f50c6"


def _load_preflight():
    spec = importlib.util.spec_from_file_location("bfcl_preflight", PREFLIGHT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bfcl_lock_records_complete_prospective_lineage() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert lock["benchmark"] == "bfcl_parity"
    assert lock["benchmark_surface"] == "instruction_to_result_file"
    assert (
        lock["canonical_source"]["revision"] == (lock["parity_source"]["canonical_parent_revision"])
    )
    assert lock["dataset"]["regeneration_revision"] == ("fe68df5b1c6c83a60e42ea9ae43eeb9733f163b9")
    assert lock["dataset"]["revision"] == ("ceeec951001f03f5fffc6c593d2d16c430d3f89b")
    assert lock["registry"]["status"] == "blocked_stale_legacy_pin"
    assert lock["legacy_trial_policy"]["analysis_eligibility"] is False
    assert (
        lock["canary_task"]["material_digest"]
        == "sha256:1d16051a0b8427cd2f7d093d5e54b2daf0c6a8efb49b6c829520144f7df8ef37"
    )


def test_bfcl_preflight_accepts_only_digest_bound_task(tmp_path: Path) -> None:
    preflight = _load_preflight()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    task = tmp_path / lock["canary_task"]["name"]
    task.mkdir()
    for relative, content in FIXTURE_FILES.items():
        (task / relative).write_text(content, encoding="utf-8")
    fixture_lock = json.loads(json.dumps(lock))
    fixture_lock["canary_task"]["material_digest"] = FIXTURE_DIGEST

    decision = preflight.validate_task(task, fixture_lock)
    assert decision == {
        "status": "proceed",
        "created_trial": False,
        "benchmark": "bfcl_parity",
        "dataset_revision": "ceeec951001f03f5fffc6c593d2d16c430d3f89b",
        "task_name": "bfcl-live-simple-2-2-0",
        "task_digest": FIXTURE_DIGEST,
        "registry_status": "blocked_stale_legacy_pin",
    }

    (task / "instruction.md").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="blocked:bfcl_task_digest_mismatch"):
        preflight.validate_task(task, fixture_lock)
