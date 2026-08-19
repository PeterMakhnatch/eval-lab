"""GYM-RUN cycle 1: the gym-v0 freeze contract.

A frozen manifest is the thing that makes next month's campaign numbers comparable
to tomorrow's. Two properties carry that weight and are tested here:

1. **Determinism** — the same registry state renders byte-identical bytes, so a
   diff means the gym changed, not that the generator wandered.
2. **Immutability** — writing over an existing frozen manifest is refused. Without
   this the file is just a cache tracking whatever the registry says today, and
   every result citing it becomes uncomparable.

The third property is honesty: the manifest may only claim tasks the registry
actually registers.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = REPO_ROOT / "library" / "frozen" / "gym-v0" / "_freeze.py"
MANIFEST_PATH = REPO_ROOT / "library" / "frozen" / "gym-v0" / "manifest.json"


def _load_freeze_module() -> Any:
    spec = importlib.util.spec_from_file_location("gym_freeze", FREEZE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


freeze = _load_freeze_module()


def test_freeze_generator_exists_and_manifest_is_committed() -> None:
    assert FREEZE_PATH.is_file(), "the freeze generator must be committed, not ad-hoc"
    assert MANIFEST_PATH.is_file(), "gym-v0 must have a committed frozen manifest"


def test_committed_manifest_declares_the_freeze_contract() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["generation"] == "gym-v0"
    assert manifest["frozen"] is True
    assert manifest["schema_version"] == freeze.MANIFEST_SCHEMA_VERSION
    assert manifest["task_count"] == len(manifest["tasks"])
    assert manifest["repo_commit"], "a freeze must record the commit it was taken at"


def test_render_is_deterministic() -> None:
    """Two renders of one manifest are byte-identical."""
    manifest = freeze.build_manifest(REPO_ROOT, frozen_at=None)
    assert freeze.render(manifest) == freeze.render(manifest)


def test_two_builds_agree_on_the_task_set() -> None:
    first = freeze.build_manifest(REPO_ROOT)
    second = freeze.build_manifest(REPO_ROOT)
    assert first["tasks"] == second["tasks"]
    assert first["task_count"] == second["task_count"]


def test_manifest_only_claims_tasks_the_registry_registers() -> None:
    """The manifest may not out-claim the registry.

    This is the honesty property: a frozen file that lists tasks nobody registered
    would make the whole campaign's provenance fiction.
    """
    from evallab.registry import TaskRegistry

    registered = {
        record.task_id
        for record in TaskRegistry.from_repo(REPO_ROOT).list_records()
        if record.state == "registered"
    }
    committed = {entry["task_id"] for entry in json.loads(MANIFEST_PATH.read_text())["tasks"]}
    assert committed <= registered, f"manifest claims unregistered tasks: {committed - registered}"


def test_empty_registry_is_recorded_as_a_note_not_hidden() -> None:
    """An empty gym must say so out loud, with the measurement behind it."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest["task_count"] == 0:
        assert "note" in manifest, "an empty generation must carry an explanatory note"
        assert "registry" in manifest["note"].lower()
    else:  # a later non-empty freeze needs no note
        assert manifest["tasks"]


def test_writing_over_a_frozen_manifest_is_refused(tmp_path: Path) -> None:
    """Frozen means frozen: the second write must raise, not overwrite."""
    destination = tmp_path / "manifest.json"
    manifest = freeze.build_manifest(REPO_ROOT)
    freeze.write_manifest(destination, manifest)
    original = destination.read_text(encoding="utf-8")

    with pytest.raises(freeze.FreezeRefused):
        freeze.write_manifest(destination, {"generation": "tampered", "tasks": []})

    assert destination.read_text(encoding="utf-8") == original


def test_comparison_render_may_be_written_elsewhere(tmp_path: Path) -> None:
    """Regenerating for comparison is allowed — just never over the frozen file."""
    destination = tmp_path / "compare.json"
    manifest = freeze.build_manifest(REPO_ROOT)
    freeze.write_manifest(destination, manifest)
    freeze.write_manifest(destination, manifest, allow_overwrite=True)
    assert json.loads(destination.read_text())["generation"] == "gym-v0"
