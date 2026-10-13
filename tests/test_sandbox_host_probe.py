"""Host-isolation diagnostic contracts; no Docker, DNS, sockets, or credentials."""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import shlex
import socket
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/probe_sandbox_host.py"
SPEC = importlib.util.spec_from_file_location("sandbox_host_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_lsof_collects_all_tcp_ports_without_process_or_address_content():
    ports = probe.listening_ports(
        "p12\nn127.0.0.1:55433\nn*:8000\nn[::1]:8001\n"
        "n192.168.1.3:9099 (LISTEN)\nn*:0\nn*:65536\ncsecret-command\n",
        bridge_port=40000,
    )
    assert ports == [8000, 8001, 8004, 9099, 40000, 54329, 55433]
    with pytest.raises(ValueError, match="1..65535"):
        probe.listening_ports("", bridge_port=0)


@pytest.mark.parametrize("value", ["8.8.8.8", "0.0.0.0", "224.0.0.1", "::"])
def test_explicit_targets_refuse_remote_or_unspecified_ips(value):
    with pytest.raises(argparse.ArgumentTypeError):
        probe.local_host_ip(value)


def test_explicit_targets_accept_docker_and_local_host_ips():
    assert probe.local_host_ip("192.168.65.254") == "192.168.65.254"
    assert probe.local_host_ip("127.0.0.1") == "127.0.0.1"
    assert probe.local_host_ip("fd00::1") == "fd00::1"


def test_prior_targets_preserve_disappeared_ports_and_raw_local_routes_only():
    ports, targets = probe.prior_targets(
        {
            "ports": [9099, 8000, 9099],
            "lanes": [
                {"probe": {"targets": ["host.docker.internal", "192.168.65.254", "172.18.0.1"]}},
                {"probe": {"targets": ["gateway.docker.internal", "192.168.65.254", "fd00::1", "8.8.8.8", "0.0.0.0", "example.com", None]}},
            ],
        }
    )
    assert ports == [8000, 9099]
    assert targets == ["172.18.0.1", "192.168.65.254", "fd00::1"]


@pytest.mark.parametrize("ports", [[True], [0], [65536], ["8000"], "8000"])
def test_prior_report_refuses_invalid_port_targets(ports):
    with pytest.raises(ValueError, match="valid numeric ports"):
        probe.prior_targets({"ports": ports})


def test_source_environment_preserves_network_and_resources_and_excludes_secrets():
    source = """
[environment]
network_mode = "public"
os = "linux"
cpus = 2
memory_mb = 512
storage_mb = 2048
build_timeout_sec = 301.0
skills_dir = "/secret"
mcp_servers = ["secret-route"]
[environment.env]
TOKEN = "must-not-copy"
"""
    result = probe.source_environment(source, "existing:image")
    assert result == {
        "network_mode": "public",
        "os": "linux",
        "cpus": 2,
        "memory_mb": 512,
        "storage_mb": 2048,
        "build_timeout_sec": 301.0,
        "docker_image": "existing:image",
        "env": {},
        "mcp_servers": [],
    }
    assert "must-not-copy" not in json.dumps(result)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ('[environment]\nnetwork_mode="no-network"', "one container"),
        ('[environment]\nos="windows"', "Linux"),
        ("[environment]\ngpus=1", "accelerators"),
    ],
)
def test_source_environment_refuses_extra_containers_and_accelerators(source, reason):
    with pytest.raises(ValueError, match=reason):
        probe.source_environment(source, "existing:image")


def test_task_digest_pins_names_and_bytes_and_refuses_symlinks(tmp_path):
    (tmp_path / "task.toml").write_text("fixture")
    original = probe.task_digest(tmp_path)
    assert probe.task_digest(tmp_path) == original
    (tmp_path / "task.toml").write_text("changed")
    assert probe.task_digest(tmp_path) != original
    (tmp_path / "task.toml").write_text("fixture")
    (tmp_path / "task.toml").rename(tmp_path / "renamed.toml")
    assert probe.task_digest(tmp_path) != original
    (tmp_path / "link").symlink_to(tmp_path / "renamed.toml")
    with pytest.raises(ValueError, match="symlinks"):
        probe.task_digest(tmp_path)


