"""Deterministic provider/state fixtures; no model or sandbox calls.

Coverage recipe for ``src/evallab/daytona_guard.py`` (HAR-360), from the
repository root, using the locked environment:

    uv run --no-sync --with coverage coverage run --data-file=/tmp/cov \\
      --source=src/evallab -m pytest -n0 -q -p no:cacheprovider -o addopts= \\
      tests/test_daytona_guard.py tests/test_harbor_daytona.py \\
      tests/test_egress_lock.py tests/test_har163_dispatch_fixes.py
    uv run --no-sync --with coverage coverage report --data-file=/tmp/cov \\
      --include=src/evallab/daytona_guard.py -m

It reports 438 statements, 0 missed. Keep it that way by injecting every
provider read, using a temporary state root, and monkeypatching time, urllib,
fcntl and os instead of touching credentials, network or host permissions.
"""


from __future__ import annotations

import copy
import json
import threading
import urllib.error
from collections.abc import Callable
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


# --- redirect, helper, transport, inventory and state refusal arms -----------
# Everything below stays fake: a dummy API key only reaches request
# construction, and urllib, fcntl, os and time are monkeypatched, never probed.

DUMMY_KEY = "dummy-daytona-api-key"
API_ROOT = "https://app.daytona.io/api"
ZERO = {"cpu": 0.0, "memory_gib": 0.0, "disk_gib": 0.0, "gpu": 0.0, "concurrent_sandboxes": 0.0}


def test_redirect_handler_refuses_to_forward_authorization() -> None:
    handler = module._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/") is None


def test_helper_number_refuses_overflowing_integer() -> None:
    with pytest.raises(GuardUnavailable, match="InvalidNumber"):
        module._number(10 ** 400)


def test_helper_counts_stopped_vm_and_unlisted_class_release_compute() -> None:
    def item(state: str, klass: str) -> dict[str, Any]:
        return {"state": state, "sandbox_class": klass, "cpu": 4.0, "memory_gib": 8.0,
                "disk_gib": 10.0, "gpu": 0.0}

    assert module._counts(item("stopped", "linux-vm")) == ZERO
    assert module._counts(item("paused", "android")) == ZERO
    assert module._counts(item("paused", "unlisted-class")) == {
        **ZERO, "disk_gib": 10.0,
    }


def test_relative_state_home_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    with pytest.raises(GuardUnavailable, match="InvalidStateRoot"):
        DaytonaGuard(fetch=API())


@pytest.mark.parametrize("api_url", ["http://app.daytona.io/api", "https://app.daytona.io/api?x=1",
                                     "https://app.daytona.io/api#fragment",
                                     "https://key:secret@app.daytona.io/api", "https://"])
def test_insecure_or_ambiguous_api_url_is_refused(tmp_path: Path, api_url: str) -> None:
    with pytest.raises(GuardUnavailable, match="InvalidAPIURL"):
        guard(tmp_path, API(), api_url=api_url)


def rewrite_config(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    source = Path(module.__file__).resolve().parents[2] / "policy" / "daytona-limits.yaml"
    config = yaml.safe_load(source.read_text())
    mutate(config)
    path = tmp_path / "limits.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.mark.parametrize("mutate,match", [
    (lambda config: config.update(schema_version=2), "InvalidConfig"),
    (lambda config: config.update(schema_version=True), "InvalidConfig"),
    (lambda config: config.pop("schema_version"), "InvalidConfig"),
    (lambda config: config.update(region="eu"), "UnsupportedConfigTarget"),
    (lambda config: config.update(safety_fraction=0.9), "InvalidSafetyFraction"),
    (lambda config: config.update(per_sandbox={"cpu": 4}), "InvalidPerSandboxLimits$"),
    (lambda config: config["per_sandbox"].update(cpu=0), "InvalidPerSandboxLimit$"),
    (lambda config: config.update(concurrent_sandboxes=5), "UnverifiedConcurrentLimit"),
    (lambda config: config.update(sources="dashboard"), "MissingLimitSources"),
    (lambda config: config.update(monetary_api="available"), "UnverifiedMonetaryLimit"),
    (lambda config: config.update(wallet_observed=None), "MissingMonetarySource"),
])
def test_guessed_or_unsupported_config_limits_fail_closed(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], match: str,
) -> None:
    with pytest.raises(GuardUnavailable, match=match):
        guard(tmp_path, API(), config_path=rewrite_config(tmp_path, mutate))


def test_non_mapping_config_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "limits.yaml"
    path.write_text("- not-a-mapping\n")
    with pytest.raises(GuardUnavailable, match="InvalidConfig"):
        guard(tmp_path, API(), config_path=path)


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return json.dumps(self._body).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exception: Any) -> bool:
        return False


