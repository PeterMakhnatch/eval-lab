"""Security audit gate: block a release whose lockfile pins a package with a serious advisory."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

SEVERITY = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


@dataclass(frozen=True)
class GatePolicy:
    block_at: str = "HIGH"
    ignored_advisories: tuple[str, ...] = ()


def load_policy(path: str | Path) -> GatePolicy:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("security_audit_gate", {})
    return GatePolicy(
        block_at=data.get("block_at", "HIGH"),
        ignored_advisories=tuple(data.get("ignored_advisories", ())),
    )


def load_advisories(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_lock(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, version = line.split("==")
            pins[name.strip()] = version.strip()
    return pins


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def security_audit_gate(pins: dict[str, str], advisories: list[dict], policy: GatePolicy) -> list[str]:
    """Return the blocking findings; the gate passes when the list is empty."""
    findings = []
    for advisory in advisories:
        pinned = pins.get(advisory["package"])
        if pinned is None or advisory["id"] in policy.ignored_advisories:
            continue
        if SEVERITY.index(advisory["severity"]) < SEVERITY.index(policy.block_at):
            continue
        if _version(pinned) < _version(advisory["fixed_in"]):
            findings.append(
                f"{advisory['id']} {advisory['severity']}: {advisory['package']}=={pinned} "
                f"(fixed in {advisory['fixed_in']})"
            )
    return findings
