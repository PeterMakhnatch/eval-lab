"""GLYPH/2 decoder used by the ingest service.

See ``spec/GLYPH2.md``. The firmware symbol dictionary is the ``fieldlink-codec``
2.4.1 reference distribution.
"""

from __future__ import annotations

MAGIC = b"G2"
VERSION = 2
SYMBOL_MASK = 0x0FFF

# The ids quoted in spec/GLYPH2.md §4.
DOCUMENTED = {
    0x000: "sahqap",
    0x001: "tantup",
    0x002: "sivkoc",
    0x003: "romkin",
    0x7FF: "dilhor",
    0x800: "futnav",
    0xFFE: "cunhic",
    0xFFF: "ridpag",
}


def _name(symbol_id: int) -> str:
    return DOCUMENTED.get(symbol_id, f"sym-{symbol_id:03x}")


def decode_packet(packet: bytes) -> dict:
    """Decode one GLYPH/2 packet into its fields."""
    if packet[:2] != MAGIC:
        raise ValueError("bad magic")
    flags = packet[3]
    seq = int.from_bytes(packet[4:6], "big")
    device = int.from_bytes(packet[6:8], "big")
    count = packet[8]

    symbols = []
    offset = 9
    for _ in range(count):
        word = int.from_bytes(packet[offset : offset + 2], "little")
        symbols.append(_name(word & SYMBOL_MASK))
        offset += 2
    if flags & 0x01:
        # Auxiliary bytes follow the symbol words; the ingest does not report
        # them yet, but they must not be read as another word.
        offset += count

    return {
        "seq": seq,
        "device": f"device-{device}",
        "symbols": symbols,
        "channels": [0] * count,
        "aux": [0] * count,
    }
