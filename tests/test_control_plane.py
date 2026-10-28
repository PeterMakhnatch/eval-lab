"""Deterministic ASGI checks for live projection, SSE replay, and operator gates."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from evallab.production.control_plane import LiveProjection, create_app

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
TOKEN = "test-operator-capability"
AUTH = {"Authorization": "Bearer " + TOKEN}
SYSTEMS = ("task_quality", "reward_seeker", "misalignment")


class Journal:
    """In-memory test double for the explicitly injected durable queue interface."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self.keys: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.reads: list[tuple[int, int]] = []
        self.stats = {
            "depth": 7,
            "by_system": {"task_quality": 3, "reward_seeker": 2, "misalignment": 2},
            "jobs": {"queued": 7, "leased": 3, "completed": 11},
            "active_workers": {system: [system + "-worker"] for system in SYSTEMS},
        }

    def append_event(self, event: Mapping, *, idempotency_key: str | None = None) -> dict:
        with self.lock:
            if idempotency_key in self.keys:
                return copy.deepcopy(self.keys[idempotency_key])
            stored = {**copy.deepcopy(event), "seq": len(self.events) + 1}
            self.events.append(stored)
            if idempotency_key is not None:
                self.keys[idempotency_key] = stored
            return copy.deepcopy(stored)

    def add(
        self,
        event_type="trace.partial",
        *,
        system="task_quality",
        run_id="run-1",
        event_id=None,
        status="provisional",
        prefix_step=None,
        **payload,
    ):
        return self.append_event(
            {
                "event_id": event_id or f"event-{len(self.events) + 1}",
                "run_id": run_id,
                "system": system,
                "event_type": event_type,
                "seq": 0,
                "ts": NOW.isoformat(),
                "prefix_step": prefix_step,
                "status": status,
                "payload": payload,
            }
        )

    def read_events(self, *, after_seq=0, limit=100):
        with self.lock:
            self.reads.append((after_seq, limit))
            return copy.deepcopy([e for e in self.events if e["seq"] > after_seq][:limit])

    def get_event(self, event_id):
        with self.lock:
            return copy.deepcopy(next((e for e in self.events if e["event_id"] == event_id), None))

    def queue_snapshot(self):
        with self.lock:
            return {**copy.deepcopy(self.stats), "last_seq": len(self.events)}


@pytest.fixture
def journal():
    return Journal()


@pytest.fixture
def app(journal):
    return create_app(journal, operator_tokens={TOKEN: "peter"}, clock=lambda: NOW)


def _post(client, *, action="pause", run_id="run-1", request_id="request-1", headers=AUTH, **extra):
    return client.post(
        "/api/intervene",
        headers=headers,
        json={
            "action": action,
            "run_id": run_id,
            "reason": "Inspect the verifier before continuing",
            "request_id": request_id,
            **extra,
        },
    )


def _stream(app, *, cursor=None, stop: Callable[[str], bool], on_frame=None):
    """Drive the real ASGI endpoint and disconnect after an observed frame.

    TestClient/httpx buffer an infinite SSE body; this transport instead sends an
    actual http.disconnect, without a live server, network, or timing sleeps.
    """

    async def run():
        disconnected = asyncio.Event()
        request_sent = False
        bodies, response = [], {}

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response.update(message)
            elif message["type"] == "http.response.body":
                body = message.get("body", b"").decode()
                bodies.append(body)
                if on_frame is not None:
                    on_frame(body)
                if stop("".join(bodies)):
                    disconnected.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/stream",
            "raw_path": b"/api/stream",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "headers": [(b"last-event-id", cursor.encode())] if cursor is not None else [],
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return response, "".join(bodies)

    return asyncio.run(run())


def _frames(body):
    result = []
    for block in body.split("\n\n"):
        values = {}
        for line in block.splitlines():
            if line.startswith(("id: ", "event: ", "data: ")):
                key, value = line.split(": ", 1)
                values[key] = json.loads(value) if key == "data" else value
        if "data" in values:
            result.append(values)
    return result


