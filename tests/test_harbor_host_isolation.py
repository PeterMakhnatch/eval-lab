"""Injected Docker lifecycle checks; no daemon, service, or credential probes."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
        _ISOLATION_SUBNET_CANDIDATES,
        ISOLATION_RECORD,
        HostIsolatedDockerEnvironment,
    )
except ModuleNotFoundError as error:
    if not (error.name == "harbor" or (error.name or "").startswith("harbor.")):
        raise
    ExecResult = DockerEnvironment = EnvironmentConfig = NetworkMode = NetworkPolicy = None
    TrialPaths = HostIsolatedDockerEnvironment = None
    ISOLATION_RECORD = "host-isolation.json"
    _ISOLATION_SUBNET_CANDIDATES = ()


_PREPARE_TOOL = (
    HostIsolatedDockerEnvironment._prepare_tool
    if HostIsolatedDockerEnvironment is not None
    else None
)
# The fixture replaces ``_daemon_output`` on the class; these tests exercise the
# real daemon protocol against an injected subprocess.
_DAEMON_OUTPUT = (
    HostIsolatedDockerEnvironment._daemon_output
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
            state["inspect"]["NetworkSettings"]["Networks"] = {}
            state["connected"] = False
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
            assert state["up"]
            if "--privileged" not in command:
                # Phase-switch interface census: unprivileged operstate view.
                assert command[1:3] == (CID, "sh")
                if state["failure"] == "interfaces":
                    return "eth0 up\nlo unknown\n"
                if state["connected"]:
                    return "eth0 up\nlo unknown\n"
                return "lo unknown\nsit0 down\ntunl0 down\n"
            if "--file" in command or "-f" in command:
                assert not state["connected"]
                if state["failure"] == "install":
                    raise RuntimeError("fixture install failure")
                state["rules"] = stdin
                state["installed"] = True
                # A reinstall restores the canonical kernel state only when the
                # test models healable drift; a persistently wrong kernel
                # listing must keep failing verification.
                if state.get("heal_on_reinstall"):
                    state["listing_override"] = None
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
            return ExecResult(
                stdout=state.get("container_alias", CID), stderr=None, return_code=0
            )
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

def _v5_agent_env(native, **overrides):
    """The mimo-clean-v5 agent shape: public baseline, no-network agent phase."""
    arguments = {
        "network_policy": NetworkPolicy(network_mode=NetworkMode.PUBLIC),
        # Shared-verifier trials also repeat the public baseline; repeats are
        # native no-ops and must not affect the switch allowance.
        "phase_network_policies": [
            NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
            NetworkPolicy(network_mode=NetworkMode.PUBLIC),
        ],
    }
    arguments.update(overrides)
    return native["make"](**arguments)


def test_v5_agent_phase_switch_roundtrip_preserves_boundary(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    assert native["connected"]

    asyncio.run(
        env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
    )
    assert not native["connected"]
    assert native["inspect"]["NetworkSettings"]["Networks"] == {}

    asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    assert native["connected"]

    record = _record(native)
    assert record["applied"] is True
    assert record["declared_phase_policies"] == [
        {"network_mode": "no-network", "allowed_hosts": []},
        {"network_mode": "public", "allowed_hosts": []},
    ]
    transitions = record["phase_transitions"]
    assert [transition["policy"] for transition in transitions] == [
        "no-network",
        "public",
    ]
    assert transitions[0]["networks"] == []
    assert transitions[0]["interfaces"] == ["lo", "sit0", "tunl0"]
    assert transitions[0]["interface_states"] == {
        "lo": "unknown",
        "sit0": "down",
        "tunl0": "down",
    }
    assert transitions[1]["networks"] == [env._network_name]
    assert transitions[1]["rules_confirmed"] is True
    # Disconnect/reconnect bracket the boundary install, never precede it.
    events = native["events"]
    assert events.index(("nft", "installed")) < events.index(
        ("daemon", "network", "disconnect", env._network_id, CID)
    )
    asyncio.run(env.stop(delete=True))


def test_no_network_baseline_can_attach_public_phase(native):
    env = native["make"](
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
        phase_network_policies=[NetworkPolicy(network_mode=NetworkMode.PUBLIC)],
    )
    asyncio.run(env.start(force_build=False))
    assert not native["connected"]
    asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    assert native["connected"]
    transitions = _record(native)["phase_transitions"]
    assert [transition["policy"] for transition in transitions] == ["public"]
    assert transitions[0]["rules_confirmed"] is True
    asyncio.run(env.stop(delete=True))


def test_repeated_baseline_policy_is_a_native_noop(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    before = list(native["events"])
    asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    assert native["events"] == before
    assert _record(native)["phase_transitions"] == []
    asyncio.run(env.stop(delete=True))


def test_allowlist_phase_policy_is_refused_before_docker(native):
    allowlist = NetworkPolicy(
        network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["example.com"]
    )
    with pytest.raises(ValueError, match="no-network/public pair"):
        native["make"](
            network_policy=NetworkPolicy(network_mode=NetworkMode.PUBLIC),
            phase_network_policies=[allowlist],
        )
    assert native["events"] == []


def test_runtime_allowlist_switch_is_refused(native):
    env = native["make"](isolation_lane="codex")
    asyncio.run(env.start(force_build=False))
    with pytest.raises(ValueError, match="allowlist"):
        asyncio.run(
            env.set_network_policy(
                NetworkPolicy(
                    network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["example.com"]
                )
            )
        )
    assert native["connected"]
    asyncio.run(env.stop(delete=True))


def test_phase_switch_before_confirmation_is_refused(native):
    env = _v5_agent_env(native)
    with pytest.raises(RuntimeError, match="before confirmation"):
        asyncio.run(
            env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
        )
    assert native["events"] == []


def test_boundary_reinstalled_before_reconnect(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    asyncio.run(
        env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
    )
    assert sum(1 for event in native["events"] if event == ("nft", "installed")) == 1
    # The detached kernel state no longer matches the remembered listing, but
    # a reinstall restores it before the public attachment returns.
    native["listing_override"] = "drifted"
    native["heal_on_reinstall"] = True
    asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    assert sum(1 for event in native["events"] if event == ("nft", "installed")) == 2
    assert native["connected"]
    assert [t["policy"] for t in _record(native)["phase_transitions"]] == [
        "no-network",
        "public",
    ]
    asyncio.run(env.stop(delete=True))


def test_no_network_phase_with_unexpected_interface_is_refused(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    native["failure"] = "interfaces"
    with pytest.raises(RuntimeError, match="unexpected interfaces"):
        asyncio.run(
            env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
        )
    assert not native["connected"]
    asyncio.run(env.stop(delete=True))

def test_separate_verifier_environment_keeps_its_own_record(native):
    """A second container sharing the trial dir must not overwrite agent evidence."""
    native["inspect"]["Config"]["Labels"]["com.docker.compose.project"] = "trial-env"
    agent = _v5_agent_env(native, session_id="trial-env")
    asyncio.run(agent.start(force_build=False))
    asyncio.run(
        agent.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
    )

    verifier_cid = "c" * 64
    native["container_alias"] = verifier_cid
    native["inspect"]["Id"] = verifier_cid
    native["inspect"]["Config"]["Labels"]["com.docker.compose.project"] = (
        "trial-verifier-v1"
    )
    verifier = native["make"](
        session_id="trial-verifier-v1",
        network_policy=NetworkPolicy(network_mode=NetworkMode.PUBLIC),
        phase_network_policies=[NetworkPolicy(network_mode=NetworkMode.PUBLIC)],
    )
    asyncio.run(verifier.start(force_build=False))

    primary = _record(native)
    assert primary["session_id"] == "trial-env"
    assert primary["container_id"] == CID
    assert [t["policy"] for t in primary["phase_transitions"]] == ["no-network"]
    sibling = native["paths"].trial_dir / "host-isolation-trial-verifier-v1.json"
    assert sibling.is_file()
    other = json.loads(sibling.read_text())
    assert other["session_id"] == "trial-verifier-v1"
    assert other["container_id"] == verifier_cid
    assert other["phase_transitions"] == []
    asyncio.run(agent.stop(delete=True))
    asyncio.run(verifier.stop(delete=True))


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


@pytest.fixture
def native_setup(native, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Native Docker exec/env merging over a real local shell transport."""
    from harbor.models.task.config import HealthcheckConfig

    import evallab.harbor_host_isolation as wrapper

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tests_dir = tmp_path / "image tests"
    state_dir = tmp_path / "image state"
    grade_dir = tmp_path / "image grade"
    setup_dir = native["environment_dir"] / "setup"
    setup_dir.mkdir()
    hidden_dir = tmp_path / "hidden-tests"
    bundle = hidden_dir / wrapper._SETUP_BUNDLE_NAME
    bundle.mkdir(parents=True)
    (hidden_dir / "assertions.py").write_text("hidden assertion sentinel\n")
    setup = f"""set -eu
mkdir -p {shlex.quote(str(state_dir))}
[ ! -f {shlex.quote(str(state_dir / "ready"))} ] || exit 0
python3 - <<'PY'
from __future__ import annotations
import os
assert os.environ["SETUP_TEST_INTERPRETER"] == "helper"
assert os.environ["PIP_NO_CACHE_DIR"] == "1"
print("supported helper completed cleanup")
PY
rm -rf {shlex.quote(str(tests_dir))}
printf "VALUE = 'baseline'\\n" > {shlex.quote(str(workdir / "task_dependency.py"))}
touch {shlex.quote(str(state_dir / "ready"))}
"""
    (setup_dir / "setup.sh").write_text(setup)
    (bundle / "setup.sh").write_text(setup)
    task_paths = {}
    for name, isolated in (("task", False), ("helper", True)):
        binary_dir = tmp_path / f"{name}-bin"
        binary_dir.mkdir()
        binary = binary_dir / "python3"
        binary.write_text(
            "#!/bin/sh\n"
            f"export SETUP_TEST_INTERPRETER={name}\n"
            f"exec {shlex.quote(sys.executable)} {'-S ' if isolated else ''}\"$@\"\n"
        )
        binary.chmod(0o755)
        task_paths[name] = f"{binary_dir}:/usr/bin:/bin"
    monkeypatch.setattr(
        wrapper, "SETUP_HELPER_ENV",
        {"PATH": task_paths["helper"], "PIP_NO_CACHE_DIR": "1"},
    )
    monkeypatch.setattr(wrapper, "_TESTS_DIR", str(tests_dir))
    monkeypatch.setattr(wrapper, "MIMO_STATE_DIR", str(state_dir))
    monkeypatch.setattr(wrapper, "V2_GRADE_DIR", str(grade_dir))
    compose = DockerEnvironment._run_docker_compose_command

    async def local_compose(self, command, **kwargs):
        result = await compose(self, command, **kwargs)
        if command[0] != "exec" or command == ["exec", "main", "true"]:
            return result
        # Do not replace exec/_merge_env: consume the real SDK's Compose argv.
        assert self._isolation_confirmed and _record(native)["applied"] is True
        main = command.index("main")
        child_env = dict(os.environ)
        child_env.pop("PYTHONPATH", None)
        child_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        cwd = workdir
        options = iter(command[1:main])
        for option in options:
            value = next(options)
            if option == "-e":
                key, _, value = value.partition("=")
                child_env[key] = value
            elif option == "-w":
                cwd = Path(value)
            else:
                assert option == "-u"
        completed = subprocess.run(
            command[main + 1:],
            cwd=cwd,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=kwargs.get("timeout_sec") or 30,
            check=False,
        )
        return ExecResult(
            stdout=completed.stdout, stderr=completed.stderr,
            return_code=completed.returncode,
        )

    monkeypatch.setattr(DockerEnvironment, "_run_docker_compose_command", local_compose)

    def make(*, separate: bool):
        if separate:
            shutil.copytree(hidden_dir, tests_dir)
        config = native["config"].model_copy(update={
            "workdir": str(workdir),
            "env": {
                "PATH": task_paths["task"], "PIP_NO_CACHE_DIR": "task-cache",
                "TASK_SETTING": "preserved",
            },
            "build_timeout_sec": 30,
            "healthcheck": HealthcheckConfig(
                command=f"bash {shlex.quote(str(setup_dir / 'setup.sh'))}",
                retries=0,
            ),
        })
        return native["make"](
            environment_dir=hidden_dir if separate else native["environment_dir"],
            task_env_config=config, enable_environment_dir_upload=not separate,
            isolation_lane="nop",
        )

    return SimpleNamespace(
        make=make, native=native, workdir=workdir, tests_dir=tests_dir,
        state_dir=state_dir, hidden_dir=hidden_dir, bundle=bundle,
        setup_dir=setup_dir,
    )


