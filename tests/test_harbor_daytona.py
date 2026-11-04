"""Bounded Daytona environment: admission, usage ledger, disappearance, resources, proxy rules.

Runs against the real Harbor ``DaytonaEnvironment`` base with an injected fake
guard and fake sandbox only: no network, no provider call, no Docker. The base
transport methods that would reach Daytona (``start``, ``stop``, ``exec``,
``_create_sandbox``, ``_sandbox_exec``) are replaced per test.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Not "harbor.environments...": tests/test_egress_lock.py installs stubs for that
# module when Harbor is absent, which would make an importorskip on it pass.
pytest.importorskip("harbor.job_diff", reason="native Harbor is not installed")

from daytona import Resources  # noqa: E402
from harbor.environments.daytona.environment import DaytonaEnvironment  # noqa: E402
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy  # noqa: E402
from harbor.models.trial.paths import TrialPaths  # noqa: E402

from evallab import harbor_daytona  # noqa: E402
from evallab.daytona_guard import AdmissionRefused, GuardUnavailable  # noqa: E402
from evallab.execution_contracts import (  # noqa: E402
    DAYTONA_AUTO_STOP_INTERVAL_MINS,
    ZAI_OPENAPI_PROXY_HOST,
    ZAI_OPENAPI_PROXY_SCRIPT_ENV,
    ZAI_OPENAPI_PROXY_USAGE_FILE_ENV,
    ZAI_OPENAPI_SECRET_FILE_ENV,
)
from evallab.harbor_daytona import (  # noqa: E402
    BoundedDaytonaEnvironment,
    SecretSafeDaytonaEnvironment,
    _ProxyDaytonaDinD,
    render_proxy_overlay,
)

LIMITS = {"cpu": 4.0, "memory_gib": 8.0, "disk_gib": 10.0}


# --- fakes --------------------------------------------------------------------


class FakeGuard:
    """Stands in for DaytonaGuard; every provider-facing call is scripted."""

    def __init__(self) -> None:
        self.observation: dict[str, Any] = {
            "per_sandbox_limits": dict(LIMITS),
            "inventory": [],
            "at_or_near_limit": False,
        }
        self.observe_error: Exception | None = None
        self.reserve_error: Exception | None = None
        self.missing: bool | Exception = False
        self.release_error: Exception | None = None
        self.snapshot = ({"cpu": 2.0, "memory_gib": 4.0, "disk_gib": 8.0}, "linux-vm")
        self.default = "default-snapshot"
        self.reserved: list[dict[str, Any]] = []
        self.released: list[str] = []
        self.observes = 0

    def observe(self, **_: Any) -> dict[str, Any]:
        self.observes += 1
        if self.observe_error is not None:
            raise self.observe_error
        return self.observation

    def reserve(
        self, name: str, resources: dict[str, float], *, ttl_seconds: int, **kwargs: Any
    ) -> dict[str, Any]:
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reserved.append(
            {"name": name, "resources": resources, "ttl_seconds": ttl_seconds, **kwargs}
        )
        return {"admitted": True, "name": name}

    def release(self, name: str) -> None:
        if self.release_error is not None:
            raise self.release_error
        self.released.append(name)

    def sandbox_missing(self, sandbox_id: str) -> bool:
        if isinstance(self.missing, Exception):
            raise self.missing
        return self.missing

    def snapshot_resources(self, name: str) -> tuple[dict[str, float], str]:
        return self.snapshot

    def default_snapshot(self) -> str:
        return self.default


class FakeFs:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.downloads: list[tuple[str, str]] = []

    async def download_file(self, remote: str, local: str) -> None:
        if self.error is not None:
            raise self.error
        self.downloads.append((remote, local))
        Path(local).write_text('{"requests": 3}')


def _sandbox(sandbox_id: str = "sbx-1", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(id=sandbox_id, network_block_all=None, **extra)


@pytest.fixture(autouse=True)
def _base_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Replace every Harbor base method that would reach Daytona."""
    calls: dict[str, list[Any]] = {"start": [], "stop": [], "create": [], "network": []}

    async def start(self: Any, force_build: bool) -> None:
        calls["start"].append(force_build)

    async def stop(self: Any, delete: bool) -> None:
        calls["stop"].append(delete)

    async def create(self: Any, params: Any, daytona: Any = None) -> None:
        calls["create"].append(params)
        self._sandbox = _sandbox("sbx-created")

    async def network(self: Any, network_policy: Any) -> None:
        calls["network"].append(network_policy)

    monkeypatch.setattr(DaytonaEnvironment, "start", start)
    monkeypatch.setattr(DaytonaEnvironment, "stop", stop)
    monkeypatch.setattr(DaytonaEnvironment, "_create_sandbox", create)
    monkeypatch.setattr(DaytonaEnvironment, "_apply_network_policy", network)
    monkeypatch.setattr(harbor_daytona, "DAYTONA_RELEASE_POLL_SECONDS", 0)
    monkeypatch.setattr(harbor_daytona, "DAYTONA_RELEASE_WAIT_SECONDS", 0)
    return calls