ROUTES = """Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth0 00000000 010012AC 0003 0 0 0 00000000 0 0 0
eth0 000012AC 00000000 0001 0 0 0 0000FFFF 0 0 0
eth0 00000000 invalid 0003 0 0 0 00000000 0 0 0
eth0 00000000 020012AC 0001 0 0 0 00000000 0 0 0
"""


def test_default_gateway_is_decoded_without_shell_or_dns():
    assert probe.default_gateways(ROUTES) == ["172.18.0.1"]
    assert probe.default_gateways("Iface Destination Gateway Flags\n") == []


class Connection:
    def __init__(self, response=b"HTTP/1.0 401 Unauthorized\r\nSECRET: value\r\n\r\nSECRET-BODY"):
        self.response = response
        self.sent = b""
        self.read = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, value):
        assert value == probe.SOCKET_TIMEOUT_SEC

    def sendall(self, value):
        self.sent += value

    def recv(self, count):
        byte, self.response = self.response[:count], self.response[count:]
        self.read += byte
        return byte


def test_http_records_only_numeric_status_and_does_not_read_headers_or_body():
    connection = Connection()
    called = []

    def connect(address, *, timeout):
        called.append((address, timeout))
        return connection

    row = probe.probe_connection(
        ("host.docker.internal", 40000, "POST", "/v1/messages"), connect=connect
    )
    assert row["tcp_reachable"] is True
    assert row["http_status"] == 401
    assert row["error_type"] is None
    assert connection.closed is True
    assert connection.read == b"HTTP/1.0 401 Unauthorized\r\n"
    assert b"Authorization" not in connection.sent
    assert b"model" not in connection.sent
    assert connection.sent.endswith(b"\r\n\r\n{}")
    assert "SECRET" not in json.dumps(row)
    assert called == [(("host.docker.internal", 40000), probe.SOCKET_TIMEOUT_SEC)]


def test_tcp_and_http_failures_preserve_reachability_without_error_messages():
    def refused(*_, **__):
        raise ConnectionRefusedError("private error detail")

    row = probe.probe_connection(("host.docker.internal", 8000, "HEAD", "/"), connect=refused)
    assert row["tcp_reachable"] is False
    assert row["error_type"] == "ConnectionRefusedError"
    assert "private" not in json.dumps(row)

    class SilentConnection(Connection):
        def recv(self, _):
            raise TimeoutError("private response detail")

    row = probe.probe_connection(
        ("host.docker.internal", 55433, "HEAD", "/"),
        connect=lambda *_, **__: SilentConnection(),
    )
    assert row["tcp_reachable"] is True
    assert row["http_status"] is None
    assert row["error_type"] == "TimeoutError"
    assert "private" not in json.dumps(row)


def test_non_http_listeners_are_still_reported_as_tcp_reachable():
    row = probe.probe_connection(
        ("host.docker.internal", 54329, "HEAD", "/"),
        connect=lambda *_, **__: Connection(b"non-http-protocol\n"),
    )
    assert row["tcp_reachable"] is True
    assert row["http_status"] is None
    assert row["error_type"] == "NonHTTPResponse"


def test_public_registry_probe_uses_fixed_https_head_without_reading_body_or_headers():
    events = []

    class HTTPSConnection:
        def __init__(self, host, *, timeout):
            events.append(("connect", host, timeout))

        def request(self, method, path):
            events.append(("request", method, path))

        def getresponse(self):
            # No read/getheaders attribute: accessing either fails this contract.
            return SimpleNamespace(status=200)

        def close(self):
            events.append(("close",))

    record = probe.public_egress_probe(connection_factory=HTTPSConnection)
    assert record == {
        "target": "registry.npmjs.org",
        "scheme": "https",
        "port": 443,
        "method": "HEAD",
        "path": "/",
        "http_status": 200,
        "error_type": None,
    }
    assert events == [
        ("connect", "registry.npmjs.org", 5),
        ("request", "HEAD", "/"),
        ("close",),
    ]


def test_public_egress_failure_records_error_type_without_private_details():
    def failure(*_, **__):
        raise TimeoutError("private detail must not enter report")

    record = probe.public_egress_probe(connection_factory=failure)
    assert record["http_status"] is None
    assert record["error_type"] == "TimeoutError"
    assert "private" not in json.dumps(record)