@pytest.mark.parametrize("separate", [False, True])
def test_native_docker_setup_keeps_task_dependencies_and_seeded_changes(
    native_setup, separate: bool,
):
    runtime = native_setup
    env = runtime.make(separate=separate)
    original = {
        path.relative_to(runtime.tests_dir): path.read_bytes()
        for path in runtime.tests_dir.rglob("*") if path.is_file()
    }

    async def exercise():
        await env.start(force_build=False)
        if not separate:
            await env.run_healthcheck()
        assert (runtime.state_dir / "ready").is_file()
        assert {
            path.relative_to(runtime.tests_dir): path.read_bytes()
            for path in runtime.tests_dir.rglob("*") if path.is_file()
        } == original
        (runtime.workdir / "task_dependency.py").write_text("VALUE = 'submitted'\n")
        (runtime.workdir / "test_runtime.py").write_text(
            "import os, task_dependency\n"
            "def test_submitted_change_and_task_environment():\n"
            "    assert task_dependency.VALUE == 'submitted'\n"
            "    assert os.environ['SETUP_TEST_INTERPRETER'] == 'task'\n"
            "    assert os.environ['PIP_NO_CACHE_DIR'] == 'task-cache'\n"
            "    assert os.environ['TASK_SETTING'] == 'preserved'\n"
        )
        result = await env.exec("python3 -m pytest -q -p no:cacheprovider test_runtime.py")
        assert result.return_code == 0, (result.stdout, result.stderr)
        # The unchanged grader calls setup again after Harbor seeds artifacts.
        setup_path = (runtime.state_dir if separate else runtime.setup_dir) / "setup.sh"
        result = await env.exec(f"bash {shlex.quote(str(setup_path))}")
        assert result.return_code == 0, (result.stdout, result.stderr)
        result = await env.exec("python3 -m pytest -q -p no:cacheprovider test_runtime.py")
        assert result.return_code == 0, (result.stdout, result.stderr)
        await env.stop(delete=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [0, 17])