def _make(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cls: type = BoundedDaytonaEnvironment,
    *,
    compose: bool = False,
    config: EnvironmentConfig | None = None,
    guard: FakeGuard | None = None,
    **kwargs: Any,
) -> Any:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    if compose:
        (environment_dir / "docker-compose.yaml").write_text("services:\n  main: {}\n")
    else:
        (environment_dir / "Dockerfile").write_text("FROM scratch\n")
    trial_paths = TrialPaths(tmp_path / "trial")
    trial_paths.mkdir()
    guard = guard or FakeGuard()
    monkeypatch.setattr(harbor_daytona, "DaytonaGuard", lambda: guard)
    kwargs.setdefault("ttl_minutes", 30)
    env = cls(
        environment_dir=environment_dir,
        environment_name="task",
        session_id="trial__task",
        trial_paths=trial_paths,
        task_env_config=config or EnvironmentConfig(),
        **kwargs,
    )
    assert env._daytona_guard is guard
    return env


def _usage(env: Any) -> dict[str, Any]:
    return json.loads((env.trial_paths.trial_dir / "daytona-usage.json").read_text())


# --- construction refusals ----------------------------------------------------


@pytest.mark.parametrize("ttl", [0, -5, 1441, True, "60", 30.0, None])
def test_lifetime_must_be_an_explicit_integer_within_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ttl: Any
) -> None:
    with pytest.raises(ValueError, match="explicit positive Daytona lifetime"):
        _make(tmp_path, monkeypatch, ttl_minutes=ttl)


@pytest.mark.parametrize("ttl", [1, 1440])
def test_lifetime_bounds_are_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ttl: int
) -> None:
    env = _make(tmp_path, monkeypatch, ttl_minutes=ttl)
    assert env._trial_ttl_minutes == ttl


@pytest.mark.parametrize("lock", ["yes", 1, None])
def test_egress_lock_must_be_a_real_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: Any
) -> None:
    with pytest.raises(ValueError, match="true or false"):
        _make(tmp_path, monkeypatch, egress_lock=lock)


def test_egress_lock_refused_for_compose_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="single-container"):
        _make(tmp_path, monkeypatch, compose=True, egress_lock=True)


def test_egress_lock_refused_when_a_phase_policy_would_restore_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phases = [NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)]
    with pytest.raises(ValueError, match="phase network policies"):
        _make(tmp_path, monkeypatch, egress_lock=True, phase_network_policies=phases)
    # A phase identical to the baseline never changes the network, so it is fine.
    same = [NetworkPolicy(network_mode=NetworkMode.PUBLIC)]
    env = _make(tmp_path, monkeypatch, egress_lock=True, phase_network_policies=same)
    assert env._egress_lock is True


def test_lifetime_deadline_and_sandbox_name_are_fixed_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = EnvironmentConfig(build_timeout_sec=600.0)
    env = _make(
        tmp_path, monkeypatch, config=config, ttl_minutes=45, auto_stop_interval_mins=999,
        auto_delete_interval_mins=999,
    )
    assert env._daytona_reservation_seconds == 45 * 60 + 600 + 60
    # Caller kwargs cannot lengthen the provider-side lifetime.
    assert env._auto_stop_interval == DAYTONA_AUTO_STOP_INTERVAL_MINS
    assert env._auto_delete_interval == 0
    name = env._daytona_sandbox_name
    assert name.startswith("evallab-") and len(name) == len("evallab-") + 24
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    assert _make(other_dir, monkeypatch, config=config)._daytona_sandbox_name == name


# --- resources ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory_mb", "storage_mb", "memory_gib", "disk_gib"),
    [(1, 1, 1, 1), (1024, 1024, 1, 1), (1025, 10241, 2, 11), (1500, 10240, 2, 10)],
)
def test_sandbox_resources_round_up_to_whole_gib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_mb: int,
    storage_mb: int,
    memory_gib: int,
    disk_gib: int,
) -> None:
    config = EnvironmentConfig(cpus=2, memory_mb=memory_mb, storage_mb=storage_mb)
    resources = _make(tmp_path, monkeypatch, config=config)._sandbox_resources()
    assert (resources.cpu, resources.memory, resources.disk) == (2, memory_gib, disk_gib)


def test_sandbox_resources_pass_none_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make(tmp_path, monkeypatch)
    monkeypatch.setattr(DaytonaEnvironment, "_sandbox_resources", lambda self: None)
    assert env._sandbox_resources() is None


def test_usage_resources_use_declared_values_without_reading_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, guard=guard)
    allocation = asyncio.run(env._usage_resources(Resources(cpu=2, memory=3, disk=5, gpu=1)))
    assert allocation == {"cpu": 2, "memory_gib": 3, "disk_gib": 5, "gpu": 1}
    assert guard.observes == 0


def test_usage_resources_fill_omitted_values_with_the_verified_per_sandbox_maximum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, guard=guard)
    allocation = asyncio.run(env._usage_resources(Resources(memory=3)))
    assert allocation == {"cpu": 4.0, "memory_gib": 3, "disk_gib": 10.0, "gpu": 0}
    assert guard.observes == 1


# --- admission at start ---------------------------------------------------------


def test_start_reserves_the_whole_gib_allocation_then_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    config = EnvironmentConfig(cpus=2, memory_mb=1500, storage_mb=10240)
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, config=config, guard=guard, ttl_minutes=10)
    asyncio.run(env.start(force_build=True))
    (reservation,) = guard.reserved
    assert reservation["name"] == env._daytona_sandbox_name
    assert reservation["resources"]["memory_gib"] == 2
    assert reservation["ttl_seconds"] == env._daytona_reservation_seconds
    assert _usage(env)["admission"] == {"admitted": True, "name": env._daytona_sandbox_name}
    assert _base_transport["start"] == [True]


