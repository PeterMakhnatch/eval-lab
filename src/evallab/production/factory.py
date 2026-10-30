"""Local DEV production factory: seed admitted runs once, score them event by event, heartbeat.

The Modal cloud app (``modal_cloud.py``) owns the October 24 production journal and
runs a supervisor step only when someone invokes it; the old local ``prod-live``
LaunchAgent is retired. This module is the continuously running local counterpart for
the monitor: a fresh journal (never the retired one, so there is one owner per
journal), one :class:`~evallab.production.supervisor.ProductionSupervisor` step loop,
the read-only control plane, and three commands around it::

    python -m evallab.production.factory seed    # blinded export directory -> queued jobs
    python -m evallab.production.factory run     # the service (launchd keeps it alive)
    python -m evallab.production.factory watchdog  # exit 0/2 on a fresh/stale heartbeat
    python -m evallab.production.factory measure   # throughput, latency, failures

Guarantees, all inherited from the existing contracts and checked here again:

- **DEV only.** ``seed`` takes a blinded export written by
  ``cloud_trace_export.export_runs`` (or the Terminal Wrench exporter), re-derives the
  admitted trace ids from the store snapshot with the shared gate and refuses a join
  entry outside it, then enqueues through :func:`ingest_public_corpus`, which resolves
  every identity against :class:`CorpusRegistry` (canonical TEST seals apply).
- **Once.** Idempotency keys come from the blind id, so a repeated seed appends
  nothing and no finished run is replayed (the 2026-10-24 service re-queued 33 items
  twelve times). Every run gets its own ``run_id``: sharing one run id merges step
  numbers across runs and forces a rescan per step.
- **No model, no Docker, no spend.** Task Quality and Reward Seeker workers exist only
  because the supervisor needs all three; they use the Modal backend with no approval,
  so any job they could claim stays provisional and unexecuted.
- **Fail loud, never clear.** A stage error is journaled by the supervisor and counted
  per class in ``factory-cycles.jsonl``; the watchdog reports ``stalled`` and ``failed``
  (the live supervisor's launchd source already reads that vocabulary).

External dataset items are not persisted by the registry, so ``run`` re-registers
every seeded item from the journal at start and after each step.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "evallab.factory/v1"
DEFAULT_ROOT = Path.home() / ".local/state/evallab-prod-live/factory"
JOURNAL = "prod-factory.sqlite"
SUPERVISOR_ID = "local-factory"
PREFIX_AUDIT_JOB = "misalignment.prefix_audit_task"
#: A seeded run's own heartbeat can be this old before the watchdog calls it stale.
STALE_AFTER_SECONDS = 180.0
IDLE_SLEEP_SECONDS = 15.0
CONTROL_PLANE_PORT = 8095


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _blind_order(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _write_json_atomic(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------- registry sync


def sync_external_items(journal: Path, registry: Any, after_seq: int = 0) -> int:
    """Register every seeded external item found in the journal; return the last seq seen.

    The registry keeps external items in memory only, and the service runs in another
    process than ``seed``. Canonical dataset ids are never touched, and a seeded
    identity that the registry resolves to TEST stays TEST (``register_external`` pins it).
    """
    from evallab.production.corpus_queue import DATASETS, CorpusItem

    last = after_seq
    query = (
        "SELECT seq, json_extract(envelope, '$.payload.dataset_id'),"
        " json_extract(envelope, '$.payload.item_id'),"
        " json_extract(envelope, '$.payload.split'),"
        " json_extract(envelope, '$.payload.task_id'),"
        " json_extract(envelope, '$.payload.task_digest'),"
        " json_extract(envelope, '$.payload.license')"
        " FROM events WHERE seq > ? AND event_type = ? ORDER BY seq"
    )
    with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=30)) as connection:
        for seq, dataset, item, split, task, digest, license_text in connection.execute(
            query, (after_seq, PREFIX_AUDIT_JOB)
        ):
            last = seq
            if not dataset or not item or dataset in DATASETS:
                continue
            registry.register_external(
                CorpusItem(
                    dataset_id=dataset,
                    item_id=item,
                    split="test" if split in ("test", "heldout") else "dev",
                    task_id=task,
                    task_digest=digest,
                    source_path=Path(dataset),
                    metadata={"license": license_text} if license_text else {},
                    source_record_sha256="",
                )
            )
    return last


# --------------------------------------------------------------------------- seeding


def _admitted_trace_ids(snapshot: Path, repo_root: Path, store_dataset: str) -> frozenset[str]:
    from evallab.production.corpus_queue import CorpusRegistry
    from evallab.production.run_atlas import admitted_task_ids, cloud_trace_ids, connect_readonly

    registry = CorpusRegistry(repo_root)
    with closing(connect_readonly(snapshot)) as connection:
        admitted = admitted_task_ids(connection, registry)
        return frozenset(
            cloud_trace_ids(connection, admitted=admitted, dataset_ids=[store_dataset])
        )


def seed(
    *,
    database: Path,
    export_directory: Path,
    manifest: Path,
    dataset_id: str,
    license_text: str,
    repo_root: Path,
    store_snapshot: Path | None = None,
    store_dataset: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Enqueue blinded runs as ``misalignment.prefix_audit_task`` jobs, one run id each."""
    from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
    from evallab.production.public_corpus_loader import PublicCorpusJob, ingest_public_corpus

    join = json.loads(manifest.read_text(encoding="utf-8"))
    files = sorted(
        (p for p in export_directory.glob("*.json") if p.stem in join),
        key=lambda p: _blind_order(p.stem),
    )
    selected = files[offset : None if limit is None else offset + limit]
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "seeded_at": _now(),
        "database": str(database),
        "dataset_id": dataset_id,
        "export_directory": str(export_directory),
        "manifest_runs": len(join),
        "files": len(files),
        "selected": len(selected),
        "offset": offset,
        "limit": limit,
    }
    if store_snapshot is not None:
        if store_dataset is None:
            raise ValueError("store_dataset is required with store_snapshot")
        admitted = _admitted_trace_ids(store_snapshot, repo_root, store_dataset)
        outside = [p.stem for p in selected if join[p.stem].get("trace_id") not in admitted]
        receipt["admission"] = {
            "gate": "run_atlas.admitted_task_ids + cloud_trace_ids",
            "store_dataset": store_dataset,
            "admitted_in_store": len(admitted),
            "checked": len(selected),
            "refused": len(outside),
        }
        if outside:
            raise ValueError(f"{len(outside)} seeded runs are not admitted DEV traces; refusing")
    registry = CorpusRegistry(repo_root)
    enqueued = excluded = 0
    reasons: Counter[str] = Counter()
    steps = payload_bytes = 0
    started = time.monotonic()
    with ContinuousWorkQueue(database, registry=registry) as queue:
        seq_before = queue.queue_snapshot()["last_seq"]
        for path in selected:
            blind = path.stem
            raw = path.read_bytes()
            document = json.loads(raw)
            payload = {
                "dataset_id": dataset_id,
                "item_id": blind,
                "source_kind": "public_rollout",
                "source_id": blind,
                "steps": document["steps"],
                "license": license_text,
                "provenance": {
                    "loader": "factory_seed",
                    "source_id": blind,
                    "license": license_text,
                    "declared_split": "dev",
                    "source_path": str(path),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                },
            }
            result = ingest_public_corpus(
                queue,
                [PublicCorpusJob(PREFIX_AUDIT_JOB, payload, blind)],
                run_id=f"factory-{blind}",
            )
            enqueued += len(result["enqueued"])
            for item in result["excluded"]:
                excluded += 1
                reasons[item["reason"]] += 1
            steps += len(document["steps"])
            payload_bytes += len(raw)
        appended = queue.queue_snapshot()["last_seq"] - seq_before
    receipt.update(
        resolved=enqueued,
        new_events=appended,
        already_queued=enqueued - appended,
        excluded=excluded,
        excluded_reasons=dict(reasons),
        steps=steps,
        payload_bytes=payload_bytes,
        seconds=round(time.monotonic() - started, 2),
    )
    return receipt


