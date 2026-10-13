"""Multi-service host isolation: task Compose sidecars locked per container.

Injected Docker lifecycle checks; no daemon, service, or credential probes.
Covers task-provided Compose acceptance in HostIsolatedDockerEnvironment
(CheatBench-style `verifier` sidecars), per-service creation/nft evidence,
isolated-subnet project networks with DNS aliases, and the strict
single-container LockedDockerEnvironment refusal.
"""

from __future__ import annotations

import asyncio
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

    from evallab.harbor_docker import EGRESS_LOCK_RECORD, LockedDockerEnvironment
    from evallab.harbor_host_isolation import (
        ISOLATION_RECORD,
        HostIsolatedDockerEnvironment,
    )
except ModuleNotFoundError as error:
    if not (error.name == "harbor" or (error.name or "").startswith("harbor.")):
        raise
    ExecResult = DockerEnvironment = EnvironmentConfig = NetworkMode = None
    NetworkPolicy = TrialPaths = None
    EGRESS_LOCK_RECORD = "egress-lock.json"
    LockedDockerEnvironment = HostIsolatedDockerEnvironment = None
    ISOLATION_RECORD = "host-isolation.json"


MAIN_CID = "a" * 64
SIDE_CID = "b" * 64
NETWORK_ID = "c" * 64
HOST_ADDRESSES = ("8.8.4.4", "192.168.1.35", "2606:4700::1111")
BRIDGE_ADDRESSES = ("192.168.65.254",)
TASK_COMPOSE = """\
services:
  verifier:
    build:
      context: ./sidecar
    environment:
      CHEATBENCH_FAMILY: prime_factorization
      CHEATBENCH_TASK_ID: "prime_factorization-fact_n52_10"
      CHEATBENCH_STATE_TOKEN: "fixture-token"
      CHEATBENCH_PORT: "8420"
"""


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 13, 3, 0, 0, tzinfo=UTC)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _require_harbor_runtime():
    pytest.importorskip("harbor.environments.docker.docker")


def _record(state) -> dict[str, Any]:
    return json.loads((state["paths"].trial_dir / ISOLATION_RECORD).read_text())


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


def _host_config() -> dict[str, Any]:
    return {
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
    }