def test_start_refused_admission_is_recorded_and_never_reaches_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    guard = FakeGuard()
    evidence = {"decision": "refused", "at_or_near_limit": True}
    guard.reserve_error = AdmissionRefused("over quota", evidence)
    env = _make(tmp_path, monkeypatch, guard=guard)
    with pytest.raises(AdmissionRefused):
        asyncio.run(env.start(force_build=False))
    assert _usage(env)["admission"] == evidence
    assert _base_transport["start"] == []
    assert "disappearance" not in _usage(env)


def test_start_guard_unavailable_is_recorded_and_never_reaches_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    guard = FakeGuard()
    guard.reserve_error = GuardUnavailable("usage endpoint down")
    env = _make(tmp_path, monkeypatch, guard=guard)
    with pytest.raises(GuardUnavailable):
        asyncio.run(env.start(force_build=False))
    assert _usage(env)["monitor_error"] == "usage endpoint down"
    assert "admission" not in _usage(env)
    assert _base_transport["start"] == []


def test_start_failure_with_a_vanished_sandbox_records_the_disappearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(self: Any, force_build: bool) -> None:
        self._sandbox = _sandbox("sbx-gone")
        raise TimeoutError("sandbox did not start")

    monkeypatch.setattr(DaytonaEnvironment, "start", failing)
    guard = FakeGuard()
    guard.missing = True
    env = _make(tmp_path, monkeypatch, guard=guard)
    with pytest.raises(TimeoutError):
        asyncio.run(env.start(force_build=False))
    record = _usage(env)["disappearance"]
    assert record["sandbox_id"] == "sbx-gone"
    assert record["error_type"] == "TimeoutError"
    assert record["cause"] == "unknown"


def test_start_failure_with_an_unavailable_guard_records_the_monitor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(self: Any, force_build: bool) -> None:
        raise GuardUnavailable("lock timeout")

    monkeypatch.setattr(DaytonaEnvironment, "start", failing)
    env = _make(tmp_path, monkeypatch)
    with pytest.raises(GuardUnavailable):
        asyncio.run(env.start(force_build=False))
    usage = _usage(env)
    assert usage["monitor_error"] == "lock timeout"
    assert usage["admission"]["admitted"] is True


# --- sandbox creation -----------------------------------------------------------


def _params(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(env_vars=None, ttl_minutes=None, name=None, **fields)


def _create(env: Any, params: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def idle(self: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(BoundedDaytonaEnvironment, "_monitor_daytona_usage", idle)

    async def run() -> None:
        await env._create_sandbox(params)
        monitor = env._daytona_monitor
        assert monitor is not None and not monitor.done()
        monitor.cancel()

    asyncio.run(run())


def test_create_from_a_snapshot_reserves_the_snapshot_allocation_and_bounds_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, guard=guard, ttl_minutes=20)
    params = _params(snapshot=None)
    _create(env, params, monkeypatch)
    assert params.ttl_minutes == 20
    assert params.name == env._daytona_sandbox_name
    (reservation,) = guard.reserved
    assert reservation["resources"] == guard.snapshot[0]
    assert reservation["sandbox_class"] == "linux-vm"
    assert _base_transport["create"] == [params]
    assert _usage(env)["admission"]["admitted"] is True


def test_create_from_an_image_reserves_container_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, guard=guard)
    _create(env, _params(resources=Resources(cpu=1, memory=2)), monkeypatch)
    (reservation,) = guard.reserved
    assert reservation["sandbox_class"] == "container"
    assert reservation["resources"] == {"cpu": 1, "memory_gib": 2, "disk_gib": 10.0, "gpu": 0}


def test_create_refused_admission_keeps_the_provider_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    guard = FakeGuard()
    guard.reserve_error = AdmissionRefused("full", {"decision": "refused"})
    env = _make(tmp_path, monkeypatch, guard=guard)
    with pytest.raises(AdmissionRefused):
        asyncio.run(env._create_sandbox(_params(snapshot="snap")))
    assert _usage(env)["admission"] == {"decision": "refused"}
    assert _base_transport["create"] == []
    assert env._daytona_monitor is None


def test_create_merges_telemetry_labels_over_existing_extra_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "evallab.harbor_laminar.daytona_telemetry_labels",
        lambda _: {"trial": "t-1", "job": "j-1"},
    )
    env = _make(tmp_path, monkeypatch)
    params = _params(snapshot="snap")
    params.env_vars = {
        "DAYTONA_SANDBOX_OTEL_EXTRA_LABELS": "keep=x, trial = stale,,job=old",
        "OTHER": "1",
    }
    _create(env, params, monkeypatch)
    assert params.env_vars["DAYTONA_SANDBOX_OTEL_EXTRA_LABELS"] == (
        "keep=x,trial=t-1,job=j-1"
    )
    assert params.env_vars["OTHER"] == "1"


def test_create_without_labels_leaves_environment_variables_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    env = _make(tmp_path, monkeypatch)
    params = _params(snapshot="snap")
    _create(env, params, monkeypatch)
    assert params.env_vars is None


def test_create_without_a_lock_records_the_open_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    env = _make(tmp_path, monkeypatch)
    _create(env, _params(snapshot="snap"), monkeypatch)
    record = json.loads((env.trial_paths.trial_dir / "egress-lock.json").read_text())
    assert (record["requested"], record["applied"]) == (False, False)
    assert record["sandbox_id"] == "sbx-created"
    assert env._egress_record_written is True


