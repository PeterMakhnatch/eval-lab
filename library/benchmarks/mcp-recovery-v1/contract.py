"""Benchmark contract for mcp-recovery-v1 using immutable program contracts."""
from __future__ import annotations

from typing import Any

from evallab.benchmark_program_contracts import (
    CellFactorsC,
    FaultClass,
    SyntheticFamilyType,
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


def campaign0_cells(seed: int = 42) -> list[CellFactorsC]:
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
        "construct": manifest.get("benchmark", {}).get("construct", ""),
        "synthetic_family": SyntheticFamilyType.FAMILY_C_FAULT_RECOVERY.value,
        "cell_factors": {
            "fault_classes": [item.value for item in CAMPAIGN0_FAULTS],
            "persistence_levels": list(CAMPAIGN0_PERSISTENCE),
            "seeds": manifest.get("benchmark", {}).get("calibration_seeds", [42]),
        },
        "verifier_truth_digest": digest,
        "evidence_contract": {
            "events_path": "/app/output/benchmark-events.jsonl",
            "final_state_path": "/app/output/final-state.json",
        },
        "cell_count": len(CAMPAIGN0_FAULTS) * len(CAMPAIGN0_PERSISTENCE),
        "identity_digest": compute_sha256(
            {
                "family": FAMILY,
                "fault_classes": [item.value for item in CAMPAIGN0_FAULTS],
                "persistence": list(CAMPAIGN0_PERSISTENCE),
            }
        ),
    }
