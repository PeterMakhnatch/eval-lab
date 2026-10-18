"""Provider-free contracts for the one-use, independently guarded BASE window."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
            "budget_state": {"path": "/unused/parent-envelope.json", "sha256": "sha256:" + "a" * 64},
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
              "controls": {"path": str(controls), "sha256": "sha256:" + controls_sha}}
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


def test_readiness_refuses_unarmed_guard_before_model_or_metadata_request(window, config, tmp_path, monkeypatch):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    control = Mock()
    monkeypatch.setattr(window, "ModalControl", control)
    with pytest.raises(ValueError, match="guard is armed"):
        window.probe(journal, binding=True, timeout=30)
    control.assert_not_called()


@pytest.mark.parametrize("unavailable", [False, True])
def test_bounded_metadata_reaches_real_sdk_transport_once(window, monkeypatch, unavailable):
    """Keep the real SDK's timeout/retry admission, replacing only the network."""
    modal = pytest.importorskip("modal")
    from grpclib import GRPCError, Status
    from modal._grpc_client import UnaryUnaryWrapper
    from modal.client import _Client
    from modal_proto import api_pb2

    calls = []

    class Transport:
        _snapshotted = False

        async def _get_channel(self, server_url):
            return object()

        async def _call_unary(self, method, request, *, timeout, metadata):
            calls.append((request.environment_name, timeout))
            if unavailable:
                raise GRPCError(Status.UNAVAILABLE, "fixture transport unavailable")
            return api_pb2.AppListResponse(apps=[{"app_id": "ap-metadata", "name": window.APP_NAME}])

    client = Transport()
    client.stub = SimpleNamespace(AppList=UnaryUnaryWrapper(
        SimpleNamespace(name="/modal.client.ModalClient/AppList"), client, "https://metadata.invalid"))

    async def from_env(cls):
        return client

    monkeypatch.setattr(_Client, "from_env", classmethod(from_env))
    control = window.ModalControl("main")
    if unavailable:
        with pytest.raises(modal.exception.ServiceError):
            control.apps()
    else:
        assert [row.app_id for row in control.apps()] == ["ap-metadata"]
    assert calls == [("main", 10)]


