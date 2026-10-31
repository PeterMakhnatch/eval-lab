#!/usr/bin/env python3
"""Generate the C5 fixture: the firmware symbol table, its vendored copies, and the grader reference.

Deterministic (fixed seed, fixed zip timestamps), so a rerun reproduces every
byte. Run from the experiment root:

    python3 tools/make_fixture.py

Outputs (all relative to the experiment root):
  tasks/glyph2-decode/environment/opt/build/fieldlink-codec-2.4.1/fieldlink_codec/{__init__,tables}.py
  tasks/glyph2-decode/environment/pip-cache/fieldlink_codec-2.4.1-py3-none-any.whl
  tasks/glyph2-decode/environment/app/data/samples/*.glb
  tasks/glyph2-decode/tests/reference_codec.py
  tasks/glyph2-decode/environment/app/spec/tables_digest.txt
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TASK = ROOT / "tasks" / "glyph2-decode"
BUILD = TASK / "environment" / "opt" / "build" / "fieldlink-codec-2.4.1" / "fieldlink_codec"
WHEEL_DIR = TASK / "environment" / "pip-cache"
SAMPLES = TASK / "environment" / "app" / "data" / "samples"
SEED = 0x2A31F1
CONSONANTS = "bcdfghjklmnpqrstvwz"
VOWELS = "aeiou"


def make_symbols() -> list[str]:
    rng = random.Random(SEED)
    names = []
    for _ in range(4096):
        names.append(
            "".join(
                rng.choice(CONSONANTS) + rng.choice(VOWELS) + rng.choice(CONSONANTS)
                for _ in range(2)
            )
        )
    return names


SYMBOLS = make_symbols()
TABLE_SOURCE = (
    '"""fieldlink-codec 2.4.1 reference tables (firmware symbol dictionary)."""\n\n'
    "#: Symbol names, indexed by the 12-bit symbol id carried in a GLYPH/2 word.\n"
    "SYMBOLS = [\n"
    + "".join(f'    "{name}",\n' for name in SYMBOLS)
    + "]\n"
)
DIGEST = hashlib.sha256(TABLE_SOURCE.encode()).hexdigest()
INIT_SOURCE = '"""fieldlink-codec: GLYPH/2 reference codec."""\n'

REFERENCE_HEADER = '''"""Grader-owned reference implementation of the GLYPH/2 framing.