def test_public_egress_has_no_network_call_unless_requested():
    calls = []

    def public_probe():
        calls.append("public HEAD")
        return {"http_status": 200, "error_type": None}

    seams = {
        "resolve": lambda *_, **__: [],
        "connect": lambda *_, **__: pytest.fail("no TCP ports selected"),
        "route_text": "",
        "public_probe": public_probe,
    }
    report = probe.inside_probe([], None, [], **seams)
    assert report["public_egress"] is None
    assert calls == []
    report = probe.inside_probe([], None, [], check_public_egress=True, **seams)
    assert report["public_egress"]["http_status"] == 200
    assert calls == ["public HEAD"]


def test_inside_probe_covers_aliases_raw_dns_ips_gateway_and_preserved_host_ip():
    calls = []

    def resolve(alias, port):
        assert port is None
        if alias == "gateway.docker.internal":
            raise socket.gaierror("secret DNS error details")
        return [(None, None, None, None, ("192.168.65.254", 0))]

    def connect(address, *, timeout):
        calls.append(address)
        return Connection()

    report = probe.inside_probe(
        [8000, 40000],
        40000,
        ["192.168.1.5"],
        resolve=resolve,
        connect=connect,
        route_text=ROUTES,
        map_requests=map,
    )
    assert report["targets"] == [
        "host.docker.internal",
        "gateway.docker.internal",
        "172.18.0.1",
        "192.168.1.5",
        "192.168.65.254",
    ]
    assert report["aliases"][1]["error_type"] == "gaierror"
    assert len(report["results"]) == 20
    for target in report["targets"]:
        rows = [row for row in report["results"] if row["target"] == target]
        assert {(row["method"], row["path"]) for row in rows} == {
            ("HEAD", "/"),
            ("POST", "/v1/messages"),
            ("GET", "/v1/models"),
        }
        assert (target, 8000) in calls
    assert "secret" not in json.dumps(report)


def test_payload_contains_exact_seamed_functions_and_compiles_without_execution():
    command = probe.payload_command([8000], 40000, ["192.168.65.254"], check_public_egress=True)
    tokens = shlex.split(command)
    assert tokens[:2] == ["python3", "-c"]
    encoded = tokens[2].split("b64decode(", 1)[1].split(")", 1)[0]
    import ast

    source = base64.b64decode(ast.literal_eval(encoded)).decode()
    compile(source, "sandbox-probe", "exec")
    assert "def inside_probe(" in source
    assert "def probe_connection(" in source
    assert "def default_gateways(" in source
    assert "def public_egress_probe(" in source
    assert "check_public_egress=True" in source
    assert "ThreadPoolExecutor(max_workers=12)" in source
    assert "Authorization:" not in source


def test_docker_inspection_excludes_environment_and_unsanitized_fields():
    container = {
        "Id": "fixture-id",
        "Config": {"Env": ["TOKEN=never-record"], "Labels": {"private": "never-record"}},
        "Mounts": [{"Source": "/private/never-record"}],
        "HostConfig": {
            "NetworkMode": "fixture-network",
            "ExtraHosts": ["host.docker.internal:192.168.65.254"],
            "Privileged": False,
            "CapDrop": ["NET_RAW"],
            "Secret": "never-record",
        },
        "NetworkSettings": {
            "Networks": {
                "fixture-network": {
                    "NetworkID": "fixture-net-id",
                    "IPAddress": "172.18.0.2",
                    "Gateway": "172.18.0.1",
                    "Aliases": ["never-record"],
                }
            }
        },
    }
    sanitized = probe.sanitized_container(container)
    assert sanitized["network_mode"] == "fixture-network"
    assert sanitized["networks"]["fixture-network"]["Gateway"] == "172.18.0.1"
    assert "never-record" not in json.dumps(sanitized)
    network = probe.sanitized_network(
        {
            "Id": "fixture-net-id",
            "Name": "fixture-network",
            "Driver": "bridge",
            "Internal": True,
            "IPAM": {"Config": [{"Gateway": "172.18.0.1"}], "Options": {"secret": "never-record"}},
            "Options": {"secret": "never-record"},
            "Containers": {"secret": "never-record"},
        }
    )
    assert network["Internal"] is True
    assert "never-record" not in json.dumps(network)


