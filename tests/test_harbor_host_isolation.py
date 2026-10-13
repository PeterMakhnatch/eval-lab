"""Injected Docker lifecycle checks; no daemon, service, or credential probes."""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from evallab import host_isolation

try:
    from harbor.environments.base import ExecResult
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths

    from evallab.harbor_host_isolation import (
        ISOLATION_RECORD,
        HostIsolatedDockerEnvironment,
    )
except ModuleNotFoundError as error:
    if not (error.name == "harbor" or (error.name or "").startswith("harbor.")):
        raise
    ExecResult = DockerEnvironment = EnvironmentConfig = NetworkMode = NetworkPolicy = None
    TrialPaths = HostIsolatedDockerEnvironment = None
    ISOLATION_RECORD = "host-isolation.json"


_PREPARE_TOOL = (
    HostIsolatedDockerEnvironment._prepare_tool
    if HostIsolatedDockerEnvironment is not None
    else None
)
CID = "a" * 64
NETWORK_ID = "b" * 64
HOST_ADDRESSES = ("8.8.4.4", "192.168.1.35", "2606:4700::1111")
BRIDGE_ADDRESSES = ("192.168.65.254",)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 13, 3, 0, 0, tzinfo=UTC)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _require_harbor_runtime():
    pytest.importorskip("harbor.environments.docker.docker")


def _record(native) -> dict[str, Any]:
    return json.loads((native["paths"].trial_dir / ISOLATION_RECORD).read_text())