def test_native_docker_verifier_setup_failure_restores_tests_and_cleans_up(
    native_setup, status: int,
):
    runtime = native_setup
    (runtime.bundle / "setup.sh").write_text(
        f"rm -rf {shlex.quote(str(runtime.tests_dir))}\n"
        f"echo setup-failed >&2\nexit {status}\n"
    )
    env = runtime.make(separate=True)
    original = {
        path.relative_to(runtime.tests_dir): path.read_bytes()
        for path in runtime.tests_dir.rglob("*") if path.is_file()
    }
    with pytest.raises(RuntimeError):
        asyncio.run(env.start(force_build=False))
    assert {
        path.relative_to(runtime.tests_dir): path.read_bytes()
        for path in runtime.tests_dir.rglob("*") if path.is_file()
    } == original
    assert runtime.native["deleted"] and _record(runtime.native)["applied"] is False


@pytest.mark.parametrize("kind", ["explicit", "ordinary", "failed-setup"])
def test_native_docker_healthcheck_preserves_task_env_outside_clean_setup(
    native_setup, kind: str,
):
    from harbor.environments.base import HealthcheckError
    from harbor.models.task.config import HealthcheckConfig

    runtime = native_setup
    env = runtime.make(separate=False)
    check_task = HealthcheckConfig(
        command="python3 -c 'import os; "
        'assert os.environ["SETUP_TEST_INTERPRETER"] == "task"; '
        'assert os.environ["PIP_NO_CACHE_DIR"] == "task-cache"'
        "'", retries=0,
    )
    if kind == "ordinary":
        (runtime.setup_dir / "setup.sh").unlink()
        env.task_env_config.healthcheck = check_task
    elif kind == "failed-setup":
        (runtime.setup_dir / "setup.sh").write_text("exit 17\n")

    async def exercise():
        await env.start(force_build=False)
        if kind == "failed-setup":
            with pytest.raises(HealthcheckError):
                await env.run_healthcheck()
        elif kind == "ordinary":
            await env.run_healthcheck()
        else:
            await env.run_healthcheck(check_task)
        result = await env.exec(check_task.command)
        assert result.return_code == 0, (result.stdout, result.stderr)
        await env.stop(delete=True)

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Construction-time refusals and positional argument plumbing.
# ---------------------------------------------------------------------------


