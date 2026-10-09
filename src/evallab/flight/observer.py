"""Host-side lifecycle for the flight observer sidecar.

The observer is a privileged container in the Docker host PID namespace
(``--pid=host``), following the state-journal pattern: it reads the trial
through ``/proc/<pid>/root`` and kernel tracepoints and writes only to a
host directory mounted into the observer. Cgroup attribution resolves
inside the observer, where the host PID namespace is visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.flight.schemas import STATUS_SCHEMA, utc_now_iso

IMAGE_CONTEXT_ENV = "EVALLAB_FLIGHT_IMAGE_CONTEXT"


@dataclass(frozen=True)
class Observer:
    name: str
    output_dir: Path
    target_container: str
    target_pid: int
    phase: str


async def _command(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return 124, "", f"command timed out after {timeout:.0f}s"
    code = process.returncode if process.returncode is not None else 1
    return code, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def docker(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run one Docker CLI command; never raises on a nonzero exit."""
    return await _command("docker", *args, timeout=timeout)




def image_context_dir() -> Path:
    explicit = os.environ.get(IMAGE_CONTEXT_ENV)
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parent


async def ensure_image() -> str:
    context = image_context_dir()
    dockerfile = context / "runtime" / "Dockerfile"
    resources = (dockerfile, context / "runtime" / "probes.bt",
                 context / "runtime" / "recorder.py", context / "gateway.py")
    if not all(path.is_file() for path in resources):
        raise FileNotFoundError(f"flight observer image context is incomplete: {context}")
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in resources)).hexdigest()
    image = f"evallab-flight-recorder:{digest[:16]}"
    code, _, _ = await _command("docker", "image", "inspect", image, timeout=15)
    if code == 0:
        return image
    code, stdout, stderr = await _command(
        "docker", "build", "--quiet", "--file", str(dockerfile), "--tag", image,
        str(context), timeout=600
    )
    if code != 0:
        raise RuntimeError(stderr or stdout or "flight observer image build failed")
    return image


async def resolve_session_container(session_id: str) -> str:
    """Resolve only a known trial session; never inspect unrelated containers."""
    import re

    project = session_id.lower()
    if not re.match(r"^[a-z0-9]", project):
        project = "0" + project
    project = re.sub(r"[^a-z0-9_-]", "-", project)
    code, stdout, stderr = await docker(
        "ps", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=main", "--format", "{{.ID}}")
    ids = stdout.splitlines()
    if code != 0 or len(ids) != 1:
        raise RuntimeError(stderr or f"session {session_id}: expected one main container, got {len(ids)}")
    return ids[0]


async def inspect_container(container: str) -> dict[str, Any]:
    code, stdout, stderr = await _command(
        "docker", "inspect", container, timeout=15
    )
    if code != 0:
        raise RuntimeError(stderr or f"docker inspect failed for {container}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"docker inspect returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"docker inspect returned no object for {container}")
    return payload[0]


def container_pid(info: dict[str, Any]) -> int:
    state = info.get("State") if isinstance(info.get("State"), dict) else {}
    pid = state.get("Pid") if isinstance(state, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"invalid target pid: {pid!r}")
    return pid


def container_ip(info: dict[str, Any]) -> tuple[str, str] | None:
    """First (network name, IPv4) pair of a container, if any."""
    networks = info.get("NetworkSettings", {})
    networks = networks.get("Networks", {}) if isinstance(networks, dict) else {}
    if not isinstance(networks, dict):
        return None
    for name, net in networks.items():
        if isinstance(net, dict) and isinstance(net.get("IPAddress"), str) and net["IPAddress"]:
            return str(name), str(net["IPAddress"])
    return None


async def bridge_interface(network_name: str) -> str | None:
    """Host bridge interface (``docker0``, ``br-…``) backing a network."""
    code, stdout, stderr = await _command(
        "docker", "network", "inspect", network_name, timeout=15
    )
    if code != 0:
        raise RuntimeError(stderr or f"docker network inspect failed for {network_name}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid network JSON: {exc}") from exc
    if not payload or not isinstance(payload[0], dict):
        return None
    options = payload[0].get("Options")
    if isinstance(options, dict) and isinstance(options.get("com.docker.network.bridge.name"), str):
        return str(options["com.docker.network.bridge.name"])
    if network_name == "bridge":
        return "docker0"
    # User-defined bridges default to br-<first 12 of the network id>.
    network_id = payload[0].get("Id")
    if isinstance(network_id, str) and len(network_id) >= 12:
        return f"br-{network_id[:12]}"
    return None


def observer_command(
    *,
    image: str,
    observer_name: str,
    target_pid: int,
    target_container_id: str,
    output_dir: Path,
    phase: str,
    sniff: str = "",
    identity: dict[str, str] | None = None,
    enforcement: str = "network_mode_none",
    nonce: str = "",
) -> list[str]:
    # Locked tier runs no sniffer: no host network, no NET_RAW. The open tier
    # needs host network + NET_RAW only for its passive tcpdump on the trial's
    # own bridge. The nonce is per-run host-only framing integrity; it travels
    # in container env, is rendered into out/probes.<phase>.bt (host-only
    # output dir), and is never logged or exported into events.
    open_tier = bool(sniff)
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        observer_name,
        "--label",
        "evallab.flight=1",
        # Start from nothing and add back only what bpftrace + /proc enrichment need,
        # so the observer does not keep Docker's default capability set.
        "--cap-drop=ALL",
        "--cap-add=SYS_ADMIN",
        "--cap-add=SYS_PTRACE",
        "--cap-add=BPF",
        "--cap-add=PERFMON",
        "--security-opt=no-new-privileges=true",
        # The observer shares the host with other trials: an agent that floods events
        # must not be able to exhaust memory or process slots there.
        "--memory=1g",
        "--pids-limit=256",
    ]
    if open_tier:
        # tcpdump insists on dropping to its own user after opening the capture
        # (setgroups/setgid/setuid) and on chowning its output to that user.
        command += ["--cap-add=NET_RAW", "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=CHOWN"]
    command += [
        "--pid=host",
        "--cgroupns=host",
        f"--network={'host' if open_tier else 'none'}",
        "--volume",
        f"{output_dir}:/out:rw",
        "--volume",
        "/sys/kernel/debug:/sys/kernel/debug:rw",
        "--volume",
        "/sys/kernel/tracing:/sys/kernel/tracing:rw",
        "--env",
        f"TARGET_PID={target_pid}",
        "--env",
        f"TARGET_CONTAINER_ID={target_container_id}",
        "--env",
        f"PHASE={phase}",
        "--env",
        f"SNIFF={sniff}",
        "--env",
        f"IDENTITY={json.dumps(identity or {})}",
        "--env",
        f"ENFORCEMENT={enforcement}",
        "--env",
        f"FLIGHT_NONCE={nonce}",
        "--env",
        "BPFTRACE_STRLEN=160",
        "--env",
        "BPFTRACE_MAP_KEYS_MAX=1024",
        image,
    ]
    return command