def _change(tree: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        tree = tree[key]
    tree[keys[-1]] = value


def _listing(rules: str) -> str:
    """Model nft's JSON output for these literal rules, including rule handles."""
    statements: list[dict[str, Any]] = [
        {"metainfo": {"version": "fixture", "json_schema_version": 1}},
        {"table": {"family": "inet", "name": "evallab_host_isolation", "handle": 1}},
        {
            "chain": {
                "family": "inet",
                "table": "evallab_host_isolation",
                "name": "output",
                "handle": 1,
                "type": "filter",
                "hook": "output",
                "prio": -200,
                "policy": "accept",
            }
        },
    ]
    for line in rules.splitlines():
        line = line.strip()
        expr: list[dict[str, Any]] = []
        if line == 'oifname "lo" accept':
            expr = [
                {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "lo"}},
                {"accept": None},
            ]
        elif line.startswith(("ip daddr ", "ip6 daddr ")):
            family, _field, rest = line.split(" ", 2)
            action = "accept" if rest.endswith("accept") else "reject"
            target = rest.rsplit(" ", 1)[0]
            op = "=="
            if target.startswith("!= "):
                op, target = "!=", target[3:]
            if " tcp dport " in target:
                target, port = target.split(" tcp dport ")
                expr.append(
                    {
                        "match": {
                            "op": "==",
                            "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                            "right": int(port),
                        }
                    }
                )
            expr.insert(
                0,
                {
                    "match": {
                        "op": op,
                        "left": {"payload": {"protocol": family, "field": "daddr"}},
                        "right": target,
                    }
                },
            )
            expr.append({action: None})
        if expr:
            statements.append(
                {
                    "rule": {
                        "family": "inet",
                        "table": "evallab_host_isolation",
                        "chain": "output",
                        "handle": len(statements),
                        "expr": expr,
                    }
                }
            )
    return json.dumps({"nftables": statements})


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _require_harbor_runtime):
    monkeypatch.delenv("EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT", raising=False)
    monkeypatch.setattr("evallab.harbor_host_isolation.datetime", _FixedDatetime)
    monkeypatch.setattr("evallab.harbor_docker.datetime", _FixedDatetime)
    monkeypatch.setattr(
        "evallab.harbor_host_isolation.uuid.uuid4",
        lambda: UUID("01234567-89ab-cdef-0123-456789abcdef"),
    )
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    config = EnvironmentConfig(docker_image="fixture:host-isolated")
    mounts = [
        {"type": "bind", "source": str(paths.agent_dir), "target": "/logs/agent"},
        {"type": "bind", "source": str(paths.verifier_dir), "target": "/logs/verifier"},
        {
            "type": "bind",
            "source": str(paths.host_artifact_path("main", "/logs/artifacts")),
            "target": "/logs/artifacts",
        },
    ]
    Path(mounts[-1]["source"]).mkdir(parents=True)
    compose_main = {
        "network_mode": "none",
        "entrypoint": ["/bin/sh", "-c", "exec sleep infinity"],
        "command": [],
        "healthcheck": {"disable": True},
        "cap_drop": ["NET_ADMIN", "NET_RAW"],
        "security_opt": ["no-new-privileges:true"],
        "user": "0",
        "volumes": mounts,
    }
    inspected = {
        "Id": CID,
        "Platform": "linux",
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "com.docker.compose.project": "fixture__env",
                "com.docker.compose.service": "main",
            },
            "Entrypoint": compose_main["entrypoint"],
            "Cmd": [],
            "Healthcheck": {"Test": ["NONE"]},
            "User": "0",
        },
        "HostConfig": {
            "NetworkMode": "none",
            "Privileged": False,
            "PublishAllPorts": False,
            "CapAdd": None,
            "CapDrop": ["NET_ADMIN", "NET_RAW"],
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
                "Source": mount["source"],
                "Destination": mount["target"],
                "Propagation": "rprivate",
                "RW": True,
            }
            for mount in mounts
        ],
    }
    state = {
        "paths": paths,
        "config": config,
        "mounts": mounts,
        "environment_dir": environment_dir,
        "compose": {"services": {"main": compose_main}},
        "inspect": inspected,
        "events": [],
        "up": False,
        "deleted": False,
        "connected": False,
        "installed": False,
        "rules": "",
        "listing_override": None,
        "listing_after_attach": None,
        "failure": None,
        "tool_path": None,
        "ordinary_exec_caps": [],
    }

    async def prepare(self):
        state["events"].append(("tool", "prepared"))
        if state["failure"] == "prepare":
            raise RuntimeError("fixture prepare failure")
        self._tool_temp = tempfile.TemporaryDirectory(dir=tmp_path)
        state["tool_path"] = Path(self._tool_temp.name)
        self._nft_command = ["/fixture/loader", "--library-path", "/fixture", "/fixture/nft"]
        self._tool_image_digest = "sha256:fixture"
        if self._bridge_port is not None:
            self._bridge_addresses = BRIDGE_ADDRESSES

    async def daemon(self, *command, stdin=None):
        state["events"].append(("daemon", *command))
        if command[:1] == ("info",):
            return json.dumps({"OSType": "linux", "OperatingSystem": "fixture"})
        if command[:2] == ("container", "inspect"):
            return json.dumps([state["inspect"]])
        if command[:2] == ("network", "create"):
            return NETWORK_ID
        if command[:2] == ("network", "disconnect"):
            assert state["installed"], "network disconnected before filter installation"
            if state["failure"] == "disconnect":
                raise RuntimeError("fixture disconnect failure")
            return ""
        if command[:2] == ("network", "connect"):
            assert state["installed"], "network attached before filter installation"
            if state["failure"] == "connect":
                raise RuntimeError("fixture connect failure")
            state["connected"] = True
            state["inspect"]["NetworkSettings"]["Networks"] = {
                self._network_name: {"NetworkID": NETWORK_ID}
            }
            return ""
        if command[:2] == ("network", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": NETWORK_ID,
                        "Name": self._network_name,
                        "Driver": "bridge",
                        "Scope": "local",
                    }
                ]
            )
        if command[:2] == ("network", "rm"):
            state["connected"] = False
            return ""
        if command[:1] == ("cp",):
            assert state["up"] and not state["connected"]
            return ""
        if command[:1] == ("exec",):
            assert "--privileged" in command
            assert state["up"]
            if "--file" in command or "-f" in command:
                assert not state["connected"]
                if state["failure"] == "install":
                    raise RuntimeError("fixture install failure")
                state["rules"] = stdin
                state["installed"] = True
                state["events"].append(("nft", "installed"))
                return _listing(stdin)
            if "list" in command:
                state["events"].append(("nft", "listed"))
                if state["failure"] == "list":
                    raise RuntimeError("fixture list failure")
                if state["connected"] and state["listing_after_attach"] is not None:
                    return state["listing_after_attach"]
                return state["listing_override"] or _listing(state["rules"])
            raise AssertionError(f"unexpected nft command: {command}")
        raise AssertionError(f"unexpected daemon operation: {command}")

    async def compose(self, command, **kwargs):
        state["events"].append(("compose", *command))
        if command[0] == "config":
            Path(command[-1]).write_text(json.dumps(state["compose"]))
        elif command[0] == "up":
            state["up"] = True
        elif command[0] == "ps":
            return ExecResult(stdout=CID, stderr=None, return_code=0)
        elif command[0] == "exec":
            assert "--privileged" not in command
            state["ordinary_exec_caps"].append(state["inspect"]["HostConfig"]["CapDrop"][:])
        elif command[0] == "down":
            state["deleted"] = True
        return ExecResult(stdout=None, stderr=None, return_code=0)

    async def start(self, force_build):
        self._use_prebuilt = True
        await self._run_docker_compose_command(["up", "--detach", "--wait"])
        # The native startup may issue trusted setup exec while still in NONE.
        await self._run_docker_compose_command(["exec", "main", "true"])

    monkeypatch.setattr(HostIsolatedDockerEnvironment, "_prepare_tool", prepare)
    monkeypatch.setattr(HostIsolatedDockerEnvironment, "_daemon_output", daemon)
    monkeypatch.setattr(DockerEnvironment, "_run_docker_compose_command", compose)
    monkeypatch.setattr(DockerEnvironment, "start", start)
    monkeypatch.setattr(
        host_isolation, "controller_addresses", lambda: HOST_ADDRESSES, raising=False
    )

    def make(**overrides):
        arguments = {
            "environment_dir": environment_dir,
            "environment_name": "fixture",
            "session_id": "fixture__env",
            "trial_paths": paths,
            "task_env_config": config,
            "mounts": mounts,
        }
        arguments.update(overrides)
        return HostIsolatedDockerEnvironment(**arguments)

    state["make"] = make
    return state


