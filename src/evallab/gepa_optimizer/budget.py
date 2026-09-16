"""Durable reservations shared by optimizer stages and independent native repeats.

Reservations precede side effects and are never refunded. Request/attempt caps
are hard local limits; reported-cost checks cannot guarantee provider billing.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class BudgetExhausted(BaseException):
    """Halt without allowing upstream evaluators to manufacture reward zero."""


class DuplicateReservationError(BudgetExhausted):
    """An ambiguous or already-spent reservation must not be retried."""


def _cost(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


class AggregateBudget:
    def __init__(
        self,
        directory: Path,
        *,
        max_target_attempts: int,
        max_proposer_requests: int,
        max_proposer_cost_usd: float,
    ) -> None:
        for value in (max_target_attempts, max_proposer_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Budget counts must be nonnegative integers")
        if not _cost(max_proposer_cost_usd):
            raise ValueError("Budget cost must be finite and nonnegative")
        self.directory = Path(directory).absolute()
        self.state_path = self.directory / "aggregate_budget.json"
        self.lock_path = self.directory / ".aggregate_budget.lock"
        self.limits = {
            "max_target_attempts": max_target_attempts,
            "max_proposer_requests": max_proposer_requests,
            "max_proposer_cost_usd": max_proposer_cost_usd,
        }
        self._check_paths()
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._locked(initialize=True):
            pass

    def _check_paths(self) -> None:
        for path in (self.state_path, self.lock_path, self.directory, *self.directory.parents):
            if path.is_symlink():
                raise ValueError(f"Budget paths must not be symlinks: {path}")

    @contextmanager
    def _locked(self, *, initialize: bool = False) -> Iterator[dict[str, Any]]:
        self._check_paths()
        fresh = False
        flags = os.O_RDWR | os.O_NOFOLLOW
        if initialize:
            try:
                fd = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                fresh = True
            except FileExistsError:
                fd = os.open(self.lock_path, flags)
        else:
            fd = os.open(self.lock_path, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._check_paths()
            if not self.state_path.exists():
                if not fresh:
                    raise ValueError(
                        "Retained budget state is missing; restore it, never reset counters"
                    )
                self._save(
                    {
                        "schema_version": 1,
                        "limits": self.limits,
                        "reservations": {"target": {}, "proposer": {}},
                    }
                )
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._validate(state)
            yield state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _validate(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("Invalid retained budget schema")
        if state.get("limits") != self.limits:
            raise ValueError("Immutable budget limits differ from the retained binding")
        buckets = state.get("reservations")
        if not isinstance(buckets, dict) or set(buckets) != {"target", "proposer"}:
            raise ValueError("Invalid retained budget reservations")
        for kind, rows in buckets.items():
            ceiling = self.limits[
                "max_target_attempts" if kind == "target" else "max_proposer_requests"
            ]
            if not isinstance(rows, dict) or len(rows) > ceiling:
                raise ValueError("Invalid retained budget count")
            for identity, row in rows.items():
                if (
                    not isinstance(row, dict)
                    or row.get("identity") != identity
                    or row.get("kind") != kind
                ):
                    raise ValueError("Invalid retained reservation identity")
                if not isinstance(row.get("status"), str) or not row["status"]:
                    raise ValueError("Invalid retained reservation status")
                value = row.get("estimated_cost_usd")
                if value is not None and not _cost(value):
                    raise ValueError("Invalid retained estimated cost")
                if not isinstance(row.get("metadata"), dict):
                    raise ValueError("Invalid retained reservation metadata")

    def _save(self, state: dict[str, Any]) -> None:
        self._check_paths()
        payload = json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n"
        temporary = self.directory / f".budget-{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _identity(kind: str, identity: str) -> None:
        if kind not in {"target", "proposer"} or not isinstance(identity, str) or not identity:
            raise ValueError("Reservation requires target/proposer kind and a nonempty identity")

    def reserve(self, kind: str, identity: str, *, metadata: dict[str, Any] | None = None) -> None:
        self._identity(kind, identity)
        with self._locked() as state:
            rows = state["reservations"][kind]
            if identity in rows:
                raise DuplicateReservationError(f"Duplicate {kind} reservation: {identity}")
            maximum = self.limits[
                "max_target_attempts" if kind == "target" else "max_proposer_requests"
            ]
            if len(rows) >= maximum:
                raise BudgetExhausted(f"{kind} reservation ceiling reached")
            if kind == "proposer":
                if any(row["status"] != "completed" for row in rows.values()):
                    raise BudgetExhausted(
                        "Previous proposer outcome is unknown or failed; no automatic retry"
                    )
                if any(row["estimated_cost_usd"] is None for row in rows.values()):
                    raise BudgetExhausted("Previous proposer cost is unknown")
                if (
                    sum(row["estimated_cost_usd"] for row in rows.values())
                    >= self.limits["max_proposer_cost_usd"]
                ):
                    raise BudgetExhausted("Aggregate reported proposer cost ceiling reached")
            rows[identity] = {
                "identity": identity,
                "kind": kind,
                "status": "reserved",
                "created_at": datetime.now(UTC).isoformat(),
                "estimated_cost_usd": None,
                "metadata": dict(metadata or {}),
            }
            self._save(state)

    def complete(
        self,
        kind: str,
        identity: str,
        *,
        status: str,
        estimated_cost_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._identity(kind, identity)
        if not isinstance(status, str) or not status or status == "reserved":
            raise ValueError("A settled outcome needs an explicit non-reserved status")
        if estimated_cost_usd is not None and not _cost(estimated_cost_usd):
            raise ValueError("Estimated cost must be finite and nonnegative, or unknown")
        outcome = {
            "status": status,
            "estimated_cost_usd": estimated_cost_usd,
            "metadata": dict(metadata or {}),
        }
        with self._locked() as state:
            row = state["reservations"][kind].get(identity)
            if row is None:
                raise ValueError("Cannot complete an unreserved request")
            if row["status"] != "reserved":
                if row.get("outcome") == outcome:
                    return
                raise ValueError("Retained reservation outcome differs; refusing to rewrite it")
            row.update(status=status, estimated_cost_usd=estimated_cost_usd, outcome=outcome)
            row["completed_at"] = datetime.now(UTC).isoformat()
            self._save(state)

    def is_reserved(self, kind: str, identity: str) -> bool:
        self._identity(kind, identity)
        with self._locked() as state:
            return identity in state["reservations"][kind]

    def summary(self) -> dict[str, Any]:
        with self._locked() as state:
            summary: dict[str, Any] = {"limits": self.limits, "actual_billing_cost_usd": None}
            for kind, rows in state["reservations"].items():
                summary[kind] = {
                    "reserved": len(rows),
                    "completed": sum(row["status"] == "completed" for row in rows.values()),
                    "unsettled": sum(row["status"] == "reserved" for row in rows.values()),
                    "errors": sum(
                        row["status"] not in {"reserved", "completed"} for row in rows.values()
                    ),
                }
            proposer = list(state["reservations"]["proposer"].values())
            costs = [
                row["estimated_cost_usd"]
                for row in proposer
                if row["estimated_cost_usd"] is not None
            ]
            missing = len(proposer) - len(costs)
            summary["proposer"].update(
                known_estimated_cost_usd=sum(costs),
                missing_cost_count=missing,
                complete_estimated_cost_usd=sum(costs) if not missing else None,
                cost_limit_scope="Reported-estimate stop, not a hard provider billing guarantee",
            )
            return summary
