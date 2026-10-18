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


def test_real_sdk_accepts_owned_tags_and_rejects_a_changed_digest_suffix(window, config):
    modal = pytest.importorskip("modal")
    tags = window.ownership_tags(config)
    modal.App(window.APP_NAME, tags=tags)
    control = object.__new__(window.ModalControl)
    control.tags = lambda app_id: tags
    control.rpc = Mock()
    changed = {**config, "budget_state": {**config["budget_state"], "sha256": "sha256:" + "a" * 63 + "b"}}
    with pytest.raises(ValueError, match="ownership"):
        control.stop(changed, "ap-owned")
    control.rpc.assert_not_called()


@pytest.fixture
def claimed_startup(window, config, tmp_path, monkeypatch):
    controls = tmp_path / "controls.json"
    controls.write_text('{"strict_tasks":19}')
    controls_sha = window.sha256(controls.read_bytes())
    monkeypatch.setattr(window, "CONTROLS_SHA256", controls_sha)
    serving = {key: config[key] for key in ("app_name", "gpu", "hard_cpu_limit", "hard_memory_mib_limit",
        "usd_per_second_upper_bound", "maximum_seconds", "shutdown_reserve_seconds", "cap_usd")}
    budget = {"schema": "mimo.phase2-envelope/v1", "reservation_id": config["reservation_id"], "admitted": True,
              "ledger_tag": "2026-10-17-v7", "approved_usd": "12.00", "serving": serving,
              "native": {"cap_usd": "6.50"}, "heldout_regrade": {"cap_usd": "3.00"},
              "controls": {"path": str(controls), "sha256": "sha256:" + controls_sha}}
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(budget))
    config["budget_state"] = {"path": str(path), "sha256": "sha256:" + window.sha256(path.read_bytes())}
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    claim = window.sha256(window.encoded(config))
    store = {"reservation:" + config["reservation_id"]: claim}
    provider = {"app_id": "", "previous_app_id": "", "tags": {}, "state": 5, "tasks": []}

    def rpc(method, request, timeout):
        if method == "AppGetByDeploymentName":
            return SimpleNamespace(app_id=provider["app_id"], previous_app_id=provider["previous_app_id"])
        if method == "AppGetLifecycle":
            return SimpleNamespace(lifecycle=SimpleNamespace(app_state=provider["state"]))
        if method == "TaskList":
            return SimpleNamespace(tasks=provider["tasks"])
        raise AssertionError(method)

    control = SimpleNamespace(store=SimpleNamespace(get=store.get), apps=lambda: [], rpc=rpc,
        tags=lambda app_id: provider["tags"], api=SimpleNamespace(APP_STATE_STOPPED=5,
            AppGetByDeploymentNameRequest=lambda **kwargs: SimpleNamespace(**kwargs),
            AppGetLifecycleRequest=lambda **kwargs: SimpleNamespace(**kwargs),
            TaskListRequest=lambda **kwargs: SimpleNamespace(**kwargs)))
    monkeypatch.setattr(window, "ModalControl", lambda environment: control)
    monkeypatch.setattr(window.time, "time", lambda: 2000)
    args = SimpleNamespace(state=journal.path, journal_sha256=window.sha256(journal.path.read_bytes()))
    return journal, args, store, provider


def test_interrupted_startup_cannot_create_a_second_app_or_extend_its_claim(window, claimed_startup, monkeypatch):
    journal, args, store, provider = claimed_startup
    before = journal.read()
    attempts = []

    def interrupted(journal, config, control):
        attempts.append(1)
        raise RuntimeError("external deployment interrupted")

    monkeypatch.setattr(window, "_deploy_reserved", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        window.complete_startup(args)
    args.journal_sha256 = window.sha256(journal.path.read_bytes())
    with pytest.raises(ValueError, match="unstarted"):
        window.complete_startup(args)
    after = journal.read()
    assert attempts == [1]
    assert after["state"] == "startup_completion_claimed"
    assert after["config"] == before["config"] and after["reserved_usd"] == before["reserved_usd"]
    assert store == {"reservation:" + before["config"]["reservation_id"]: before["config_sha256"]}


@pytest.mark.parametrize("unsafe", ["expired", "claim_mismatch", "armed_guard", "owned_previous_app", "foreign_live_app",
                                  "deployment_invoked", "journal_changed"])
def test_startup_refuses_unsafe_claims_before_any_deployment(window, claimed_startup, monkeypatch, unsafe):
    journal, args, store, provider = claimed_startup
    config = journal.read()["config"]
    if unsafe == "expired":
        monkeypatch.setattr(window.time, "time", lambda: 4381)
    elif unsafe == "claim_mismatch":
        store["reservation:" + config["reservation_id"]] = "different-config"
    elif unsafe == "armed_guard":
        store["guard:" + config["reservation_id"]] = {"armed": True}
    elif unsafe == "owned_previous_app":
        provider.update(previous_app_id="ap-previous", tags={"evallab_window_id": config["window_id"]})
    elif unsafe == "foreign_live_app":
        provider.update(app_id="ap-foreign", state=4, tasks=[SimpleNamespace(task_id="task-live")])
    elif unsafe == "deployment_invoked":
        journal.update(deployment_invoked_at=window.utc(1500))
        args.journal_sha256 = window.sha256(journal.path.read_bytes())
    elif unsafe == "journal_changed":
        journal.update(watchdog_heartbeat_at=window.utc(1500))
    deployment = Mock()
    monkeypatch.setattr(window, "_deploy_reserved", deployment)
    with pytest.raises(ValueError):
        window.complete_startup(args)
    deployment.assert_not_called()
    assert journal.read()["reserved_usd"] == "2.50"


def test_cli_retains_modal_validation_reason_without_credentials(window, monkeypatch, capsys):
    modal = pytest.importorskip("modal")
    secret = "fixture-private-value"
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", secret)
    message = (
        "scaledown_window must be between 2 and 3600 (inclusive); "
        f"credential {secret}; Bearer fixture-bearer; "
        """api_key="fixture-quoted"; MODAL_TOKEN_SECRET='fixture-token'"""
    )

    def invalid_rate(*args):
        raise modal.exception.InvalidError(message)

    monkeypatch.setattr(window, "resource_rate", invalid_rate)
    monkeypatch.setattr("sys.argv", ["serving_window.py", "quote", "--gpu", "A100-80GB",
        "--cpu-limit", "4", "--memory-mib-limit", "32768", "--cap-usd", "4.50",
        "--shutdown-reserve-seconds", "120"])
    with pytest.raises(SystemExit) as stopped:
        window.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1
    assert output.out == ""
    assert "InvalidError: scaledown_window must be between 2 and 3600 (inclusive)" in output.err
    for value in (secret, "fixture-bearer", "fixture-quoted", "fixture-token"):
        assert value not in output.err


def test_cli_does_not_disclose_arbitrary_sdk_or_http_exception_bodies(window, monkeypatch, capsys):
    def failed_rate(*args):
        raise RuntimeError("response body contains private fixture-value")

    monkeypatch.setattr(window, "resource_rate", failed_rate)
    monkeypatch.setattr("sys.argv", ["serving_window.py", "quote", "--gpu", "A100-80GB",
        "--cpu-limit", "4", "--memory-mib-limit", "32768", "--cap-usd", "4.50",
        "--shutdown-reserve-seconds", "120"])
    with pytest.raises(SystemExit) as stopped:
        window.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1
    assert output.out == ""
    assert output.err == "serving-window failed: RuntimeError\n"