def test_filter_is_installed_and_verified_before_public_network_attachment(native):
    env = native["make"](isolation_lane="codex")
    asyncio.run(env.start(force_build=False))

    events = native["events"]
    assert events.index(("nft", "installed")) < events.index(("nft", "listed"))
    disconnect = next(
        index
        for index, item in enumerate(events)
        if item[:3] == ("daemon", "network", "disconnect")
    )
    connect = next(
        index for index, item in enumerate(events) if item[:3] == ("daemon", "network", "connect")
    )
    assert events.index(("nft", "listed")) < disconnect < connect
    assert _record(native)["applied"] is True
    assert _record(native)["recorded_at"] == "2026-10-13T03:00:00+00:00"
    assert _record(native)["startup_evidence"]["HostConfig"]["NetworkMode"] == "none"
    assert _record(native)["tool_image_digest"] == "sha256:fixture"
    assert native["ordinary_exec_caps"] == [["NET_ADMIN", "NET_RAW"]]
    assert native["tool_path"] is not None and not native["tool_path"].exists()
    assert not any(item[:2] in (("daemon", "run"), ("daemon", "create")) for item in events)
    asyncio.run(env.stop(delete=True))
    assert native["deleted"] and env._network_id is None


@pytest.mark.parametrize("lane", ["nop", "oracle", "codex"])
def test_non_claude_lanes_never_receive_a_host_endpoint_exception(native, lane):
    env = native["make"](isolation_lane=lane)
    asyncio.run(env.start(force_build=False))
    record = _record(native)
    assert record["bridge_port"] is None and record["bridge_addresses"] == []
    assert "dport" not in record["rules"]
    asyncio.run(env.stop(delete=True))


