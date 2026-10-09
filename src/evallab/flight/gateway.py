"""Passive, not enforcing, metadata observer at the native Docker bridge.

Packets exist only transiently in memory. Exported records contain an explicit
allowlist: endpoints, DNS names/answers, TLS ClientHello SNI, transport outcome,
packet/byte counters and timestamps. Never application or TLS bodies.
"""
from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def dns_name(data: bytes, offset: int, *, depth: int = 0) -> tuple[str, int]:
    if depth > 12:
        raise ValueError("DNS compression loop")
    labels: list[str] = []
    while offset < len(data):
        length = data[offset]
        offset += 1
        if length == 0:
            return ".".join(labels), offset
        if length & 0xC0 == 0xC0:
            if offset >= len(data):
                raise ValueError("short DNS pointer")
            pointer = ((length & 0x3F) << 8) | data[offset]
            suffix, _ = dns_name(data, pointer, depth=depth + 1)
            labels.append(suffix)
            return ".".join(labels), offset + 1
        if length > 63 or offset + length > len(data):
            raise ValueError("short DNS label")
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
    raise ValueError("unterminated DNS name")


def dns_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 12:
        return None
    try:
        ident, flags, nq, na, _, _ = struct.unpack_from("!6H", data)
        if nq > 32 or na > 128:
            return None
        questions: list[dict[str, Any]] = []
        answers: list[dict[str, Any]] = []
        offset = 12
        for _ in range(nq):
            name, offset = dns_name(data, offset)
            qtype, _ = struct.unpack_from("!HH", data, offset)
            offset += 4
            questions.append({"name": name, "type": qtype})
        for _ in range(na):
            name, offset = dns_name(data, offset)
            atype, _, ttl, size = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            end = offset + size
            if end > len(data):
                return None
            answer: str | None = None
            if atype == 1 and size == 4:
                answer = str(ipaddress.IPv4Address(data[offset:end]))
            elif atype == 28 and size == 16:
                answer = str(ipaddress.IPv6Address(data[offset:end]))
            elif atype in (5, 2, 12):
                answer, _ = dns_name(data, offset)
            if answer is not None:
                answers.append({"name": name, "type": atype, "ttl": ttl, "answer": answer})
            offset = end
        return {"id": ident, "response": bool(flags & 0x8000), "rcode": flags & 15,
                "questions": questions, "answers": answers}
    except (ValueError, struct.error):
        return None


def client_hello_sni(data: bytes) -> str | None:
    """Extract only SNI from a bounded, unencrypted TLS ClientHello."""
    if len(data) < 9 or data[0] != 22 or data[5] != 1:
        return None
    try:
        record_end = 5 + int.from_bytes(data[3:5], "big")
        if record_end > len(data):
            return None
        offset = 9 + 2 + 32
        offset += 1 + data[offset]
        offset += 2 + int.from_bytes(data[offset:offset + 2], "big")
        offset += 1 + data[offset]
        extensions_end = offset + 2 + int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        while offset + 4 <= min(record_end, extensions_end):
            kind, size = struct.unpack_from("!HH", data, offset)
            offset += 4
            if offset + size > len(data):
                return None
            if kind == 0 and size >= 5:
                name_type = data[offset + 2]
                length = int.from_bytes(data[offset + 3:offset + 5], "big")
                if name_type == 0 and length <= size - 5:
                    return data[offset + 5:offset + 5 + length].decode("ascii", "replace")
            offset += size
    except (IndexError, struct.error):
        pass
    return None


@dataclass(frozen=True)
class Packet:
    src: str
    dst: str
    sport: int
    dport: int
    protocol: str
    length: int
    flags: int = 0
    sequence: int = 0
    payload: bytes = b""


