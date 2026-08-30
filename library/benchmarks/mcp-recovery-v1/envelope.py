"""AES-256-GCM sealed evidence envelope. Requires trusted cryptography wheels at runtime/verifier."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # ty: ignore[unresolved-import]
except ImportError:
    AESGCM = None  # type: ignore[assignment,misc]

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_mutation_digest(
    *,
    fault_class: str,
    persistence: int,
    seed: int,
    is_clean_twin: bool,
    twin_task_id: str,
    mutation_tool: str | None = None,
) -> str:
    """Compute deterministic SHA-256 mutation digest bound into sealed evidence payload.

    Pure stdlib implementation with zero external imports.
    For clean twins, mutation_tool is None.
    For fault cells, mutation_tool is the designated repair move executed during recovery.
    """
    payload = {
        "fault_class": str(fault_class),
        "is_clean_twin": bool(is_clean_twin),
        "mutation_tool": mutation_tool,
        "persistence": int(persistence),
        "seed": int(seed),
        "twin_task_id": str(twin_task_id),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def derive_aad(
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
    sequence: int,
    **kwargs: Any,
) -> bytes:
    return canonical_json(
        {
            "fault_id": fault_id,
            "persistence": persistence,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "task_id": task_id,
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
    **kwargs: Any,
) -> dict[str, Any]:
    if AESGCM is None:
        raise RuntimeError("cryptography package is required for AES-256-GCM operations")
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    nonce = os.urandom(12)
    aad = derive_aad(
        task_id=task_id,
        fault_id=fault_id,
        persistence=persistence,
        sequence=sequence,
    )
    encrypted = AESGCM(key).encrypt(nonce, canonical_json(payload), aad)
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
    **kwargs: Any,
) -> dict[str, Any]:
    if AESGCM is None:
        raise RuntimeError("cryptography package is required for AES-256-GCM operations")
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
        aad = derive_aad(
            task_id=task_id,
            fault_id=fault_id,
            persistence=persistence,
            sequence=sequence,
        )
        raw = AESGCM(key).decrypt(nonce, ciphertext, aad)
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