# --------------------------------------------------------------------------- service


class _Hooks:
    """Optional consumers that run after each supervisor step (journal-cursor based)."""

    def __init__(self) -> None:
        self._calls: list[tuple[str, Callable[[], dict[str, Any]]]] = []

    def add(self, name: str, call: Callable[[], dict[str, Any]]) -> None:
        self._calls.append((name, call))

    def run(self) -> tuple[dict[str, Any], list[dict[str, str]]]:
        results: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        for name, call in self._calls:
            try:
                results[name] = call()
            except Exception as exc:  # noqa: BLE001 - a failing hook is journaled, never stops the loop
                errors.append({"stage": name, "error": f"{type(exc).__name__}: {exc}"})
        return results, errors


def _error_class(error: str) -> str:
    return error.partition(":")[0].strip() or "unknown"


class Factory:
    """The supervisor loop over a local journal; ``step()`` is one prioritized pass."""

    def __init__(
        self, root: Path, repo_root: Path, *, port: int | None = CONTROL_PLANE_PORT
    ) -> None:
        self.root = root
        self.repo_root = repo_root
        self.journal = root / JOURNAL
        self._port = port
        self._stack = ExitStack()
        self.hooks = _Hooks()
        self._registered_seq = 0
        self._cycle = 0

    def __enter__(self) -> Factory:
        from evallab.execution_backend import ExecutionBackendConfig
        from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
        from evallab.production.monitor_worker import MisalignmentLiveWorker
        from evallab.production.outbox_relay import LocalJetStreamAdapter, OutboxRelay
        from evallab.production.seeker_worker import RewardSeekerLiveWorker
        from evallab.production.supervisor import ProductionSupervisor
        from evallab.production.tq_worker import TaskQualityLiveWorker

        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._stack.enter_context((self.root / "factory.lock").open("a+"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("factory is already running") from None
        self.registry = CorpusRegistry(self.repo_root)
        self.queue = self._stack.enter_context(
            ContinuousWorkQueue(self.journal, registry=self.registry)
        )
        self._registered_seq = sync_external_items(self.journal, self.registry)
        # Task Quality and the Seeker are constructed because the supervisor wants all
        # three; the Modal backend with no approval keeps any claimed job unexecuted.
        config = ExecutionBackendConfig(backend="modal", allow_local_docker=False)
        monitor = MisalignmentLiveWorker(emit=self.queue.append_event, registry=self.registry)
        self._stack.callback(monitor.close)
        self.monitor = monitor
        quality = TaskQualityLiveWorker(
            self.queue, repo_root=self.repo_root, execution_config=config
        )
        seeker = RewardSeekerLiveWorker(
            self.queue,
            repo_root=self.repo_root,
            work_root=self.root / "seeker",
            execution_config=config,
            allow_paid_lanes=False,
        )
        relay = self._stack.enter_context(
            OutboxRelay(
                self.queue, LocalJetStreamAdapter(self.root / "outbox.sqlite"), batch_limit=2000
            )
        )
        self.supervisor = ProductionSupervisor(
            self.queue,
            monitor=monitor,
            task_quality=quality,
            seeker=seeker,
            relay=relay,
            repo_root=self.repo_root,
            supervisor_id=SUPERVISOR_ID,
        )
        self._stack.callback(self.supervisor.close)
        if self._port:
            self._serve_control_plane()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def _serve_control_plane(self) -> None:
        import uvicorn

        from evallab.production.control_plane import create_app

        server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.queue),
                host="127.0.0.1",
                port=int(self._port or 0),
                log_level="warning",
                access_log=False,
            )
        )
        thread = threading.Thread(target=server.run, name="factory-control-plane", daemon=True)
        thread.start()

        def stop() -> None:
            server.should_exit = True
            thread.join(timeout=10)

        self._stack.callback(stop)

    def step(self) -> dict[str, Any]:
        """One supervisor pass plus hooks; appends one line to ``factory-cycles.jsonl``."""
        started = time.monotonic()
        self._registered_seq = sync_external_items(
            self.journal, self.registry, self._registered_seq
        )
        report = self.supervisor.run_step()
        hooked, hook_errors = self.hooks.run()
        errors = [*report["errors"], *hook_errors]
        snapshot = self.queue.queue_snapshot()
        misalignment = report.get("misalignment") or {}
        self._cycle += 1
        line = {
            "ts": _now(),
            "cycle": self._cycle,
            "seconds": round(time.monotonic() - started, 3),
            "claimed": misalignment.get("claimed"),
            "emitted": misalignment.get("emitted", 0),
            "consumed": misalignment.get("consumed", 0),
            "consumed_meaningful": misalignment.get("consumed_meaningful", 0),
            "idle": bool(report["idle"]),
            "errors": [{**e, "class": _error_class(e["error"])} for e in errors],
            "queue_depth": snapshot["depth"],
            "last_seq": snapshot["last_seq"],
            "hooks": hooked,
        }
        with (self.root / "factory-cycles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        _write_json_atomic(
            self.root / "factory-status.json",
            {"schema": SCHEMA, "pid": os.getpid(), "supervisor_id": SUPERVISOR_ID, **line},
        )
        return line

    def run_forever(self, stop: threading.Event, *, idle_sleep: float = IDLE_SLEEP_SECONDS) -> None:
        while not stop.is_set():
            line = self.step()
            if line["idle"] or line["errors"]:
                stop.wait(idle_sleep)


