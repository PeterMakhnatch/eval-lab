"""Deterministic provider/state fixtures; no model or sandbox calls."""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

import evallab.daytona_guard as module
from evallab.daytona_guard import AdmissionRefused, DaytonaGuard, GuardUnavailable

ORG = "75ae5c7a-cc37-40e9-8f96-9bd4e8798e7b"
SECRET = "identity-record-secret-never-persist-this"
RESOURCES = {"cpu": 4.0, "memory_gib": 8.0, "disk_gib": 10.0, "gpu": 0.0}


def usage(**changes: Any) -> dict[str, Any]:
    entry = {
        "regionId": "us", "sandboxClass": "container",
        "totalCpuQuota": 100, "totalMemoryQuota": 200, "totalDiskQuota": 300,
        "totalGpuQuota": 0,
        "currentCpuUsage": 0, "currentMemoryUsage": 0, "currentDiskUsage": 0,
        "currentGpuUsage": 0,
        "maxCpuPerSandbox": None, "maxMemoryPerSandbox": None, "maxDiskPerSandbox": None,
    }
    entry.update(changes)
    return {"regionUsage": [entry]}


def sandbox(number: int, **changes: Any) -> dict[str, Any]:
    item = {
        "id": f"sandbox-{number}", "name": f"sandbox-{number}", "organizationId": ORG,
        "target": "us", "sandboxClass": "container", "state": "started",
        "cpu": 4, "memory": 8, "disk": 10, "gpu": 0,
        # Other lanes must count, but labels/credentials must not be copied to evidence.
        "labels": {"lane": "unrelated"}, "env": {"API_KEY": SECRET},
    }
    item.update(changes)
    return item


class API:
    def __init__(self, *, items: list[dict[str, Any]] | None = None,
                 usage_record: dict[str, Any] | None = None) -> None:
        self.identity = {"organizationId": ORG, "value": SECRET, "name": SECRET}
        self.usage = usage_record if usage_record is not None else usage()
        self.pages: list[dict[str, Any]] = [{"items": items or [], "nextCursor": None}]
        self.overrides: dict[str, Any] = {}
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        if path == "/api-keys/current":
            return 200, copy.deepcopy(self.identity)
        if path == f"/organizations/{ORG}/usage":
            return 200, copy.deepcopy(self.usage)
        if path.startswith("/sandbox?"):
            index = int(path.split("cursor=", 1)[1]) if "cursor=" in path else 0
            return 200, copy.deepcopy(self.pages[index])
        raise AssertionError("UnexpectedFixturePath")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAYTONA_TARGET", raising=False)
    monkeypatch.delenv("DAYTONA_API_URL", raising=False)


def guard(tmp_path: Path, api: API, **kwargs: Any) -> DaytonaGuard:
    return DaytonaGuard(state_root=tmp_path / "state", fetch=api, **kwargs)


def test_margin_boundary_preview_and_observed_pressure(tmp_path: Path) -> None:
    api = API(usage_record=usage(currentCpuUsage=76, currentMemoryUsage=152))
    capacity = guard(tmp_path, api)
    exact = capacity.check(RESOURCES)
    assert exact["admitted"] is True
    assert exact["projected"]["cpu"] == 80
    assert exact["projected"]["memory_gib"] == 160
    assert exact["at_or_near_limit"] == []  # pressure is observed, not projected
    assert not (tmp_path / "state" / "reservations.json").exists()
    with pytest.raises(AdmissionRefused) as refusal:
        capacity.check(RESOURCES, count=2)
    assert refusal.value.snapshot["requested"]["cpu"] == 8
    assert refusal.value.snapshot["requested"]["concurrent_sandboxes"] == 2
    assert set(refusal.value.snapshot["reasons"]) == {
        "projected-cpu-over-cap", "projected-memory_gib-over-cap",
    }
    api.usage = usage(currentCpuUsage=80, currentMemoryUsage=160)
    pressure = capacity.observe()
    assert set(pressure["at_or_near_limit"]) == {"cpu", "memory_gib"}
    assert "gpu" not in pressure["at_or_near_limit"]  # zero use / zero quota is not pressure