def test_probe_compose_wrapper_never_pulls_or_removes_shared_images(monkeypatch):
    calls = []

    class NativeEnvironment:
        async def _run_docker_compose_command(self, command, **kwargs):
            calls.append((command, kwargs))

    module = types.ModuleType("harbor.environments.docker.docker")
    module.DockerEnvironment = NativeEnvironment
    monkeypatch.setitem(sys.modules, module.__name__, module)
    environment = probe.probe_environment_type("before")()
    asyncio.run(environment._run_docker_compose_command(["up", "--detach", "--wait"]))
    asyncio.run(
        environment._run_docker_compose_command(
            ["down", "--rmi", "local", "--volumes", "--remove-orphans"], timeout_sec=10
        )
    )
    assert calls == [
        (["up", "--detach", "--wait", "--pull", "never"], {}),
        (["down", "--volumes", "--remove-orphans"], {"timeout_sec": 10}),
    ]


@pytest.mark.parametrize("lane", probe.LANES)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_native_lifecycle_is_sequential_and_after_options_are_lane_specific(tmp_path, lane, phase):
    state = {"running": 0, "events": [], "arguments": None}

    class TrialPaths:
        def __init__(self, root):
            self.root = root

        def mkdir(self):
            self.root.mkdir(parents=True)

    class Environment:
        _probe_isolation_record = "host-isolation.json" if phase == "after" else None

        def __init__(self, **kwargs):
            state["arguments"] = kwargs
            self.record_path = kwargs["trial_paths"].root / "host-isolation.json"

        async def start(self, *, force_build):
            assert force_build is False
            assert state["running"] == 0
            state["running"] += 1
            state["events"].append("start")
            if phase == "after":
                self.record_path.write_text(json.dumps({
                    "applied": True,
                    "startup_evidence": {"HostConfig": {"NetworkMode": "none"}},
                    "rules": "fixture-filter",
                }))

        async def exec(self, command, *, timeout_sec):
            assert state["running"] == 1
            assert command.startswith("python3 -c ")
            assert timeout_sec >= 90
            state["events"].append("exec")
            return SimpleNamespace(return_code=0, stdout='{"results": []}')

        async def stop(self, *, delete):
            assert delete is True
            state["running"] -= 1
            state["events"].append("stop")
            self.record_path.unlink(missing_ok=True)

    def run(arguments):
        assert arguments[0] == "docker"
        if arguments[1] == "ps":
            return "fixture-id\n" if state["running"] else ""
        if arguments[1:3] == ["container", "inspect"]:
            return json.dumps([{"Id": "fixture-id", "HostConfig": {}, "NetworkSettings": {}}])
        assert arguments[1:3] == ["network", "ls"]
        assert state["running"] == 0
        return ""

    baseline = object()
    config = SimpleNamespace(resolve_baseline=lambda: baseline)
    report = asyncio.run(
        probe.probe_lane(
            phase=phase,
            lane=lane,
            environment_type=Environment,
            environment_config=config,
            root=tmp_path,
            ports=[8000],
            bridge_port=40000,
            host_ips=["192.168.65.254"],
            run=run,
            trial_paths_type=TrialPaths,
        )
    )
    assert report["lane"] == lane
    assert state["events"] == ["start", "exec", "stop"]
    assert state["running"] == 0
    assert state["arguments"]["network_policy"] is baseline
    assert state["arguments"]["keep_containers"] is False
    if phase == "after":
        assert state["arguments"]["isolation_lane"] == lane
        assert state["arguments"]["claude_bridge_port"] == (40000 if lane == "claude-code" else None)
        assert report["isolation_record"] == {
            "applied": True,
            "startup_evidence": {"HostConfig": {"NetworkMode": "none"}},
            "rules": "fixture-filter",
        }
        assert not (tmp_path / lane / "trial" / "host-isolation.json").exists()
    else:
        assert "isolation_lane" not in state["arguments"]
        assert report["isolation_record"] is None