def test_claude_lane_admits_only_its_explicit_bridge_tuple(native):
    env = native["make"](isolation_lane="claude-code", claude_bridge_port=45123)
    asyncio.run(env.start(force_build=False))
    record = _record(native)
    assert record["bridge_port"] == 45123
    assert record["bridge_addresses"] == list(BRIDGE_ADDRESSES)
    assert "ip daddr 192.168.65.254 tcp dport 45123 accept" in record["rules"]
    assert "udp" not in record["rules"]
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize("lane", ["nop", "oracle", "codex"])
def test_host_bridge_configuration_is_refused_outside_claude_before_docker(native, lane):
    with pytest.raises(ValueError, match="only the Claude"):
        native["make"](isolation_lane=lane, claude_bridge_port=45123)
    assert native["events"] == []


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_invalid_bridge_port_is_refused_before_docker(native, port):
    with pytest.raises(ValueError):
        native["make"](isolation_lane="claude-code", claude_bridge_port=port)
    assert native["events"] == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cap_drop", ["NET_RAW"]),
        ("entrypoint", ["/app/untrusted-startup"]),
        ("command", ["/app/untrusted-command"]),
        ("healthcheck", {"test": ["CMD", "/app/untrusted-healthcheck"]}),
        ("user", "1000"),
        ("privileged", True),
        ("cap_add", ["NET_ADMIN"]),
        ("network_mode", "host"),
    ],
)
def test_creation_overrides_are_refused_before_any_sandbox_starts(native, key, value):
    native["compose"]["services"]["main"][key] = value
    with pytest.raises(RuntimeError):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["up"] is False
    assert _record(native)["applied"] is False
    assert ("nft", "installed") not in native["events"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("HostConfig.CapDrop", ["NET_RAW"]),
        ("Config.Entrypoint", ["/app/untrusted-startup"]),
        ("Config.Cmd", ["/app/untrusted-command"]),
        ("Config.Healthcheck", {"Test": ["CMD", "/app/untrusted-healthcheck"]}),
        ("Config.User", "1000"),
        ("HostConfig.NetworkMode", "host"),
    ],
)
def test_daemon_creation_mismatch_refuses_privileged_install_and_cleans(native, path, value):
    _change(native["inspect"], path, value)
    with pytest.raises(RuntimeError):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["deleted"]
    assert _record(native)["applied"] is False
    assert ("nft", "installed") not in native["events"]
    assert native["ordinary_exec_caps"] == []


@pytest.mark.parametrize("failure", ["prepare", "install", "list", "disconnect", "connect"])
def test_lifecycle_failures_record_unapplied_and_delete_owned_resources(native, failure):
    native["failure"] = failure
    env = native["make"]()
    with pytest.raises(RuntimeError, match=f"fixture {failure} failure"):
        asyncio.run(env.start(force_build=False))
    assert _record(native)["applied"] is False
    assert _record(native)["error_type"] == "RuntimeError"
    assert native["deleted"]
    assert env._network_id is None
    assert env._tool_temp is None
    assert not env._isolation_confirmed


@pytest.mark.parametrize(
    "listing",
    [
        {"nftables": []},
        {"nftables": [{"table": {"family": "inet", "name": "unrelated_table"}}]},
        {"nftables": [{"table": {"family": "inet", "name": "evallab_host_isolation"}}]},
    ],
)
def test_nonempty_listing_without_the_expected_boundary_never_admits_network(native, listing):
    native["listing_override"] = json.dumps(listing)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(native["make"]().start(force_build=False))
    assert not native["connected"]
    assert _record(native)["applied"] is False
    assert native["deleted"]


