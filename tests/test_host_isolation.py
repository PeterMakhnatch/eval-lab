"""Deterministic checks for the sandbox's immutable host-destination boundary."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import sys

import pytest

from evallab.host_isolation import (
    blocked_addresses,
    controller_addresses,
    desktop_dns_addresses,
    nft_objects,
    nft_rules,
)

_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
    inet6 ::1 prefixlen 128
    inet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ether aa:bb:cc:dd:ee:ff
    inet 192.168.1.35 netmask 0xffffff00 broadcast 192.168.1.255
    inet6 fe80::abcd%en0 prefixlen 64 secured scopeid 0xa
    inet6 2606:4700:0000:0000:0000:0000:0000:1111 prefixlen 64 autoconf secured
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
    inet 100.64.0.7 --> 100.64.0.7 netmask 0xffffffff
    inet6 fe80::abcd%utun0 prefixlen 64 scopeid 0xb
    inet 8.8.4.4 netmask 0xffffffff
"""


def _rule_lines(rules: str) -> list[str]:
    return [line.strip() for line in rules.splitlines() if line.strip()]


def _address_tokens(rules: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Read nft address literals without invoking a host firewall or daemon."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in re.split(r"[\s{},;]+", rules):
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return networks


def test_ifconfig_addresses_are_canonical_deduplicated_and_complete() -> None:
    addresses = blocked_addresses(_IFCONFIG)

    assert set(addresses) == {
        "127.0.0.1",
        "::1",
        "fe80::1",
        "192.168.1.35",
        "fe80::abcd",
        "2606:4700::1111",
        "100.64.0.7",
        "8.8.4.4",
    }
    assert len(addresses) == len(set(addresses))
    assert blocked_addresses(_IFCONFIG) == addresses
    assert all(str(ipaddress.ip_address(address)) == address for address in addresses)
    assert "192.168.1.255" not in addresses


def test_ifconfig_empty_inventory_is_empty() -> None:
    assert blocked_addresses("") == ()


def test_ifconfig_ignores_non_address_fields() -> None:
    assert blocked_addresses(
        "ether 00:11:22:33:44:55\ndescription: inet 8.8.8.8\n    inet 8.8.4.4 netmask 0xffffffff\n"
    ) == ("8.8.4.4",)


@pytest.mark.parametrize(
    "text",
    [
        "inet garbage netmask 0xffffffff\n",
        "inet6 invalid%en0 prefixlen 64\n",
        "inet 192.168.1.35;flush ruleset\n",
    ],
)
def test_malformed_ifconfig_inventory_fails_closed(text: str) -> None:
    with pytest.raises(ValueError):
        blocked_addresses(text)


@pytest.mark.parametrize(
    "address",
    [
        "host.docker.internal",
        "192.168.65.254/24",
        "192.168.65.254; accept",
        "192.168.65.254\nflush ruleset",
        "[::1]",
        "fe80::1%en0",
        "",
    ],
)
def test_host_rule_addresses_refuse_names_cidrs_scopes_and_injection(address: str) -> None:
    with pytest.raises(ValueError):
        nft_rules((address,))


@pytest.mark.parametrize(
    ("addresses", "port"),
    [
        (("192.168.65.254",), None),
        ((), 45123),
        (("192.168.65.254",), 0),
        (("192.168.65.254",), -1),
        (("192.168.65.254",), 65536),
        (("192.168.65.254",), True),
        (("host.docker.internal",), 45123),
        (("192.168.65.254; accept",), 45123),
        (("192.168.65.0/24",), 45123),
        (("fe80::1%en0",), 45123),
    ],
)
def test_bridge_exception_requires_valid_complete_tuple(
    addresses: tuple[str, ...], port: int | None
) -> None:
    with pytest.raises(ValueError):
        nft_rules((), addresses, port)


def test_public_egress_does_not_imply_access_to_any_host_port() -> None:
    rules = nft_rules(("8.8.4.4", "2606:4700::1111"))
    lines = _rule_lines(rules)

    assert re.search(r"type filter hook output priority -200; policy accept;", rules)
    assert any("oifname" in line and "lo" in line and "accept" in line for line in lines)
    assert "8.8.4.4" in rules
    assert "2606:4700::1111" in rules
    assert "dport" not in rules
    assert "sport" not in rules
    assert not any("accept" in line and "daddr" in line for line in lines)
    assert "established" not in rules
    assert "related" not in rules
    assert "mark" not in rules


@pytest.mark.parametrize(
    "destination",
    [
        "0.1.2.3",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.2",
        "169.254.169.254",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.65.254",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
    ],
)
def test_ipv4_boundary_covers_private_reserved_and_multicast_destinations(
    destination: str,
) -> None:
    rules = nft_rules(())
    address = ipaddress.ip_address(destination)

    assert any(address in network for network in _address_tokens(rules) if network.version == 4)
    assert not any("accept" in line and "daddr" in line for line in _rule_lines(rules))


def test_ipv6_boundary_denies_every_destination_outside_global_unicast() -> None:
    rules = nft_rules(())

    assert re.search(r"ip6 daddr != 2000::/3\s+(?:drop|reject)", rules)
    for destination in ("::", "::1", "::ffff:192.168.65.254", "fe80::1", "fd00::1", "ff02::1"):
        assert ipaddress.ip_address(destination) not in ipaddress.ip_network("2000::/3")


def test_only_exact_claude_bridge_tuple_precedes_host_destination_denial() -> None:
    addresses = ("192.168.65.254", "2606:4700::1111")
    rules = nft_rules(addresses, addresses, 45123)
    lines = _rule_lines(rules)
    exceptions = [line for line in lines if "daddr" in line and "accept" in line]

    assert len(exceptions) == 2
    assert all("tcp dport 45123" in line for line in exceptions)
    assert not any("udp" in line and "accept" in line for line in lines)
    for address in addresses:
        allowing = [
            index for index, line in enumerate(lines) if address in line and "accept" in line
        ]
        denying = [
            index
            for index, line in enumerate(lines)
            if address in line and re.search(r"\b(?:drop|reject)\b", line)
        ]
        assert len(allowing) == 1
        assert denying and allowing[0] < min(denying)
    assert "established" not in rules
    assert "related" not in rules
    assert "mark" not in rules


def test_address_literals_are_canonicalized_before_firewall_emission() -> None:
    rules = nft_rules(("2606:4700:0000:0000:0000:0000:0000:1111",))

    assert "2606:4700::1111" in rules
    assert "2606:4700:0000:0000:0000:0000:0000:1111" not in rules


def test_desktop_dns_parser_uses_only_host_alias_answers_and_canonicalizes() -> None:
    output = """\
/lib/ld-musl-aarch64.so.1 (0x0001)
Server: 192.168.65.7
Address: 192.168.65.7#53
Name: host.docker.internal
Address: 192.168.65.254
Address: 2606:4700:0000:0000:0000:0000:0000:1111
Name: other.example.invalid
Address: 8.8.8.8
Server: 192.168.65.7
Address: 192.168.65.7#53
Name: host.docker.internal
Address: 192.168.65.254
"""
    assert desktop_dns_addresses(output) == ("192.168.65.254", "2606:4700::1111")


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Server: 192.168.65.7\nAddress: 192.168.65.7#53\n",
        "Name: other.example.invalid\nAddress: 192.168.65.254\n",
        "Name: host.docker.internal\nAddress: 192.168.65.254;flush\n",
        "Name: host.docker.internal\nAddress: fe80::1%en0\n",
    ],
)
def test_absent_or_malformed_desktop_dns_answers_fail_closed(output: str) -> None:
    with pytest.raises(ValueError):
        desktop_dns_addresses(output)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_controller_inventory_uses_only_injected_platform_probe(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    calls = []
    if platform == "darwin":
        output = _IFCONFIG
        expected_command = ["/sbin/ifconfig"]
        expected = blocked_addresses(_IFCONFIG)
    else:
        output = json.dumps(
            [
                {"addr_info": [{"local": "8.8.4.4"}, {"local": "2606:4700:0:0:0:0:0:1111"}]},
                {"addr_info": [{"local": "8.8.4.4"}]},
            ]
        )
        expected_command = ["ip", "-json", "address"]
        expected = ("2606:4700::1111", "8.8.4.4")

    def run(command, **kwargs):
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert controller_addresses() == expected
    assert calls == [expected_command]


@pytest.mark.parametrize(("platform", "output"), [("darwin", ""), ("linux", "[]")])
def test_empty_controller_inventory_never_establishes_a_boundary(monkeypatch, platform, output):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=output),
    )
    with pytest.raises(RuntimeError, match="inventory is empty"):
        controller_addresses()


