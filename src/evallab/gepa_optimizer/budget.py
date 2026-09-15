"""Process- and thread-safe durable aggregate budget reservations.

Manages durable aggregate attempt, request, and cost limits within a
campaign-group output directory using flock and atomic JSON state.

Preserves conservative reservation counts prior to external side effects.
Failed and unknown reservations remain charged without automatic retries
or refunds. Missing estimated costs remain strictly unknown.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "AggregateBudget",
    "BudgetExhausted",
    "DuplicateReservationError",
]

_DIR_LOCKS: dict[str, threading.Lock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _get_dir_lock(resolved_dir: str) -> threading.Lock:
    with _DIR_LOCKS_GUARD:
        lock = _DIR_LOCKS.get(resolved_dir)
        if lock is None:
            lock = threading.Lock()
            _DIR_LOCKS[resolved_dir] = lock
        return lock


class BudgetExhausted(BaseException):
    """Raised when an aggregate attempt, request, or cost limit is exhausted."""


class DuplicateReservationError(BudgetExhausted):
    """Raised when an attempt is made to reserve an already reserved identity."""


class AggregateBudget:
    """Process- and thread-safe aggregate budget for target attempts and proposer requests.

    Limits are immutable once bound to a campaign directory. State is stored in
    an atomic JSON file and synchronized using fcntl.flock and an in-process thread lock.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_target_attempts: int,
        max_proposer_requests: int,
        max_proposer_cost_usd: float,
    ) -> None:
        if max_target_attempts < 0:
            raise ValueError(f"max_target_attempts must be non-negative, got {max_target_attempts}")
        if max_proposer_requests < 0:
            raise ValueError(f"max_proposer_requests must be non-negative, got {max_proposer_requests}")
        if max_proposer_cost_usd < 0.0:
            raise ValueError(f"max_proposer_cost_usd must be non-negative, got {max_proposer_cost_usd}")

        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError(f"Budget directory must not be a symlink: {self.directory}")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError(f"Budget directory must not be a symlink: {self.directory}")

        self.max_target_attempts = int(max_target_attempts)
        self.max_proposer_requests = int(max_proposer_requests)
        self.max_proposer_cost_usd = float(max_proposer_cost_usd)

        if (self.directory / "budget.json").exists() and not (self.directory / "aggregate_budget.json").exists():
            self.state_path = self.directory / "budget.json"
        else:
            self.state_path = self.directory / "aggregate_budget.json"

        if (self.directory / ".budget.lock").exists() and not (self.directory / ".aggregate_budget.lock").exists():
            self.lock_path = self.directory / ".budget.lock"
        else:
            self.lock_path = self.directory / ".aggregate_budget.lock"

        self._initialized = False
        with self._lock():
            self._load_or_initialize_state_unlocked()
        self._initialized = True

    @contextmanager
    def _lock(self):
        if self.directory.is_symlink():
            raise ValueError(f"Budget directory must not be a symlink: {self.directory}")
        if self.lock_path.is_symlink():
            raise ValueError(f"Budget lock file must not be a symlink: {self.lock_path}")

        thread_lock = _get_dir_lock(str(self.directory.resolve()))
        with thread_lock:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.path.islink(self.lock_path):
                    raise ValueError(f"Budget lock file must not be a symlink: {self.lock_path}")
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    if self.directory.is_symlink():
                        raise ValueError(f"Budget directory must not be a symlink: {self.directory}")
                    if self.state_path.is_symlink():
                        raise ValueError(f"Budget state file must not be a symlink: {self.state_path}")
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_or_initialize_state_unlocked(self) -> dict[str, Any]:
        if not self.state_path.exists():
            if self._initialized:
                raise ValueError(
                    f"Retained budget state file is missing at {self.state_path}; "
                    "refusing to reset counters"
                )
            state = {
                "schema_version": 1,
                "limits": {
                    "max_target_attempts": self.max_target_attempts,
                    "max_proposer_requests": self.max_proposer_requests,
                    "max_proposer_cost_usd": self.max_proposer_cost_usd,
                },
                "reservations": {
                    "target": {},
                    "proposer": {},
                },
            }
            self._save_state_unlocked(state)
            return state

        if self.state_path.is_symlink():
            raise ValueError(f"Budget state file must not be a symlink: {self.state_path}")

        raw = self.state_path.read_text(encoding="utf-8")
        if not raw.strip():
            raise ValueError(f"Corrupt budget state: empty state file at {self.state_path}")

        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt budget state: invalid JSON in {self.state_path}") from exc

        if not isinstance(state, dict):
            raise ValueError(f"Corrupt budget state: root must be a dict in {self.state_path}")

        limits = state.get("limits")
        if not isinstance(limits, dict):
            raise ValueError(f"Corrupt budget state: missing 'limits' dict in {self.state_path}")

        stored_target = limits.get("max_target_attempts")
        stored_proposer = limits.get("max_proposer_requests")
        stored_cost = limits.get("max_proposer_cost_usd")

        if not isinstance(stored_target, int) or stored_target < 0:
            raise ValueError(f"Corrupt budget state: invalid max_target_attempts in {self.state_path}")
        if not isinstance(stored_proposer, int) or stored_proposer < 0:
            raise ValueError(f"Corrupt budget state: invalid max_proposer_requests in {self.state_path}")
        if not isinstance(stored_cost, (int, float)) or stored_cost < 0.0:
            raise ValueError(f"Corrupt budget state: invalid max_proposer_cost_usd in {self.state_path}")

        if stored_target != self.max_target_attempts:
            raise ValueError(
                f"Immutable budget limits violated: stored max_target_attempts={stored_target} "
                f"differs from requested {self.max_target_attempts}"
            )
        if stored_proposer != self.max_proposer_requests:
            raise ValueError(
                f"Immutable budget limits violated: stored max_proposer_requests={stored_proposer} "
                f"differs from requested {self.max_proposer_requests}"
            )
        if not math.isclose(float(stored_cost), self.max_proposer_cost_usd, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"Immutable budget limits violated: stored max_proposer_cost_usd={stored_cost} "
                f"differs from requested {self.max_proposer_cost_usd}"
            )

        reservations = state.get("reservations")
        if not isinstance(reservations, dict):
            raise ValueError(f"Corrupt budget state: missing 'reservations' dict in {self.state_path}")
        if "target" not in reservations or not isinstance(reservations["target"], dict):
            raise ValueError(f"Corrupt budget state: missing 'reservations.target' dict in {self.state_path}")
        if "proposer" not in reservations or not isinstance(reservations["proposer"], dict):
            raise ValueError(f"Corrupt budget state: missing 'reservations.proposer' dict in {self.state_path}")

        return state

    def _save_state_unlocked(self, state: dict[str, Any]) -> None:
        if self.directory.is_symlink():
            raise ValueError(f"Budget directory must not be a symlink: {self.directory}")
        if self.state_path.is_symlink():
            raise ValueError(f"Budget state file must not be a symlink: {self.state_path}")

        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        tmp_path = self.directory / f".budget.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, self.state_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def reserve(
        self,
        kind: str,
        identity: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Conservatively reserve an attempt or request slot before side effects.

        Halts if ceiling reached, if duplicate identity attempted, or if any previous
        proposer outcome or estimated cost is unknown. Target unknown reservations
        remain charged toward the target ceiling but do not halt subsequent target
        reservations.
        """
        if kind not in {"target", "proposer"}:
            raise ValueError(f"Unknown reservation kind: {kind!r}. Must be 'target' or 'proposer'.")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Reservation identity must be a non-empty string.")

        with self._lock():
            state = self._load_or_initialize_state_unlocked()
            res_bucket = state["reservations"][kind]

            if identity in res_bucket:
                raise DuplicateReservationError(
                    f"Duplicate {kind} reservation identity: {identity!r}. "
                    "Second physical request rejected; caller must halt."
                )

            if kind == "target":
                if len(res_bucket) >= self.max_target_attempts:
                    raise BudgetExhausted(
                        f"Target attempt ceiling reached: {len(res_bucket)} >= {self.max_target_attempts}"
                    )
            elif kind == "proposer":
                if len(res_bucket) >= self.max_proposer_requests:
                    raise BudgetExhausted(
                        f"Proposer request ceiling reached: {len(res_bucket)} >= {self.max_proposer_requests}"
                    )

                known_cost = sum(
                    float(e["estimated_cost_usd"])
                    for e in res_bucket.values()
                    if e.get("estimated_cost_usd") is not None
                )
                if known_cost >= self.max_proposer_cost_usd:
                    raise BudgetExhausted(
                        f"Proposer estimated cost cap reached: {known_cost:.6f} >= {self.max_proposer_cost_usd:.6f}"
                    )

                for prev_id, prev_entry in res_bucket.items():
                    if prev_entry.get("status") != "completed":
                        raise BudgetExhausted(
                            f"Previous proposer reservation {prev_id!r} has unknown outcome "
                            f"(status: {prev_entry.get('status')!r}); cannot reserve new proposer request"
                        )
                    if prev_entry.get("estimated_cost_usd") is None:
                        raise BudgetExhausted(
                            f"Previous proposer reservation {prev_id!r} has unknown cost; "
                            "cannot reserve new proposer request"
                        )

            res_bucket[identity] = {
                "identity": identity,
                "kind": kind,
                "status": "reserved",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "estimated_cost_usd": None,
                "metadata": dict(metadata) if metadata else {},
            }
            self._save_state_unlocked(state)

    def complete(
        self,
        kind: str,
        identity: str,
        *,
        status: str,
        estimated_cost_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record outcome and optional estimated cost for an existing reservation.

        Missing estimated cost remains None; unknown costs are never converted to zero.
        """
        if kind not in {"target", "proposer"}:
            raise ValueError(f"Unknown reservation kind: {kind!r}. Must be 'target' or 'proposer'.")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Reservation identity must be a non-empty string.")
        if not isinstance(status, str) or not status:
            raise ValueError("Completion status must be a non-empty string.")
        if estimated_cost_usd is not None and float(estimated_cost_usd) < 0.0:
            raise ValueError(f"estimated_cost_usd cannot be negative, got {estimated_cost_usd}")

        with self._lock():
            state = self._load_or_initialize_state_unlocked()
            res_bucket = state["reservations"][kind]

            if identity not in res_bucket:
                raise ValueError(
                    f"Missing reservation: no existing reservation found for {kind}:{identity!r}"
                )

            entry = res_bucket[identity]
            if entry.get("status") != "reserved":
                raise ValueError(
                    f"Reservation {kind}:{identity!r} has already been completed "
                    f"with status {entry.get('status')!r}"
                )

            entry["status"] = status
            entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            if estimated_cost_usd is not None:
                entry["estimated_cost_usd"] = float(estimated_cost_usd)
            else:
                entry["estimated_cost_usd"] = None

            if metadata:
                if "metadata" not in entry or not isinstance(entry["metadata"], dict):
                    entry["metadata"] = {}
                entry["metadata"].update(metadata)

            self._save_state_unlocked(state)

    def is_reserved(self, kind: str, identity: str) -> bool:
        """Check if an identity has already been reserved."""
        if kind not in {"target", "proposer"}:
            raise ValueError(f"Unknown reservation kind: {kind!r}. Must be 'target' or 'proposer'.")
        with self._lock():
            state = self._load_or_initialize_state_unlocked()
            return identity in state["reservations"][kind]

    def summary(self) -> dict[str, Any]:
        """Expose reserved/completed/unknown counts, known estimated cost, and missingness.

        Never counts unknown costs as zero. Makes no claim of provider billing.
        """
        with self._lock():
            state = self._load_or_initialize_state_unlocked()
            target_res = state["reservations"]["target"]
            proposer_res = state["reservations"]["proposer"]

            target_reserved = len(target_res)
            target_completed = sum(1 for e in target_res.values() if e.get("status") == "completed")
            target_unknown = target_reserved - target_completed

            proposer_reserved = len(proposer_res)
            proposer_completed = sum(1 for e in proposer_res.values() if e.get("status") == "completed")
            proposer_unknown = proposer_reserved - proposer_completed

            known_cost = sum(
                float(e["estimated_cost_usd"])
                for e in proposer_res.values()
                if e.get("estimated_cost_usd") is not None
            )
            proposer_missing_costs = sum(
                1 for e in proposer_res.values() if e.get("estimated_cost_usd") is None
            )
            has_unknown_proposer_cost = proposer_missing_costs > 0

            target_remaining = max(0, self.max_target_attempts - target_reserved)
            proposer_remaining = max(0, self.max_proposer_requests - proposer_reserved)

            return {
                "target_reserved": target_reserved,
                "target_completed": target_completed,
                "target_unknown": target_unknown,
                "target_max_attempts": self.max_target_attempts,
                "target_remaining_attempts": target_remaining,
                "proposer_reserved": proposer_reserved,
                "proposer_completed": proposer_completed,
                "proposer_unknown": proposer_unknown,
                "proposer_max_requests": self.max_proposer_requests,
                "proposer_remaining_requests": proposer_remaining,
                "known_estimated_proposer_cost_usd": known_cost,
                "proposer_missing_cost_count": proposer_missing_costs,
                "has_unknown_proposer_cost": has_unknown_proposer_cost,
                "max_proposer_cost_usd": self.max_proposer_cost_usd,
                "actual_billing_claim": False,
                "accounting_basis": "upstream_reported_estimate_not_complete_provider_billing",
                "missingness": {
                    "proposer_unknown_outcomes": proposer_unknown,
                    "proposer_missing_costs": proposer_missing_costs,
                    "target_unknown_outcomes": target_unknown,
                    "has_missing_data": (
                        proposer_unknown > 0 or proposer_missing_costs > 0 or target_unknown > 0
                    ),
                },
                "target": {
                    "reserved": target_reserved,
                    "completed": target_completed,
                    "unknown": target_unknown,
                    "max_attempts": self.max_target_attempts,
                    "remaining": target_remaining,
                },
                "proposer": {
                    "reserved": proposer_reserved,
                    "completed": proposer_completed,
                    "unknown": proposer_unknown,
                    "max_requests": self.max_proposer_requests,
                    "remaining": proposer_remaining,
                    "known_estimated_cost_usd": known_cost,
                    "missing_cost_count": proposer_missing_costs,
                    "has_unknown_cost": has_unknown_proposer_cost,
                    "max_cost_usd": self.max_proposer_cost_usd,
                },
                "totals": {
                    "reserved_attempts": target_reserved + proposer_reserved,
                    "completed_attempts": target_completed + proposer_completed,
                    "unknown_attempts": target_unknown + proposer_unknown,
                },
            }
