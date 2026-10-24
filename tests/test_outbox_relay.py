"""Behavioral contracts for the production outbox relay.

Everything is real: ``ContinuousWorkQueue`` on SQLite against a synthetic DEV
corpus, the local JetStream adapter, a fake broker speaking the actual NATS
TCP wire protocol on loopback, and an HTTP relay gateway. No heldout corpus
content is ever inspected; sealed envelopes are injected as raw journal rows
or admitted with the fixture's explicit frozen-eval token.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
from evallab.production.outbox_relay import (
    DrainReport,
    HttpGatewayTransport,
    LocalJetStreamAdapter,
    NatsTcpTransport,
    OutboxRelay,
    PermanentPublishError,
    PoisonQuarantine,
    TransientBrokerError,
    route_subject,
)

DATASET = "tb4-audit-v1"
FROZEN_TOKEN = "test-only-explicit-frozen-evaluation"
DEV = {"dataset_id": DATASET, "item_id": "finding-dev"}
SEALED = {"dataset_id": DATASET, "item_id": "finding-test"}
TS = "2026-10-24T00:00:00+00:00"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _item(item_id: str, split: str, task: str, digest: str | None = None) -> dict:
    return {
        "item_id": item_id,
        "split": split,
        "subject": {"task_id": task, "task_digest": digest},
    }


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    _write_jsonl(
        keys / DATASET / "items.jsonl",
        [
            _item("finding-dev", "dev", "terminal-bench/dev-task", "sha256:dev"),
            _item("finding-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / DATASET / "tasks.jsonl",
        [
            _item("ignored-dev", "dev", "terminal-bench/dev-task", "sha256:dev"),
            _item("ignored-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / "cheatbench-actions-v1/items.jsonl",
        [_item("cheat-dev", "dev", "cheatbench/independent")],
    )
    _write_jsonl(
        keys / "hacktrace-act-v1/items.jsonl",
        [_item("hack-dev", "dev", "hacktrace/independent")],
    )
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
                "tasks": [
                    {
                        "record_id": "mimo-dev@sha256:version1",
                        "task_id": "mimo-dev",
                        "package_digest": "sha256:mimo-dev",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def registry(corpus_root: Path) -> CorpusRegistry:
    return CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)


@pytest.fixture
def queue(tmp_path: Path, registry: CorpusRegistry) -> Iterator[ContinuousWorkQueue]:
    with ContinuousWorkQueue(tmp_path / "production.sqlite", registry=registry) as result:
        yield result


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[LocalJetStreamAdapter]:
    with LocalJetStreamAdapter(tmp_path / "broker.sqlite") as result:
        yield result


_SYSTEMS = {
    "seeker": "reward_seeker",
    "worker": "reward_seeker",
    "task_quality": "task_quality",
    "monitor": "misalignment",
    "control": "misalignment",
    "executor": "misalignment",
    "trace": "misalignment",
    "run": "misalignment",
    "operator": "queue",
    "asset": "reward_seeker",
    "verified_asset": "task_quality",
    "reward_seeker": "queue",
    "queue": "queue",
}


def _event(
    event_type: str,
    payload: dict | None = None,
    *,
    system: str | None = None,
    status: str = "confirmed",
    run_id: str = "run-1",
    event_id: str | None = None,
) -> dict[str, Any]:
    if system is None:
        system = _SYSTEMS[event_type.partition(".")[0]]
    return {
        "event_id": event_id or str(uuid4()),
        "run_id": run_id,
        "system": system,
        "event_type": event_type,
        "seq": 0,
        "ts": TS,
        "prefix_step": None,
        "status": status,
        "payload": dict(payload or {}),
    }


def _insert_pending(
    queue: ContinuousWorkQueue, envelope: Any, *, event_id: str | None = None
) -> int:
    """Journal an envelope row directly, bypassing admission normalization.

    Used for corruption/malformed cases the queue's own validation would never
    produce; the row still lands in ``pending_outbox`` like any committed event.
    """
    with queue._transaction(write=True):
        seq = queue._execute(
            "UPDATE queue_state SET last_seq = last_seq + 1 WHERE singleton = 1 RETURNING last_seq"
        ).fetchone()[0]
        if isinstance(envelope, dict):
            envelope = {**envelope, "seq": envelope.get("seq", seq)}
        row_id = (
            event_id
            or (envelope.get("event_id") if isinstance(envelope, Mapping) else None)
            or f"generated-{seq}"
        )
        queue._execute(
            "INSERT INTO events(seq, event_id, idempotency_key, content_hash, run_id, system, event_type, envelope) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                row_id,
                None,
                "synthetic",
                str(envelope.get("run_id", "run")) if isinstance(envelope, Mapping) else "run",
                str(envelope.get("system", "queue")) if isinstance(envelope, Mapping) else "queue",
                str(envelope.get("event_type", "x")) if isinstance(envelope, Mapping) else "x",
                json.dumps(envelope),
            ),
        )
        queue._execute("INSERT INTO outbox(event_id, seq) VALUES (?, ?)", (row_id, seq))
        return seq


class _RecordingQueue:
    """Delegates to the real queue and records mark order; no behavior is faked."""

    def __init__(self, queue: ContinuousWorkQueue) -> None:
        self.queue = queue
        self.calls: list[tuple[str, str]] = []

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.queue.pending_outbox(limit=limit)

    def mark_published(self, event_id: str) -> None:
        self.calls.append(("mark", event_id))
        self.queue.mark_published(event_id)


class _RecordingPublisher:
    """Delegates to the real adapter and records publish order."""

    def __init__(self, adapter: LocalJetStreamAdapter) -> None:
        self.adapter = adapter
        self.calls: list[tuple[str, str]] = []

    def publish(self, subject: str, msg_id: str, payload: bytes):
        self.calls.append(("publish", msg_id))
        return self.adapter.publish(subject, msg_id, payload)

    def close(self) -> None:
        self.adapter.close()


class _ConnReader:
    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buf = b""

    def _recv(self) -> bytes:
        chunk = self.conn.recv(65536)
        if not chunk:
            raise ConnectionError("client closed")
        return chunk

    def line(self) -> bytes:
        while b"\r\n" not in self.buf:
            self.buf += self._recv()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def exactly(self, count: int) -> bytes:
        while len(self.buf) < count:
            self.buf += self._recv()
        data, self.buf = self.buf[:count], self.buf[count:]
        return data


class _FakeNatsBroker:
    """A real TCP server speaking the NATS wire protocol on loopback.

    Sends INFO, answers PING/PONG, accepts SUB, and answers each HPUB with a
    real ``MSG`` frame carrying a JetStream pub-ack JSON body on the reply
    inbox. ``err_subjects`` reply ``-ERR``, ``silent_subjects`` never reply
    (driving client timeouts), and ``drop_subjects`` hang up mid-request.
    ``Nats-Msg-Id`` headers are deduplicated like JetStream.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.2)
        self.port = int(self._listener.getsockname()[1])
        self.lock = threading.Lock()
        self.seen: dict[str, int] = {}
        self.published: list[dict[str, Any]] = []
        self.ops: list[str] = []
        self.err_subjects: set[str] = set()
        self.silent_subjects: set[str] = set()
        self.drop_subjects: set[str] = set()
        self.raw_replies: dict[str, bytes] = {}
        self._seq = 0
        self._stop = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._accept_thread.join(timeout=2)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.sendall(
                b'INFO {"server_id":"fake","version":"2.11.4","headers":true}\r\n'
            )
            reader = _ConnReader(conn)
            while True:
                line = reader.line()
                op = line.split(b" ", 1)[0]
                if op == b"CONNECT":
                    with self.lock:
                        self.ops.append("CONNECT")
                elif op == b"PING":
                    with self.lock:
                        self.ops.append("PING")
                    conn.sendall(b"PONG\r\n")
                elif op == b"PONG":
                    continue
                elif op == b"SUB":
                    with self.lock:
                        self.ops.append(line.decode())
                elif op == b"UNSUB":
                    continue
                elif op in (b"PUB", b"HPUB"):
                    self._publish(conn, reader, line, has_headers=op == b"HPUB")
                else:
                    conn.sendall(b"-ERR 'Unknown Protocol Operation'\r\n")
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    def _publish(
        self, conn: socket.socket, reader: _ConnReader, line: bytes, *, has_headers: bool
    ) -> None:
        parts = line.split(b" ")
        subject = parts[1].decode()
        if has_headers:
            header_len, total_len = int(parts[-2]), int(parts[-1])
            blob = reader.exactly(total_len + 2)[:-2]
            header, body = blob[:header_len], blob[header_len:]
        else:
            total_len = int(parts[-1])
            header, body = b"", reader.exactly(total_len + 2)[:-2]
        reply_index = 4 if has_headers else 3
        reply = parts[2].decode() if len(parts) > reply_index else ""
        msg_id = None
        for header_line in header.split(b"\r\n"):
            if header_line.lower().startswith(b"nats-msg-id:"):
                msg_id = header_line.split(b":", 1)[1].strip().decode()
        with self.lock:
            self.published.append(
                {"subject": subject, "reply": reply, "msg_id": msg_id, "body": body}
            )
            self.ops.append(f"HPUB {subject}")
        if subject in self.drop_subjects:
            conn.close()
            raise ConnectionError("dropping per test configuration")
        if subject in self.silent_subjects or not reply:
            return
        if subject in self.raw_replies:
            conn.sendall(self.raw_replies[subject].replace(b"{REPLY}", reply.encode()))
            return
        if subject in self.err_subjects:
            conn.sendall(b"-ERR 'Permissions Violation for Publish'\r\n")
            return
        with self.lock:
            if msg_id is not None and msg_id in self.seen:
                seq, duplicate = self.seen[msg_id], True
            else:
                self._seq += 1
                seq, duplicate = self._seq, False
                if msg_id is not None:
                    self.seen[msg_id] = seq
            ack = json.dumps(
                {"stream": "FAKE", "seq": seq, "duplicate": duplicate}
            ).encode()
            conn.sendall(f"MSG {reply} 1 {len(ack)}\r\n".encode() + ack + b"\r\n")


