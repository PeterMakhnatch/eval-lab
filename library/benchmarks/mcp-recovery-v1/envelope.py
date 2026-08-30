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


def compute_mutation_digest(
    *,
    fault_class: str,
    persistence: int,
    seed: int,
    is_clean_twin: bool,
    twin_task_id: str,
    mutation_tool: str | None = None,
) -> str:
    """Compute deterministic SHA-256 mutation digest bound into sealed evidence.

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
    fault_class: str | None = None,
    seed: int | None = None,
    is_clean_twin: bool | None = None,
    twin_task_id: str | None = None,
    mutation_digest: str | None = None,
) -> bytes:
    aad_dict: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "fault_id": fault_id,
        "persistence": persistence,
        "sequence": sequence,
    }
    if fault_class is not None:
        aad_dict["fault_class"] = fault_class
    if seed is not None:
        aad_dict["seed"] = seed
    if is_clean_twin is not None:
        aad_dict["is_clean_twin"] = is_clean_twin
    if twin_task_id is not None:
        aad_dict["twin_task_id"] = twin_task_id
    if mutation_digest is not None:
        aad_dict["mutation_digest"] = mutation_digest
    return canonical_json(aad_dict)


def encrypt_envelope(
    key: bytes,
    payload: dict[str, Any],
    *,
    task_id: str,
    fault_id: str,
    persistence: int,
    sequence: int,
    fault_class: str | None = None,
    seed: int | None = None,
    is_clean_twin: bool | None = None,
    twin_task_id: str | None = None,
    mutation_digest: str | None = None,
) -> dict[str, Any]:
    if len(key) != 32:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    nonce = os.urandom(12)
    aad = derive_aad(
        task_id=task_id,
        fault_id=fault_id,
        persistence=persistence,
        sequence=sequence,
        fault_class=fault_class,
        seed=seed,
        is_clean_twin=is_clean_twin,
        twin_task_id=twin_task_id,
        mutation_digest=mutation_digest,
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
    fault_class: str | None = None,
    seed: int | None = None,
    is_clean_twin: bool | None = None,
    twin_task_id: str | None = None,
    mutation_digest: str | None = None,
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
        # Try decrypt with full AAD; if caller provided extended fields and it fails,
        # try backward-compatible basic AAD in case envelope was sealed without extended fields.
        try:
            aad = derive_aad(
                task_id=task_id,
                fault_id=fault_id,
                persistence=persistence,
                sequence=sequence,
                fault_class=fault_class,
                seed=seed,
                is_clean_twin=is_clean_twin,
                twin_task_id=twin_task_id,
                mutation_digest=mutation_digest,
            )
            raw = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except Exception:
            if any(x is not None for x in (fault_class, seed, is_clean_twin, twin_task_id, mutation_digest)):
                basic_aad = derive_aad(
                    task_id=task_id,
                    fault_id=fault_id,
                    persistence=persistence,
                    sequence=sequence,
                )
                raw = AESGCM(key).decrypt(nonce, ciphertext, basic_aad)
            else:
                raise
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
