"""Gym freeze contract and generation tests.

A frozen manifest makes campaign numbers comparable across time.
Properties tested here:
1. Determinism — same registry state renders byte-identical JSON.
2. Immutability — writing over an existing frozen manifest is refused.
3. Honesty & Completeness — manifest matches registered tasks, task IDs, versions,
   component digests, and battery evidence pointers.
4. Digest mismatch & drift detection — mutation or registry tampering is detected.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = REPO_ROOT / "library" / "frozen" / "gym-v0" / "_freeze.py"
MANIFEST_V0_PATH = REPO_ROOT / "library" / "frozen" / "gym-v0" / "manifest.json"
MANIFEST_V1_PATH = REPO_ROOT / "library" / "frozen" / "gym-v1" / "manifest.json"


def _load_freeze_module() -> Any:
    spec = importlib.util.spec_from_file_location("gym_freeze", FREEZE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


freeze = _load_freeze_module()


def test_freeze_generator_exists_and_manifests_are_committed() -> None:
    assert FREEZE_PATH.is_file(), "the freeze generator must be committed, not ad-hoc"
    assert MANIFEST_V0_PATH.is_file(), "gym-v0 must have a committed frozen manifest"
    assert MANIFEST_V1_PATH.is_file(), "gym-v1 must have a committed frozen manifest"


def test_committed_manifest_v0_declares_the_freeze_contract() -> None:
    manifest = json.loads(MANIFEST_V0_PATH.read_text(encoding="utf-8"))
    assert manifest["generation"] == "gym-v0"
    assert manifest["frozen"] is True
    assert manifest["schema_version"] == freeze.MANIFEST_SCHEMA_VERSION
    assert manifest["task_count"] == len(manifest["tasks"]) == 0
    assert manifest["repo_commit"], "a freeze must record the commit it was taken at"
    assert "note" in manifest, "gym-v0 empty generation must carry explanatory note"


def test_committed_manifest_v1_declares_four_tasks_and_provenance() -> None:
    manifest = json.loads(MANIFEST_V1_PATH.read_text(encoding="utf-8"))
    assert manifest["generation"] == "gym-v1"
    assert manifest["frozen"] is True
    assert manifest["schema_version"] == freeze.MANIFEST_SCHEMA_VERSION
    assert manifest["task_count"] == len(manifest["tasks"]) == 4
    assert manifest["repo_commit"], "a freeze must record the commit it was taken at"
    assert "note" not in manifest, "gym-v1 is non-empty and needs no empty-set note"

    task_ids = {t["task_id"] for t in manifest["tasks"]}
    expected_ids = {
        "event-summary",
        "query-optimize",
        "terminal-bench-html-js-filter",
        "transaction-reconciliation",
    }
    assert task_ids == expected_ids

    for t in manifest["tasks"]:
        assert t["state"] == "registered"
        assert t["version"]
        assert t["task_path"]
        digests = t["digests"]
        for key in ("package", "task_toml", "instruction", "environment", "verifier"):
            assert digests[key].startswith("sha256:"), (
                f"missing or invalid digest {key} in {t['task_id']}"
            )
        battery = t["battery_evidence"]
        assert battery["oracle"]["reward"] == 1.0
        assert battery["oracle"]["evidence_digest"].startswith("sha256:")
        assert battery["nop"]["reward"] == 0.0
        assert battery["nop"]["evidence_digest"].startswith("sha256:")


def test_render_is_deterministic() -> None:
    """Two renders of one manifest are byte-identical."""
    manifest = freeze.build_manifest(REPO_ROOT, generation="gym-v1", frozen_at=None)
    assert freeze.render(manifest) == freeze.render(manifest)


def test_two_builds_agree_on_the_task_set() -> None:
    first = freeze.build_manifest(REPO_ROOT, generation="gym-v1")
    second = freeze.build_manifest(REPO_ROOT, generation="gym-v1")
    assert first["tasks"] == second["tasks"]
    assert first["task_count"] == second["task_count"]


def test_manifest_matches_exact_registry_digests_and_evidence() -> None:
    """The manifest's digests and evidence must faithfully mirror the active TaskRegistry."""
    from evallab.registry import TaskRegistry

    registry = TaskRegistry.from_repo(REPO_ROOT)
    registered_map = {
        record.task_id: record
        for record in registry.list_records()
        if record.state == "registered"
    }

    manifest = json.loads(MANIFEST_V1_PATH.read_text(encoding="utf-8"))
    assert manifest["task_count"] == len(registered_map)

    for entry in manifest["tasks"]:
        task_id = entry["task_id"]
        assert task_id in registered_map
        rec = registered_map[task_id]
        assert entry["version"] == rec.version
        assert entry["task_path"] == rec.task_path
        assert entry["digests"]["package"] == rec.digests.package
        assert entry["digests"]["task_toml"] == rec.digests.task_toml
        assert entry["digests"]["instruction"] == rec.digests.instruction
        assert entry["digests"]["environment"] == rec.digests.environment
        assert entry["digests"]["verifier"] == rec.digests.verifier
        assert entry["battery_evidence"]["oracle"]["reward"] == rec.control_evidence.oracle.reward
        assert (
            entry["battery_evidence"]["oracle"]["evidence_digest"]
            == rec.control_evidence.oracle.evidence_digest
        )
        assert entry["battery_evidence"]["nop"]["reward"] == rec.control_evidence.nop.reward
        assert (
            entry["battery_evidence"]["nop"]["evidence_digest"]
            == rec.control_evidence.nop.evidence_digest
        )