def test_all_lane_inventory_state_accounting_and_max_telemetry(tmp_path: Path) -> None:
    api = API(items=[
        sandbox(0, state="started"),
        sandbox(1, state="creating"),
        sandbox(2, state="starting"),
        sandbox(3, state="stopping"),
        sandbox(4, state="stopped"),
        sandbox(5, state="paused"),
        sandbox(6, state="archived"),
        sandbox(7, state="destroyed"),
        sandbox(8, state="new-unknown-provider-state"),
        sandbox(9, target="eu"),
        sandbox(10, sandboxClass="linux-vm", state="stopped"),
    ], usage_record=usage(currentCpuUsage=23, currentMemoryUsage=20, currentDiskUsage=80))
    observed = guard(tmp_path, api).observe()
    assert observed["inventory_used"] == {
        "cpu": 20, "memory_gib": 40, "disk_gib": 70, "gpu": 0,
        "concurrent_sandboxes": 5,
    }
    assert observed["used"] == {
        "cpu": 23, "memory_gib": 40, "disk_gib": 80, "gpu": 0,
        "concurrent_sandboxes": 5,
    }
    assert observed["usage_source"] == {
        "cpu": "telemetry", "memory_gib": "inventory", "disk_gib": "telemetry", "gpu": "tie",
    }
    assert len(observed["inventory"]) == 11
    assert observed["inventory"][8]["state"] == "new-unknown-provider-state"
    assert observed["limits"]["concurrent_sandboxes"] is None
    assert SECRET not in json.dumps(observed)


def test_smaller_live_quota_and_live_per_sandbox_caps_take_precedence(tmp_path: Path) -> None:
    api = API(usage_record=usage(totalCpuQuota=5, totalMemoryQuota=400, maxCpuPerSandbox=2))
    capacity = guard(tmp_path, api)
    with pytest.raises(AdmissionRefused) as refusal:
        capacity.check(RESOURCES)
    snapshot = refusal.value.snapshot
    assert snapshot["limits"]["cpu"] == 5
    assert snapshot["limits"]["memory_gib"] == 200  # committed cap cannot silently expand
    assert snapshot["per_sandbox_limits"]["cpu"] == 2
    assert snapshot["per_sandbox_limits"]["memory_gib"] == 8  # real dashboard null fallback
    assert snapshot["reasons"] == ["per-sandbox-cpu-exceeded"]
    assert snapshot["per_sandbox_limit_sources"]["memory_gib"] == "dated_dashboard"


def test_two_concurrent_runners_cannot_take_last_capacity(tmp_path: Path) -> None:
    api = API(items=[sandbox(i) for i in range(19)])  # 76 CPU / 152 RAM
    runners = [guard(tmp_path, api), guard(tmp_path, api)]
    start = threading.Barrier(2)

    def attempt(index: int) -> tuple[str, dict[str, Any]]:
        start.wait(timeout=5)
        try:
            return "admitted", runners[index].reserve(f"lane-{index}", RESOURCES, ttl_seconds=600)
        except AdmissionRefused as error:
            return "refused", error.snapshot

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [0, 1]))
    assert sorted(status for status, _ in results) == ["admitted", "refused"]
    rejected = next(snapshot for status, snapshot in results if status == "refused")
    assert rejected["pending"]["cpu"] == 4
    assert rejected["projected"]["cpu"] == 84
    state = json.loads((tmp_path / "state" / "reservations.json").read_text())
    assert len(state["reservations"]) == 1


def test_own_retry_preserves_reservation_and_others_cannot_bypass_or_release(tmp_path: Path) -> None:
    api = API(usage_record=usage(currentCpuUsage=76, currentMemoryUsage=152))
    owner, other = guard(tmp_path, api), guard(tmp_path, api)
    owner.reserve("pending", RESOURCES, ttl_seconds=600)
    reused = owner.reserve("pending", RESOURCES, ttl_seconds=600)
    assert reused["reservation_status"] == "reused"
    assert reused["pending"]["cpu"] == 0
    assert reused["projected"]["cpu"] == 80
    other.release("pending")
    with pytest.raises(AdmissionRefused) as refused:
        other.reserve("pending", RESOURCES, ttl_seconds=600)
    assert "reservation-name-owned-by-another-runner" in refused.value.snapshot["reasons"]
    with pytest.raises(AdmissionRefused) as changed:
        owner.reserve("pending", {**RESOURCES, "cpu": 5}, ttl_seconds=600)
    assert "per-sandbox-cpu-exceeded" in changed.value.snapshot["reasons"]
    assert other.observe()["pending"]["cpu"] == 4
    owner.release("pending")
    other.reserve("next", RESOURCES, ttl_seconds=600)