def test_probe_refuses_to_continue_when_own_cleanup_is_incomplete(tmp_path):
    events = []

    class TrialPaths:
        def __init__(self, root):
            self.root = root

        def mkdir(self):
            self.root.mkdir(parents=True)

    class Environment:
        def __init__(self, **_):
            pass

        async def start(self, **_):
            events.append("start")
            raise RuntimeError("controlled startup failure")

        async def stop(self, **_):
            events.append("stop")

    def run(arguments):
        return "leftover-id" if arguments[1] == "ps" else ""

    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        asyncio.run(
            probe.probe_lane(
                phase="before",
                lane="nop",
                environment_type=Environment,
                environment_config=SimpleNamespace(resolve_baseline=lambda: object()),
                root=tmp_path,
                ports=[8000],
                bridge_port=None,
                host_ips=[],
                run=run,
                trial_paths_type=TrialPaths,
            )
        )
    assert events == ["start", "stop"]


def test_probe_checks_existing_image_before_any_container_lifecycle(tmp_path, monkeypatch):
    module = types.ModuleType("harbor.models.task.config")
    module.EnvironmentConfig = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    commands = []

    def run(arguments):
        commands.append(arguments)
        raise RuntimeError("image absent")

    monkeypatch.setattr(probe, "run_command", run)
    monkeypatch.setattr(
        probe,
        "probe_environment_type",
        lambda _: pytest.fail("must not instantiate a Docker environment"),
    )
    with pytest.raises(RuntimeError, match="image absent"):
        asyncio.run(
            probe.run_probe(SimpleNamespace(task=tmp_path, image="missing:image"))
        )
    assert commands == [
        ["docker", "image", "inspect", "--format", "{{.Id}}", "missing:image"]
    ]


def test_report_provenance_and_all_three_lanes_use_injected_host_state(tmp_path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('[environment]\nnetwork_mode="public"\ncpus=1\n')
    previous = tmp_path / "before.json"
    previous.write_text(json.dumps({
        "ports": [12345],
        "lanes": [{"probe": {"targets": ["host.docker.internal", "192.168.65.2"]}}],
    }))
    module = types.ModuleType("harbor.models.task.config")
    module.EnvironmentConfig = SimpleNamespace(model_validate=lambda value: value)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    commands = []

    def run(arguments):
        commands.append(arguments)
        if arguments[0] == "docker":
            return "existing-image-id"
        if arguments[0] == "lsof":
            return "n127.0.0.1:9099\n"
        if arguments[0] == "git":
            return "a" * 40
        pytest.fail("unexpected external-state probe")

    lanes = []

    async def lane(**kwargs):
        # Every lane is awaited before the next one is entered.
        assert len(lanes) == probe.LANES.index(kwargs["lane"])
        lanes.append(kwargs)
        return {"lane": kwargs["lane"], "probe": {"results": []}}

    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(probe, "datetime", SimpleNamespace(now=lambda _: fixed))
    monkeypatch.setattr(probe, "run_command", run)
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: "0.24.0-fixture")
    environment_type = object()
    monkeypatch.setattr(probe, "probe_environment_type", lambda _: environment_type)
    monkeypatch.setattr(probe, "probe_lane", lane)
    arguments = SimpleNamespace(
        task=task,
        image="existing:image",
        phase="after",
        bridge_port=40000,
        host_ip=["192.168.65.254"],
        ports_from=previous,
        check_public_egress=True,
    )
    report = asyncio.run(probe.run_probe(arguments))
    assert report["recorded_at_utc"] == "2026-01-02T03:04:05+00:00"
    assert report["repo_sha"] == "a" * 40
    assert report["harbor_version"] == "0.24.0-fixture"
    assert report["task_sha256"] == probe.task_digest(task)
    assert report["ports"] == [8000, 8001, 8004, 9099, 12345, 40000, 54329, 55433]
    assert report["host_ips"] == ["192.168.65.2", "192.168.65.254"]
    assert report["prior_probe_sha256"] is not None
    assert report["public_egress_requested"] is True
    assert report["model_calls"] == 0
    assert report["credential_free"] is True
    assert [entry["lane"] for entry in report["lanes"]] == list(probe.LANES)
    assert len(commands) == 3
    for entry in lanes:
        assert entry["environment_type"] is environment_type
        assert entry["environment_config"]["cpus"] == 1
        assert entry["host_ips"] == ["192.168.65.2", "192.168.65.254"]
        assert entry["check_public_egress"] is True