async def start_observer(
    *,
    image: str,
    observer_name: str,
    container: str,
    output_dir: Path,
    phase: str,
    sniff: str = "",
    ready_timeout_seconds: float = 20.0,
    identity: dict[str, str] | None = None,
    enforcement: str = "network_mode_none",
) -> Observer:
    info = await inspect_container(container)
    pid = container_pid(info)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    await _command("docker", "rm", "--force", observer_name, timeout=15)
    (output_dir / "READY").unlink(missing_ok=True)
    nonce = secrets.token_hex(16)
    code, stdout, stderr = await _command(
        *observer_command(
            image=image,
            observer_name=observer_name,
            target_pid=pid,
            target_container_id=str(info["Id"]),
            output_dir=output_dir,
            phase=phase,
            sniff=sniff,
            identity=identity,
            enforcement=enforcement,
            nonce=nonce,
        ),
        timeout=60,
    )
    if code != 0:
        with contextlib.suppress(Exception):
            await stop_observer(observer_name)
        raise RuntimeError(stderr or stdout or "observer container failed to start")
    try:
        deadline = asyncio.get_running_loop().time() + ready_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if (output_dir / "READY").is_file():
                return Observer(observer_name, output_dir, container, pid, phase)
            state = await inspect_container(observer_name)
            if not state.get("State", {}).get("Running"):
                raise RuntimeError("observer exited before readiness; inspect phase status")
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await stop_observer(observer_name)
        raise
    except Exception:
        with contextlib.suppress(Exception):
            await stop_observer(observer_name)
        raise
    with contextlib.suppress(Exception):
        await stop_observer(observer_name)
    raise RuntimeError("observer did not become ready before agent execution")


async def stop_observer(observer_name: str) -> None:
    try:
        await _command("docker", "stop", "--time", "10", observer_name, timeout=10)
    finally:
        await _command("docker", "rm", "--force", observer_name, timeout=5)


def write_status_unavailable(
    output_dir: Path,
    reason: str,
    image: str | None,
    *,
    phase: str | None = None,
    code: str = "observer_unavailable",
) -> bool:
    """Record an explicit unavailable status; never raises, returns success."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": STATUS_SCHEMA,
            "schema_version": 1,
            "status": "unavailable",
            "reason": str(reason)[:4096],
            "code": code,
            "observer": {
                "mode": "external-sidecar",
                "target_mutated": False,
                "model_visible_output": False,
                "image": image,
            },
            "recorded_at": utc_now_iso(),
        }
        if phase is not None:
            payload["phase"] = phase
            path = output_dir / f"status.{phase}.json"
        else:
            path = output_dir / "status.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False
