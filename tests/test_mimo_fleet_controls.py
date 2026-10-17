from __future__ import annotations

import csv
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate, SpendCapReached

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


@pytest.mark.parametrize("custom_rate", [None, 0.003456789])
@pytest.mark.parametrize("cap_delta", [0.0, -2e-8])
def test_plan_cap_matches_native_reservations_without_rounding_or_provider_work(
    controls: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    custom_rate: float | None,
    cap_delta: float,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "task.toml").write_text(
        "[environment]\ncpus = 2\nmemory_mb = 8192\n"
        "[verifier]\nenvironment_mode = 'separate'\n"
        "[verifier.environment]\ncpus = 2\nmemory_mb = 8192\n"
    )
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["task_id", "status", "package_path", "final_digest"]
        )
        writer.writeheader()
        writer.writerow({
            "task_id": "format-code-task-000001",
            "status": "built",
            "package_path": "package",
            "final_digest": task_directory_digest(package),
        })
    rate = controls["MODAL_USD_PER_ALLOCATION_UNIT"] if custom_rate is None else custom_rate
    units = 12  # Two controls × two environments × preparation/two creation attempts.
    cap = units * rate + cap_delta
    arguments = [
        str(DRIVER), "--repo-root", str(tmp_path), "--package-root", str(tmp_path),
        "--manifest", str(manifest), "--jobs-dir", str(tmp_path / "jobs"),
        "--max-usd", str(cap),
    ]
    if custom_rate is not None:
        arguments += ["--usd-per-unit", str(custom_rate)]
    monkeypatch.setattr(sys, "argv", arguments)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a provider-free plan created an execution gate")

    main = controls["main"]
    monkeypatch.setitem(main.__globals__, "CloudSpendGate", forbidden)
    assert main() == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["pending_executions"] == 2
    assert plan["pending_reservation_units"] == units
    assert plan["proposed_total_reserved_usd"] == units * rate
    assert plan["fits_cap"] is (cap_delta == 0)
    assert not (tmp_path / controls["RELEASE"] / "native-admissions.json").exists()
    assert not (tmp_path / "jobs").exists()
    gate = CloudSpendGate("inert exact-cap regression", cap, rate)
    for _ in range(units if cap_delta == 0 else units - 1):
        gate.charge()
    with pytest.raises(SpendCapReached):
        gate.charge()


def test_calibration_cannot_reprice_a_bound_historical_ledger(
    controls: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / controls["RELEASE"] / "native-admissions.json"
    ledger.parent.mkdir(parents=True)
    original = json.dumps({
        "schema": "mimo-v6-native-admissions/2",
        "cap_usd": 4.0,
        "usd_per_unit": 0.05,
        "units_reserved": 6,
        "admissions": [{"state": "failed", "reservation_units": 6}],
    }).encode()
    ledger.write_bytes(original)
    monkeypatch.setattr(sys, "argv", [
        str(DRIVER), "--repo-root", str(tmp_path), "--package-root", str(tmp_path),
        "--jobs-dir", str(tmp_path / "jobs"),
    ])
    with pytest.raises(ValueError, match="existing lane cap and reservation unit"):
        controls["main"]()
    assert ledger.read_bytes() == original
    assert not (tmp_path / "jobs").exists()
