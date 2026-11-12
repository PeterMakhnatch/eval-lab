"""In-sandbox verification of an egress lock (HAR-205).

``egress-lock.json`` ``applied`` records that the provider *acknowledged* a
network block. Two of fourteen auditable locked Daytona trials reached the
internet minutes after such an acknowledgement, so the acknowledgement is not
evidence of a block. This module owns the evidence that is:

* a fixed probe plan executed from inside the sandbox after the provider call
  (DNS resolution and TCP connects over IPv4 and IPv6, each recorded
  separately) plus two loopback controls that prove the probe tooling works;
* the verdict rule: verified only when both controls pass and every network
  probe shows a block; any reachability, tool error, exec failure or timeout
  leaves the lock unverified;
* :func:`lock_enforced`, the one reader every consumer of a lock record uses
  to decide whether a trial counted as blocked.

The probe plan is deliberately free of the lab's ``/etc/hosts`` answer-leak
blocklist names so a hosts entry cannot mask a leak. Which sandboxes leak, and
whether the unfiltered path is IPv6, is an open question the live battery in
``research/experiments/har205-egress-verify/PROPOSAL.md`` would answer; nothing
here asserts a cause.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

#: ``verification`` payload version inside ``egress-lock.json`` (record
#: ``schema_version`` 3).

#: Seconds a single connect or lookup may run inside the sandbox.
PROBE_INNER_TIMEOUT_SEC = 5
DNS_INNER_TIMEOUT_SEC = 8
#: Slack between the in-sandbox ``timeout`` and the exec call's own deadline.
EXEC_SLACK_SEC = 7
#: Outer asyncio deadline slack: an exec that never returns is a probe error.


#: Messages bash prints when a connect is refused or unrouted. Exit status 1
#: with anything else (for example a bash built without ``/dev/tcp``) is a
#: broken probe, not a block.
_NETWORK_FAILURE = re.compile(
    r"Connection refused|Network is unreachable|No route to host|Connection timed out"
    r"|Host is unreachable|Operation not permitted|Permission denied"
)


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str  # "tcp" | "dns" | "control_tcp" | "control_dns"
    family: str | None  # "v4" | "v6" | None for controls
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
    """True only when a trial's ``egress-lock.json`` shows an enforced block.

    ``applied`` alone is a provider acknowledgement. A record that carries the
    HAR-205 ``verified`` field counts only if it is ``True``. A Daytona record
    written before HAR-205 (identified by its mechanism) has no in-sandbox
    evidence and therefore never counts. Other backends (locked Docker,
    Modal) record their own enforcement observation in ``applied`` and are
    read as before.
    """
    if not isinstance(record, dict) or record.get("applied") is not True or record.get("error"):
        return False
    if "verified" in record:
        return record["verified"] is True
    return "daytona" not in str(record.get("mechanism") or "").lower()