# --------------------------------------------------------------------------- watchdog


def watchdog(
    root: Path, *, max_age: float = STALE_AFTER_SECONDS, now: float | None = None
) -> dict[str, Any]:
    """Read-only liveness verdict from the journal's newest collector heartbeat."""
    journal = root / JOURNAL
    moment = time.time() if now is None else now
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "checked_at": datetime.fromtimestamp(moment, UTC).isoformat(),
        "max_age_seconds": max_age,
    }
    if not journal.is_file():
        return {**result, "status": "failed", "reason": "no journal"}
    try:
        with closing(
            sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=10)
        ) as connection:
            row = connection.execute(
                "SELECT seq, json_extract(envelope, '$.ts') FROM events"
                " WHERE event_type = 'collector.heartbeat' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            last_seq = connection.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    except sqlite3.Error as exc:
        return {**result, "status": "failed", "reason": f"journal unreadable: {type(exc).__name__}"}
    if row is None:
        return {**result, "status": "failed", "reason": "no heartbeat yet", "last_seq": last_seq}
    age = moment - datetime.fromisoformat(row[1]).timestamp()
    status = "ok" if age <= max_age else "stalled"
    return {
        **result,
        "status": status,
        "heartbeat_seq": row[0],
        "heartbeat_ts": row[1],
        "age_seconds": round(age, 1),
        "last_seq": last_seq,
    }


# --------------------------------------------------------------------------- measurement


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _events(
    journal: Path, event_types: Sequence[str]
) -> Iterator[tuple[int, str, str, str, str | None]]:
    marks = ",".join("?" for _ in event_types)
    query = (
        "SELECT seq, run_id, event_type, json_extract(envelope, '$.ts'),"
        " json_extract(envelope, '$.payload.deterministic_latency_seconds')"
        f" FROM events WHERE event_type IN ({marks}) ORDER BY seq"
    )
    with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=30)) as connection:
        yield from connection.execute(query, tuple(event_types))


