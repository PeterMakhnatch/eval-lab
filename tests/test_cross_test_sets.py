"""The cross-test page shows registry metadata only and refuses entries that break the rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cross_test_sets import load_registry, parse_registry

PRIVATE = "private caveat naming a path /Users/someone/.local/state/evallab-cross-test/bundle"


def _entry(entry_id: str = "set-a", **changes: Any) -> dict[str, Any]:
    entry = {
        "id": entry_id,
        "name": f"org/{entry_id}",
        "source_url": "https://huggingface.co/datasets/org/set",
        "revision": "b3d6f678",
        "bundle_path": "~/.local/state/evallab-cross-test/bundle",
        "unit": "run",
        "unit_note": "One scored record is a trajectory.",
        "status": "ready",
        "flags": {
            "eval_only": True,
            "local_only": True,
            "no_training": True,
            "no_rehosting": True,
            "uploadable_to_vendor_projects": False,
            "hosted_model_api_for_evaluation": "needs_peter",
            "basis": PRIVATE,
        },
        "licence": {"text": "cc-by-nc-4.0", "redistributable": False},
        "labels": {"type": "human verdict", "counts": {"rows": 84, "positives": 30}},
        "scoring": {"command_level_localization_scorable": False, "command_level_reason": "No gold index."},
        "adapter": {"merge_state": "merged", "pr": 1273},
        "caveats": [PRIVATE],
        "sources": [PRIVATE],
    }
    return entry | changes


def _hold(hold_id: str = "org/gated", **changes: Any) -> dict[str, Any]:
    return {
        "id": hold_id,
        "name": "Gated set",
        "source_url": "https://huggingface.co/datasets/org/gated",
        "status": "needs_peter",
        "available": False,
        "staged": False,
        "bundle_path": None,
        "why_held": "Gated dataset.",
        "action_for_peter": PRIVATE,
    } | changes


def _registry(**changes: Any) -> dict[str, Any]:
    return {
        "schema_version": "cross-test-registry/v1",
        "created": "2026-10-28",
        "validator": {"script": "bridge/run/validate_cross_test_registry.py"},
        "entries": [_entry("set-a"), _entry("set-b")],
        "hold_access": [_hold(), _hold("org/gated-2")],
        "hold_licence_not_staged": [{"id": "x"}, {"id": "y"}, {"id": "z"}],
    } | changes


def _board(registry: dict[str, Any]) -> dict[str, Any]:
    return parse_registry(json.dumps(registry).encode())


def test_entries_show_unit_labels_counts_scoring_licence_flags_and_adapter() -> None:
    board = _board(_registry())
    first = board["entries"][0]
    assert (first["id"], first["unit"], first["label_type"]) == ("set-a", "run", "human verdict")
    assert first["counts"] == {"rows": 84, "positives": 30}
    assert first["command_level"] == {"scorable": False, "reason": "No gold index."}
    assert first["licence"] == {"text": "cc-by-nc-4.0", "redistributable": False}
    assert first["flags"] == {"eval_only": True, "local_only": True, "no_training": True, "no_rehosting": True}
    assert first["uploadable"] is False and first["hosted_model_api"] == "needs_peter"
    assert first["adapter"] == {
        "merge_state": "merged", "pr": 1273, "pr_url": "https://github.com/PeterMakhnatch/eval-lab/pull/1273",
    }
    assert board["malformed"] == [] and board["hold_licence_not_staged_n"] == 3
    assert [item["state"] for item in board["hold_access"]] == ["needs Peter, not available"] * 2


def test_only_registry_metadata_is_served_never_paths_or_notes() -> None:
    served = json.dumps(_board(_registry()))
    assert PRIVATE not in served and "bundle_path" not in served and "/.local/state/" not in served
    assert "revision" not in served and "b3d6f678" not in served


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda e: e["flags"].update(uploadable_to_vendor_projects=True), "uploadable"),
        (lambda e: e["flags"].update(eval_only=False), "eval_only"),
        (lambda e: e["flags"].update(local_only=False), "local_only"),
        (lambda e: e["flags"].update(no_training=False), "no_training"),
        (lambda e: e["flags"].update(no_rehosting=False), "no-rehosting"),  # not redistributable
        (lambda e: e["labels"]["counts"].update(rows="84"), "counts"),
        (lambda e: e["labels"]["counts"].update(rows=True), "counts"),
        (lambda e: e["adapter"].update(merge_state="deployed"), "merge_state"),
        (lambda e: e["scoring"].update(command_level_localization_scorable="yes"), "command_level"),
        (lambda e: e.pop("licence"), "licence"),
    ],
)
def test_a_malformed_entry_is_reported_and_the_others_still_show(mutate: Any, needle: str) -> None:
    registry = _registry()
    mutate(registry["entries"][0])
    board = _board(registry)
    assert [row["id"] for row in board["entries"]] == ["set-b"]
    (problem,) = board["malformed"]
    assert problem["where"] == "entries[0]" and problem["id"] == "set-a"
    assert needle in problem["reason"]


def test_a_non_object_or_duplicate_entry_is_malformed_not_fatal() -> None:
    registry = _registry(entries=["not an object", _entry("set-a"), _entry("set-a")])
    board = _board(registry)
    assert [row["id"] for row in board["entries"]] == ["set-a"]
    assert [problem["where"] for problem in board["malformed"]] == ["entries[0]", "entries[2]"]
    assert "more than once" in board["malformed"][1]["reason"]


@pytest.mark.parametrize(
    "change",
    [{"available": True}, {"staged": True}, {"status": "ready"}, {"bundle_path": "/some/bundle"}],
)
def test_a_hold_set_is_never_shown_as_available(change: dict[str, Any]) -> None:
    registry = _registry(hold_access=[_hold(**change), _hold("org/gated-2")])
    board = _board(registry)
    assert [item["id"] for item in board["hold_access"]] == ["org/gated-2"]
    assert board["malformed"][0]["where"] == "hold_access[0]"


def test_links_are_https_only() -> None:
    registry = _registry(entries=[_entry(source_url="javascript:alert(1)")])
    assert _board(registry)["entries"][0]["source_url"] is None


def test_the_validator_command_is_built_from_a_safe_relative_script_only() -> None:
    board = _board(_registry())
    assert board["validator"]["command"] == (
        "cd ~/Developer/research-context && python3 bridge/run/validate_cross_test_registry.py"
    )
    assert board["validator"]["quick_command"].endswith("validate_cross_test_registry.py --quick --offline")
    for script in ("../escape.py", "/abs/validate.py", "x.py; rm -rf ~", "a b.py", 7):
        assert _board(_registry(validator={"script": script}))["validator"] is None
    assert _board(_registry(validator="nope"))["validator"] is None


def test_a_wrong_schema_or_broken_file_is_unreadable_and_an_absent_one_is_an_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "cross-test" / "registry.json"
    absent = load_registry(path)
    assert absent["present"] is False and "no cross-test registry" in absent["error"]
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    assert load_registry(path)["error"].startswith("unreadable")
    for bad in (_registry(schema_version="cross-test-registry/v2"), _registry(entries={}), [1, 2]):
        path.write_text(json.dumps(bad), encoding="utf-8")
        loaded = load_registry(path)
        assert loaded["present"] is False and loaded["error"].startswith("unreadable")
    path.write_text(json.dumps(copy.deepcopy(_registry())), encoding="utf-8")
    loaded = load_registry(path)
    assert loaded["present"] is True and loaded["error"] is None and len(loaded["entries"]) == 2
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere.json")
    assert load_registry(path)["present"] is False


def test_a_held_set_without_a_url_yet_is_still_listed_as_not_available() -> None:
    registry = _registry(hold_access=[_hold(source_url=None), _hold("org/gated-2")])
    board = _board(registry)
    assert board["malformed"] == []
    assert [(item["id"], item["source_url"]) for item in board["hold_access"]] == [
        ("org/gated", None),
        ("org/gated-2", "https://huggingface.co/datasets/org/gated"),
    ]
