"""Grader repair board: one row per keyed DEV task, with how far each repair has been proven.

Reads the TS-9 defensive inventory (``evallab.ts9_defensive_inventory/v1``) from a configurable
path and joins counts from the Run Atlas. Read-only. Only these fields leave the server: task
and dataset ids, defect-type names, annotation counts, repair PR numbers, validation status
and run counts. Annotation text, weak-check wording, key ids, answer-key paths and digests
stay in the inventory.

A task is ``native-validated`` only when a repair names a native receipt. Everything else is
``static only`` (a delivered candidate with host checks, no sandbox run), ``candidate`` (a
related candidate that is not bound to the exact package) or ``no repair``. "Repaired" is never
inferred from a merged PR.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evallab.production.atlas_view import json_reason, read_regular, strict_loads

SCHEMA = "evallab.repair_board/v1"
INVENTORY_SCHEMA = "evallab.ts9_defensive_inventory/v1"
DEFAULT_FILE = "repair-board/inventory.json"
MAX_BYTES = 4 * 1024 * 1024
NATIVE = "native-validated"
STATIC = "static only"
CANDIDATE = "candidate"
NONE = "no repair"
STATUSES = (NATIVE, STATIC, CANDIDATE, NONE)
STATUS_NOTE = {
    NATIVE: "A native sandbox receipt is named for this repair.",
    STATIC: "A repair candidate was delivered and passed host checks. No native proof: no sandbox, oracle or historical shortcut was run.",
    CANDIDATE: "A related candidate exists but is not bound to this exact package. No native proof.",
    NONE: "No repair is recorded for this task.",
}
_Id = Annotated[str, Field(min_length=1, max_length=300)]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Annotation(_Loose):
    defect_type: _Id
    existing_candidate_pr: int | None = None
    registered_exact_package_repair: str | None = None
    selected_ts9_repair: bool = False


class _Task(_Loose):
    task_id: _Id
    dataset_id: _Id
    dev_assignments: list[list[str]]
    annotations: list[_Annotation]


class _Delivery(_Loose):
    task: _Id
    pr: int
    pr_url: str
    delivery_status: str
    verification_class: str
    native_qualification: bool = False
    native_receipt: Annotated[str, Field(max_length=300)] | None = None


class _Completion(_Loose):
    status: str = ""
    native_trials: int = 0
    native_qualified_repairs: int = 0


class _Inventory(_Loose):
    schema_: str = Field(alias="schema")
    observed_at: str
    source_revision: str
    tasks: list[_Task]
    selected_repair_deliveries: list[_Delivery] = []
    completion: _Completion = _Completion()


def _is_dev(task: _Task) -> bool:
    """Strictly DEV: at least one assignment and every assignment is ``dev``."""
    pairs = task.dev_assignments
    return bool(pairs) and all(len(pair) == 2 and pair[1] == "dev" for pair in pairs)


def _row(task: _Task, deliveries: list[_Delivery]) -> dict[str, Any]:
    related = sorted({a.existing_candidate_pr for a in task.annotations if a.existing_candidate_pr})
    repairs: list[dict[str, Any]] = []
    for delivery in deliveries:
        native = bool(delivery.native_qualification and delivery.native_receipt)
        repairs.append(
            {
                "kind": "ts9-candidate",
                "pr": delivery.pr,
                "pr_url": delivery.pr_url if delivery.pr_url.startswith("https://github.com/") else None,
                "merged": delivery.delivery_status == "MERGED",
                "verification": delivery.verification_class,
                "native_receipt": delivery.native_receipt if native else None,
            }
        )
    for pr in related:
        repairs.append(
            {"kind": "related-candidate", "pr": pr, "pr_url": None, "merged": None,
             "verification": "static only; empty native evidence", "native_receipt": None}
        )
    if any(item["native_receipt"] for item in repairs):
        status = NATIVE
    elif deliveries:
        status = STATIC
    elif related:
        status = CANDIDATE
    else:
        status = NONE
    states = {a.registered_exact_package_repair for a in task.annotations if a.registered_exact_package_repair}
    return {
        "task_id": task.task_id,
        "dataset_id": task.dataset_id,
        "annotations": len(task.annotations),
        "defect_types": dict(sorted(Counter(a.defect_type for a in task.annotations).items())),
        "targeted_annotations": sum(a.selected_ts9_repair for a in task.annotations),
        "main_repair": "; ".join(sorted(states)) or "not recorded",
        "repairs": repairs,
        "status": status,
        "status_note": STATUS_NOTE[status],
    }


def parse_inventory(raw: bytes) -> dict[str, Any]:
    """The reduced, DEV-only board from inventory bytes (counts and identity only)."""
    inventory = _Inventory.model_validate(strict_loads(raw.decode("utf-8")))
    if inventory.schema_ != INVENTORY_SCHEMA:
        raise ValueError(f"schema must be {INVENTORY_SCHEMA}")
    by_task: dict[str, list[_Delivery]] = {}
    for delivery in inventory.selected_repair_deliveries:
        by_task.setdefault(delivery.task, []).append(delivery)
    dev = [task for task in inventory.tasks if _is_dev(task)]
    seen: set[str] = set()
    rows = []
    for task in dev:
        if task.task_id in seen:
            raise ValueError(f"task {task.task_id!r} appears twice")
        seen.add(task.task_id)
        rows.append(_row(task, by_task.get(task.task_id, [])))
    return {
        "observed_at": inventory.observed_at,
        "source_revision": inventory.source_revision,
        "completion_status": inventory.completion.status,
        "native_trials": inventory.completion.native_trials,
        "excluded_non_dev": len(inventory.tasks) - len(dev),
        "tasks": rows,
    }


class RepairInventoryCache:
    """Re-reads the inventory only when the file changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._loaded = False
        self._value: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        try:
            seen = self.path.lstat()
            key: tuple[int, int, int] | None = (seen.st_mtime_ns, seen.st_size, seen.st_ino)
        except OSError:
            key = None
        with self._lock:
            if not self._loaded or key != self._key:
                self._key, self._loaded = key, True
                self._value = self._load()
            return self._value

    def _load(self) -> dict[str, Any]:
        try:
            raw = read_regular(self.path.parent, self.path.name, MAX_BYTES)
            if raw is None:
                return {"present": False, "error": "no repair inventory is retained at the configured path"}
            return {"present": True, "error": None, **parse_inventory(raw)}
        except (OSError, ValueError, ValidationError, UnicodeDecodeError) as error:
            return {"present": False, "error": f"unreadable: {json_reason(error)}"}