def test_controller_inventory_probe_failure_propagates_without_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        controller_addresses()


_TABLE = {"family": "inet", "name": "evallab_host_isolation"}
_CHAIN = {
    "family": "inet",
    "table": "evallab_host_isolation",
    "name": "output",
    "type": "filter",
    "hook": "output",
    "prio": -200,
    "policy": "accept",
}
_RULE = {
    "family": "inet",
    "table": "evallab_host_isolation",
    "chain": "output",
    "expr": [{"drop": None}],
}


def _nft_document(rule_count: int = 2, *, wrap: bool = False) -> dict:
    items = [
        {"metainfo": {"version": "1.0.9"}},
        {"table": dict(_TABLE)},
        {"chain": dict(_CHAIN)},
        *({"rule": dict(_RULE)} for _ in range(rule_count)),
    ]
    if wrap:
        items = [item if "metainfo" in item else {"add": item} for item in items]
    return {"nftables": items}


def _mutated(kind: str, **changes: object) -> dict:
    document = _nft_document()
    for item in document["nftables"]:
        if kind in item:
            item[kind].update(changes)
    return document


def test_nft_objects_accepts_kernel_listing_skipping_metainfo() -> None:
    objects = nft_objects(_nft_document(), 2)

    assert [next(iter(item)) for item in objects] == ["table", "chain", "rule", "rule"]


