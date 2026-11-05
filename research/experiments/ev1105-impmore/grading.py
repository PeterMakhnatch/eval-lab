"""Separate, non-agent grading on the canonical writable image filesystem.

The caller captures the submission and destroys the read-only agent first.
G1 and G2 share this grader; safe_io owns paused workspace restoration.
No model tools, credentials, host environment, or network enter this container.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

TRANSPORT_SHA256 = "986330b5e43a52b9056b39dc337c4875c4e32556aa4ecf6415ba8e4d650f30ac"
LABEL = "ov1104.owner"
MEMORY_BYTES = 3 * 1024**3
MAX_JSON_BYTES = 1024 * 1024


def _load(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("impmore_grading_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transport():
    path = Path(__file__).with_name("container_tool.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != TRANSPORT_SHA256:
        raise RuntimeError("Frozen container watchdog differs")
    module = _load("container_tool")  # Also verifies the underlying transport hash.
    if module.LIFETIME_SECONDS != 3000:
        raise RuntimeError("Frozen watchdog lifetime differs")
    return module, path


def _id(config):
    cid = config["container_id"]
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Exact full container ID required")
    return cid


def _socket(config):
    endpoint = config["docker_env"]["DOCKER_HOST"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise ValueError("Only an absolute local Docker Unix socket is permitted")
    path = endpoint[len("unix://"):]
    if any(char in path for char in "\0\r\n?#"):
        raise ValueError("Invalid Docker Unix socket path")
    return path


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


def _engine(config, method="GET", suffix="/json"):
    """Query only this exact full ID; never use model-controlled process output."""
    connection = _UnixHTTP(_socket(config))
    try:
        connection.request(method, f"/containers/{_id(config)}{suffix}")
        response = connection.getresponse()
        data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise RuntimeError("Docker Engine response exceeds JSON limit")
        return response.status, json.loads(data) if data else None
    finally:
        connection.close()


def absence(config: dict) -> dict:
    """Unknown daemon/transport failures are false evidence, never absence."""
    evidence = {"container_id": config.get("container_id"), "verified_absent": False,
                "exit_channel": "docker-engine-container-inspect-v1"}
    try:
        status, _ = _engine(config)
        evidence["http_status"] = status
        evidence["verified_absent"] = status == 404
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    return evidence


def _remove(config):
    """Delete only an inspected, exactly owned ID, then demand Engine absence."""
    before = absence(config)
    if before["verified_absent"]:
        return {"status": "already_absent", "absence": before}
    status, info = _engine(config)
    if status == 404:
        return {"status": "already_absent", "absence": absence(config)}
    if (status != 200 or info["Id"] != _id(config)
            or info["Config"]["Labels"].get(LABEL) != config["owner"]
            or info["Image"] != config["image_id"]):
        raise RuntimeError("Refusing cleanup without exact owner/image evidence")
    deleted, _ = _engine(config, "DELETE", "?force=1")
    if deleted not in (204, 404):
        raise RuntimeError(f"Owner-verified deletion returns HTTP {deleted}")
    proof = absence(config)
    return {"status": "owner_verified_delete", "delete_http_status": deleted, "absence": proof}


def close(config: dict, watchdog: subprocess.Popen | None) -> dict:
    """Close the parent pipe, join the watchdog, and prove exact-ID absence."""
    evidence = {"container_id": config.get("container_id"), "verified_absent": False}
    if watchdog is not None:
        try:
            if watchdog.stdin is not None:
                watchdog.stdin.close()
            evidence["watchdog_rc"] = watchdog.wait(timeout=5)
        except Exception as exc:
            evidence["watchdog_error"] = f"{type(exc).__name__}: {exc}"
    try:
        evidence["cleanup"] = _remove(config)
    except Exception as exc:
        evidence["cleanup"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if watchdog is not None and watchdog.poll() is None:
        try:
            evidence["watchdog_rc"] = watchdog.wait(timeout=25)
        except Exception as exc:
            # Stop only our child watchdog. Container absence remains a separate fact.
            evidence["watchdog_join_error"] = f"{type(exc).__name__}: {exc}"
            try:
                watchdog.terminate()
                evidence["watchdog_rc"] = watchdog.wait(timeout=5)
            except Exception as stop_exc:
                evidence["watchdog_stop_error"] = f"{type(stop_exc).__name__}: {stop_exc}"
    proof = absence(config)
    evidence["absence"] = proof
    evidence["verified_absent"] = proof["verified_absent"]
    return evidence


def _validate(config, info, environment, user, *, running):
    host, spec, mounts = info["HostConfig"], info["Config"], info["Mounts"]
    expected_command = ["-i", *environment, "/bin/sleep", "2100"]
    checks = {
        "identity": info["Id"] == config["container_id"] and info["Image"] == config["image_id"],
        "owner": spec.get("Labels", {}).get(LABEL) == config["owner"],
        "state": info["State"]["Running"] is running and not info["State"]["Paused"],
        "canonical_environment": sorted(spec.get("Env") or []) == sorted(environment),
        "canonical_user": spec.get("User", "") == user,
        "workdir": spec["WorkingDir"] == config["workdir"],
        "lifetime_process": spec["Entrypoint"] == ["/usr/bin/env"] and spec["Cmd"] == expected_command,
        "no_healthcheck": spec.get("Healthcheck", {}).get("Test") == ["NONE"],
        "no_network": host["NetworkMode"] == "none" and not host.get("PortBindings")
                      and not host.get("PublishAllPorts") and not host.get("Links")
                      and set(info["NetworkSettings"].get("Networks") or {}) <= {"none"},
        "root_writable": host["ReadonlyRootfs"] is False,
        "unprivileged": host["Privileged"] is False and not host.get("CapAdd")
                        and host["CapDrop"] == ["ALL"]
                        and host["SecurityOpt"] == ["no-new-privileges"],
        "private_namespaces": host.get("PidMode", "") == "" and host["IpcMode"] == "private"
                              and host.get("UTSMode", "") == "" and host.get("UsernsMode", "") == ""
                              and host["CgroupnsMode"] == "private",
        "no_devices": not host.get("Devices") and not host.get("DeviceRequests")
                      and not host.get("DeviceCgroupRules"),
        "resource_caps": host["Memory"] == MEMORY_BYTES and host["NanoCpus"] == 2_000_000_000
                         and host["PidsLimit"] == 256 and host["MemorySwap"] == MEMORY_BYTES
                         and not host.get("OomKillDisable"),
        "one_workspace": len(mounts) == 1 and mounts[0]["Type"] == "bind"
                         and mounts[0]["Source"] == config["workspace_source"]
                         and mounts[0]["Destination"] == config["workdir"] and mounts[0]["RW"] is True
                         and mounts[0].get("Propagation") == "rprivate",
        "no_extra_mounts": not host.get("Binds") and not host.get("VolumesFrom")
                           and not host.get("Tmpfs") and not host.get("VolumeDriver")
                           and not spec.get("Volumes") and len(host.get("Mounts") or []) == 1,
        "no_host_overrides": not host.get("ExtraHosts") and not host.get("Dns")
                             and not host.get("DnsOptions") and not host.get("DnsSearch")
                             and not host.get("CgroupParent") and not host.get("StorageOpt")
                             and not host.get("Sysctls") and not host.get("AutoRemove")
                             and host.get("RestartPolicy", {}).get("Name") in ("", "no"),
        "no_interactive_process": not spec.get("Tty") and not spec.get("OpenStdin"),
    }
    if not all(checks.values()):
        raise RuntimeError("Grader boundary differs: " + ", ".join(k for k, value in checks.items() if not value))
    return checks


def start(agent_config: dict, control: Path) -> tuple[dict, subprocess.Popen, dict]:
    """Start a non-agent grader only after strict proof that the agent is absent."""
    transport, transport_path = _transport()
    agent_absence = absence(agent_config)
    if not agent_absence["verified_absent"]:
        raise RuntimeError("Agent absence is not proven: " + json.dumps(agent_absence))
    endpoint = "unix://" + _socket(agent_config)
    workspace = Path(agent_config["workspace_source"])
    control = control.resolve()
    if (not workspace.is_absolute() or not workspace.is_dir() or control.is_relative_to(workspace)
            or workspace.resolve() != workspace or "," in str(workspace)):
        raise ValueError("Grader control must be outside the exact absolute workspace")
    image, workdir = agent_config["image_id"], agent_config["workdir"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Immutable image ID required")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", workdir) or workdir == "/":
        raise ValueError("Unsafe grader workdir")
    binary = Path(agent_config["docker"])
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError("Absolute Docker executable required")
    control.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = {"docker": str(binary), "docker_env": {
        "PATH": "/usr/bin:/bin", "HOME": str(control), "DOCKER_HOST": endpoint,
        "DOCKER_CONFIG": str(control / "docker-config")}, "control": str(control),
        "workspace_source": str(workspace), "owner": uuid.uuid4().hex,
        "image_id": image, "workdir": workdir}
    watchdog = None
    try:
        environment = _load("safe_io").image_environment(config)
        inspected = transport.docker(config, "image", "inspect", image)
        if inspected.returncode:
            raise RuntimeError("Cannot inspect immutable grader image")
        image_info, = json.loads(inspected.stdout)
        if image_info["Id"] != image or image_info["Config"].get("Volumes"):
            raise RuntimeError("Image identity differs or image declares extra volumes")
        user = image_info["Config"].get("User", "")
        argv = ["create", "--label", f"{LABEL}={config['owner']}",
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--ipc", "private", "--cgroupns", "private", "--no-healthcheck",
                "--pids-limit", "256", "--memory", "3g", "--memory-swap", "3g", "--cpus", "2",
                "--mount", f"type=bind,source={workspace},target={workdir}"]
        for entry in environment:
            argv += ["--env", entry]
        argv += ["--workdir", workdir, "--entrypoint", "/usr/bin/env", image,
                 "-i", *environment, "/bin/sleep", "2100"]
        created = transport.docker(config, *argv)
        cid = created.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", cid):
            config["container_id"] = cid
        if created.returncode or "container_id" not in config:
            raise RuntimeError("Grader creation fails: " + created.stderr[-1000:])
        config_path = control / "transport.json"
        transport.save_new(config_path, config)
        status, before = _engine(config)
        if status != 200:
            raise RuntimeError(f"Created grader inspection returns HTTP {status}")
        _validate(config, before, environment, user, running=False)
        watchdog = subprocess.Popen(
            [sys.executable, str(transport_path), "watchdog", "--config", str(config_path)],
            cwd=control, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.monotonic() + 5
        ready_path = control / "watchdog-ready.json"
        while not ready_path.exists():
            if watchdog.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Grader watchdog fails before startup")
            time.sleep(0.05)
        ready = json.loads(ready_path.read_text())
        if (ready["pid"] != watchdog.pid or ready["lifetime_seconds"] != 3000
                or ready["source_sha256"] != transport.SOURCE_SHA256 or watchdog.poll() is not None):
            raise RuntimeError("Grader watchdog identity differs")
        started = transport.docker(config, "start", cid)
        if started.returncode:
            raise RuntimeError("Grader start fails: " + started.stderr[-1000:])
        status, info = _engine(config)
        if status != 200:
            raise RuntimeError(f"Running grader inspection returns HTTP {status}")
        checks = _validate(config, info, environment, user, running=True)
        transport.save_new(control / "grader-inspect.json", info)
        evidence = {"container_id": cid, "agent_absence": agent_absence,
                    "transport_sha256": TRANSPORT_SHA256, "watchdog": ready,
                    "checks": checks, "root_writable": True, "network_mode": "none",
                    "mounts": info["Mounts"], "image_environment": environment,
                    "create_argv": [str(binary), *argv]}
        transport.save_new(control / "grader-start.json", evidence)
        return config, watchdog, evidence
    except BaseException as exc:
        cleanup = close(config, watchdog) if "container_id" in config else {
            "verified_absent": False, "cleanup": {"status": "no_exact_id_available"}}
        transport.save_new(control / "grader-start-failure.json", {
            "error": f"{type(exc).__name__}: {exc}", "cleanup": cleanup})
        raise
