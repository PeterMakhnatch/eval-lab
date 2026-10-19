"""Provider-free contracts for the one-use, independently guarded BASE window."""

from __future__ import annotations

import importlib.util
import json
import sys
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


@pytest.mark.parametrize("upstream,accepted", [
    ("https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct", True),
    ("https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.modal.run", True),
    ("https://other-owner--app-server.us-west.modal.direct", True),
    ("https://abc123.ap-southeast-2.modal.direct", True),
    ("http://x.us-east.modal.direct", False),
    ("https://modal.direct", False),
    ("https://modal.run", False),
    ("https://x.modal.direct", False),
    ("https://xmodal.direct", False),
    ("https://x.notmodal.direct", False),
    ("https://x.evil.modal.direct", False),
    ("https://x.us-central.modal.direct", False),
    ("https://a.b.us-east.modal.direct", False),
    ("https://a.b.modal.run", False),
    ("https://x.modal.direct.evil.example", False),
    ("https://x.modal.run.evil.example", False),
    ("https://x.us-east.modal.directx", False),
    ("https://user:pw@x.us-east.modal.direct", False),
    ("https://x.us-east.modal.direct:8443", False),
    ("https://x.us-east.modal.direct/v1", False),
    ("https://x.us-east.modal.direct/?q=1", False),
    ("https://x.us-east.modal.direct#frag", False),
    ("https://other.example/redirect?to=x.modal.direct", False),
])
def test_upstream_origin_boundary(window, upstream, accepted):
    if accepted:
        window.validate_upstream(upstream)
    else:
        with pytest.raises(ValueError, match="Modal origin"):
            window.validate_upstream(upstream)


def test_locked_state_precondition_blocks_lost_race(window, config, tmp_path):
    journal = window.Journal(tmp_path / "window.json")
    journal.create(config)
    with pytest.raises(ValueError, match="state"):
        journal.update(expected_state="stopped", upstream="https://a.modal.run")
    assert journal.read()["upstream"] is None


@pytest.fixture
def adoptable(window, config, tmp_path, monkeypatch):
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
    # The original deploy consumed its remote claim and invoked the deployment,
    # then crashed before recording it; the live watchdog still heartbeats.
    journal.update(deployment_invoked_at=window.utc(1400), watchdog_heartbeat_at=window.utc(1500))
    claim = window.sha256(window.encoded(config))
    app_id = "ap-existing"
    store = {
        "reservation:" + config["reservation_id"]: claim,
        "guard:" + config["reservation_id"]: {
            "armed": True, "app_id": app_id, "deadline": config["expires_at"],
            "config_sha256": claim, "armed_at": window.utc(1400),
            "mechanism": "same-app scheduled guard plus detached local watchdog and container absolute deadline",
        },
    }
    provider = {
        "app_id": app_id, "previous_app_id": "", "state": 3,
        "tasks": [SimpleNamespace(task_id="ta-1", started_at=1400.0, enqueued_at=1390.0)],
        "url": "https://owner--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct",
        "apps": [SimpleNamespace(app_id=app_id, name="", description=window.APP_NAME)],
        "tags": {app_id: window.ownership_tags(config)},
    }
    calls = []

    def rpc(method, request, timeout):
        calls.append(method)
        if method == "AppGetByDeploymentName":
            return SimpleNamespace(app_id=provider["app_id"], previous_app_id=provider["previous_app_id"])
        if method == "AppGetTags":
            return SimpleNamespace(tags=provider["tags"].get(request.app_id, {}))
        if method == "AppGetLifecycle":
            return SimpleNamespace(lifecycle=SimpleNamespace(app_state=provider["state"]))
        if method == "TaskList":
            return SimpleNamespace(tasks=provider["tasks"])
        raise AssertionError(method)

    control = object.__new__(window.ModalControl)
    control.api = SimpleNamespace(
        APP_STATE_STOPPED=5,
        AppGetByDeploymentNameRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        AppGetTagsRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        AppGetLifecycleRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        TaskListRequest=lambda **kwargs: SimpleNamespace(**kwargs))
    control.rpc = rpc
    control.store = SimpleNamespace(get=store.get, put=Mock())
    control.apps = lambda: provider["apps"]
    control.environment = "main"
    monkeypatch.setattr(window, "ModalControl", lambda environment: control)
    monkeypatch.setattr(window, "deployed_server_url", lambda environment: provider["url"])
    monkeypatch.setattr(window.time, "time", lambda: 2000)
    args = SimpleNamespace(state=journal.path, config_sha256=claim,
                           deployed_source_revision="e698e6cb7132274ce2e30960963a717e3812f87b")
    return journal, args, store, provider, control, calls


