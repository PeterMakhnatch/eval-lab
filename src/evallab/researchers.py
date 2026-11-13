from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from evallab.queue import new_ulid
from evallab.schemas import (
    ContractModel,
    StandingApprovalsPolicy,
    effective_daily_cost_ceiling,
)

ResearchRole = Literal["analyst", "synthesizer", "proposer"]


class InvocationUsage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)


class CallLedgerRecord(ContractModel):
    schema_version: Literal[1] = 1
    invocation_id: str
    pass_id: str
    role: ResearchRole
    day: date
    occurred_at: datetime
    event: Literal["started", "completed", "failed", "timed_out"]
    attributed_cost_usd: float = Field(default=0, ge=0)
    usage: InvocationUsage | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RoleLimits:
    max_calls_per_day: int
    max_tokens: int
    timeout_seconds: int
    attributed_cost_usd: float


class ResearcherDeferred(RuntimeError):
    pass


class CallLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(
        self,
        *,
        pass_id: str,
        role: ResearchRole,
        day: date,
        limits: RoleLimits,
        policy: StandingApprovalsPolicy,
        catalog_spend_usd: float,
        now: datetime | None = None,
    ) -> str:
        if limits.attributed_cost_usd > policy.per_job_cost_ceiling_usd:
            raise ResearcherDeferred("researcher_per_call_cost_ceiling")
        with self._locked_file() as descriptor:
            records = self._read_descriptor(descriptor)
            calls = sum(
                record.event == "started" and record.day == day and record.role == role
                for record in records
            )
            if calls >= limits.max_calls_per_day:
                raise ResearcherDeferred(f"daily_{role}_call_cap")
            reservations = sum(
                record.attributed_cost_usd
                for record in records
                if record.event == "started" and record.day == day
            )
            projected = catalog_spend_usd + reservations + limits.attributed_cost_usd
            ceiling = effective_daily_cost_ceiling(policy, now or datetime.now(UTC))
            if projected > ceiling:
                raise ResearcherDeferred("daily_cost_ceiling")
            invocation_id = new_ulid()
            self._append_descriptor(
                descriptor,
                CallLedgerRecord(
                    invocation_id=invocation_id,
                    pass_id=pass_id,
                    role=role,
                    day=day,
                    occurred_at=datetime.now(UTC),
                    event="started",
                    attributed_cost_usd=limits.attributed_cost_usd,
                ),
            )
        return invocation_id

    def finish(
        self,
        *,
        invocation_id: str,
        pass_id: str,
        role: ResearchRole,
        day: date,
        event: Literal["completed", "failed", "timed_out"],
        usage: InvocationUsage | None = None,
        reason: str | None = None,
    ) -> None:
        with self._locked_file() as descriptor:
            self._append_descriptor(
                descriptor,
                CallLedgerRecord(
                    invocation_id=invocation_id,
                    pass_id=pass_id,
                    role=role,
                    day=day,
                    occurred_at=datetime.now(UTC),
                    event=event,
                    usage=usage,
                    reason=reason,
                ),
            )

    def records(self) -> list[CallLedgerRecord]:
        if not self.path.is_file():
            return []
        with self._locked_file() as descriptor:
            return self._read_descriptor(descriptor)

    def pass_totals(self, pass_id: str) -> tuple[int, float]:
        records = [
            record
            for record in self.records()
            if record.pass_id == pass_id and record.event == "started"
        ]
        return len(records), sum(record.attributed_cost_usd for record in records)

    def daily_attributed_cost(self, day: date) -> float:
        return sum(
            record.attributed_cost_usd
            for record in self.records()
            if record.day == day and record.event == "started"
        )

    @contextmanager
    def _locked_file(self):
        with self.path.open("a+", encoding="utf-8") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
            try:
                yield descriptor
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_descriptor(descriptor) -> list[CallLedgerRecord]:
        descriptor.seek(0)
        records: list[CallLedgerRecord] = []
        for line_number, line in enumerate(descriptor, start=1):
            if not line.strip():
                continue
            try:
                records.append(CallLedgerRecord.model_validate_json(line))
            except ValidationError as exc:
                raise ValueError(f"invalid researcher ledger line {line_number}: {exc}") from exc
        descriptor.seek(0, os.SEEK_END)
        return records

    @staticmethod
    def _append_descriptor(descriptor, record: CallLedgerRecord) -> None:
        descriptor.seek(0, os.SEEK_END)
        descriptor.write(record.model_dump_json(exclude_none=True) + "\n")
        descriptor.flush()
        os.fsync(descriptor.fileno())