def test_owned_resize_races_another_lane_without_double_charge_or_ambiguous_shrink(tmp_path: Path) -> None:
    api = API(items=[sandbox(i) for i in range(19)])
    owner, other = guard(tmp_path, api), guard(tmp_path, api)
    owner.reserve("building", RESOURCES, ttl_seconds=600)
    path = tmp_path / "state" / "reservations.json"
    before = json.loads(path.read_text())["reservations"]["building"]
    actual = {"cpu": 2.0, "memory_gib": 4.0, "disk_gib": 7.0, "gpu": 0.0}
    start = threading.Barrier(2)

    def attempt(index: int) -> tuple[str, dict[str, Any]]:
        start.wait(timeout=5)
        try:
            snapshot = (owner.reserve("building", actual, ttl_seconds=60) if index == 0
                        else other.reserve("other-lane", actual, ttl_seconds=60))
            return "admitted", snapshot
        except AdmissionRefused as error:
            return "refused", error.snapshot

    with ThreadPoolExecutor(max_workers=2) as executor:
        resized, competing = list(executor.map(attempt, [0, 1]))
    assert resized[0] == "admitted"
    assert competing[0] == "refused"
    assert resized[1]["reservation_status"] == "resized"
    assert resized[1]["requested"]["cpu"] == 2
    assert resized[1]["reservation_resources"]["cpu"] == 4
    assert resized[1]["pending"]["cpu"] == 0
    assert resized[1]["projected"]["cpu"] == 80  # 76 + hold 4, never old 4 + new 2
    assert competing[1]["pending"]["cpu"] == 4
    assert competing[1]["projected"]["cpu"] == 82
    record = json.loads(path.read_text())["reservations"]["building"]
    assert record["resources"] == RESOURCES
    assert record["requested_resources"] == actual
    assert record["expires_at_epoch"] >= before["expires_at_epoch"]
    assert record["owner"] == before["owner"]
    # Only authoritative visibility can replace an ambiguous earlier high-water
    # allocation with the smaller final allocation and free capacity for others.
    api.pages[0]["items"].append(sandbox(
        19, name="building", cpu=actual["cpu"], memory=actual["memory_gib"], disk=actual["disk_gib"],
    ))
    admitted = other.reserve("other-lane", actual, ttl_seconds=60)
    assert admitted["used"]["cpu"] == 78
    assert admitted["pending"]["cpu"] == 0
    assert admitted["projected"]["cpu"] == 80
    assert list(json.loads(path.read_text())["reservations"]) == ["other-lane"]


def test_owned_upsize_checks_other_pending_and_preserves_old_hold_when_refused(tmp_path: Path) -> None:
    api = API(usage_record=usage(currentCpuUsage=74))
    owner, other = guard(tmp_path, api), guard(tmp_path, api)
    owner.reserve("owner", {**RESOURCES, "cpu": 2}, ttl_seconds=600)
    other.reserve("other", {**RESOURCES, "cpu": 3}, ttl_seconds=600)
    path = tmp_path / "state" / "reservations.json"
    before = path.read_bytes()
    with pytest.raises(AdmissionRefused) as refused:
        owner.reserve("owner", RESOURCES, ttl_seconds=600)
    assert refused.value.snapshot["pending"]["cpu"] == 3
    assert refused.value.snapshot["projected"]["cpu"] == 81
    assert "projected-cpu-over-cap" in refused.value.snapshot["reasons"]
    assert path.read_bytes() == before
    api.usage = usage(currentCpuUsage=74, maxCpuPerSandbox=1)
    with pytest.raises(AdmissionRefused) as capped:
        owner.reserve("owner", {**RESOURCES, "cpu": 2}, ttl_seconds=600)
    assert "per-sandbox-cpu-exceeded" in capped.value.snapshot["reasons"]
    assert path.read_bytes() == before


def test_visible_name_retry_and_reconciliation_do_not_double_count(tmp_path: Path) -> None:
    api = API(items=[sandbox(i) for i in range(19)])
    capacity = guard(tmp_path, api)
    capacity.reserve("appearing", RESOURCES, ttl_seconds=600)
    api.pages[0]["items"].append(sandbox(19, name="appearing"))
    observed = capacity.observe()
    assert observed["used"]["cpu"] == 80
    assert observed["pending"]["cpu"] == 0
    retry = capacity.reserve("appearing", RESOURCES, ttl_seconds=600)
    assert retry["reservation_status"] == "already-visible"
    assert retry["requested"]["cpu"] == 4
    assert retry["projected"]["cpu"] == 80
    state = json.loads((tmp_path / "state" / "reservations.json").read_text())
    assert state["reservations"] == {}
    with pytest.raises(AdmissionRefused):
        capacity.reserve("different-new-name", RESOURCES, ttl_seconds=600)


