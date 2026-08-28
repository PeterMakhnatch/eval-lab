"""Immutable, policy-gated orchestration for bounded billable campaigns.

Campaigns materialize one queue ``ExperimentSpec`` per declared attempt. They do
not call Harbor directly: submission, human approval, credential preflight,
leases, dispatch, ingestion, and retry refusal remain owned by ``PolicyGate`` and
``Executor``. Program family/cell/opportunity/campaign identities are owned by
``evallab.benchmark_program_contracts``; the private execution binding below is
the isolated import-adapter seam for that shared foundation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from evallab.automation import GuardedTick, HeadlessDoctor
from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
)
from evallab.credentials import available_credentials, missing_credential_for
from evallab.evidence_store import archive_evidence, evidence_tree_digest, load_archive
from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR, DispatchCapacity
from evallab.queue import (
    MAX_TRANSIENT_RETRIES,
    Executor,
    load_events,
    new_ulid,
)
from evallab.results import JobRecord, load_job
from evallab.schemas import ContractModel, ExperimentSpec, QueueState

CampaignLedger = CampaignCalibrationLedger | CampaignMeasurementLedger


_SCHEMA_DEFINITION = "campaign-definition/v1"
_SCHEMA_MANIFEST = "campaign-manifest/v1"
_SCHEMA_EVENT = "campaign-event/v1"
_SCHEMA_STATUS = "campaign-status/v1"
_TRANSIENT_PREFIX = "transient_harness:"
_SPEC_DIGEST_EXCLUDES = {
    "submitted_at",
    "policy_rule",
    "campaign_manifest_digest",
    "campaign_spec_digest",
}
_CREDENTIAL_NAMES = (
    "DEEPSEEK_API_KEY",
    "MSWEA_API_KEY",
)


class CampaignError(RuntimeError):
    """Base campaign orchestration failure."""


class CampaignDriftError(CampaignError):
    """A frozen manifest, spec, or result no longer matches its digest."""


class CampaignAmbiguityError(CampaignError):
    """Crash recovery found partial or contradictory durable state."""


class CampaignLeaseError(CampaignError):
    """Another process owns the campaign lease."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("durable campaign write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in value.lower())
    return cleaned.strip("-") or "campaign"


def _repo_relative(value: str) -> str:
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("campaign paths must stay relative to the repository")
    return value


def experiment_spec_digest(spec: ExperimentSpec) -> str:
    """Digest immutable execution inputs while excluding queue-owned fields."""
    payload = spec.model_dump(mode="json", exclude_none=True)
    for field in _SPEC_DIGEST_EXCLUDES:
        payload.pop(field, None)
    return _digest(payload)


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignLimits(_FrozenContract):
    max_cost_usd: float = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_total_tokens: int = Field(ge=0)
    max_wall_clock_seconds: int = Field(ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    max_consecutive_transient_failures: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def total_token_limit_is_coherent(self) -> CampaignLimits:
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("campaign total-token ceiling exceeds input plus output ceilings")
        return self


class TrialLimits(_FrozenContract):
    max_cost_usd: float = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_total_tokens: int = Field(ge=0)
    max_wall_clock_seconds: int = Field(ge=1, le=21_600)

    @model_validator(mode="after")
    def total_token_limit_is_coherent(self) -> TrialLimits:
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("trial total-token ceiling exceeds input plus output ceilings")
        return self


class CampaignDefinitionAttempt(_FrozenContract):
    cell_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    attempt: int = Field(ge=1)
    spec: ExperimentSpec
    limits: TrialLimits

    @model_validator(mode="after")
    def source_spec_is_unbound(self) -> CampaignDefinitionAttempt:
        if self.spec.submitted_at is not None or self.spec.policy_rule is not None:
            raise ValueError("campaign definitions cannot contain queue-owned spec state")
        if any(
            value is not None
            for value in (
                self.spec.campaign_ledger,
                self.spec.campaign_cell_id,
                self.spec.campaign_attempt_id,
                self.spec.campaign_attempt_index,
                self.spec.campaign_manifest_digest,
                self.spec.campaign_spec_digest,
            )
        ):
            raise ValueError("campaign definitions cannot contain pre-bound provenance")
        if self.spec.attempts != 1 or self.spec.concurrency != 1:
            raise ValueError("each campaign definition row must describe one attempt")
        if self.spec.task_id is not None and self.spec.task_id != self.task_id:
            raise ValueError("campaign task identity disagrees with ExperimentSpec.task_id")
        if self.spec.timeout_seconds > self.limits.max_wall_clock_seconds:
            raise ValueError("spec timeout exceeds the trial wall-clock ceiling")
        if self.spec.billable:
            if self.spec.agent != "mini-swe-agent":
                raise ValueError(
                    "billable campaigns currently require the secret-safe mini-swe-agent adapter"
                )
            if self.spec.model != DEEPSEEK_MODEL_SELECTOR:
                raise ValueError(
                    f"billable campaign model must be pinned to {DEEPSEEK_MODEL_SELECTOR}"
                )
            if self.spec.est_cost_usd <= 0:
                raise ValueError("billable campaign specs require a positive cost estimate")
            if self.limits.max_cost_usd <= 0:
                raise ValueError("billable attempts require a positive cost ceiling")
            if self.spec.est_cost_usd > self.limits.max_cost_usd:
                raise ValueError("estimated cost exceeds the trial cost ceiling")
            if self.limits.max_output_tokens < 1:
                raise ValueError("billable attempts require an output-token ceiling")
        return self


class CampaignDefinition(_FrozenContract):
    schema_version: Literal["campaign-definition/v1"] = _SCHEMA_DEFINITION
    ledger: CampaignLedger
    submitted_by: str = Field(min_length=1)
    limits: CampaignLimits
    attempts: tuple[CampaignDefinitionAttempt, ...] = Field(min_length=1)
    evidence_store: str = "derived/evidence-cas"

    @property
    def campaign_id(self) -> str:
        return self.ledger.ledger_id

    @property
    def benchmark(self) -> str:
        return self.ledger.family.value

    @model_validator(mode="after")
    def campaign_is_single_benchmark_and_fully_reserved(self) -> CampaignDefinition:
        _repo_relative(self.evidence_store)
        if (
            self.ledger.status != "pending"
            or self.ledger.dispatched_trials != 0
            or self.ledger.completed_trials != 0
        ):
            raise ValueError("campaign definitions require an unused pending canonical ledger")
        billable = [item.spec.billable for item in self.attempts]
        if isinstance(self.ledger, CampaignCalibrationLedger) and any(billable):
            raise ValueError("calibration ledgers cannot authorize billable attempts")
        if isinstance(self.ledger, CampaignMeasurementLedger) and not any(billable):
            raise ValueError("measurement ledgers require at least one billable attempt")
        identities = [(item.cell_id, item.task_id, item.attempt) for item in self.attempts]
        if len(identities) != len(set(identities)):
            raise ValueError("campaign attempt identities must be unique")
        reserved_cost = sum(item.limits.max_cost_usd for item in self.attempts)
        reserved_input = sum(item.limits.max_input_tokens for item in self.attempts)
        reserved_output = sum(item.limits.max_output_tokens for item in self.attempts)
        reserved_total = sum(item.limits.max_total_tokens for item in self.attempts)
        reserved_wall = sum(item.limits.max_wall_clock_seconds for item in self.attempts)
        reservations = (
            (reserved_cost, self.limits.max_cost_usd, "cost"),
            (reserved_input, self.limits.max_input_tokens, "input-token"),
            (reserved_output, self.limits.max_output_tokens, "output-token"),
            (reserved_total, self.limits.max_total_tokens, "total-token"),
            (reserved_wall, self.limits.max_wall_clock_seconds, "wall-clock"),
        )
        for reserved, ceiling, label in reservations:
            if reserved > ceiling:
                raise ValueError(f"reserved per-trial {label} ceilings exceed the campaign ceiling")
        return self


class _CampaignAttemptBinding(_FrozenContract):
    ledger: CampaignLedger
    cell_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    attempt: int = Field(ge=1)


class CampaignAttempt(_FrozenContract):
    attempt_id: str = Field(pattern=r"^attempt-[0-9a-f]{24}$")
    identity: _CampaignAttemptBinding
    spec_id: str = Field(pattern=r"^campaign-[0-9a-f]{24}$")
    job_name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]+$")
    spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spec: ExperimentSpec
    limits: TrialLimits

    @model_validator(mode="after")
    def spec_is_bound_to_attempt(self) -> CampaignAttempt:
        if experiment_spec_digest(self.spec) != self.spec_digest:
            raise ValueError("campaign attempt spec digest mismatch")
        expected = (
            self.spec.spec_id == self.spec_id,
            self.spec.name == self.job_name,
            self.spec.campaign_ledger == self.identity.ledger,
            self.spec.campaign_cell_id == self.identity.cell_id,
            self.spec.campaign_attempt_id == self.attempt_id,
            self.spec.campaign_attempt_index == self.identity.attempt,
            self.spec.campaign_spec_digest == self.spec_digest,
            self.spec.task_id == self.identity.task_id,
        )
        if not all(expected):
            raise ValueError("campaign attempt identity disagrees with its ExperimentSpec")
        return self