def test_claude_lane_reads_its_bridge_port_from_the_environment(native, monkeypatch):
    monkeypatch.setenv("EVALLAB_CLAUDE_GATEWAY_ISOLATION_PORT", "45123")
    env = native["make"](isolation_lane="claude-code")
    assert env._bridge_port == 45123
    assert native["events"] == []


def test_explicit_egress_lock_option_is_refused(native):
    with pytest.raises(ValueError, match="LockedDockerEnvironment for explicit egress_lock"):
        native["make"](egress_lock=True)
    assert native["events"] == []


def test_constructor_accepts_positional_baseline_and_phase_policies(native):
    policy = NetworkPolicy(network_mode=NetworkMode.PUBLIC)
    env = HostIsolatedDockerEnvironment(
        native["environment_dir"],
        "fixture",
        "fixture__env",
        native["paths"],
        native["config"],
        False,
        None,
        [policy],
    )
    assert env._declared_phase_policies == (policy,)
    assert env.network_policy == policy


def test_constructor_requires_a_network_baseline_source(native):
    with pytest.raises(ValueError, match="requires a task environment config"):
        HostIsolatedDockerEnvironment(
            native["environment_dir"], "fixture", "fixture__env", native["paths"]
        )


# ---------------------------------------------------------------------------
# Task Compose parsing fails closed on unusable documents.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ("~\n", None),
        ("- scalar\n- sequence\n", "non-mapping task Compose"),
        ("services: [oops]\n", "without services"),
        ("services:\n  1: {}\n", "non-string service"),
    ],
)
def test_task_compose_service_names_fail_closed(native, document, match):
    (native["environment_dir"] / "docker-compose.yaml").write_text(document)
    env = native["make"]()
    if match is None:
        assert env._task_compose_service_names() == []
    else:
        with pytest.raises(ValueError, match=match):
            env._task_compose_service_names()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("- scalar\n", (False, {})),
        ("services: [oops]\n", (False, {})),
        ("services:\n  1: {}\n", (False, {})),
        ("services:\n  main:\n    networks: [oops]\n", (True, {})),
        ("services:\n  main:\n    networks:\n      net: oops\n", (True, {})),
    ],
)
def test_task_compose_network_meta_ignores_unusable_entries(native, document, expected):
    (native["environment_dir"] / "docker-compose.yaml").write_text(document)
    assert native["make"]()._task_compose_network_meta() == expected