@pytest.mark.parametrize("state", ["stopped", "paused", "archived", "destroyed"])
def test_visible_inactive_name_cannot_bypass_new_compute_admission(tmp_path: Path, state: str) -> None:
    api = API(items=[sandbox(0, name="old-name", state=state)])
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, api).reserve("old-name", RESOURCES, ttl_seconds=60)
    assert "sandbox-name-not-active" in refused.value.snapshot["reasons"]
    assert not (tmp_path / "state" / "reservations.json").exists()


def test_other_region_same_name_cannot_hide_pending(tmp_path: Path) -> None:
    api = API()
    capacity = guard(tmp_path, api)
    capacity.reserve("same-name", RESOURCES, ttl_seconds=600)
    api.pages[0]["items"] = [sandbox(0, name="same-name", target="eu")]
    assert capacity.observe()["pending"]["cpu"] == 4
    with pytest.raises(AdmissionRefused) as refused:
        capacity.reserve("same-name", RESOURCES, ttl_seconds=600)
    assert "sandbox-name-pool-mismatch" in refused.value.snapshot["reasons"]


def test_expiry_and_retry_extension_are_conservative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    api = API(usage_record=usage(currentCpuUsage=76))
    capacity = guard(tmp_path, api)
    capacity.reserve("expires", RESOURCES, ttl_seconds=60)
    clock[0] = 1040
    capacity.reserve("expires", RESOURCES, ttl_seconds=60)  # retry extends, never shortens
    clock[0] = 1099
    with pytest.raises(AdmissionRefused):
        guard(tmp_path, api).check(RESOURCES)
    clock[0] = 1101
    assert guard(tmp_path, api).check(RESOURCES)["admitted"] is True


def test_api_refresh_failure_keeps_pending_across_process_loss(tmp_path: Path) -> None:
    api = API(usage_record=usage(currentCpuUsage=76))
    capacity = guard(tmp_path, api)
    capacity.reserve("ambiguous-creation", RESOURCES, ttl_seconds=600)
    path = tmp_path / "state" / "reservations.json"
    before = path.read_bytes()
    api.overrides[f"/organizations/{ORG}/usage"] = (503, {"error": SECRET})
    with pytest.raises(GuardUnavailable, match="HTTP 503"):
        capacity.observe()
    with pytest.raises(GuardUnavailable, match="HTTP 503"):
        capacity.reserve("another", RESOURCES, ttl_seconds=600)
    assert path.read_bytes() == before
    del api.overrides[f"/organizations/{ORG}/usage"]
    # A fresh guard sees the orphan reservation, but cannot erase it.
    restarted = guard(tmp_path, api)
    restarted.release("ambiguous-creation")
    with pytest.raises(AdmissionRefused):
        restarted.check(RESOURCES)


def test_full_cursor_inventory_counts_all_pages_without_filters(tmp_path: Path) -> None:
    api = API()
    api.pages = [
        {"items": [sandbox(0)], "nextCursor": "1"},
        {"items": [sandbox(1, target="eu")], "nextCursor": "2"},
        {"items": [sandbox(2, state="stopped")], "nextCursor": None},
    ]
    observed = guard(tmp_path, api).observe()
    assert observed["used"]["cpu"] == 4
    assert observed["used"]["disk_gib"] == 20
    assert [item["id"] for item in observed["inventory"]] == ["sandbox-0", "sandbox-1", "sandbox-2"]
    inventory_calls = [path for path in api.calls if path.startswith("/sandbox?")]
    assert len(inventory_calls) == 3
    assert all("includeErroredDeleted=true" in path and "includeWarm=true" in path for path in inventory_calls)
    assert all("labels=" not in path and "regionIds=" not in path and "states=" not in path for path in inventory_calls)