def test_create_with_a_lock_leaves_the_record_to_the_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    env = _make(tmp_path, monkeypatch, egress_lock=True)
    _create(env, _params(snapshot="snap"), monkeypatch)
    assert not (env.trial_paths.trial_dir / "egress-lock.json").exists()


def test_create_survives_an_unwritable_egress_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_laminar.daytona_telemetry_labels", lambda _: {})
    env = _make(tmp_path, monkeypatch)
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path / "missing" / "trial")
    monkeypatch.setattr(env, "_write_daytona_usage", lambda: None)
    _create(env, _params(snapshot="snap"), monkeypatch)
    assert env._egress_record_written is False
    assert env._daytona_monitor is not None


# --- disappearance attribution ----------------------------------------------------


def _vanish(env: Any, **kwargs: Any) -> dict[str, Any] | None:
    asyncio.run(env._capture_daytona_disappearance(**kwargs))
    return env._daytona_usage.get("disappearance")


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_disappearance_ignored_without_a_sandbox_or_when_already_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    env = _make(tmp_path, monkeypatch, guard=guard)
    assert _vanish(env) is None
    env._sandbox = _sandbox()
    env._daytona_usage["disappearance"] = {"first": True}
    assert _vanish(env) == {"first": True}
    assert guard.observes == 0


def test_disappearance_not_recorded_while_the_sandbox_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make(tmp_path, monkeypatch)
    env._sandbox = _sandbox()
    assert _vanish(env) is None
    assert not (env.trial_paths.trial_dir / "daytona-usage.json").exists()


def test_disappearance_check_failure_is_a_monitor_error_not_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = GuardUnavailable("cannot reach provider")
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    assert _vanish(env) is None
    assert _usage(env)["monitor_error"] == "cannot reach provider"


def test_disappearance_under_current_pressure_is_attributed_to_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    pressured = {"at_or_near_limit": True, "inventory": []}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-x")
    record = _vanish(env, current_usage=pressured, error=OSError("boom"))
    assert record["cause"] == "usage_limit_pressure"
    assert record["evidence_strength"] == "observed_capacity_pressure_not_provider_event"
    assert record["pressure_source"] == "current_usage"
    assert record["current_usage"] == pressured
    assert record["error_type"] == "OSError"
    assert record["sandbox_name"] == env._daytona_sandbox_name
    assert guard.observes == 0
    assert _usage(env)["disappearance"] == record


def test_disappearance_reads_current_usage_when_none_was_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observation = {"at_or_near_limit": True}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    record = _vanish(env)
    assert (record["cause"], record["pressure_source"]) == ("usage_limit_pressure", "current_usage")
    assert record["error_type"] is None


def test_disappearance_after_recent_pressure_uses_the_last_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observation = {"at_or_near_limit": False}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    env._daytona_usage["last_observation"] = {"at_or_near_limit": True, "observed_at": _ago(10)}
    record = _vanish(env)
    assert record["cause"] == "usage_limit_pressure"
    assert record["pressure_source"] == "last_observation"


@pytest.mark.parametrize("age", [harbor_daytona.DAYTONA_MONITOR_SECONDS * 2 + 30, -300])
def test_disappearance_ignores_stale_or_future_pressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: float
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observation = {"at_or_near_limit": False}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    env._daytona_usage["last_observation"] = {"at_or_near_limit": True, "observed_at": _ago(age)}
    record = _vanish(env)
    assert record["cause"] == "unknown"
    assert record["evidence_strength"] == "no_capacity_evidence"
    assert record["pressure_source"] is None


def test_disappearance_without_pressure_evidence_is_not_blamed_on_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observation = {"at_or_near_limit": False}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    env._daytona_usage["last_observation"] = {"at_or_near_limit": False, "observed_at": _ago(1)}
    record = _vanish(env)
    assert record["cause"] == "unknown"
    assert record["read_error"] is None


def test_disappearance_records_a_failed_usage_read_and_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observe_error = GuardUnavailable("quota read failed")
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    record = _vanish(env)
    assert record["cause"] == "unknown"
    assert record["current_usage"] is None
    assert record["read_error"] == "quota read failed"


# --- usage monitor ------------------------------------------------------------------


class _Stop(Exception):
    """Ends the otherwise endless monitor loop from inside the fake guard."""


def test_monitor_records_observations_and_survives_guard_outages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harbor_daytona, "DAYTONA_MONITOR_SECONDS", 0)
    guard = FakeGuard()
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-1")
    visible = {"inventory": [{"id": "sbx-1"}], "at_or_near_limit": False}
    seen: list[dict[str, Any]] = []

    def observe(**_: Any) -> dict[str, Any]:
        seen.append(dict(env._daytona_usage))
        step = len(seen)
        if step == 1:
            raise GuardUnavailable("blip")
        if step == 2:
            return visible
        raise _Stop

    guard.observe = observe  # type: ignore[method-assign]
    with pytest.raises(_Stop):
        asyncio.run(env._monitor_daytona_usage())
    assert seen[1]["monitor_error"] == "blip"
    assert "monitor_error" not in seen[2]
    assert seen[2]["last_observation"] == visible
    assert "monitor_error" not in _usage(env)
    assert _usage(env)["last_observation"] == visible


