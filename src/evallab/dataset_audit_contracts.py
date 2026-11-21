from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from evallab.schemas import ContractModel

AuditStage = Literal["leak", "oracle", "nop", "static", "history", "exploit"]
AuditVerdict = Literal["keep", "fix", "discard", "unknown"]
StageStatus = Literal[
    "recorded", "executed", "planned", "not_applicable", "unavailable", "failed"
]
ALL_STAGES: tuple[AuditStage, ...] = (
    "leak", "oracle", "nop", "static", "history", "exploit"
)
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


class AuditSource(ContractModel):

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    available: bool = True
    binding: str | None = None


class AuditTask(ContractModel):

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    task_id: str
    task_name: str
    path: Path | None = None
    package_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    harbor_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    aliases: tuple[str, ...] = ()
    source_uri: str
    revision: str | None = None
    parent_source: dict[str, JsonValue] = Field(default_factory=dict)


class AuditDataset(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    source_uri: str
    revision: str | None = None
    license: str | None = None
    tasks: tuple[AuditTask, ...]
    sources: tuple[AuditSource, ...] = ()
    plugin: str = "harbor"


class AuditObservation(ContractModel):

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: AuditStage
    status: StageStatus
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    sources: tuple[AuditSource, ...] = ()
    reason: str | None = None


class AuditRecord(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: AuditTask
    verdict: AuditVerdict = "unknown"
    verdict_reason: str | None = None
    verdict_sources: tuple[AuditSource, ...] = ()
    tags: tuple[str, ...] = ()
    stages: dict[AuditStage, AuditObservation] = Field(default_factory=dict)
    facets: dict[str, JsonValue] = Field(default_factory=dict)