def test_rule_change_during_attachment_refuses_the_finished_sandbox(native):
    native["listing_after_attach"] = json.dumps({"nftables": [{"table": {"name": "changed"}}]})
    with pytest.raises(RuntimeError, match="rules changed"):
        asyncio.run(native["make"]().start(force_build=False))
    assert _record(native)["applied"] is False
    assert native["deleted"]


@pytest.mark.parametrize(
    "mutation", ["wrong-hook", "wrong-priority", "missing-denial", "changed-verdict"]
)
def test_kernel_listing_must_match_the_installed_boundary(native, mutation):
    listing = json.loads(_listing(host_isolation.nft_rules(HOST_ADDRESSES)))
    statements = listing["nftables"]
    if mutation == "wrong-hook":
        statements[2]["chain"]["hook"] = "input"
    elif mutation == "wrong-priority":
        statements[2]["chain"]["prio"] = 0
    elif mutation == "missing-denial":
        statements.pop()
    else:
        statements[-1]["rule"]["expr"][-1] = {"accept": None}
    native["listing_override"] = json.dumps(listing)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(native["make"]().start(force_build=False))
    assert not native["connected"]
    assert _record(native)["applied"] is False
    assert native["deleted"]


def test_native_prefixed_capability_drop_names_are_accepted(native):
    native["inspect"]["HostConfig"]["CapDrop"] = ["CAP_NET_ADMIN", "CAP_NET_RAW"]
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    assert _record(native)["applied"] is True
    assert native["ordinary_exec_caps"] == [["CAP_NET_ADMIN", "CAP_NET_RAW"]]
    asyncio.run(env.stop(delete=True))


def test_no_network_baseline_retains_none_without_a_public_attachment(native):
    policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
    env = native["make"](network_policy=policy)
    asyncio.run(env.start(force_build=False))
    assert not native["connected"]
    assert _record(native)["applied"] is True
    assert not any(item[:3] == ("daemon", "network", "connect") for item in native["events"])
    asyncio.run(env.stop(delete=True))


def test_final_overlay_disables_untrusted_image_startup_and_capabilities(native):
    env = native["make"]()
    overlay = json.loads(env._docker_compose_paths[-1].read_text())["services"]["main"]
    assert overlay["network_mode"] == "none"
    assert overlay["entrypoint"] == ["/bin/sh", "-c", "exec sleep infinity"]
    assert overlay["command"] == []
    assert overlay["healthcheck"] == {"disable": True}
    assert set(overlay["cap_drop"]) >= {"NET_ADMIN", "NET_RAW"}
    assert overlay["security_opt"] == ["no-new-privileges:true"]
    assert overlay["user"] == "0"
    asyncio.run(env.stop(delete=True))


def test_creation_evidence_is_retained_without_mutating_the_daemon_fixture(native):
    initial = copy.deepcopy(native["inspect"])
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    record = _record(native)
    assert record["startup_evidence"]["HostConfig"] == {
        key: initial["HostConfig"].get(key) for key in record["startup_evidence"]["HostConfig"]
    }
    asyncio.run(env.stop(delete=True))


_DEPENDENCIES = """\
/lib/ld-musl-aarch64.so.1 (0x0001)
libnftables.so.1 => /usr/lib/libnftables.so.1 (0x0002)
libmnl.so.0 => /usr/lib/libmnl.so.0 (0x0003)
"""
_DESKTOP_DNS = """\
Server: 192.168.65.7
Address: 192.168.65.7#53

Name: host.docker.internal
Address: 192.168.65.254
"""