def test_task_compose_network_aliases_must_be_a_list(native):
    (native["environment_dir"] / "docker-compose.yaml").write_text(
        "services:\n  main:\n    networks:\n      net:\n        aliases: alias\n"
    )
    with pytest.raises(ValueError, match="non-list network aliases"):
        native["make"]()._task_compose_network_meta()


def test_resolved_sidecar_must_be_a_mapping(native):
    (native["environment_dir"] / "docker-compose.yaml").write_text(
        "services:\n  sidecar:\n    image: sidecar:1\n"
    )
    native["compose"]["services"]["sidecar"] = "not-a-mapping"
    with pytest.raises(RuntimeError, match="resolved to a non-mapping"):
        asyncio.run(native["make"]().start(force_build=False))
    assert _record(native)["applied"] is False


# ---------------------------------------------------------------------------
# Lock confirmation refuses unusable daemon identity evidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inspected", "match"),
    [
        ({"not": "a list"}, "invalid Docker inspect evidence"),
        ([], "invalid Docker inspect evidence"),
        (["not-a-container"], "invalid Docker inspect evidence"),
        ([{"Id": CID, "Config": {"Labels": {}}}], "non-string service"),
        (
            [{"Id": CID, "Config": {"Labels": {"com.docker.compose.service": "extra"}}}],
            "service identity is unconfirmed",
        ),
    ],
)
def test_confirm_lock_refuses_unusable_daemon_identity(native, inspected, match):
    env = native["make"]()

    async def evidence(*command):
        return inspected

    env._daemon_json = evidence
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(env._confirm_lock())
    assert not env._lock_confirmed


