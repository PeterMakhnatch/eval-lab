"""Single production dashboard over the durable, ordered production event journal.

Wire ``create_app(queue, operator_tokens={token: operator_name})`` to the same
ContinuousWorkQueue used by the workers. ``app_from_env`` is the ASGI factory for
EVALLAB_PRODUCTION_DATABASE and EVALLAB_OPERATOR_TOKEN (optional, read-only when
unset). No model calls, queue drains, or database creation occur on module import.

GET /api/stream starts with a ``state`` SSE snapshot, then emits shared event
envelopes as ordinary messages. Reconnect with Last-Event-ID to replay strictly
after the last delivered event; an unknown cursor fails closed with HTTP 409.
GET /api/state rebuilds a bounded live view from journal events and authoritative
queue counters. POST /api/intervene accepts an authenticated, audited request,
not an execution acknowledgement: only control.applied changes a run's state.

GET /api/runs/{run_id}/timeline folds one run's journal events into a causal-prefix
timeline; GET /api/grader-diff compares the original and repaired task packages of a
DEV run or asset (TEST items are refused). GET /api/assets, GET /api/assets/detail and
POST /api/assets/export expose the Verified Asset Store; exports are operator-authenticated
and blocked by the tb4-audit-v1 TEST split-leak guard. Filesystem paths named by events are
only read inside the roots given to the asset store and inspector.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from evallab.production.asset_store import (
    AssetNotFound,
    ExportConflict,
    ExportDisabled,
    SplitLeakError,
    VerifiedAssetStore,
)
from evallab.production.dashboard import dashboard_html
from evallab.production.inspector import AmbiguousRunError, RunInspector
from evallab.production.run_catalog import RunCatalog
from evallab.production.safe_paths import PathOutsideRootsError

SYSTEMS = ("task_quality", "reward_seeker", "misalignment")
_TERMINAL = (".completed", ".finished", ".failed", ".stopped", ".cancelled", ".canceled")
_ASSET_EVENTS = frozenset({"asset.verified", "verified_asset.created", "asset.promoted"})


class EventJournal(Protocol):
    """The synchronous production queue interface; journal order is global seq."""

    def read_events(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...

    def get_event(self, event_id: str) -> dict[str, Any] | None: ...

    def queue_snapshot(self) -> dict[str, Any]: ...

    def append_event(
        self, event: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]: ...


class InterventionRequest(BaseModel):
    """Run-scoped intervention. Caller identity comes from the bearer credential."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "cancel", "quarantine"]
    run_id: str = Field(min_length=1, max_length=256)
    target: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("run_id", "target", "reason", "request_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(c in value for c in "\r\n\x00")):
            raise ValueError("must be nonblank and contain no line breaks or NUL")
        return value


class AssetExportRequest(BaseModel):
    """One-click Verified Asset Store export; the operator comes from the bearer token."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=512)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("asset_id", "request_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(c in value for c in "\r\n\x00")):
            raise ValueError("must be nonblank and contain no line breaks or NUL")
        return value


def _remember(records: OrderedDict, key: str, value: dict, limit: int) -> None:
    records[key] = value
    records.move_to_end(key)
    while len(records) > limit:
        records.popitem(last=False)


class LiveProjection:
    """Rebuildable event projection, with bounded trace/card payload retention.

    Asset identities are retained to count distinct verified assets across replay
    and repeated verification events; only the most recent asset payloads remain
    in the live view. The journal, never this process, is the authority.
    """

    def __init__(self, *, max_runs: int = 100, max_steps: int = 100, max_cards: int = 100):
        if min(max_runs, max_steps, max_cards) < 1:
            raise ValueError("projection limits must be positive")
        self.max_runs, self.max_steps, self.max_cards = max_runs, max_steps, max_cards
        self.last_seq = 0
        self.cursor: str | None = None
        self.workers: dict[str, dict[str, dict]] = {system: {} for system in SYSTEMS}
        self.traces: OrderedDict[str, dict] = OrderedDict()
        self.cards: OrderedDict[str, dict] = OrderedDict()
        self.controls: OrderedDict[str, dict] = OrderedDict()
        self.assets: OrderedDict[str, dict] = OrderedDict()
        self.asset_kinds: dict[str, str] = {}
        self.asset_counts: Counter[str] = Counter()
        self.run_catalog = RunCatalog(max_items=max_runs)

    def apply(self, event: Mapping[str, Any]) -> None:
        seq = int(event["seq"])
        if seq <= self.last_seq:
            return
        system, run_id = str(event["system"]), str(event["run_id"])
        kind, payload = str(event["event_type"]), event["payload"]
        trace_key = f"{system}:{run_id}"
        if system in self.workers:
            worker_id = str(payload.get("worker_id") or run_id)
            if kind.endswith((".started", ".heartbeat")):
                self.workers[system][worker_id] = {
                    "worker_id": worker_id,
                    "run_id": run_id,
                    "updated_at": event["ts"],
                }
            elif kind.endswith(_TERMINAL):
                self.workers[system].pop(worker_id, None)

        steps = payload.get("steps")
        if not isinstance(steps, list):
            partial = payload.get("partial_trace")
            steps = partial.get("steps") if isinstance(partial, dict) else partial
        if not isinstance(steps, list):
            steps = [payload["step"]] if isinstance(payload.get("step"), dict) else []
        if kind in {"seeker.attempt_started", "seeker.grader_feedback"}:
            steps = []
        prefix_only = (
            kind.startswith(("trace.", "monitor.")) and event.get("prefix_step") is not None
        )
        if steps or prefix_only:
            trace = self.traces.get(trace_key) or {
                "run_id": run_id,
                "system": system,
                "prefix_step": None,
                "steps": [],
                "truncated": False,
                "run_state": "running",
            }
            indexed = OrderedDict((step["step_id"], step) for step in trace["steps"])
            for step in steps:
                if not isinstance(step, dict):
                    continue
                step_id = step.get("step_id", step.get("id", event.get("prefix_step")))
                if not isinstance(step_id, (str, int)) or isinstance(step_id, bool):
                    continue
                indexed[step_id] = {**step, "step_id": step_id}
            if len(indexed) > self.max_steps:
                trace["truncated"] = True
            trace["steps"] = list(indexed.values())[-self.max_steps :]
            prefix = event.get("prefix_step")
            if isinstance(prefix, int) and not isinstance(prefix, bool):
                trace["prefix_step"] = max(prefix, trace["prefix_step"] or 0)
            # Provenance is shown to operators so recorded replays never pass as live runs.
            trace["source_kind"] = trace.get("source_kind") or payload.get("source_kind")
            trace["updated_at"] = event["ts"]
            _remember(self.traces, trace_key, trace, self.max_runs)
        if trace_key in self.traces and kind.endswith(_TERMINAL):
            self.traces[trace_key]["run_state"] = kind.rsplit(".", 1)[-1]

        finding = payload.get("finding")
        finding = finding if isinstance(finding, dict) else {}
        if finding or any(word in kind for word in ("finding", "suspicion", "exploit_confirmed")):
            finding_id = str(
                payload.get("finding_id") or finding.get("finding_id") or f"{run_id}:{kind}"
            )
            card = {
                "finding_id": finding_id,
                "run_id": run_id,
                "system": system,
                "summary": payload.get("summary")
                or finding.get("summary")
                or payload.get("reason")
                or kind,
                "severity": payload.get("severity") or finding.get("severity") or "unknown",
                "status": event["status"],
                "prefix_step": event.get("prefix_step"),
                "event_id": event["event_id"],
                "updated_at": event["ts"],
                "finding": finding,
            }
            _remember(self.cards, f"{system}:{run_id}:{finding_id}", card, self.max_cards)

        if kind == "operator.control_requested" or kind.startswith("control."):
            request_id = str(payload.get("request_id") or event["event_id"])
            previous = self.controls.get(request_id, {})
            state = (
                "requested"
                if kind == "operator.control_requested"
                else kind.removeprefix("control.")
            )
            # A delayed delivery must not turn an applied/expired request back into a request.
            if previous.get("state") not in {"applied", "expired"} or state == previous.get(
                "state"
            ):
                control = {
                    **previous,
                    **payload,
                    "request_id": request_id,
                    "run_id": run_id,
                    "state": state,
                    "event_id": event["event_id"],
                    "updated_at": event["ts"],
                }
                _remember(self.controls, request_id, control, self.max_cards)
                if state == "applied":
                    for trace in self.traces.values():
                        if trace["run_id"] == run_id:
                            trace["run_state"] = payload.get("run_state") or {
                                "pause": "paused",
                                "cancel": "stopped",
                                "quarantine": "quarantined",
                            }.get(control.get("action"), "running")

        if kind in _ASSET_EVENTS and event["status"] in {"confirmed", "applied"}:
            asset = payload.get("asset")
            asset = asset if isinstance(asset, dict) else payload
            asset_id = asset.get("asset_id")
            if asset_id:
                asset_id = str(asset_id)
                asset_kind = str(asset.get("kind") or asset.get("asset_type") or system)
                old_kind = self.asset_kinds.get(asset_id)
                if old_kind != asset_kind:
                    if old_kind is not None:
                        self.asset_counts[old_kind] -= 1
                    self.asset_counts[asset_kind] += 1
                    self.asset_kinds[asset_id] = asset_kind
                _remember(
                    self.assets,
                    asset_id,
                    {
                        **asset,
                        "asset_id": asset_id,
                        "kind": asset_kind,
                        "run_id": run_id,
                        "event_id": event["event_id"],
                        "verified_at": event["ts"],
                    },
                    self.max_cards,
                )
        self.run_catalog.apply(event)
        self.last_seq, self.cursor = seq, str(event["event_id"])

    def state(self, queue: Mapping[str, Any]) -> dict[str, Any]:
        traces, cards = list(self.traces.values()), list(self.cards.values())
        systems = {}
        authoritative_workers = queue.get("active_workers", {})
        for system in SYSTEMS:
            workers = list(self.workers[system].values())
            count = len(workers)
            if system in authoritative_workers:
                live = authoritative_workers[system]
                if isinstance(live, int):
                    count = live
                elif isinstance(live, list):
                    count = len(live)
                    workers = [w if isinstance(w, dict) else {"worker_id": w} for w in live]
            systems[system] = {
                "active_workers": count,
                "workers": workers,
                "partial_traces": [t for t in traces if t["system"] == system],
                "suspicion_cards": [c for c in cards if c["system"] == system],
            }
        return {
            "cursor": self.cursor,
            "last_seq": self.last_seq,
            "queue": dict(queue),
            "systems": systems,
            "run_items": self.run_catalog.state(queue),
            "partial_traces": traces,
            "suspicion_cards": cards,
            "interventions": list(self.controls.values()),
            "verified_assets": {
                "total": len(self.asset_kinds),
                "by_kind": {k: v for k, v in self.asset_counts.items() if v},
                "recent": list(self.assets.values()),
            },
        }


class ControlPlane:
    def __init__(self, journal: EventJournal, *, projection: LiveProjection | None = None):
        self.journal = journal
        self.projection = projection or LiveProjection()
        self.lock = threading.Lock()

    def state(self) -> dict[str, Any]:
        with self.lock:
            snapshot = self.journal.queue_snapshot()
            high_water = int(snapshot["last_seq"])
            while self.projection.last_seq < high_water:
                events = self.journal.read_events(after_seq=self.projection.last_seq, limit=500)
                if not events:
                    raise RuntimeError("journal snapshot is ahead of readable events")
                previous = self.projection.last_seq
                for event in events:
                    if int(event["seq"]) > high_water:
                        break
                    self.projection.apply(event)
                if self.projection.last_seq == previous:
                    raise RuntimeError("journal did not advance to its snapshot cursor")
            # Projection rows must not mutate while a response is being serialized.
            return json.loads(json.dumps(self.projection.state(snapshot)))


def _sse(data: Mapping[str, Any], *, event: str | None = None, event_id: str | None = None) -> str:
    lines = []
    if event_id is not None:
        if any(c in event_id for c in "\r\n\x00"):
            raise ValueError("journal event_id is not a valid SSE cursor")
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def create_app(
    journal: EventJournal,
    *,
    operator_tokens: Mapping[str, str] | None = None,
    projection: LiveProjection | None = None,
    asset_store: VerifiedAssetStore | None = None,
    inspector: RunInspector | None = None,
    poll_interval: float = 0.5,
    home: str = "live",
    heartbeat_interval: float = 15.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Build the production ASGI app without starting workers or spending money.

    Tokens map to server-owned operator identities and are never sent to the UI.
    An empty mapping leaves the live dashboard readable and interventions disabled.
    Synchronous journal I/O runs in worker threads, not on the SSE event loop.
    """
    if poll_interval <= 0 or heartbeat_interval <= 0:
        raise ValueError("stream intervals must be positive")
    tokens = dict(operator_tokens or {})
    if any(not token or not operator.strip() for token, operator in tokens.items()):
        raise ValueError("operator tokens and identities must be nonempty")
    page = dashboard_html(home)
    plane = ControlPlane(journal, projection=projection)
    app = FastAPI(title="Eval Lab Live Production", docs_url=None, redoc_url=None)
    app.state.control_plane = plane

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            },
        )

    @app.get("/api/state")
    def state():
        return plane.state()

    @app.get("/api/stream")
    async def stream(request: Request):
        cursor = request.headers.get("last-event-id")
        initial = None
        if cursor is not None:
            if not cursor or len(cursor) > 512 or any(c in cursor for c in "\r\n\x00"):
                raise HTTPException(400, "Invalid Last-Event-ID")
            previous = await asyncio.to_thread(journal.get_event, cursor)
            if previous is None:
                raise HTTPException(
                    409, "Unknown Last-Event-ID; reload /api/state before reconnecting"
                )
            after_seq = int(previous["seq"])
        else:
            initial = await asyncio.to_thread(plane.state)
            after_seq = int(initial["last_seq"])

        async def events():
            nonlocal after_seq
            yield ": connected\nretry: 1000\n\n"
            if initial is not None:
                yield _sse(initial, event="state", event_id=initial["cursor"])
            heartbeat_at = time.monotonic()
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(journal.read_events, after_seq=after_seq, limit=100)
                for envelope in batch:
                    if await request.is_disconnected():
                        return
                    yield _sse(envelope, event_id=str(envelope["event_id"]))
                    after_seq = int(envelope["seq"])
                if batch:
                    heartbeat_at = time.monotonic()
                    continue
                if time.monotonic() - heartbeat_at >= heartbeat_interval:
                    yield ": keep-alive\n\n"
                    yield _sse(await asyncio.to_thread(plane.state), event="state")
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def operator_identity(request: Request) -> str:
        if not tokens:
            raise HTTPException(503, "Operator interventions are disabled")
        authorization = request.headers.get("authorization", "").encode()
        identity = None
        for token, operator in tokens.items():
            if hmac.compare_digest(authorization, ("Bearer " + token).encode()):
                identity = operator
        if identity is None:
            raise HTTPException(
                401, "Operator authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        return identity

    @app.post("/api/intervene", status_code=202)
    def intervene(body: InterventionRequest, request: Request):
        operator = operator_identity(request)
        request_id = body.request_id or str(uuid4())
        event_id = f"operator-control:{request_id}"
        payload = {
            "request_id": request_id,
            "action": body.action,
            "target": body.target or body.run_id,
            "reason": body.reason,
            "operator": operator,
            "state": "requested",
        }
        event = {
            "event_id": event_id,
            "run_id": body.run_id,
            "system": "queue",
            "event_type": "operator.control_requested",
            "seq": 0,
            "ts": clock().isoformat(),
            "prefix_step": None,
            "status": "provisional",
            "payload": payload,
        }
        try:
            existing = journal.get_event(event_id)
            if existing is None:
                try:
                    existing = journal.append_event(event, idempotency_key=event_id)
                except ValueError:
                    # A simultaneous retry may have won the journal's unique key.
                    existing = journal.get_event(event_id)
                    if existing is None:
                        raise
        except OSError as error:
            raise HTTPException(503, "Intervention could not be durably recorded") from error
        if (
            existing["event_type"] != event["event_type"]
            or existing["run_id"] != body.run_id
            or existing["payload"] != payload
        ):
            raise HTTPException(409, "request_id already belongs to a different intervention")
        return {"request_id": request_id, "state": "requested", "event": existing}

    def inspector_or_503() -> RunInspector:
        if inspector is None:
            raise HTTPException(503, "Run inspector is not configured")
        return inspector

    def store_or_503() -> VerifiedAssetStore:
        if asset_store is None:
            raise HTTPException(503, "Verified Asset Store is not configured")
        return asset_store

    def task_selector(dataset_id: str | None, item_id: str | None) -> None:
        if (dataset_id is None) != (item_id is None):
            raise HTTPException(400, "Pass dataset_id and item_id together")
        if dataset_id is not None and (
            not dataset_id.strip() or not item_id or not item_id.strip()
        ):
            raise HTTPException(400, "Task selectors must be nonblank")

    @app.get("/api/runs/{run_id}/timeline")
    def run_timeline(run_id: str, dataset_id: str | None = None, item_id: str | None = None):
        task_selector(dataset_id, item_id)
        try:
            return inspector_or_503().timeline(run_id, dataset_id=dataset_id, item_id=item_id)
        except AmbiguousRunError as error:
            raise HTTPException(409, {"code": "ambiguous_run", "message": str(error)}) from error
        except LookupError as error:
            raise HTTPException(404, "Unknown run or task item") from error
        except PermissionError as error:
            raise HTTPException(403, {"code": "split_sealed", "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(
                400,
                {
                    "code": "invalid_selection",
                    "message": "Inspection requires an unambiguous task selection",
                },
            ) from error

    @app.get("/api/grader-diff")
    def grader_diff(
        run_id: str | None = None,
        asset_id: str | None = None,
        dataset_id: str | None = None,
        item_id: str | None = None,
    ):
        if (run_id is None) == (asset_id is None):
            raise HTTPException(400, "Pass exactly one of run_id or asset_id")
        task_selector(dataset_id, item_id)
        if asset_id is not None and dataset_id is not None:
            raise HTTPException(400, "An asset already identifies its task item")
        try:
            return inspector_or_503().grader_diff(
                run_id=run_id, asset_id=asset_id, dataset_id=dataset_id, item_id=item_id
            )
        except AmbiguousRunError as error:
            raise HTTPException(409, {"code": "ambiguous_run", "message": str(error)}) from error
        except LookupError as error:
            raise HTTPException(
                404, "No original/repaired grader pair for this task item"
            ) from error
        except PathOutsideRootsError as error:
            raise HTTPException(
                403, {"code": "path_outside_roots", "message": "Task path is outside allowed roots"}
            ) from error
        except PermissionError as error:
            raise HTTPException(403, {"code": "split_sealed", "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(
                400,
                {
                    "code": "invalid_selection",
                    "message": "Inspection requires an unambiguous task selection",
                },
            ) from error

    @app.get("/api/assets")
    def assets():
        return store_or_503().list_assets()

    @app.get("/api/assets/detail")
    def asset_detail(asset_id: str):
        try:
            return store_or_503().get_asset(asset_id)
        except AssetNotFound as error:
            raise HTTPException(404, "Unknown asset") from error

    @app.post("/api/assets/export")
    def export_asset(body: AssetExportRequest, request: Request):
        operator = operator_identity(request)
        store = store_or_503()
        request_id = body.request_id or str(uuid4())
        try:
            asset = store.get_asset(body.asset_id)
        except AssetNotFound as error:
            raise HTTPException(404, "Unknown asset") from error

        def audit_blocked(reasons: list[str]) -> None:
            # A blocked request is client-controlled: ids may themselves be sealed identifiers,
            # so the append-only journal records only opaque references and reason codes.
            # The store itself journals successful exports (asset.exported).
            def ref(value: str) -> str:
                return hashlib.sha256(value.encode()).hexdigest()[:16]

            event_id = f"asset-export:{ref(request_id)}:{ref(body.asset_id)}:blocked"
            event = {
                "event_id": event_id,
                "run_id": "asset-export",
                "system": "queue",
                "event_type": "asset.export_blocked",
                "seq": 0,
                "ts": clock().isoformat(),
                "prefix_step": None,
                "status": "refuted",
                "payload": {
                    "asset_ref": ref(body.asset_id),
                    "kind": asset["kind"],
                    "request_ref": ref(request_id),
                    "operator": operator,
                    "reasons": reasons,
                },
            }
            try:
                journal.append_event(event, idempotency_key=event_id)
            except OSError as error:
                raise HTTPException(503, "Export could not be durably audited") from error
            except ValueError as error:
                raise HTTPException(
                    409, "request_id already belongs to a different asset export"
                ) from error

        try:
            result = store.export(body.asset_id, operator=operator, request_id=request_id)
        except ExportDisabled as error:
            raise HTTPException(
                503, {"code": "export_disabled", "message": "No export root is configured"}
            ) from error
        except SplitLeakError as error:
            audit_blocked(sorted({str(m["reason"]) for m in error.matches}))
            # Operators see which field tripped the guard and why, never the sealed value.
            raise HTTPException(
                409,
                {
                    "code": "split_leak",
                    "message": str(error),
                    "matches": [
                        {"field": m["field"], "reason": m["reason"]} for m in error.matches
                    ],
                },
            ) from error
        except PathOutsideRootsError as error:
            raise HTTPException(
                403,
                {"code": "path_outside_roots", "message": "Asset path is outside allowed roots"},
            ) from error
        except ExportConflict as error:
            raise HTTPException(409, {"code": "request_conflict", "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(422, {"code": "export_invalid", "message": str(error)}) from error
        return result

    return app


def app_from_env() -> FastAPI:
    """ASGI factory: an explicit database is required; no default queue is drained.

    EVALLAB_ASSET_EXPORT_ROOT enables Verified Asset Store exports (otherwise the store is
    browse-only). EVALLAB_ALLOWED_ROOTS (os.pathsep-separated) adds directories whose task
    packages, trial directories and exploit bundles the inspector and store may read; the
    repository root is always allowed.
    """
    from pathlib import Path

    from evallab.production.corpus_queue import ContinuousWorkQueue

    database = os.environ.get("EVALLAB_PRODUCTION_DATABASE")
    if not database:
        raise ValueError("EVALLAB_PRODUCTION_DATABASE is required")
    token = os.environ.get("EVALLAB_OPERATOR_TOKEN")
    operator = os.environ.get("EVALLAB_OPERATOR_ID", "operator")
    export_root = os.environ.get("EVALLAB_ASSET_EXPORT_ROOT")
    queue = ContinuousWorkQueue(database)
    roots = [queue.registry.repo_root]
    roots += [Path(p) for p in os.environ.get("EVALLAB_ALLOWED_ROOTS", "").split(os.pathsep) if p]
    return create_app(
        queue,
        operator_tokens={token: operator} if token else {},
        asset_store=VerifiedAssetStore(
            queue,
            queue.registry,
            export_root=Path(export_root) if export_root else None,
            allowed_roots=roots,
        ),
        inspector=RunInspector(queue, queue.registry, allowed_roots=roots),
    )