def measure(root: Path, *, first_runs: int | None = None) -> dict[str, Any]:
    """Throughput, per-run latency and failure classes from the journal and cycle log.

    Per run: ``admitted`` is the seeded job's event time, ``queue_wait`` runs to the
    run's first scored prefix, ``scoring`` to its last, ``to_first_card`` to its first
    ``monitor.suspicion``. ``first_runs`` keeps only the first N runs to finish scoring
    (by last prefix seq) so a receipt can state "the first 200 runs".
    """
    journal = root / JOURNAL
    admitted: dict[str, float] = {}
    first_score: dict[str, float] = {}
    last_score: dict[str, tuple[int, float]] = {}
    first_card: dict[str, float] = {}
    prefix_latency: list[float] = []
    steps: Counter[str] = Counter()
    cards: Counter[str] = Counter()
    for seq, run_id, kind, ts, latency in _events(
        journal, (PREFIX_AUDIT_JOB, "monitor.prefix_scored", "monitor.suspicion")
    ):
        moment = datetime.fromisoformat(ts).timestamp()
        if kind == PREFIX_AUDIT_JOB:
            admitted[run_id] = moment
        elif kind == "monitor.prefix_scored":
            first_score.setdefault(run_id, moment)
            last_score[run_id] = (seq, moment)
            steps[run_id] += 1
            if latency is not None:
                prefix_latency.append(float(latency))
        else:
            first_card.setdefault(run_id, moment)
            cards[run_id] += 1
    finished = sorted((v[0], run) for run, v in last_score.items() if run in admitted)
    if first_runs is not None:
        finished = finished[:first_runs]
    runs = [run for _, run in finished]
    wait = [first_score[r] - admitted[r] for r in runs]
    scoring = [last_score[r][1] - first_score[r] for r in runs]
    card_latency = [first_card[r] - admitted[r] for r in runs if r in first_card]
    total_steps = sum(steps[r] for r in runs)
    window = max(last_score[r][1] for r in runs) - min(admitted[r] for r in runs) if runs else 0.0
    scored_window = (
        max(last_score[r][1] for r in runs) - min(first_score[r] for r in runs) if runs else 0.0
    )
    cycle_errors: Counter[str] = Counter()
    cycles = 0
    cycles_path = root / "factory-cycles.jsonl"
    if cycles_path.is_file():
        for text in cycles_path.read_text(encoding="utf-8").splitlines():
            line = json.loads(text)
            cycles += 1
            for error in line["errors"]:
                cycle_errors[f"{error['stage']}:{error['class']}"] += 1
    event_total = 0
    with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=30)) as connection:
        event_total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {
        "schema": SCHEMA,
        "runs": len(runs),
        "prefix_scores": total_steps,
        "runs_with_cards": sum(1 for r in runs if r in first_card),
        "cards": sum(cards[r] for r in runs),
        "events_total": event_total,
        "seconds_first_admit_to_last_score": round(window, 1),
        "seconds_first_score_to_last_score": round(scored_window, 1),
        "runs_per_minute": round(len(runs) / (scored_window / 60), 2) if scored_window else None,
        "prefix_scores_per_minute": round(total_steps / (scored_window / 60), 1)
        if scored_window
        else None,
        "events_per_minute": round(
            sum(steps[r] * 2 + cards[r] for r in runs) / (scored_window / 60), 1
        )
        if scored_window
        else None,
        "latency_seconds": {
            "queue_wait_admitted_to_first_score": _summary(wait),
            "scoring_first_to_last_prefix": _summary(scoring),
            "admitted_to_first_card": _summary(card_latency),
            "per_prefix_deterministic": _summary(prefix_latency),
        },
        "cycles": cycles,
        "failures_by_class": dict(cycle_errors),
    }