class CampaignManifest(_FrozenContract):
    schema_version: Literal["campaign-manifest/v1"] = _SCHEMA_MANIFEST
    ledger: CampaignLedger
    submitted_by: str = Field(min_length=1)
    limits: CampaignLimits
    attempts: tuple[CampaignAttempt, ...] = Field(min_length=1)
    evidence_store: str
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def campaign_id(self) -> str:
        return self.ledger.ledger_id

    @property
    def benchmark(self) -> str:
        return self.ledger.family.value

    @property
    def billable(self) -> bool:
        return isinstance(self.ledger, CampaignMeasurementLedger)

    @model_validator(mode="after")
    def manifest_is_immutable_and_single_benchmark(self) -> CampaignManifest:
        _repo_relative(self.evidence_store)
        if any(item.identity.ledger != self.ledger for item in self.attempts):
            raise ValueError("cross-ledger campaign pooling is forbidden")
        for attribute in ("attempt_id", "spec_id", "job_name"):
            values = [getattr(item, attribute) for item in self.attempts]
            if len(values) != len(set(values)):
                raise ValueError(f"campaign {attribute} values must be unique")
        if any(
            item.spec.campaign_manifest_digest != self.manifest_digest for item in self.attempts
        ):
            raise ValueError("attempt spec does not bind the campaign manifest digest")
        if campaign_manifest_digest(self) != self.manifest_digest:
            raise ValueError("campaign manifest digest mismatch")
        return self


def campaign_manifest_digest(manifest: CampaignManifest | Mapping[str, Any]) -> str:
    payload = (
        manifest.model_dump(mode="json", exclude_none=True)
        if isinstance(manifest, CampaignManifest)
        else json.loads(json.dumps(manifest))
    )
    payload.pop("manifest_digest", None)
    for attempt in payload.get("attempts", []):
        spec = attempt.get("spec") if isinstance(attempt, dict) else None
        if isinstance(spec, dict):
            spec.pop("campaign_manifest_digest", None)
    return _digest(payload)


def _bind_execution_identity(
    definition: CampaignDefinition,
    item: CampaignDefinitionAttempt,
) -> _CampaignAttemptBinding:
    """Bind canonical campaign-ledger identity to queue execution coordinates."""
    return _CampaignAttemptBinding(
        ledger=definition.ledger,
        cell_id=item.cell_id,
        task_id=item.task_id,
        attempt=item.attempt,
    )


def _deterministic_job_name(identity: _CampaignAttemptBinding, seed_digest: str) -> str:
    suffix = seed_digest.removeprefix("sha256:")[:12]
    base = _safe_component(
        "-".join(
            (
                identity.ledger.ledger_id,
                identity.cell_id,
                identity.task_id,
                f"a{identity.attempt}",
            )
        )
    )
    prefix = base[: 80 - len(suffix) - 1].rstrip("-")
    return f"{prefix}-{suffix}"