def decode_packet(frame: bytes) -> Packet | None:
    """Decode Ethernet IPv4/IPv6 TCP/UDP; payload stays internal, never a row."""
    if len(frame) < 14:
        return None
    ethertype = int.from_bytes(frame[12:14], "big")
    offset = 14
    if ethertype == 0x8100 and len(frame) >= 18:
        ethertype, offset = int.from_bytes(frame[16:18], "big"), 18
    try:
        if ethertype == 0x0800:
            header_size = (frame[offset] & 15) * 4
            size = int.from_bytes(frame[offset + 2:offset + 4], "big")
            if header_size < 20 or frame[offset + 6] & 0x1F or frame[offset + 7]:
                return None  # fragmented packets cannot safely supply transport metadata
            protocol = frame[offset + 9]
            src = str(ipaddress.IPv4Address(frame[offset + 12:offset + 16]))
            dst = str(ipaddress.IPv4Address(frame[offset + 16:offset + 20]))
            end = min(len(frame), offset + size)
            offset += header_size
        elif ethertype == 0x86DD:
            size = int.from_bytes(frame[offset + 4:offset + 6], "big") + 40
            protocol = frame[offset + 6]
            src = str(ipaddress.IPv6Address(frame[offset + 8:offset + 24]))
            dst = str(ipaddress.IPv6Address(frame[offset + 24:offset + 40]))
            end = min(len(frame), offset + size)
            offset += 40
        else:
            return None
        sport, dport = struct.unpack_from("!HH", frame, offset)
        if protocol == 6:
            header_size = (frame[offset + 12] >> 4) * 4
            if header_size < 20 or offset + header_size > end:
                return None
            sequence = int.from_bytes(frame[offset + 4:offset + 8], "big")
            return Packet(src, dst, sport, dport, "tcp", size, frame[offset + 13], sequence,
                          frame[offset + header_size:end])
        if protocol == 17 and offset + 8 <= end:
            return Packet(src, dst, sport, dport, "udp", size, payload=frame[offset + 8:end])
    except (ValueError, IndexError, struct.error):
        return None
    return None


#: Retained TLS ClientHello bytes per flow (~4 KiB). Oversized, gapped, or
#: non-TLS payloads drop the reassembly buffer instead of growing it.
TLS_HELLO_CAP = 4096

#: Tracked-flow cap; the oldest flow is evicted (LRU) past this bound so a
#: busy bridge cannot grow gateway memory without limit.
MAX_FLOWS = 2048

@dataclass
class Flow:
    src: str
    dst: str
    sport: int
    dport: int
    protocol: str
    first: float
    last: float
    packets: int = 0
    bytes: int = 0
    outcome: str = "unknown"
    hello: bytearray = field(default_factory=bytearray)
    next_sequence: int | None = None
    sni: str | None = None

    def row(self) -> dict[str, Any]:
        outcome = "timeout" if self.outcome == "syn" and self.last - self.first >= 3 else self.outcome
        return {"plane": "egress", "kind": "flow", "enforcement": "none",
                "mode": "passive", "ts": timestamp(self.first), "finished_at": timestamp(self.last),
                "src": self.src, "dst": self.dst, "sport": self.sport, "dport": self.dport,
                "protocol": self.protocol, "packets": self.packets, "bytes": self.bytes,
                "outcome": outcome, "tls_sni": self.sni}


