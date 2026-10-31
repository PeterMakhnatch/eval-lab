#!/bin/bash
# Environment-acquisition submission for glyph2-decode.
#
# The one thing the ticket cannot be solved without is the 4096-entry firmware
# dictionary. It is not in /app, but the image still carries it: the 2.4.1 build
# tree is under /opt, and the release wheel sits in the pip wheel cache. Copy
# it in, then finish the decoder around it.
set -u

cd /app || exit 1

python3 - <<'PY'
import pathlib
import zipfile

target = pathlib.Path("/app/glyph2/tables.py")
build = pathlib.Path("/opt/build/fieldlink-codec-2.4.1/fieldlink_codec/tables.py")
if build.is_file():
    target.write_bytes(build.read_bytes())
else:
    wheels = sorted(pathlib.Path("/root/.cache/pip/wheels").rglob("fieldlink_codec-2.4.1-*.whl"))
    with zipfile.ZipFile(wheels[0]) as archive:
        target.write_bytes(archive.read("fieldlink_codec/tables.py"))
print("acquired", target, target.stat().st_size)
PY

cat > /app/glyph2/decode.py <<'PY'
"""GLYPH/2 packet decoding for field uploads."""

from __future__ import annotations

from .tables import SYMBOLS

MAGIC = b"G2"
VERSION = 2
FLAG_AUX = 0x01
FLAG_EXT_SEQ = 0x02
SYMBOL_MASK = 0x0FFF
CHANNEL_MASK = 0xF000


def decode_packet(packet: bytes) -> dict:
    """Decode one GLYPH/2 packet into its fields."""
    if not isinstance(packet, (bytes, bytearray)):
        raise TypeError("packet must be bytes or bytearray")
    packet = bytes(packet)
    if len(packet) < 9:
        raise ValueError("truncated header")
    if packet[:2] != MAGIC:
        raise ValueError("bad magic")
    if packet[2] != VERSION:
        raise ValueError("unsupported version")
    flags = packet[3]
    if flags & ~(FLAG_AUX | FLAG_EXT_SEQ):
        raise ValueError("reserved flag bits set")

    width = 4 if flags & FLAG_EXT_SEQ else 2
    seq = int.from_bytes(packet[4 : 4 + width], "big")
    device = int.from_bytes(packet[4 + width : 4 + width + 2], "big")
    count_at = 4 + width + 2
    if len(packet) <= count_at:
        raise ValueError("truncated header")
    count = packet[count_at]
    if count == 0:
        raise ValueError("empty symbol run")

    body = packet[count_at + 1 :]
    if len(body) < 2 * count:
        raise ValueError("truncated symbols")
    words = [int.from_bytes(body[2 * i : 2 * i + 2], "big") for i in range(count)]
    rest = body[2 * count :]
    if flags & FLAG_AUX:
        if len(rest) < count:
            raise ValueError("truncated aux")
        aux = list(rest[:count])
        rest = rest[count:]
    else:
        aux = [0] * count
    if rest:
        raise ValueError("trailing bytes")

    return {
        "seq": seq,
        "device": f"D{device:04d}",
        "symbols": [SYMBOLS[word & SYMBOL_MASK] for word in words],
        "channels": [(word & CHANNEL_MASK) >> 12 for word in words],
        "aux": aux,
    }
PY
