"""AES-256-GCM sealed evidence envelope. Requires trusted cryptography wheels."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # ty: ignore[unresolved-import]

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def derive_aad(*, task_id: str, fault_id: str, persistence: int, sequence: int) -> bytes:
    return canonical_json(
        {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "fault_id": fault_id,
            "persistence": persistence,
            "sequence": sequence,
        }
    )


def encrypt_envelope(
    key: bytes,
    payload: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
    sequence: int,
) -> dict[str, Any]:
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, canonical_json(payload), derive_aad(task_id=task_id, fault_id=fault_id, persistence=persistence, sequence=sequence))
    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
    }


def decrypt_envelope(
    key: bytes,
    envelope: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
) -> dict[str, Any]:
    expected = {"schema_version", "sequence", "nonce", "ciphertext"}
    if set(envelope) != expected or envelope.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid envelope schema")
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    sequence = envelope["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("invalid envelope sequence")
    try:
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        raw = AESGCM(key).decrypt(nonce, ciphertext, derive_aad(task_id=task_id, fault_id=fault_id, persistence=persistence, sequence=sequence))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("envelope decryption/authentication failed") from exc
    if not isinstance(payload, dict) or payload.get("sequence") != sequence:
        raise ValueError("envelope payload sequence mismatch")
    return payload


def write_atomic_envelope(path: Path, envelope: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    data = canonical_json(envelope) + b"\n"
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()