def test_registry_digest_mismatch_is_refused_for_frozen_task_inputs(tmp_path: Path) -> None:
    """A task mutation must fail registry resolution, not silently become a new cohort."""
    from evallab.registry import TaskDigestMismatchError, TaskRegistry
    from evallab.schemas import ExperimentSpec

    temp_registry = tmp_path / "library" / "registry"
    temp_tasks = tmp_path / "library" / "tasks"
    temp_registry.parent.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "library" / "registry", temp_registry)
    shutil.copytree(
        REPO_ROOT / "library" / "tasks" / "event-summary",
        temp_tasks / "event-summary",
    )

    instruction = temp_tasks / "event-summary" / "instruction.md"
    instruction.write_text(
        instruction.read_text(encoding="utf-8") + "\nmutation\n",
        encoding="utf-8",
    )

    spec = ExperimentSpec(
        name="gym-v1-digest-check",
        hypothesis="tampered frozen task must be refused",
        purpose="baseline",
        task="registered/event-summary",
        task_path="library/tasks/event-summary",
        task_version="1.0.0",
        agent="oracle",
        submitted_by="test",
    )
    with pytest.raises(TaskDigestMismatchError, match="instruction bytes"):
        TaskRegistry.from_repo(tmp_path).resolve_spec(spec, tmp_path)


def test_manifest_only_claims_tasks_the_registry_registers() -> None:
    """The manifest may not out-claim the registry."""
    from evallab.registry import TaskRegistry

    registered = {
        record.task_id
        for record in TaskRegistry.from_repo(REPO_ROOT).list_records()
        if record.state == "registered"
    }
    committed_v0 = {
        entry["task_id"] for entry in json.loads(MANIFEST_V0_PATH.read_text())["tasks"]
    }
    committed_v1 = {
        entry["task_id"] for entry in json.loads(MANIFEST_V1_PATH.read_text())["tasks"]
    }

    assert committed_v0 <= registered
    assert committed_v1 == registered


def test_writing_over_a_frozen_manifest_is_refused(tmp_path: Path) -> None:
    """Frozen means frozen: the second write must raise, not overwrite."""
    destination = tmp_path / "manifest.json"
    manifest = freeze.build_manifest(REPO_ROOT, generation="gym-v1")
    freeze.write_manifest(destination, manifest)
    original = destination.read_text(encoding="utf-8")

    with pytest.raises(freeze.FreezeRefused):
        freeze.write_manifest(destination, {"generation": "tampered", "tasks": []})

    assert destination.read_text(encoding="utf-8") == original


def test_comparison_render_may_be_written_elsewhere(tmp_path: Path) -> None:
    """Regenerating for comparison is allowed — just never over the frozen file."""
    destination = tmp_path / "compare.json"
    manifest = freeze.build_manifest(REPO_ROOT, generation="gym-v1")
    freeze.write_manifest(destination, manifest)
    freeze.write_manifest(destination, manifest, allow_overwrite=True)
    assert json.loads(destination.read_text())["generation"] == "gym-v1"


def test_gym_v0_manifest_is_immutable_and_preserved() -> None:
    """gym-v0 must remain unchanged as the immutable zero-task record."""
    manifest_v0 = json.loads(MANIFEST_V0_PATH.read_text(encoding="utf-8"))
    assert manifest_v0["generation"] == "gym-v0"
    assert manifest_v0["task_count"] == 0
    assert manifest_v0["tasks"] == []
