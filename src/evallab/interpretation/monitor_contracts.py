"""Evidence-bound contracts for read-only live-watch investigations.

An alert is a hypothesis, not an outcome or a policy decision. These records
cannot change a Harbor reward, counts verdict, task admission, or running job.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MonitorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def content_digest(value: Any) -> str:
    """Hash the canonical JSON representation, not filesystem timestamps."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MonitorAlert(MonitorModel):
    rule: str = Field(min_length=1, max_length=100)
    severity: Literal["high", "medium", "low"]
    scope: Literal["trial", "fleet"] = "trial"
    job: str = Field(min_length=1, max_length=300)
    trial: str = Field(min_length=1, max_length=300)
    task: str = Field(max_length=500)
    step_ref: str | None = None
    quote: str = Field(default="", max_length=2000)
    detail: str = Field(default="", max_length=4000)
    target: str | None = None
    trials: tuple[str, ...] = ()


class SourceArtifact(MonitorModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class EvidenceRecord(MonitorModel):
    record_id: str
    trial_key: str
    document: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    step_ref: str | None = None
    ordinal: int | None = Field(default=None, ge=1)
    role: str
    text: str
    truncated: bool = False
    redacted: bool = False


class TrialSnapshot(MonitorModel):
    trial_key: str
    job: str
    trial: str
    task: str | None
    source_path: str
    state: Literal["running", "finished", "unavailable"]
    reward: float | None = None
    complete: bool = False
    artifacts: tuple[SourceArtifact, ...] = ()
    records: tuple[EvidenceRecord, ...] = ()
    alerts: tuple[MonitorAlert, ...] = ()
    limitations: tuple[str, ...] = ()


class MonitorCorpus(MonitorModel):
    schema_version: Literal["evallab.monitor_corpus/v1"] = "evallab.monitor_corpus/v1"
    trials: tuple[TrialSnapshot, ...]
    fleet_alerts: tuple[MonitorAlert, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return content_digest(self)

    @model_validator(mode="after")
    def unique_identities(self) -> MonitorCorpus:
        keys = [trial.trial_key for trial in self.trials]
        records = [record.record_id for trial in self.trials for record in trial.records]
        if len(keys) != len(set(keys)) or len(records) != len(set(records)):
            raise ValueError("duplicate trial or evidence identity")
        return self


class InvestigationCase(MonitorModel):
    schema_version: Literal["evallab.monitor_case/v1"] = "evallab.monitor_case/v1"
    case_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    primary_trial: str
    related_trials: tuple[str, ...] = ()
    alerts: tuple[MonitorAlert, ...] = ()
    selection: Literal["alert", "unflagged_control"] = "alert"


class MonitorCitation(MonitorModel):
    record_id: str
    quote: str = Field(min_length=1, max_length=1200)


class ProposedAction(MonitorModel):
    kind: Literal["data", "grader", "environment", "harness", "experiment"]
    description: str = Field(min_length=1, max_length=2000)
    validation: str = Field(min_length=1, max_length=2000)
    requires_approval: Literal[True] = True


class MonitorFinding(MonitorModel):
    disposition: Literal["suspicious", "not_supported", "inconclusive"]
    category: Literal[
        "reward_hacking", "agent_failure", "harness_failure", "task_defect",
        "infrastructure", "unknown",
    ]
    summary: str = Field(min_length=1, max_length=4000)
    evidence: tuple[MonitorCitation, ...] = ()
    counterevidence: tuple[MonitorCitation, ...] = ()
    alternatives: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    proposed_actions: tuple[ProposedAction, ...] = ()

    @model_validator(mode="after")
    def ground_claim(self) -> MonitorFinding:
        if self.disposition == "suspicious" and not self.evidence:
            raise ValueError("a suspicious finding requires supporting evidence")
        if self.disposition == "not_supported" and not (self.evidence or self.counterevidence):
            raise ValueError("a negative finding requires evidence or counterevidence")
        if self.disposition == "inconclusive" and not self.missing_evidence:
            raise ValueError("inconclusive findings must explain missing evidence")
        return self


class ReadStepsAction(MonitorModel):
    action: Literal["read_steps"]
    trial_key: str
    start: int = Field(ge=1, description="Inclusive 1-based start.")
    end: int = Field(ge=1, description="Inclusive end.")

    @model_validator(mode="after")
    def valid_range(self) -> ReadStepsAction:
        if self.end < self.start:
            raise ValueError("read_steps end precedes start")
        return self


class SearchAction(MonitorModel):
    action: Literal["search"]
    query: str = Field(min_length=1, max_length=300)
    trial_key: str | None = None


class RelatedAction(MonitorModel):
    action: Literal["related"]


class ConcludeAction(MonitorModel):
    action: Literal["conclude"]
    finding: MonitorFinding


type EvidenceAction = ReadStepsAction | SearchAction | RelatedAction | ConcludeAction


class InvestigationAction(MonitorModel):
    # A root object keeps this compatible with strict structured-output APIs;
    # the union makes operation-specific required/forbidden fields explicit.
    request: EvidenceAction


class InvestigationLimits(MonitorModel):
    max_calls: int = Field(default=8, ge=1, le=32)
    max_output_tokens: int = Field(default=2000, ge=128, le=8000)
    max_context_chars: int = Field(default=64000, ge=4000, le=256000)
    max_tool_chars: int = Field(default=12000, ge=1000, le=32000)
    timeout_seconds: float = Field(default=90, gt=0, le=600)


class InvestigationReport(MonitorModel):
    schema_version: Literal["evallab.monitor_report/v1"] = "evallab.monitor_report/v1"
    case_id: str
    snapshot_id: str
    status: Literal["completed", "budget_exhausted", "failed", "inconclusive"]
    model: str
    finding: MonitorFinding | None = None
    calls: int = Field(default=0, ge=0)
    reserved_usd: float = Field(default=0, ge=0)
    estimated_usage_usd: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    viewed_records: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    error: str | None = None