def test_monitor_stops_once_the_sandbox_leaves_the_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    guard.observation = {"inventory": [{"id": "someone-else"}], "at_or_near_limit": True}
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-1")
    asyncio.run(asyncio.wait_for(env._monitor_daytona_usage(), timeout=5))
    record = _usage(env)["disappearance"]
    assert record["cause"] == "usage_limit_pressure"
    assert record["current_usage"] == guard.observation


def test_monitor_keeps_going_when_an_inventory_miss_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harbor_daytona, "DAYTONA_MONITOR_SECONDS", 0)
    guard = FakeGuard()
    guard.missing = False
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-1")
    calls = 0

    def observe(**_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise _Stop
        return {"inventory": [], "at_or_near_limit": False}

    guard.observe = observe  # type: ignore[method-assign]
    with pytest.raises(_Stop):
        asyncio.run(env._monitor_daytona_usage())
    assert "disappearance" not in _usage(env)


# --- exec wrappers ------------------------------------------------------------------


def test_sandbox_exec_failure_on_a_vanished_sandbox_is_recorded_and_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("sandbox unreachable")

    monkeypatch.setattr(DaytonaEnvironment, "_sandbox_exec", broken)
    guard = FakeGuard()
    guard.missing = True
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-gone")
    with pytest.raises(ConnectionError):
        asyncio.run(env._sandbox_exec("true"))
    assert _usage(env)["disappearance"]["error_type"] == "ConnectionError"


def test_sandbox_exec_passes_results_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fine(self: Any, *args: Any, **kwargs: Any) -> Any:
        return ("ran", args, kwargs)

    monkeypatch.setattr(DaytonaEnvironment, "_sandbox_exec", fine)
    env = _make(tmp_path, monkeypatch)
    assert asyncio.run(env._sandbox_exec("ls", cwd="/")) == ("ran", ("ls",), {"cwd": "/"})


def test_exec_failure_on_a_vanished_sandbox_is_recorded_and_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("gone")

    monkeypatch.setattr(DaytonaEnvironment, "exec", broken)
    guard = FakeGuard()
    guard.missing = True
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox("sbx-gone")
    with pytest.raises(ConnectionError):
        asyncio.run(env.exec("true"))
    assert _usage(env)["disappearance"]["sandbox_id"] == "sbx-gone"


# --- stop ---------------------------------------------------------------------------


def test_stop_release_poll_failure_is_recorded_without_releasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    guard = FakeGuard()
    guard.missing = GuardUnavailable("provider read failed")
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()
    asyncio.run(env.stop(delete=False))
    assert _base_transport["stop"] == [True]
    assert guard.released == []
    assert _usage(env)["monitor_error"] == "provider read failed"


def test_stop_cancels_the_usage_monitor_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = FakeGuard()
    guard.missing = True
    env = _make(tmp_path, monkeypatch, guard=guard)
    env._sandbox = _sandbox()

    async def run() -> asyncio.Task[None]:
        monitor = asyncio.create_task(asyncio.sleep(3600))
        env._daytona_monitor = monitor
        await env.stop(delete=True)
        return monitor

    monitor = asyncio.run(run())
    assert monitor.cancelled()
    assert env._daytona_monitor is None
    assert guard.released == [env._daytona_sandbox_name]


# --- proxy overlay rendering ----------------------------------------------------------


SOURCE_OVERLAY = """\
services:
  main:
    networks: {}
  zai-openapi-secret-proxy:
    command: ["python", "/opt/evallab/zai_openapi_secret_proxy.py"]
"""


def _rendered(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "name": "project",
        "services": {
            "main": {"image": "task", "command": None, "entrypoint": None},
            ZAI_OPENAPI_PROXY_HOST: {
                "command": ["python", "/opt/evallab/zai_openapi_secret_proxy.py"],
                "entrypoint": None,
                "volumes": [
                    {"target": "/opt/evallab/zai_openapi_secret_proxy.py", "source": "/h/p.py"},
                    {"target": "/run/secrets/evallab_zai_openapi_api_key", "source": "/h/key"},
                    {"target": "/run/evallab", "source": "/h/usage"},
                ],
            },
        },
        "networks": {
            "default": {"name": "proj_default"},
            "workbench-internal": {"name": "proj_internal"},
        },
    }
    config.update(overrides)
    return config


def _fake_compose(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(config), stderr="")

    monkeypatch.setattr(harbor_daytona.subprocess, "run", run)
    return commands


def _source(tmp_path: Path, text: str = SOURCE_OVERLAY) -> Path:
    source = tmp_path / "overlay.yaml"
    source.write_text(text)
    return source


def test_overlay_rewrites_mounts_user_and_networks_for_the_remote_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _fake_compose(monkeypatch, _rendered())
    config = render_proxy_overlay(_source(tmp_path))
    assert commands[0][:2] == ["docker", "compose"]
    assert "name" not in config
    proxy = config["services"][ZAI_OPENAPI_PROXY_HOST]
    root = "/run/evallab-zai-openapi"
    assert {mount["target"]: mount["source"] for mount in proxy["volumes"]} == {
        "/opt/evallab/zai_openapi_secret_proxy.py": f"{root}/proxy.py",
        "/run/secrets/evallab_zai_openapi_api_key": f"{root}/key",
        "/run/evallab": f"{root}/usage",
    }
    assert proxy["user"] == "65532:65532"
    assert config["services"]["main"]["networks"] == {"default": None, "workbench-internal": None}
    assert config["networks"]["workbench-internal"]["internal"] is True
    # Compose emits null command/entrypoint; only fields the source set survive.
    assert "command" not in config["services"]["main"]
    assert "entrypoint" not in config["services"]["main"]
    assert proxy["command"] == ["python", "/opt/evallab/zai_openapi_secret_proxy.py"]
    assert "entrypoint" not in proxy


def test_overlay_requires_exactly_main_and_the_proxy_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = _rendered()
    rendered["services"]["extra"] = {}
    _fake_compose(monkeypatch, rendered)
    source = _source(tmp_path, SOURCE_OVERLAY + "  extra: {}\n")
    with pytest.raises(ValueError, match="two-service proxy overlay"):
        render_proxy_overlay(source)


def test_overlay_refuses_an_unexpected_proxy_mount_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = _rendered()
    rendered["services"][ZAI_OPENAPI_PROXY_HOST]["volumes"].append(
        {"target": "/host-root", "source": "/"}
    )
    _fake_compose(monkeypatch, rendered)
    with pytest.raises(ValueError, match="proxy mount contract"):
        render_proxy_overlay(_source(tmp_path))


# --- staging the proxy in the VM --------------------------------------------------------


class FakeVm:
    """Records VM commands and uploads; the credential is never read back."""

    def __init__(self, failures: tuple[int, ...] = ()) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.upload_bodies: list[str] = []
        self.compose: list[list[str]] = []
        self._failures = set(failures)

    async def vm_exec(self, command: str, **_: Any) -> SimpleNamespace:
        self.commands.append(command)
        failed = len(self.commands) in self._failures
        return SimpleNamespace(return_code=1 if failed else 0, stdout="", stderr="")

    async def upload(self, source: Any, target: str) -> None:
        self.uploads.append((str(source), target))
        self.upload_bodies.append(Path(source).read_text())


def _dind(
    tmp_path: Path, vm: FakeVm, monkeypatch: pytest.MonkeyPatch, overlays: int = 1
) -> tuple[_ProxyDaytonaDinD, SimpleNamespace]:
    script = tmp_path / "proxy.py"
    script.write_text("print('proxy')")
    secret = tmp_path / "secret"
    secret.write_text("sk-never-in-compose")
    monkeypatch.setenv(ZAI_OPENAPI_PROXY_SCRIPT_ENV, str(script))
    monkeypatch.setenv(ZAI_OPENAPI_SECRET_FILE_ENV, str(secret))
    paths = [_source(tmp_path)] * overlays
    env = SimpleNamespace(
        task_env_config=SimpleNamespace(env=None),
        extra_docker_compose_paths=paths,
        _sdk_upload_file=vm.upload,
    )
    strategy = _ProxyDaytonaDinD(env)  # type: ignore[arg-type]
    strategy._vm_exec = vm.vm_exec  # type: ignore[method-assign]
    return strategy, env


def test_staging_uploads_the_proxy_and_a_resolved_overlay_without_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_compose(monkeypatch, _rendered())
    vm = FakeVm()
    strategy, env = _dind(tmp_path, vm, monkeypatch)
    asyncio.run(strategy._stage_extra_compose_files())
    root = "/run/evallab-zai-openapi"
    assert [target for _, target in vm.uploads[:2]] == [f"{root}/proxy.py", f"{root}/key"]
    assert vm.uploads[2][1].endswith("docker-compose-extra-0.yaml")
    compose = json.loads(vm.upload_bodies[2])
    assert set(compose["services"]) == {"main", ZAI_OPENAPI_PROXY_HOST}
    assert all("sk-never-in-compose" not in body for body in vm.upload_bodies[2:])
    assert env._proxy_networks == {
        "default": "proj_default", "workbench-internal": "proj_internal",
    }
    assert env._proxy_staged is True
    assert "chmod 400" in vm.commands[1]
    assert not Path(vm.uploads[2][0]).exists()


def test_staging_requires_exactly_one_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy, env = _dind(tmp_path, FakeVm(), monkeypatch, overlays=2)
    with pytest.raises(ValueError, match="exactly one proxy overlay"):
        asyncio.run(strategy._stage_extra_compose_files())
    assert not hasattr(env, "_proxy_staged")


@pytest.mark.parametrize(
    ("failing_command", "message", "uploads"),
    [(1, "private remote proxy directories", 0), (2, "protect the remote provider credential", 2)],
)
def test_staging_stops_when_the_vm_cannot_protect_the_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_command: int,
    message: str,
    uploads: int,
) -> None:
    _fake_compose(monkeypatch, _rendered())
    vm = FakeVm(failures=(failing_command,))
    strategy, env = _dind(tmp_path, vm, monkeypatch)
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(strategy._stage_extra_compose_files())
    assert len(vm.uploads) == uploads
    assert not hasattr(env, "_proxy_staged")