# ---------------------------------------------------------------------------
# Container evidence: every unsafe daemon host setting is refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("Platform", "windows", "identity/OS/running state is unconfirmed"),
        ("State.Running", False, "identity/OS/running state is unconfirmed"),
        ("Id", "c" * 64, "identity/OS/running state is unconfirmed"),
        ("HostConfig", "not-a-mapping", "host/network/mount evidence"),
        ("NetworkSettings", [], "host/network/mount evidence"),
        ("Mounts", {}, "host/network/mount evidence"),
        ("HostConfig.Privileged", True, "privilege/port flags are unsafe"),
        ("HostConfig.Privileged", None, "privilege/port flags are unsafe"),
        ("HostConfig.PublishAllPorts", True, "privilege/port flags are unsafe"),
        ("HostConfig.CapAdd", ["SYS_ADMIN"], "unsafe host access"),
        ("HostConfig.Devices", [{"PathOnHost": "/dev/mem"}], "unsafe host access"),
        ("HostConfig.DeviceRequests", [{"Driver": "nvidia"}], "unsafe host access"),
        ("HostConfig.DeviceCgroupRules", ["c 1:3 rwm"], "unsafe host access"),
        ("HostConfig.VolumesFrom", ["host-root"], "unsafe host access"),
        ("HostConfig.PortBindings", {"80/tcp": [{"HostPort": "80"}]}, "unsafe host access"),
        ("HostConfig.Links", ["host:alias"], "unsafe host access"),
        (
            "HostConfig.ExtraHosts",
            ["host.docker.internal:0.0.0.0"],
            "unsafe host access",
        ),
        ("HostConfig.CgroupParent", "host.slice", "unsafe host access"),
        ("HostConfig.PidMode", "host", "unsafe or unknown Docker PidMode"),
        ("HostConfig.IpcMode", "host", "unsafe or unknown Docker IpcMode"),
        ("HostConfig.UTSMode", "host", "unsafe or unknown Docker UTSMode"),
        ("HostConfig.UsernsMode", "host", "unsafe or unknown Docker UsernsMode"),
        ("HostConfig.CgroupnsMode", "host", "unsafe or unknown Docker CgroupnsMode"),
        ("HostConfig.Runtime", "nvidia", "unsafe or unknown Docker Runtime"),
        ("HostConfig.CapDrop", ["NET_RAW"], "capability drops are unconfirmed"),
        ("HostConfig.SecurityOpt", [], "security options are unsafe"),
        ("HostConfig.SecurityOpt", "no-new-privileges:true", "security options are unsafe"),
        ("HostConfig.SecurityOpt", ["seccomp=unconfined"], "security options are unsafe"),
    ],
)
def test_container_evidence_refuses_unsafe_daemon_settings(native, path, value, match):
    _change(native["inspect"], path, value)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["deleted"]
    assert _record(native)["applied"] is False
    assert ("nft", "installed") not in native["events"]
    assert native["ordinary_exec_caps"] == []


