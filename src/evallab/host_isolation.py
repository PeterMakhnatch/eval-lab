"""Pure rules for denying sandbox access to the controller and private networks."""

from __future__ import annotations

import ipaddress
import re

# Destination ranges, never listener/port blocklists. Loopback is allowed only
# on the sandbox's own lo interface; Docker's DNS stub also uses that interface.
BLOCKED_IPV4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def blocked_addresses(ifconfig_text: str) -> tuple[str, ...]:
    """Extract controller interface addresses without retaining host metadata."""
    addresses = set()
    for match in re.finditer(r"^\s*inet6?\s+(\S+)", ifconfig_text, re.MULTILINE):
        address = match[1].split("%", 1)[0]
        addresses.add(str(ipaddress.ip_address(address)))
    return tuple(sorted(addresses))


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if "%" in value:
        raise ValueError("scoped addresses are not nft address literals")
    return ipaddress.ip_address(value)


def nft_rules(
    host_addresses: tuple[str, ...],
    bridge_addresses: tuple[str, ...] = (),
    bridge_port: int | None = None,
) -> str:
    """Atomic inet output filter; only an exact Claude TCP tuple bypasses denial.

    The controller installs this before attaching a network. Sandbox processes
    lack NET_ADMIN and NET_RAW, so root cannot remove the table or spoof packets.
    Public internet remains available for CLI installation and hosted APIs.
    """
    hosts = sorted({_address(value) for value in host_addresses}, key=str)
    bridge = sorted({_address(value) for value in bridge_addresses}, key=str)
    if bool(bridge) != (bridge_port is not None):
        raise ValueError("bridge address and port must be supplied together")
    if bridge_port is not None and (
        isinstance(bridge_port, bool)
        or not isinstance(bridge_port, int)
        or not 1 <= bridge_port <= 65535
    ):
        raise ValueError("invalid bridge port")
    lines = [
        "table inet evallab_host_isolation {",
        "  chain output {",
        "    type filter hook output priority -200; policy accept;",
        '    oifname "lo" accept',
    ]
    for address in bridge:
        family = "ip" if address.version == 4 else "ip6"
        lines.append(f"    {family} daddr {address} tcp dport {bridge_port} accept")
    for address in hosts:
        family = "ip" if address.version == 4 else "ip6"
        lines.append(f"    {family} daddr {address} reject")
    lines.extend(
        [
            "    ip daddr { " + ", ".join(BLOCKED_IPV4) + " } reject",
            "    ip6 daddr != 2000::/3 reject",
            "  }",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def desktop_dns_addresses(nslookup_text: str) -> tuple[str, ...]:
    """Parse only host.docker.internal answers from trusted tool DNS output."""
    addresses = set()
    in_answer = False
    for line in nslookup_text.splitlines():
        if line.startswith("Name:"):
            in_answer = line.split(":", 1)[1].strip() == "host.docker.internal"
        elif line.startswith("Server:"):
            in_answer = False
        elif in_answer and line.startswith("Address:"):
            addresses.add(str(_address(line.split(":", 1)[1].strip())))
    if not addresses:
        raise ValueError("Docker Desktop host DNS answers are unavailable")
    return tuple(sorted(addresses))


def controller_addresses() -> tuple[str, ...]:
    """Inventory every actual controller interface, including public addresses.

    This is intentionally fail closed; an unavailable inventory cannot establish
    the destination boundary. Remote Docker contexts are unsupported.
    """
    import json
    import subprocess
    import sys

    if sys.platform == "darwin":
        output = subprocess.run(["/sbin/ifconfig"], check=True, capture_output=True, text=True)
        addresses = blocked_addresses(output.stdout)
    else:
        output = subprocess.run(
            ["ip", "-json", "address"], check=True, capture_output=True, text=True
        )
        addresses = tuple(
            sorted(
                {
                    str(_address(info["local"]))
                    for interface in json.loads(output.stdout)
                    for info in interface["addr_info"]
                }
            )
        )
    if not addresses:
        raise RuntimeError("controller interface address inventory is empty")
    return addresses


def nft_objects(document: dict, rule_count: int) -> list[dict]:
    """Normalize installer echo/kernel listing, refusing absent or unrelated rules."""
    objects = []
    for item in document.get("nftables", []):
        if "metainfo" in item:
            continue
        item = item.get("add", item)
        if set(item) not in ({"table"}, {"chain"}, {"rule"}):
            raise ValueError("unexpected nft object")
        objects.append(item)
    tables = [item["table"] for item in objects if "table" in item]
    chains = [item["chain"] for item in objects if "chain" in item]
    rules = [item["rule"] for item in objects if "rule" in item]
    if (
        len(tables) != 1
        or tables[0].get("family") != "inet"
        or tables[0].get("name") != "evallab_host_isolation"
        or len(chains) != 1
        or chains[0].get("family") != "inet"
        or chains[0].get("table") != "evallab_host_isolation"
        or chains[0].get("name") != "output"
        or chains[0].get("hook") != "output"
        or chains[0].get("prio") != -200
        or chains[0].get("type") != "filter"
        or chains[0].get("policy") != "accept"
        or len(rules) != rule_count
        or any(
            rule.get("family") != "inet"
            or rule.get("table") != "evallab_host_isolation"
            or rule.get("chain") != "output"
            or not rule.get("expr")
            for rule in rules
        )
    ):
        raise ValueError("host-isolation kernel rules are incomplete or unrelated")
    return objects
