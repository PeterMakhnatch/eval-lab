"""HAR-191's scoped, CPU-only Sandbox admission and durable cost accounting.

This implements the approved batch formula, not an account-level dollar kill switch.
Compute reservations bound Sandbox runtime at the hard resource/lifetime limits;
cold image pulls and unobserved egress remain explicitly accepted residual risks.
Delayed billing never releases an attempted allocation to zero. Only the parent
calls the read-only provider function; importing this module does not import Modal
or create provider objects. The journal has one writer, the sweep orchestrator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

CPU_REQUEST = 0.125
CPU_LIMIT = 1.0
MEMORY_REQUEST_MIB = 128
MEMORY_LIMIT_MIB = 4096
SANDBOX_SECONDS = 180
MAX_BATCH_TASKS = 100
CAP_USD = Decimal("2.00")
MAX_OPEN_TASKS = 20

_LIMITATIONS = [
    "Hourly report data can arrive late; absent rows do not prove settled zero usage.",
    "The current hour is queried in full but is incomplete until it ends and billing settles.",
    "The start hour includes the whole bucket; exact owned apps must belong to this study.",
    "Compute reservations do not bound cold registry pulls or unobserved egress charges.",
]
_POLICY = {
    "cpu_request": str(CPU_REQUEST),
    "cpu_limit": str(CPU_LIMIT),
    "memory_request_mib": MEMORY_REQUEST_MIB,
    "memory_limit_mib": MEMORY_LIMIT_MIB,
    "sandbox_seconds": SANDBOX_SECONDS,
    "max_batch_tasks": MAX_BATCH_TASKS,
    "cap_usd": str(CAP_USD),
    "max_open_tasks": MAX_OPEN_TASKS,
}


class BudgetError(ValueError):
    """Admission, evidence, or journal identity is insufficient to proceed safely."""


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise BudgetError(f"{name} must be a Decimal or decimal string, not {type(value).__name__}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise BudgetError(f"Invalid {name}") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise BudgetError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _precision(values: list[Decimal]) -> int:
    # Preserve inputs exactly, including small increments next to a larger cost.
    return max(
        50, max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + 20
    )


def _sum(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    with localcontext() as context:
        context.prec = _precision(values)
        context.rounding = ROUND_CEILING
        return sum(values, Decimal("0"))


def _rates(rates: Mapping) -> dict[str, str]:
    if not isinstance(rates, Mapping) or not rates:
        raise BudgetError("Missing Sandbox pricing rates")
    normalized = {}
    for name, value in rates.items():
        if not isinstance(name, str) or not name:
            raise BudgetError("Invalid rate name")
        normalized[name] = _text(_decimal(value, f"rate {name}"))
    for name in ("cpu_hour_cost_sandbox", "mem_gib_hour_cost_sandbox"):
        if name not in normalized:
            raise BudgetError(f"Missing {name}; function rates are not Sandbox rates")
        _decimal(normalized[name], name, positive=True)
    return normalized


def _runtime_cost(rates: Mapping, seconds: int) -> Decimal:
    values = _rates(rates)
    cpu = Decimal(values["cpu_hour_cost_sandbox"])
    memory = Decimal(values["mem_gib_hour_cost_sandbox"])
    with localcontext() as context:
        context.prec = _precision([cpu, memory])
        context.rounding = ROUND_CEILING
        hourly = cpu * Decimal(str(CPU_LIMIT)) + memory * Decimal(MEMORY_LIMIT_MIB) / 1024
        # Division can recur for a one-second lifetime; round upward, never down.
        return hourly * seconds / 3600


def per_sandbox_worst_case(rates: dict[str, str]) -> Decimal:
    """Reserve 180 seconds at one physical core (= two vCPU) and four GiB."""
    return _runtime_cost(rates, SANDBOX_SECONDS)


def _utc(value: Any, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BudgetError(f"Invalid {name} timestamp") from exc
    if not isinstance(value, datetime):
        raise BudgetError(f"Missing or invalid {name} timestamp")
    # The official SDK also interprets timezone-naive timestamps as UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ids(values: Any, name: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise BudgetError(f"{name} must be a nonempty list")
    if any(not isinstance(v, str) or not v or v.strip() != v for v in values):
        raise BudgetError(f"Invalid {name}")
    if len(values) != len(set(values)):
        raise BudgetError(f"Duplicate {name}")
    return list(values)


def _field(item: Any, name: str) -> Any:
    # Modal 1.6.1 returns frozen BillingReportItem dataclasses, not dictionaries.
    return item[name] if isinstance(item, Mapping) else getattr(item, name)


def _billing_rows_snapshot(
    items: list[Any],
    app_ids: list[str],
    started_at: datetime,
    queried_at: datetime,
    observed_at: datetime,
) -> dict:
    """Normalize SDK rows (or retained local rows) without any provider call."""
    owned = _ids(app_ids, "owned app IDs")
    start = _utc(started_at, "study start")
    query = _utc(queried_at, "query start")
    observed = _utc(observed_at, "observation")
    if start > query or observed < query:
        raise BudgetError("Invalid billing observation chronology")
    window_start = start.replace(minute=0, second=0, microsecond=0)
    hour = query.replace(minute=0, second=0, microsecond=0)
    window_end = hour if query == hour else hour + timedelta(hours=1)
    rows = []
    seen = set()
    for item in items:
        if _field(item, "object_id") not in owned:
            continue
        object_id = _field(item, "object_id")
        interval = _utc(_field(item, "interval_start"), "billing interval")
        if not window_start <= interval < window_end:
            raise BudgetError("Owned billing row falls outside the requested window")
        key = (object_id, interval)
        if key in seen:
            raise BudgetError("Duplicate owned app billing interval")
        seen.add(key)
        cost = _decimal(_field(item, "cost"), "owned app cost")
        resources = _field(item, "cost_by_resource")
        if not isinstance(resources, Mapping):
            raise BudgetError("Missing owned app resource cost breakdown")
        resource_costs = {k: _text(_decimal(v, f"resource {k}")) for k, v in resources.items()}
        # Never reduce metered usage by a credit/net-charge discrepancy.
        counted = max(cost, _sum([Decimal(v) for v in resource_costs.values()]))
        rows.append(
            {
                "object_id": object_id,
                "description": _field(item, "description"),
                "environment_name": _field(item, "environment_name"),
                "interval_start": _stamp(interval),
                "cost": _text(cost),
                "cost_by_resource": resource_costs,
                "counted_usd": _text(counted),
                "tags": dict(_field(item, "tags")),
            }
        )
    return {
        "actual_usd": _text(_sum([Decimal(row["counted_usd"]) for row in rows])),
        "observed_at": _stamp(observed),
        "requested_start_at": _stamp(start),
        "window_start": _stamp(window_start),
        "window_end": _stamp(window_end),
        "app_ids": owned,
        "rows": rows,
        "missing_app_ids": sorted(set(owned) - {row["object_id"] for row in rows}),
        "billing_settled": False,
        "partial_current_hour": query != hour,
        "cost_basis": "nonnegative owned metered cost, at least the resource breakdown sum",
        "limitations": list(_LIMITATIONS),
    }


async def billing_snapshot(workspace: Any, app_ids: list[str], started_at: datetime) -> dict:
    """Read only Workspace.billing.report.aio, filtered to exact study app IDs.

    SDK report drops a partial final interval, so query its ceiling-hour boundary.
    A future end retains the current bucket, not a guarantee of timely billing.
    """
    owned = _ids(app_ids, "owned app IDs")
    start = _utc(started_at, "study start")
    queried = datetime.now(UTC)
    if start > queried:
        raise BudgetError("Study start is in the future")
    hour = queried.replace(minute=0, second=0, microsecond=0)
    end = hour if queried == hour else hour + timedelta(hours=1)
    items = await workspace.billing.report.aio(
        start=start.replace(minute=0, second=0, microsecond=0),
        end=end,
        resolution="h",
        tag_names=["*"],
    )
    return _billing_rows_snapshot(items, owned, start, queried, datetime.now(UTC))


def _snapshot(value: Any, previous: dict | None = None) -> dict:
    if not isinstance(value, dict):
        raise BudgetError("Missing billing snapshot; unknown cost is not zero")
    result = copy.deepcopy(value)
    actual = _decimal(result.get("actual_usd"), "observed actual cost")
    apps = _ids(result.get("app_ids"), "owned app IDs")
    start = _utc(result.get("window_start"), "billing window start")
    end = _utc(result.get("window_end"), "billing window end")
    observed = _utc(result.get("observed_at"), "billing observation")
    if start >= end or observed < start:
        raise BudgetError("Invalid cumulative billing window")
    if any(t.minute or t.second or t.microsecond for t in (start, end)):
        raise BudgetError("Billing windows must use full-hour boundaries")
    if previous is not None:
        if start != _utc(previous["window_start"], "previous billing start"):
            raise BudgetError("Billing window start changed on resume")
        if not set(previous["app_ids"]).issubset(apps):
            raise BudgetError("Billing snapshot omitted a previously owned app")
        if observed < _utc(previous["observed_at"], "previous observation"):
            raise BudgetError("Billing observation went backwards")
        if end < _utc(previous["window_end"], "previous billing end"):
            raise BudgetError("Cumulative billing window shrank")
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise BudgetError("Missing retained provider billing rows")
    counted = []
    seen = set()
    by_app = {app_id: [] for app_id in apps}
    for row in rows:
        if not isinstance(row, dict) or row.get("object_id") not in apps:
            raise BudgetError("Billing snapshot contains an unowned object")
        interval = _utc(row.get("interval_start"), "retained billing interval")
        key = (row["object_id"], interval)
        if key in seen or not start <= interval < end:
            raise BudgetError("Duplicate or out-of-window retained billing row")
        seen.add(key)
        cost = _decimal(row.get("cost"), "retained owned cost")
        resources = row.get("cost_by_resource", {})
        if not isinstance(resources, dict):
            raise BudgetError("Invalid retained resource costs")
        breakdown = _sum([_decimal(v, "retained resource cost") for v in resources.values()])
        gross = max(cost, breakdown, _decimal(row.get("counted_usd", _text(cost)), "counted cost"))
        row["counted_usd"] = _text(gross)
        counted.append(gross)
        by_app[row["object_id"]].append(gross)
    if actual != _sum(counted):
        raise BudgetError("Snapshot actual cost does not match its gross owned billing rows")
    result["actual_by_app_usd"] = {app_id: _text(_sum(costs)) for app_id, costs in by_app.items()}
    result.update(
        actual_usd=_text(actual),
        app_ids=apps,
        window_start=_stamp(start),
        window_end=_stamp(end),
        observed_at=_stamp(observed),
        billing_settled=False,
    )
    result["limitations"] = list(_LIMITATIONS)
    return result


def _record_snapshot(state: dict, observed: dict) -> None:
    """Maintain gross high-water marks independently for every exact owned app."""
    for app_id, cost in observed["actual_by_app_usd"].items():
        previous = Decimal(state["actual_by_app_usd"].get(app_id, "0"))
        state["actual_by_app_usd"][app_id] = _text(max(previous, Decimal(cost)))
    state["actual_usd"] = _text(_sum([Decimal(v) for v in state["actual_by_app_usd"].values()]))
    state["latest_snapshot"] = observed
    state["snapshots"].append(observed)


def _exposure(state: dict) -> Decimal:
    """Never let delayed compute on one app swallow posted overhead on another."""
    upper_by_app = {
        batch["app_id"]: Decimal(batch["retained_upper_usd"]) for batch in state["batches"]
    }
    apps = set(upper_by_app).union(state["actual_by_app_usd"])
    return _sum(
        [
            max(
                upper_by_app.get(app_id, Decimal("0")),
                Decimal(state["actual_by_app_usd"].get(app_id, "0")),
            )
            for app_id in apps
        ]
    )


def _sandbox_seconds(report: dict) -> tuple[int, str | None]:
    """Unknown attempts cost the entire reservation; explicit non-attempts cost zero."""
    if (
        report.get("creation_attempted") is False
        and report.get("terminal_confirmed") is True
        and report.get("sandbox_id") is None
        and type(report.get("lifetime_upper_seconds")) is int
        and report["lifetime_upper_seconds"] == 0
    ):
        return 0, None
    if report.get("creation_attempted") is not True or report.get("terminal_confirmed") is not True:
        return SANDBOX_SECONDS, "Allocation or termination is unconfirmed"
    sandbox_id = report.get("sandbox_id")
    seconds = report.get("lifetime_upper_seconds")
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return SANDBOX_SECONDS, "Attempted allocation has no actual Sandbox ID"
    if type(seconds) is not int or not 0 <= seconds <= SANDBOX_SECONDS:
        return SANDBOX_SECONDS, "Invalid Sandbox lifetime upper bound"
    try:
        start = _utc(report.get("creation_started_at"), "Sandbox creation")
        end = _utc(report.get("terminated_at"), "Sandbox termination")
    except BudgetError as exc:
        return SANDBOX_SECONDS, str(exc)
    if end < start:
        return SANDBOX_SECONDS, "Sandbox termination precedes creation"
    measured = min(SANDBOX_SECONDS, max(1, math.ceil((end - start).total_seconds())))
    return max(seconds, measured), None


def _bounds(
    rates: dict,
    expected: int,
    reports: list[dict],
    prior_ids: set[str],
    history: list[list[dict]] | None = None,
) -> tuple[Decimal, list[str]]:
    errors = []
    seconds = []
    seen = set(prior_ids)
    for index in range(max(expected, len(reports))):
        if index >= len(reports):
            seconds.append(SANDBOX_SECONDS)
            errors.append(f"Sandbox {index}: no allocation/cleanup report")
            continue
        report = reports[index]
        duration, reason = _sandbox_seconds(report)
        sandbox_id = report.get("sandbox_id")
        for earlier in history or []:
            if index < len(earlier) and earlier[index].get("sandbox_id") == sandbox_id:
                prior_duration, prior_reason = _sandbox_seconds(earlier[index])
                if prior_reason is None:
                    duration = max(duration, prior_duration)
        if sandbox_id is not None:
            if not isinstance(sandbox_id, str) or not sandbox_id or sandbox_id in seen:
                duration, reason = SANDBOX_SECONDS, "Duplicate or invalid Sandbox ID"
            else:
                seen.add(sandbox_id)
        if reason:
            errors.append(f"Sandbox {index}: {reason}")
        seconds.append(duration)
    if len(reports) > expected:
        errors.append("Unreserved additional allocation reports; stop execution")
    return _sum([_runtime_cost(rates, duration) for duration in seconds]), errors


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetError(f"Duplicate JSON journal key {key}")
        result[key] = value
    return result


class BudgetLedger:
    """One atomic/fsynced journal; unresolved admission survives process crashes."""

    def __init__(self, path: Path, rates: dict, manifest_sha256: str, authority: str):
        self.path = Path(path)
        self.rates = _rates(rates)
        if (
            not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or any(c not in "0123456789abcdef" for c in manifest_sha256)
        ):
            raise BudgetError("Manifest identity must be a lowercase SHA-256 hex digest")
        if not isinstance(authority, str) or not authority.strip():
            raise BudgetError("Missing study authority")
        self._identity = {
            "manifest_sha256": manifest_sha256,
            "authority": authority,
            "rates": self.rates,
            "policy": _POLICY,
        }
        self._disk_digest = None
        self._write_failed = False
        if self.path.exists():
            raw = self.path.read_bytes()
            try:
                state = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError) as exc:
                raise BudgetError("Unreadable budget journal; do not reset or relaunch") from exc
            try:
                self._validate(state)
            except (KeyError, TypeError, AttributeError) as exc:
                raise BudgetError("Malformed budget journal; do not reset or relaunch") from exc
            self._state = state
            self._disk_digest = hashlib.sha256(raw).hexdigest()
        else:
            self._commit(
                {
                    "schema_version": 2,
                    **copy.deepcopy(self._identity),
                    "actual_usd": "0",
                    "actual_by_app_usd": {},
                    "latest_snapshot": None,
                    "batches": [],
                    "snapshots": [],
                    "refusals": [],
                    "limitations": list(_LIMITATIONS),
                }
            )

    def _validate(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != 2:
            raise BudgetError("Unknown budget journal schema")
        if any(state.get(key) != value for key, value in self._identity.items()):
            raise BudgetError("Budget journal manifest, authority, pricing, or limits mismatch")
        if not isinstance(state.get("batches"), list):
            raise BudgetError("Invalid batch journal")
        previous = None
        actual_by_app = {}
        if not isinstance(state.get("snapshots"), list):
            raise BudgetError("Missing cumulative snapshot history")
        for snapshot in state["snapshots"]:
            previous = _snapshot(snapshot, previous)
            for app_id, cost in previous["actual_by_app_usd"].items():
                actual_by_app[app_id] = _text(
                    max(
                        Decimal(actual_by_app.get(app_id, "0")),
                        Decimal(cost),
                    )
                )
        if state.get("latest_snapshot") != previous:
            raise BudgetError("Latest provider snapshot does not match journal history")
        if state.get("actual_by_app_usd") != actual_by_app:
            raise BudgetError("Per-app observed cost attribution does not match journal history")
        if _decimal(state.get("actual_usd"), "journal observed actual") != _sum(
            [Decimal(value) for value in actual_by_app.values()]
        ):
            raise BudgetError("Observed actual high-water marks do not match journal history")
        task_sets = {"locked": set(), "open": set()}
        sandbox_ids = set()
        batch_ids = set()
        batch_apps = set()
        pending_seen = False
        for batch in state["batches"]:
            if not isinstance(batch, dict) or batch.get("batch_id") in batch_ids:
                raise BudgetError("Invalid or duplicate journal batch")
            if not isinstance(batch.get("batch_id"), str) or not batch["batch_id"]:
                raise BudgetError("Missing journal batch ID")
            batch_ids.add(batch["batch_id"])
            app_id = batch.get("app_id")
            if not isinstance(app_id, str) or not app_id or app_id in batch_apps:
                raise BudgetError("Missing or reused batch app identity")
            batch_apps.add(app_id)
            kind = batch.get("kind")
            if kind not in task_sets:
                raise BudgetError("Unknown journal batch kind")
            tasks = _ids(batch.get("task_ids"), "batch task IDs")
            expected = len(tasks) * (2 if kind == "locked" else 1)
            if (
                len(tasks) > MAX_BATCH_TASKS
                or type(batch.get("sandbox_count")) is not int
                or batch["sandbox_count"] != expected
                or task_sets[kind].intersection(tasks)
                or pending_seen
            ):
                raise BudgetError(
                    "Invalid counts, duplicate tasks, or admission after a pending batch"
                )
            task_sets[kind].update(tasks)
            if len(task_sets["open"]) > MAX_OPEN_TASKS:
                raise BudgetError("Journal exceeds the distinct open-task limit")
            reserved = _sum([per_sandbox_worst_case(self.rates)] * expected)
            if _decimal(batch.get("reserved_usd"), "journal reservation") != reserved:
                raise BudgetError("Journal reservation does not cover every Sandbox")
            before = _snapshot(batch.get("snapshot_before"))
            if before not in state["snapshots"]:
                raise BudgetError("Batch admission snapshot is absent from cumulative history")
            if app_id not in before["app_ids"]:
                raise BudgetError("Batch app was absent from its admission billing scope")
            observations = batch.get("finish_observations")
            if not isinstance(observations, list):
                raise BudgetError("Invalid finish observation history")
            upper, errors = reserved, ["Batch reserved; allocation/cleanup not yet reported"]
            report_history = []
            for observation in observations:
                if not isinstance(observation, dict):
                    raise BudgetError("Invalid finish observation")
                after = _snapshot(observation.get("snapshot"))
                if after not in state["snapshots"]:
                    raise BudgetError("Batch finish snapshot is absent from cumulative history")
                reports = observation.get("sandboxes")
                if not isinstance(reports, list) or any(not isinstance(r, dict) for r in reports):
                    raise BudgetError("Invalid Sandbox observation history")
                upper, errors = _bounds(self.rates, expected, reports, sandbox_ids, report_history)
                report_history.append(reports)
            status = "reserved" if not observations else "pending" if errors else "finished"
            if (
                batch.get("status") != status
                or _decimal(batch.get("retained_upper_usd"), "retained exposure") != upper
            ):
                raise BudgetError("Journal completion state or retained exposure is inconsistent")
            pending_seen = status != "finished"
            for report in observations[-1]["sandboxes"] if observations else []:
                sandbox_id = report.get("sandbox_id")
                if isinstance(sandbox_id, str) and sandbox_id:
                    sandbox_ids.add(sandbox_id)

    def _assert_current(self) -> None:
        if self._write_failed:
            raise BudgetError("A journal write failed; stop and reopen the durable journal")
        if self._disk_digest is None:
            if self.path.exists():
                raise BudgetError("Another writer created the budget journal")
        elif (
            not self.path.exists()
            or hashlib.sha256(self.path.read_bytes()).hexdigest() != self._disk_digest
        ):
            raise BudgetError(
                "Budget journal changed outside this writer; reopen before proceeding"
            )

    def _commit(self, state: dict) -> None:
        self._assert_current()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(state, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self.path.name}.", dir=self.path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self._write_failed = True
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._state = state
        self._disk_digest = hashlib.sha256(raw).hexdigest()

    @property
    def actual_usd(self) -> Decimal:
        return Decimal(self._state["actual_usd"])

    @property
    def exposure_usd(self) -> Decimal:
        return _exposure(self._state)

    @property
    def pending(self) -> bool:
        return any(b["status"] != "finished" for b in self._state["batches"])

    @property
    def batches(self) -> list[dict]:
        return copy.deepcopy(self._state["batches"])

    @property
    def open_task_ids(self) -> set[str]:
        return {
            task for b in self._state["batches"] if b["kind"] == "open" for task in b["task_ids"]
        }

    def observe(self, snapshot: dict) -> None:
        """Retain late provider charges without inventing a new allocation."""
        self._assert_current()
        observed = _snapshot(snapshot, self._state["latest_snapshot"])
        state = copy.deepcopy(self._state)
        _record_snapshot(state, observed)
        self._commit(state)

    def reserve(
        self,
        task_ids: list[str],
        sandbox_count: int,
        snapshot: dict,
        *,
        kind: str = "locked",
        app_id: str,
    ) -> dict:
        self._assert_current()
        if self.pending:
            raise BudgetError(
                "Pending or unknown allocation/cleanup; do not relaunch or admit a batch"
            )
        if kind not in ("locked", "open"):
            raise BudgetError("Batch kind must be locked or open")
        tasks = _ids(task_ids, "batch task IDs")
        if len(tasks) > MAX_BATCH_TASKS:
            raise BudgetError("Batch exceeds 100 tasks")
        expected = len(tasks) * (2 if kind == "locked" else 1)
        if type(sandbox_count) is not int or sandbox_count != expected:
            raise BudgetError("Reserve both fresh locked arms (2/task), or one open arm (1/task)")
        used = {t for b in self._state["batches"] if b["kind"] == kind for t in b["task_ids"]}
        if used.intersection(tasks):
            raise BudgetError(f"Duplicate {kind} task admission")
        if kind == "open" and len(used.union(tasks)) > MAX_OPEN_TASKS:
            raise BudgetError(
                "Distinct open confirmations exceed 20 tasks, including prior resumes"
            )
        if not isinstance(app_id, str) or not app_id or app_id.strip() != app_id:
            raise BudgetError("A fresh exact app_id is required for each batch")
        if any(batch["app_id"] == app_id for batch in self._state["batches"]):
            raise BudgetError("Batch app_id was already used; allocate a fresh app per batch")
        observed = _snapshot(snapshot, self._state["latest_snapshot"])
        if app_id not in observed["app_ids"]:
            raise BudgetError("Batch app_id is absent from the owned billing snapshot")
        state = copy.deepcopy(self._state)
        _record_snapshot(state, observed)
        actual = Decimal(state["actual_usd"])
        exposure = _exposure(state)
        reserved = _sum([per_sandbox_worst_case(self.rates)] * sandbox_count)
        if _sum([exposure, reserved]) > CAP_USD:
            # A refused batch still durably records its newly observed charges.
            state["refusals"].append(
                {
                    "task_ids": tasks,
                    "kind": kind,
                    "app_id": app_id,
                    "sandbox_count": sandbox_count,
                    "reserved_usd": _text(reserved),
                    "exposure_before_usd": _text(exposure),
                    "snapshot": observed,
                }
            )
            self._commit(state)
            raise BudgetError(
                f"Batch would exceed ${CAP_USD}: exposure {_text(exposure)} + reserve {_text(reserved)}"
            )
        batch = {
            "batch_id": uuid.uuid4().hex,
            "task_ids": tasks,
            "kind": kind,
            "app_id": app_id,
            "sandbox_count": sandbox_count,
            "reserved_usd": _text(reserved),
            "actual_before_usd": _text(actual),
            "exposure_before_usd": _text(exposure),
            "snapshot_before": observed,
            "admitted_at": _stamp(datetime.now(UTC)),
            "status": "reserved",
            "retained_upper_usd": _text(reserved),
            "pending_reasons": ["Batch reserved; allocation/cleanup not yet reported"],
            "finish_observations": [],
        }
        state["batches"].append(batch)
        self._commit(state)
        return copy.deepcopy(batch)

    def finish(self, batch_id: str, snapshot: dict, sandboxes: list[dict]) -> dict:
        self._assert_current()
        matches = [i for i, b in enumerate(self._state["batches"]) if b["batch_id"] == batch_id]
        if not matches:
            raise BudgetError("Unknown batch ID")
        index = matches[0]
        old = self._state["batches"][index]
        if old["status"] == "finished":
            raise BudgetError("Batch already finished; do not replace allocation evidence")
        if not isinstance(sandboxes, list) or any(not isinstance(r, dict) for r in sandboxes):
            raise BudgetError("Sandbox reports must be a list of dictionaries")
        if old["finish_observations"]:
            prior_reports = old["finish_observations"][-1]["sandboxes"]
            if len(sandboxes) < len(prior_reports):
                raise BudgetError("Recovery cannot discard an observed allocation slot")
            for offset, report in enumerate(prior_reports):
                known = report.get("sandbox_id")
                if known is not None and (
                    offset >= len(sandboxes) or sandboxes[offset].get("sandbox_id") != known
                ):
                    raise BudgetError("Recovery cannot replace or discard an observed Sandbox ID")
                if (
                    report.get("creation_attempted") is True
                    and sandboxes[offset].get("creation_attempted") is False
                ):
                    raise BudgetError(
                        "Recovery cannot turn an attempted allocation into a non-attempt"
                    )
                if (
                    _sandbox_seconds(report) == (0, None)
                    and sandboxes[offset].get("creation_attempted") is True
                ):
                    raise BudgetError("Recovery cannot create a previously unattempted Sandbox")
        observed = _snapshot(snapshot, self._state["latest_snapshot"])
        prior_ids = {
            r["sandbox_id"]
            for b in self._state["batches"][:index]
            for observation in b["finish_observations"]
            for r in observation["sandboxes"]
            if isinstance(r.get("sandbox_id"), str) and r["sandbox_id"]
        }
        history = [observation["sandboxes"] for observation in old["finish_observations"]]
        upper, errors = _bounds(self.rates, old["sandbox_count"], sandboxes, prior_ids, history)
        state = copy.deepcopy(self._state)
        batch = state["batches"][index]
        batch["finish_observations"].append(
            {"snapshot": observed, "sandboxes": copy.deepcopy(sandboxes)}
        )
        batch.update(
            status="pending" if errors else "finished",
            retained_upper_usd=_text(upper),
            pending_reasons=errors,
            finished_at=_stamp(datetime.now(UTC)),
            sandboxes=copy.deepcopy(sandboxes),
            snapshot_after=observed,
        )
        _record_snapshot(state, observed)
        batch["actual_after_usd"] = state["actual_usd"]
        batch["exposure_after_usd"] = _text(_exposure(state))
        self._commit(state)
        return copy.deepcopy(batch)