def build_board(
    inventory: Mapping[str, Any],
    admitted: frozenset[str],
    facets: Mapping[str, Mapping[str, Any]],
    traces: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The served board: inventory rows plus per-task atlas counts.

    ``facets`` are the visible atlas facets and ``traces`` the admitted identities; a run counts
    as "passed with tests modified" when its database outcome is ``pass`` and its facet says the
    tests were modified. Counts only: no run id is returned.
    """
    base: dict[str, Any] = {
        "schema": SCHEMA, "present": inventory["present"], "error": inventory["error"],
        "statuses": list(STATUSES),
    }
    if not inventory["present"]:
        return base | {"tasks": [], "defect_types": [], "counts": None, "inventory": None}
    wanted = {row["task_id"] for row in inventory["tasks"]}
    faceted: Counter[str] = Counter()
    modified_pass: Counter[str] = Counter()
    for run_id, facet in facets.items():
        task_id = traces[run_id]["task_id"]
        if task_id not in wanted:
            continue
        faceted[task_id] += 1
        modified_pass[task_id] += facet["outcome"] == "pass" and facet["tests"] == "modified"
    rows = [
        row | {
            "admitted": row["task_id"] in admitted,
            "atlas_runs": faceted[row["task_id"]],
            "passed_tests_modified": modified_pass[row["task_id"]],
        }
        for row in inventory["tasks"]
    ]
    rows.sort(key=lambda row: (STATUSES.index(row["status"]), -row["passed_tests_modified"], row["task_id"]))
    return base | {
        "tasks": rows,
        "defect_types": sorted({kind for row in rows for kind in row["defect_types"]}),
        "counts": {
            "tasks": len(rows),
            "annotations": sum(row["annotations"] for row in rows),
            "by_status": {status: sum(row["status"] == status for row in rows) for status in STATUSES},
            "not_in_cloud_database": sum(not row["admitted"] for row in rows),
            "passed_tests_modified": sum(row["passed_tests_modified"] for row in rows),
            "excluded_non_dev": inventory["excluded_non_dev"],
        },
        "inventory": {
            key: inventory[key]
            for key in ("observed_at", "source_revision", "completion_status", "native_trials")
        },
        "notice": (
            "Read-only. A merged repair PR is not proof the grader is fixed: only a row marked "
            "native-validated names a native receipt. Passed with tests modified counts atlas "
            "runs whose outcome is pass and whose facet says the tests were modified; it is a "
            "lead for review, not a count of wrong passes."
        ),
    }