@pytest.fixture
def broker() -> Iterator[_FakeNatsBroker]:
    result = _FakeNatsBroker()
    try:
        yield result
    finally:
        result.close()


def _gateway(handler_state: dict[str, Any]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            handler_state["received"].append(
                {
                    "subject": self.headers.get("X-Subject"),
                    "msg_id": self.headers.get("Nats-Msg-Id"),
                    "body": self.rfile.read(length),
                }
            )
            behavior = handler_state["behavior"]
            if behavior == "reject":
                code, payload = 400, b'{"error":"rejected by gateway"}'
            elif behavior == "flaky":
                code, payload = 500, b"upstream unavailable"
            elif behavior == "noack":
                code, payload = 200, b'{"ok":true}'
            elif "ack_body" in handler_state:
                code, payload = 200, handler_state["ack_body"]
            else:
                code, payload = 200, json.dumps(
                    {
                        "stream": "GW",
                        "seq": len(handler_state["received"]),
                        "duplicate": False,
                    }
                ).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def gateway() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"behavior": "ack", "received": []}
    server = _gateway(state)
    try:
        yield f"http://127.0.0.1:{server.server_port}/jetstream", state
    finally:
        server.shutdown()
        server.server_close()


def test_route_subject_explicit_families_and_fallback() -> None:
    cases = [
        ("trace.step", "misalignment", "evallab.capture.misalignment"),
        ("run.snapshot", "misalignment", "evallab.capture.misalignment"),
        ("monitor.prefix_scored", "misalignment", "evallab.capture.misalignment"),
        ("monitor.coverage_gap", "misalignment", "evallab.capture.misalignment"),
        ("monitor.triage_unavailable", "misalignment", "evallab.capture.misalignment"),
        ("monitor.gate_decided", "misalignment", "evallab.capture.misalignment"),
        ("seeker.attempt_started", "reward_seeker", "evallab.capture.reward_seeker"),
        ("seeker.grader_feedback", "reward_seeker", "evallab.capture.reward_seeker"),
        ("monitor.suspicion", "misalignment", "evallab.findings.misalignment"),
        ("seeker.exploit_confirmed", "reward_seeker", "evallab.findings.reward_seeker"),
        ("seeker.job_refused", "reward_seeker", "evallab.findings.reward_seeker"),
        ("task_quality.failed", "task_quality", "evallab.findings.task_quality"),
        ("task_quality.repair_qualified", "task_quality", "evallab.findings.task_quality"),
        ("task_quality.repair_rejected", "task_quality", "evallab.findings.task_quality"),
        ("task_quality.repair_unavailable", "task_quality", "evallab.findings.task_quality"),
        ("control.requested", "misalignment", "evallab.control.misalignment"),
        ("control.applied", "misalignment", "evallab.control.misalignment"),
        ("executor.control_applied", "misalignment", "evallab.control.misalignment"),
        ("operator.control_requested", "queue", "evallab.control.queue"),
        ("worker.started", "reward_seeker", "evallab.control.reward_seeker"),
        ("worker.failed", "reward_seeker", "evallab.control.reward_seeker"),
        ("task_quality.repair_task", "queue", "evallab.control.queue"),
        ("task_quality.audit_task", "queue", "evallab.control.queue"),
        ("reward_seeker.rebreak_task", "queue", "evallab.control.queue"),
        ("reward_seeker.seek_task", "queue", "evallab.control.queue"),
        ("vendor.future_event", "queue", "evallab.capture.queue"),
    ]
    for event_type, system, expected in cases:
        assert route_subject(_event(event_type, system=system)) == expected, event_type
    assert (
        route_subject(_event("asset.verified", {"kind": "exploit_witness"}))
        == "evallab.assets.exploit_witness"
    )
    assert (
        route_subject(_event("verified_asset.created", {"asset_type": "labeled_trace"}))
        == "evallab.assets.labeled_trace"
    )
    assert (
        route_subject(_event("asset.promoted", {"asset": {"kind": "repaired_task"}}))
        == "evallab.assets.repaired_task"
    )
    assert route_subject(_event("asset.verified", {})) == "evallab.assets.unknown"


