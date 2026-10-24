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
"""

from __future__ import annotations

import asyncio
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
        if steps or event.get("prefix_step") is not None:
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
    poll_interval: float = 0.5,
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
    plane = ControlPlane(journal, projection=projection)
    app = FastAPI(title="Eval Lab Live Production", docs_url=None, redoc_url=None)
    app.state.control_plane = plane

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(
            _DASHBOARD,
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

    return app


def app_from_env() -> FastAPI:
    """ASGI factory: an explicit database is required; no default queue is drained."""
    from evallab.production.corpus_queue import ContinuousWorkQueue

    database = os.environ.get("EVALLAB_PRODUCTION_DATABASE")
    if not database:
        raise ValueError("EVALLAB_PRODUCTION_DATABASE is required")
    token = os.environ.get("EVALLAB_OPERATOR_TOKEN")
    operator = os.environ.get("EVALLAB_OPERATOR_ID", "operator")
    return create_app(
        ContinuousWorkQueue(database), operator_tokens={token: operator} if token else {}
    )


_DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eval Lab · Live Production</title>
<style>
:root{color-scheme:dark;--bg:#0b1018;--panel:#131c28;--line:#263449;--text:#edf3fa;--muted:#a3b2c6;--accent:#6ce4b8;--warn:#f1c16d}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif}main{max-width:1480px;margin:auto;padding:32px}header,.row{display:flex;align-items:center;justify-content:space-between;gap:16px}h1{font-size:25px;margin:0}h2{font-size:19px;margin:0 0 16px}h3{font-size:16px;margin:0}p{margin:6px 0;color:var(--muted)}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.12em;font-size:11px;font-weight:700}.status{color:var(--accent)}.disconnected{color:var(--warn)}section{margin-top:24px}.panel,.system{border:1px solid var(--line);border-radius:14px;padding:22px;background:var(--panel)}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.metrics{display:flex;gap:32px;flex-wrap:wrap;margin:18px 0}.metric strong{font-size:30px;display:block}.metric span,.muted{color:var(--muted)}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{border:1px solid var(--line);border-radius:20px;padding:3px 10px;color:var(--muted);font-size:12px}.two{display:grid;grid-template-columns:1.1fr 1fr;gap:16px}.cards{display:grid;gap:10px;margin-top:14px}.card{padding:12px;border-left:2px solid var(--accent);background:#0e1622;overflow-wrap:anywhere}.card.provisional{border-color:var(--warn)}.card.refuted,.card.expired{border-color:var(--muted)}.card strong{font-weight:600}.card small{display:block;color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;color:var(--muted);font:12px/1.5 ui-monospace,monospace}label{display:block;color:var(--muted);font-size:12px;margin-top:10px}input,select,textarea,button{font:inherit;border:1px solid var(--line);border-radius:7px;background:#0e1622;color:var(--text);padding:9px 10px;width:100%;margin-top:4px}button{background:var(--accent);color:#072318;font-weight:700;cursor:pointer}button:disabled{opacity:.5;cursor:wait}textarea{resize:vertical;min-height:70px}#result{min-height:22px;color:var(--warn)}details{margin-top:12px}summary{cursor:pointer;color:var(--muted)}#cursor{font:11px ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere}footer{margin-top:24px;color:var(--muted);font-size:12px}@media(max-width:950px){.grid,.two{grid-template-columns:1fr}main{padding:18px}header{align-items:flex-start}.metrics{gap:20px}}
</style></head><body><main>
<header><div><div class="eyebrow">Always-on production</div><h1>Eval Lab</h1><p>Public data → continuous work → verified assets</p></div><div><div id="connection" class="status" role="status">Connecting to journal…</div><div id="cursor">Waiting for durable cursor</div></div></header>
<section class="panel"><div class="eyebrow">01 Fuel + 02 Engine</div><h2>Public Corpus Queue</h2><div class="chips"><span class="chip">TB4 DEV</span><span class="chip">MiMo 567</span><span class="chip">Terminal Wrench</span><span class="chip">Goodfire</span><span class="chip">CheatBench</span><span class="chip">DEV work only · TEST sealed</span></div><div class="metrics"><div class="metric"><strong id="queue-depth">—</strong><span>Queue depth (queued + leased)</span></div><div class="metric"><strong id="leased">—</strong><span>Leased jobs</span></div><div class="metric"><strong id="completed">—</strong><span>Completed jobs</span></div><div class="metric"><strong id="events">—</strong><span>Journal sequence</span></div></div><p>Durable journal feeds independent workers. Confirmed exploit → task repair → qualified repair → seeker re-break.</p></section>
<section><div class="eyebrow">03 Independent consumers</div><h2>Three Real-Time Systems</h2><div class="grid" id="systems"></div></section>
<section class="two"><div class="panel"><div class="eyebrow">Human control gate</div><h2>Live Interventions</h2><p>Requests are durable. A request is not applied until the executor acknowledges and applies it.</p><form id="intervention"><label for="token">Operator token (kept only in this page)</label><input id="token" type="password" autocomplete="off" required><div class="two"><div><label for="action">Action</label><select id="action"><option value="pause">Pause</option><option value="cancel">Cancel</option><option value="quarantine">Quarantine</option></select></div><div><label for="run-id">Run ID</label><input id="run-id" required maxlength="256"></div></div><label for="reason">Reason</label><textarea id="reason" required maxlength="2000"></textarea><button id="submit" type="submit">Request intervention</button><p id="result" role="status"></p></form><div class="cards" id="controls"></div></div><div class="panel"><div class="eyebrow">04 Output</div><h2>Verified Asset Store</h2><div class="metric"><strong id="asset-total">—</strong><span>Distinct verified assets</span></div><div id="asset-kinds" class="chips"></div><div class="cards" id="assets"></div></div></section>
<footer>All live views are projections of the durable journal. Provisional suspicion is not a confirmed verdict. No execution or paid model calls are launched by opening this page.</footer>
</main><script>
'use strict';
const $ = id => document.getElementById(id);
const names = {task_quality:'Task Quality',reward_seeker:'Reward Seeker',misalignment:'Misalignment Monitor'};
const node = (tag,text,cls) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el;};
function cards(parent,items,render,empty){parent.replaceChildren();if(!items.length){parent.append(node('p',empty));return;}items.slice().reverse().forEach(item=>parent.append(render(item)));}
function card(title,detail,status){const el=node('div',undefined,'card '+(status||''));el.append(node('strong',title),node('small',detail));return el;}
function render(state){
 $('cursor').textContent=state.cursor||'Journal is empty';$('events').textContent=state.last_seq;
 $('queue-depth').textContent=state.queue.depth;$('leased').textContent=state.queue.jobs.leased;$('completed').textContent=state.queue.jobs.completed;
 $('systems').replaceChildren();for(const [key,name] of Object.entries(names)){const data=state.systems[key];const el=node('article',undefined,'system');el.append(node('h3',name),node('p',data.active_workers+' active workers · '+data.partial_traces.length+' live traces'));const findings=node('div',undefined,'cards');cards(findings,data.suspicion_cards.slice(-6),f=>card(f.summary,f.run_id+' · '+f.severity+' · '+f.status,f.status),'No suspicion cards yet.');el.append(findings);for(const trace of data.partial_traces.slice(-4)){const d=node('details');d.append(node('summary',trace.run_id+' · prefix '+(trace.prefix_step??'?')+' · '+trace.run_state));d.append(node('pre',JSON.stringify(trace.steps,null,2)));if(trace.truncated)d.append(node('small','Showing the most recent retained steps.'));el.append(d);}$('systems').append(el);}
 cards($('controls'),state.interventions.slice(-10),c=>card(c.action+' · '+c.state,c.run_id+' · '+(c.operator||'executor')+' · '+(c.reason||''),c.state),'No intervention requests.');
 $('asset-total').textContent=state.verified_assets.total;$('asset-kinds').replaceChildren();for(const [kind,count] of Object.entries(state.verified_assets.by_kind))$('asset-kinds').append(node('span',kind+' · '+count,'chip'));
 cards($('assets'),state.verified_assets.recent.slice(-12),a=>card(a.asset_id,a.kind+' · '+a.run_id,'confirmed'),'Verified assets will appear here as workers qualify them.');
}
let refreshing=false,again=false;
async function refresh(){if(refreshing){again=true;return;}refreshing=true;try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw new Error('State unavailable');render(await r.json());}catch(e){$('connection').textContent=e.message;$('connection').className='disconnected';}finally{refreshing=false;if(again){again=false;refresh();}}}
const stream=new EventSource('/api/stream');
stream.addEventListener('state',event=>render(JSON.parse(event.data)));
stream.onmessage=()=>refresh();
stream.onopen=()=>{$('connection').textContent='Live · connected';$('connection').className='status';};
stream.onerror=()=>{$('connection').textContent='Reconnecting · journal replay enabled';$('connection').className='disconnected';};
window.addEventListener('pagehide',()=>stream.close());
let pending=null;
$('intervention').addEventListener('submit',async event=>{event.preventDefault();const body={action:$('action').value,run_id:$('run-id').value,reason:$('reason').value};const signature=JSON.stringify(body);if(!pending||pending.signature!==signature)pending={signature,id:crypto.randomUUID()};body.request_id=pending.id;$('submit').disabled=true;try{const response=await fetch('/api/intervene',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+$('token').value},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Invalid intervention request');$('result').textContent='Requested '+result.request_id+' · awaiting executor acknowledgement';pending=null;refresh();}catch(error){$('result').textContent=error.message;}finally{$('submit').disabled=false;}});
</script></body></html>"""