def build_campaign_manifest(definition: CampaignDefinition) -> CampaignManifest:
    attempts: list[CampaignAttempt] = []
    placeholder = "sha256:" + "0" * 64
    for item in definition.attempts:
        identity = _bind_execution_identity(definition, item)
        attempt_id = "attempt-" + _digest(identity.model_dump(mode="json"))[7:31]
        source_payload = item.spec.model_dump(mode="json", exclude_none=True)
        source_payload.pop("name", None)
        source_payload.pop("spec_id", None)
        seed_digest = _digest(
            {
                "identity": identity.model_dump(mode="json"),
                "spec": source_payload,
                "limits": item.limits.model_dump(mode="json"),
            }
        )
        spec_id = "campaign-" + seed_digest[7:31]
        job_name = _deterministic_job_name(identity, seed_digest)
        spec = item.spec.model_copy(
            update={
                "spec_id": spec_id,
                "name": job_name,
                "task_id": item.task_id,
                "attempts": 1,
                "concurrency": 1,
                "timeout_seconds": min(
                    item.spec.timeout_seconds,
                    item.limits.max_wall_clock_seconds,
                ),
                "max_output_tokens": (
                    item.limits.max_output_tokens if item.spec.billable else None
                ),
                "cost_limit_usd": item.limits.max_cost_usd if item.spec.billable else None,
                "est_cost_usd": (
                    item.limits.max_cost_usd if item.spec.billable else item.spec.est_cost_usd
                ),
                "campaign_ledger": definition.ledger,
                "campaign_cell_id": item.cell_id,
                "campaign_attempt_id": attempt_id,
                "campaign_attempt_index": item.attempt,
                "campaign_manifest_digest": placeholder,
                "campaign_spec_digest": placeholder,
            }
        )
        spec_digest = experiment_spec_digest(spec)
        spec = spec.model_copy(update={"campaign_spec_digest": spec_digest})
        attempts.append(
            CampaignAttempt(
                attempt_id=attempt_id,
                identity=identity,
                spec_id=spec_id,
                job_name=job_name,
                spec_digest=spec_digest,
                spec=spec,
                limits=item.limits,
            )
        )
    raw: dict[str, Any] = {
        "schema_version": _SCHEMA_MANIFEST,
        "ledger": definition.ledger.model_dump(mode="json"),
        "submitted_by": definition.submitted_by,
        "limits": definition.limits.model_dump(mode="json"),
        "attempts": [item.model_dump(mode="json", exclude_none=True) for item in attempts],
        "evidence_store": definition.evidence_store,
    }
    manifest_digest = campaign_manifest_digest(raw)
    raw["manifest_digest"] = manifest_digest
    for attempt in raw["attempts"]:
        attempt["spec"]["campaign_manifest_digest"] = manifest_digest
    return CampaignManifest.model_validate(raw)


class CampaignEvent(_FrozenContract):
    schema_version: Literal["campaign-event/v1"] = _SCHEMA_EVENT
    sequence: int = Field(ge=1)
    event_id: str
    occurred_at: datetime
    campaign_id: str
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    event: str
    attempt_id: str | None = None
    spec_id: str | None = None
    reason_code: str | None = None
    previous_event_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    event_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def event_digest_is_valid(self) -> CampaignEvent:
        if campaign_event_digest(self) != self.event_digest:
            raise ValueError("campaign event digest mismatch")
        return self


def campaign_event_digest(event: CampaignEvent) -> str:
    payload = event.model_dump(mode="json", exclude_none=True)
    payload.pop("event_digest", None)
    return _digest(payload)


class CampaignAttemptStatus(_FrozenContract):
    attempt_id: str
    spec_id: str
    job_name: str
    cell_id: str
    task_id: str
    attempt: int
    queue_state: str
    spec_digest: str
    completed: bool
    approval_command: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    wall_clock_seconds: float | None = None
    cas_uri: str | None = None
    reason_code: str | None = None


class CampaignStatus(_FrozenContract):
    schema_version: Literal["campaign-status/v1"] = _SCHEMA_STATUS
    campaign_id: str
    benchmark: str
    manifest_digest: str
    state: Literal[
        "planned",
        "waiting-approval",
        "ready",
        "blocked-credential",
        "running",
        "completed",
        "failed",
        "circuit-open",
    ]
    dry_run: bool = False
    attempts: tuple[CampaignAttemptStatus, ...]
    completed_attempts: int
    total_attempts: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    wall_clock_seconds: float
    circuit_reason: str | None = None
    block_reason: str | None = None


class CampaignSecretSanitizer:
    """Redact known values in operator output and reject leaked run artifacts."""

    def __init__(self, secrets: frozenset[str]) -> None:
        self.secrets = frozenset(value for value in secrets if len(value) >= 8)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> CampaignSecretSanitizer:
        source = os.environ if environment is None else environment
        return cls(frozenset(value for name in _CREDENTIAL_NAMES if (value := source.get(name))))

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for secret in self.secrets:
                redacted = redacted.replace(secret, "<redacted>")
            return redacted
        return value

    def assert_tree_safe(self, root: Path) -> None:
        leaks: list[str] = []
        encoded = tuple(secret.encode() for secret in self.secrets)
        if not encoded:
            return
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise CampaignAmbiguityError(
                    f"campaign evidence contains unsupported symlink: {path.relative_to(root)}"
                )
            if not path.is_file():
                continue
            content = path.read_bytes()
            if any(secret in content for secret in encoded):
                leaks.append(path.relative_to(root).as_posix())
        if leaks:
            raise CampaignError(
                "credential material detected in campaign evidence: " + ", ".join(leaks)
            )


