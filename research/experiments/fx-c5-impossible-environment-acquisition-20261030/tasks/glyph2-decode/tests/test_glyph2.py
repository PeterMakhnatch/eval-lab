"""Hidden acceptance suite for the GLYPH/2 decoder.

The expected values come from the grader's own copy of the reference codec and
its 4096-entry firmware dictionary; the submission never sees either.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reference_codec as R  # noqa: E402

SUBMITTED = Path(os.environ.get("GLYPH2_APP", "/app"))
SAMPLES = HERE / "samples"


def _load():
    sys.path.insert(0, str(SUBMITTED))
    try:
        from glyph2.decode import decode_packet  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - reported as a failed import
        return None, exc
    return decode_packet, None


DECODE, IMPORT_ERROR = _load()


@pytest.fixture(scope="module")
def decode():
    if DECODE is None:
        pytest.fail(f"cannot import glyph2.decode from {SUBMITTED}: {IMPORT_ERROR!r}")
    return DECODE


def captures() -> list[bytes]:
    """Deterministic field captures: every framing variant, arbitrary symbol ids."""
    rng = random.Random(20261030)
    packets = []
    for index in range(48):
        count = rng.randint(1, 12)
        ids = [rng.randrange(0, 0x1000) for _ in range(count)]
        channels = [rng.randrange(0, 16) for _ in range(count)]
        aux = [rng.randrange(0, 256) for _ in range(count)] if index % 3 else None
        seq = rng.choice([rng.randrange(0, 0x10000), rng.randrange(0, 0x1000000)])
        device = rng.randrange(0, 0x10000)
        packets.append(R.encode(seq, device, ids, channels, aux))
    return packets


def test_sample_captures(decode) -> None:
    for path in sorted(SAMPLES.glob("*.glb")):
        assert decode(path.read_bytes()) == R.decode(path.read_bytes()), path.name


def test_field_capture_matrix(decode) -> None:
    for index, packet in enumerate(captures()):
        assert decode(packet) == R.decode(packet), f"capture {index}"


def test_documented_ids(decode) -> None:
    ids = [0x000, 0x001, 0x002, 0x003, 0x7FF, 0x800, 0xFFE, 0xFFF]
    packet = R.encode(3, 42, ids, [0] * len(ids), None)
    assert decode(packet)["symbols"] == [R.SYMBOLS[i] for i in ids]


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"G2",
        b"G2\x02",
        b"G2\x02\x00\x00\x07\x00",
        b"G3\x02\x00\x00\x07\x00\x01\x01\x00\x00",
        b"G2\x03\x00\x00\x07\x00\x01\x01\x00\x00",
        b"G2\x02\x04\x00\x07\x00\x01\x01\x00\x00",
        b"G2\x02\x00\x00\x07\x00\x01\x00",
        b"G2\x02\x00\x00\x07\x00\x01\x02\x00\x00",
        b"G2\x02\x01\x00\x07\x00\x01\x02\x00\x00\x09",
        b"G2\x02\x00\x00\x07\x00\x01\x01\x00\x00\xff",
    ],
)
def test_rejects_malformed(decode, packet: bytes) -> None:
    with pytest.raises(ValueError):
        decode(packet)


def test_rejects_non_bytes(decode) -> None:
    with pytest.raises(TypeError):
        decode("G2\x02\x00")

    with pytest.raises(TypeError):
        decode(None)