@pytest.fixture
def sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _require_harbor_runtime):
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
    (environment_dir / "docker-compose.yaml").write_text(TASK_COMPOSE)
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
    compose_side = {
        "build": {"context": "./sidecar"},
        "environment": {
            "CHEATBENCH_FAMILY": "prime_factorization",
            "CHEATBENCH_TASK_ID": "prime_factorization-fact_n52_10",
            "CHEATBENCH_STATE_TOKEN": "fixture-token",
            "CHEATBENCH_PORT": "8420",
        },
        "network_mode": "none",
        "cap_drop": ["NET_ADMIN", "NET_RAW"],
        "security_opt": ["no-new-privileges:true"],
    }

    def _inspect(cid: str, service: str, *, entrypoint: Any, user: str) -> dict[str, Any]:
        return {
            "Id": cid,
            "Platform": "linux",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "fixture__env",
                    "com.docker.compose.service": service,
                },
                "Entrypoint": entrypoint,
                "Cmd": [],
                "Healthcheck": {"Test": ["NONE"]}
                if service == "main"
                else {"Test": ["CMD-SHELL", "true"]},
                "User": user,
            },
            "HostConfig": _host_config(),
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
            ]
            if service == "main"
            else [],
        }

    state = {
        "paths": paths,
        "config": config,
        "mounts": mounts,
        "environment_dir": environment_dir,
        "compose": {"services": {"main": compose_main, "verifier": compose_side}},
        "inspects": {
            MAIN_CID: _inspect(
                MAIN_CID,
                "main",
                entrypoint=["/bin/sh", "-c", "exec sleep infinity"],
                user="0",
            ),
            SIDE_CID: _inspect(
                SIDE_CID, "verifier", entrypoint=["python3", "/srv/server.py"], user=""
            ),
        },
        "events": [],
        "up": False,
        "deleted": False,
        "attached": set(),
        "aliases": {},
        "installed": {},
        "rules": {},
        "creates": [],
        "failure": None,
        "ordinary_exec_caps": [],
    }

    async def prepare(self):
        state["events"].append(("tool", "prepared"))
        self._tool_temp = tempfile.TemporaryDirectory(dir=tmp_path)
        self._nft_command = ["/fixture/loader", "--library-path", "/fixture", "/fixture/nft"]
        self._tool_image_digest = "sha256:fixture"
        if self._bridge_port is not None:
            self._bridge_addresses = BRIDGE_ADDRESSES

    def self_name() -> str:
        return "evallab-isolation-0123456789abcdef0123456789abcdef"

    async def daemon(self, *command, stdin=None):
        state["events"].append(("daemon", *command))
        if command[:1] == ("info",):
            return json.dumps({"OSType": "linux", "OperatingSystem": "fixture"})
        if command[:2] == ("container", "inspect"):
            return json.dumps(
                [state["inspects"][cid] for cid in command[2:] if cid in state["inspects"]]
            )
        if command[:2] == ("network", "create"):
            state["creates"].append(command)
            return NETWORK_ID
        if command[:2] == ("network", "disconnect"):
            cid = command[-1]
            assert state["installed"].get(cid), "network changed before filter installation"
            if state["failure"] == "disconnect":
                raise RuntimeError("fixture disconnect failure")
            state["attached"].discard(cid)
            for inspect in state["inspects"].values():
                if inspect["Id"] == cid:
                    inspect["NetworkSettings"]["Networks"] = {}
            return ""
        if command[:2] == ("network", "connect"):
            parts = list(command[2:])
            alias = None
            if "--alias" in parts:
                alias = parts[parts.index("--alias") + 1]
            cid = parts[-1]
            assert state["installed"].get(cid), "network attached before filter installation"
            if state["failure"] == "connect":
                raise RuntimeError("fixture connect failure")
            state["attached"].add(cid)
            if alias is not None:
                state["aliases"][cid] = alias
            for inspect in state["inspects"].values():
                if inspect["Id"] == cid:
                    inspect["NetworkSettings"]["Networks"] = {
                        self_name(): {"NetworkID": NETWORK_ID}
                    }
            return ""
        if command[:2] == ("network", "rm"):
            state["attached"].clear()
            return ""
        if command[:1] == ("cp",):
            cid = command[-1].split(":")[0]
            assert state["up"] and cid not in state["attached"]
            return ""
        if command[:1] == ("exec",):
            if "--privileged" not in command:
                assert command[1] is not None
                if state["failure"] == "interfaces":
                    return "eth0 up\nlo unknown\n"
                if command[1] in state["attached"]:
                    return "eth0 up\nlo unknown\n"
                return "lo unknown\nsit0 down\ntunl0 down\n"
            cid = command[command.index("-i") + 1]
            if "--file" in command or "-f" in command:
                assert cid not in state["attached"]
                if state["failure"] == "install":
                    raise RuntimeError("fixture install failure")
                state["rules"][cid] = stdin
                state["installed"][cid] = True
                state["events"].append(("nft", "installed", cid))
                return _listing(stdin)
            if "list" in command:
                state["events"].append(("nft", "listed", cid))
                if state["failure"] == "list":
                    raise RuntimeError("fixture list failure")
                return _listing(state["rules"][cid])
            raise AssertionError(f"unexpected nft command: {command}")
        raise AssertionError(f"unexpected daemon operation: {command}")

    async def compose(self, command, **kwargs):
        state["events"].append(("compose", *command))
        if command[0] == "config":
            Path(command[-1]).write_text(json.dumps(state["compose"]))
        elif command[0] == "up":
            state["up"] = True
        elif command[0] == "ps":
            return ExecResult(stdout="\n".join(state["ps_ids"]), stderr=None, return_code=0)
        elif command[0] == "exec":
            state["ordinary_exec_caps"].append(True)
        elif command[0] == "down":
            state["deleted"] = True
        return ExecResult(stdout=None, stderr=None, return_code=0)

    async def start(self, force_build):
        self._use_prebuilt = True
        await self._run_docker_compose_command(["up", "--detach", "--wait"])
        await self._run_docker_compose_command(["exec", "main", "true"])

    state["ps_ids"] = [MAIN_CID, SIDE_CID]
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


def test_locked_backend_still_refuses_task_compose(sidecar):
    (sidecar["environment_dir"] / "docker-compose.yaml").write_text("services: {}")
    with pytest.raises(ValueError, match="task-provided Compose"):
        LockedDockerEnvironment(
            environment_dir=sidecar["environment_dir"],
            environment_name="fixture",
            session_id="fixture__env",
            trial_paths=sidecar["paths"],
            task_env_config=sidecar["config"],
            mounts=sidecar["mounts"],
            egress_lock=True,
        )
    record = json.loads((sidecar["paths"].trial_dir / EGRESS_LOCK_RECORD).read_text())
    assert record["applied"] is False


def test_overlay_locks_every_service_but_keeps_sidecar_startup(sidecar):
    env = sidecar["make"]()
    overlay = json.loads(env._docker_compose_paths[-1].read_text())["services"]
    assert set(overlay) == {"main", "verifier"}
    main = overlay["main"]
    assert main["network_mode"] == "none"
    assert main["entrypoint"] == ["/bin/sh", "-c", "exec sleep infinity"]
    assert main["command"] == []
    assert main["healthcheck"] == {"disable": True}
    assert set(main["cap_drop"]) >= {"NET_ADMIN", "NET_RAW"}
    assert main["security_opt"] == ["no-new-privileges:true"]
    assert main["user"] == "0"
    # The sidecar keeps its own startup; only the lock boundary is forced.
    assert overlay["verifier"] == {
        "network_mode": "none",
        "cap_drop": ["NET_ADMIN", "NET_RAW"],
        "security_opt": ["no-new-privileges:true"],
    }
    asyncio.run(env.stop(delete=True))


