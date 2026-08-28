"""Immutable data contracts and canonical identity schemas for the Three-Vertical Harbor-Native Benchmark Program.

Grounding: Architecture PR #265 (research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA256_HEX = r"^[a-f0-9]{64}$"
ULID_STR = r"^[0-9A-HJKMNP-TV-Z]{26}$"


def canonical_json(value: Any) -> str:
    """Serialize value to deterministic canonical JSON with sorted keys and tight separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    """Encode value to deterministic canonical JSON bytes."""
    return canonical_json(value).encode("utf-8")


def compute_sha256(value: Any) -> str:
    """Compute sha256 hex string over canonical bytes or raw bytes/string."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def compute_prefixed_sha256(value: Any) -> str:
    """Compute sha256:<hex> digest string."""
    return f"sha256:{compute_sha256(value)}"


def validate_safe_relative_path(path_str: str) -> str:
    """Validate that a relative path is clean, POSIX-style, and does not escape container/task roots.

    Rejects:
    - Absolute paths (/foo)
    - Empty or whitespace paths
    - Traversal elements (..)
    - Leading/trailing slashes or duplicate slashes
    - Windows drive prefixes or backslashes
    """
    if not path_str or not isinstance(path_str, str):
        raise ValueError("Path must be a non-empty string")

    if "\\" in path_str:
        raise ValueError(f"Path must use POSIX separators, got backslash: {path_str!r}")

    pure = PurePosixPath(path_str)
    if pure.is_absolute():
        raise ValueError(f"Path must be relative, got absolute: {path_str!r}")

    parts = pure.parts
    if not parts or parts == (".",):
        raise ValueError(f"Path cannot resolve to current root or empty: {path_str!r}")

    if any(p in ("..", "") for p in parts):
        raise ValueError(f"Path contains directory escape or empty segment: {path_str!r}")

    normalized = pure.as_posix()
    if path_str.startswith("/") or path_str.endswith("/") or "//" in path_str:
        raise ValueError(f"Path contains redundant slashes: {path_str!r}")

    return normalized


def safe_resolve_subpath(root: Path, relative_path: str) -> Path:
    """Safely resolve relative_path under root, ensuring no directory escapes."""
    clean_rel = validate_safe_relative_path(relative_path)
    resolved = (root / clean_rel).resolve()
    resolved_root = root.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Resolved path {resolved} escapes root {resolved_root}") from exc
    return resolved


class ProgramContractModel(BaseModel):
    """Base immutable Pydantic v2 model with extra='forbid'."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SyntheticFamilyType(StrEnum):
    FAMILY_A_STATE_INVERSION = "family_a_state_inversion"
    FAMILY_B_FUNCDAG_V2 = "family_b_funcdag_v2"
    FAMILY_C_FAULT_RECOVERY = "family_c_fault_recovery"


class FaultClass(StrEnum):
    TRANSIENT_HTTP_5XX = "transient_http_5xx"
    TRANSIENT_NETWORK_TIMEOUT = "transient_network_timeout"
    PERSISTENT_SCHEMA_MISMATCH = "persistent_schema_mismatch"
    PERSISTENT_SIGNATURE_ERROR = "persistent_signature_error"
    SILENT_WRONG_PAYLOAD = "silent_wrong_payload"


class FaultInjectionRecord(ProgramContractModel):
    """Deterministic fault injection ledger entry establishing opportunity denominator."""

    fault_id: str = Field(pattern=SHA256_HEX)
    task_id: str
    twin_task_id: str
    target_service: str = "mcp-service"
    target_tool: str
    fault_class: FaultClass
    target_canonical_event_ordinal: int = Field(
        ge=1, description="1-indexed sequence ordinal in StateJournalEvent stream"
    )
    target_atif_step: int | None = Field(
        default=None,
        ge=0,
        description="Optional ATIF step coordinate; verified matching canonical event",
    )
    injection_payload: dict[str, Any]
    recovery_contract: str
    verifier_oracle_digest: str = Field(pattern=SHA256_HEX)

    @field_validator("task_id", "twin_task_id", "target_tool", "recovery_contract")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field must not be empty or blank")
        return v

    def identity_digest(self) -> str:
        """Compute stable content-addressable SHA-256 hash over deterministic contract fields."""
        payload = {
            "fault_class": self.fault_class.value,
            "injection_payload": self.injection_payload,
            "recovery_contract": self.recovery_contract,
            "target_canonical_event_ordinal": self.target_canonical_event_ordinal,
            "target_service": self.target_service,
            "target_tool": self.target_tool,
            "task_id": self.task_id,
            "twin_task_id": self.twin_task_id,
            "verifier_oracle_digest": self.verifier_oracle_digest,
        }
        return compute_sha256(payload)