@pytest.mark.parametrize(
    "key",
    [
        "CapAdd",
        "Devices",
        "DeviceRequests",
        "DeviceCgroupRules",
        "VolumesFrom",
        "PortBindings",
        "Links",
        "ExtraHosts",
        "CgroupParent",
    ],
)
def test_missing_host_access_evidence_refuses_the_container(native, key):
    del native["inspect"]["HostConfig"][key]
    with pytest.raises(RuntimeError, match="lacks Docker host-access evidence"):
        asyncio.run(native["make"]().start(force_build=False))
    assert native["deleted"]
    assert _record(native)["applied"] is False
    assert ("nft", "installed") not in native["events"]


# ---------------------------------------------------------------------------
# Phase confirmation guards (single container).
# ---------------------------------------------------------------------------


def test_no_network_confirmation_refuses_a_retained_bridge(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    with pytest.raises(RuntimeError, match="keeps a public attachment"):
        asyncio.run(env._confirm_no_network_state())
    asyncio.run(env.stop(delete=True))


def test_public_confirmation_refuses_a_missing_attachment(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    native["inspect"]["NetworkSettings"]["Networks"] = {}
    with pytest.raises(RuntimeError, match="lacks its bridge attachment"):
        asyncio.run(env._confirm_public_state())
    asyncio.run(env.stop(delete=True))


def test_public_confirmation_refuses_a_changed_boundary(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    native["listing_after_attach"] = json.dumps({"nftables": [{"table": {"name": "changed"}}]})
    with pytest.raises(RuntimeError, match="rules changed during network attachment"):
        asyncio.run(env._confirm_public_state())
    asyncio.run(env.stop(delete=True))


def test_container_network_evidence_must_be_a_mapping(native):
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    native["inspect"]["NetworkSettings"]["Networks"] = []
    with pytest.raises(RuntimeError, match="network evidence is missing"):
        asyncio.run(env._container_networks())
    asyncio.run(env.stop(delete=True))


def test_private_nft_helper_reads_the_confirmed_container(native):
    env = native["make"]()
    asyncio.run(env.start(force_build=False))
    listing = asyncio.run(env._nft("--json", "list", "table", "inet", "evallab_host_isolation"))
    assert "evallab_host_isolation" in listing
    assert any(
        event[:7] == ("daemon", "exec", "--privileged", "--user", "0", "-i", CID)
        for event in native["events"]
    )
    asyncio.run(env.stop(delete=True))


def test_container_identity_refuses_a_foreign_compose_project(native):
    native["inspect"]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
    with pytest.raises(RuntimeError, match="identity/OS/running state is unconfirmed"):
        asyncio.run(native["make"]().start(force_build=False))
    assert _record(native)["applied"] is False


def test_unreadable_existing_record_does_not_claim_another_container(native):
    record_path = native["paths"].trial_dir / ISOLATION_RECORD
    record_path.write_text("{not json")
    env = native["make"]()
    assert env._resolve_record_path() == record_path
    asyncio.run(env.start(force_build=False))
    assert _record(native)["container_id"] == CID
    asyncio.run(env.stop(delete=True))


# ---------------------------------------------------------------------------
# Phase application refuses allowlists and repeated policies directly.
# ---------------------------------------------------------------------------


def test_phase_application_refuses_allowlists_and_ignores_repeats(native):
    env = _v5_agent_env(native)
    asyncio.run(env.start(force_build=False))
    before = list(native["events"])
    with pytest.raises(ValueError, match="refuses network allowlists"):
        asyncio.run(
            env._apply_network_policy(
                NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["example.com"])
            )
        )
    asyncio.run(env._apply_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    assert native["events"] == before
    asyncio.run(env.stop(delete=True))


# ---------------------------------------------------------------------------
# Isolated project subnet allocation.
# ---------------------------------------------------------------------------


def test_isolation_network_retries_the_next_subnet(native):
    env = native["make"]()
    attempts: list[tuple[str, ...]] = []

    async def daemon(*command):
        attempts.append(command)
        if len(attempts) == 1:
            raise RuntimeError("subnet in use")
        return NETWORK_ID + "\n"

    env._daemon_output = daemon
    assert asyncio.run(env._create_isolation_network(internal=True)) == NETWORK_ID
    assert attempts[0][-2:] == (_ISOLATION_SUBNET_CANDIDATES[0], env._network_name)
    assert "--internal" in attempts[0]
    assert attempts[1][-2:] == (_ISOLATION_SUBNET_CANDIDATES[1], env._network_name)
    assert env._isolation_subnet == _ISOLATION_SUBNET_CANDIDATES[1]


def test_exhausted_isolation_subnets_are_refused(native):
    env = native["make"]()
    attempts: list[tuple[str, ...]] = []

    async def daemon(*command):
        attempts.append(command)
        raise RuntimeError("subnet in use")

    env._daemon_output = daemon
    with pytest.raises(RuntimeError, match="could not allocate an isolated project subnet"):
        asyncio.run(env._create_isolation_network(internal=False))
    assert len(attempts) == len(_ISOLATION_SUBNET_CANDIDATES)
    assert env._isolation_subnet is None


# ---------------------------------------------------------------------------
# The real daemon protocol helper, against an injected subprocess.
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int | None = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.stdin_written: bytes | None = None
        self.terminated = False
        self.killed = False

    async def communicate(self, data: bytes | None = None):
        self.stdin_written = data
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def test_daemon_output_pipes_stdin_and_decodes_output(native, monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    process = _FakeProcess(stdout=b"listing\n")

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    env = native["make"]()
    assert asyncio.run(_DAEMON_OUTPUT(env, "exec", "cid", stdin="rules")) == "listing\n"
    argv, kwargs = calls[0]
    assert argv[-2:] == ("exec", "cid")
    assert argv[0] == "docker"
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert process.stdin_written == b"rules"

    calls.clear()
    asyncio.run(_DAEMON_OUTPUT(env, "info"))
    assert calls[0][1]["stdin"] is asyncio.subprocess.DEVNULL


def test_daemon_output_gives_builds_the_longer_timeout(native, monkeypatch):
    timeouts: list[int | float] = []
    process = _FakeProcess()

    async def spawn(*argv, **kwargs):
        return process

    async def wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    env = native["make"]()
    asyncio.run(_DAEMON_OUTPUT(env, "build", "--tag", "fixture"))
    asyncio.run(_DAEMON_OUTPUT(env, "image", "inspect", "fixture"))
    assert timeouts == [180, 30]


def test_daemon_output_refuses_a_failing_command(native, monkeypatch):
    process = _FakeProcess(stderr=b"boom", returncode=17)

    async def spawn(*argv, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    env = native["make"]()
    with pytest.raises(RuntimeError, match=r"Docker build failed \(exit 17\)"):
        asyncio.run(_DAEMON_OUTPUT(env, "build"))


def test_daemon_output_terminates_the_process_when_wait_times_out(native, monkeypatch):
    process = _FakeProcess(returncode=None)

    async def spawn(*argv, **kwargs):
        return process

    async def wait_for(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    env = native["make"]()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_DAEMON_OUTPUT(env, "exec", "cid"))
    assert process.terminated