def test_dashboard_serves_all_production_parts_without_exposing_operator_token(app):
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for label in (
        "Public Corpus Queue",
        "Three Real-Time Systems",
        "Task Quality",
        "Reward Seeker",
        "Misalignment Monitor",
        "Live Interventions",
        "Verified Asset Store",
    ):
        assert label in response.text
    assert "new EventSource('/api/stream')" in response.text
    assert TOKEN not in response.text
    assert '<body data-home="live">' in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_state_projects_queue_workers_growing_traces_findings_and_distinct_assets(app, journal):
    journal.add("task_quality.started", worker_id="tq-worker")
    journal.add(prefix_step=1, step={"step_id": 1, "message": "first"})
    journal.add(
        prefix_step=2,
        steps=[{"step_id": 1, "message": "first updated"}, {"step_id": 2, "message": "second"}],
    )
    journal.add(system="reward_seeker", prefix_step=1, step={"step_id": 1, "message": "attack"})
    journal.add(
        system="misalignment",
        prefix_step=2,
        partial_trace={"steps": [{"step_id": 2, "message": "observed"}]},
    )
    journal.add("qa.finding/v1", finding_id="finding-1", summary="Verifier bypass", severity="high")
    with TestClient(app) as client:
        provisional = client.get("/api/state").json()
        assert provisional["suspicion_cards"][0]["status"] == "provisional"
        journal.add(
            "qa.finding/v1",
            finding_id="finding-1",
            summary="Replay disproved bypass",
            severity="high",
            status="refuted",
        )
        journal.add("asset.verified", status="confirmed", asset_id="repair-1", kind="repaired_task")
        journal.add(
            "asset.verified",
            status="confirmed",
            asset={"asset_id": "repair-1", "kind": "repaired_task"},
        )
        journal.add(
            "asset.verified",
            system="reward_seeker",
            status="confirmed",
            asset_id="exploit-1",
            kind="exploit_witness",
        )
        journal.add("asset.verified", asset_id="not-verified", kind="labeled_trace")
        state = client.get("/api/state").json()
    assert state["queue"]["depth"] == 7
    assert state["queue"]["jobs"] == {"queued": 7, "leased": 3, "completed": 11}
    assert set(state["systems"]) == set(SYSTEMS)
    assert all(s["active_workers"] == 1 for s in state["systems"].values())
    trace = state["systems"]["task_quality"]["partial_traces"][0]
    assert trace["prefix_step"] == 2
    assert [s["message"] for s in trace["steps"]] == ["first updated", "second"]
    assert len(state["partial_traces"]) == 3  # Same run is independent in each system.
    assert len(state["suspicion_cards"]) == 1
    assert state["suspicion_cards"][0]["status"] == "refuted"
    assert state["verified_assets"]["total"] == 2
    assert state["verified_assets"]["by_kind"] == {"repaired_task": 1, "exploit_witness": 1}
    assert state["last_seq"] == 11
    assert state["cursor"] == "event-11"


def test_queue_lease_snapshot_is_authoritative_over_stale_worker_start(app, journal):
    journal.add("task_quality.started", worker_id="expired-worker")
    journal.stats["active_workers"]["task_quality"] = []
    journal.stats["jobs"] = {"queued": 10, "leased": 0, "completed": 12}
    with TestClient(app) as client:
        state = client.get("/api/state").json()
    assert state["systems"]["task_quality"]["active_workers"] == 0
    assert state["systems"]["task_quality"]["workers"] == []
    assert state["queue"]["jobs"]["leased"] == 0


def test_projection_retention_is_bounded_without_losing_distinct_asset_counts(journal):
    projection = LiveProjection(max_runs=2, max_steps=2, max_cards=2)
    for run in range(3):
        projection.apply(
            journal.add(
                run_id=f"run-{run}", prefix_step=3, steps=[{"step_id": i} for i in (1, 2, 3)]
            )
        )
        projection.apply(
            journal.add("monitor.suspicion", run_id=f"run-{run}", finding_id=f"finding-{run}")
        )
        projection.apply(
            journal.add(
                "asset.verified", status="confirmed", asset_id=f"asset-{run}", kind="labeled_trace"
            )
        )
    projection.apply(
        journal.add("asset.verified", status="confirmed", asset_id="asset-0", kind="labeled_trace")
    )
    state = projection.state(journal.queue_snapshot())
    assert len(state["partial_traces"]) == len(state["suspicion_cards"]) == 2
    assert all([s["step_id"] for s in t["steps"]] == [2, 3] for t in state["partial_traces"])
    assert all(t["truncated"] for t in state["partial_traces"])
    assert state["verified_assets"]["total"] == 3
    assert state["verified_assets"]["by_kind"] == {"labeled_trace": 3}
    assert len(state["verified_assets"]["recent"]) == 2


def test_state_rebuilds_from_journal_after_restart_and_reads_incrementally(journal):
    for _ in range(503):
        journal.add("queue.observed", system="queue")
    journal.add("asset.verified", status="confirmed", asset_id="asset-1", kind="repaired_task")
    with TestClient(create_app(journal)) as client:
        first = client.get("/api/state").json()
        reads = len(journal.reads)
        second = client.get("/api/state").json()
    assert first == second
    assert len(journal.reads) == reads == 2
    with TestClient(create_app(journal)) as client:
        rebuilt = client.get("/api/state").json()
    assert rebuilt == first
    assert rebuilt["last_seq"] == 504
    assert rebuilt["verified_assets"]["total"] == 1