The dictionary below is the fieldlink-codec 2.4.1 firmware symbol table
(sha256 of the vendored tables.py: {digest}). It ships only in the verifier.
"""

from __future__ import annotations

MAGIC = b"G2"
VERSION = 2
FLAG_AUX = 0x01
FLAG_EXT_SEQ = 0x02
SYMBOL_MASK = 0x0FFF
CHANNEL_MASK = 0xF000

'''

REFERENCE_BODY = '''

def decode(packet: bytes) -> dict:
    """Reference decoder: the behaviour the submitted decode_packet must match."""
    if not isinstance(packet, (bytes, bytearray)):
        raise TypeError("packet must be bytes")
    if len(packet) < 9:
        raise ValueError("truncated header")
    if bytes(packet[:2]) != MAGIC:
        raise ValueError("bad magic")
    if packet[2] != VERSION:
        raise ValueError("unsupported version")
    flags = packet[3]
    if flags & ~(FLAG_AUX | FLAG_EXT_SEQ):
        raise ValueError("reserved flag bits set")
    width = 4 if flags & FLAG_EXT_SEQ else 2
    end = 4 + width + 3
    if len(packet) < end:
        raise ValueError("truncated header")
    seq = int.from_bytes(packet[4 : 4 + width], "big")
    device = int.from_bytes(packet[4 + width : 4 + width + 2], "big")
    count = packet[end - 1]
    if count == 0:
        raise ValueError("empty symbol run")
    body = packet[end:]
    if len(body) < 2 * count:
        raise ValueError("truncated symbols")
    words = [int.from_bytes(body[2 * i : 2 * i + 2], "big") for i in range(count)]
    symbols = [SYMBOLS[word & SYMBOL_MASK] for word in words]
    channels = [(word & CHANNEL_MASK) >> 12 for word in words]
    aux_bytes = body[2 * count :]
    if flags & FLAG_AUX:
        if len(aux_bytes) < count:
            raise ValueError("truncated aux")
        aux = list(aux_bytes[:count])
    else:
        aux = [0] * count
    if len(body) > 2 * count + (count if flags & FLAG_AUX else 0):
        raise ValueError("trailing bytes")
    return {
        "seq": seq,
        "device": f"D{device:04d}",
        "symbols": symbols,
        "channels": channels,
        "aux": aux,
    }


def encode(
    seq: int,
    device: int,
    ids: list[int],
    channels: list[int],
    aux: list[int] | None = None,
) -> bytes:
    """Build a well-formed packet for the given symbol ids."""
    flags = 0
    width = 2
    if seq > 0xFFFF:
        flags |= FLAG_EXT_SEQ
        width = 4
    if aux is not None:
        flags |= FLAG_AUX
    head = MAGIC + bytes([VERSION, flags]) + seq.to_bytes(width, "big")
    head += device.to_bytes(2, "big") + bytes([len(ids)])
    body = b"".join(
        ((ch << 12) | sid).to_bytes(2, "big") for sid, ch in zip(ids, channels, strict=True)
    )
    if aux is not None:
        body += bytes(aux)
    return head + body
'''

SAMPLES_SPEC = [
    ("boot.glb", 7, 1, [0x000, 0x001, 0x002], [0, 0, 1], None),
    ("status.glb", 18, 4, [0x003, 0x7FF, 0x000], [2, 2, 2], [9, 10, 11]),
    ("wrap.glb", 0xFFFE, 9, [0xFFE, 0xFFF, 0x800, 0x001], [0, 1, 2, 3], [1, 2, 3, 4]),
]


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    WHEEL_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (BUILD / "__init__.py").write_text(INIT_SOURCE)
    (BUILD / "tables.py").write_text(TABLE_SOURCE)
    (TASK / "environment" / "app" / "spec").mkdir(parents=True, exist_ok=True)
    (TASK / "environment" / "app" / "spec" / "tables_digest.txt").write_text(DIGEST + "\n")

    wheel = WHEEL_DIR / "fieldlink_codec-2.4.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in (
            ("fieldlink_codec/__init__.py", INIT_SOURCE),
            ("fieldlink_codec/tables.py", TABLE_SOURCE),
            (
                "fieldlink_codec-2.4.1.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: fieldlink-codec\nVersion: 2.4.1\n",
            ),
            (
                "fieldlink_codec-2.4.1.dist-info/WHEEL",
                "Wheel-Version: 1.0\nGenerator: bdist_wheel (0.43.0)\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n",
            ),
            ("fieldlink_codec-2.4.1.dist-info/RECORD", ""),
        ):
            info = zipfile.ZipInfo(name, date_time=(2024, 3, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text)

    table_literal = "SYMBOLS = [\n" + "".join(f'    "{n}",\n' for n in SYMBOLS) + "]\n"
    reference = REFERENCE_HEADER.format(digest=DIGEST) + table_literal + REFERENCE_BODY
    tests = TASK / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "reference_codec.py").write_text(reference)

    spec = importlib.util.spec_from_file_location("reference_codec", tests / "reference_codec.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sample_dir = tests / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    for filename, seq, device, ids, channels, aux in SAMPLES_SPEC:
        packet = module.encode(seq, device, ids, channels, aux)
        (SAMPLES / filename).write_bytes(packet)
        (sample_dir / filename).write_bytes(packet)
    print(f"table sha256 {DIGEST}")
    print(f"symbols {len(SYMBOLS)} distinct {len(set(SYMBOLS))}")


if __name__ == "__main__":
    main()