def test_public_start_confirms_every_container_with_aliases(sidecar):
    env = sidecar["make"](isolation_lane="codex")
    asyncio.run(env.start(force_build=False))

    assert env._container_id == MAIN_CID
    assert env._service_container_ids == {"main": MAIN_CID, "verifier": SIDE_CID}
    # Tool bundle and filter reach every container before any attachment.
    installs = [item for item in sidecar["events"] if item[:2] == ("nft", "installed")]
    assert {item[2] for item in installs} == {MAIN_CID, SIDE_CID}
    # The project network avoids the filter's blocked ranges.
    (create,) = sidecar["creates"]
    assert "--internal" not in create
    subnet = create[create.index("--subnet") + 1]
    assert subnet.startswith("192.31.196.")
    # Every container joins with its DNS alias; main reaches `verifier` by name.
    assert sidecar["aliases"] == {MAIN_CID: "main", SIDE_CID: "verifier"}
    assert sidecar["attached"] == {MAIN_CID, SIDE_CID}
    record = _record(sidecar)
    assert record["applied"] is True
    assert record["container_id"] == MAIN_CID
    assert record["service_container_ids"] == {"main": MAIN_CID, "verifier": SIDE_CID}
    assert record["isolation_subnet"] == subnet
    assert set(record["startup_evidence"]["services"]) == {"main", "verifier"}
    asyncio.run(env.stop(delete=True))
    assert sidecar["deleted"] and env._network_id is None


def test_sidecar_never_gets_the_claude_bridge_exception(sidecar):
    env = sidecar["make"](isolation_lane="claude-code", claude_bridge_port=45123)
    asyncio.run(env.start(force_build=False))
    assert "dport 45123" in env._service_rules["main"]
    assert "dport" not in env._service_rules["verifier"]
    record = _record(sidecar)
    assert record["bridge_port"] == 45123
    assert record["applied"] is True
    asyncio.run(env.stop(delete=True))


def test_no_network_multi_uses_an_internal_project_network(sidecar):
    env = sidecar["make"](
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
    )
    asyncio.run(env.start(force_build=False))
    (create,) = sidecar["creates"]
    assert "--internal" in create
    assert "--subnet" in create
    assert sidecar["attached"] == {MAIN_CID, SIDE_CID}
    assert sidecar["aliases"] == {MAIN_CID: "main", SIDE_CID: "verifier"}
    assert _record(sidecar)["applied"] is True
    asyncio.run(env.stop(delete=True))


@pytest.mark.parametrize(
    "key,value",
    [
        ("ports", ["8420:8420"]),
        ("privileged", True),
        ("cap_add", ["NET_ADMIN"]),
        ("volumes", [{"type": "bind", "source": "/tmp", "target": "/tmp"}]),
        ("networks", {"default": {}}),
        ("network_mode", "host"),
        ("security_opt", ["seccomp:unconfined"]),
        ("cap_drop", ["NET_RAW"]),
        ("pid", "host"),
    ],
)
def test_unsafe_sidecar_compose_is_refused_before_creation(sidecar, key, value):
    sidecar["compose"]["services"]["verifier"][key] = value
    with pytest.raises(RuntimeError):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert sidecar["up"] is False
    assert ("nft", "installed", MAIN_CID) not in sidecar["events"]
    assert _record(sidecar)["applied"] is False


def test_undeclared_compose_service_is_refused(sidecar):
    sidecar["compose"]["services"]["evil"] = {"network_mode": "none"}
    with pytest.raises(RuntimeError, match="do not match"):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert sidecar["up"] is False
    assert _record(sidecar)["applied"] is False


def test_missing_declared_sidecar_is_refused(sidecar):
    del sidecar["compose"]["services"]["verifier"]
    with pytest.raises(RuntimeError, match="do not match"):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert sidecar["up"] is False


def test_unparseable_task_compose_fails_closed(sidecar):
    (sidecar["environment_dir"] / "docker-compose.yaml").write_text("services: [oops")
    with pytest.raises(ValueError, match="cannot parse"):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert sidecar["up"] is False


def test_sidecar_daemon_mismatch_refuses_install_and_cleans(sidecar):
    sidecar["inspects"][SIDE_CID]["HostConfig"]["NetworkMode"] = "host"
    with pytest.raises(RuntimeError):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert sidecar["deleted"]
    assert _record(sidecar)["applied"] is False
    assert not [item for item in sidecar["events"] if item[:2] == ("nft", "installed")]


def test_sidecar_bind_mount_is_refused(sidecar):
    sidecar["inspects"][SIDE_CID]["Mounts"] = [
        {
            "Type": "bind",
            "Source": "/tmp",
            "Destination": "/srv/data",
            "Propagation": "rprivate",
            "RW": True,
        }
    ]
    with pytest.raises(RuntimeError, match="bind mounts"):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert _record(sidecar)["applied"] is False


def test_extra_sidecar_container_is_refused(sidecar):
    replica = dict(sidecar["inspects"][SIDE_CID])
    replica["Id"] = "d" * 64
    sidecar["inspects"][replica["Id"]] = replica
    sidecar["ps_ids"] = [MAIN_CID, SIDE_CID, replica["Id"]]
    with pytest.raises(RuntimeError):
        asyncio.run(sidecar["make"]().start(force_build=False))
    assert _record(sidecar)["applied"] is False