def test_snapshot_cursor_does_not_include_events_appended_during_projection(journal):
    journal.add(event_id="before")
    original_read = journal.read_events
    appended = False

    def racing_read(**kwargs):
        nonlocal appended
        if not appended:
            appended = True
            journal.add(event_id="after", prefix_step=2, step={"step_id": 2})
        return original_read(**kwargs)

    journal.read_events = racing_read
    with TestClient(create_app(journal)) as client:
        first = client.get("/api/state").json()
        second = client.get("/api/state").json()
    assert (first["cursor"], first["last_seq"]) == ("before", 1)
    assert first["partial_traces"] == []
    assert (second["cursor"], second["last_seq"]) == ("after", 2)
    assert second["partial_traces"][0]["prefix_step"] == 2


def test_sse_cursor_resume_uses_journal_sequence_not_event_id_sorting(app, journal):
    journal.add(event_id="z-first")
    journal.add(event_id="a-second")
    journal.add(event_id="m-third")
    response, body = _stream(app, cursor="z-first", stop=lambda b: "id: m-third\n" in b)
    frames = _frames(body)
    assert response["status"] == 200
    headers = dict(response["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"x-accel-buffering"] == b"no"
    assert [f["id"] for f in frames] == ["a-second", "m-third"]
    assert [f["data"]["seq"] for f in frames] == [2, 3]
    assert all("event" not in f for f in frames)
    assert journal.reads[0][0] == 1


def test_sse_initial_snapshot_has_cursor_and_no_snapshot_to_live_gap(app, journal):
    journal.add(event_id="initial")

    def arriving(frame):
        if "event: state\n" in frame:
            journal.add(event_id="arrived-during-snapshot", prefix_step=1, step={"step_id": 1})

    _, body = _stream(app, on_frame=arriving, stop=lambda b: "id: arrived-during-snapshot\n" in b)
    frames = _frames(body)
    assert frames[0]["event"] == "state"
    assert frames[0]["id"] == frames[0]["data"]["cursor"] == "initial"
    assert frames[0]["data"]["last_seq"] == 1
    assert [f["id"] for f in frames] == ["initial", "arrived-during-snapshot"]


def test_sse_resumed_connection_receives_new_live_event_without_replaying_tail(app, journal):
    journal.add(event_id="tail")

    def arriving(frame):
        if frame.startswith(": connected"):
            journal.add(event_id="live")

    _, body = _stream(app, cursor="tail", on_frame=arriving, stop=lambda b: "id: live\n" in b)
    assert [f["id"] for f in _frames(body)] == ["live"]


def test_sse_replay_spans_batches_and_stops_on_disconnect(app, journal):
    journal.add(event_id="resume")
    for i in range(205):
        journal.add(event_id=f"next-{i}")
    _, body = _stream(app, cursor="resume", stop=lambda b: "id: next-204\n" in b)
    assert len(_frames(body)) == 205
    assert _frames(body)[-1]["data"]["seq"] == 206
    assert len(journal.reads) == 3


@pytest.mark.parametrize(("cursor", "expected"), [("unknown", 409), ("", 400), ("x" * 513, 400)])
def test_sse_invalid_or_unknown_cursor_never_silently_restarts(app, cursor, expected):
    with TestClient(app) as client:
        response = client.get("/api/stream", headers={"Last-Event-ID": cursor})
    assert response.status_code == expected


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
def test_operator_interventions_require_authentication(app, journal, headers):
    with TestClient(app) as client:
        response = _post(client, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert journal.events == []


def test_unconfigured_control_gate_fails_closed_but_dashboard_remains_readable(journal):
    with TestClient(create_app(journal)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/state").status_code == 200
        response = _post(client)
    assert response.status_code == 503
    assert journal.events == []


@pytest.mark.parametrize("action", ["pause", "cancel", "quarantine"])
def test_operator_intervention_records_authenticated_audit_and_only_requests_action(
    app, journal, action
):
    journal.add(prefix_step=1, step={"step_id": 1})
    with TestClient(app) as client:
        response = _post(client, action=action)
        state = client.get("/api/state").json()
    assert response.status_code == 202
    result = response.json()
    assert result["state"] == "requested"
    event = result["event"]
    assert event == journal.events[-1]
    assert event["event_type"] == "operator.control_requested"
    assert event["status"] == "provisional"
    assert event["payload"] == {
        "request_id": "request-1",
        "action": action,
        "target": "run-1",
        "operator": "peter",
        "reason": "Inspect the verifier before continuing",
        "state": "requested",
    }
    assert event["ts"] == NOW.isoformat()
    assert TOKEN not in json.dumps(event)
    assert state["interventions"][0]["state"] == "requested"
    assert state["partial_traces"][0]["run_state"] == "running"


def test_intervention_retry_is_idempotent_and_request_id_reuse_conflicts(app, journal):
    with TestClient(app) as client:
        first = _post(client)
        retry = _post(client)
        conflict = _post(client, action="cancel")
    assert first.status_code == retry.status_code == 202
    assert first.json() == retry.json()
    assert conflict.status_code == 409
    assert len(journal.events) == 1


@pytest.mark.parametrize("conflicting", [False, True])
def test_simultaneous_intervention_request_reconciles_the_journal_winner(app, journal, conflicting):
    original_append = journal.append_event

    def another_request_won(event, **kwargs):
        winning = copy.deepcopy(event)
        if conflicting:
            winning["payload"]["action"] = "cancel"
        original_append(winning, **kwargs)
        raise ValueError("journal key already claimed")

    journal.append_event = another_request_won
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == (409 if conflicting else 202)
    assert len(journal.events) == 1


def test_intervention_id_cannot_be_reused_by_a_different_operator(journal):
    app = create_app(
        journal, operator_tokens={TOKEN: "peter", "second-capability": "other"}, clock=lambda: NOW
    )
    with TestClient(app) as client:
        assert _post(client).status_code == 202
        response = _post(client, headers={"Authorization": "Bearer second-capability"})
    assert response.status_code == 409
    assert len(journal.events) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"action": "delete"},
        {"reason": "  "},
        {"run_id": ""},
        {"operator": "forged"},
        {"request_id": "unsafe\ninjected"},
        {"target": "\x00"},
    ],
)
def test_invalid_interventions_never_write_to_journal(app, journal, extra):
    with TestClient(app) as client:
        response = _post(client, **extra)
    assert response.status_code == 422
    assert journal.events == []


def test_intervention_is_not_acknowledged_when_durable_append_fails(app, journal):
    def unavailable(*args, **kwargs):
        raise OSError("test journal unavailable")

    journal.append_event = unavailable
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert journal.events == []
    assert "requested" not in response.json()


@pytest.mark.parametrize(
    ("action", "run_state"),
    [("pause", "paused"), ("cancel", "stopped"), ("quarantine", "quarantined")],
)
def test_run_state_changes_only_after_executor_applied_event(app, journal, action, run_state):
    journal.add(prefix_step=1, step={"step_id": 1})
    with TestClient(app) as client:
        assert _post(client, action=action).status_code == 202
        journal.add(
            "control.acknowledged",
            system="misalignment",
            request_id="request-1",
            executor_id="executor-1",
        )
        acknowledged = client.get("/api/state").json()
        assert acknowledged["interventions"][0]["state"] == "acknowledged"
        assert acknowledged["partial_traces"][0]["run_state"] == "running"
        journal.add(
            "control.applied",
            system="misalignment",
            status="applied",
            request_id="request-1",
            executor_id="executor-1",
            run_state=run_state,
        )
        applied = client.get("/api/state").json()
        assert applied["interventions"][0]["state"] == "applied"
        assert applied["partial_traces"][0]["run_state"] == run_state
        journal.add("control.requested", system="misalignment", request_id="request-1")
        delayed = client.get("/api/state").json()
    assert delayed["interventions"][0]["state"] == "applied"
    assert delayed["partial_traces"][0]["run_state"] == run_state


def test_expired_control_does_not_claim_run_was_stopped(app, journal):
    journal.add(prefix_step=1, step={"step_id": 1})
    with TestClient(app) as client:
        _post(client, action="cancel")
        journal.add("control.expired", system="misalignment", request_id="request-1")
        state = client.get("/api/state").json()
    assert state["interventions"][0]["state"] == "expired"
    assert state["partial_traces"][0]["run_state"] == "running"


def test_factory_requires_explicit_database_before_constructing_queue(monkeypatch):
    import sys
    from types import SimpleNamespace

    from evallab.production.control_plane import app_from_env

    def forbidden(*args, **kwargs):
        pytest.fail("No implicit database/queue may be constructed")

    monkeypatch.setitem(
        sys.modules,
        "evallab.production.corpus_queue",
        SimpleNamespace(ContinuousWorkQueue=forbidden),
    )
    monkeypatch.delenv("EVALLAB_PRODUCTION_DATABASE", raising=False)
    with pytest.raises(ValueError, match="EVALLAB_PRODUCTION_DATABASE"):
        app_from_env()
