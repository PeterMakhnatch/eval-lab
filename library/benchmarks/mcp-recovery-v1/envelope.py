#!/usr/bin/env python3
"""AES-256-GCM NIST SP 800-38D implementation verified against OpenSSL."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _CryptoAESGCM  # ty: ignore[unresolved-import]
except ImportError:
    _CryptoAESGCM = None


# AES S-Box
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

_RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtimes(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1) & 0xFF


def _aes256_key_expansion(key: bytes) -> list[int]:
    w = list(key)
    for i in range(8, 60):
        t = w[(i - 1) * 4 : i * 4]
        if i % 8 == 0:
            t = [_SBOX[t[1]], _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
            t[0] ^= _RCON[i // 8]
        elif i % 8 == 4:
            t = [_SBOX[b] for b in t]
        prev = w[(i - 8) * 4 : (i - 7) * 4]
        w.extend(prev[j] ^ t[j] for j in range(4))
    return w


def _aes_encrypt_block(block: bytes, round_keys: list[int]) -> bytes:
    # State is 4x4 matrix in column-major order: state[r + 4*c]
    state = list(block)
    for j in range(16):
        state[j] ^= round_keys[j]

    for rnd in range(1, 14):
        # SubBytes
        state = [_SBOX[b] for b in state]
        # ShiftRows on column-major matrix
        # row 0: state[0], state[4], state[8], state[12] (shift 0)
        # row 1: state[1], state[5], state[9], state[13] (shift 1 -> state[5], state[9], state[13], state[1])
        # row 2: state[2], state[6], state[10], state[14] (shift 2 -> state[10], state[14], state[2], state[6])
        # row 3: state[3], state[7], state[11], state[15] (shift 3 -> state[15], state[3], state[7], state[11])
        state = [
            state[0], state[5], state[10], state[15],
            state[4], state[9], state[14], state[3],
            state[8], state[13], state[2], state[7],
            state[12], state[1], state[6], state[11],
        ]
        # MixColumns
        new_state = [0] * 16
        for c in range(4):
            i = c * 4
            s0, s1, s2, s3 = state[i], state[i + 1], state[i + 2], state[i + 3]
            new_state[i] = _xtimes(s0) ^ _xtimes(s1) ^ s1 ^ s2 ^ s3
            new_state[i + 1] = s0 ^ _xtimes(s1) ^ _xtimes(s2) ^ s2 ^ s3
            new_state[i + 2] = s0 ^ s1 ^ _xtimes(s2) ^ _xtimes(s3) ^ s3
            new_state[i + 3] = _xtimes(s0) ^ s0 ^ s1 ^ s2 ^ _xtimes(s3)
        state = new_state
        rk_offset = rnd * 16
        for j in range(16):
            state[j] ^= round_keys[rk_offset + j]

    # Final round
    state = [_SBOX[b] for b in state]
    state = [
        state[0], state[5], state[10], state[15],
        state[4], state[9], state[14], state[3],
        state[8], state[13], state[2], state[7],
        state[12], state[1], state[6], state[11],
    ]
    for j in range(16):
        state[j] ^= round_keys[224 + j]

    return bytes(state)


# GHASH in GF(2^128)
def _gf128_mul(x: int, y: int) -> int:
    r = 0xE1000000000000000000000000000000
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ (r << 0)
        else:
            v >>= 1
    return z


def _ghash(h_int: int, data: bytes) -> bytes:
    y = 0
    for i in range(0, len(data), 16):
        block = data[i : i + 16]
        if len(block) < 16:
            block = block + b"\x00" * (16 - len(block))
        x = int.from_bytes(block, "big")
        y = _gf128_mul(y ^ x, h_int)
    return y.to_bytes(16, "big")


def _pure_aes_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    round_keys = _aes256_key_expansion(key)
    h_bytes = _aes_encrypt_block(b"\x00" * 16, round_keys)
    h_int = int.from_bytes(h_bytes, "big")

    j0 = nonce + b"\x00\x00\x00\x01"
    ctr_val = int.from_bytes(j0, "big")

    ciphertext_blocks = []
    for i in range(0, len(plaintext), 16):
        ctr_val = (ctr_val + 1) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        ctr_block = ctr_val.to_bytes(16, "big")
        ks = _aes_encrypt_block(ctr_block, round_keys)
        pt_chunk = plaintext[i : i + 16]
        ct_chunk = bytes(a ^ b for a, b in zip(pt_chunk, ks))
        ciphertext_blocks.append(ct_chunk)
    ciphertext = b"".join(ciphertext_blocks)

    pad_aad = aad + b"\x00" * ((16 - (len(aad) % 16)) % 16)
    pad_ct = ciphertext + b"\x00" * ((16 - (len(ciphertext) % 16)) % 16)
    len_block = struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8)
    s = _ghash(h_int, pad_aad + pad_ct + len_block)

    ek0 = _aes_encrypt_block(j0, round_keys)
    tag = bytes(a ^ b for a, b in zip(s, ek0))
    return ciphertext + tag


def _pure_aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext_and_tag: bytes, aad: bytes) -> bytes:
    if len(ciphertext_and_tag) < 16:
        raise ValueError("Ciphertext too short")
    ciphertext = ciphertext_and_tag[:-16]
    expected_tag = ciphertext_and_tag[-16:]

    round_keys = _aes256_key_expansion(key)
    h_bytes = _aes_encrypt_block(b"\x00" * 16, round_keys)
    h_int = int.from_bytes(h_bytes, "big")
    j0 = nonce + b"\x00\x00\x00\x01"

    pad_aad = aad + b"\x00" * ((16 - (len(aad) % 16)) % 16)
    pad_ct = ciphertext + b"\x00" * ((16 - (len(ciphertext) % 16)) % 16)
    len_block = struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8)
    s = _ghash(h_int, pad_aad + pad_ct + len_block)
    ek0 = _aes_encrypt_block(j0, round_keys)
    actual_tag = bytes(a ^ b for a, b in zip(s, ek0))

    if actual_tag != expected_tag:
        raise ValueError("Authentication tag mismatch")

    ctr_val = int.from_bytes(j0, "big")
    plaintext_blocks = []
    for i in range(0, len(ciphertext), 16):
        ctr_val = (ctr_val + 1) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        ctr_block = ctr_val.to_bytes(16, "big")
        ks = _aes_encrypt_block(ctr_block, round_keys)
        ct_chunk = ciphertext[i : i + 16]
        pt_chunk = bytes(a ^ b for a, b in zip(ct_chunk, ks))
        plaintext_blocks.append(pt_chunk)
    return b"".join(plaintext_blocks)


def canonical_json(value: Any) -> bytes:
    """Serialize value to deterministic UTF-8 canonical JSON bytes with tight separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def derive_aad(*, schema_version: int = SCHEMA_VERSION, task_id: str, fault_id: str, persistence: int, sequence: int) -> bytes:
    return canonical_json({
        "schema_version": schema_version,
        "task_id": task_id,
        "fault_id": fault_id,
        "persistence": persistence,
        "sequence": sequence,
    })


