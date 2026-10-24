"""Bounded DEV item summaries over the ordered production event journal.

A campaign is not a task: identity includes its run, dataset and corpus item.
Enqueue records count submissions, worker records describe lifecycle, and only the
current queue snapshot establishes active leases. No event bodies or trace steps
are retained; the journal and task-scoped inspector remain the evidence authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_SYSTEMS = frozenset({"reward_seeker", "task_quality", "misalignment"})
_ASSET_EVENTS = frozenset({"asset.verified", "verified_asset.created", "asset.promoted"})
_OUTCOME_EVENTS = frozenset(
    {
        "seeker.exploit_confirmed",
        "seeker.grader_feedback",
        "seeker.job_refused",
        "task_quality.repair_qualified",
        "task_quality.repair_rejected",
        "task_quality.repair_unavailable",
    }
)
_LIFECYCLE = {
    "started": "running",
    "heartbeat": "running",
    "completed": "completed",
    "finished": "completed",
    "failed": "failed",
    "blocked": "blocked",
    "stopped": "stopped",
    "cancelled": "stopped",
    "canceled": "stopped",
}
_CONTROL_STATES = {
    "pause": "paused",
    "cancel": "stopped",
    "stop": "stopped",
    "quarantine": "quarantined",
    "resume": "running",
}
_MAX_ASSETS = 100


def _text(value: Any, limit: int = 1024) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return value
    return None


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    asset = payload.get("asset")
    for source in (payload, asset):
        if not isinstance(source, Mapping):
            continue
        if "split" in source and source["split"] != "dev":
            return None
        if ("dataset_id" in source or "item_id" in source) and (
            _text(source.get("dataset_id")) is None
            or _text(source.get("item_id")) is None
            or source.get("split") != "dev"
        ):
            return None
    return {**payload, **asset} if isinstance(asset, Mapping) else payload


def _references(payload: Mapping[str, Any]) -> list[str]:
    references = []
    for name in ("job_id", "source_event_id", "qualification_event_id"):
        value = _text(payload.get(name))
        if value is not None:
            references.append(value)
    for name in ("source_event_ids", "provenance_event_ids"):
        values = payload.get(name)
        if isinstance(values, list):
            references.extend(value for value in values if _text(value) is not None)
    return list(dict.fromkeys(references))


def _has_trace(payload: Mapping[str, Any], kind: str) -> bool:
    # Seeker phase telemetry is not an agent rollout, even when called "step".
    if kind in {"seeker.attempt_started", "seeker.grader_feedback"}:
        return False
    steps = payload.get("steps")
    if not isinstance(steps, list):
        partial = payload.get("partial_trace")
        steps = partial.get("steps") if isinstance(partial, Mapping) else partial
    if not isinstance(steps, list):
        step = payload.get("step")
        steps = [step] if isinstance(step, Mapping) else []
    if not steps and kind == "trace.step" and "step_id" in payload:
        steps = [payload]
    return any(
        isinstance(step, Mapping)
        and isinstance(step.get("step_id", step.get("id")), (str, int))
        and not isinstance(step.get("step_id", step.get("id")), bool)
        for step in steps
    )


def _aggregate(states: list[str]) -> str:
    for state in ("running", "failed", "blocked", "submitted", "stopped", "completed"):
        if state in states:
            return state
    return "observed"


@dataclass
class _Job:
    job_type: str
    state: str = "submitted"

    @property
    def system(self) -> str:
        return self.job_type.partition(".")[0]


@dataclass
class _Item:
    key: str
    run_id: str
    dataset_id: str
    item_id: str
    source_kind: str | None = None
    jobs: dict[str, _Job] = field(default_factory=dict)
    lifecycle: dict[str, str] = field(default_factory=dict)
    event_ids: set[str] = field(default_factory=set)
    attempt_ids: set[str] = field(default_factory=set)
    original_reward: float | None = None
    repaired_reward: float | None = None
    outcome_status: str | None = None
    asset_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    has_trace: bool = False
    control_state: str | None = None
    last_event_type: str = ""
    last_seq: int = 0
    updated_at: str = ""

    def touch(self, event: Mapping[str, Any]) -> None:
        self.last_event_type = str(event["event_type"])
        self.last_seq = int(event["seq"])
        self.updated_at = str(event["ts"])


class RunCatalog:
    """Fold normalized, globally ordered journal envelopes into recent DEV rows.

    Exact distinct-item totals retain opaque keys, not historical row payloads.
    Job/attempt identities deduplicate counts within retained rows. Compact event
    attribution indexes are discarded with their rows; asset lists are capped.
    Old or replayed sequence numbers are ignored, as in the live projection.
    """

    def __init__(self, *, max_items: int = 200) -> None:
        if type(max_items) is not int or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        self.max_items = max_items
        self._last_seq = 0
        self._items: OrderedDict[str, _Item] = OrderedDict()
        self._seen: set[str] = set()
        self._event_items: dict[str, str] = {}
        self._controls: OrderedDict[str, str | None] = OrderedDict()

    def _source_item(self, event_id: str) -> _Item | None:
        key = self._event_items.get(event_id)
        return self._items.get(key) if key is not None else None

    def _identity(
        self, payload: Mapping[str, Any], references: list[str]
    ) -> tuple[str, str] | None:
        if "dataset_id" in payload or "item_id" in payload:
            dataset_id, item_id = _text(payload.get("dataset_id")), _text(payload.get("item_id"))
            if dataset_id is not None and item_id is not None and payload.get("split") == "dev":
                return dataset_id, item_id
            # A reference must never launder an explicit sealed/unresolved identity.
            return None
        if "split" in payload and payload["split"] != "dev":
            return None
        identities = set()
        for reference in references:
            item = self._source_item(reference)
            if item is not None:
                identities.add((item.dataset_id, item.item_id))
        return next(iter(identities)) if len(identities) == 1 else None

    def _apply_control(self, event: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        if event["event_type"] != "control.applied":
            return
        state = _text(payload.get("run_state"), 32) or _CONTROL_STATES.get(
            _text(payload.get("action"), 32) or ""
        )
        if state not in {"running", "paused", "stopped", "quarantined"}:
            return
        run_id = str(event["run_id"])
        # Resume removes the intervention override, revealing item-specific work.
        override = None if state == "running" else state
        self._controls[run_id] = override
        self._controls.move_to_end(run_id)
        while len(self._controls) > self.max_items:
            self._controls.popitem(last=False)
        for item in list(self._items.values()):
            if item.run_id == run_id:
                item.control_state = override
                item.touch(event)
                self._items.move_to_end(item.key)

    @staticmethod
    def _worker_state(
        item: _Item, system: str, kind: str, payload: Mapping[str, Any], references: list[str]
    ) -> str | None:
        prefix, _, suffix = kind.rpartition(".")
        if system not in _SYSTEMS or prefix not in {"worker", "run", system}:
            return None
        state = _LIFECYCLE.get(suffix)
        if state is None:
            return None
        jobs = [item.jobs[reference] for reference in references if reference in item.jobs]
        jobs = [job for job in jobs if job.system == system]
        if not jobs:
            # A direct, normalized item identity plus one matching job is unambiguous.
            job_type = payload.get("job_type")
            jobs = [
                job
                for job in item.jobs.values()
                if job.system == system and (job_type is None or job.job_type == job_type)
            ]
        if len(jobs) == 1:
            jobs[0].state = state
        else:
            item.lifecycle[system] = state
        return state

    def apply(self, event: Mapping[str, Any]) -> None:
        seq = int(event["seq"])
        if seq <= self._last_seq:
            return
        self._last_seq = seq
        run_id = _text(event.get("run_id"))
        event_id = _text(event.get("event_id"))
        kind = _text(event.get("event_type"), 256)
        if run_id is None or event_id is None or kind is None:
            return
        payload = _payload(event)
        if payload is None:
            return
        if kind == "operator.control_requested" or kind.startswith("control."):
            self._apply_control(event, payload)
            return
        references = _references(payload)
        identity = self._identity(payload, references)
        if identity is None:
            return
        dataset_id, item_id = identity
        key = hashlib.sha256(
            json.dumps(
                [run_id, dataset_id, item_id], ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        source_kind = _text(payload.get("source_kind"), 128)
        if source_kind is None:
            for reference in references:
                source = self._source_item(reference)
                if source is not None and (source.dataset_id, source.item_id) == identity:
                    source_kind = source.source_kind
                    if source_kind is not None:
                        break
        item = self._items.get(key)
        if item is None:
            item = _Item(
                key,
                run_id,
                dataset_id,
                item_id,
                source_kind=source_kind,
                control_state=self._controls.get(run_id),
            )
            self._items[key] = item
            self._seen.add(key)
            if len(self._items) > self.max_items:
                _, evicted = self._items.popitem(last=False)
                for source_id in evicted.event_ids:
                    self._event_items.pop(source_id, None)
        if item.source_kind is None:
            item.source_kind = source_kind
        self._event_items[event_id] = key
        item.event_ids.add(event_id)
        job_system = kind.partition(".")[0]
        if job_system in _SYSTEMS and kind.endswith("_task"):
            item.jobs.setdefault(event_id, _Job(kind))
        system = str(event.get("system", ""))
        worker_state = self._worker_state(item, system, kind, payload, references)
        if kind == "seeker.attempt_started":
            item.attempt_ids.add(event_id)
        for name in ("original_reward", "repaired_reward"):
            reward = _number(payload.get(name))
            if name == "original_reward" and reward is None:
                reward = _number(payload.get("upstream_reward"))
            if reward is not None:
                setattr(item, name, reward)
        status = event.get("status")
        if (
            kind in _OUTCOME_EVENTS or worker_state in {"completed", "failed", "stopped"}
        ) and status in {"confirmed", "refuted", "provisional", "applied"}:
            item.outcome_status = status
        if kind in _ASSET_EVENTS and status in {"confirmed", "applied"}:
            asset_id = _text(payload.get("asset_id"), 512)
            if asset_id is not None:
                item.asset_ids[asset_id] = None
                item.asset_ids.move_to_end(asset_id)
                if len(item.asset_ids) > _MAX_ASSETS:
                    item.asset_ids.popitem(last=False)
        item.has_trace = item.has_trace or _has_trace(payload, kind)
        item.touch(event)
        self._items.move_to_end(key)

    def state(self, queue: Mapping[str, Any]) -> dict[str, Any]:
        leases: dict[str, set[str]] = {}
        active = queue.get("active_leases", [])
        for lease in active if isinstance(active, list) else []:
            if not isinstance(lease, Mapping):
                continue
            event_id = _text(lease.get("event_id"))
            if event_id is None:
                continue
            item = self._source_item(event_id)
            if item is None:
                continue
            job = item.jobs.get(event_id)
            if job is not None and lease.get("job_type") == job.job_type:
                leases.setdefault(item.key, set()).add(event_id)
        recent = []
        for item in reversed(self._items.values()):
            item_leases = leases.get(item.key, set())
            systems: dict[str, list[str]] = {}
            for event_id, job in item.jobs.items():
                state = "running" if event_id in item_leases else job.state
                systems.setdefault(job.system, []).append(state)
            for system, state in item.lifecycle.items():
                systems.setdefault(system, []).append(state)
            system_states = {system: _aggregate(states) for system, states in systems.items()}
            recent.append(
                {
                    "key": item.key,
                    "run_id": item.run_id,
                    "dataset_id": item.dataset_id,
                    "item_id": item.item_id,
                    "split": "dev",
                    "source_kind": item.source_kind,
                    "run_state": item.control_state or _aggregate(list(system_states.values())),
                    "system_states": system_states,
                    "jobs_submitted": dict(Counter(job.job_type for job in item.jobs.values())),
                    "active_leases": len(item_leases),
                    "attempts": len(item.attempt_ids),
                    "original_reward": item.original_reward,
                    "repaired_reward": item.repaired_reward,
                    "outcome_status": item.outcome_status,
                    "verified_asset_ids": list(item.asset_ids),
                    "has_trace": item.has_trace,
                    "last_event_type": item.last_event_type,
                    "last_seq": item.last_seq,
                    "updated_at": item.updated_at,
                }
            )
        return {
            "total": len(self._seen),
            "retained": len(recent),
            "truncated": len(self._seen) > len(recent),
            "recent": recent,
        }