@pytest.fixture
def recovery(window, config, tmp_path, monkeypatch):
    """Exercise the CLI against an in-memory provider, not a fabricated receipt."""
    monkeypatch.setattr(window.time, "time", lambda: 1500)
    controls = tmp_path / "controls.json"
    controls.write_text('{"strict_tasks":19}')
    controls_sha = window.sha256(controls.read_bytes())
    monkeypatch.setattr(window, "CONTROLS_SHA256", controls_sha)
    budget = {"schema": "mimo.phase2-envelope/v1", "admitted": True,
              "reservation_id": config["reservation_id"], "ledger_tag": "2026-10-17-v7", "approved_usd": "12.00",
              "controls": {"path": str(controls), "sha256": "sha256:" + controls_sha},
              "native": {"cap_usd": "6.50"}, "heldout_regrade": {"cap_usd": "3.00"},
              "serving": {key: config[key] for key in (
                  "app_name", "gpu", "hard_cpu_limit", "hard_memory_mib_limit", "usd_per_second_upper_bound",
                  "maximum_seconds", "shutdown_reserve_seconds", "cap_usd")}}
    budget_path = tmp_path / "budget.json"
    budget_path.write_text(json.dumps(budget))
    budget_sha = window.sha256(budget_path.read_bytes())
    original = window.admitted_config(budget_path, budget_sha, config["owner"], config["stop_deadline"], now=1000)
    original["environment"] = "main"
    journal = window.Journal(tmp_path / "owner.json")
    journal.create(original)
    journal.update()  # Recovery must retain an already nonzero exposure high-water mark too.
    records = {}
    deployments = []
    metadata = {"listed": [], "previous": "", "tags": {}, "state": 5, "tasks": [], "dict_missing": False}

    class Store:
        def get(self, key):
            if metadata["dict_missing"]:
                raise exceptions.NotFoundError()
            return records.get(key)

        def put(self, key, value, *, skip_if_exists=False):
            metadata["dict_missing"] = False
            if skip_if_exists and key in records:
                return False
            records[key] = value
            return True

    store = Store()
    sdk = ModuleType("modal")
    exceptions = ModuleType("modal.exception")
    exceptions.NotFoundError = type("NotFoundError", (Exception,), {})
    sdk.Dict = SimpleNamespace(from_name=lambda *args, **kwargs: store)
    monkeypatch.setitem(sys.modules, "modal", sdk)
    monkeypatch.setitem(sys.modules, "modal.exception", exceptions)

    class Control(window.ModalControl):
        def __init__(self, environment):
            self.environment, self.store = environment, store
            self.api = SimpleNamespace(APP_STATE_STOPPED=5,
                AppGetByDeploymentNameRequest=lambda **kwargs: SimpleNamespace(**kwargs),
                AppGetLifecycleRequest=lambda **kwargs: SimpleNamespace(**kwargs),
                TaskListRequest=lambda **kwargs: SimpleNamespace(**kwargs))

        def apps(self):
            return metadata["listed"]

        def rpc(self, method, request, timeout):
            if method == "AppGetByDeploymentName":
                return SimpleNamespace(app_id="", previous_app_id=metadata["previous"])
            if method == "AppGetLifecycle":
                return SimpleNamespace(lifecycle=SimpleNamespace(app_state=metadata["state"]))
            if method == "TaskList":
                return SimpleNamespace(tasks=metadata["tasks"])
            raise AssertionError("unexpected provider operation")

        def tags(self, app_id):
            return window.ownership_tags(original) if app_id == "ap-recovered" else metadata["tags"]

    control = Control("main")
    monkeypatch.setattr(window, "ModalControl", lambda environment: control)

    def watchdog(*args, **kwargs):
        journal.update(watchdog_heartbeat_at=window.utc())
        return SimpleNamespace(pid=123, poll=lambda: None)

    def arm_guard():
        records["guard:" + original["reservation_id"]] = {
            "armed": True, "app_id": "ap-recovered", "config_sha256": window.sha256(window.encoded(original)),
            "deadline": original["expires_at"]}

    serve = SimpleNamespace(WINDOW=original, MODEL_ID=window.MODEL_ID, MODEL_REVISION="fixture-revision",
        SGLANG_IMAGE="fixture-image",
        app=SimpleNamespace(app_id="ap-recovered", deploy=lambda **kwargs: deployments.append(dict(original))),
        MimoServer=SimpleNamespace(get_url=lambda: "https://fixture.modal.run"), stop_window=SimpleNamespace(spawn=arm_guard))
    monkeypatch.setattr(window.subprocess, "Popen", watchdog)
    monkeypatch.setattr(window, "importlib", SimpleNamespace(import_module=lambda name: serve))
    for key in (window.WINDOW_ENV, "EVALLAB_MIMO_GPU", "EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS"):
        monkeypatch.setenv(key, "")
    args = SimpleNamespace(budget_state=budget_path, budget_state_sha256=budget_sha, owner=original["owner"],
        deadline=original["stop_deadline"], environment="main", state=journal.path,
        recover_predeployment_sha256=window.sha256(journal.path.read_bytes()))
    argv = ["deploy", "--budget-state", str(budget_path), "--budget-state-sha256", budget_sha,
            "--owner", original["owner"], "--deadline", original["stop_deadline"], "--environment", "main",
            "--state", str(journal.path), "--recover-predeployment-sha256", args.recover_predeployment_sha256]
    return SimpleNamespace(journal=journal, config=original, args=args, argv=argv, control=control,
                           records=records, deployments=deployments, metadata=metadata)


@pytest.mark.parametrize("missing_dict", [False, True])
def test_cli_recovers_only_original_unused_window_without_refund_or_deadline_extension(
    window, recovery, capsys, missing_dict,
):
    before = recovery.journal.read()
    recovery.metadata["previous"] = "ap-old-foreign-stopped"
    recovery.metadata["dict_missing"] = missing_dict
    window.main(recovery.argv)
    result = json.loads(capsys.readouterr().out)
    after = recovery.journal.read()
    assert result["state"] == "armed_not_ready" and result["app_id"] == "ap-recovered"
    assert len(recovery.deployments) == 1
    for key in ("config", "config_sha256", "reserved_usd"):
        assert after[key] == before[key]
    assert Decimal(after["wall_exposure_upper_bound_usd"]) >= Decimal(before["wall_exposure_upper_bound_usd"]) > 0
    assert result["starts_at"] == before["config"]["starts_at"]
    assert result["stop_deadline"] == before["config"]["stop_deadline"]
    assert after["predeployment_recovery"]["remote_reservation_absent"] is True
    with pytest.raises(SystemExit):
        window.main(recovery.argv)
    assert len(recovery.deployments) == 1