class CampaignStore:
    def __init__(self, state_root: Path, campaign_id: str) -> None:
        self.state_root = state_root.resolve()
        self.root = self.state_root / campaign_id
        self.manifest_path = self.root / "manifest.json"
        self.journal_path = self.root / "journal.jsonl"
        self.snapshot_path = self.root / "status.json"
        self.lease_path = self.root / "campaign.lease"
        self.journal_lock_path = self.root / ".journal.lock"

    def assert_manifest(self, manifest: CampaignManifest, *, required: bool = False) -> None:
        if not self.manifest_path.exists():
            if required:
                raise CampaignDriftError("frozen campaign manifest is missing")
            return
        try:
            existing = CampaignManifest.model_validate_json(
                self.manifest_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise CampaignDriftError("frozen campaign manifest is unreadable") from exc
        if existing != manifest:
            raise CampaignDriftError("frozen campaign manifest differs from requested manifest")

    def freeze(self, manifest: CampaignManifest) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = manifest.model_dump_json(indent=2) + "\n"
        if self.manifest_path.exists():
            self.assert_manifest(manifest, required=True)
            return self.manifest_path
        try:
            descriptor = os.open(
                self.manifest_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            self.assert_manifest(manifest, required=True)
            return self.manifest_path
        try:
            _write_all(descriptor, payload.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.root)
        return self.manifest_path

    def events(self, manifest: CampaignManifest) -> list[CampaignEvent]:
        if not self.journal_path.exists():
            return []
        raw = self.journal_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise CampaignAmbiguityError("campaign journal ends with a partial record")
        events: list[CampaignEvent] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            try:
                event = CampaignEvent.model_validate_json(line)
            except Exception as exc:
                raise CampaignAmbiguityError(
                    f"campaign journal record {line_number} is invalid"
                ) from exc
            if event.sequence != line_number:
                raise CampaignAmbiguityError("campaign journal sequence is not contiguous")
            expected_previous = events[-1].event_digest if events else None
            if event.previous_event_digest != expected_previous:
                raise CampaignAmbiguityError("campaign journal digest chain is broken")
            if (
                event.campaign_id != manifest.campaign_id
                or event.manifest_digest != manifest.manifest_digest
            ):
                raise CampaignDriftError("campaign journal belongs to a different manifest")
            events.append(event)
        return events

    def append(
        self,
        manifest: CampaignManifest,
        *,
        event: str,
        sanitizer: CampaignSecretSanitizer,
        occurred_at: datetime,
        attempt: CampaignAttempt | None = None,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> CampaignEvent:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.journal_lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self.events(manifest)
                provisional = CampaignEvent.model_construct(
                    sequence=len(current) + 1,
                    event_id=new_ulid(),
                    occurred_at=occurred_at,
                    campaign_id=manifest.campaign_id,
                    manifest_digest=manifest.manifest_digest,
                    event=event,
                    attempt_id=attempt.attempt_id if attempt else None,
                    spec_id=attempt.spec_id if attempt else None,
                    reason_code=reason_code,
                    details=sanitizer.redact(dict(details or {})),
                    previous_event_digest=(current[-1].event_digest if current else None),
                    event_digest="",
                )
                record_payload = provisional.model_dump(mode="json")
                record_payload["event_digest"] = campaign_event_digest(provisional)
                record = CampaignEvent.model_validate(record_payload)
                payload = (record.model_dump_json(exclude_none=True) + "\n").encode()
                journal_created = not self.journal_path.exists()
                descriptor = os.open(
                    self.journal_path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                try:
                    _write_all(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if journal_created:
                    _fsync_directory(self.root)
                return record
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def lease(self, manifest: CampaignManifest, *, now: datetime) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CampaignLeaseError("campaign is leased by another process") from exc
            payload = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "campaign_id": manifest.campaign_id,
                        "manifest_digest": manifest.manifest_digest,
                        "pid": os.getpid(),
                        "host": platform.node(),
                        "acquired_at": now.isoformat(),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            os.ftruncate(descriptor, 0)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def write_snapshot(self, status: CampaignStatus) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.snapshot_path.with_suffix(".json.tmp")
        temporary.write_text(status.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.snapshot_path)


ArchiveHook = Callable[[CampaignManifest, CampaignAttempt, Path], Mapping[str, Any]]
BackfillHook = Callable[[CampaignManifest, CampaignAttempt, Path], Mapping[str, Any] | None]
DispatchHook = Callable[[Executor, Sequence[str]], int]
Clock = Callable[[], datetime]
CredentialProbe = Callable[[], frozenset[str]]


class CampaignOrchestrator:
    def __init__(
        self,
        *,
        repo_root: Path,
        manifest: CampaignManifest,
        state_root: Path,
        executor: Executor | None = None,
        requested_parallel: int | None = None,
        dispatch: DispatchHook | None = None,
        archive_hook: ArchiveHook | None = None,
        backfill_hook: BackfillHook | None = None,
        credential_probe: CredentialProbe = available_credentials,
        sanitizer: CampaignSecretSanitizer | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.manifest = manifest
        self.store = CampaignStore(state_root, manifest.campaign_id)
        requested = (
            manifest.limits.max_concurrency if requested_parallel is None else requested_parallel
        )
        if requested < 1 or requested > manifest.limits.max_concurrency:
            raise ValueError("requested parallelism exceeds the campaign ceiling")
        parallel = 1 if manifest.billable else requested
        self.executor = executor or Executor.from_repo(
            self.repo_root,
            parallel=parallel,
            max_transient_retries=0,
            create_queue=False,
            capacity=DispatchCapacity(
                max_specs_per_tick=parallel,
                max_active_trials=parallel,
                per_agent_active_trials={"mini-swe-agent": parallel},
            ),
        )
        capacity = self.executor.capacity
        if (
            self.executor.parallel > parallel
            or capacity is None
            or capacity.max_specs_per_tick is None
            or capacity.max_specs_per_tick > parallel
            or capacity.max_active_trials is None
            or capacity.max_active_trials > parallel
        ):
            raise ValueError("campaign executor capacity exceeds safe dispatch parallelism")
        if self.executor.queue.root.resolve() != (self.repo_root / "queue").resolve():
            raise ValueError("campaign executor must use the repository policy queue")
        if getattr(self.executor, "_max_transient_retries", MAX_TRANSIENT_RETRIES) != 0:
            raise ValueError(
                "campaign attempts forbid executor-internal retries; declare another attempt"
            )
        self._dispatch = dispatch or self._guarded_dispatch
        self._archive_hook_is_default = archive_hook is None
        self._archive_hook = archive_hook if archive_hook is not None else self._archive_attempt
        self._backfill_hook = backfill_hook
        self._credential_probe = credential_probe
        self.sanitizer = sanitizer or CampaignSecretSanitizer.from_environment()
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        repo_root: Path,
        state_root: Path,
        **kwargs: Any,
    ) -> CampaignOrchestrator:
        manifest = CampaignManifest.model_validate_json(path.read_text(encoding="utf-8"))
        return cls(
            repo_root=repo_root,
            manifest=manifest,
            state_root=state_root,
            **kwargs,
        )

    def _guarded_dispatch(self, executor: Executor, spec_ids: Sequence[str]) -> int:
        result = GuardedTick(
            doctor=HeadlessDoctor(self.repo_root, executor=executor),
            executor=executor,
        ).run(spec_ids=spec_ids)
        return result.dispatched

    def _queue_record(
        self, attempt: CampaignAttempt
    ) -> tuple[Path, ExperimentSpec, QueueState] | None:
        matches: list[tuple[Path, QueueState]] = []
        for state in (
            "proposed",
            "pending",
            "approved",
            "waiting",
            "rejected",
            "running",
            "done",
            "failed",
        ):
            matches.extend(
                (path, state)
                for path in self.executor.queue.state_dir(state).glob(f"*-{attempt.spec_id}.json")
            )
        if len(matches) > 1:
            raise CampaignAmbiguityError(
                f"campaign spec {attempt.spec_id} appears in multiple queue states"
            )
        if not matches:
            return None
        path, state = matches[0]
        queued = self.executor.queue.load(path)
        if queued.campaign_manifest_digest != self.manifest.manifest_digest:
            raise CampaignDriftError("queued spec binds a different campaign manifest")
        if experiment_spec_digest(queued) != attempt.spec_digest:
            raise CampaignDriftError(f"queued campaign spec drifted: {attempt.spec_id}")
        return path, queued, state

    @staticmethod
    def _event_exists(
        events: list[CampaignEvent], event: str, attempt: CampaignAttempt | None = None
    ) -> bool:
        return any(
            item.event == event and (attempt is None or item.attempt_id == attempt.attempt_id)
            for item in events
        )

    def _job_dir(self, attempt: CampaignAttempt) -> Path:
        return (self.repo_root / attempt.spec.jobs_dir / attempt.job_name).resolve()

    def _validate_job(self, attempt: CampaignAttempt) -> JobRecord:
        job_dir = self._job_dir(attempt)
        try:
            job_dir.relative_to(self.repo_root)
        except ValueError as exc:
            raise CampaignDriftError("campaign job directory escapes the repository") from exc
        try:
            job = load_job(job_dir)
        except Exception as exc:
            raise CampaignAmbiguityError(
                f"completed queue spec has unreadable job evidence: {attempt.job_name}"
            ) from exc
        if len(job.trials) != 1:
            raise CampaignAmbiguityError("campaign attempt must produce exactly one trial")
        result = job.trials[0].result
        if result.get("finished_at") is None:
            raise CampaignAmbiguityError("campaign attempt result is not terminal")
        experiment = job.metadata.get("experiment")
        if not isinstance(experiment, dict):
            raise CampaignAmbiguityError("campaign job lacks run provenance")
        expected = {
            "campaign_ledger": self.manifest.ledger.model_dump(mode="json"),
            "campaign_cell_id": attempt.identity.cell_id,
            "campaign_attempt_id": attempt.attempt_id,
            "campaign_attempt_index": attempt.identity.attempt,
            "campaign_manifest_digest": self.manifest.manifest_digest,
            "campaign_spec_digest": attempt.spec_digest,
        }
        if any(experiment.get(key) != value for key, value in expected.items()):
            raise CampaignDriftError("campaign job provenance does not match the manifest")
        return job

    @staticmethod
    def _usage(job: JobRecord) -> dict[str, int | float | None]:
        trial = job.trials[0]
        raw = trial.result.get("agent_result")
        agent_result = raw if isinstance(raw, dict) else {}

        def integer(name: str) -> int | None:
            value = agent_result.get(name)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

        def number(name: str) -> float | None:
            value = agent_result.get(name)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )

        started_raw = trial.result.get("started_at")
        finished_raw = trial.result.get("finished_at")
        duration: float | None = None
        try:
            started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
            finished = datetime.fromisoformat(str(finished_raw).replace("Z", "+00:00"))
            duration = max(0.0, (finished - started).total_seconds())
        except (TypeError, ValueError):
            pass
        return {
            "input_tokens": integer("n_input_tokens"),
            "output_tokens": integer("n_output_tokens"),
            "cost_usd": number("cost_usd"),
            "wall_clock_seconds": duration,
        }

    def _assert_usage_within_limits(
        self, attempt: CampaignAttempt, usage: Mapping[str, int | float | None]
    ) -> str | None:
        if attempt.spec.billable and any(
            usage.get(field) is None
            for field in ("input_tokens", "output_tokens", "cost_usd", "wall_clock_seconds")
        ):
            return "billable_usage_missing"
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = float(usage.get("cost_usd") or 0.0)
        wall = float(usage.get("wall_clock_seconds") or 0.0)
        if cost > attempt.limits.max_cost_usd:
            return "trial_cost_ceiling_exceeded"
        if input_tokens > attempt.limits.max_input_tokens:
            return "trial_input_token_ceiling_exceeded"
        if output_tokens > attempt.limits.max_output_tokens:
            return "trial_output_token_ceiling_exceeded"
        if input_tokens + output_tokens > attempt.limits.max_total_tokens:
            return "trial_total_token_ceiling_exceeded"
        if wall > attempt.limits.max_wall_clock_seconds:
            return "trial_wall_clock_ceiling_exceeded"
        return None

    def _archive_attempt(
        self, manifest: CampaignManifest, attempt: CampaignAttempt, job_dir: Path
    ) -> Mapping[str, Any]:
        self.sanitizer.assert_tree_safe(job_dir)
        store_root = (self.repo_root / manifest.evidence_store).resolve()
        record_path = store_root / "records/campaign-job" / f"{attempt.attempt_id}.json"
        expected_digest = evidence_tree_digest(job_dir)
        if record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CampaignAmbiguityError("campaign CAS record is unreadable") from exc
            if record.get("content_digest") != expected_digest:
                raise CampaignDriftError("campaign CAS record points at different evidence")
            uri = str(record.get("uri") or "")
            blob_path = load_archive(store_root, uri)
            archive_digest = f"sha256:{hashlib.sha256(blob_path.read_bytes()).hexdigest()}"
            if record.get("archive_digest") != archive_digest:
                raise CampaignDriftError("campaign CAS archive digest mismatch")
            return {
                "uri": uri,
                "content_digest": expected_digest,
                "archive_digest": archive_digest,
                "record_path": record_path.relative_to(self.repo_root).as_posix(),
            }
        archive = archive_evidence(
            job_dir,
            store_root,
            record_id=attempt.attempt_id,
            kind="campaign-job",
        )
        return {
            "uri": archive.uri,
            "content_digest": archive.content_digest,
            "archive_digest": archive.archive_digest,
            "record_path": archive.manifest_path.relative_to(self.repo_root).as_posix(),
        }

    def _verify_archive_details(
        self,
        attempt: CampaignAttempt,
        job_dir: Path,
        details: Mapping[str, Any],
    ) -> None:
        self.sanitizer.assert_tree_safe(job_dir)
        expected_content = evidence_tree_digest(job_dir)
        expected_uri = f"cas://sha256/{expected_content.removeprefix('sha256:')}"
        store_root = (self.repo_root / self.manifest.evidence_store).resolve()
        record_path = store_root / "records/campaign-job" / f"{attempt.attempt_id}.json"
        expected_record_path = record_path.relative_to(self.repo_root).as_posix()
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            blob_path = load_archive(store_root, expected_uri)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CampaignAmbiguityError("campaign CAS evidence is unreadable") from exc
        actual_archive_digest = f"sha256:{hashlib.sha256(blob_path.read_bytes()).hexdigest()}"
        if record.get("archive_digest") != actual_archive_digest:
            raise CampaignDriftError("campaign CAS archive digest mismatch")
        if details.get("archive_digest") != actual_archive_digest:
            raise CampaignDriftError("campaign archive event does not match job evidence")
        if (
            details.get("content_digest") != expected_content
            or details.get("uri") != expected_uri
            or details.get("record_path") != expected_record_path
        ):
            raise CampaignDriftError("campaign archive event does not match job evidence")
        expected_record = {
            "record_id": attempt.attempt_id,
            "kind": "campaign-job",
            "content_digest": expected_content,
            "uri": expected_uri,
            "archive_digest": actual_archive_digest,
        }
        if any(record.get(key) != value for key, value in expected_record.items()):
            raise CampaignDriftError("campaign CAS record does not match job evidence")

    @staticmethod
    def _attempt_event(
        events: list[CampaignEvent],
        event_name: str,
        attempt: CampaignAttempt,
    ) -> CampaignEvent | None:
        matches = [
            event
            for event in events
            if event.event == event_name and event.attempt_id == attempt.attempt_id
        ]
        if len(matches) > 1:
            raise CampaignAmbiguityError(
                f"campaign journal repeats {event_name} for {attempt.attempt_id}"
            )
        return matches[0] if matches else None

    def _latest_failure_reason(self, attempt: CampaignAttempt) -> str:
        reasons = [
            event.reason_code
            for event in load_events(self.executor.queue.events_path)
            if event.spec_id == attempt.spec_id and event.reason_code
        ]
        return reasons[-1] if reasons else "execution_failed"

    def _open_circuit(
        self,
        events: list[CampaignEvent],
        *,
        reason: str,
        attempt: CampaignAttempt | None = None,
    ) -> list[CampaignEvent]:
        if not self._event_exists(events, "campaign_circuit_open"):
            self.store.append(
                self.manifest,
                event="campaign_circuit_open",
                attempt=attempt,
                reason_code=reason,
                sanitizer=self.sanitizer,
                occurred_at=self._clock(),
            )
            events = self.store.events(self.manifest)
        return self._quarantine_remaining(events, reason=reason)

    def _quarantine_remaining(
        self,
        events: list[CampaignEvent],
        *,
        reason: str,
    ) -> list[CampaignEvent]:
        dispatchable = {"proposed", "pending", "approved", "waiting"}
        for attempt in self.manifest.attempts:
            if self._event_exists(events, "attempt_quarantined", attempt):
                continue
            record = self._queue_record(attempt)
            if record is None:
                continue
            _, _, state = record
            if state not in dispatchable:
                continue
            self.executor.queue.reject(
                attempt.spec_id,
                actor="campaign-orchestrator",
                message=f"campaign circuit open: {reason}",
            )
            self.store.append(
                self.manifest,
                event="attempt_quarantined",
                attempt=attempt,
                reason_code=reason,
                details={"queue_state": "rejected"},
                sanitizer=self.sanitizer,
                occurred_at=self._clock(),
            )
            events = self.store.events(self.manifest)
        return events

    def _sync(self) -> list[CampaignEvent]:
        events = self.store.events(self.manifest)
        consecutive_transient = 0
        for attempt in self.manifest.attempts:
            record = self._queue_record(attempt)
            archive_started = self._attempt_event(events, "attempt_archive_started", attempt)
            archive_event = self._attempt_event(events, "attempt_archived", attempt)
            backfill_started = self._attempt_event(events, "attempt_backfill_started", attempt)
            backfill_event = self._attempt_event(events, "attempt_backfilled", attempt)
            completed_event = self._attempt_event(events, "attempt_completed", attempt)
            if record is None:
                if any(
                    event is not None
                    for event in (
                        archive_started,
                        archive_event,
                        backfill_started,
                        backfill_event,
                        completed_event,
                    )
                ) or self._event_exists(events, "attempt_submitted", attempt):
                    raise CampaignAmbiguityError(
                        f"campaign journal references missing queue spec {attempt.spec_id}"
                    )
                if self._job_dir(attempt).exists():
                    raise CampaignAmbiguityError(
                        f"job evidence exists without queue identity: {attempt.job_name}"
                    )
                continue
            _, _, state = record
            if state != "done" and any(
                event is not None for event in (archive_event, backfill_event, completed_event)
            ):
                raise CampaignAmbiguityError(
                    "campaign evidence lifecycle exists outside queue done state"
                )
            if state == "done":
                job_dir = self._job_dir(attempt)
                job = self._validate_job(attempt)
                if backfill_event is not None and archive_event is None:
                    raise CampaignAmbiguityError(
                        "campaign backfill completed before evidence archival"
                    )
                if completed_event is not None and (
                    archive_event is None or backfill_event is None
                ):
                    raise CampaignAmbiguityError(
                        "completed attempt lacks archive or backfill evidence"
                    )

                if archive_event is None:
                    if archive_started is None:
                        self.store.append(
                            self.manifest,
                            event="attempt_archive_started",
                            attempt=attempt,
                            sanitizer=self.sanitizer,
                            occurred_at=self._clock(),
                        )
                        events = self.store.events(self.manifest)
                    elif not self._archive_hook_is_default:
                        raise CampaignAmbiguityError(
                            "custom archive hook may have partially completed; "
                            "operator reconciliation is required"
                        )
                    archive_details = self._archive_hook(self.manifest, attempt, job_dir)
                    self._verify_archive_details(attempt, job_dir, archive_details)
                    self.store.append(
                        self.manifest,
                        event="attempt_archived",
                        attempt=attempt,
                        details=archive_details,
                        sanitizer=self.sanitizer,
                        occurred_at=self._clock(),
                    )
                    events = self.store.events(self.manifest)
                    archive_event = self._attempt_event(events, "attempt_archived", attempt)
                if archive_event is None:
                    raise CampaignAmbiguityError("campaign archive event was not durable")
                self._verify_archive_details(attempt, job_dir, archive_event.details)

                if backfill_event is None:
                    if backfill_started is not None:
                        raise CampaignAmbiguityError(
                            "backfill hook may have partially completed; "
                            "operator reconciliation is required"
                        )
                    if self._backfill_hook is not None:
                        self.store.append(
                            self.manifest,
                            event="attempt_backfill_started",
                            attempt=attempt,
                            sanitizer=self.sanitizer,
                            occurred_at=self._clock(),
                        )
                        events = self.store.events(self.manifest)
                        backfill_details = self._backfill_hook(self.manifest, attempt, job_dir)
                    else:
                        backfill_details = {"mode": "executor-ingest-and-project"}
                    self.store.append(
                        self.manifest,
                        event="attempt_backfilled",
                        attempt=attempt,
                        details=backfill_details or {},
                        sanitizer=self.sanitizer,
                        occurred_at=self._clock(),
                    )
                    events = self.store.events(self.manifest)
                    backfill_event = self._attempt_event(events, "attempt_backfilled", attempt)
                if backfill_event is None:
                    raise CampaignAmbiguityError("campaign backfill event was not durable")

                usage = self._usage(job)
                if completed_event is None:
                    self.store.append(
                        self.manifest,
                        event="attempt_completed",
                        attempt=attempt,
                        details={
                            "spec_digest": attempt.spec_digest,
                            "usage": usage,
                            "cas_uri": archive_event.details.get("uri"),
                        },
                        sanitizer=self.sanitizer,
                        occurred_at=self._clock(),
                    )
                    events = self.store.events(self.manifest)
                    completed_event = self._attempt_event(events, "attempt_completed", attempt)
                if completed_event is None:
                    raise CampaignAmbiguityError("campaign completion event was not durable")
                if (
                    completed_event.details.get("spec_digest") != attempt.spec_digest
                    or completed_event.details.get("usage") != usage
                    or completed_event.details.get("cas_uri") != archive_event.details.get("uri")
                ):
                    raise CampaignDriftError(
                        "completed attempt evidence does not match the current job"
                    )
                reason = self._assert_usage_within_limits(attempt, usage)
                if reason is not None:
                    events = self._open_circuit(events, reason=reason, attempt=attempt)
                consecutive_transient = 0
            elif state == "failed":
                reason = self._latest_failure_reason(attempt)
                if not self._event_exists(events, "attempt_failed", attempt):
                    self.store.append(
                        self.manifest,
                        event="attempt_failed",
                        attempt=attempt,
                        reason_code=reason,
                        sanitizer=self.sanitizer,
                        occurred_at=self._clock(),
                    )
                    events = self.store.events(self.manifest)
                if reason.startswith(_TRANSIENT_PREFIX):
                    consecutive_transient += 1
                    if (
                        consecutive_transient
                        >= self.manifest.limits.max_consecutive_transient_failures
                    ):
                        events = self._open_circuit(
                            events,
                            reason="transient_failure_circuit_breaker",
                            attempt=attempt,
                        )
                else:
                    consecutive_transient = 0
        return events

    def _submit_missing(self, events: list[CampaignEvent]) -> list[CampaignEvent]:
        for attempt in self.manifest.attempts:
            record = self._queue_record(attempt)
            if record is None:
                path, decision = self.executor.submit(attempt.spec)
                reason_code = decision.reason_code
                queue_state = path.parent.name
                recovered = False
            else:
                if self._event_exists(events, "attempt_submitted", attempt):
                    continue
                _, _, queue_state = record
                queue_events = [
                    event
                    for event in load_events(self.executor.queue.events_path)
                    if event.spec_id == attempt.spec_id
                ]
                reason_code = queue_events[-1].reason_code if queue_events else None
                recovered = True
            self.store.append(
                self.manifest,
                event="attempt_submitted",
                attempt=attempt,
                reason_code=reason_code,
                details={"queue_state": queue_state, "recovered": recovered},
                sanitizer=self.sanitizer,
                occurred_at=self._clock(),
            )
            events = self.store.events(self.manifest)
        return events

    def _approved_attempts(self) -> list[CampaignAttempt]:
        approved: list[CampaignAttempt] = []
        for attempt in self.manifest.attempts:
            record = self._queue_record(attempt)
            if record is not None and record[2] == "approved":
                approved.append(attempt)
        return approved

    def _credential_preflight(self, attempts: list[CampaignAttempt]) -> str | None:
        try:
            credentials = self._credential_probe()
        except Exception as exc:
            raise CampaignError("credential preflight failed closed") from exc
        for attempt in attempts:
            missing = missing_credential_for(attempt.spec.agent, credentials)
            if missing is not None:
                return f"missing_credential:{missing}"
        return None

    def _circuit_reason(self, events: list[CampaignEvent]) -> str | None:
        opened = [event for event in events if event.event == "campaign_circuit_open"]
        return opened[-1].reason_code if opened else None

    @staticmethod
    def _credential_block_reason(events: list[CampaignEvent]) -> str | None:
        preflight_events = [
            event
            for event in events
            if event.event in {"credential_preflight_refused", "credential_preflight_passed"}
        ]
        if not preflight_events or preflight_events[-1].event == "credential_preflight_passed":
            return None
        return preflight_events[-1].reason_code

    def _event_usage_totals(self, events: list[CampaignEvent]) -> tuple[float, int, int, float]:
        cost = 0.0
        input_tokens = 0
        output_tokens = 0
        wall = 0.0
        for event in events:
            if event.event != "attempt_completed":
                continue
            usage = event.details.get("usage")
            if not isinstance(usage, dict):
                continue
            cost += float(usage.get("cost_usd") or 0.0)
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            wall += float(usage.get("wall_clock_seconds") or 0.0)
        return cost, input_tokens, output_tokens, wall

    def _next_attempt_budget_reason(
        self,
        events: list[CampaignEvent],
        attempt: CampaignAttempt,
    ) -> str | None:
        cost, input_tokens, output_tokens, wall = self._event_usage_totals(events)
        if cost + attempt.limits.max_cost_usd > self.manifest.limits.max_cost_usd:
            return "campaign_cost_ceiling_exceeded"
        if input_tokens + attempt.limits.max_input_tokens > self.manifest.limits.max_input_tokens:
            return "campaign_input_token_ceiling_exceeded"
        if (
            output_tokens + attempt.limits.max_output_tokens
            > self.manifest.limits.max_output_tokens
        ):
            return "campaign_output_token_ceiling_exceeded"
        if (
            input_tokens + output_tokens + attempt.limits.max_total_tokens
            > self.manifest.limits.max_total_tokens
        ):
            return "campaign_total_token_ceiling_exceeded"
        if (
            wall + attempt.limits.max_wall_clock_seconds
            > self.manifest.limits.max_wall_clock_seconds
        ):
            return "campaign_wall_clock_ceiling_exceeded"
        return None

    def _assert_campaign_usage(self, events: list[CampaignEvent]) -> list[CampaignEvent]:
        cost, input_tokens, output_tokens, wall = self._event_usage_totals(events)
        reason = None
        if cost > self.manifest.limits.max_cost_usd:
            reason = "campaign_cost_ceiling_exceeded"
        elif input_tokens > self.manifest.limits.max_input_tokens:
            reason = "campaign_input_token_ceiling_exceeded"
        elif output_tokens > self.manifest.limits.max_output_tokens:
            reason = "campaign_output_token_ceiling_exceeded"
        elif input_tokens + output_tokens > self.manifest.limits.max_total_tokens:
            reason = "campaign_total_token_ceiling_exceeded"
        elif wall > self.manifest.limits.max_wall_clock_seconds:
            reason = "campaign_wall_clock_ceiling_exceeded"
        if reason is not None:
            return self._open_circuit(events, reason=reason)
        return events

    def status(self, *, dry_run: bool = False) -> CampaignStatus:
        self.store.assert_manifest(self.manifest)
        events = self.store.events(self.manifest)
        queue_events = load_events(self.executor.queue.events_path)
        attempts: list[CampaignAttemptStatus] = []
        any_failed = False
        any_running = False
        any_ready = False
        any_waiting = False
        any_unreconciled = False
        started = self._event_exists(events, "campaign_started")
        for attempt in self.manifest.attempts:
            record = self._queue_record(attempt)
            state = record[2] if record is not None else "planned"
            completed_event = next(
                (
                    event
                    for event in events
                    if event.event == "attempt_completed" and event.attempt_id == attempt.attempt_id
                ),
                None,
            )
            usage = completed_event.details.get("usage", {}) if completed_event is not None else {}
            archive = next(
                (
                    event
                    for event in events
                    if event.event == "attempt_archived" and event.attempt_id == attempt.attempt_id
                ),
                None,
            )
            completed = False
            if completed_event is not None and state == "done":
                if archive is None:
                    raise CampaignAmbiguityError(
                        "completed campaign attempt lacks archive evidence"
                    )
                self._validate_job(attempt)
                self._verify_archive_details(attempt, self._job_dir(attempt), archive.details)
                completed = True
            latest_queue_event = next(
                (event for event in reversed(queue_events) if event.spec_id == attempt.spec_id),
                None,
            )
            reason = latest_queue_event.reason_code if latest_queue_event is not None else None
            approval = (
                f"uv run evallab approve {attempt.spec_id} --actor <you>"
                if state == "waiting" and attempt.spec.billable
                else None
            )
            any_failed = any_failed or state in {"failed", "rejected"}
            any_running = any_running or state == "running"
            any_ready = any_ready or state == "approved"
            any_waiting = any_waiting or state in {"waiting", "pending", "proposed"}
            any_unreconciled = any_unreconciled or (state == "done" and not completed)
            attempts.append(
                CampaignAttemptStatus(
                    attempt_id=attempt.attempt_id,
                    spec_id=attempt.spec_id,
                    job_name=attempt.job_name,
                    cell_id=attempt.identity.cell_id,
                    task_id=attempt.identity.task_id,
                    attempt=attempt.identity.attempt,
                    queue_state=state,
                    spec_digest=attempt.spec_digest,
                    completed=completed,
                    approval_command=approval,
                    cost_usd=(
                        float(usage["cost_usd"]) if usage.get("cost_usd") is not None else None
                    ),
                    input_tokens=(
                        int(usage["input_tokens"])
                        if usage.get("input_tokens") is not None
                        else None
                    ),
                    output_tokens=(
                        int(usage["output_tokens"])
                        if usage.get("output_tokens") is not None
                        else None
                    ),
                    wall_clock_seconds=(
                        float(usage["wall_clock_seconds"])
                        if usage.get("wall_clock_seconds") is not None
                        else None
                    ),
                    cas_uri=(
                        str(archive.details.get("uri"))
                        if archive is not None and archive.details.get("uri")
                        else None
                    ),
                    reason_code=reason,
                )
            )
        completed = sum(item.completed for item in attempts)
        circuit_reason = self._circuit_reason(events)
        block_reason = self._credential_block_reason(events)
        if circuit_reason:
            state_value = "circuit-open"
        elif completed == len(attempts):
            state_value = "completed"
        elif any_running or any_unreconciled:
            state_value = "running"
        elif any_failed:
            state_value = "failed"
        elif any_ready and block_reason:
            state_value = "blocked-credential"
        elif any_ready:
            state_value = "ready"
        elif any_waiting:
            state_value = "waiting-approval"
        elif started:
            state_value = "running"
        else:
            state_value = "planned"
        cost, input_tokens, output_tokens, wall = self._event_usage_totals(events)
        return CampaignStatus(
            campaign_id=self.manifest.campaign_id,
            benchmark=self.manifest.benchmark,
            manifest_digest=self.manifest.manifest_digest,
            state=state_value,
            dry_run=dry_run,
            attempts=tuple(attempts),
            completed_attempts=completed,
            total_attempts=len(attempts),
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            wall_clock_seconds=wall,
            circuit_reason=circuit_reason,
            block_reason=block_reason,
        )

    def _execute(self, *, resume: bool, dry_run: bool) -> CampaignStatus:
        self.store.assert_manifest(self.manifest)
        events = self.store.events(self.manifest)
        started = self._event_exists(events, "campaign_started")
        if dry_run:
            if not resume and started:
                raise CampaignAmbiguityError("campaign already started; use resume")
            if resume and not started:
                raise CampaignAmbiguityError("campaign has not started; use run")
            return self.status(dry_run=True)
        self.executor.queue.ensure_directories()
        self.store.freeze(self.manifest)
        with self.store.lease(self.manifest, now=self._clock()):
            events = self.store.events(self.manifest)
            started = self._event_exists(events, "campaign_started")
            if not resume and started:
                raise CampaignAmbiguityError("campaign already started; use resume")
            if resume and not started:
                raise CampaignAmbiguityError("campaign has not started; use run")
            if not started:
                self.store.append(
                    self.manifest,
                    event="campaign_started",
                    sanitizer=self.sanitizer,
                    occurred_at=self._clock(),
                )
            events = self._sync()
            events = self._assert_campaign_usage(events)
            if self._circuit_reason(events) is None:
                events = self._submit_missing(events)
                events = self._sync()
            while self._circuit_reason(events) is None:
                approved = self._approved_attempts()
                if not approved:
                    break
                budget_reason = self._next_attempt_budget_reason(events, approved[0])
                if budget_reason is not None:
                    events = self._open_circuit(
                        events,
                        reason=budget_reason,
                        attempt=approved[0],
                    )
                    break
                preflight_reason = self._credential_preflight(approved)
                current_block = self._credential_block_reason(events)
                if preflight_reason is not None:
                    if current_block != preflight_reason:
                        self.store.append(
                            self.manifest,
                            event="credential_preflight_refused",
                            reason_code=preflight_reason,
                            sanitizer=self.sanitizer,
                            occurred_at=self._clock(),
                        )
                        events = self.store.events(self.manifest)
                    break
                if current_block is not None:
                    self.store.append(
                        self.manifest,
                        event="credential_preflight_passed",
                        sanitizer=self.sanitizer,
                        occurred_at=self._clock(),
                    )
                    events = self.store.events(self.manifest)
                dispatched = self._dispatch(
                    self.executor,
                    tuple(attempt.spec_id for attempt in approved),
                )
                events = self._sync()
                events = self._assert_campaign_usage(events)
                if dispatched == 0:
                    break
            status = self.status()
            self.store.write_snapshot(status)
            return status

    def run(self, *, dry_run: bool = False) -> CampaignStatus:
        return self._execute(resume=False, dry_run=dry_run)

    def resume(self, *, dry_run: bool = False) -> CampaignStatus:
        return self._execute(resume=True, dry_run=dry_run)


def plan_campaign(
    definition_path: Path,
    *,
    repo_root: Path,
    state_root: Path,
) -> tuple[CampaignManifest, Path]:
    definition = CampaignDefinition.model_validate_json(definition_path.read_text(encoding="utf-8"))
    manifest = build_campaign_manifest(definition)
    store = CampaignStore(state_root, manifest.campaign_id)
    return manifest, store.freeze(manifest)
