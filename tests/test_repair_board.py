"""The repair board never turns a merged PR or a static check into a repaired grader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import repair_board
from evallab.production.file_cache import ChangeCache
from evallab.production.repair_board import build_board, load_inventory, parse_inventory

SECRET_WORDING = "weak_check wording that must stay in the inventory"


def _annotation(defect: str = "weak_check", **fields: Any) -> dict[str, Any]:
    return {
        "key_id": "pm-tb4:secret-key-id",
        "defect_type": defect,
        "weak_check": SECRET_WORDING,
        "source_evidence_paths": ["/private/answer-keys/x"],
        "registered_exact_package_repair": "none found",
        "existing_candidate_pr": None,
        "selected_ts9_repair": False,
    } | fields


def _task(task_id: str, annotations: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "dataset_id": "tb4-audit-v1",
        "key_file": "/private/answer-keys/key.json",
        "dev_assignments": [["tb4-audit-v1", "dev"]],
        "annotations": annotations,
    } | fields


def _delivery(task: str, **fields: Any) -> dict[str, Any]:
    return {
        "task": task,
        "pr": 1326,
        "pr_url": "https://github.com/PeterMakhnatch/eval-lab/pull/1326",
        "delivery_status": "MERGED",
        "verification_class": "static-only",
        "native_qualification": False,
    } | fields


def _inventory(**changes: Any) -> dict[str, Any]:
    return {
        "schema": "evallab.ts9_defensive_inventory/v1",
        "observed_at": "2026-10-28T04:03:55+00:00",
        "source_revision": "837dc7c",
        "tasks": [
            _task("static-task", [_annotation(), _annotation("weak", selected_ts9_repair=True)]),
            _task(
                "candidate-task",
                [_annotation(registered_exact_package_repair="not established", existing_candidate_pr=1042)],
            ),
            _task("bare-task", [_annotation("tamperable")]),
        ],
        "selected_repair_deliveries": [_delivery("static-task")],
        "completion": {"status": "delivered-static-candidates", "native_trials": 0},
    } | changes


def _board(inventory: dict[str, Any]) -> dict[str, Any]:
    return parse_inventory(json.dumps(inventory).encode())


def _status(inventory: dict[str, Any]) -> dict[str, str]:
    return {row["task_id"]: row["status"] for row in _board(inventory)["tasks"]}


def test_status_is_static_candidate_or_none_never_repaired_from_a_merged_pr() -> None:
    statuses = _status(_inventory())
    assert statuses == {
        "static-task": "static only",
        "candidate-task": "candidate",
        "bare-task": "no repair",
    }
    static = next(row for row in _board(_inventory())["tasks"] if row["task_id"] == "static-task")
    assert static["repairs"][0]["merged"] is True and static["repairs"][0]["native_receipt"] is None
    assert static["annotations"] == 2 and static["targeted_annotations"] == 1
    assert static["defect_types"] == {"weak": 1, "weak_check": 1}


def test_native_validated_needs_a_named_native_receipt() -> None:
    claim_only = _inventory(
        selected_repair_deliveries=[_delivery("static-task", native_qualification=True)]
    )
    assert _status(claim_only)["static-task"] == "static only"
    receipt_without_claim = _inventory(
        selected_repair_deliveries=[_delivery("static-task", native_receipt="runs/x/receipt.json")]
    )
    assert _status(receipt_without_claim)["static-task"] == "static only"
    proven = _inventory(
        selected_repair_deliveries=[
            _delivery("static-task", native_qualification=True, native_receipt="runs/x/receipt.json")
        ]
    )
    board = _board(proven)
    row = next(item for item in board["tasks"] if item["task_id"] == "static-task")
    assert row["status"] == "native-validated"
    assert row["repairs"][0]["native_receipt"] == "runs/x/receipt.json"


def test_only_reduced_fields_leave_the_server() -> None:
    served = json.dumps(_board(_inventory()))
    for private in (SECRET_WORDING, "pm-tb4:secret-key-id", "/private/answer-keys", "key.json"):
        assert private not in served


def test_tasks_not_strictly_dev_are_dropped_and_counted() -> None:
    inventory = _inventory()
    inventory["tasks"] += [
        _task("test-task", [_annotation()], dev_assignments=[["tb4-audit-v1", "test"]]),
        _task("mixed-task", [_annotation()], dev_assignments=[["a", "dev"], ["b", "test"]]),
        _task("unassigned-task", [_annotation()], dev_assignments=[]),
    ]
    board = _board(inventory)
    assert {row["task_id"] for row in board["tasks"]} == {"static-task", "candidate-task", "bare-task"}
    assert board["excluded_non_dev"] == 3


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "evallab.ts9_defensive_inventory/v2"},
        {"observed_at": None},
        {"tasks": [_task("t", [_annotation()]), _task("t", [_annotation()])]},
        {"tasks": [_task("t", [{"weak_check": "no defect type"}])]},
    ],
)
def test_inconsistent_inventories_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _board(_inventory(**change))


def test_a_non_github_pr_url_is_not_linked() -> None:
    inventory = _inventory(selected_repair_deliveries=[_delivery("static-task", pr_url="javascript:alert(1)")])
    row = next(item for item in _board(inventory)["tasks"] if item["task_id"] == "static-task")
    assert row["repairs"][0]["pr_url"] is None


def test_the_cache_reports_absent_unreadable_and_reloads_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "board" / "inventory.json"
    cache = ChangeCache(path, load_inventory)
    assert cache.get()["present"] is False and "no repair inventory" in cache.get()["error"]
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    assert cache.get()["error"].startswith("unreadable")
    path.write_text(json.dumps(_inventory()), encoding="utf-8")
    first = cache.get()
    assert first["present"] is True and len(first["tasks"]) == 3 and cache.get() is first
    path.write_text(json.dumps(_inventory(tasks=[_task("only", [_annotation()])])), encoding="utf-8")
    assert [row["task_id"] for row in cache.get()["tasks"]] == ["only"]
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere.json")
    assert cache.get()["present"] is False


def _identity(task_id: str, outcome: str) -> dict[str, Any]:
    return {"task_id": task_id, "outcome": outcome}


def test_counts_use_pass_and_modified_tests_only_and_return_no_run_ids() -> None:
    inventory = {"present": True, "error": None, **_board(_inventory())}
    traces = {
        "r1": _identity("static-task", "pass"),
        "r2": _identity("static-task", "pass"),
        "r3": _identity("static-task", "fail"),
        "r4": _identity("static-task", "pass"),
        "r5": _identity("elsewhere", "pass"),
    }
    facets = {
        "r1": {"outcome": "pass", "tests": "modified"},
        "r2": {"outcome": "pass", "tests": "read"},
        "r3": {"outcome": "fail", "tests": "modified"},
        "r4": {"outcome": "pass", "tests": "deleted"},
        "r5": {"outcome": "pass", "tests": "modified"},
    }
    board = build_board(inventory, frozenset({"static-task"}), facets, traces)
    by_id = {row["task_id"]: row for row in board["tasks"]}
    assert (by_id["static-task"]["atlas_runs"], by_id["static-task"]["passed_tests_modified"]) == (4, 1)
    assert by_id["bare-task"]["passed_tests_modified"] == 0 and by_id["bare-task"]["admitted"] is False
    assert board["counts"]["passed_tests_modified"] == 1 and board["counts"]["not_in_cloud_database"] == 2
    assert board["counts"]["by_status"] == {
        "native-validated": 0, "static only": 1, "candidate": 1, "no repair": 1,
    }
    assert [row["status"] for row in board["tasks"]] == ["static only", "candidate", "no repair"]
    assert board["defect_types"] == ["tamperable", "weak", "weak_check"]
    assert not any(run_id in json.dumps(board) for run_id in traces)


def test_an_absent_inventory_serves_an_empty_state_not_zero_rows() -> None:
    absent = {"present": False, "error": "no repair inventory is retained at the configured path"}
    board = build_board(absent, frozenset(), {}, {})
    assert board["present"] is False and board["tasks"] == [] and board["counts"] is None
    assert board["statuses"] == list(repair_board.STATUSES)