# --- secret-safe environment ------------------------------------------------------------


def _overlay_file(tmp_path: Path) -> Path:
    overlay = tmp_path / "proxy-overlay.yaml"
    overlay.write_text(SOURCE_OVERLAY)
    return overlay


def _secret_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    kwargs.setdefault("extra_docker_compose", [_overlay_file(tmp_path)])
    return _make(tmp_path, monkeypatch, SecretSafeDaytonaEnvironment, **kwargs)


def test_secret_safe_refuses_an_egress_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="cut the in-sandbox model proxy off its provider"):
        _secret_safe(tmp_path, monkeypatch, egress_lock=True)


def test_secret_safe_refuses_tasks_that_bring_their_own_compose_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="single-container task"):
        _secret_safe(tmp_path, monkeypatch, compose=True)


def test_secret_safe_swaps_in_the_proxy_transport_for_compose_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _secret_safe(tmp_path, monkeypatch)
    assert env._compose_mode is True
    assert isinstance(env._strategy, _ProxyDaytonaDinD)
    assert env._proxy_staged is False


def test_secret_safe_capabilities_allow_dynamic_allowlists_but_not_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = _secret_safe(tmp_path, monkeypatch).capabilities
    assert compose.network_allowlist and compose.network_allowlist_hostnames
    assert compose.dynamic_network_policy
    assert compose.stream is False


