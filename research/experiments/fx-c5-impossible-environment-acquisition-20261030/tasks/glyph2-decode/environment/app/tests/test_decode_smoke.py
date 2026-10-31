"""Smoke coverage for the decoder, using only the symbol ids quoted in the spec."""

from __future__ import annotations

from pathlib import Path

import pytest
from glyph2.decode import decode_packet

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def build(seq: int, device: int, words: list[tuple[int, int]], aux: list[int] | None = None):
    flags = 0x01 if aux is not None else 0x00
    width = 2
    if seq > 0xFFFF:
        flags |= 0x02
        width = 4
    out = b"G2" + bytes([2, flags]) + seq.to_bytes(width, "big")
    out += device.to_bytes(2, "big") + bytes([len(words)])
    out += b"".join(((channel << 12) | sid).to_bytes(2, "big") for sid, channel in words)
    if aux is not None:
        out += bytes(aux)
    return out


def test_boot_capture() -> None:
    decoded = decode_packet(build(7, 1, [(0x000, 0), (0x001, 0)]))
    assert decoded["seq"] == 7
    assert decoded["device"] == "D0001"
    assert decoded["symbols"] == ["sahqap", "tantup"]
    assert decoded["channels"] == [0, 0]


def test_channel_and_aux() -> None:
    decoded = decode_packet(build(18, 4, [(0x003, 2), (0x7FF, 2)], aux=[9, 10]))
    assert decoded["channels"] == [2, 2]
    assert decoded["aux"] == [9, 10]


def test_wrap_sequence() -> None:
    decoded = decode_packet(build(0xFFFE, 9, [(0xFFE, 0)]))
    assert decoded["seq"] == 0xFFFE


def test_bad_magic() -> None:
    with pytest.raises(ValueError):
        decode_packet(b"G3\x02\x00\x00\x07\x00\x01\x01\x00\x00")


def test_sample_files_exist() -> None:
    assert sorted(p.name for p in SAMPLES.glob("*.glb")) == ["boot.glb", "status.glb", "wrap.glb"]