@pytest.mark.parametrize("damage", ["missing-cursor", "cursor-cycle", "missing-items", "duplicate-id"])
def test_incomplete_or_inconsistent_inventory_refused(tmp_path: Path, damage: str) -> None:
    api = API()
    if damage == "missing-cursor":
        api.pages = [{"items": []}]
    elif damage == "cursor-cycle":
        api.pages = [{"items": [], "nextCursor": "1"}, {"items": [], "nextCursor": "1"}]
    elif damage == "missing-items":
        api.pages = [{"nextCursor": None}]
    else:
        api.pages = [{"items": [sandbox(0), sandbox(0)], "nextCursor": None}]
    with pytest.raises(GuardUnavailable):
        guard(tmp_path, api).reserve("not-recorded", RESOURCES, ttl_seconds=60)
    assert not (tmp_path / "state" / "reservations.json").exists()


@pytest.mark.parametrize("field", ["totalCpuQuota", "currentDiskUsage", "maxMemoryPerSandbox"])
def test_usage_missing_required_fields_fails_closed(tmp_path: Path, field: str) -> None:
    api = API()
    del api.usage["regionUsage"][0][field]
    with pytest.raises(GuardUnavailable):
        guard(tmp_path, api).check(RESOURCES)


@pytest.mark.parametrize("entry", [None, {"organizationId": "wrong"}, {"value": SECRET}])
def test_unverified_identity_fails_closed(tmp_path: Path, entry: Any) -> None:
    api = API()
    api.overrides["/api-keys/current"] = (200, entry)
    with pytest.raises(GuardUnavailable, match="OrganizationMismatch"):
        guard(tmp_path, api).observe()


@pytest.mark.parametrize("field,value", [("memory", None), ("gpu", True), ("target", None),
                                         ("sandboxClass", None), ("sandboxClass", "new-unverified-class")])
def test_unknown_inventory_resources_or_pool_fails_closed(tmp_path: Path, field: str, value: Any) -> None:
    api = API(items=[sandbox(0, **{field: value})])
    with pytest.raises(GuardUnavailable):
        guard(tmp_path, api).observe()


def test_corrupt_or_invalid_pending_state_is_not_ignored(tmp_path: Path) -> None:
    api = API()
    capacity = guard(tmp_path, api)
    capacity.reserve("pending", RESOURCES, ttl_seconds=600)
    path = tmp_path / "state" / "reservations.json"
    data = json.loads(path.read_text())
    data["reservations"]["pending"]["resources"]["cpu"] = -4
    path.write_text(json.dumps(data))
    with pytest.raises(GuardUnavailable):
        capacity.check(RESOURCES)
    path.write_text("not-json")
    with pytest.raises(GuardUnavailable):
        capacity.observe()


@pytest.mark.parametrize("dimension,value", [("cpu", True), ("cpu", None), ("cpu", 0),
                                            ("memory_gib", -1), ("disk_gib", float("nan")),
                                            ("gpu", float("inf")), ("gpu", -1)])
def test_candidate_invalid_numbers_are_refused(tmp_path: Path, dimension: str, value: Any) -> None:
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, API()).check({**RESOURCES, dimension: value})
    assert "invalid-resources" in refused.value.snapshot["reasons"]


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, 10 ** 1000])
def test_candidate_invalid_counts_are_refused(tmp_path: Path, count: Any) -> None:
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, API()).check(RESOURCES, count=count)
    assert "invalid-count" in refused.value.snapshot["reasons"]


@pytest.mark.parametrize("resources", [
    {"cpu": 4},
    {**RESOURCES, "unknown_dimension": 1},
    [4, 8, 10, 0],
])
def test_incomplete_or_unknown_resource_dimensions_are_refused(tmp_path: Path, resources: Any) -> None:
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, API()).check(resources)
    assert "invalid-resources" in refused.value.snapshot["reasons"]


def test_gpu_use_refused_without_fake_concurrency_cap(tmp_path: Path) -> None:
    capacity = guard(tmp_path, API())
    with pytest.raises(AdmissionRefused) as refused:
        capacity.check({**RESOURCES, "gpu": 1})
    assert refused.value.snapshot["reasons"] == ["projected-gpu-over-cap"]
    assert refused.value.snapshot["limits"]["concurrent_sandboxes"] is None


def test_sandbox_missing_requires_main_sandbox_404_not_session_404(tmp_path: Path) -> None:
    api = API()
    api.overrides.update({
        "/sandbox/gone": (404, "non-json or secret body"),
        "/sandbox/live": (200, {"id": "live", "session_error": {"status": 404}}),
        "/sandbox/empty": (200, None),
        "/sandbox/denied": (403, {"error": SECRET}),
        "/sandbox/transient": (503, {"error": SECRET}),
        "/sandbox/http-missing": urllib.error.HTTPError("unused", 404, SECRET, {}, None),
    })
    capacity = guard(tmp_path, api)
    assert capacity.sandbox_missing("gone") is True
    assert capacity.sandbox_missing("http-missing") is True
    assert capacity.sandbox_missing("live") is False
    assert capacity.sandbox_missing("empty") is False
    for name in ("denied", "transient"):
        with pytest.raises(GuardUnavailable) as unavailable:
            capacity.sandbox_missing(name)
        assert SECRET not in str(unavailable.value)