def test_secret_safe_without_compose_keeps_the_base_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make(tmp_path, monkeypatch, SecretSafeDaytonaEnvironment)
    assert env._compose_mode is False
    assert env.capabilities == DaytonaEnvironment.capabilities.fget(env)  # type: ignore[attr-defined]


def test_secret_safe_allowlists_may_name_only_the_internal_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _secret_safe(tmp_path, monkeypatch)
    allow = NetworkMode.ALLOWLIST
    env.validate_network_policy_support(
        NetworkPolicy(network_mode=allow, allowed_hosts=[ZAI_OPENAPI_PROXY_HOST])
    )
    for hosts in (["api.example.com"], [ZAI_OPENAPI_PROXY_HOST, "api.example.com"], []):
        with pytest.raises(ValueError, match="only the internal model proxy"):
            env.validate_network_policy_support(
                NetworkPolicy(network_mode=allow, allowed_hosts=hosts)
            )
    for mode in (NetworkMode.PUBLIC, NetworkMode.NO_NETWORK):
        env.validate_network_policy_support(NetworkPolicy(network_mode=mode))


def test_secret_safe_without_compose_delegates_network_policy_to_harbor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    env = _make(tmp_path, monkeypatch, SecretSafeDaytonaEnvironment)
    policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
    asyncio.run(env._apply_network_policy(policy))
    assert _base_transport["network"] == [policy]


# --- network isolation inside the VM --------------------------------------------------------

CONTAINER = "ab" * 8
PUBLIC, INTERNAL = "proj_default", "proj_internal"


class FakeStrategy(_ProxyDaytonaDinD):
    """The scoped transport with every VM and compose call scripted."""

    def __init__(self) -> None:  # noqa: D107 - no real environment behind it
        self.vm: list[str] = []
        self.compose: list[list[str]] = []
        self.ps = SimpleNamespace(return_code=0, stdout=CONTAINER + "\n", stderr="")
        self.inspect = SimpleNamespace(return_code=0, stdout="{}", stderr="")
        self.failing_prefix: str | None = None
        self.stop_result = SimpleNamespace(return_code=0, stdout="", stderr="")

    def attached(self, *names: str) -> None:
        self.inspect.stdout = json.dumps({name: {} for name in names})

    async def _compose_exec(self, args: list[str], **_: Any) -> Any:
        self.compose.append(args)
        return self.ps if args[0] == "ps" else self.stop_result

    async def _vm_exec(self, command: str, **_: Any) -> Any:
        self.vm.append(command)
        if command.startswith("docker inspect"):
            return self.inspect
        failed = self.failing_prefix is not None and command.startswith(self.failing_prefix)
        return SimpleNamespace(return_code=1 if failed else 0, stdout="", stderr="")


def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeStrategy]:
    env = _secret_safe(tmp_path, monkeypatch)
    strategy = FakeStrategy()
    env._strategy = strategy
    env._proxy_networks = {"default": PUBLIC, "workbench-internal": INTERNAL}
    return env, strategy


def _apply(env: Any, mode: NetworkMode, hosts: list[str] | None = None) -> None:
    policy = NetworkPolicy(network_mode=mode, allowed_hosts=hosts or [])
    asyncio.run(env._apply_network_policy(policy))


def _network_commands(strategy: FakeStrategy) -> list[str]:
    return [c for c in strategy.vm if c.startswith("docker network")]


def test_public_policy_attaches_both_networks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(INTERNAL)
    _apply(env, NetworkMode.PUBLIC)
    assert _network_commands(strategy) == [f"docker network connect {PUBLIC} {CONTAINER}"]


def test_allowlist_policy_leaves_only_the_internal_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(PUBLIC, INTERNAL)
    _apply(env, NetworkMode.ALLOWLIST, [ZAI_OPENAPI_PROXY_HOST])
    assert _network_commands(strategy) == [f"docker network disconnect {PUBLIC} {CONTAINER}"]


def test_no_network_policy_detaches_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(PUBLIC, INTERNAL)
    _apply(env, NetworkMode.NO_NETWORK)
    assert sorted(_network_commands(strategy)) == sorted(
        f"docker network disconnect {name} {CONTAINER}" for name in (PUBLIC, INTERNAL)
    )


def test_policy_already_in_force_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(INTERNAL)
    _apply(env, NetworkMode.ALLOWLIST, [ZAI_OPENAPI_PROXY_HOST])
    assert _network_commands(strategy) == []


def test_allowlist_with_foreign_hosts_is_refused_before_touching_the_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="only the internal model proxy"):
        _apply(env, NetworkMode.ALLOWLIST, ["api.example.com"])
    assert strategy.vm == [] and strategy.compose == []