@pytest.fixture
def tool(native, monkeypatch: pytest.MonkeyPatch):
    """Exercise real preparation using an injected local Docker protocol."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    runtime_daemon = HostIsolatedDockerEnvironment._daemon_output
    make_environment = native["make"]
    state = {
        "context_endpoint": "unix:///fixture/docker.sock",
        "output": _DEPENDENCIES + _DESKTOP_DNS,
        "failure": None,
        "calls": [],
        "helper_alive": False,
        "helper_id": "fixture-tool-helper",
        "tag": None,
        "removed_images": [],
        "copied": [],
        "environments": [],
    }

    async def daemon(self, *command, stdin=None):
        operation = command[0]
        is_tool_operation = (
            operation in ("context", "build", "create", "start", "rm")
            or (operation == "image")
            or (operation == "cp" and command[1:2] == ("-L",))
        )
        if not is_tool_operation:
            if operation == "info":
                assert not state["helper_alive"], "helper exists when sandbox creation begins"
            return await runtime_daemon(self, *command, stdin=stdin)
        state["calls"].append(command)
        native["events"].append(("tool-daemon", *command))
        if command[:2] == ("context", "inspect"):
            return json.dumps(state["context_endpoint"])
        if command[:2] == ("image", "inspect"):
            if state["failure"] == "base-inspect" and state["tag"] is None:
                raise RuntimeError("fixture base-inspect failure")
            return json.dumps([{"Id": "sha256:fixture-tool-image"}])
        if operation == "build":
            state["tag"] = command[command.index("--tag") + 1]
            if state["failure"] == "build":
                raise RuntimeError("fixture build failure")
            return ""
        if operation == "create":
            assert native["up"] is False and not state["helper_alive"]
            if state["failure"] == "create":
                raise RuntimeError("fixture create failure")
            state["helper_alive"] = True
            return state["helper_id"]
        if operation == "start":
            assert state["helper_alive"] and native["up"] is False
            if state["failure"] == "start":
                raise RuntimeError("fixture start failure")
            return state["output"]
        if operation == "cp":
            assert state["helper_alive"] and native["up"] is False
            if state["failure"] == "copy":
                raise RuntimeError("fixture copy failure")
            source, destination = command[-2:]
            state["copied"].append((source, destination))
            Path(destination).write_bytes(b"fixture binary")
            return ""
        if operation == "rm":
            assert command == ("rm", "--force", state["helper_id"])
            state["helper_alive"] = False
            return ""
        if command[:2] == ("image", "rm"):
            state["removed_images"].append(command[-1])
            return ""
        raise AssertionError(f"unexpected tool operation: {command}")

    def make(**overrides):
        environment = make_environment(**overrides)
        state["environments"].append(environment)
        return environment

    monkeypatch.setattr(HostIsolatedDockerEnvironment, "_prepare_tool", _PREPARE_TOOL)
    monkeypatch.setattr(HostIsolatedDockerEnvironment, "_daemon_output", daemon)
    native["make"] = make
    state["native"] = native
    yield state
    for environment in state["environments"]:
        if environment._tool_temp is not None:
            environment._tool_temp.cleanup()
        if environment._lock_temp_dir is not None:
            environment._lock_temp_dir.cleanup()


def test_tool_helper_and_unique_image_are_removed_before_sandbox_creation(tool):
    native = tool["native"]
    env = native["make"](isolation_lane="claude-code", claude_bridge_port=45123)
    asyncio.run(env.start(force_build=False))

    helper_removed = next(
        index for index, event in enumerate(native["events"]) if event[:2] == ("tool-daemon", "rm")
    )
    image_removed = next(
        index
        for index, event in enumerate(native["events"])
        if event[:3] == ("tool-daemon", "image", "rm")
    )
    sandbox_started = next(
        index for index, event in enumerate(native["events"]) if event[:2] == ("compose", "up")
    )
    assert helper_removed < image_removed < sandbox_started
    assert not tool["helper_alive"]
    assert tool["removed_images"] == [tool["tag"]]
    assert tool["tag"].startswith("evallab-isolation-tools:")
    assert env._bridge_addresses == BRIDGE_ADDRESSES
    assert _record(native)["applied"] is True
    assert _record(native)["tool_image_digest"] == "sha256:fixture-tool-image"
    create = next(command for command in tool["calls"] if command[0] == "create")
    assert create[create.index("--name") + 1].startswith("evallab-isolation-helper-")
    assert create[create.index("--entrypoint") + 1] == "/bin/sh"
    assert len([command for command in tool["calls"] if command[0] == "create"]) == 1
    assert not any(command[0] in ("run", "pull") for command in tool["calls"])
    build = next(command for command in tool["calls"] if command[0] == "build")
    assert "--pull=false" in build
    assert not any(command[:2] == ("image", "pull") for command in tool["calls"])
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://remote.example.invalid:2375",
        "ssh://remote.example.invalid",
        "npipe://fixture",
        "",
        None,
    ],
)
def test_nonlocal_docker_context_refuses_tool_build_or_helper(tool, endpoint):
    tool["context_endpoint"] = endpoint
    native = tool["native"]
    with pytest.raises(RuntimeError, match="local Unix Docker"):
        asyncio.run(native["make"]().start(force_build=False))
    assert tool["calls"] == [("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")]
    assert not tool["helper_alive"] and native["up"] is False
    assert _record(native)["applied"] is False


def test_remote_docker_host_environment_refuses_before_context_build_or_helper(tool, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example.invalid:2375")
    with pytest.raises(RuntimeError, match="local Unix Docker"):
        asyncio.run(tool["native"]["make"]().start(force_build=False))
    assert tool["calls"] == []
    assert tool["native"]["up"] is False


def test_local_docker_host_environment_does_not_depend_on_current_context(tool, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/explicit.sock")
    tool["context_endpoint"] = "tcp://remote.example.invalid:2375"
    env = tool["native"]["make"]()
    asyncio.run(env.start(force_build=False))
    assert not any(command[0] == "context" for command in tool["calls"])
    assert _record(tool["native"])["applied"] is True
    asyncio.run(env.stop(delete=True))


def test_docker_context_precedence_does_not_admit_remote_context_via_local_host(tool, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-fixture")
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/explicit.sock")
    tool["context_endpoint"] = "ssh://remote.example.invalid"
    with pytest.raises(RuntimeError, match="local Unix Docker"):
        asyncio.run(tool["native"]["make"]().start(force_build=False))
    assert not any(command[0] in ("build", "create") for command in tool["calls"])


def test_explicit_local_context_takes_precedence_over_remote_host_environment(tool, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "local-fixture")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example.invalid:2375")
    env = tool["native"]["make"]()
    asyncio.run(env.start(force_build=False))
    assert any(command[0] == "context" for command in tool["calls"])
    assert _record(tool["native"])["applied"] is True
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize("failure", ["build", "create", "start", "copy"])
def test_tool_preparation_failures_delete_only_the_unique_image_and_helper(tool, failure):
    tool["failure"] = failure
    native = tool["native"]
    env = native["make"]()
    with pytest.raises(RuntimeError, match=f"fixture {failure} failure"):
        asyncio.run(env.start(force_build=False))
    assert tool["removed_images"] == [tool["tag"]]
    assert tool["tag"].startswith("evallab-isolation-tools:")
    assert not tool["helper_alive"]
    assert native["up"] is False
    assert _record(native)["applied"] is False
    assert env._tool_temp is None


def test_missing_preloaded_base_is_refused_without_pulling_or_building(tool):
    tool["failure"] = "base-inspect"
    with pytest.raises(RuntimeError, match="fixture base-inspect failure"):
        asyncio.run(tool["native"]["make"]().start(force_build=False))
    assert not any(command[0] in ("build", "create", "pull") for command in tool["calls"])
    assert tool["removed_images"] == []
    assert tool["native"]["up"] is False


def test_matched_dependency_bundle_includes_loader_libraries_and_nft(tool):
    env = tool["native"]["make"]()
    asyncio.run(env.start(force_build=False))
    copied = tool["copied"]
    assert {source.split(":", 1)[1] for source, _destination in copied} == {
        "/lib/ld-musl-aarch64.so.1",
        "/usr/lib/libnftables.so.1",
        "/usr/lib/libmnl.so.0",
        "/usr/sbin/nft",
    }
    assert all(
        Path(destination).name == Path(source.split(":", 1)[1]).name
        for source, destination in copied
    )
    assert env._nft_command == [
        "/opt/evallab-host-isolation/ld-musl-aarch64.so.1",
        "--library-path",
        "/opt/evallab-host-isolation",
        "/opt/evallab-host-isolation/nft",
    ]
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize(
    "output",
    [
        "",
        "libnftables.so.1 => /usr/lib/libnftables.so.1 (0x0002)\n",
        _DEPENDENCIES + "/lib/ld-musl-other.so.1 (0x0004)\n",
        _DEPENDENCIES + "unsafe.so => /tmp/unsafe.so (0x0004)\n",
        _DEPENDENCIES + "unsafe.so => /usr/lib/../../unsafe.so (0x0004)\n",
        _DEPENDENCIES + "unsafe.so => /usr/lib/unsafe;payload.so (0x0004)\n",
    ],
)
def test_missing_or_unsafe_dependency_inventory_refuses_sandbox_creation(tool, output):
    tool["output"] = output
    native = tool["native"]
    with pytest.raises(RuntimeError, match="dependency"):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["up"] is False
    assert not tool["helper_alive"]
    assert tool["removed_images"] == [tool["tag"]]
    assert _record(native)["applied"] is False


def test_claude_tool_preparation_requires_actual_desktop_host_dns_answers(tool):
    tool["output"] = _DEPENDENCIES + "Server: 192.168.65.7\nAddress: 192.168.65.7#53\n"
    native = tool["native"]
    with pytest.raises(ValueError, match="DNS answers"):
        asyncio.run(
            native["make"](isolation_lane="claude-code", claude_bridge_port=45123).start(
                force_build=False
            )
        )
    assert native["up"] is False
    assert not tool["helper_alive"]
    assert tool["removed_images"] == [tool["tag"]]


def test_dual_stack_desktop_dns_admits_only_the_actual_ipv4_bridge_frontend(tool):
    tool["output"] = (
        _DEPENDENCIES
        + _DESKTOP_DNS
        + "Name: host.docker.internal\nAddress: 2606:4700::1111\n"
    )
    native = tool["native"]
    env = native["make"](isolation_lane="claude-code", claude_bridge_port=45123)
    asyncio.run(env.start(force_build=False))
    record = _record(native)
    assert env._bridge_addresses == BRIDGE_ADDRESSES
    assert record["bridge_addresses"] == list(BRIDGE_ADDRESSES)
    assert "ip daddr 192.168.65.254 tcp dport 45123 accept" in record["rules"]
    assert "ip6 daddr 2606:4700::1111 reject" in record["rules"]
    assert "ip6 daddr 2606:4700::1111 tcp dport 45123 accept" not in record["rules"]
    assert tool["removed_images"] == [tool["tag"]] and not tool["helper_alive"]
    asyncio.run(env.stop(delete=True))


def test_ipv6_only_desktop_dns_refuses_ipv4_bridge_and_cleans_tool_resources(tool):
    tool["output"] = (
        _DEPENDENCIES + "Name: host.docker.internal\nAddress: 2606:4700::1111\n"
    )
    native = tool["native"]
    env = native["make"](isolation_lane="claude-code", claude_bridge_port=45123)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(env.start(force_build=False))
    assert native["up"] is False and not native["connected"]
    assert _record(native)["applied"] is False
    assert not tool["helper_alive"]
    assert tool["removed_images"] == [tool["tag"]]
    assert env._tool_temp is None
