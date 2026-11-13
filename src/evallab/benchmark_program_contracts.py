"""Immutable data contracts and canonical identity schemas for the Three-Vertical Harbor-Native Benchmark Program.

Grounding: Architecture PR #265 (research/archive/2026-W36/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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




class ProgramContractModel(BaseModel):
    """Base immutable Pydantic v2 model with extra='forbid'."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SyntheticFamilyType(StrEnum):
    FAMILY_A_STATE_INVERSION = "family_a_state_inversion"
    FAMILY_B_FUNCDAG_V2 = "family_b_funcdag_v2"
    FAMILY_C_FAULT_RECOVERY = "family_c_fault_recovery"










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