def test_unexpected_network_attachment_is_refused_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(INTERNAL, "bridge")
    with pytest.raises(RuntimeError, match="unexpected network attachment"):
        _apply(env, NetworkMode.NO_NETWORK)
    assert _network_commands(strategy) == []


def test_network_policy_needs_the_proxy_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _ = _isolated(tmp_path, monkeypatch)
    env._strategy = object()
    with pytest.raises(RuntimeError, match="scoped Daytona compose transport"):
        _apply(env, NetworkMode.PUBLIC)


@pytest.mark.parametrize("stdout", ["", "not-hex", "abc", "AB" * 8, "g" * 12])
def test_network_policy_refuses_an_unidentifiable_task_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.ps = SimpleNamespace(return_code=0, stdout=stdout, stderr="")
    with pytest.raises(RuntimeError, match="identify the task container"):
        _apply(env, NetworkMode.PUBLIC)
    strategy.ps = SimpleNamespace(return_code=1, stdout=CONTAINER, stderr="")
    with pytest.raises(RuntimeError, match="identify the task container"):
        _apply(env, NetworkMode.PUBLIC)
    assert strategy.vm == []


@pytest.mark.parametrize(("code", "stdout"), [(1, "{}"), (0, "")])
def test_network_policy_refuses_when_networks_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.inspect = SimpleNamespace(return_code=code, stdout=stdout, stderr="")
    with pytest.raises(RuntimeError, match="inspect task container networks"):
        _apply(env, NetworkMode.NO_NETWORK)


@pytest.mark.parametrize(
    ("mode", "prefix", "attached", "message"),
    [
        (NetworkMode.NO_NETWORK, "docker network disconnect", (PUBLIC, INTERNAL), "disconnect"),
        (NetworkMode.PUBLIC, "docker network connect", (INTERNAL,), "connect"),
    ],
)
def test_failed_network_change_fails_the_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: NetworkMode,
    prefix: str,
    attached: tuple[str, ...],
    message: str,
) -> None:
    env, strategy = _isolated(tmp_path, monkeypatch)
    strategy.attached(*attached)
    strategy.failing_prefix = prefix
    with pytest.raises(RuntimeError, match=f"Failed to {message} task network"):
        _apply(env, mode)


# --- secret-safe stop ---------------------------------------------------------------------


def _staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fs: FakeFs
) -> tuple[Any, FakeStrategy, Path]:
    target = tmp_path / "usage" / "proxy-usage.json"
    target.parent.mkdir()
    monkeypatch.setenv(ZAI_OPENAPI_PROXY_USAGE_FILE_ENV, str(target))
    guard = FakeGuard()
    guard.missing = True
    env = _secret_safe(tmp_path, monkeypatch, guard=guard)
    env._strategy = strategy = FakeStrategy()
    env._proxy_staged = True
    env._sandbox = _sandbox("sbx-proxy", fs=fs)
    return env, strategy, target


def test_stop_quiesces_the_proxy_then_recovers_its_private_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    fs = FakeFs()
    env, strategy, target = _staged(tmp_path, monkeypatch, fs)
    asyncio.run(env.stop(delete=False))
    assert strategy.compose == [["stop", "-t", "5", "main", ZAI_OPENAPI_PROXY_HOST]]
    assert fs.downloads == [
        ("/run/evallab-zai-openapi/usage/zai-openapi-proxy-usage.json", str(target))
    ]
    assert target.stat().st_mode & 0o777 == 0o600
    assert _base_transport["stop"] == [True]
    assert env._daytona_guard.released == [env._daytona_sandbox_name]


def test_stop_never_invents_usage_when_the_ledger_download_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    fs = FakeFs(error=OSError("download failed with sk-secret"))
    env, _, target = _staged(tmp_path, monkeypatch, fs)
    logged: list[str] = []
    monkeypatch.setattr(
        env.logger, "error", lambda message, *args: logged.append(message % args)
    )
    asyncio.run(env.stop(delete=False))
    assert not target.exists()
    assert _base_transport["stop"] == [True]
    # Only the exception type is logged, never its message.
    assert logged == ["Could not recover GLM proxy accounting: OSError"]


def test_stop_still_deletes_when_the_proxy_will_not_quiesce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    fs = FakeFs()
    env, strategy, target = _staged(tmp_path, monkeypatch, fs)
    strategy.stop_result = SimpleNamespace(return_code=1, stdout="", stderr="")
    with pytest.raises(RuntimeError, match="quiesce the remote model proxy"):
        asyncio.run(env.stop(delete=False))
    assert fs.downloads == []
    assert _base_transport["stop"] == [True]


def test_stop_requires_the_proxy_transport_but_still_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    env, _, _ = _staged(tmp_path, monkeypatch, FakeFs())
    env._strategy = object()
    with pytest.raises(RuntimeError, match="scoped Daytona compose transport"):
        asyncio.run(env.stop(delete=False))
    assert _base_transport["stop"] == [True]


def test_stop_skips_the_ledger_when_the_proxy_was_never_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _base_transport: dict[str, list[Any]]
) -> None:
    fs = FakeFs()
    env, strategy, _ = _staged(tmp_path, monkeypatch, fs)
    env._proxy_staged = False
    asyncio.run(env.stop(delete=False))
    assert strategy.compose == [] and fs.downloads == []
    assert _base_transport["stop"] == [True]