class MetadataGateway:
    """Strict trial-IP attribution with bounded memory (LRU flows, capped hellos)."""
    def __init__(self, trial_ips: set[str], *, max_flows: int = MAX_FLOWS) -> None:
        self.trial_ips = trial_ips
        self.flows: dict[tuple[str, str, int, int, str], Flow] = {}
        self.dropped_other = 0
        self.dropped_capacity = 0
        self.max_flows = max_flows

    def observe(self, frame: bytes, at: float) -> list[dict[str, Any]]:
        packet = decode_packet(frame)
        if packet is None:
            return []
        if packet.src not in self.trial_ips and packet.dst not in self.trial_ips:
            self.dropped_other += 1
            return []
        outgoing = packet.src in self.trial_ips
        src, dst = (packet.src, packet.dst) if outgoing else (packet.dst, packet.src)
        sport, dport = (packet.sport, packet.dport) if outgoing else (packet.dport, packet.sport)
        key = (src, dst, sport, dport, packet.protocol)
        flow = self.flows.get(key)
        if flow is None:
            if len(self.flows) >= self.max_flows:
                self.dropped_capacity += 1
                if self.max_flows <= 0:
                    return []
                del self.flows[next(iter(self.flows))]
            flow = Flow(src, dst, sport, dport, packet.protocol, at, at)
            self.flows[key] = flow
        else:
            self.flows[key] = self.flows.pop(key)
        flow.last = at
        flow.packets += 1
        flow.bytes += packet.length
        if packet.protocol == "tcp":
            if packet.flags & 4:
                flow.outcome = "reset"
            elif packet.flags & 0x12 == 0x12:
                flow.outcome = "syn_ack"
            elif packet.flags & 2:
                flow.outcome = "syn"
            elif flow.outcome == "syn_ack" and packet.flags & 16:
                flow.outcome = "established"
            if outgoing and packet.payload and flow.sni is None:
                payload = packet.payload
                if not flow.hello:
                    if payload[0] != 22:
                        flow.next_sequence = None
                    else:
                        flow.next_sequence = packet.sequence
                elif packet.sequence != flow.next_sequence:
                    flow.hello.clear()
                    flow.next_sequence = None
                if flow.next_sequence == packet.sequence and len(flow.hello) < TLS_HELLO_CAP:
                    flow.hello.extend(payload[:TLS_HELLO_CAP - len(flow.hello)])
                    flow.next_sequence = packet.sequence + len(payload)
                    flow.sni = client_hello_sni(bytes(flow.hello))
                    if flow.sni is not None:
                        flow.hello.clear()
                    elif len(flow.hello) >= TLS_HELLO_CAP:
                        flow.hello.clear()
                        flow.next_sequence = None
        elif flow.outcome == "unknown":
            flow.outcome = "sent" if outgoing else "response"
        elif not outgoing:
            flow.outcome = "response"
        if 53 in (packet.sport, packet.dport):
            payload = packet.payload
            if packet.protocol == "tcp":
                payload = payload[2:]
            metadata = dns_metadata(payload)
            if metadata is not None:
                return [{"plane": "egress", "kind": "dns", "enforcement": "none",
                         "mode": "passive", "ts": timestamp(at), "src": packet.src,
                         "dst": packet.dst, "sport": packet.sport, "dport": packet.dport,
                         "protocol": packet.protocol, **metadata}]
        return []

    def finish(self, at: float) -> list[dict[str, Any]]:
        rows = []
        for flow in self.flows.values():
            if flow.outcome == "syn":
                flow.last = at
            rows.append(flow.row())
        return rows


class PcapStream:
    """Incremental pcap decoder; no packet file and no retained application data."""
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.endian: str | None = None
        self.nanoseconds = False

    def feed(self, data: bytes) -> list[tuple[float, bytes]]:
        self.buffer.extend(data)
        if self.endian is None:
            if len(self.buffer) < 24:
                return []
            magic = bytes(self.buffer[:4])
            if magic not in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4",
                             b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
                raise ValueError("not a pcap stream")
            self.endian = "<" if magic[0] in (0xD4, 0x4D) else ">"
            self.nanoseconds = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
            linktype = struct.unpack_from(self.endian + "I", self.buffer, 20)[0]
            if linktype != 1:
                raise ValueError(f"expected Ethernet pcap, got linktype {linktype}")
            del self.buffer[:24]
        rows = []
        while len(self.buffer) >= 16:
            sec, frac, size, _ = struct.unpack_from(self.endian + "4I", self.buffer)
            if size > 1_048_576:
                raise ValueError("pcap packet too large")
            if len(self.buffer) < 16 + size:
                break
            rows.append((sec + frac / (1e9 if self.nanoseconds else 1e6),
                         bytes(self.buffer[16:16 + size])))
            del self.buffer[:16 + size]
        return rows
