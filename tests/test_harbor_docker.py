"""Injected native-daemon boundaries; these are not containment certification.

The integration owner runs HTTP/root negative controls through real Docker.
No daemon probe occurs on import, construction, or in these fixtures.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

try:
    from harbor.environments.base import ExecResult
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.config import EnvironmentConfig as NativeEnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    from evallab.harbor_docker import EGRESS_LOCK_RECORD, LockedDockerEnvironment
except ModuleNotFoundError as error:
    if not (error.name == "harbor" or (error.name or "").startswith("harbor.")):
        raise
    # The base lab env (and CI shards) have no `harbor` package installed;
    # fall back to placeholders and skip per test, keeping the module
    # collected for the CI collection contract. Names are only touched at
    # test runtime, after the gate below skips.
    ExecResult = None
    DockerEnvironment = None
    EnvironmentFactory = None
    EnvironmentConfig = None
    NetworkMode = None
    NetworkPolicy = None
    NativeEnvironmentConfig = None
    TrialPaths = None
    EGRESS_LOCK_RECORD = "egress-lock.json"
    LockedDockerEnvironment = None


@pytest.fixture(autouse=True)
def _require_harbor_runtime():
    pytest.importorskip("harbor.environments.docker.docker")


CID = "a" * 64


def _record(paths: TrialPaths) -> dict[str, Any]:
    return json.loads((paths.trial_dir / EGRESS_LOCK_RECORD).read_text())


def _change(tree: Any, path: str, value: Any) -> None:
    keys = [int(key) if key.isdigit() else key for key in path.split(".")]
    for key in keys[:-1]:
        tree = tree[key]
    tree[keys[-1]] = value


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    config = EnvironmentConfig(
        docker_image="fixture:locked", cpus=3, memory_mb=3072, storage_mb=4096
    )
    mounts = [
        {
            "type": "bind",
            "source": str(paths.agent_dir),
            "target": "/logs/agent",
            "bind": {"selinux": "z"},
        },
        {"type": "bind", "source": str(paths.verifier_dir), "target": "/logs/verifier"},
        {
            "type": "bind",
            "source": str(paths.host_artifact_path("main", "/logs/artifacts")),
            "target": "/logs/artifacts",
        },
    ]
    Path(mounts[-1]["source"]).mkdir(parents=True)
    inspected = {
        "Id": CID,
        "Platform": "linux",
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "com.docker.compose.project": "fixture__env",
                "com.docker.compose.service": "main",
            }
        },
        "HostConfig": {
            "NetworkMode": "none",
            "Privileged": False,
            "PublishAllPorts": False,
            "CapAdd": None,
            "CapDrop": ["NET_RAW"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "Runtime": "runc",
            "Devices": [],
            "DeviceRequests": None,
            "DeviceCgroupRules": None,
            "VolumesFrom": None,
            "PortBindings": {},
            "Links": None,
            "ExtraHosts": None,
            "CgroupParent": "",
        },
        "NetworkSettings": {"Networks": {}, "Ports": {}},
        "Mounts": [
            {
                "Type": "bind",
                "Source": m["source"],
                "Destination": m["target"],
                "Propagation": "rprivate",
                "RW": True,
            }
            for m in mounts
        ],
    }
    state = {
        "paths": paths,
        "config": config,
        "mounts": mounts,
        "environment_dir": environment_dir,
        "info": {
            "OSType": "linux",
            "OperatingSystem": "Docker Desktop",
            "ServerVersion": "fixture",
        },
        "inspect": inspected,
        "inspect_error": None,
        "inspect_applied": None,
        "compose": {
            "services": {
                "main": {
                    "network_mode": "none",
                    "cap_drop": ["NET_RAW"],
                    "security_opt": ["no-new-privileges:true"],
                    "volumes": mounts,
                }
            }
        },
        "none_network": [{"Id": "none-id", "Name": "none", "Driver": "null", "Scope": "local"}],
        "up": False,
        "deleted": False,
        "running": False,
        "exec_applied": [],
    }

    async def daemon(self, *command):
        if command[0] == "info":
            return state["info"]
        if command[0] == "network":
            return state["none_network"]
        state["inspect_applied"] = (
            _record(paths)["applied"] if (paths.trial_dir / EGRESS_LOCK_RECORD).exists() else None
        )
        if state["inspect_error"] is not None:
            raise state["inspect_error"]
        return [state["inspect"]]

    async def compose(self, command, **kwargs):
        op = command[0]
        if op == "config":
            Path(command[-1]).write_text(json.dumps(state["compose"]))
        elif op == "up":
            state.update(up=True, running=True, deleted=False)
        elif op == "ps":
            return ExecResult(stdout=CID, stderr=None, return_code=0)
        elif op == "exec":
            state["exec_applied"].append(_record(paths)["applied"])
        elif op == "down":
            state.update(running=False, deleted=True)
        elif op == "stop":
            state["running"] = False
        return ExecResult(stdout=None, stderr=None, return_code=0)

    async def image_os(self, image):
        return None

    async def rootless(self):
        return False

    monkeypatch.setattr(LockedDockerEnvironment, "_daemon_json", daemon)
    monkeypatch.setattr(DockerEnvironment, "_run_docker_compose_command", compose)
    monkeypatch.setattr(DockerEnvironment, "_validate_image_os", image_os)
    monkeypatch.setattr(DockerEnvironment, "_is_rootless_docker", rootless)

    def make(**overrides):
        arguments = dict(
            environment_dir=environment_dir,
            environment_name="fixture",
            session_id="fixture__env",
            trial_paths=paths,
            task_env_config=config,
            mounts=mounts,
            egress_lock=True,
        )
        arguments.update(overrides)
        return LockedDockerEnvironment(**arguments)

    state["make"] = make
    return state


@pytest.mark.parametrize("value", [None, False, 1, "true"])
def test_explicit_true_is_required_and_refusal_is_recorded(native, value):
    with pytest.raises(ValueError, match="explicit egress_lock=true"):
        native["make"](egress_lock=value)
    record = _record(native["paths"])
    assert record["requested"] is False and record["applied"] is False
    assert record["locked_at"] is None and "ValueError" in record["error"]
    assert native["up"] is False


@pytest.mark.parametrize(
    "name", ["docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"]
)
def test_task_compose_is_refused_before_creation(native, name):
    (native["environment_dir"] / name).write_text("services: {}")
    with pytest.raises(ValueError, match="task-provided Compose"):
        native["make"]()
    assert _record(native["paths"])["applied"] is False
    assert native["up"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"extra_docker_compose": ["missing.yaml"]},
        {"extra_docker_compose_paths": ["missing.yaml"]},
        {"privileged": True},
        {"network_mode": "host"},
        {"stream": True},
        {"override_gpus": 1},
        "windows-task",
        "gpu-task",
        "allowlist-policy",
        "phase-policies",
    ],
)
def test_unsupported_execution_routes_fail_closed(native, override):
    # Harbor-backed overrides are built in the body: constructing them in the
    # decorator would touch Harbor names at collection time.
    if isinstance(override, str):
        override = {
            "windows-task": {
                "task_env_config": EnvironmentConfig(docker_image="fixture", os="windows")
            },
            "gpu-task": {"task_env_config": EnvironmentConfig(docker_image="fixture", gpus=1)},
            "allowlist-policy": {
                "network_policy": NetworkPolicy(
                    network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["example.com"]
                )
            },
            "phase-policies": {
                "phase_network_policies": [NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)]
            },
        }[override]
    with pytest.raises((ValueError, RuntimeError)):
        native["make"](**override)
    assert _record(native["paths"])["applied"] is False
    assert native["up"] is False


@pytest.mark.parametrize(
    "source,target",
    [
        ("/", "/logs/agent"),
        ("/var/run/docker.sock", "/logs/agent"),
        ("/home/user/.ssh", "/logs/agent"),
        ("/tmp", "/host"),
    ],
)
def test_secret_socket_and_unrestricted_host_mounts_are_refused(native, source, target):
    with pytest.raises(ValueError):
        native["make"](mounts=[{"type": "bind", "source": source, "target": target}])
    assert _record(native["paths"])["applied"] is False


def test_symlink_escape_in_harbor_mount_is_refused(native, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("not container data")
    (native["paths"].agent_dir / "escape").symlink_to(secret)
    with pytest.raises(ValueError, match="host-data symlinks"):
        native["make"]()
    assert _record(native["paths"])["applied"] is False


@pytest.mark.parametrize("info", [{"OSType": "windows"}, {}, None, []])
def test_non_linux_or_missing_daemon_evidence_refuses_before_native_start(native, info):
    env = native["make"]()
    native["info"] = info
    with pytest.raises(RuntimeError):
        asyncio.run(env.start(force_build=False))
    assert _record(native["paths"])["applied"] is False
    assert native["up"] is False and native["exec_applied"] == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("network_mode", "host"),
        ("privileged", True),
        ("cap_add", ["NET_ADMIN"]),
        ("volumes_from", ["host"]),
        ("security_opt", ["seccomp:unconfined"]),
    ],
)
def test_unsafe_resolved_compose_is_refused_before_creation(native, key, value):
    native["compose"]["services"]["main"][key] = value
    with pytest.raises(RuntimeError):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["up"] is False and native["exec_applied"] == []
    assert _record(native["paths"])["applied"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        ("HostConfig.NetworkMode", "host"),
        ("NetworkSettings.Networks", {"bridge": {}}),
        ("HostConfig.Privileged", True),
        ("HostConfig.CapAdd", ["SYS_ADMIN"]),
        ("HostConfig.CapDrop", []),
        ("HostConfig.SecurityOpt", ["seccomp:unconfined"]),
        ("HostConfig.PidMode", "host"),
        ("HostConfig.VolumesFrom", ["other"]),
        ("Mounts.0.Source", "/"),
        ("Mounts.0.Propagation", "shared"),
        ("Mounts.0.RW", None),
        ("State.Running", False),
        ("Platform", "windows"),
    ],
)
def test_daemon_mismatch_records_failure_and_cleans_without_exec(native, path, value):
    _change(native["inspect"], path, value)
    env = native["make"]()
    with pytest.raises(RuntimeError):
        asyncio.run(env.start(force_build=False))
    record = _record(native["paths"])
    assert record["requested"] is True and record["applied"] is False
    assert record["container_id"] == CID and record["error"]
    assert native["deleted"] is True and native["running"] is False
    assert native["exec_applied"] == [] and env._lock_temp_dir is None


@pytest.mark.parametrize("mutation", ["missing-host-field", "extra-volume", "extra-service"])
def test_missing_and_additional_boundary_evidence_are_refused(native, mutation):
    if mutation == "missing-host-field":
        del native["inspect"]["HostConfig"]["CapAdd"]
    elif mutation == "extra-volume":
        native["inspect"]["Mounts"].append({"Type": "volume", "Destination": "/extra"})
    else:
        native["compose"]["services"]["other"] = {}
    with pytest.raises(RuntimeError):
        asyncio.run(native["make"]().start(force_build=False))
    assert _record(native["paths"])["applied"] is False and native["exec_applied"] == []


@pytest.mark.parametrize("error", [RuntimeError("daemon unavailable"), asyncio.CancelledError()])
def test_inspection_error_or_cancellation_retains_failed_receipt_and_cleanup(native, error):
    native["inspect_error"] = error
    with pytest.raises(type(error)):
        asyncio.run(native["make"]().start(force_build=False))
    assert _record(native["paths"])["applied"] is False
    assert native["deleted"] is True and native["exec_applied"] == []


def test_success_is_recorded_only_after_confirmation_and_native_lifecycle_works(native):
    env = EnvironmentFactory.create_environment_from_config(
        config=NativeEnvironmentConfig(
            import_path="evallab.harbor_docker:LockedDockerEnvironment",
            kwargs={"egress_lock": True},
        ),
        environment_dir=native["environment_dir"],
        environment_name="fixture",
        session_id="fixture__env",
        trial_paths=native["paths"],
        task_env_config=native["config"],
        mounts=native["mounts"],
        phase_network_policies=[NetworkPolicy(), NetworkPolicy()],
    )
    with pytest.raises(RuntimeError, match="unconfirmed"):
        asyncio.run(env.exec("true"))
    asyncio.run(env.start(force_build=False))
    record = _record(native["paths"])
    assert record["applied"] is True and record["locked_at"] is not None
    assert record["backend"] == "docker" and record["container_id"] == CID
    assert record["native_evidence"]["HostConfig"]["NetworkMode"] == "none"
    assert native["inspect_applied"] is False and native["exec_applied"] == [True]
    asyncio.run(env.stop(delete=True))
    assert native["deleted"] is True and env._lock_temp_dir is None
    with pytest.raises(RuntimeError, match="unconfirmed"):
        asyncio.run(env.exec("true"))


def test_keep_containers_obeys_native_stop_instead_of_delete(native):
    env = native["make"](keep_containers=True)
    asyncio.run(env.start(force_build=False))
    asyncio.run(env.stop(delete=True))
    assert native["running"] is False and native["deleted"] is False
    assert env._lock_temp_dir is None


@pytest.mark.parametrize("driver", ["null", "bridge"])
def test_none_pseudo_network_requires_native_driver_confirmation(native, driver):
    native["inspect"]["NetworkSettings"]["Networks"] = {
        "none": {
            "NetworkID": "none-id",
            "IPAddress": "",
            "Gateway": "",
            "GlobalIPv6Address": "",
            "IPv6Gateway": "",
            "MacAddress": "",
            "IPPrefixLen": 0,
            "GlobalIPv6PrefixLen": 0,
        }
    }
    native["none_network"][0]["Driver"] = driver
    env = native["make"]()
    if driver == "bridge":
        with pytest.raises(RuntimeError, match="non-none"):
            asyncio.run(env.start(force_build=False))
        assert _record(native["paths"])["applied"] is False and native["exec_applied"] == []
    else:
        asyncio.run(env.start(force_build=False))
        assert _record(native["paths"])["applied"] is True
        asyncio.run(env.stop(delete=False))


def test_unwritable_success_receipt_never_allows_setup_exec(native, monkeypatch):
    env = native["make"]()
    original = env._write_lock_record

    def fail_success(*, applied, error=None):
        if applied:
            raise OSError("receipt disk full")
        original(applied=applied, error=error)

    monkeypatch.setattr(env, "_write_lock_record", fail_success)
    with pytest.raises(OSError, match="receipt disk full"):
        asyncio.run(env.start(force_build=False))
    assert _record(native["paths"])["applied"] is False
    assert native["exec_applied"] == [] and native["deleted"] is True


def test_runtime_policy_change_cannot_lift_an_applied_lock(native):
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    asyncio.run(env.set_network_policy(NetworkPolicy()))
    with pytest.raises(ValueError, match="policy switches"):
        asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)))
    assert _record(native["paths"])["applied"] is True and native["running"] is True
    assert native["exec_applied"] == [True]
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize("cap", ["NET_RAW", "CAP_NET_RAW"])
def test_native_capability_names_do_not_reject_an_enforced_drop(native, cap):
    native["inspect"]["HostConfig"]["CapDrop"] = [cap]
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    assert _record(native["paths"])["applied"] is True
    asyncio.run(env.stop(delete=True))
