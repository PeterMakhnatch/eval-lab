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

import asyncio
import re
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: ``verification`` payload version inside ``egress-lock.json`` (record
#: ``schema_version`` 3).
VERIFICATION_SCHEMA_VERSION = 1

#: Seconds a single connect or lookup may run inside the sandbox.
PROBE_INNER_TIMEOUT_SEC = 5
DNS_INNER_TIMEOUT_SEC = 8
#: Slack between the in-sandbox ``timeout`` and the exec call's own deadline.
EXEC_SLACK_SEC = 7
#: Outer asyncio deadline slack: an exec that never returns is a probe error.
OUTER_SLACK_SEC = 5

BLOCKED = "blocked"
REACHABLE = "reachable"
ERROR = "error"
CONTROL_OK = "control_ok"

#: Messages bash prints when a connect is refused or unrouted. Exit status 1
#: with anything else (for example a bash built without ``/dev/tcp``) is a
#: broken probe, not a block.
_NETWORK_FAILURE = re.compile(
    r"Connection refused|Network is unreachable|No route to host|Connection timed out"
    r"|Host is unreachable|Operation not permitted|Permission denied"
)

ExecFn = Callable[[str, int], Awaitable[Any]]


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


def classify(probe: Probe, return_code: Any, stdout: str, stderr: str) -> tuple[str, str]:
    """Map one probe's exit status and output to ``(outcome, detail)``."""
    if not isinstance(return_code, int) or isinstance(return_code, bool):
        return ERROR, f"no integer exit status ({return_code!r})"
    if probe.kind == "control_tcp":
        if return_code == 0 or (return_code == 1 and "Connection refused" in stderr):
            return CONTROL_OK, "loopback connect behaved as a working /dev/tcp probe"
        return ERROR, f"loopback control failed (exit {return_code}): {_tail(stderr)}"
    if probe.kind == "control_dns":
        if return_code == 0 and stdout.strip():
            return CONTROL_OK, "getent resolved localhost from local files"
        return ERROR, f"local resolver control failed (exit {return_code}): {_tail(stderr)}"
    if probe.kind == "tcp":
        if return_code == 0:
            return REACHABLE, "TCP connect succeeded"
        if return_code == 124:
            return BLOCKED, "connect timed out"
        if return_code == 1 and _NETWORK_FAILURE.search(stderr):
            return BLOCKED, _tail(stderr)
        return ERROR, f"probe could not be interpreted (exit {return_code}): {_tail(stderr)}"
    if probe.kind == "dns":
        if return_code == 0 and stdout.strip():
            return REACHABLE, f"resolved: {_tail(stdout)}"
        if return_code in (2, 124):
            return BLOCKED, "lookup timed out" if return_code == 124 else "no answer"
        return ERROR, f"probe could not be interpreted (exit {return_code}): {_tail(stderr)}"
    return ERROR, f"unknown probe kind {probe.kind!r}"


def _tail(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text[-limit:]


async def _run_probe(execute: ExecFn, probe: Probe) -> dict[str, Any]:
    started = time.monotonic()
    exit_code: int | None = None
    try:
        raw = await asyncio.wait_for(
            execute(probe.command, probe.exec_timeout_sec),
            timeout=probe.exec_timeout_sec + OUTER_SLACK_SEC,
        )
        exit_code = getattr(raw, "return_code", None)
        outcome, detail = classify(
            probe,
            exit_code,
            str(getattr(raw, "stdout", None) or ""),
            str(getattr(raw, "stderr", None) or ""),
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        outcome, detail = ERROR, "exec did not return before its deadline"
    except Exception as error:
        outcome, detail = ERROR, f"exec failed: {type(error).__name__}: {error}"
    return {
        "name": probe.name,
        "kind": probe.kind,
        "family": probe.family,
        "target": probe.target,
        "outcome": outcome,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "detail": detail,
        "duration_sec": round(time.monotonic() - started, 3),
    }


async def run_probes(execute: ExecFn, probes: Sequence[Probe] = PROBES) -> list[dict[str, Any]]:
    """Run every probe through ``execute(command, timeout_sec)``, one record each.

    Probes run concurrently so the lock adds one timeout, not the sum of all
    of them, to a trial's start. ``execute`` returns an object with
    ``return_code``, ``stdout`` and ``stderr``. Any exception, hang or
    malformed result is that probe's ``error`` outcome; nothing here raises
    except cancellation. Records keep the plan's order.
    """
    return list(await asyncio.gather(*(_run_probe(execute, probe) for probe in probes)))


def verdict(
    results: Sequence[Mapping[str, Any]], probes: Sequence[Probe] = PROBES
) -> tuple[bool, str]:
    """``(verified, reason)``: verified only if the whole plan ran and all blocked."""
    by_name = {item.get("name"): item for item in results}
    missing = [probe.name for probe in probes if probe.name not in by_name]
    if missing:
        return False, f"probes did not run: {', '.join(missing)}"
    controls = [p for p in probes if p.kind.startswith("control_")]
    failed_controls = [p.name for p in controls if by_name[p.name]["outcome"] != CONTROL_OK]
    if failed_controls:
        return False, f"probe tooling unusable (controls failed): {', '.join(failed_controls)}"
    network = [p for p in probes if not p.kind.startswith("control_")]
    leaked = [p.name for p in network if by_name[p.name]["outcome"] == REACHABLE]
    if leaked:
        return False, f"network reachable after lock: {', '.join(leaked)}"
    errored = [p.name for p in network if by_name[p.name]["outcome"] != BLOCKED]
    if errored:
        return False, f"block could not be established (probe errors): {', '.join(errored)}"
    return True, "controls passed and every DNS and TCP probe was blocked"


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
