"""Native Modal cell execution, keyed cache reuse and reserve/settle spend accounting."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate, SpendCapReached

#: Modal Sandbox pricing rates (USD per CPU-second, per GiB-second)
MODAL_CPU_USD_PER_S: float = 0.00003942
MODAL_GIB_USD_PER_S: float = 0.00000667
#: Shared estimate margin over measured sandbox seconds
ESTIMATE_MARGIN: float = 1.25
#: Default seconds reserved per cell when task package provides no specific calibration
PROJECTED_CELL_SECONDS: int = 240
#: Default flat reservation amount in USD when no calibration or package estimate is available
DEFAULT_RESERVATION_USD: float = 0.01
#: Default retry count for infrastructure errors

CellRunner = Callable[[Path, Path, str], dict[str, Any]]
"""Callable (cell_dir, package, script) -> result dict."""


def sha256_text(text: str) -> str:
    """Return SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_cell_key(
    package_digest: str,
    variant: str,
    submission_digest: str,
    backend_image: str = "",
    grader_hook: str = "",
    timeout_seconds: int = 600,
) -> str:
    """Deterministic hash of a cell's evaluation parameters.

    cell_key = sha256(package digest, variant, submission or mutant digest, backend image, grader hook, timeout)
    """
    payload = "\0".join([
        str(package_digest).strip(),
        str(variant).strip(),
        str(submission_digest).strip(),
        str(backend_image).strip(),
        str(grader_hook).strip(),
        str(timeout_seconds),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_modal_cost(package: Path, wall_seconds: float) -> float:
    """Estimate Modal cost from task package resources and elapsed wall seconds."""
    env: dict[str, Any] = {}
    task_toml = package / "task.toml"
    if task_toml.is_file():
        try:
            env = tomllib.loads(task_toml.read_text(encoding="utf-8")).get("environment", {})
        except Exception:
            env = {}
    cpus = int(env.get("cpus", 2))
    memory_mb = int(env.get("memory_mb", 8192))
    rate = cpus * MODAL_CPU_USD_PER_S + (memory_mb / 1024.0) * MODAL_GIB_USD_PER_S
    cost = rate * wall_seconds * ESTIMATE_MARGIN
    if wall_seconds > 0:
        return max(0.00001, cost)
    return cost


# ---------------------------------------------------------------------------
# Error Classes
# ---------------------------------------------------------------------------


class CellError(Exception):
    """Base error for cell execution."""


class CellCapReachedError(CellError, SpendCapReached):
    """Execution would exceed approved spend cap. Clean stop, resumable."""


class CellCancelledError(CellError):
    """Cell execution was cancelled (e.g. keyboard interrupt or job abort)."""


# ---------------------------------------------------------------------------
# Reserve / Settle Spend Gate
# ---------------------------------------------------------------------------

_CURRENT_RESERVATION: ContextVar[Reservation | None] = ContextVar(
    "modal_cells_current_reservation", default=None
)
_THREAD_RESERVATIONS: dict[int, Reservation] = {}

@dataclass
class Reservation:
    """An active spend reservation for a cell."""

    reservation_id: str
    cell_key: str
    reserved_usd: float
    at: str
    gate: ReserveSettleSpendGate
    pid: int = field(default_factory=os.getpid)
    settled_usd: float | None = None
    released_usd: float | None = None
    status: str = "reserved"  # reserved, settled, completed, released, cancelled, failed, stale_settled, cap_reached
    metadata: dict[str, Any] = field(default_factory=dict)
    _token: Any = field(default=None, repr=False, compare=False)

    def settle(self, actual_usd: float, status: str = "settled") -> float:
        """Settle this reservation to measured actual_usd and release difference."""
        return self.gate.settle(self, actual_usd, status=status)

    def release(self) -> float:
        """Release this reservation with 0 actual cost."""
        return self.gate.settle(self, 0.0, status="released")

    def __enter__(self) -> Reservation:
        self._token = _CURRENT_RESERVATION.set(self)
        _THREAD_RESERVATIONS[threading.get_ident()] = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _CURRENT_RESERVATION.reset(self._token)
        _THREAD_RESERVATIONS.pop(threading.get_ident(), None)
        if self.status == "reserved":
            if exc_type is not None:
                st = "cancelled" if issubclass(exc_type, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
                self.settle(0.0, status=st)
            else:
                self.release()


class ReserveSettleSpendGate(CloudSpendGate):
    """A spend gate that reserves estimated cost up front and settles to real cost upon completion.

    The approved cap applies to `settled + outstanding reservations`. When a cell
    finishes, unused reserved funds are released back to available budget.
    Concurrency-safe with cross-process file locking.

    Note on ledger file deletion:
        Deleting the spend ledger file resets tracked spend to 0.0. The lock file and
        any running processes remain active, but previously accumulated spend history is
        not recovered unless restored from backup.
    """

    def __init__(
        self,
        approval: str,
        cap_usd: float,
        usd_per_execution: float = DEFAULT_RESERVATION_USD,
        *,
        default_reservation_usd: float | None = None,
        calibration_path: Path | str | None = None,
        calibration_data: dict[str, Any] | None = None,
        spend_ledger: Path | str | None = None,
        spend_log: Path | str | None = None,
        modal_app_name: str | None = None,
        billing_tag: str | None = None,
        reconcile_stale: bool = True,
        executions: int = 0,
    ) -> None:
        if spend_ledger is not None:
            billing_tag = billing_tag or "modal-cells"
            modal_app_name = modal_app_name or billing_tag
        super().__init__(
            approval=approval,
            cap_usd=cap_usd,
            usd_per_execution=usd_per_execution,
            executions=executions,
            spend_ledger=None,
            modal_app_name=modal_app_name,
            billing_tag=billing_tag,
        )
        self.spend_ledger = (
            Path(spend_ledger).expanduser().resolve() if spend_ledger is not None else None
        )
        res_usd = default_reservation_usd if default_reservation_usd is not None else usd_per_execution
        if (
            isinstance(res_usd, bool)
            or not isinstance(res_usd, int | float)
            or not math.isfinite(res_usd)
            or res_usd <= 0
        ):
            raise ValueError(
                f"default_reservation_usd must be a positive finite number, got {res_usd!r}"
            )
        self.default_reservation_usd = float(res_usd)
        self.calibration_path = (
            Path(calibration_path).expanduser().resolve() if calibration_path is not None else None
        )
        self.calibration_data = calibration_data
        self.spend_log = (
            Path(spend_log).expanduser().resolve() if spend_log is not None else None
        )
        self.reconcile_stale = reconcile_stale
        self._active_reservations: dict[str, Reservation] = {}
        self._settled_usd: float = 0.0
        self._reserved_usd: float = 0.0
        self._cond: threading.Condition = threading.Condition(self._lock)
        if self.spend_ledger is not None:
            with self._lock_ledger():
                self._load_and_reconcile_ledger()
    def _lock_ledger(self) -> Any:
        assert self.spend_ledger is not None
        ledger_path = Path(self.spend_ledger)
        lock_path = ledger_path.with_name(ledger_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return _FileLock(lock_path)

    def _load_and_reconcile_ledger(self) -> dict[str, Any]:
        """Load and reconcile spend ledger from disk.

        If the ledger file is absent, initializes a fresh spend record starting at 0.0.
        """
        assert self.spend_ledger is not None
        ledger_path = Path(self.spend_ledger)
        if not ledger_path.exists():
            data = {
                "schema": "evallab.modal_cells.spend/v1",
                "approval": self.approval,
                "max_usd": self.cap_usd,
                "reserved_usd": 0.0,
                "settled_usd": 0.0,
                "cumulative_spend_usd": 0.0,
                "effective_spend_usd": 0.0,
                "executions": 0,
                "reservations": [],
            }
            self._write_ledger_atomic(data)
            return data

        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ledger is not a JSON object")
        schema = raw.get("schema")
        if schema == "evallab.upstream_grader.spend/v1":
            # Legacy ledger migration: preserve spend conservatively
            raw["schema"] = "evallab.modal_cells.spend/v1"
            legacy_res = raw.get("reservations", [])
            for r in legacy_res:
                if not isinstance(r, dict):
                    continue
                if "status" not in r:
                    r["status"] = "settled"
                    r["settled_usd"] = float(
                        r.get("finalized_estimate_usd") or r.get("reserved_usd", 0.0)
                    )
                    r.setdefault("reservation_id", uuid.uuid4().hex)
            raw["reservations"] = legacy_res
        elif schema != "evallab.modal_cells.spend/v1":
            raise ValueError(f"unsupported spend ledger schema: {schema!r}")

        if raw.get("approval") != self.approval:
            raise ValueError(
                f"existing spend ledger differs at approval: {raw.get('approval')} != {self.approval}"
            )
        if self.cap_usd < float(raw.get("max_usd", 0.0)) - 1e-6:
            raise ValueError(
                f"existing spend ledger cap cannot be decreased: {raw.get('max_usd')} > {self.cap_usd}"
            )
        raw["max_usd"] = self.cap_usd
        reservations: list[dict[str, Any]] = raw.get("reservations", [])
        if self.reconcile_stale:
            # Reclaim reservations from dead processes; settle at reserved amount so spend is not lost
            for r in reservations:
                if r.get("status") == "reserved":
                    pid = r.get("pid")
                    if pid and pid != os.getpid():
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            r["status"] = "stale_settled"
                            r["settled_usd"] = float(r.get("reserved_usd", 0.0))
                            r["released_usd"] = 0.0

        outstanding = round(
            sum(
                float(r["reserved_usd"])
                for r in reservations
                if r.get("status") == "reserved" and "reserved_usd" in r
            ),
            6,
        )
        settled = round(
            sum(
                float(r.get("settled_usd", 0.0))
                for r in reservations
                if r.get("status") is not None
                and r.get("status") not in {"reserved", "stale_released"}
            ),
            6,
        )
        raw["reserved_usd"] = outstanding
        raw["settled_usd"] = settled
        raw["cumulative_spend_usd"] = settled
        raw["effective_spend_usd"] = round(outstanding + settled, 6)
        raw["reservations"] = reservations
        raw["executions"] = int(raw.get("executions", len(reservations)))

        self._reserved_usd = outstanding
        self._settled_usd = settled
        self.executions = raw["executions"]
        self._write_ledger_atomic(raw)
        return raw

    def _write_ledger_atomic(self, data: dict[str, Any]) -> None:
        assert self.spend_ledger is not None
        ledger_path = Path(self.spend_ledger)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = ledger_path.with_name(
            f"{ledger_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(ledger_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def get_p95_calibration(self, backend: str = "modal") -> float | None:
        """Load p95 calibrated cost from calibration data or file if available."""
        data = self.calibration_data
        if data is None and self.calibration_path is not None and self.calibration_path.exists():
            try:
                data = json.loads(self.calibration_path.read_text(encoding="utf-8"))
            except Exception:
                data = None
        if not isinstance(data, dict):
            return None

        backend_info = data.get(backend) or data.get("backends", {}).get(backend)
        if isinstance(backend_info, dict):
            for key in ("p95_cost_usd", "p95", "default_p95_usd"):
                val = backend_info.get(key)
                if isinstance(val, int | float) and math.isfinite(val) and val > 0:
                    return float(val)
        elif isinstance(backend_info, list) and backend_info:
            nums: list[float] = []
            for item in backend_info:
                if isinstance(item, int | float):
                    nums.append(float(item))
                elif isinstance(item, dict):
                    c = item.get("cost_usd") or item.get("est_usd")
                    if isinstance(c, int | float):
                        nums.append(float(c))
            if nums:
                nums.sort()
                idx = max(0, int(math.ceil(0.95 * len(nums))) - 1)
                return nums[idx]
        val = data.get("default_p95_usd") or data.get("p95_cost_usd")
        if isinstance(val, int | float) and math.isfinite(val) and val > 0:
            return float(val)
        return None

    def estimate_reservation(
        self,
        package: Path | str | None = None,
        backend: str = "modal",
        timeout_seconds: float | None = None,
        expected_seconds: float | None = None,
    ) -> float:
        """Estimate reservation amount in USD for a cell based on expected cell time.

        Uses calibration p95 when available, else PROJECTED_CELL_SECONDS (240s)
        bounded by kill timeout when provided, rather than sizing on the kill timeout.
        """
        candidates: list[float] = [self.default_reservation_usd]
        p95 = self.get_p95_calibration(backend=backend)
        if p95 is not None:
            candidates.append(p95)
        elif package is not None:
            pkg_path = Path(package)
            exp = expected_seconds if expected_seconds is not None else PROJECTED_CELL_SECONDS
            if timeout_seconds is not None:
                exp = min(exp, timeout_seconds)
            candidates.append(self.estimate_cost(pkg_path, int(exp)))
        return max(candidates)
    def estimate_cost(self, package: Path | str, wall_seconds: float) -> float:
        """Estimate actual cost from package resources and elapsed wall seconds."""
        return estimate_modal_cost(Path(package), wall_seconds)

    @property
    def settled_usd(self) -> float:
        """Cumulative real spend of all settled cells."""
        with self._lock:
            return self._settled_usd

    @property
    def reserved_usd(self) -> float:
        """Outstanding reservation amount currently in flight."""
        with self._lock:
            return self._reserved_usd

    @property
    def effective_spend_usd(self) -> float:
        """Total settled spend plus outstanding reservations."""
        with self._lock:
            return round(self._settled_usd + self._reserved_usd, 6)

    @property
    def remaining_usd(self) -> float:
        """Remaining approved cap."""
        with self._lock:
            return round(max(0.0, self.cap_usd - (self._settled_usd + self._reserved_usd)), 6)

    def reserve(
        self,
        amount: float | None = None,
        *,
        cell_key: str = "",
        package: Path | str | None = None,
        backend: str = "modal",
        timeout_seconds: float | None = None,
        expected_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        wait_timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> Reservation:
        """Reserve funds for one cell execution, or raise SpendCapReached.

        If other cells are currently in flight (reserved_usd > 0), waits and retries
        until budget is freed or timeout is reached; permanent cap stop occurs only
        when settled + amount > cap with nothing in flight.
        """
        if amount is None:
            amount = self.estimate_reservation(
                package=package,
                backend=backend,
                timeout_seconds=timeout_seconds,
                expected_seconds=expected_seconds,
            )
        amount = round(float(amount), 6)
        if amount <= 0:
            raise ValueError(f"reservation amount must be positive, got {amount}")

        deadline = time.monotonic() + wait_timeout

        if self.spend_ledger is not None:
            while True:
                with self._lock, self._lock_ledger():
                    ledger = self._load_and_reconcile_ledger()
                    settled = float(ledger["settled_usd"])
                    reserved = float(ledger["reserved_usd"])
                    effective = float(ledger["effective_spend_usd"])
                    if effective + amount <= self.cap_usd + 1e-9:
                        reservation_id = uuid.uuid4().hex
                        res = Reservation(
                            reservation_id=reservation_id,
                            cell_key=cell_key,
                            reserved_usd=amount,
                            at=datetime.now(UTC).isoformat(),
                            gate=self,
                            metadata=dict(metadata or {}),
                        )
                        ledger["reservations"].append({
                            "reservation_id": reservation_id,
                            "cell_key": cell_key,
                            "at": res.at,
                            "pid": res.pid,
                            "reserved_usd": amount,
                            "status": "reserved",
                            "metadata": res.metadata,
                        })
                        ledger["reserved_usd"] = round(reserved + amount, 6)
                        ledger["effective_spend_usd"] = round(
                            settled + ledger["reserved_usd"], 6
                        )
                        ledger["executions"] = int(ledger.get("executions", 0)) + 1
                        self.executions = ledger["executions"]
                        self._reserved_usd = ledger["reserved_usd"]
                        self._settled_usd = settled
                        self._write_ledger_atomic(ledger)
                        self._active_reservations[reservation_id] = res
                        return res

                    # Check if permanently stopped: settled alone exceeds cap, or nothing in flight, or wait disabled
                    if (wait_timeout <= 0) or (settled + amount > self.cap_usd + 1e-9) or (reserved <= 1e-9):
                        raise SpendCapReached(
                            f"reservation ${amount:.4f} would take spend (${settled:.4f} settled + ${amount:.4f} = "
                            f"${settled + amount:.4f}) over approved cap ${self.cap_usd:.4f}"
                        )
                if time.monotonic() >= deadline:
                    raise SpendCapReached(
                        f"timed out waiting for in-flight reservations to settle; reservation ${amount:.4f} exceeds remaining cap"
                    )
                time.sleep(poll_interval)

        # In-memory path
        with self._lock:
            while True:
                effective = self._settled_usd + self._reserved_usd
                if effective + amount <= self.cap_usd + 1e-9:
                    reservation_id = uuid.uuid4().hex
                    res = Reservation(
                        reservation_id=reservation_id,
                        cell_key=cell_key,
                        reserved_usd=amount,
                        at=datetime.now(UTC).isoformat(),
                        gate=self,
                        metadata=dict(metadata or {}),
                    )
                    self._active_reservations[reservation_id] = res
                    self._reserved_usd = round(self._reserved_usd + amount, 6)
                    self.executions += 1
                    return res

                if (wait_timeout <= 0) or (self._settled_usd + amount > self.cap_usd + 1e-9) or (self._reserved_usd <= 1e-9):
                    raise SpendCapReached(
                        f"reservation ${amount:.4f} would take spend (${self._settled_usd:.4f} settled + ${amount:.4f} = "
                        f"${self._settled_usd + amount:.4f}) over approved cap ${self.cap_usd:.4f}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpendCapReached(
                        f"timed out waiting for in-flight reservations to settle; reservation ${amount:.4f} exceeds remaining cap"
                    )
                self._cond.wait(timeout=min(poll_interval, remaining))

    def extend_reservation(self, reservation: Reservation, additional_usd: float) -> bool:
        """Extend an active reservation by additional_usd if budget permits."""
        if additional_usd <= 0:
            return True
        additional_usd = round(float(additional_usd), 6)
        if self.spend_ledger is not None:
            with self._lock, self._lock_ledger():
                ledger = self._load_and_reconcile_ledger()
                effective = float(ledger["effective_spend_usd"])
                if effective + additional_usd > self.cap_usd + 1e-9:
                    return False
                found = False
                for r in ledger.get("reservations", []):
                    if r.get("reservation_id") == reservation.reservation_id and r.get("status") == "reserved":
                        r["reserved_usd"] = round(float(r.get("reserved_usd", 0.0)) + additional_usd, 6)
                        found = True
                        break
                if not found:
                    return False
                ledger["reserved_usd"] = round(float(ledger["reserved_usd"]) + additional_usd, 6)
                ledger["effective_spend_usd"] = round(
                    float(ledger["settled_usd"]) + ledger["reserved_usd"], 6
                )
                self._write_ledger_atomic(ledger)
                self._reserved_usd = ledger["reserved_usd"]
                self._settled_usd = ledger["settled_usd"]
                reservation.reserved_usd = round(reservation.reserved_usd + additional_usd, 6)
                return True
        else:
            with self._lock:
                if (self._settled_usd + self._reserved_usd + additional_usd) > self.cap_usd + 1e-9:
                    return False
                self._reserved_usd = round(self._reserved_usd + additional_usd, 6)
                reservation.reserved_usd = round(reservation.reserved_usd + additional_usd, 6)
                return True

    def settle(
        self,
        reservation: Reservation | str,
        actual_usd: float,
        *,
        status: str = "settled",
    ) -> float:
        """Settle a reservation to actual_usd and release difference. Returns released USD."""
        actual_usd = round(max(0.0, float(actual_usd)), 6)
        res_id = reservation.reservation_id if isinstance(reservation, Reservation) else reservation

        with self._lock:
            released = 0.0
            cell_key = ""
            if self.spend_ledger is not None:
                with self._lock_ledger():
                    ledger = self._load_and_reconcile_ledger()
                    found_entry = None
                    for r in ledger.get("reservations", []):
                        if r.get("reservation_id") == res_id:
                            found_entry = r
                            break
                    if found_entry is not None:
                        prior_status = found_entry.get("status")
                        prior_settled = float(found_entry.get("settled_usd", 0.0))
                        cell_key = found_entry.get("cell_key", "")
                        if prior_status == "reserved":
                            reserved = float(found_entry["reserved_usd"])
                            released = round(max(0.0, reserved - actual_usd), 6)
                            found_entry["settled_usd"] = actual_usd
                            found_entry["released_usd"] = released
                            found_entry["status"] = status
                            found_entry["settled_at"] = datetime.now(UTC).isoformat()
                            self._log_spend(cell_key, actual_usd, status)
                        else:
                            # SHOULD 3: Late settle or adjustment on non-reserved entry
                            if status in {"released"} and prior_status in {
                                "settled",
                                "completed",
                                "failed",
                                "stale_settled",
                            }:
                                # Double release or release after settle: harmless no-op
                                pass
                            elif actual_usd == prior_settled:
                                # Double settle with identical cost: harmless no-op
                                pass
                            else:
                                delta = round(actual_usd - prior_settled, 6)
                                found_entry["settled_usd"] = actual_usd
                                found_entry["status"] = status
                                found_entry["adjustment_usd"] = delta
                                found_entry["settled_at"] = datetime.now(UTC).isoformat()
                                self._log_spend(cell_key, actual_usd, f"{status}_adjustment")

                        outstanding = round(
                            sum(
                                float(r["reserved_usd"])
                                for r in ledger["reservations"]
                                if r.get("status") == "reserved"
                            ),
                            6,
                        )
                        settled = round(
                            sum(
                                float(r.get("settled_usd", 0.0))
                                for r in ledger["reservations"]
                                if r.get("status") is not None
                                and r.get("status") not in {"reserved", "stale_released"}
                            ),
                            6,
                        )
                        ledger["reserved_usd"] = outstanding
                        ledger["settled_usd"] = settled
                        ledger["cumulative_spend_usd"] = settled
                        ledger["effective_spend_usd"] = round(outstanding + settled, 6)
                        self._reserved_usd = outstanding
                        self._settled_usd = settled
                        self._write_ledger_atomic(ledger)

                    self._active_reservations.pop(res_id, None)
                    if isinstance(reservation, Reservation):
                        reservation.status = status
                        reservation.settled_usd = actual_usd
                        reservation.released_usd = released
                    self._cond.notify_all()
                    return released

            # In-memory path
            res = self._active_reservations.pop(res_id, None)
            if res is not None and res.status == "reserved":
                reserved = res.reserved_usd
                released = round(max(0.0, reserved - actual_usd), 6)
                res.settled_usd = actual_usd
                res.released_usd = released
                res.status = status
                cell_key = res.cell_key
                self._reserved_usd = round(max(0.0, self._reserved_usd - reserved), 6)
                if status != "stale_released":
                    self._settled_usd = round(self._settled_usd + actual_usd, 6)
                self._log_spend(cell_key, actual_usd, status)
            elif isinstance(reservation, Reservation) and reservation.settled_usd is not None:
                prior = reservation.settled_usd
                if status != "released" and actual_usd != prior:
                    delta = round(actual_usd - prior, 6)
                    self._settled_usd = round(self._settled_usd + delta, 6)
                    self._log_spend(cell_key, actual_usd, f"{status}_adjustment")

            if isinstance(reservation, Reservation):
                reservation.status = status
                reservation.settled_usd = actual_usd
                reservation.released_usd = released
            self._cond.notify_all()
            return released

    def _log_spend(self, cell_key: str, actual_usd: float, status: str) -> None:
        if self.spend_log is None or actual_usd <= 0:
            return
        entry = {
            "cost_usd": actual_usd,
            "job": f"{self.modal_app_name or 'modal-cell'}:{cell_key[:12]}",
            "note": f"cell {cell_key[:12]} status={status}",
            "route": "modal",
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            lock_path = self.spend_log.with_name(self.spend_log.name + ".lock")
            with _FileLock(lock_path), self.spend_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("failed to append spend log entry: %s", exc)

    def reserve_cell(
        self,
        cell_key: str,
        package: Path | str | None = None,
        backend: str = "modal",
        timeout_seconds: float | None = None,
        amount: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Context manager reserving and setting active thread/context reservation."""
        return self.reserve(
            amount=amount,
            cell_key=cell_key,
            package=package,
            backend=backend,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )

    def charge(self) -> None:
        """Satisfy ModalBackend / CloudSpendGate protocol without double-charging active cell."""
        current = _CURRENT_RESERVATION.get() or _THREAD_RESERVATIONS.get(threading.get_ident())
        if current is not None:
            with self._lock:
                self.executions += 1
            return
        res = self.reserve(self.usd_per_execution)
        res.settle(self.usd_per_execution, status="settled")
    def summary(self) -> dict[str, Any]:
        """Summary of current spend gate status."""
        with self._lock:
            return {
                "approval": self.approval,
                "cap_usd": self.cap_usd,
                "reserved_usd": self._reserved_usd,
                "settled_usd": self._settled_usd,
                "cumulative_spend_usd": self._settled_usd,
                "effective_spend_usd": round(self._settled_usd + self._reserved_usd, 6),
                "remaining_usd": round(
                    max(0.0, self.cap_usd - (self._settled_usd + self._reserved_usd)), 6
                ),
                "executions": self.executions,
            }

    def to_json(self) -> dict[str, Any]:
        """JSON-serializable representation of gate state for trial facts."""
        with self._lock:
            attribution: dict[str, Any] = {}
            if self.spend_ledger is not None:
                attribution.update(
                    spend_ledger=str(self.spend_ledger),
                    settled_usd=self._settled_usd,
                )
            if self.modal_app_name is not None or self.billing_tag is not None:
                attribution.update(
                    modal_app_name=self.modal_app_name, billing_tag=self.billing_tag
                )
            return {
                "approval": self.approval,
                "cap_usd": self.cap_usd,
                "usd_per_execution": self.usd_per_execution,
                "executions": self.executions,
                "reserved_usd": self._reserved_usd,
                "settled_usd": self._settled_usd,
                "effective_spend_usd": round(self._settled_usd + self._reserved_usd, 6),
                **attribution,
            }


class _FileLock:
    """Simple cross-process file lock using flock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd: Any = None

    def __enter__(self) -> _FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = self.lock_path.open("a+")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SandboxMeter (Drop-in replacement for mutant_auditor.SandboxMeter)
# ---------------------------------------------------------------------------


class SandboxMeter:
    """Cost estimate from measured sandbox seconds with reserve/settle support.

    Drop-in replacement for legacy mutant_auditor.SandboxMeter.
    """

    def __init__(self, cap_usd: float, gate: ReserveSettleSpendGate | None = None):
        self.cap = float(cap_usd)
        self.spent = 0.0
        self.cells = 0
        self.seconds = 0.0
        self.gate = gate
        self._lock = threading.Lock()

    @staticmethod
    def estimate(package: Path, wall_seconds: float) -> float:
        return estimate_modal_cost(package, wall_seconds)

    def admit(self, projected_usd: float) -> None:
        with self._lock:
            if self.spent + projected_usd > self.cap:
                raise RuntimeError(
                    f"Modal cap ${self.cap:.2f} would be passed (spent ${self.spent:.3f})"
                )

    def add(self, usd: float, wall_seconds: float) -> None:
        with self._lock:
            self.spent += usd
            self.seconds += wall_seconds
            self.cells += 1


# ---------------------------------------------------------------------------
# Keyed Cell Specification and Batch Runner
# ---------------------------------------------------------------------------


def modal_cell_runner(
    *,
    repo_root: Path,
    spend_gate: Any,
    meter: SandboxMeter | None = None,
    timeout_seconds: int = 600,
    results_dir: Path | None = None,
    backend_factory: Callable[..., Any] | None = None,
) -> CellRunner:
    """A CellRunner over ModalBackend; result.json is the consumption marker.

    Default timeout_seconds is 600s rather than 1800s to bound worst-case overrun
    per in-flight cell: (kill_timeout_cost - reservation).
    For 3 packages x 6 cells = 18 in flight on 2 CPU / 8 GiB instances:
    Projected 240s reservation is $0.0397. 600s kill timeout is $0.0991.
    Worst-case overrun per in-flight cell is $(0.0991 - 0.0397) = $0.0594.
    Aggregate worst-case overrun for 18 cells is 18 * $0.0594 = $1.0692
    (compared to 18 * $(0.2974 - 0.0397) = $4.6386 under 1800s).

    Returns cached or executed result dict; cached results include `cache_hit: True`.
    """

    def run(cell_dir: Path, package: Path, script: str) -> dict[str, Any]:
        variant = cell_dir.name
        pkg_path = Path(package).resolve()
        pkg_digest = task_directory_digest(pkg_path) if pkg_path.is_dir() else sha256_text(str(pkg_path))
        sub_digest = sha256_text(script)
        cell_key = compute_cell_key(
            package_digest=pkg_digest,
            variant=variant,
            submission_digest=sub_digest,
            backend_image="modal-default",
            grader_hook="",
            timeout_seconds=timeout_seconds,
        )

        cache_dir = results_dir or cell_dir.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        key_lock_path = cache_dir / f"{cell_key}.lock"
        with _FileLock(key_lock_path):
            result_path = cell_dir / "result.json"
            if result_path.exists():
                try:
                    data = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["cache_hit"] = True
                        return data
                except Exception:
                    pass

            keyed_result_path = cache_dir / cell_key / "result.json"
            if keyed_result_path.is_file():
                try:
                    data = json.loads(keyed_result_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["cache_hit"] = True
                        cell_dir.mkdir(parents=True, exist_ok=True)
                        _write_json_atomic(result_path, data)
                        return data
                except Exception:
                    pass

            if isinstance(spend_gate, ReserveSettleSpendGate):
                if meter is not None:
                    meter.admit(meter.estimate(pkg_path, PROJECTED_CELL_SECONDS))
                reservation = spend_gate.reserve(
                    cell_key=cell_key,
                    package=pkg_path,
                    backend="modal",
                    timeout_seconds=timeout_seconds,
                    wait_timeout=60.0,
                )
                started = time.monotonic()
                cell_dir.mkdir(parents=True, exist_ok=True)
                settled = False
                with reservation:
                    try:
                        if backend_factory is not None:
                            backend = backend_factory(repo_root=repo_root, spend_gate=spend_gate)
                        else:
                            from evallab.qa.task_modal import ModalBackend

                            backend = ModalBackend(repo_root=repo_root, spend_gate=spend_gate)

                        observation = backend.run_container_trial(
                            pkg_path,
                            digest=pkg_digest,
                            mode="shortcut",
                            script=script,
                            trial_dir=cell_dir / "trial",
                            timeout_seconds=timeout_seconds,
                        )
                        wall = time.monotonic() - started
                        cost = spend_gate.estimate_cost(pkg_path, wall)
                        reservation.settle(cost, status="completed")
                        settled = True
                        if meter is not None:
                            meter.add(cost, wall)

                        trial = cell_dir / "trial"
                        agent_log = _read_text(trial / "agent/oracle.txt")
                        test_output = _read_text(trial / "verifier/test_output.log")
                        result = {
                            "cell_key": cell_key,
                            "status": observation.status,
                            "reason": observation.reason,
                            "reward": observation.facts.get("reward"),
                            "wall_s": round(wall, 1),
                            "est_usd": round(cost, 5),
                            "agent_log": agent_log,
                            "test_output_tail": test_output[-600:],
                            "evidence_sha256": sha256_text(agent_log + test_output),
                            "cache_hit": False,
                        }
                        _write_json_atomic(result_path, result)
                        if results_dir is not None:
                            (cache_dir / cell_key).mkdir(parents=True, exist_ok=True)
                            _write_json_atomic(keyed_result_path, result)
                        return result
                    finally:
                        if not settled and reservation.status == "reserved":
                            wall = time.monotonic() - started
                            cost = spend_gate.estimate_cost(pkg_path, wall) if wall > 0 else 0.0
                            reservation.settle(cost, status="failed")
                            if meter is not None and cost > 0:
                                meter.add(cost, wall)
            else:
                # Legacy spend_gate / meter path
                if meter is not None:
                    meter.admit(meter.estimate(pkg_path, PROJECTED_CELL_SECONDS))
                cell_dir.mkdir(parents=True, exist_ok=True)
                if backend_factory is not None:
                    backend = backend_factory(repo_root=repo_root, spend_gate=spend_gate)
                else:
                    from evallab.qa.task_modal import ModalBackend

                    backend = ModalBackend(repo_root=repo_root, spend_gate=spend_gate)
                started = time.monotonic()
                observation = backend.run_container_trial(
                    pkg_path,
                    digest=pkg_digest,
                    mode="shortcut",
                    script=script,
                    trial_dir=cell_dir / "trial",
                    timeout_seconds=timeout_seconds,
                )
                wall = time.monotonic() - started
                trial = cell_dir / "trial"
                agent_log = _read_text(trial / "agent/oracle.txt")
                test_output = _read_text(trial / "verifier/test_output.log")
                cost = meter.estimate(pkg_path, wall) if meter else 0.0
                if meter is not None:
                    meter.add(cost, wall)
                result = {
                    "cell_key": cell_key,
                    "status": observation.status,
                    "reason": observation.reason,
                    "reward": observation.facts.get("reward"),
                    "wall_s": round(wall, 1),
                    "est_usd": round(cost, 5),
                    "agent_log": agent_log,
                    "test_output_tail": test_output[-600:],
                    "evidence_sha256": sha256_text(agent_log + test_output),
                    "cache_hit": False,
                }
                _write_json_atomic(result_path, result)
                return result
    return run


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