def load_serve(tmp_path, monkeypatch, env):
    """Import ``serve.py`` with a recorded fake ``modal`` module; provider-free."""
    calls = {}

    def decorator(**kwargs):
        def apply(fn):
            calls[fn.__name__] = kwargs
            return fn
        return apply

    image = Mock()
    image.entrypoint.return_value = image
    image.env.return_value = image
    image.add_local_python_source.return_value = image
    app = SimpleNamespace(function=decorator, server=decorator, app_id="ap-fixture")
    modal = Mock()
    modal.Image.from_registry.return_value = image
    modal.App.side_effect = lambda *args, **kwargs: app
    modal.Period.side_effect = lambda **kwargs: dict(kwargs)
    modal.enter.side_effect = lambda **kwargs: (lambda fn: fn)
    modal.exit.side_effect = lambda **kwargs: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "modal", modal)
    for key in ("EVALLAB_MIMO_GPU", "EVALLAB_MIMO_MAX_RUNNING_REQUESTS",
                "EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS", "EVALLAB_MIMO_SERVING_WINDOW"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).resolve().parents[1] / "tools/modal-mimo-serve/serve.py"
    spec = importlib.util.spec_from_file_location("serve_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def owned_window_config(window, *, start: float, maximum_seconds: int = 3500, reserve: int = 120):
    return {"schema": "mimo.serving-window-config/v1", "app_name": window.APP_NAME,
            "gpu": "A100-80GB", "hard_cpu_limit": 4, "hard_memory_mib_limit": 32768,
            "usd_per_second_upper_bound": "0.0008196325", "maximum_seconds": maximum_seconds,
            "shutdown_reserve_seconds": reserve, "cap_usd": "3.00",
            "reservation_id": "fixture-window3", "window_id": "fixture-window3",
            "owner": "fixture-owner", "environment": "main",
            "budget_state": {"path": "/unused/parent-envelope.json", "sha256": "sha256:" + "a" * 64},
            "starts_at": window.utc(start), "expires_at": window.utc(start + maximum_seconds - reserve),
            "stop_deadline": window.utc(start + maximum_seconds)}


def test_owned_window_holds_one_warm_container_for_the_bounded_window(window, tmp_path, monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(window.time, "time", lambda: now)
    config = owned_window_config(window, start=now + 60)
    _, calls = load_serve(tmp_path, monkeypatch, {"EVALLAB_MIMO_SERVING_WINDOW": json.dumps(config)})
    server = calls["MimoServer"]
    assert server["min_containers"] == 1
    assert server["max_containers"] == 1 and server["buffer_containers"] == 0
    # The original stop bounds are unchanged: the same-app guard still runs on
    # its minute schedule with the baked deadline timeout and no warm floor.
    guard = calls["stop_window"]
    assert guard["schedule"] == {"seconds": 60}
    assert guard["timeout"] == window.MAX_SECONDS + 60
    assert guard["min_containers"] == 0 and guard["max_containers"] == 1


def test_unowned_deployment_still_scales_to_zero_with_no_window_guard(window, tmp_path, monkeypatch):
    _, calls = load_serve(tmp_path, monkeypatch, {})
    server = calls["MimoServer"]
    assert server["min_containers"] == 0
    assert server["scaledown_window"] == 300
    assert "stop_window" not in calls


def test_owned_window_start_refuses_after_immutable_expiry(window, tmp_path, monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(window.time, "time", lambda: now)
    config = owned_window_config(window, start=now - 3400)
    module, _ = load_serve(tmp_path, monkeypatch, {"EVALLAB_MIMO_SERVING_WINDOW": json.dumps(config)})
    with pytest.raises(RuntimeError, match="expired"):
        module.MimoServer().start()


def test_adopt_existing_binds_deployed_app_under_live_watchdog(window, adoptable):
    journal, args, store, provider, control, calls = adoptable
    value = window.adopt_existing(args)
    config = value["config"]
    assert value["state"] == "armed_not_ready"
    assert value["app_id"] == provider["app_id"]
    assert value["upstream"] == provider["url"]
    assert value["config_sha256"] == args.config_sha256
    assert value["deployment_invoked_at"] == window.utc(1400)
    assert value["watchdog_heartbeat_at"] == window.utc(1500)
    assert value["reserved_usd"] == config["cap_usd"]
    control.store.put.assert_not_called()
    assert "AppStop" not in calls
    record = json.loads(Path(value["deployment"]["path"]).read_bytes())
    assert record["adoption"] == "existing_owned_deployment"
    assert record["observed_at"] and record["adopted_at"] and "deployed_at" not in record
    assert record["upstream"] == provider["url"] and record["app_id"] == provider["app_id"]
    assert record["deployed_source"]["reported_revision"] == args.deployed_source_revision
    assert record["controller_source"]["sha256"] == window.sha256(
        Path(record["controller_source"]["path"]).read_bytes())
    assert record["observation"]["app_state"] == provider["state"]
    guard = value["stop_guard"]
    assert guard["armed"] is True and guard["app_id"] == provider["app_id"]
    assert guard["deadline"] == config["expires_at"] and Path(guard["evidence"]["path"]).is_file()


@pytest.mark.parametrize("unsafe", [
    "config_sha_mismatch", "missing_source_revision", "journal_upstream_recorded", "journal_stopped",
    "expired", "claim_mismatch", "no_named_deployment", "journal_app_mismatch", "wrong_owner",
    "second_deploy", "stopped_app", "bad_upstream", "guard_missing", "guard_unarmed",
    "guard_wrong_app", "guard_wrong_config", "guard_wrong_deadline",
])
def test_adopt_existing_refuses_unsafe_adoption(window, adoptable, unsafe, monkeypatch):
    journal, args, store, provider, control, calls = adoptable
    config = journal.read()["config"]
    if unsafe == "config_sha_mismatch":
        args.config_sha256 = "0" * 64
    elif unsafe == "missing_source_revision":
        args.deployed_source_revision = " "
    elif unsafe == "journal_upstream_recorded":
        journal.update(upstream="https://earlier.modal.run")
    elif unsafe == "journal_stopped":
        journal.update(state="stopped", stopped_at=window.utc(1600))
    elif unsafe == "expired":
        monkeypatch.setattr(window.time, "time", lambda: 4381)
    elif unsafe == "claim_mismatch":
        store["reservation:" + config["reservation_id"]] = "different-config"
    elif unsafe == "no_named_deployment":
        provider["app_id"] = ""
    elif unsafe == "journal_app_mismatch":
        journal.update(app_id="ap-another")
    elif unsafe == "wrong_owner":
        provider["tags"][provider["app_id"]] = {"evallab_window_id": "another-window"}
    elif unsafe == "second_deploy":
        second = "ap-second-owned"
        provider["apps"].append(SimpleNamespace(app_id=second, name="", description=window.APP_NAME))
        provider["tags"][second] = window.ownership_tags(config)
    elif unsafe == "stopped_app":
        provider["state"] = 5
    elif unsafe == "bad_upstream":
        provider["url"] = "https://x.modal.direct.evil.example"
    elif unsafe == "guard_missing":
        del store["guard:" + config["reservation_id"]]
    elif unsafe == "guard_unarmed":
        store["guard:" + config["reservation_id"]]["armed"] = False
    elif unsafe == "guard_wrong_app":
        store["guard:" + config["reservation_id"]]["app_id"] = "ap-other"
    elif unsafe == "guard_wrong_config":
        store["guard:" + config["reservation_id"]]["config_sha256"] = "f" * 64
    elif unsafe == "guard_wrong_deadline":
        store["guard:" + config["reservation_id"]]["deadline"] = config["stop_deadline"]
    with pytest.raises(ValueError):
        window.adopt_existing(args)
    value = journal.read()
    assert value["state"] in {"reserved_not_deployed", "stopped"}
    assert value["stop_guard"] == {"armed": False}
    assert not value.get("deployment")
    control.store.put.assert_not_called()
    assert "AppStop" not in calls


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
