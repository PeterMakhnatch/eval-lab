"""Provider-free contracts for the one-use, independently guarded BASE window."""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def window():
    path = Path(__file__).resolve().parents[1] / "tools/modal-mimo-serve/serving_window.py"
    spec = importlib.util.spec_from_file_location("owned_serving_window", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(window):
    return {"schema": "mimo.serving-window-config/v1", "app_name": window.APP_NAME,
            "gpu": "L40S", "hard_cpu_limit": 4, "hard_memory_mib_limit": 32768,
            "usd_per_second_upper_bound": "0.0006676325", "maximum_seconds": 3500,
            "shutdown_reserve_seconds": 120, "cap_usd": "2.50", "reservation_id": "fixture-reservation",
            "window_id": "fixture-reservation", "owner": "fixture-owner", "environment": "main",
            "budget_state": {"path": "/unused/parent-envelope.json", "sha256": "a" * 64},
            "starts_at": window.utc(1000), "expires_at": window.utc(4380), "stop_deadline": window.utc(4500)}


def test_quote_counts_both_hard_limited_containers(window):
    assert window.resource_rate("L40S", 4, 32768) == Decimal("0.0006676325")
    assert window.resource_rate("A100-80GB", 4, 32768) == Decimal("0.0008196325")
    with pytest.raises(ValueError, match="supported"):
        window.resource_rate("A10", 4, 32768)


def test_twelve_dollar_parent_total_includes_heldout_and_other_declared_caps(window, config, tmp_path, monkeypatch):
    controls = tmp_path / "controls.json"
    controls.write_text('{"strict_tasks":19}')
    controls_sha = window.sha256(controls.read_bytes())
    monkeypatch.setattr(window, "CONTROLS_SHA256", controls_sha)
    serving = {key: config[key] for key in ("app_name", "gpu", "hard_cpu_limit", "hard_memory_mib_limit",
        "usd_per_second_upper_bound", "maximum_seconds", "shutdown_reserve_seconds", "cap_usd")}
    budget = {"schema": "mimo.phase2-envelope/v1", "reservation_id": "fixture-reservation", "admitted": True,
              "ledger_tag": "2026-10-17-v7", "approved_usd": "12.00", "serving": serving,
              "native": {"cap_usd": "6.50"}, "heldout_regrade": {"cap_usd": "3.00"},
              "controls": {"path": str(controls), "sha256": controls_sha}}
    path = tmp_path / "budget.json"

    def admit():
        path.write_text(json.dumps(budget))
        return window.admitted_config(path, window.sha256(path.read_bytes()), "fixture-owner",
                                      config["stop_deadline"], now=1000)

    assert admit()["cap_usd"] == "2.50"
    budget["extra_reservation"] = {"cap_usd": "0.01"}
    with pytest.raises(ValueError, match="all declared"):
        admit()
    del budget["extra_reservation"]
    budget["admitted"] = False
    with pytest.raises(ValueError, match="explicitly admitted"):
        admit()


def test_rate_and_shutdown_reserve_cannot_understate_exposure(window, config):
    window.validate_config(config)
    config["usd_per_second_upper_bound"] = "0.000542"
    with pytest.raises(ValueError, match="understates"):
        window.validate_config(config)
    config["usd_per_second_upper_bound"] = "0.0006676325"
    config["shutdown_reserve_seconds"] = 28
    with pytest.raises(ValueError, match="at least 120s"):
        window.validate_config(config)


def test_journal_is_exclusive_and_never_refunds_or_restarts_accounting(window, config, tmp_path, monkeypatch):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    with pytest.raises(FileExistsError):
        journal.create(config)
    monkeypatch.setattr(window.time, "time", lambda: 2000)
    first = journal.update()
    monkeypatch.setattr(window.time, "time", lambda: 1500)
    second = journal.update()
    assert second["wall_exposure_upper_bound_usd"] == first["wall_exposure_upper_bound_usd"]
    assert second["reserved_usd"] == "2.50"
    with pytest.raises(ValueError, match="immutable"):
        journal.update(reserved_usd="0")


def test_stop_refuses_foreign_owner_before_any_mutation(window, config):
    control = object.__new__(window.ModalControl)
    control.tags = Mock(return_value={"evallab_window_id": "some-other-window"})
    control.rpc = Mock()
    with pytest.raises(ValueError, match="ownership"):
        control.stop(config, "ap-unowned")
    control.rpc.assert_not_called()


def test_stopped_app_is_not_settled_until_zero_active_containers(window, config, tmp_path, monkeypatch):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    observations = iter([
        {"stopped": True, "active_containers": 1, "app_id": "ap-owned"},
        {"stopped": True, "active_containers": 0, "app_id": "ap-owned"},
    ])
    control = SimpleNamespace(owned_app=lambda *args: "ap-owned", observation=lambda *args: next(observations))
    monkeypatch.setattr(window, "ModalControl", lambda *args: control)
    monkeypatch.setattr(window.time, "time", lambda: 1500)
    assert not window.watch(journal, once=True).get("stopped_at")
    stopped = window.watch(journal, once=True)
    assert stopped["state"] == "stopped" and stopped["stopped_at"]
    assert stopped["reserved_usd"] == "2.50"


def test_active_receipt_cannot_outlive_absolute_deadline(window, config, tmp_path, monkeypatch):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    value = journal.update(state="active")
    monkeypatch.setattr(window.time, "time", lambda: 4381)
    assert window.receipt(value)["state"] == "expired"
    assert window.receipt(value)["budget"]["envelope"] == config["budget_state"]


def test_readiness_refuses_unarmed_guard_before_model_or_metadata_request(window, config, tmp_path, monkeypatch):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    control = Mock()
    monkeypatch.setattr(window, "ModalControl", control)
    with pytest.raises(ValueError, match="guard is armed"):
        window.probe(journal, binding=True, timeout=30)
    control.assert_not_called()