# --------------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.production.factory", description=__doc__
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help=f"state root (default {DEFAULT_ROOT})"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed_cmd = commands.add_parser(
        "seed", help="enqueue a blinded export as prefix-audit jobs, once"
    )
    seed_cmd.add_argument("--export-directory", type=Path, required=True)
    seed_cmd.add_argument(
        "--manifest", type=Path, required=True, help="private join manifest of the export"
    )
    seed_cmd.add_argument("--dataset-id", required=True)
    seed_cmd.add_argument(
        "--license", dest="license_text", required=True, help="declared source license text"
    )
    seed_cmd.add_argument(
        "--store-snapshot", type=Path, help="re-check each join entry against the shared DEV gate"
    )
    seed_cmd.add_argument(
        "--store-dataset", help="store dataset id the join entries must belong to"
    )
    seed_cmd.add_argument("--limit", type=int)
    seed_cmd.add_argument("--offset", type=int, default=0)
    run_cmd = commands.add_parser(
        "run", help="run the supervisor loop and the read-only control plane"
    )
    run_cmd.add_argument(
        "--port", type=int, default=CONTROL_PLANE_PORT, help="0 disables the control plane"
    )
    run_cmd.add_argument("--idle-sleep", type=float, default=IDLE_SLEEP_SECONDS)
    run_cmd.add_argument("--max-steps", type=int, help="stop after N steps (smoke runs)")
    watch_cmd = commands.add_parser(
        "watchdog", help="exit 0 when the heartbeat is fresh, 2 otherwise"
    )
    watch_cmd.add_argument("--max-age", type=float, default=STALE_AFTER_SECONDS)
    measure_cmd = commands.add_parser("measure", help="throughput, latency and failure classes")
    measure_cmd.add_argument(
        "--first-runs", type=int, help="only the first N runs to finish scoring"
    )
    measure_cmd.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root: Path = args.root.expanduser()
    repo_root = _repo_root()
    if args.command == "seed":
        receipt = seed(
            database=root / JOURNAL,
            export_directory=args.export_directory,
            manifest=args.manifest,
            dataset_id=args.dataset_id,
            license_text=args.license_text,
            repo_root=repo_root,
            store_snapshot=args.store_snapshot,
            store_dataset=args.store_dataset,
            limit=args.limit,
            offset=args.offset,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0
    if args.command == "watchdog":
        verdict = watchdog(root, max_age=args.max_age)
        _write_json_atomic(root / "factory-watchdog.json", verdict)
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict["status"] == "ok" else 2
    if args.command == "measure":
        report = measure(root, first_runs=args.first_runs)
        text = json.dumps(report, sort_keys=True, indent=2)
        if args.out:
            _write_json_atomic(args.out, report)
        print(text)
        return 0
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    with Factory(root, repo_root, port=args.port or None) as factory:
        steps = 0
        while not stop.is_set():
            line = factory.step()
            steps += 1
            if args.max_steps is not None and steps >= args.max_steps:
                break
            if line["idle"] or line["errors"]:
                stop.wait(args.idle_sleep)
    with suppress(OSError):
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
