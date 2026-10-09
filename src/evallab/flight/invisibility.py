"""Monitored vs unmonitored snapshots of the same unchanged probe container.

Seven checks retain raw evidence. Open adds routes, interfaces, gateway IP/MAC,
first hop, visible iptables and first-connect latency under the DNS/timing checks.
Timing is reported as a difference, not claimed to be identical or undetectable.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from evallab.flight.observer import (
    bridge_interface,
    container_ip,
    docker,
    ensure_image,
    inspect_container,
    start_observer,
    stop_observer,
)

CHECKS = ("ps", "env", "mounts", "mountinfo", "inotify", "dns", "timing")
TIMING_RATIO = 5.0

PROBE_PY = r'''
import json, os, re, socket, ssl, struct, subprocess, time
out = {}
procs = []
inotify = []
for pid in sorted(p for p in os.listdir("/proc") if p.isdigit()):
    try:
        comm = open(f"/proc/{pid}/comm").read().strip()
        args = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode().strip()
        procs.append([comm, args])
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                if "inotify" in os.readlink(f"/proc/{pid}/fd/{fd}"):
                    inotify.append([comm, fd])
            except OSError:
                pass
    except OSError:
        pass
out["ps"] = sorted(procs)
out["env"] = sorted(f"{k}={v}" for k, v in os.environ.items())
out["mounts"] = sorted(open("/proc/self/mounts").read().splitlines())
out["mountinfo"] = sorted(open("/proc/self/mountinfo").read().splitlines())
out["inotify"] = {"instances": inotify}
dns = {}
for name in ("resolv.conf", "hosts", "nsswitch.conf"):
    try:
        dns[name] = open(f"/etc/{name}").read()
    except OSError as exc:
        dns[name] = str(exc)
# DNS query is explicit so both locked failures and embedded-DNS packets have
# known expected names, independent of Python/libc resolver implementation.
server = next((line.split()[1] for line in dns["resolv.conf"].splitlines()
               if line.startswith("nameserver")), "127.0.0.11")
query = struct.pack("!6H", 42, 0x100, 1, 0, 0, 0) + b"\x07example\x03com\0" + struct.pack("!HH", 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
try:
    s.sendto(query, (server, 53))
    reply = s.recv(2048)
    dns["lookup_outcome"] = "reply" if len(reply) >= 12 else "short-reply"
except OSError as exc:
    dns["lookup_outcome"] = type(exc).__name__
finally:
    s.close()

def command(*argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return {"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)}
network = {}
network["routes"] = command("ip", "-j", "route", "show")
network["interfaces"] = command("ip", "-j", "address", "show")
network["iptables"] = command("iptables", "-S")
try:
    routes = json.loads(network["routes"].get("stdout", "[]"))
    gateway = next((r.get("gateway") for r in routes if r.get("dst") == "default"), None)
except ValueError:
    gateway = None
network["gateway_ip"] = gateway
# Warm ARP before both snapshots; the recorder neither sends nor answers.
if gateway:
    warm = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        warm.sendto(b"", (gateway, 9))
    except OSError:
        pass
    warm.close()
    time.sleep(0.05)
network["neighbors"] = command("ip", "-j", "neigh", "show")
network["hop1"] = command("traceroute", "-n", "-m", "1", "-q", "1", "-w", "1", "1.1.1.1")
dns["network"] = network
out["dns"] = dns
start = time.perf_counter_ns()
for _ in range(200):
    fd = os.open("/proc/self/comm", os.O_RDONLY)
    os.read(fd, 64)
    os.close(fd)
out["timing"] = {"open_read_close_ns": time.perf_counter_ns() - start}
start = time.perf_counter_ns()
try:
    sock = socket.create_connection(("1.1.1.1", 443), timeout=3)
    out["timing"]["connect_outcome"] = "connected"
    out["timing"]["first_connect_ns"] = time.perf_counter_ns() - start
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        wrapped = context.wrap_socket(sock, server_hostname="cloudflare-dns.com")
        wrapped.close()
    except OSError:
        sock.close()
except OSError as exc:
    out["timing"]["connect_outcome"] = type(exc).__name__
    out["timing"]["first_connect_ns"] = time.perf_counter_ns() - start
print(json.dumps(out, sort_keys=True))
'''


def canonicalize(check: str, value: Any) -> Any:
    if check == "ps":
        return sorted(row for row in value if "invisibility-probe.py" not in row[1])
    if check == "mountinfo":
        # Same target, so only mount IDs may be ignored; retain all options.
        return sorted(" ".join(line.split()[2:]) for line in value)
    if check in ("env", "mounts"):
        return sorted(value)
    return value


def _network_comparable(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    network = dict(result.get("network", {}))
    neighbors = network.get("neighbors")
    if isinstance(neighbors, dict) and "stdout" in neighbors:
        try:
            rows = json.loads(neighbors["stdout"])
            # NUD reachability timer advances independently of observation.
            network["neighbors"] = [{k: v for k, v in row.items() if k != "state"} for row in rows]
        except ValueError:
            pass
    hop = network.get("hop1")
    if isinstance(hop, dict):
        import re
        normalized = dict(hop)
        normalized["stdout"] = re.sub(r"\d+(?:\.\d+)? ms", "<timing>", hop.get("stdout", ""))
        network["hop1"] = normalized
    if "network" in result:
        result["network"] = network
    return result


def diff_probes(plain: dict[str, Any], monitored: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for check in CHECKS:
        if check == "timing":
            left, right = plain[check], monitored[check]
            ratio = right["open_read_close_ns"] / max(left["open_read_close_ns"], 1)
            checks[check] = {"pass": ratio <= TIMING_RATIO and left.get("connect_outcome") == right.get("connect_outcome"),
                             "plain_ns": left["open_read_close_ns"],
                             "monitored_ns": right["open_read_close_ns"],
                             "delta_ns": right["open_read_close_ns"] - left["open_read_close_ns"],
                             "ratio": ratio, "bound": TIMING_RATIO,
                             "plain_first_connect_ns": left.get("first_connect_ns"),
                             "monitored_first_connect_ns": right.get("first_connect_ns"),
                             "plain_connect_outcome": left.get("connect_outcome"),
                             "monitored_connect_outcome": right.get("connect_outcome")}
            continue
        left, right = canonicalize(check, plain[check]), canonicalize(check, monitored[check])
        if check == "dns":
            left, right = _network_comparable(left), _network_comparable(right)
        detail: dict[str, Any] = {"pass": left == right, "raw_identical": plain[check] == monitored[check]}
        if left != right:
            if isinstance(left, list):
                ls = {json.dumps(v, sort_keys=True) for v in left}
                rs = {json.dumps(v, sort_keys=True) for v in right}
                detail.update(only_plain=sorted(ls - rs), only_monitored=sorted(rs - ls))
            else:
                detail.update(plain=left, monitored=right)
        if check == "dns" and plain[check] != monitored[check]:
            detail.update(raw_plain=plain[check], raw_monitored=monitored[check])
        checks[check] = detail
    return {"pass": all(c["pass"] for c in checks.values()), "checks": checks}


@dataclass
class ProbeResult:
    passed: bool
    checks: dict[str, Any]
    plain: dict[str, Any]
    monitored: dict[str, Any]
    events: int
    egress: str
    output_dir: str


async def _exec_probe(container: str) -> dict[str, Any]:
    code, stdout, stderr = await docker("exec", container, "python3", "/tmp/invisibility-probe.py", timeout=60)
    if code:
        raise RuntimeError(f"probe exec failed: {stderr}")
    return json.loads(stdout.strip().splitlines()[-1])


async def run_probe(*, image: str | None = None, egress: str = "locked",
                    output_dir: Path | None = None) -> ProbeResult:
    flight_image = await ensure_image()
    token = uuid4().hex[:12]
    target, watcher = f"evallab-flight-probe-{token}", f"evallab-flight-watch-{token}"
    network = "none" if egress == "locked" else f"evallab-flight-net-{token}"
    out = output_dir or Path(tempfile.mkdtemp(prefix="flight-probe-"))
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"probe output directory must be empty: {out}")
    script = out / "invisibility-probe.py"
    if egress == "open":
        code, _, error = await docker("network", "create", network)
        if code:
            raise RuntimeError(error)
    try:
        code, _, error = await docker("run", "--detach", "--name", target, "--network", network,
                                      "--entrypoint", "sleep", image or flight_image, "300")
        if code:
            raise RuntimeError(error)
        script.write_text(PROBE_PY)
        code, _, error = await docker("cp", str(script), f"{target}:/tmp/invisibility-probe.py")
        if code:
            raise RuntimeError(error)
        # Warm network/caches, then compare the SAME container with unchanged
        # image, env, mounts and namespace. No target mutation between samples.
        await _exec_probe(target)
        plain = await _exec_probe(target)
        sniff = ""
        if egress == "open":
            found = container_ip(await inspect_container(target))
            assert found is not None
            interface = await bridge_interface(found[0])
            assert interface is not None
            sniff = f"{interface},{found[1]}"
        observer = await start_observer(
            image=flight_image, observer_name=watcher, container=target, output_dir=out,
            phase="probe", sniff=sniff, enforcement="none" if egress == "open" else "network_mode_none",
            identity={"trial_id": token, "job_id": "invisibility-probe", "task": "probe", "task_id": "probe"})
        try:
            monitored = await _exec_probe(target)
        finally:
            await stop_observer(observer.name)
        events_path = out / "events.jsonl"
        events = sum(1 for line in events_path.read_text().splitlines() if line.strip())
        diff = diff_probes(plain, monitored)
        result = ProbeResult(bool(diff["pass"]), diff["checks"], plain, monitored, events, egress, str(out))
        (out / "probe.json").write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
        return result
    finally:
        script.unlink(missing_ok=True)
        await docker("rm", "--force", target)
        await docker("rm", "--force", watcher)
        if egress == "open":
            await docker("network", "rm", network)