class CellFactorsA(ProgramContractModel):
    """Vertical A: Context Dilation, State Inversion & Actionable Memory factors."""

    dilation_tokens: int = Field(
        ge=0, description="Context padding volume in tokens (4k, 16k, 64k, 128k)"
    )
    forced_compaction: bool = Field(
        default=False, description="Whether forced compaction budget pressure is active"
    )
    semantic_distractors: bool = Field(
        default=False, description="Matched semantic vs neutral distractor arm"
    )
    seed: int = Field(ge=0, description="Deterministic task generation seed")


class CellFactorsB(ProgramContractModel):
    """Vertical B: MCP-FuncDAG v2 factors."""

    critical_path_depth: int = Field(ge=1, description="Critical path DAG depth")
    parallel_width: int = Field(ge=1, description="Parallel node width per depth layer")
    distractor_count: int = Field(ge=0, description="Count of unreferenced distractor tools")
    seed: int = Field(ge=0, description="Deterministic DAG seed")


class CellFactorsC(ProgramContractModel):
    """Vertical C: Single-Fault Recovery Twin factors."""

    fault_class: FaultClass = Field(description="Target fault taxonomy class")
    fault_injection_count: int = Field(
        ge=1, description="Persistence dose ladder fault count (1, 2, 4, 8)"
    )
    seed: int = Field(ge=0, description="Deterministic seed")


class SyntheticFamilySpec(ProgramContractModel):
    """Specification metadata for synthetic benchmark tasks."""

    family: SyntheticFamilyType
    variant_id: str
    dilation_tokens: int = Field(default=0, ge=0)
    forced_compaction: bool = False
    critical_path_depth: int = Field(default=0, ge=0)
    parallel_width: int = Field(default=0, ge=0)
    distractor_count: int = Field(default=0, ge=0)
    fault_record: FaultInjectionRecord | None = None
    hidden_contract_hash: str = Field(pattern=SHA256_HEX)
    twin_task_ref: str | None = None

    def identity_digest(self) -> str:
        """Compute canonical identity digest without timestamps or mutable clocks."""
        data = self.model_dump(mode="json")
        return compute_sha256(data)


class CampaignCalibrationLedger(ProgramContractModel):
    """Discriminated ledger wrapper for Campaign 0; mechanically bars reportable rates."""

    ledger_id: str = Field(pattern=ULID_STR)
    matrix_ref: str = Field(pattern=ULID_STR, description="Reference to canonical ExperimentMatrix")
    campaign_phase: Literal["campaign_0_pilot"] = "campaign_0_pilot"
    reportable_rates: Literal[False] = False
    family: SyntheticFamilyType
    status: Literal["pending", "active", "gated_passed", "gated_refused"]
    dispatched_trials: int = Field(default=0, ge=0)
    completed_trials: int = Field(default=0, ge=0)


class CampaignMeasurementLedger(ProgramContractModel):
    """Discriminated ledger wrapper for billable measurement and replication campaigns."""

    ledger_id: str = Field(pattern=ULID_STR)
    matrix_ref: str = Field(pattern=ULID_STR, description="Reference to canonical ExperimentMatrix")
    campaign_phase: Literal["billable_cohort", "replication_arm"]
    reportable_rates: Literal[True] = True
    family: SyntheticFamilyType
    status: Literal["pending", "active", "completed", "failed"]
    dispatched_trials: int = Field(default=0, ge=0)
    completed_trials: int = Field(default=0, ge=0)