def test_snapshot_allocations_and_class_are_actual_not_defaults(tmp_path: Path) -> None:
    api = API()
    api.overrides.update({
        "/config": (200, {"defaultSnapshot": "default:actual"}),
        "/snapshots/default%3Aactual": (200, {
            "cpu": 2, "mem": 6, "disk": 7, "gpu": 0, "sandboxClass": "container",
        }),
        "/snapshots/vm": (200, {
            "cpu": 4, "mem": 8, "disk": 20, "gpu": 1, "sandboxClass": "linux-vm",
        }),
    })
    capacity = guard(tmp_path, api)
    name = capacity.default_snapshot()
    assert name == "default:actual"
    assert capacity.snapshot_resources(name) == (
        {"cpu": 2, "memory_gib": 6, "disk_gib": 7, "gpu": 0}, "container",
    )
    assert capacity.snapshot_resources("vm") == (
        {"cpu": 4, "memory_gib": 8, "disk_gib": 20, "gpu": 1}, "linux-vm",
    )
    del api.overrides["/snapshots/default%3Aactual"][1]["gpu"]
    with pytest.raises(GuardUnavailable):
        capacity.snapshot_resources(name)
    api.overrides["/config"] = (200, {})
    with pytest.raises(GuardUnavailable):
        capacity.default_snapshot()


def test_safe_evidence_keeps_dated_monetary_observations_not_authorization(tmp_path: Path) -> None:
    capacity = guard(tmp_path, API())
    evidence = capacity.reserve("owned", RESOURCES, ttl_seconds=600)
    assert evidence["limit_sources"]["usage_observed_at"] == "2026-10-01T20:45:55Z"
    money = evidence["monetary"]
    assert money["api_status"] == "unavailable"
    assert money["credit_ceiling_usd"] is None
    assert money["live_credit_headroom_usd"] is None
    assert money["spending_authorization"] is False
    assert money["dated_dashboard_observation"]["free_usd"] == 185.15
    assert money["dated_dashboard_observation"]["month_spend_usd"] == 8.47
    assert SECRET not in json.dumps(evidence)
    assert SECRET not in (tmp_path / "state" / "reservations.json").read_text()
    assert "DAYTONA_API_KEY" not in (tmp_path / "state" / "reservations.json").read_text()


def test_injected_exception_messages_never_leak_secrets(tmp_path: Path) -> None:
    api = API()
    api.overrides["/api-keys/current"] = RuntimeError(SECRET)
    with pytest.raises(GuardUnavailable) as unavailable:
        guard(tmp_path, api).observe()
    assert str(unavailable.value) == "RuntimeError"
    assert unavailable.value.__cause__ is None


def test_defaults_use_global_state_root_and_explicit_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    api = API()
    capacity = DaytonaGuard(fetch=api)
    capacity.reserve("global", RESOURCES, ttl_seconds=60)
    assert (tmp_path / "evallab" / "daytona-admission" / "reservations.json").is_file()
    monkeypatch.setenv("DAYTONA_TARGET", "eu")
    with pytest.raises(GuardUnavailable, match="UnsupportedTarget"):
        capacity.observe()
    assert capacity.observe(region="us")["region"] == "us"
    with pytest.raises(GuardUnavailable, match="UnsupportedTarget"):
        capacity.check(RESOURCES, region="us", sandbox_class="linux-vm")


def test_missing_or_unverified_config_values_fail_closed(tmp_path: Path) -> None:
    source = Path(module.__file__).resolve().parents[2] / "policy" / "daytona-limits.yaml"
    config = yaml.safe_load(source.read_text())
    config["per_sandbox"]["cpu"] = None
    broken = tmp_path / "limits.yaml"
    broken.write_text(yaml.safe_dump(config))
    with pytest.raises(GuardUnavailable, match="MissingPerSandboxLimit"):
        guard(tmp_path, API(), config_path=broken)
    with pytest.raises(GuardUnavailable):
        guard(tmp_path, API(), config_path=tmp_path / "missing.yaml")