def test_nft_objects_accepts_installer_echo_add_wrappers() -> None:
    assert nft_objects(_nft_document(wrap=True), 2) == nft_objects(_nft_document(), 2)


def test_nft_objects_refuses_document_without_table_and_chain() -> None:
    with pytest.raises(ValueError, match="incomplete or unrelated"):
        nft_objects({}, 0)


def _two_tables() -> dict:
    document = _nft_document()
    document["nftables"].append({"table": {"family": "inet", "name": "other"}})
    return document


def _two_chains() -> dict:
    document = _nft_document()
    document["nftables"].append({"chain": dict(_CHAIN, name="forward")})
    return document


def _rule_without_expr() -> dict:
    document = _nft_document()
    del document["nftables"][-1]["rule"]["expr"]
    return document


def _rule_with_empty_expr() -> dict:
    document = _nft_document()
    document["nftables"][-1]["rule"]["expr"] = []
    return document


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(_two_tables(), id="two-tables"),
        pytest.param(
            {"nftables": [{"chain": dict(_CHAIN)}, {"rule": dict(_RULE)}, {"rule": dict(_RULE)}]},
            id="no-table",
        ),
        pytest.param(_mutated("table", family="ip"), id="table-family"),
        pytest.param(_mutated("table", name="other"), id="table-name"),
        pytest.param(_two_chains(), id="two-chains"),
        pytest.param(_mutated("chain", family="ip"), id="chain-family"),
        pytest.param(_mutated("chain", table="other"), id="chain-table"),
        pytest.param(_mutated("chain", name="input"), id="chain-name"),
        pytest.param(_mutated("chain", hook="input"), id="chain-hook"),
        pytest.param(_mutated("chain", prio=0), id="chain-prio"),
        pytest.param(_mutated("chain", type="nat"), id="chain-type"),
        pytest.param(_mutated("chain", policy="drop"), id="chain-policy"),
        pytest.param(_nft_document(1), id="too-few-rules"),
        pytest.param(_nft_document(3), id="too-many-rules"),
        pytest.param(_rule_without_expr(), id="rule-without-expr"),
        pytest.param(_rule_with_empty_expr(), id="rule-empty-expr"),
    ],
)
def test_nft_objects_refuses_incomplete_or_unrelated_listing(document: dict) -> None:
    with pytest.raises(ValueError, match="incomplete or unrelated"):
        nft_objects(document, 2)


@pytest.mark.parametrize("field", ["family", "table", "chain"])
def test_nft_objects_refuses_rule_in_foreign_table_family_or_chain(field: str) -> None:
    document = _nft_document()
    document["nftables"][-1]["rule"][field] = "other"

    with pytest.raises(ValueError, match="incomplete or unrelated"):
        nft_objects(document, 2)


@pytest.mark.parametrize(
    "item",
    [
        {"set": {"name": "x"}},
        {"table": dict(_TABLE), "chain": dict(_CHAIN)},
        {"add": {"set": {"name": "x"}}},
        {},
    ],
    ids=["unknown-kind", "mixed-kinds", "wrapped-unknown-kind", "empty-object"],
)
def test_nft_objects_refuses_unexpected_object_kind(item: dict) -> None:
    document = _nft_document()
    document["nftables"].append(item)

    with pytest.raises(ValueError, match="unexpected nft object"):
        nft_objects(document, 2)