class Wire:
    """Fake urllib opener: records requests and routes responses by URL path."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.requests: list[Any] = []
        self.responses: dict[str, Any] = {
            "/api-keys/current": (200, {"organizationId": ORG}),
            f"/organizations/{ORG}/usage": (200, usage()),
            module._INVENTORY_PATH: (200, {"items": [], "nextCursor": None}),
        }

    def opener(self, handler: Any) -> Wire:
        self.handlers.append(handler)
        return self

    def open(self, request: Any, timeout: int) -> Any:
        self.requests.append(request)
        value = self.responses[request.full_url.removeprefix(API_ROOT)]
        if isinstance(value, Exception):
            raise value
        status, body = value
        return _Response(status, body)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    fake = Wire()
    monkeypatch.setattr(module.urllib.request, "build_opener", fake.opener)
    monkeypatch.setenv("DAYTONA_API_KEY", DUMMY_KEY)
    return fake


def test_http_get_authenticates_reads_and_keeps_the_no_redirect_handler(
    tmp_path: Path, wire: Wire,
) -> None:
    observed = DaytonaGuard(state_root=tmp_path / "state").observe()
    assert observed["used"]["cpu"] == 0
    assert wire.requests[0].full_url == API_ROOT + "/api-keys/current"
    for request in wire.requests:
        assert request.full_url.startswith(API_ROOT + "/")
        assert request.get_header("Authorization") == "Bearer " + DUMMY_KEY
        assert request.get_method() == "GET"
    assert len(wire.handlers) == len(wire.requests)
    assert all(isinstance(handler, module._NoRedirect) for handler in wire.handlers)


def test_http_get_maps_transport_failures_without_leaking_bodies(
    tmp_path: Path, wire: Wire,
) -> None:
    capacity = DaytonaGuard(state_root=tmp_path / "state")
    usage_path = f"/organizations/{ORG}/usage"
    wire.responses[usage_path] = (503, {"error": SECRET})
    with pytest.raises(GuardUnavailable, match="HTTP 503") as unavailable:
        capacity.observe()
    assert SECRET not in str(unavailable.value)
    wire.responses[usage_path] = urllib.error.HTTPError("unused", 500, SECRET, {"body": SECRET}, None)
    with pytest.raises(GuardUnavailable, match="HTTP 500"):
        capacity.observe()
    wire.responses[usage_path] = TimeoutError(SECRET)
    with pytest.raises(GuardUnavailable) as transport:
        capacity.observe()
    assert str(transport.value) == "TimeoutError"
    assert transport.value.__cause__ is None


def test_http_get_refuses_without_credentials_using_the_real_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    with pytest.raises(GuardUnavailable, match="CredentialsUnavailable") as unavailable:
        DaytonaGuard(state_root=tmp_path / "state").observe()
    assert unavailable.value.__cause__ is None


def test_injected_fetch_may_return_parsed_json_without_a_status(tmp_path: Path) -> None:
    api = API()
    api.overrides["/api-keys/current"] = {"organizationId": ORG}
    api.overrides[f"/organizations/{ORG}/usage"] = usage()
    api.overrides[module._INVENTORY_PATH] = {"items": [], "nextCursor": None}
    assert guard(tmp_path, api).observe()["projected"]["cpu"] == 0


@pytest.mark.parametrize("result", [(200,), (True, {}), ("200", {}), (200, {}, 1)])
def test_malformed_injected_http_results_are_refused(tmp_path: Path, result: Any) -> None:
    api = API()
    api.overrides["/api-keys/current"] = result
    with pytest.raises(GuardUnavailable, match="InvalidHTTPResponse"):
        guard(tmp_path, api).observe()


@pytest.mark.parametrize("code", [404, 500])
def test_injected_http_error_statuses_are_unavailable(tmp_path: Path, code: int) -> None:
    api = API()
    api.overrides["/api-keys/current"] = urllib.error.HTTPError("unused", code, SECRET, {}, None)
    with pytest.raises(GuardUnavailable, match=f"HTTP {code}") as unavailable:
        guard(tmp_path, api).observe()
    assert SECRET not in str(unavailable.value)


@pytest.mark.parametrize("items", [["not-an-object"], [None]])
def test_non_object_inventory_items_are_refused(tmp_path: Path, items: list[Any]) -> None:
    api = API()
    api.pages = [{"items": items, "nextCursor": None}]
    with pytest.raises(GuardUnavailable, match="InvalidSandbox"):
        guard(tmp_path, api).observe()


def test_inventory_from_another_organization_is_refused(tmp_path: Path) -> None:
    api = API(items=[sandbox(0, organizationId="another-organization")])
    with pytest.raises(GuardUnavailable, match="OrganizationMismatch"):
        guard(tmp_path, api).observe()


def test_inventory_pagination_limit_refuses_instead_of_truncating(tmp_path: Path) -> None:
    api = API()
    api.pages = [{"items": [], "nextCursor": str(index + 1)} for index in range(1000)]
    with pytest.raises(GuardUnavailable, match="IncompleteInventory"):
        guard(tmp_path, api).observe()
    assert len([path for path in api.calls if path.startswith("/sandbox?")]) == 1000


@pytest.mark.parametrize("record", [None, [], {"regionUsage": {}}])
def test_malformed_usage_envelope_is_refused(tmp_path: Path, record: Any) -> None:
    api = API()
    api.overrides[f"/organizations/{ORG}/usage"] = (200, record)
    with pytest.raises(GuardUnavailable, match="IncompleteUsage"):
        guard(tmp_path, api).observe()


def test_non_object_usage_entry_is_refused(tmp_path: Path) -> None:
    api = API()
    api.overrides[f"/organizations/{ORG}/usage"] = (200, {"regionUsage": ["us"]})
    with pytest.raises(GuardUnavailable, match="InvalidUsage"):
        guard(tmp_path, api).observe()


@pytest.mark.parametrize("record", [usage(regionId="eu"),
                                    {"regionUsage": [usage()["regionUsage"][0]] * 2}])
def test_missing_or_duplicated_region_usage_is_ambiguous(tmp_path: Path, record: dict[str, Any]) -> None:
    api = API()
    api.overrides[f"/organizations/{ORG}/usage"] = (200, record)
    with pytest.raises(GuardUnavailable, match="AmbiguousRegionUsage"):
        guard(tmp_path, api).observe()


def test_lock_file_open_failure_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = module.os.open

    def refuse_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith("reservations.lock"):
            raise OSError("ENOSPC")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", refuse_open)
    with pytest.raises(GuardUnavailable) as unavailable:
        guard(tmp_path, API()).observe()
    assert str(unavailable.value) == "OSError"


def test_exclusive_lock_failure_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_flock = module.fcntl.flock

    def refuse_lock(fd: int, operation: int) -> Any:
        if operation == module.fcntl.LOCK_EX:
            raise OSError("EWOULDBLOCK")
        return real_flock(fd, operation)

    monkeypatch.setattr(module.fcntl, "flock", refuse_lock)
    with pytest.raises(GuardUnavailable) as unavailable:
        guard(tmp_path, API()).observe()
    assert str(unavailable.value) == "OSError"


@pytest.mark.parametrize("damage", ["schema", "reservations", "entry", "requested"])
def test_invalid_reservation_state_shapes_fail_closed(tmp_path: Path, damage: str) -> None:
    api = API()
    capacity = guard(tmp_path, api)
    capacity.reserve("pending", RESOURCES, ttl_seconds=600)
    path = tmp_path / "state" / "reservations.json"
    data = json.loads(path.read_text())
    if damage == "schema":
        data["schema_version"] = 2
    elif damage == "reservations":
        data["reservations"] = []
    elif damage == "entry":
        data["reservations"]["pending"] = ["not-an-entry"]
    else:
        data["reservations"]["pending"]["requested_resources"]["cpu"] = 6
    path.write_text(json.dumps(data))
    with pytest.raises(GuardUnavailable, match="InvalidReservationState"):
        capacity.observe()


def test_atomic_save_failure_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = module.os.replace

    def refuse_replace(source: Any, destination: Any) -> None:
        if str(destination).endswith("reservations.json"):
            raise OSError("EROFS")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", refuse_replace)
    capacity = guard(tmp_path, API())
    with pytest.raises(GuardUnavailable) as unavailable:
        capacity.reserve("pending", RESOURCES, ttl_seconds=60)
    assert str(unavailable.value) == "OSError"
    assert list((tmp_path / "state").glob("reservations-*.tmp")) == []
    assert not (tmp_path / "state" / "reservations.json").exists()


def test_projected_request_that_overflows_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, API()).check(RESOURCES, count=10 ** 308)
    assert refused.value.snapshot["reasons"] == ["invalid-count"]


@pytest.mark.parametrize("ttl_seconds", [True, 0, -30, 60.5])
def test_invalid_reservation_lifetime_is_refused_before_any_provider_read(
    tmp_path: Path, ttl_seconds: Any,
) -> None:
    api = API()
    with pytest.raises(GuardUnavailable, match="InvalidReservationLifetime"):
        guard(tmp_path, api).reserve("lane", RESOURCES, ttl_seconds=ttl_seconds)
    assert api.calls == []


def test_visible_same_name_with_other_resources_is_refused(tmp_path: Path) -> None:
    api = API(items=[sandbox(0, name="lane")])
    with pytest.raises(AdmissionRefused) as refused:
        guard(tmp_path, api).reserve("lane", {**RESOURCES, "disk_gib": 9.0}, ttl_seconds=60)
    assert refused.value.snapshot["reasons"] == ["sandbox-name-resources-mismatch"]
    assert not (tmp_path / "state" / "reservations.json").exists()


def test_non_object_snapshot_and_config_responses_are_refused(tmp_path: Path) -> None:
    api = API()
    api.overrides["/snapshots/name"] = (200, ["not-an-object"])
    api.overrides["/config"] = (200, ["not-an-object"])
    capacity = guard(tmp_path, api)
    with pytest.raises(GuardUnavailable, match="InvalidSnapshot"):
        capacity.snapshot_resources("name")
    with pytest.raises(GuardUnavailable, match="InvalidConfigResponse"):
        capacity.default_snapshot()