def encrypt_envelope(
    key: bytes,
    payload: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
    sequence: int,
) -> dict[str, Any]:
    """Encrypt payload into a sealed evidence envelope binding task/fault/sequence AAD."""
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    nonce = os.urandom(12)
    aad = derive_aad(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        fault_id=fault_id,
        persistence=persistence,
        sequence=sequence,
    )
    raw_payload = canonical_json(payload)
    if _CryptoAESGCM is not None:
        ciphertext_and_tag = _CryptoAESGCM(key).encrypt(nonce, raw_payload, aad)
    else:
        ciphertext_and_tag = _pure_aes_gcm_encrypt(key, nonce, raw_payload, aad)

    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext_and_tag).decode("ascii"),
    }


def decrypt_envelope(
    key: bytes,
    envelope: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
) -> dict[str, Any]:
    """Decrypt and authenticate envelope against declared task, fault, and persistence metadata."""
    if not isinstance(envelope, dict):
        raise ValueError("Envelope must be a JSON object")
    required = {"schema_version", "sequence", "nonce", "ciphertext"}
    if set(envelope) != required:
        raise ValueError(f"Envelope keys {set(envelope)} != {required}")
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported envelope schema version: {envelope['schema_version']}")

    sequence = int(envelope["sequence"])
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext_and_tag = base64.b64decode(envelope["ciphertext"], validate=True)
    aad = derive_aad(
        schema_version=envelope["schema_version"],
        task_id=task_id,
        fault_id=fault_id,
        persistence=persistence,
        sequence=sequence,
    )

    if _CryptoAESGCM is not None:
        try:
            plaintext = _CryptoAESGCM(key).decrypt(nonce, ciphertext_and_tag, aad)
        except Exception as exc:
            raise ValueError("Envelope decryption/authentication failed") from exc
    else:
        plaintext = _pure_aes_gcm_decrypt(key, nonce, ciphertext_and_tag, aad)

    payload = json.loads(plaintext.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Envelope payload is not a JSON object")
    if payload.get("sequence") != sequence:
        raise ValueError(f"Envelope payload sequence {payload.get('sequence')} != envelope header {sequence}")
    return payload


def write_atomic_envelope(path: Path, envelope: dict[str, Any]) -> str:
    """Atomically write envelope to disk and return its content SHA-256 digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    data = canonical_json(envelope) + b"\n"
    temp_path.write_bytes(data)
    temp_path.replace(path)
    return hashlib.sha256(data).hexdigest()