def test_route_subject_rejects_injection_and_malformed() -> None:
    for payload in ({"kind": "bad.kind"}, {"kind": "evil*"}, {"kind": "a>b"}):
        with pytest.raises(ValueError, match="subject token"):
            route_subject(_event("asset.verified", payload))
    with pytest.raises(ValueError, match="system"):
        route_subject(_event("trace.step", system="evil.injected"))
    with pytest.raises(ValueError, match="event_type"):
        route_subject(_event("bad..dots", system="queue"))
    with pytest.raises(ValueError, match="event_type"):
        route_subject(_event("trace.*", system="misalignment"))
    with pytest.raises(ValueError, match="heldout|TEST"):
        route_subject(_event("task_quality.observed", {"split": "heldout"}, system="task_quality"))


def test_drain_publishes_complete_envelope_then_marks(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    recording_queue = _RecordingQueue(queue)
    recording_publisher = _RecordingPublisher(adapter)
    event = queue.append_event(
        _event(
            "seeker.exploit_confirmed",
            {**DEV, "upstream_reward": 1.0, "repaired_reward": 0.0},
        )
    )
    relay = OutboxRelay(recording_queue, recording_publisher, quarantine=adapter)
    report = relay.drain()
    assert isinstance(report, DrainReport)
    assert event["event_id"] in report.published
    # The chained repair job is published in the same drain under control.queue.
    assert len(report.published) == 2
    assert recording_publisher.calls[0] == ("publish", event["event_id"])
    assert recording_queue.calls[0] == ("mark", event["event_id"])
    assert queue.pending_outbox() == []
    stored = adapter.replay_subject("evallab.findings.reward_seeker")
    assert [message.envelope for message in stored] == [event]
    chained = adapter.replay_subject("evallab.control.queue")
    assert [message.envelope["event_type"] for message in chained] == [
        "task_quality.repair_task"
    ]


def test_all_subject_families_end_to_end(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    queue.append_event(_event("trace.step", {"step": {"step_id": 1}}))
    queue.append_event(_event("monitor.suspicion", {"finding_id": "f1"}))
    queue.append_event(_event("control.applied", {"request_id": "r1"}))
    queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    queue.append_event(_event("asset.verified", {"kind": "repaired_task", "asset_id": "a1"}))
    queue.append_event(_event("vendor.future_event", system="queue"))
    report = OutboxRelay(queue, adapter).drain()
    assert len(report.published) == 6
    families = {
        "evallab.capture.misalignment": 1,
        "evallab.findings.misalignment": 1,
        "evallab.control.misalignment": 1,
        "evallab.control.reward_seeker": 1,
        "evallab.assets.repaired_task": 1,
        "evallab.capture.queue": 1,
    }
    for subject, count in families.items():
        assert len(adapter.replay_subject(subject)) == count, subject
    assert len(adapter.replay_subject("evallab.>")) == 6


def test_crash_after_ack_republishes_and_dedupes(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    event = queue.append_event(_event("task_quality.failed", DEV))
    subject = route_subject(event)
    payload = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    first = adapter.publish(subject, event["event_id"], payload)
    assert not first.duplicate  # a previous relay crashed after this ack
    report = OutboxRelay(queue, adapter).drain()
    assert report.duplicates == (event["event_id"],)
    assert queue.pending_outbox() == []
    assert len(adapter.replay_subject("evallab.>")) == 1


def test_repeat_drains_are_idempotent(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    relay = OutboxRelay(queue, adapter)
    queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    first = relay.drain()
    second = relay.drain()
    assert len(first.published) == 1 and second.examined == 0
    queue.append_event(_event("worker.completed", {"worker_id": "w1"}))
    third = relay.drain()
    assert len(third.published) == 1
    assert len(adapter.replay_subject("evallab.control.reward_seeker")) == 2


def test_local_adapter_persistence_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite"
    with LocalJetStreamAdapter(path) as adapter:
        adapter.publish("evallab.capture.queue", "id-1", b'{"seq":1}')
    with LocalJetStreamAdapter(path) as reopened:
        stored = reopened.replay_subject("evallab.capture.queue")
        assert [m.msg_id for m in stored] == ["id-1"]
        duplicate = reopened.publish("evallab.capture.queue", "id-1", b'{"seq":1}')
        assert duplicate.duplicate and duplicate.seq == 1
        assert len(reopened.replay_subject("evallab.>")) == 1


def test_replay_subject_cursor_and_scoping(adapter: LocalJetStreamAdapter) -> None:
    adapter.publish("evallab.capture.queue", "m1", b"{}")
    adapter.publish("evallab.findings.queue", "m2", b"{}")
    adapter.publish("evallab.capture.task_quality", "m3", b"{}")
    all_messages = adapter.replay_subject("evallab.>")
    assert [m.seq for m in all_messages] == [1, 2, 3]
    assert [m.seq for m in adapter.replay_subject("evallab.>", after_seq=1)] == [2, 3]
    assert [m.msg_id for m in adapter.replay_subject("evallab.capture.*")] == ["m1", "m3"]
    assert [m.msg_id for m in adapter.replay_subject("evallab.findings.queue")] == ["m2"]
    with pytest.raises(ValueError):
        adapter.replay_subject("evallab.>.tail")
    with pytest.raises(ValueError):
        adapter.replay_subject("evallab..gap")
    with pytest.raises(ValueError):
        adapter.replay_subject("evallab.>", after_seq=-1)
    with pytest.raises(ValueError):
        adapter.replay_subject("evallab.>", limit=0)


def test_replay_is_bounded_without_full_stream_scan(tmp_path: Path) -> None:
    for storage in (None, tmp_path / "bounded.sqlite"):
        with LocalJetStreamAdapter(storage) as store_adapter:
            for index in range(8):
                store_adapter.publish("evallab.capture.queue", f"m{index}", b"{}")
            bounded = store_adapter.replay_subject("evallab.>", limit=3)
            assert [m.msg_id for m in bounded] == ["m0", "m1", "m2"]
            calls = 0

            def counting(subject: str) -> bool:
                nonlocal calls
                calls += 1
                return subject.startswith("evallab.")

            matched = store_adapter._store.messages(counting, 0, 4)
            assert [m.msg_id for m in matched] == ["m0", "m1", "m2", "m3"]
            assert calls == 4  # iteration stops at the limit; no full-stream scan
            assert store_adapter.replay_subject("evallab.miss.*") == []


def test_immutable_stored_snapshots(adapter: LocalJetStreamAdapter) -> None:
    adapter.publish("evallab.capture.queue", "id-1", b'{"payload":{"value":1}}')
    first = adapter.replay_subject("evallab.capture.queue")[0]
    first.envelope["payload"]["value"] = 999
    again = adapter.replay_subject("evallab.capture.queue")[0]
    assert again.envelope == {"payload": {"value": 1}}
    assert again.payload == b'{"payload":{"value":1}}'


def test_poison_quarantine_does_not_starve_valid_events(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    good_one = queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    _insert_pending(queue, _event("worker.failed", {"worker_id": "w1"}, status="invented"))
    good_two = queue.append_event(_event("worker.completed", {"worker_id": "w1"}))
    report = OutboxRelay(queue, adapter).drain()
    assert report.published == (good_one["event_id"], good_two["event_id"])
    assert len(report.quarantined) == 1
    assert queue.pending_outbox() == []
    records = adapter.poisoned()
    assert len(records) == 1
    assert records[0].reason == "invalid_envelope"
    assert "status" in records[0].detail
    assert records[0].envelope["event_type"] == "worker.failed"


def test_heldout_envelopes_are_quarantined_not_published(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    for split in ("test", "heldout", "TEST"):
        _insert_pending(queue, _event("task_quality.observed", {"split": split}))
    # Even when admission was explicitly unsealed, the relay refuses to publish.
    queue.append_event(
        _event("task_quality.observed", dict(SEALED), system="task_quality"),
        frozen_eval_token=FROZEN_TOKEN,
    )
    report = OutboxRelay(queue, adapter).drain()
    assert len(report.quarantined) == 4
    assert not report.published
    assert adapter.replay_subject("evallab.>") == []
    assert all(record.reason == "heldout_payload" for record in adapter.poisoned())
    assert queue.pending_outbox() == []


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda e: e.update(system="evil.sys"), "invalid_envelope"),
        (lambda e: e.update(system="invented"), "invalid_envelope"),
        (lambda e: e.update(event_type="bad type"), "invalid_envelope"),
        (lambda e: e.update(event_type="*.wildcard"), "invalid_envelope"),
        (lambda e: e.update(ts="2026-10-24"), "invalid_envelope"),
        (lambda e: e.update(ts="not-a-date"), "invalid_envelope"),
        (lambda e: e.update(seq=-1), "invalid_envelope"),
        (lambda e: e.update(seq="1"), "invalid_envelope"),
        (lambda e: e.update(status="invented"), "invalid_envelope"),
        (lambda e: e.update(prefix_step=-2), "invalid_envelope"),
        (lambda e: e.update(payload=["not-a-mapping"]), "invalid_envelope"),
        (lambda e: e.update(payload={"split": "test"}), "heldout_payload"),
        (lambda e: e.update(payload={"split": "HELDOUT"}), "heldout_payload"),
    ],
)
def test_malformed_envelopes_quarantine_with_reason(
    queue: ContinuousWorkQueue,
    adapter: LocalJetStreamAdapter,
    mutate: Any,
    reason: str,
) -> None:
    envelope = _event("worker.started", {"worker_id": "w1"})
    mutate(envelope)
    _insert_pending(queue, envelope)
    report = OutboxRelay(queue, adapter).drain()
    assert len(report.quarantined) == 1
    assert adapter.poisoned()[0].reason == reason


def test_envelope_without_event_id_is_quarantined_but_unmarkable(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    envelope = _event("worker.started", {"worker_id": "w1"})
    envelope.pop("event_id")
    _insert_pending(queue, envelope)
    report = OutboxRelay(queue, adapter).drain()
    record = adapter.poisoned()[0]
    assert record.event_id is None and record.reason == "invalid_envelope"
    assert report.quarantined == (record.key,)
    assert report.deferred == 1  # still pending: nothing to mark consumed
    assert len(queue.pending_outbox()) == 1
    again = OutboxRelay(queue, adapter).drain()
    assert len(adapter.poisoned()) == 1  # first poison record wins
    assert again.deferred == 1


def test_mark_failure_is_distinct_from_invalid_envelope(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    # Journal row and outbox row agree on "real-id"; the envelope body claims
    # "phantom-id" — corruption the queue's own invariant would never produce.
    envelope = _event("worker.started", {"worker_id": "w1"}, event_id="phantom-id")
    _insert_pending(queue, envelope, event_id="real-id")
    relay = OutboxRelay(queue, adapter)
    report = relay.drain()
    assert report.published == ()
    assert report.mark_failed == ("phantom-id",)
    assert report.quarantined == ()
    assert len(queue.pending_outbox()) == 1
    second = relay.drain()
    assert second.mark_failed == ("phantom-id",)
    stored = adapter.replay_subject("evallab.control.reward_seeker")
    assert [m.msg_id for m in stored] == ["phantom-id"]  # deduped republish


def test_concurrent_publish_dedupes_once(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite"
    first_adapter, second_adapter = LocalJetStreamAdapter(path), LocalJetStreamAdapter(path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            acks = list(
                pool.map(
                    lambda index: (first_adapter if index % 2 else second_adapter).publish(
                        "evallab.capture.queue", "shared-id", b'{"k":1}'
                    ),
                    range(16),
                )
            )
        assert sum(ack.duplicate for ack in acks) == 15
        assert len(first_adapter.replay_subject("evallab.>")) == 1
    finally:
        first_adapter.close()
        second_adapter.close()


def test_tcp_transport_real_wire_protocol_and_puback(
    queue: ContinuousWorkQueue, broker: _FakeNatsBroker
) -> None:
    event = queue.append_event(_event("seeker.exploit_confirmed", {**DEV}))
    with (
        NatsTcpTransport("127.0.0.1", broker.port, request_timeout=5.0) as transport,
        PoisonQuarantine() as quarantine,
    ):
        relay = OutboxRelay(queue, transport, quarantine=quarantine)
        report = relay.drain()
    assert event["event_id"] in report.published
    published = broker.published[0]
    assert published["subject"] == "evallab.findings.reward_seeker"
    assert published["msg_id"] == event["event_id"]  # Nats-Msg-Id on the wire
    assert json.loads(published["body"]) == event
    assert "CONNECT" in broker.ops
    assert "PING" in broker.ops
    assert any(op.startswith("SUB _INBOX.") for op in broker.ops)


def test_tcp_crash_window_duplicate_puback(
    queue: ContinuousWorkQueue, broker: _FakeNatsBroker
) -> None:
    event = queue.append_event(_event("task_quality.failed", DEV))
    subject = route_subject(event)
    payload = json.dumps(event, sort_keys=True).encode()
    with NatsTcpTransport("127.0.0.1", broker.port, request_timeout=5.0) as transport:
        first = transport.publish(subject, event["event_id"], payload)
        assert not first.duplicate
        relay = OutboxRelay(queue, transport, quarantine=PoisonQuarantine())
        report = relay.drain()
        assert report.duplicates == (event["event_id"],)
        assert queue.pending_outbox() == []


def test_tcp_permanent_reject_quarantines_without_starving(
    queue: ContinuousWorkQueue, broker: _FakeNatsBroker
) -> None:
    bad = queue.append_event(_event("monitor.suspicion", {"finding_id": "f1"}))
    good = queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    broker.err_subjects.add("evallab.findings.misalignment")
    quarantine = PoisonQuarantine()
    with NatsTcpTransport("127.0.0.1", broker.port, request_timeout=5.0) as transport:
        report = OutboxRelay(queue, transport, quarantine=quarantine).drain()
    assert report.published == (good["event_id"],)
    assert report.quarantined == (f"event:{bad['event_id']}",)
    assert queue.pending_outbox() == []
    records = quarantine.poisoned()
    assert records[0].reason == "publish_rejected"
    assert records[0].subject == "evallab.findings.misalignment"


def test_tcp_timeout_and_hangup_leave_event_retryable(
    queue: ContinuousWorkQueue, broker: _FakeNatsBroker
) -> None:
    event = queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    subject = route_subject(event)
    quarantine = PoisonQuarantine()
    with NatsTcpTransport("127.0.0.1", broker.port, request_timeout=0.5) as transport:
        relay = OutboxRelay(queue, transport, quarantine=quarantine)
        broker.silent_subjects.add(subject)
        report = relay.drain()
        assert report.halted and "timed out" in (report.transient_error or "")
        assert queue.pending_outbox() == [event]
        assert quarantine.poisoned() == ()
        broker.silent_subjects.clear()
        broker.drop_subjects.add(subject)
        report = relay.drain()
        assert report.halted and queue.pending_outbox() == [event]
        broker.drop_subjects.clear()
        report = relay.drain()
        assert report.published == (event["event_id"],)
        assert queue.pending_outbox() == []


def test_tcp_transient_error_never_reaches_quarantine(
    queue: ContinuousWorkQueue, broker: _FakeNatsBroker
) -> None:
    queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    quarantine = PoisonQuarantine()
    with NatsTcpTransport("127.0.0.1", broker.port, request_timeout=0.5) as transport:
        broker.close()  # broker gone: connect and publish are transient failures
        relay = OutboxRelay(queue, transport, quarantine=quarantine)
        report = relay.drain()
        assert report.halted
        assert report.transient_error is not None
        assert len(queue.pending_outbox()) == 1
        assert quarantine.poisoned() == ()


def test_tcp_transport_validates_before_writing(broker: _FakeNatsBroker) -> None:
    with NatsTcpTransport("127.0.0.1", broker.port, request_timeout=1.0) as transport:
        cases: list[tuple[str, str, Any]] = [
            ("evallab..gap", "id", b"{}"),
            ("evallab.*", "id", b"{}"),
            ("evallab.capture.queue", "bad\nid", b"{}"),
            ("evallab.capture.queue", "id", "not-bytes"),
        ]
        for subject, msg_id, payload in cases:
            with pytest.raises(PermanentPublishError):
                transport.publish(subject, msg_id, payload)
        assert broker.published == []


@pytest.mark.parametrize(
    "frame",
    [
        b"MSG {REPLY} 1 junk\r\n",  # non-integer length
        b"MSG {REPLY} 1 -3\r\n",  # negative length
        b"MSG {REPLY} 1 99999999\r\n",  # oversized ack frame
        b"MSG {REPLY} 1 4\r\nABCD\x00\x00",  # missing trailing CRLF
        b"MSG {REPLY} 1\r\n",  # too few MSG fields
        b"HMSG {REPLY} 1 4\r\n",  # too few HMSG fields
        b"HMSG {REPLY} 1 junk 4\r\n",  # non-integer header length
        b"HMSG {REPLY} 1 9 4\r\nNATS/1.0\r\n",  # header length exceeds total
        b"HMSG {REPLY} 1 4 -2\r\n",  # negative total length
        b"HMSG {REPLY} 1 5 5\r\nNATS/1.0",  # missing trailing CRLF
    ],
)
def test_tcp_malformed_frames_are_transient_and_close(
    broker: _FakeNatsBroker, frame: bytes
) -> None:
    broker.raw_replies["evallab.capture.queue"] = frame
    with NatsTcpTransport(
        "127.0.0.1", broker.port, connect_timeout=1.0, request_timeout=2.0
    ) as transport:
        with pytest.raises(TransientBrokerError):
            transport.publish("evallab.capture.queue", "msg-x", b"{}")
        assert transport._sock is None
        broker.raw_replies.clear()
        recovered = transport.publish("evallab.capture.queue", "msg-y", b"{}")
        assert recovered.seq == 1 and recovered.stream == "FAKE"


def test_tcp_malformed_puback_body_is_transient(broker: _FakeNatsBroker) -> None:
    for ack in (
        json.dumps({"stream": "FAKE", "seq": 0, "duplicate": False}).encode(),
        json.dumps({"seq": 4, "duplicate": False}).encode(),
    ):
        frame = b"MSG {REPLY} 1 " + str(len(ack)).encode() + b"\r\n" + ack + b"\r\n"
        broker.raw_replies["evallab.capture.queue"] = frame
        with NatsTcpTransport(
            "127.0.0.1", broker.port, connect_timeout=1.0, request_timeout=2.0
        ) as transport:
            with pytest.raises(TransientBrokerError):
                transport.publish("evallab.capture.queue", "msg-z", b"{}")
            assert transport._sock is None


def test_tcp_connect_timeout_bounds_handshake() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    release = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        release.wait(30)  # never sends INFO: the handshake stalls
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with (
            NatsTcpTransport(
                "127.0.0.1", port, connect_timeout=0.3, request_timeout=30.0
            ) as transport,
            pytest.raises(TransientBrokerError, match="timed out"),
        ):
            transport.publish("evallab.capture.queue", "msg-t", b"{}")
        assert time.monotonic() - started < 10
    finally:
        release.set()
        listener.close()
        thread.join(timeout=2)


def test_http_gateway_puback_reject_and_transient(
    queue: ContinuousWorkQueue, gateway: tuple[str, dict[str, Any]]
) -> None:
    url, state = gateway
    first = queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    second = queue.append_event(_event("monitor.suspicion", {"finding_id": "f1"}))
    with (
        HttpGatewayTransport(url, timeout=5.0) as transport,
        PoisonQuarantine() as quarantine,
    ):
        relay = OutboxRelay(queue, transport, quarantine=quarantine)
        state["behavior"] = "reject"
        report = relay.drain()
        # Both events share the gateway; rejection of the first does not starve the second.
        assert report.quarantined == (
            f"event:{first['event_id']}",
            f"event:{second['event_id']}",
        )
        assert queue.pending_outbox() == []
        assert quarantine.poisoned()[0].reason == "publish_rejected"
        state["received"].clear()
        queue.append_event(_event("worker.completed", {"worker_id": "w1"}))
        state["behavior"] = "flaky"
        report = relay.drain()
        assert report.halted and len(queue.pending_outbox()) == 1
        state["behavior"] = "noack"
        report = relay.drain()
        assert report.halted and len(queue.pending_outbox()) == 1
        state["behavior"] = "ack"
        report = relay.drain()
        assert len(report.published) == 1
        assert queue.pending_outbox() == []
        sent = state["received"][-1]
        assert sent["subject"] == "evallab.control.reward_seeker"
        assert sent["msg_id"] == queue.read_events()[-1]["event_id"]


@pytest.mark.parametrize(
    "ack_body",
    [
        b'{"stream":"GW","seq":0,"duplicate":false}',
        b'{"stream":"GW","seq":-7,"duplicate":false}',
        b'{"stream":"GW","seq":3.5,"duplicate":false}',
        b'{"stream":"GW","seq":"3","duplicate":false}',
        b'{"seq":3,"duplicate":false}',
        b'{"stream":"","seq":3,"duplicate":false}',
        b'{"stream":"GW","seq":3,"duplicate":"yes"}',
        b'{"stream":"GW","seq":3,"duplicate":1}',
    ],
)
def test_malformed_puback_leaves_event_unpublished(
    queue: ContinuousWorkQueue, gateway: tuple[str, dict[str, Any]], ack_body: bytes
) -> None:
    url, state = gateway
    state["ack_body"] = ack_body
    event = queue.append_event(_event("worker.started", {"worker_id": "w1"}))
    with (
        HttpGatewayTransport(url, timeout=5.0) as transport,
        PoisonQuarantine() as quarantine,
    ):
        report = OutboxRelay(queue, transport, quarantine=quarantine).drain()
    assert report.halted and report.transient_error is not None
    assert report.published == ()
    assert queue.pending_outbox() == [event]
    assert quarantine.poisoned() == ()


def test_remote_transport_requires_explicit_quarantine(queue: ContinuousWorkQueue) -> None:
    transport = HttpGatewayTransport("http://127.0.0.1:1/unused", timeout=1.0)
    with pytest.raises(ValueError, match="quarantine"):
        OutboxRelay(queue, transport)
    transport.close()


def test_relay_rejects_closed_and_validates_limit(
    queue: ContinuousWorkQueue, adapter: LocalJetStreamAdapter
) -> None:
    relay = OutboxRelay(queue, adapter)
    with pytest.raises(ValueError):
        relay.drain(limit=0)
    relay.close()
    with pytest.raises(RuntimeError, match="closed"):
        relay.drain()
