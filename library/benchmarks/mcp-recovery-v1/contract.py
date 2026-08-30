"""Benchmark contract for mcp-recovery-v1 using immutable program contracts."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from evallab.benchmark_program_contracts import (
    CellFactorsC,
    FaultClass,
    SyntheticFamilyType,
    canonical_bytes,
    compute_sha256,
)

from source import load_manifest, source_digest

FAMILY = "mcp-recovery-v1"
CAMPAIGN0_PERSISTENCE = (1, 2)
CAMPAIGN0_FAULTS: tuple[FaultClass, ...] = tuple(FaultClass)

# Ecological Recovery-Bench class names mapped onto program FaultClass.
ECOLOGICAL_FAULT_ALIASES = {
    "permission-denied": FaultClass.PERSISTENT_SIGNATURE_ERROR,
    "permission_denied": FaultClass.PERSISTENT_SIGNATURE_ERROR,
    "not-found": FaultClass.PERSISTENT_SCHEMA_MISMATCH,
    "not_found": FaultClass.PERSISTENT_SCHEMA_MISMATCH,
    "timeout": FaultClass.TRANSIENT_NETWORK_TIMEOUT,
    "malformed-output": FaultClass.TRANSIENT_HTTP_5XX,
    "malformed_output": FaultClass.TRANSIENT_HTTP_5XX,
    "silent-wrong-result": FaultClass.SILENT_WRONG_PAYLOAD,
    "silent_wrong_result": FaultClass.SILENT_WRONG_PAYLOAD,
}

# Deterministic designated causal repair tool per fault class.
# Recovery requires executing the exact designated tool strictly after fault injection
# and prior to post-fault confirmed target success.
DESIGNATED_REPAIR_MOVES: dict[FaultClass, str] = {
    FaultClass.PERSISTENT_SIGNATURE_ERROR: "refresh_auth",
    FaultClass.PERSISTENT_SCHEMA_MISMATCH: "fallback_query",
    FaultClass.TRANSIENT_NETWORK_TIMEOUT: "refresh_auth",
    FaultClass.TRANSIENT_HTTP_5XX: "fallback_query",
    FaultClass.SILENT_WRONG_PAYLOAD: "fallback_query",
}

# Alternative (non-designated) repair tool used to construct wrong-repair mutants.
ALTERNATIVE_REPAIR_MOVES: dict[FaultClass, str] = {
    FaultClass.PERSISTENT_SIGNATURE_ERROR: "fallback_query",
    FaultClass.PERSISTENT_SCHEMA_MISMATCH: "refresh_auth",
    FaultClass.TRANSIENT_NETWORK_TIMEOUT: "fallback_query",
    FaultClass.TRANSIENT_HTTP_5XX: "refresh_auth",
    FaultClass.SILENT_WRONG_PAYLOAD: "refresh_auth",
}


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

    Pure stdlib / program-contract helper safe for host-side materialization without crypto extras.
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
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def slugify_fault(fault: FaultClass | str) -> str:
    resolved = resolve_fault_class(fault)
    return resolved.value.replace("_", "-")


def resolve_fault_class(fault: FaultClass | str) -> FaultClass:
    if isinstance(fault, FaultClass):
        return fault
    raw = str(fault).strip()
    if raw in ECOLOGICAL_FAULT_ALIASES:
        return ECOLOGICAL_FAULT_ALIASES[raw]
    hyphen = raw.replace("_", "-")
    for item in FaultClass:
        if item.value == raw or item.value.replace("_", "-") == hyphen:
            return item
    raise ValueError(f"unknown recovery fault class: {fault!r}")


def get_designated_repair(fault: FaultClass | str) -> str:
    """Return the designated repair tool name for a given fault class."""
    resolved = resolve_fault_class(fault)
    return DESIGNATED_REPAIR_MOVES[resolved]


def get_alternative_repair(fault: FaultClass | str) -> str:
    """Return the non-designated (wrong) repair tool name for mutant generation."""
    resolved = resolve_fault_class(fault)
    return ALTERNATIVE_REPAIR_MOVES[resolved]


def campaign0_cells(seed: int = 42) -> list[CellFactorsC]:
    """Return all 10 Campaign 0 fault cell factor instances."""
    return [
        CellFactorsC(fault_class=fault, fault_injection_count=persistence, seed=seed)
        for fault in CAMPAIGN0_FAULTS
        for persistence in CAMPAIGN0_PERSISTENCE
    ]


def get_benchmark_contract() -> dict[str, Any]:
    manifest = load_manifest()
    digest = source_digest()
    return {
        "family": FAMILY,
        "version": manifest.get("version", "v1.0.0"),
        "construct": manifest.get("benchmark", {}).get("construct", "certified_mcp_fault_recovery"),
        "synthetic_family": SyntheticFamilyType.FAMILY_C_FAULT_RECOVERY.value,
        "cell_factors": {
            "fault_classes": [item.value for item in CAMPAIGN0_FAULTS],
            "persistence_levels": list(CAMPAIGN0_PERSISTENCE),
            "seeds": manifest.get("benchmark", {}).get("calibration_seeds", [42]),
            "matched_arms": ["fault", "clean_twin"],
            "designated_repair_moves": {item.value: DESIGNATED_REPAIR_MOVES[item] for item in CAMPAIGN0_FAULTS},
        },
        "verifier_truth_digest": digest,
        "evidence_contract": {
            "sealed_envelope_path": "/app/output/sealed-evidence.json",
            "encryption": "AES-256-GCM",
            "aad_binding": ["schema_version", "task_id", "fault_id", "persistence", "sequence"],
        },
        "cell_count": len(CAMPAIGN0_FAULTS) * len(CAMPAIGN0_PERSISTENCE),
        "total_task_count": len(CAMPAIGN0_FAULTS) * len(CAMPAIGN0_PERSISTENCE) * 2,  # 10 fault + 10 clean twin
        "identity_digest": compute_sha256(
            {
                "family": FAMILY,
                "fault_classes": [item.value for item in CAMPAIGN0_FAULTS],
                "persistence": list(CAMPAIGN0_PERSISTENCE),
                "matched_arms": ["fault", "clean_twin"],
                "designated_repair_moves": {item.value: DESIGNATED_REPAIR_MOVES[item] for item in CAMPAIGN0_FAULTS},
            }
        ),
    }
