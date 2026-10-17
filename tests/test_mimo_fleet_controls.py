from __future__ import annotations

import csv
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.registry import task_directory_digest

DRIVER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/mimo-v6-full-fleet/run_controls.py"
)


@pytest.fixture
def controls() -> dict[str, Any]:
    return runpy.run_path(str(DRIVER))


def test_zero_provider_refusals_preserve_history_without_exhausting_control_budget(
    controls: dict[str, Any],
) -> None:
    admissions = [
        {"state": "preflight_refused", "provider_started": False, "reservation_units": 6}
        for _ in range(6)
    ] + [{"state": "executed", "reservation_units": 6} for _ in range(6)]
    accounting = controls["reservation_accounting"](admissions)
    assert accounting == {
        "total_reservation_units": 72,
        "unstarted_preflight_units": 36,
        "units_reserved": 36,
    }


def test_paid_failures_and_uncertain_refusals_keep_their_full_reservation(
    controls: dict[str, Any],
) -> None:
    admissions = [
        {"state": "failed", "provider_started": False, "reservation_units": 6},
        {"state": "preflight_refused", "reservation_units": 6},
        {"state": "preflight_refused", "provider_started": True, "reservation_units": 6},
        {"state": "reserved", "reservation_units": 6},
    ]
    assert controls["reservation_accounting"](admissions) == {
        "total_reservation_units": 24,
        "unstarted_preflight_units": 0,
        "units_reserved": 24,
    }


def test_uncertain_refusal_is_not_automatically_retried(
    controls: dict[str, Any], tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "task.toml").write_text("[environment]\ncpus = 2\nmemory_mb = 8192\n")
    digest = task_directory_digest(package)
    task_id = "format-code-task-000001"
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["task_id", "status", "package_path", "final_digest"],
        )
        writer.writeheader()
        writer.writerow({
            "task_id": task_id, "status": "built", "package_path": "package",
            "final_digest": digest,
        })
    pending, _ = controls["pending_controls"](
        SimpleNamespace(manifest=manifest, task_id=[task_id], package_root=tmp_path),
        [{"task_id": task_id, "final_digest": digest, "agent": "oracle",
          "state": "preflight_refused", "reservation_units": 6}],
    )
    assert [(item["task_id"], item["agent"]) for item in pending] == [(task_id, "nop")]
