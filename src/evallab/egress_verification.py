from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

PROBE_INNER_TIMEOUT_SEC = 5
DNS_INNER_TIMEOUT_SEC = 8
EXEC_SLACK_SEC = 7


_NETWORK_FAILURE = re.compile(
    r"Connection refused|Network is unreachable|No route to host|Connection timed out"
    r"|Host is unreachable|Operation not permitted|Permission denied"
)


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str
    family: str | None
    target: str
    command: str
    inner_timeout_sec: int

    @property
    def exec_timeout_sec(self) -> int:
        return self.inner_timeout_sec + EXEC_SLACK_SEC


def _tcp(name: str, family: str, address: str, port: int) -> Probe:
    inner = f"exec 3<>/dev/tcp/{address}/{port}"
    command = f"timeout {PROBE_INNER_TIMEOUT_SEC} bash -c {shlex.quote(inner)}"
    return Probe(name, "tcp", family, f"{address}:{port}", command, PROBE_INNER_TIMEOUT_SEC)


def _dns(name: str, family: str, host: str) -> Probe:
    database = "ahostsv4" if family == "v4" else "ahostsv6"
    command = f"timeout {DNS_INNER_TIMEOUT_SEC} getent {database} {shlex.quote(host)}"
    return Probe(name, "dns", family, host, command, DNS_INNER_TIMEOUT_SEC)


PROBES: tuple[Probe, ...] = (
    Probe(
        "control_loopback_tcp",
        "control_tcp",
        None,
        "127.0.0.1:9",
        f"timeout {PROBE_INNER_TIMEOUT_SEC} bash -c {shlex.quote('exec 3<>/dev/tcp/127.0.0.1/9')}",
        PROBE_INNER_TIMEOUT_SEC,
    ),
    Probe(
        "control_local_dns",
        "control_dns",
        None,
        "localhost",
        f"timeout {DNS_INNER_TIMEOUT_SEC} getent hosts localhost",
        DNS_INNER_TIMEOUT_SEC,
    ),
    _dns("dns_a", "v4", "example.com"),
    _dns("dns_aaaa", "v6", "example.com"),
    _tcp("tcp4_cloudflare", "v4", "1.1.1.1", 443),
    _tcp("tcp4_google", "v4", "8.8.8.8", 443),
    _tcp("tcp6_cloudflare", "v6", "2606:4700:4700::1111", 443),
    _tcp("tcp6_google", "v6", "2001:4860:4860::8888", 443),
)


def lock_enforced(record: object) -> bool:
    if not isinstance(record, dict) or record.get("applied") is not True or record.get("error"):
        return False
    if "verified" in record:
        return record["verified"] is True
    return "daytona" not in str(record.get("mechanism") or "").lower()