@pytest.mark.parametrize("changes", [
    {"state": "active"}, {"state": "stopped"}, {"state": "deployed_not_ready"},
    {"app_id": "ap-prior"}, {"deployment": {"path": "prior"}},
    {"stop_guard": {"armed": True}}, {"watchdog_spawned_pid": 123}, {"stopped_at": "fixture"},
    {"predeployment_recovery": {"checked_at": "fixture"}},
])
def test_recovery_rejects_any_local_deployment_or_prior_attempt_evidence(window, recovery, changes):
    recovery.journal.update(**changes)
    recovery.args.recover_predeployment_sha256 = window.sha256(recovery.journal.path.read_bytes())
    raw = recovery.journal.path.read_bytes()
    with pytest.raises(ValueError, match="only an unused"):
        window.deploy(recovery.args)
    assert recovery.journal.path.read_bytes() == raw
    assert recovery.records == {} and recovery.deployments == []


@pytest.mark.parametrize("conflict", ["reservation", "guard", "previous-owned", "previous-live", "previous-containers",
                                     "listed-live"])
def test_recovery_requires_absent_remote_authority_and_no_previous_owned_or_live_app(window, recovery, conflict):
    if conflict in {"reservation", "guard"}:
        recovery.records[conflict + ":" + recovery.config["reservation_id"]] = "already-consumed"
    elif conflict == "listed-live":
        recovery.metadata["listed"] = [SimpleNamespace(
            name=window.APP_NAME + "-lora", description="", state=3, n_running_tasks=1)]
    else:
        recovery.metadata["previous"] = "ap-prior"
        if conflict == "previous-owned":
            recovery.metadata["tags"] = window.ownership_tags(recovery.config)
        elif conflict == "previous-live":
            recovery.metadata["state"] = 3
        else:
            recovery.metadata["tasks"] = [object()]
    raw = recovery.journal.path.read_bytes()
    records = dict(recovery.records)
    with pytest.raises(ValueError):
        window.deploy(recovery.args)
    assert recovery.journal.path.read_bytes() == raw
    assert recovery.records == records and recovery.deployments == []


@pytest.mark.parametrize("conflict", ["digest", "deadline", "owner", "environment", "expiry"])
def test_recovery_refuses_changed_authority_or_original_expiry(window, recovery, monkeypatch, conflict):
    if conflict == "digest":
        recovery.args.recover_predeployment_sha256 = "f" * 64
    elif conflict == "deadline":
        recovery.args.deadline = window.utc(4600)
    elif conflict == "expiry":
        monkeypatch.setattr(window.time, "time", lambda: window.instant(recovery.config["expires_at"]))
    else:
        setattr(recovery.args, conflict, "changed")
    raw = recovery.journal.path.read_bytes()
    with pytest.raises(ValueError):
        window.deploy(recovery.args)
    assert recovery.journal.path.read_bytes() == raw
    assert recovery.records == {} and recovery.deployments == []


def test_recovery_claim_refuses_intervening_journal_mutation_before_remote_consumption(window, recovery, monkeypatch):
    proof = recovery.control.predeployment_proof

    def concurrent_owner(config):
        result = proof(config)
        recovery.journal.update(state="stopped")
        return result

    monkeypatch.setattr(recovery.control, "predeployment_proof", concurrent_owner)
    with pytest.raises(ValueError, match="journal changed"):
        window.deploy(recovery.args)
    assert recovery.journal.read()["state"] == "stopped"
    assert recovery.records == {} and recovery.deployments == []


def test_remote_claim_race_never_replaces_consumed_slot_or_permits_another_recovery(window, recovery, monkeypatch):
    proof = recovery.control.predeployment_proof
    key = "reservation:" + recovery.config["reservation_id"]

    def concurrent_reservation(config):
        result = proof(config)
        recovery.records[key] = "concurrent-owner"
        return result

    monkeypatch.setattr(recovery.control, "predeployment_proof", concurrent_reservation)
    with pytest.raises(ValueError, match="already been consumed"):
        window.deploy(recovery.args)
    assert recovery.records == {key: "concurrent-owner"} and recovery.deployments == []
    recovery.args.recover_predeployment_sha256 = window.sha256(recovery.journal.path.read_bytes())
    with pytest.raises(ValueError, match="only an unused"):
        window.deploy(recovery.args)
    assert recovery.records == {key: "concurrent-owner"} and recovery.deployments == []
