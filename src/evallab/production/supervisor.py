"""Production supervisor: one deterministic, lease-aware orchestration step.

``ProductionSupervisor`` coordinates the existing durable production systems —
``ContinuousWorkQueue``, ``MisalignmentLiveWorker``, ``TaskQualityLiveWorker``,
``RewardSeekerLiveWorker``, ``OutboxRelay`` and ``VerifiedAssetExporter`` — as a
thin collector over their own contracts, not a parallel orchestration model:

1. **Misalignment first.** One ``misalignment.prefix_audit_task`` job is claimed
   durably and its envelope ingested by the monitor worker (the lease is
   acked only after ingestion completes), then one batch of the monitor's
   durable journal consumer is replayed through ``ingest_event``.
2. **Task quality.** One audit/repair/annotation job through the worker's own
   ``run_once()`` — claim, execute, ack/release stay inside the worker.
3. **Reward seeker.** One seek/rebreak job through the worker's async
   ``run_once()``; the supervisor drives the coroutine without taking over the
   lease. Coroutines run on a single supervisor-owned event loop living on a
   private daemon thread and driven via ``run_coroutine_threadsafe``, so any
   loop-bound client the worker keeps stays on one live loop across steps
   rather than being stranded on a closed ``asyncio.run`` loop; the caller's
   own loop is never blocked by the seeker and ``close()`` stops the thread.
4. **Export gate.** A dedicated durable consumer cursor scans pending
   ``asset.verified`` events. Once at least ``asset_batch_threshold`` confirmed
   assets are pending, real candidates are derived from the verified events and
   handed to ``VerifiedAssetExporter.export_training_bundle`` — which
   re-verifies every identity and receipt fail-closed (TEST/heldout identities
   are refused regardless of what any caller claims). The export boundary is
   the newest ``asset.verified`` sequence in the window — an immutable mark
   that receipts, heartbeats, and unrelated events can never move. That
   boundary and the exact candidate event set are journaled as an
   ``asset.export`` intent BEFORE the bundle is written, so a crash between
   write and acknowledgement replays the recorded export byte-identically —
   even when further verified assets were appended inside the crash window
   (they wait for the next batch). The intent is paired with its receipt by
   ``export_id``, and the cursor acknowledges only the recorded boundary after
   the bundle is durably written, so later events stay unacknowledged and an
   already-exported window is never re-scanned.

Every step — idle, productive, or failed — ends by refreshing liveness from the
journal, emitting ``worker.heartbeat`` heartbeats per worker system plus one
``collector.heartbeat``, and draining the relay outbox. Worker heartbeats report
real evidence: the newest journal event timestamp for that system, the
supervisor's last observed work for it, live leases from ``queue_snapshot()``
with per-lease ``held_seconds``, and leases watched lapsing uncompleted —
never synthetic refreshed timestamps. A live lease is held work, not progress:
the first observed claim and each lease-deadline extension count as activity,
but an unchanged lease cannot postpone ``stale``. A worker is ``stale`` when it
has produced no journal output and no observed work for longer than
``stale_after_seconds`` (default 10), even while holding a lease;
``expired_leases`` lists jobs the supervisor saw leased that left the leased
set past their deadline without completing, resolved per job through the
queue's own durable state so an unrelated same-type completion never hides a
lapse.

``clock`` is injectable for deterministic tests. It must return epoch seconds
in the same domain the journal's ``ts`` values and the queue's lease clock use
(``time.time``); lease-deadline accounting uses the queue's own real clock.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.asset_exporter import VerifiedAssetExporter
from evallab.production.corpus_queue import WORKER_SYSTEMS, ContinuousWorkQueue
from evallab.production.monitor_worker import MisalignmentLiveWorker
from evallab.production.outbox_relay import OutboxRelay
from evallab.production.public_corpus_loader import PREFIX_AUDIT_JOB
from evallab.production.seeker_worker import RewardSeekerLiveWorker
from evallab.production.tq_worker import TaskQualityLiveWorker

__all__ = ["ProductionSupervisor"]

#: Job types the supervisor claims on the monitor worker's behalf. The monitor
#: consumes journal events (its own durable cursor contract); claimed jobs are
#: fed through ``ingest_event`` so prefix audits keep the same scoring path.
MONITOR_JOB_TYPES: tuple[str, ...] = (PREFIX_AUDIT_JOB,)

#: Event types the supervisor itself emits. They are never monitor input and
#: never evidence of worker liveness, so they cannot hide a stalled worker or
#: keep the pipeline busy forever.
_SUPERVISOR_EVENT_TYPES = frozenset(
    {"worker.heartbeat", "collector.heartbeat", "asset.export"}
)

#: ``SeekerJobOutcome`` fields flattened into the stage report under their own
#: names; the outcome's ``status`` stays distinct as ``outcome_status``.
_SEEKER_SUMMARY_KEYS = ("event_id", "run_id", "job_type", "task_id")


def _require_finite_positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def _require_positive_int(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


class ProductionSupervisor:
    """Deterministic priority orchestration over the live production queue.

    ``run_step()`` executes at most one unit of work per stage in fixed
    priority order (misalignment -> task quality -> reward seeker -> export),
    and a lower-priority worker never claims while higher-priority work is
    pending or blocked: task quality waits for misalignment jobs to drain, and
    the seeker waits on both — a skipped stage reports ``status="blocked"``.
    ``run_until_idle`` repeats steps until one reports ``idle`` — nothing
    claimed, nothing meaningful consumed, no export, no errors, nothing
    blocked — or ``max_steps`` bounds the run.
    """

    def __init__(
        self,
        queue: ContinuousWorkQueue,
        *,
        monitor: MisalignmentLiveWorker,
        task_quality: TaskQualityLiveWorker,
        seeker: RewardSeekerLiveWorker,
        relay: OutboxRelay,
        exporter: VerifiedAssetExporter | None = None,
        export_root: str | Path | None = None,
        repo_root: str | Path | None = None,
        supervisor_id: str = "production-supervisor",
        monitor_worker_id: str = "misalignment-live",
        monitor_consumer_id: str = "misalignment-live",
        export_consumer_id: str | None = None,
        asset_batch_threshold: int = 4,
        monitor_batch_limit: int = 500,
        claim_lease_seconds: float = 60.0,
        stale_after_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.supervisor_id = _require_text(supervisor_id, "supervisor_id")
        self._queue = queue
        self._monitor = monitor
        self._task_quality = task_quality
        self._seeker = seeker
        self._relay = relay
        self._monitor_worker_id = _require_text(monitor_worker_id, "monitor_worker_id")
        self._monitor_consumer_id = _require_text(
            monitor_consumer_id, "monitor_consumer_id"
        )
        self._stale_after = _require_finite_positive(
            stale_after_seconds, "stale_after_seconds"
        )
        self._claim_lease_seconds = _require_finite_positive(
            claim_lease_seconds, "claim_lease_seconds"
        )
        self._monitor_batch_limit = _require_positive_int(
            monitor_batch_limit, "monitor_batch_limit"
        )
        self._asset_batch_threshold = _require_positive_int(
            asset_batch_threshold, "asset_batch_threshold"
        )
        if exporter is None and export_root is not None:
            exporter = VerifiedAssetExporter(queue, repo_root=repo_root)
        if exporter is not None and export_root is None:
            raise ValueError("export_root is required when an exporter is configured")
        self._exporter = exporter
        self._export_root = Path(export_root).expanduser() if export_root else None
        self._export_consumer_id = _require_text(
            export_consumer_id or f"{self.supervisor_id}:asset-export",
            "export_consumer_id",
        )
        self._clock = clock
        self._step = 0
        self._started = clock()
        self._run_id = f"supervisor:{self.supervisor_id}"
        # Activity/liveness state: journal-derived event timestamps plus
        # supervisor-observed work marks; both are real timestamps.
        self._seen_seq = 0
        self._event_activity: dict[str, float] = {}
        self._observed_activity: dict[str, float] = {}
        # Lease tracking across snapshots: event_id -> {job_type, worker_id,
        # lease_until, first_seen}; lapsed leases land in _expired.
        self._leases: dict[str, dict[str, Any]] = {}
        self._expired: dict[str, dict[str, Any]] = {}
        self._exports: list[dict[str, Any]] = []
        self._pending_assets = 0
        self._last_relay: dict[str, Any] | None = None
        # One owned event loop on one owned thread drives every async worker
        # coroutine, so loop-bound worker clients keep a single live loop.
        self._loop_guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ time / events

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, UTC).isoformat()

    @staticmethod
    def _epoch(ts: Any) -> float | None:
        if not isinstance(ts, str) or not ts.strip():
            return None
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()

    @staticmethod
    def _is_supervisor_event(event: Mapping[str, Any]) -> bool:
        """Supervisor-emitted envelopes never count as worker liveness."""
        if str(event.get("event_type")) not in _SUPERVISOR_EVENT_TYPES:
            return False
        payload = event.get("payload")
        return isinstance(payload, Mapping) and isinstance(payload.get("emitter"), str)

    def _emit(
        self,
        event_type: str,
        system: str,
        payload: Mapping[str, Any],
        *,
        status: str = "provisional",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        envelope = {
            "event_id": f"evt-{uuid.uuid4().hex}",
            "run_id": self._run_id,
            "system": system,
            "event_type": event_type,
            "seq": 0,
            "ts": self._iso(self._now()),
            "prefix_step": None,
            "status": status,
            "payload": {**dict(payload), "emitter": self.supervisor_id},
        }
        return self._queue.append_event(envelope, idempotency_key=idempotency_key)

    def _mark_active(self, system: str) -> None:
        """Record that the supervisor observed the worker produce real work."""
        self._observed_activity[system] = self._now()

    # ------------------------------------------------------------------ stages

    def _misalignment_stage(self, report: dict[str, Any]) -> None:
        """Misalignment prefix checks first: one durable job claim, then a batch.

        ``ingest_event`` is the monitor's real ingestion path (deduped by
        ``run.seen_events``), so a claimed job envelope and its later consumer
        replay score once. The claimed lease is acked only after ingest; an
        ingest failure releases the job for retry exactly like the workers' own
        run loops do. The consumer batch mirrors ``MisalignmentLiveWorker.consume``
        with per-event accounting so supervisor heartbeats replayed through the
        cursor can never look like monitor work.
        """
        stage: dict[str, Any] = {
            "status": "ok",
            "claimed": None,
            "emitted": 0,
            "consumed": 0,
            "consumed_meaningful": 0,
        }
        report["misalignment"] = stage
        try:
            self._monitor.recover(self._queue)
            claimed = self._queue.claim(
                self._monitor_worker_id,
                job_types=MONITOR_JOB_TYPES,
                lease_seconds=self._claim_lease_seconds,
            )
            if claimed is not None:
                event_id = str(claimed["event_id"])
                stage["claimed"] = event_id
                try:
                    produced = self._monitor.ingest_event(claimed)
                except Exception:
                    with suppress(Exception):
                        self._queue.release(
                            event_id, lease_token=str(claimed["lease_token"])
                        )
                    raise
                self._queue.ack(event_id, lease_token=str(claimed["lease_token"]))
                stage["emitted"] += len(produced)
                self._mark_active("misalignment")
            events = self._queue.read_consumer(
                self._monitor_consumer_id, limit=self._monitor_batch_limit
            )
            for event in events:
                produced = self._monitor.ingest_event(event)
                self._queue.ack_consumer(self._monitor_consumer_id, event["seq"])
                stage["consumed"] += 1
                stage["emitted"] += len(produced)
                if not self._is_supervisor_event(event):
                    stage["consumed_meaningful"] += 1
            expired = self._monitor.expire_controls()
            stage["emitted"] += len(expired)
            if stage["consumed_meaningful"] or claimed is not None:
                self._mark_active("misalignment")
        except Exception as exc:
            stage["status"] = "error"
            self._record_error(report, "misalignment", exc)

    def _task_quality_stage(self, report: dict[str, Any]) -> None:
        stage: dict[str, Any] = {"status": "idle"}
        report["task_quality"] = stage
        try:
            result = self._task_quality.run_once()
        except Exception as exc:
            stage["status"] = "error"
            self._record_error(report, "task_quality", exc)
            return
        if result is None:
            return
        stage.update(
            status="processed",
            complete=bool(result.get("complete")),
            qualified=(result.get("qualification") or {}).get("qualified"),
            findings=len(result.get("findings") or []),
            result=result,
        )
        self._mark_active("task_quality")

    def _seeker_stage(self, report: dict[str, Any]) -> None:
        stage: dict[str, Any] = {"status": "idle"}
        report["reward_seeker"] = stage
        try:
            outcome = self._drive_coroutine(self._seeker.run_once)
        except Exception as exc:
            stage["status"] = "error"
            self._record_error(report, "reward_seeker", exc)
            return
        if outcome is None:
            return
        stage.update(
            status="processed",
            outcome_status=outcome.status,
            exploit_confirmed=outcome.exploit_confirmed,
            attempts_run=outcome.attempts_run,
            original_reward=outcome.original_reward,
            repaired_reward=outcome.repaired_reward,
            emitted=len(outcome.emitted_events),
            outcome=outcome,
            **{key: getattr(outcome, key) for key in _SEEKER_SUMMARY_KEYS},
        )
        self._mark_active("reward_seeker")

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """The supervisor-owned loop, created lazily on first async drive."""
        with self._loop_guard:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            started = threading.Event()

            def target() -> None:
                asyncio.set_event_loop(loop)
                started.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(
                target=target, name="production-supervisor-loop", daemon=True
            )
            thread.start()
            started.wait()
            self._loop, self._loop_thread = loop, thread
            return loop

    def _drive_coroutine(self, factory: Callable[[], Any]) -> Any:
        """Run the seeker's async ``run_once`` on the supervisor-owned loop.

        The coroutine is submitted to the one private loop on its own thread
        via ``run_coroutine_threadsafe``, which works identically whether or
        not the calling thread already runs an event loop — unlike per-step
        ``asyncio.run`` calls, loop-bound resources the worker creates stay on
        a loop that outlives the coroutine. The durable queue is already
        thread-safe, and the job's lease stays owned by the seeker worker.
        """
        loop = self._ensure_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "cannot synchronously drive a coroutine on the supervisor's own loop"
            )
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        return future.result()

    def close(self) -> None:
        """Stop the owned loop thread; idempotent."""
        with self._loop_guard:
            loop, thread = self._loop, self._loop_thread
            self._loop = self._loop_thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)

    def __enter__(self) -> ProductionSupervisor:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ export gate

    def _export_stage(self, report: dict[str, Any]) -> None:
        stage: dict[str, Any] = {"status": "disabled"}
        report["export"] = stage
        if self._exporter is None or self._export_root is None:
            return
        try:
            events = self._export_window()
            intent = self._pending_export_intent(events)
            replayed = intent is not None
            if replayed:
                # The intent's recorded boundary and candidate set replay
                # verbatim; verified assets appended during the crash window
                # wait for the next batch instead of shifting this export.
                asset_events, boundary, scanned = self._intent_window(intent)
            else:
                asset_events = [
                    event for event in events if event.get("event_type") == "asset.verified"
                ]
                # The export boundary is the newest asset.verified sequence in
                # the window, not the journal tail: receipts, heartbeats, and
                # unrelated events appended between the scan and the ack can
                # never move it.
                boundary = max((int(event["seq"]) for event in asset_events), default=0)
                scanned = len(events)
            confirmed = [
                event for event in asset_events if event.get("status") == "confirmed"
            ]
            stage.update(
                status="pending",
                pending_assets=len(confirmed),
                asset_events=len(asset_events),
                events_scanned=scanned,
                consumer_id=self._export_consumer_id,
                replayed=replayed,
            )
            self._pending_assets = len(confirmed)
            if not replayed and len(confirmed) < self._asset_batch_threshold:
                return
            slug = self._export_consumer_id.replace(":", "-").replace("/", "-")
            export_id = f"{slug}-bundle-{boundary:08d}"
            out_dir = self._export_root / export_id
            if not replayed:
                # Durable export intent BEFORE any bytes move: the boundary
                # and exact candidate set are journaled, so a crash between
                # write and ack replays this identical export — assets newly
                # verified inside the crash window can never rename or change
                # the bundle a restart produces.
                self._emit(
                    "asset.export",
                    "queue",
                    {
                        "kind": "training_bundle",
                        "phase": "intent",
                        "consumer_id": self._export_consumer_id,
                        "export_id": export_id,
                        "boundary": boundary,
                        "asset_event_ids": [
                            str(event["event_id"]) for event in asset_events
                        ],
                        "events_scanned": scanned,
                        "output_dir": str(out_dir),
                    },
                    status="provisional",
                    idempotency_key=(
                        f"{self._export_consumer_id}:export-intent:{export_id}"
                    ),
                )
            candidates, unverifiable = self._export_candidates(asset_events)
            # The exporter is the gate: it re-verifies identity, receipts and
            # the TEST lock fail-closed; a passed candidate is never trusted.
            manifest = self._exporter.export_training_bundle(candidates, out_dir)
            # The durable cursor acknowledges only the processed boundary after
            # the bundle is written and BEFORE the receipt is emitted: a crash
            # anywhere in this window leaves the intent pending, so replay
            # reproduces the recorded candidate set into a byte-identical
            # bundle. Later events are never acknowledged beyond the window,
            # and an exported window is never re-scanned.
            self._queue.ack_consumer(self._export_consumer_id, boundary)
            event = self._emit(
                "asset.export",
                "queue",
                {
                    "kind": "training_bundle",
                    "phase": "receipt",
                    "consumer_id": self._export_consumer_id,
                    "export_id": export_id,
                    "output_dir": str(out_dir),
                    "last_seq": boundary,
                    "events_scanned": scanned,
                    "asset_events": len(asset_events),
                    "replayed": replayed,
                    "manifest": manifest,
                },
                status="confirmed",
                idempotency_key=(
                    f"{self._export_consumer_id}:export-receipt:{export_id}"
                ),
            )
            self._exports.append(manifest)
            stage.update(
                status="exported",
                candidates=len(candidates),
                unverifiable=unverifiable,
                output_dir=str(out_dir),
                boundary=boundary,
                manifest=manifest,
                event_id=event["event_id"],
            )
        except Exception as exc:
            stage["status"] = "error"
            self._record_error(report, "export", exc)

    def _pending_export_intent(
        self, events: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        """The oldest journaled export intent this consumer never completed.

        ``asset.export`` intent/receipt events pair by ``export_id`` in journal
        order; an intent without a later receipt marks an export that was
        recorded durably but crashed before the cursor acknowledged. Receipts
        always follow their intent in sequence, so both stay inside the
        consumer's unacked window together.
        """
        pending: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in events:
            if event.get("event_type") != "asset.export":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if payload.get("consumer_id") != self._export_consumer_id:
                continue
            export_id = payload.get("export_id")
            if not isinstance(export_id, str) or not export_id:
                continue
            if payload.get("phase") == "intent":
                if export_id not in pending:
                    pending[export_id] = event
                    order.append(export_id)
            elif payload.get("phase") == "receipt":
                pending.pop(export_id, None)
        for export_id in order:
            if export_id in pending:
                return pending[export_id]
        return None

    def _intent_window(
        self, intent: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Rebuild the recorded candidate set for a pending export intent.

        Every recorded ``asset.verified`` event id must still resolve in the
        append-only journal at or below the recorded boundary; a broken or
        out-of-range reference fails closed instead of silently re-deriving
        the window from the moved journal tail.
        """
        payload = intent.get("payload")
        if not isinstance(payload, Mapping):
            raise RuntimeError("export intent payload is not an object")
        boundary = payload.get("boundary")
        event_ids = payload.get("asset_event_ids")
        scanned = payload.get("events_scanned")
        if (
            type(boundary) is not int
            or boundary < 1
            or not isinstance(event_ids, list)
            or any(not isinstance(event_id, str) for event_id in event_ids)
            or type(scanned) is not int
        ):
            raise RuntimeError("export intent is missing a valid boundary/candidate set")
        asset_events: list[dict[str, Any]] = []
        for event_id in event_ids:
            event = self._queue.get_event(event_id)
            if event is None:
                raise RuntimeError(f"export intent references a missing event: {event_id}")
            if event.get("event_type") != "asset.verified" or int(event["seq"]) > boundary:
                raise RuntimeError(f"export intent references invalid event: {event_id}")
            asset_events.append(event)
        return asset_events, boundary, scanned

    def _export_window(self) -> list[dict[str, Any]]:
        """Unacknowledged journal window for the export consumer cursor.

        Mirrors ``VerifiedAssetExporter._scan``'s consumer branch: page the
        durable cursor, then continue with plain ``read_events`` so the whole
        pending window is scanned while the cursor stays unacknowledged.
        """
        events: list[dict[str, Any]] = []
        batch = self._queue.read_consumer(
            self._export_consumer_id, limit=self._monitor_batch_limit
        )
        events.extend(batch)
        while batch:
            batch = self._queue.read_events(
                after_seq=int(batch[-1]["seq"]), limit=self._monitor_batch_limit
            )
            events.extend(batch)
        return events

    def _export_candidates(
        self, asset_events: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Build training candidates from real verified-asset events only.

        The candidate references its source ``asset.verified`` event by
        ``asset_event_id`` and embeds the exporter's own verified row as
        evidence; assets that fail verification still reach the gate so their
        rejection is recorded in the bundle manifest instead of being hidden.
        """
        assert self._exporter is not None
        candidates: list[dict[str, Any]] = []
        unverifiable = 0
        seen: set[str] = set()
        for event in asset_events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            raw_asset_id = payload.get("asset_id")
            asset_id = (
                raw_asset_id
                if isinstance(raw_asset_id, str) and raw_asset_id.strip()
                else None
            )
            # Dedupe key only; a missing producer asset_id is never invented.
            dedupe_key = asset_id if asset_id is not None else str(event.get("event_id"))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            verified_row, problems = self._exporter.verify_asset_event(event)
            if verified_row is None:
                unverifiable += 1
            kind = payload.get("kind") or payload.get("asset_type") or "unknown"
            candidates.append(
                {
                    "dataset_id": payload.get("dataset_id"),
                    "item_id": payload.get("item_id"),
                    "mechanism_signature": self._mechanism_signature(payload),
                    "asset_event_id": event.get("event_id"),
                    "row": {
                        "asset": verified_row
                        if verified_row is not None
                        else {
                            "asset_id": asset_id,
                            "kind": str(kind),
                            "verification_problems": list(problems),
                        }
                    },
                }
            )
        return candidates, unverifiable

    @staticmethod
    def _mechanism_signature(payload: Mapping[str, Any]) -> str | None:
        """The producer's explicit semantic signature, or ``None``.

        Only producer-declared ``mechanism_signature``/``shortcut_signature``/
        ``mechanism`` fields qualify — the signature keys the exporter's
        ``(dataset, task, mechanism)`` dedupe, so a per-asset fallback invented
        here would pass the gate dishonestly instead of being rejected. The
        exporter records a missing value as a manifest rejection.
        """
        for key in ("mechanism_signature", "shortcut_signature", "mechanism"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    # ------------------------------------------------------------------ liveness

    def _observe(self) -> dict[str, Any]:
        """Refresh journal activity, queue counters, and lease transitions."""
        while True:
            batch = self._queue.read_events(after_seq=self._seen_seq, limit=500)
            if not batch:
                break
            for event in batch:
                seq = int(event["seq"])
                self._seen_seq = max(self._seen_seq, seq)
                system = event.get("system")
                if system not in WORKER_SYSTEMS or self._is_supervisor_event(event):
                    continue
                ts = self._epoch(event.get("ts"))
                if ts is not None:
                    self._event_activity[system] = max(
                        ts, self._event_activity.get(system, float("-inf"))
                    )
        snapshot = self._queue.queue_snapshot()
        # Lease deadlines live on the queue's own clock. A newly observed claim
        # and each deadline extension are real worker activity — a lease that
        # merely persists is not. Disappearances resolve per job through its
        # own durable state: a same-type completion never explains another
        # job's lapse, a release is a clean handback, and only a lease gone
        # past its deadline uncompleted lapsed while held.
        now = time.time()
        active = {
            lease["event_id"]: lease for lease in snapshot["active_leases"]
        }
        for lease in snapshot["active_leases"]:
            system = lease["job_type"].partition(".")[0]
            tracked = self._leases.get(lease["event_id"])
            if tracked is None:
                self._leases[lease["event_id"]] = {
                    "event_id": lease["event_id"],
                    "job_type": lease["job_type"],
                    "worker_id": lease["worker_id"],
                    "lease_until": lease["lease_until"],
                    "first_seen": self._now(),
                }
                if system in WORKER_SYSTEMS:
                    self._mark_active(system)
            elif lease["lease_until"] > tracked["lease_until"]:
                tracked["lease_until"] = lease["lease_until"]
                tracked["worker_id"] = lease["worker_id"]
                if system in WORKER_SYSTEMS:
                    self._mark_active(system)
        for event_id, info in list(self._leases.items()):
            if event_id in active:
                continue
            job = self._queue.job_state(event_id)
            if job is None or job["state"] == "completed":
                del self._leases[event_id]
                continue
            if job["state"] == "leased":
                # Reclaimed between the snapshot and this read; keep tracking
                # under the new deadline and holder — the reclaim itself is
                # worker activity when it extended the deadline.
                system = str(info["job_type"]).partition(".")[0]
                if job["lease_until"] > info["lease_until"] and system in WORKER_SYSTEMS:
                    self._mark_active(system)
                info["lease_until"] = max(info["lease_until"], job["lease_until"])
                info["worker_id"] = job["worker_id"] or info["worker_id"]
                continue
            del self._leases[event_id]
            if job["lease_until"] is not None and job["lease_until"] <= now:
                self._expired[event_id] = {**info, "noticed_at": now}
        return snapshot

    def _worker_state(
        self, system: str, snapshot: Mapping[str, Any], now: float
    ) -> dict[str, Any]:
        leases = [
            lease
            for lease in snapshot["active_leases"]
            if str(lease["job_type"]).partition(".")[0] == system
        ]
        marks = [
            value
            for value in (
                self._event_activity.get(system),
                self._observed_activity.get(system),
            )
            if value is not None
        ]
        last = max(marks) if marks else None
        age = None if last is None else now - last
        # A live lease is held work, not a heartbeat: staleness comes from real
        # activity marks alone, so overdue work surfaces even while a lease is
        # held and held_seconds reports how long.
        if age is not None:
            stale = age > self._stale_after
        else:
            stale = (now - self._started) > self._stale_after
        return {
            "worker_id": self._worker_ids[system],
            "last_activity_at": None if last is None else self._iso(last),
            "seconds_since_activity": None if age is None else round(age, 6),
            "stale": stale,
            "stale_after_seconds": self._stale_after,
            "leased_jobs": len(leases),
            "leases": [
                {
                    **{
                        key: lease[key]
                        for key in ("event_id", "job_type", "worker_id", "lease_until")
                    },
                    "held_seconds": round(
                        max(
                            0.0,
                            now
                            - self._leases.get(lease["event_id"], {}).get(
                                "first_seen", now
                            ),
                        ),
                        6,
                    ),
                }
                for lease in leases
            ],
            "expired_leases": sorted(
                event_id
                for event_id, info in self._expired.items()
                if str(info["job_type"]).partition(".")[0] == system
            ),
            "queued_jobs": snapshot["by_system"].get(system, 0),
            "active_workers": list(snapshot["active_workers"].get(system, [])),
        }

    @property
    def _worker_ids(self) -> dict[str, str]:
        return {
            "misalignment": self._monitor_worker_id,
            "task_quality": self._task_quality.worker_id,
            "reward_seeker": self._seeker.worker_id,
        }

    def _emit_heartbeats(
        self, report: dict[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        now = self._now()
        stage_errors = {error["stage"]: error["error"] for error in report["errors"]}
        workers = {
            system: self._worker_state(system, snapshot, now)
            for system in WORKER_SYSTEMS
        }
        report["workers"] = workers
        report["stale_workers"] = sorted(
            system for system, state in workers.items() if state["stale"]
        )
        for system in WORKER_SYSTEMS:
            state = workers[system]
            event = self._emit(
                "worker.heartbeat",
                system,
                {
                    "worker_id": state["worker_id"],
                    "observed_at": self._iso(now),
                    "last_activity_at": state["last_activity_at"],
                    "seconds_since_activity": state["seconds_since_activity"],
                    "stale": state["stale"],
                    "stale_after_seconds": state["stale_after_seconds"],
                    "leased_jobs": state["leased_jobs"],
                    "leases": state["leases"],
                    "expired_leases": state["expired_leases"],
                    "queued_jobs": state["queued_jobs"],
                    "last_error": stage_errors.get(system),
                },
            )
            report["heartbeats"].append(event["event_id"])
        collector = self._emit(
            "collector.heartbeat",
            "queue",
            {
                "role": "collector",
                "supervisor_id": self.supervisor_id,
                "observed_at": self._iso(now),
                "step_index": self._step,
                "queue_depth": snapshot["depth"],
                "queued_jobs": snapshot["jobs"]["queued"],
                "leased_jobs": snapshot["jobs"]["leased"],
                "completed_jobs": snapshot["jobs"]["completed"],
                "last_seq": snapshot["last_seq"],
                "pending_outbox": len(self._queue.pending_outbox(limit=1000)),
                "stale_workers": report["stale_workers"],
                "expired_leases": sorted(self._expired),
                "assets_pending": self._pending_assets,
                "exports_written": len(self._exports),
                "errors_this_step": len(report["errors"]),
                "last_relay": self._last_relay,
            },
        )
        report["heartbeats"].append(collector["event_id"])

    # ------------------------------------------------------------------ step / loop

    def _record_error(
        self, report: dict[str, Any], stage: str, exc: BaseException
    ) -> None:
        report["errors"].append(
            {"stage": stage, "error": f"{type(exc).__name__}: {exc}"}
        )

    def _drain_relay(self) -> dict[str, Any]:
        try:
            drained = self._relay.drain()
        except Exception as exc:
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return {"status": "ok", **asdict(drained)}

    def _system_pending(self, system: str) -> int:
        """Queued + leased jobs for a worker system right now."""
        return int(self._queue.queue_snapshot()["by_system"].get(system, 0))

    def _misalignment_pending(self, report: Mapping[str, Any]) -> bool:
        """Misalignment work that must finish before lower-priority claims.

        Backlog pressure is measured on consumer-relevant events only: the
        supervisor's own heartbeats and receipts flow through the monitor
        cursor every step, so counting them would starve the lower-priority
        stages behind the batch limit forever.
        """
        stage = report.get("misalignment") or {}
        return (
            stage.get("status") == "error"
            or int(stage.get("consumed_meaningful") or 0) == self._monitor_batch_limit
            or self._system_pending("misalignment") > 0
        )

    @staticmethod
    def _is_idle(report: Mapping[str, Any]) -> bool:
        misalignment = report.get("misalignment") or {}
        stages = ("task_quality", "reward_seeker")
        return not (
            report["errors"]
            or misalignment.get("claimed")
            or misalignment.get("consumed_meaningful")
            or (report.get("export") or {}).get("status") == "exported"
            or any(
                (report.get(name) or {}).get("status") in {"processed", "blocked"}
                for name in stages
            )
        )

    def _blocked_by(self, stage: str, report: Mapping[str, Any]) -> list[str]:
        """Higher-priority systems whose pending work defers this stage."""
        if stage == "task_quality":
            return (
                ["misalignment"] if self._misalignment_pending(report) else []
            )
        if stage == "reward_seeker":
            blocked = ["misalignment"] if self._misalignment_pending(report) else []
            if self._system_pending("task_quality") > 0:
                blocked.append("task_quality")
            return blocked
        return []

    def run_step(self) -> dict[str, Any]:
        """One prioritized pass; always emits heartbeats and drains the outbox."""
        self._step += 1
        report: dict[str, Any] = {
            "step": self._step,
            "supervisor_id": self.supervisor_id,
            "observed_at": self._iso(self._now()),
            "stages": [],
            "heartbeats": [],
            "errors": [],
            "relay": None,
            "idle": False,
        }
        try:
            for name, stage in (
                ("misalignment", self._misalignment_stage),
                ("task_quality", self._task_quality_stage),
                ("reward_seeker", self._seeker_stage),
                ("export", self._export_stage),
            ):
                report["stages"].append(name)
                blocked_by = self._blocked_by(name, report)
                if blocked_by:
                    report[name] = {"status": "blocked", "blocked_by": blocked_by}
                    continue
                stage(report)
        finally:
            try:
                snapshot = self._observe()
                report["pending_by_system"] = dict(snapshot["by_system"])
                self._emit_heartbeats(report, snapshot)
            except Exception as exc:
                self._record_error(report, "heartbeat", exc)
            report["relay"] = self._drain_relay()
            self._last_relay = report["relay"]
            report["idle"] = self._is_idle(report)
        return report

    def run_until_idle(self, *, max_steps: int = 50) -> dict[str, Any]:
        """Run steps until one reports ``idle`` or ``max_steps`` bounds the run."""
        _require_positive_int(max_steps, "max_steps")
        reports = []
        for _ in range(max_steps):
            report = self.run_step()
            reports.append(report)
            if report["idle"]:
                break
        return {
            "supervisor_id": self.supervisor_id,
            "steps": len(reports),
            "idle": bool(reports and reports[-1]["idle"]),
            "errors": [error for report in reports for error in report["errors"]],
            "exports": [
                report["export"]["manifest"]
                for report in reports
                if report["export"].get("status") == "exported"
            ],
            "reports": reports,
        }

    def status(self) -> dict[str, Any]:
        """Current liveness projection without dispatching work or emitting events."""
        snapshot = self._observe()
        now = self._now()
        workers = {
            system: self._worker_state(system, snapshot, now)
            for system in WORKER_SYSTEMS
        }
        return {
            "observed_at": self._iso(now),
            "queue": snapshot,
            "workers": workers,
            "stale_workers": sorted(
                system for system, state in workers.items() if state["stale"]
            ),
            "expired_leases": sorted(self._expired),
            "exports_written": len(self._exports),
            "assets_pending": self._pending_assets,
        }
