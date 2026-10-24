"""Exercise the production journal against SQLite and an isolated local PostgreSQL.

PostgreSQL cases skip only when server binaries are not installed. The temporary
server listens solely on its private Unix socket; no shared lab database, corpus
bytes, queue, provider credentials, or paid execution are touched.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest

from evallab.production.corpus_queue import (
    DATASETS,
    ContinuousWorkQueue,
    CorpusRegistry,
    IdempotencyConflict,
    LeaseLostError,
    SplitLockError,
)

DATASET = "tb4-audit-v1"
FROZEN_TOKEN = "test-only-explicit-frozen-evaluation"
DEV = {"dataset_id": DATASET, "item_id": "finding-dev"}
TEST = {"dataset_id": DATASET, "item_id": "finding-test"}
REPAIR = "task_quality.repair_task"
REBREAK = "reward_seeker.rebreak_task"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _item(item_id: str, split: str, task: str, digest: str | None = None) -> dict:
    return {"item_id": item_id, "split": split, "subject": {"task_id": task, "task_digest": digest}}


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    _write_jsonl(keys / DATASET / "items.jsonl", [
        _item("finding-dev", "dev", "terminal-bench/dev-task", "sha256:dev"),
        _item("finding-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
    ])
    _write_jsonl(keys / DATASET / "tasks.jsonl", [
        _item("ignored", "dev", "terminal-bench/dev-task", "sha256:dev"),
        _item("ignored", "test", "terminal-bench/sealed-task", "sha256:sealed"),
    ])
    _write_jsonl(keys / "cheatbench-actions-v1/items.jsonl", [
        _item("cheat-dev", "dev", "cheatbench/independent"),
        _item("cheat-test", "test", "cheatbench/held"),
        _item("renamed-sealed", "dev", "cheatbench/renamed", "sha256:sealed"),
    ])
    _write_jsonl(keys / "hacktrace-act-v1/items.jsonl", [
        _item("hack-dev", "dev", "hacktrace/independent"),
        _item("name-alias", "dev", "another-benchmark/sealed-task@v2"),
    ])
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
        "tasks": [
            {"record_id": "mimo-dev@sha256:version1", "task_id": "mimo-dev", "package_digest": "sha256:mimo-dev", "materialization": {"locator": "/immutable/mimo-dev"}},
            {"record_id": "mimo-held@sha256:version1", "task_id": "mimo-held", "package_digest": "sha256:mimo-held"},
            {"record_id": "mimo-hidden@sha256:version1", "task_id": "mimo-hidden", "heldout_excluded_from_training_all_versions": True},
        ],
    }), encoding="utf-8")
    return root


@pytest.fixture
def registry(corpus_root: Path) -> CorpusRegistry:
    return CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)


@pytest.fixture(scope="module")
def postgres_server() -> Iterator[str]:
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("local PostgreSQL server binaries are not installed")
    with tempfile.TemporaryDirectory(prefix="cq-postgres-") as directory:
        root = Path(directory).resolve()
        data = root / "data"
        subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "--no-locale", "--encoding=UTF8"], check=True, capture_output=True, text=True)
        subprocess.run([pg_ctl, "-D", str(data), "-l", str(root / "server.log"), "-o", f"-F -h '' -k {root} -p 5432", "-w", "start"], check=True, capture_output=True, text=True)
        try:
            yield f"postgresql://postgres@/postgres?host={quote(str(root), safe='')}"
        finally:
            subprocess.run([pg_ctl, "-D", str(data), "-m", "fast", "-w", "stop"], check=True, capture_output=True, text=True)


@pytest.fixture(params=("sqlite", "postgres"))
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str | Path]:
    if request.param == "sqlite":
        yield tmp_path / "production.sqlite"
        return
    import psycopg
    server = request.getfixturevalue("postgres_server")
    schema = "corpus_queue_" + uuid4().hex
    with psycopg.connect(server, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        try:
            yield server + "&options=" + quote(f"-csearch_path={schema}", safe="")
        finally:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def queue(database: str | Path, registry: CorpusRegistry) -> Iterator[ContinuousWorkQueue]:
    with ContinuousWorkQueue(database, registry=registry) as result:
        yield result


def _event(event_type: str, payload: dict | None = None, *, status: str = "confirmed", event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or str(uuid4()), "run_id": "run-1",
        "system": "reward_seeker" if event_type.startswith("seeker.") else "task_quality",
        "event_type": event_type, "seq": 999, "ts": "2026-10-24T00:00:00+00:00",
        "prefix_step": None, "status": status, "payload": dict(DEV if payload is None else payload),
    }


def _exploit(**changes) -> dict:
    return _event("seeker.exploit_confirmed", {**DEV, "upstream_reward": 1.0, "repaired_reward": 0.0, "exploit_witness": "cas:real-proof", **changes})


def _qualified(**changes) -> dict:
    return _event("task_quality.repair_qualified", {
        **DEV, "ref_passes": 3, "nop": 0, "exploit": 0,
        "reference_trials": 3, "nop_trials": 3, "exploit_trials": 3,
        "repaired_task": "/immutable/repaired-task", **changes,
    })


def test_registry_indexes_all_four_public_corpora_and_exact_mimo_versions(registry: CorpusRegistry) -> None:
    assert {item.dataset_id for item in registry.items()} == set(DATASETS)
    assert registry.get(DATASET, "terminal-bench/dev-task").task_id == "terminal-bench/dev-task"
    mimo = registry.get("mimo-defect-map-v1", "mimo-dev@sha256:version1")
    assert mimo.metadata["materialization"]["locator"] == "/immutable/mimo-dev"
    assert mimo.source_path.name == "inventory.json"
    with pytest.raises(KeyError):
        registry.get("mimo-defect-map-v1", "mimo-dev")


def test_registry_dev_admission_and_test_split_rejection(registry: CorpusRegistry) -> None:
    assert registry.admit(**DEV).split == "dev"
    for token in (None, "wrong-token"):
        with pytest.raises(SplitLockError, match="TEST split is sealed"):
            registry.admit(**TEST, frozen_eval_token=token)
    assert registry.admit(**TEST, frozen_eval_token=FROZEN_TOKEN).split == "test"
    assert all(item.split == "dev" for item in registry.items(split="DEV"))


def test_frozen_token_is_explicit_not_truthy_or_self_authorizing(corpus_root: Path) -> None:
    registry = CorpusRegistry(corpus_root)
    for token in (FROZEN_TOKEN, "true", "allow-test", ""):
        with pytest.raises(SplitLockError):
            registry.admit(**TEST, frozen_eval_token=token)


@pytest.mark.parametrize("dataset,item_id", [
    ("mimo-defect-map-v1", "mimo-held@sha256:version1"),
    ("mimo-defect-map-v1", "mimo-hidden@sha256:version1"),
    ("cheatbench-actions-v1", "renamed-sealed"),
    ("hacktrace-act-v1", "name-alias"),
])
def test_test_lock_follows_heldout_task_name_and_digest(registry: CorpusRegistry, dataset: str, item_id: str) -> None:
    assert registry.get(dataset, item_id).split == "test"
    with pytest.raises(SplitLockError):
        registry.admit(dataset, item_id)


def test_canonical_assignment_can_only_tighten_a_split(corpus_root: Path) -> None:
    path = corpus_root / "research/answer-keys/splits/assignments.csv"
    path.parent.mkdir()
    path.write_text("key_id,item_id,task,split\n"
        "tb4-audit-v1,finding-dev,terminal-bench/dev-task,test\n"
        "tb4-audit-v1,finding-test,terminal-bench/sealed-task,dev\n", encoding="utf-8")
    registry = CorpusRegistry(corpus_root)
    for ref in (DEV, TEST):
        with pytest.raises(SplitLockError):
            registry.admit(**ref)


def test_registry_metadata_cannot_be_mutated_to_relabel_later_admission(registry: CorpusRegistry) -> None:
    item = registry.get(**TEST)
    item.metadata["split"] = "dev"
    item.metadata["subject"]["task_id"] = "new-name"
    assert registry.get(**TEST).metadata["split"] == "test"
    with pytest.raises(SplitLockError):
        registry.admit(**TEST)


def test_missing_and_unknown_split_indices_fail_closed(corpus_root: Path) -> None:
    path = corpus_root / "research/answer-keys/cheatbench-actions-v1/items.jsonl"
    _write_jsonl(path, [_item("unknown", "train", "cheatbench/unknown")])
    with pytest.raises(ValueError, match="unsupported corpus split"):
        CorpusRegistry(corpus_root)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        CorpusRegistry(corpus_root)


def test_real_local_public_corpus_indices_are_readable() -> None:
    registry = CorpusRegistry(Path(__file__).resolve().parents[1])
    assert {item.dataset_id for item in registry.items()} == set(DATASETS)
    assert len(registry.items(dataset_id="mimo-defect-map-v1")) == 567
    assert registry.items(dataset_id=DATASET, split="dev")
    assert registry.items(dataset_id=DATASET, split="test")
    dev = registry.items(dataset_id=DATASET, split="dev")[0]
    sealed = registry.items(dataset_id=DATASET, split="test")[0]
    assert registry.admit(dev.dataset_id, dev.item_id).split == "dev"
    with pytest.raises(SplitLockError):
        registry.admit(sealed.dataset_id, sealed.item_id)


def test_queue_dev_admission_test_rejection_and_no_relabeling(queue: ContinuousWorkQueue) -> None:
    dev = queue.enqueue(REPAIR, {**DEV, "split": "test"}, run_id="dev", idempotency_key="dev")
    assert dev["payload"]["split"] == "dev"
    assert dev["seq"] == 1
    for token in (None, "incorrect"):
        with pytest.raises(SplitLockError):
            queue.enqueue(REPAIR, {**TEST, "split": "dev"}, run_id="test", idempotency_key="test", frozen_eval_token=token)
    with pytest.raises(SplitLockError):
        queue.append_event({**_event(REPAIR, {**TEST, "split": "dev"}), "system": "queue", "status": "applied"})
    assert len(queue.read_events()) == len(queue.pending_outbox()) == 1
    sealed = queue.enqueue(REPAIR, {**TEST, "split": "dev"}, run_id="frozen", idempotency_key="frozen", frozen_eval_token=FROZEN_TOKEN)
    assert sealed["payload"]["split"] == "test"
    assert FROZEN_TOKEN not in json.dumps(queue.read_events())


def test_unknown_or_incomplete_corpus_reference_cannot_enter_job_queue(queue: ContinuousWorkQueue) -> None:
    for payload in ({}, {"dataset_id": DATASET}, {"item_id": "finding-test"}):
        with pytest.raises(ValueError):
            queue.enqueue(REPAIR, payload, run_id="run", idempotency_key=str(payload))
    with pytest.raises(KeyError):
        queue.enqueue(REPAIR, {**DEV, "item_id": "unregistered"}, run_id="run", idempotency_key="unknown")
    assert queue.read_events() == queue.pending_outbox() == []


def test_automatic_exploit_repair_rebreak_job_chaining(queue: ContinuousWorkQueue) -> None:
    exploit = queue.append_event(_exploit(), idempotency_key="confirmed-exploit")
    repair = queue.claim("quality-worker", job_types=(REPAIR,))
    assert repair is not None and repair["event_type"] == REPAIR
    assert repair["payload"]["source_event_id"] == exploit["event_id"]
    assert repair["payload"]["exploit_witness"] == "cas:real-proof"
    assert repair["payload"]["dataset_id"] == DATASET
    qualified = queue.append_event(_qualified(), idempotency_key="qualified-repair")
    queue.ack(repair["event_id"], lease_token=repair["lease_token"])
    rebreak = queue.claim("seeker-worker", job_types=(REBREAK,))
    assert rebreak is not None and rebreak["event_type"] == REBREAK
    assert rebreak["payload"]["source_event_id"] == qualified["event_id"]
    assert rebreak["payload"]["repaired_task"] == "/immutable/repaired-task"
    assert [row["seq"] for row in queue.read_events()] == [1, 2, 3, 4]
    assert len(queue.pending_outbox()) == 4
    assert queue.queue_snapshot()["jobs"] == {"queued": 0, "leased": 1, "completed": 1}


def test_nonqualifying_and_provisional_evidence_never_chains(queue: ContinuousWorkQueue) -> None:
    bad = [
        _exploit(upstream_reward=0.0), _exploit(repaired_reward=1.0),
        _exploit(upstream_reward=True), _qualified(ref_passes=2),
        _qualified(nop=1), _qualified(exploit=1),
        _qualified(reference_trials=2), _qualified(nop_trials=1),
        _qualified(exploit_trials=None), _qualified(ref_passes=True),
    ]
    missing_counts = _qualified()
    missing_counts["payload"].pop("reference_trials")
    bad.extend([missing_counts, {**_exploit(), "status": "provisional"}, {**_qualified(), "status": "refuted"}])
    for event in bad:
        queue.append_event(event)
    assert queue.claim("worker", job_types=(REPAIR, REBREAK)) is None
    assert len(queue.read_events()) == len(bad)


def test_idempotency_and_conflicting_reuse_are_transactional(queue: ContinuousWorkQueue) -> None:
    event = _exploit()
    first = queue.append_event(event, idempotency_key="exploit")
    assert queue.append_event({**event, "seq": -2}, idempotency_key="exploit") == first
    assert queue.append_event({**event, "event_id": "retry-with-new-id", "ts": "2026-10-24T12:00:00Z"}, idempotency_key="exploit") == first
    with pytest.raises(IdempotencyConflict):
        queue.append_event({**event, "payload": {**event["payload"], "repaired_reward": 1.0}}, idempotency_key="exploit")
    with pytest.raises(IdempotencyConflict):
        queue.append_event(event, idempotency_key="different-key")
    assert len(queue.read_events()) == len(queue.pending_outbox()) == 2
    assert queue.queue_snapshot()["depth"] == 1


def test_enqueue_retry_is_stable_even_when_wall_clock_changes(queue: ContinuousWorkQueue) -> None:
    first = queue.enqueue(REPAIR, DEV, run_id="run-1", idempotency_key="job")
    assert queue.enqueue(REPAIR, DEV, run_id="run-1", idempotency_key="job") == first
    with pytest.raises(IdempotencyConflict):
        queue.enqueue(REPAIR, {**DEV, "changed": True}, run_id="run-1", idempotency_key="job")
    assert len(queue.read_events()) == 1


def test_child_admission_failure_rolls_back_parent_and_sequence(queue: ContinuousWorkQueue) -> None:
    event = _exploit()
    event["payload"].pop("dataset_id")
    event["payload"].pop("item_id")
    with pytest.raises(ValueError, match="dataset_id"):
        queue.append_event(event)
    assert queue.read_events() == queue.pending_outbox() == []
    assert queue.append_event(_event("task_quality.observed", {}))["seq"] == 1


def test_outbox_insert_failure_rolls_back_event(queue: ContinuousWorkQueue) -> None:
    with queue._transaction(write=True):
        queue._execute("ALTER TABLE outbox ADD CONSTRAINT reject_all CHECK (seq < 0)" if queue._postgres else "CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END")
    with pytest.raises(Exception, match="reject_all|injected outbox failure"):
        queue.append_event(_event("task_quality.observed", {}))
    assert queue.read_events() == queue.pending_outbox() == []
    assert queue.queue_snapshot()["last_seq"] == 0


def test_event_journal_is_append_only_at_database_boundary(queue: ContinuousWorkQueue) -> None:
    event = queue.append_event(_event("task_quality.observed", {}))
    for sql in ("UPDATE events SET run_id = 'changed'", "DELETE FROM events"):
        with pytest.raises(Exception, match="append-only"), queue._transaction(write=True):
            queue._execute(sql)
    if queue._postgres:
        with pytest.raises(Exception, match="append-only"), queue._transaction(write=True):
            queue._execute("TRUNCATE events CASCADE")
    assert queue.get_event(event["event_id"]) == event


def test_durable_cursors_outbox_and_jobs_survive_reopening(database: str | Path, registry: CorpusRegistry) -> None:
    with ContinuousWorkQueue(database, registry=registry) as queue:
        first = queue.append_event(_event("task_quality.observed", {}))
        second = queue.enqueue(REPAIR, DEV, run_id="run-1", idempotency_key="job")
        assert queue.read_consumer("ui")[0] == first
        queue.ack_consumer("ui", first["seq"])
        queue.mark_published(first["event_id"])
    with ContinuousWorkQueue(database, registry=registry) as resumed:
        assert resumed.read_consumer("ui") == [second]
        assert resumed.read_consumer("new-consumer") == [first, second]
        assert resumed.pending_outbox() == [second]
        resumed.ack_consumer("ui", second["seq"])
        resumed.ack_consumer("ui", first["seq"])
        assert resumed.read_consumer("ui") == []
        with pytest.raises(ValueError, match="committed"):
            resumed.ack_consumer("ui", 100)
        claimed = resumed.claim("worker", job_types=(REPAIR,))
        assert claimed is not None and claimed["event_id"] == second["event_id"]


def test_ordered_event_read_resume_filter_and_publication_independence(queue: ContinuousWorkQueue) -> None:
    first = queue.enqueue(REPAIR, DEV, run_id="run-1", idempotency_key="one")
    second = queue.append_event({**_event("task_quality.observed", {}), "run_id": "run-2"})
    assert queue.event_seq(first["event_id"]) == first["seq"]
    assert queue.event_seq("unknown") is None and queue.get_event("unknown") is None
    assert queue.read_events(after_event_id=first["event_id"]) == [second]
    assert queue.read_events(run_id="run-1", limit=1) == [first]
    with pytest.raises(KeyError):
        queue.read_events(after_event_id="unknown")
    queue.mark_published(first["event_id"])
    queue.mark_published(first["event_id"])
    assert queue.pending_outbox() == [second]
    claimed = queue.claim("worker", job_types=(REPAIR,))
    queue.ack(first["event_id"], lease_token=claimed["lease_token"])
    assert queue.pending_outbox() == [second]
    assert queue.read_events() == [first, second]


def test_leases_release_renew_expiry_and_stale_worker_fencing(queue: ContinuousWorkQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("evallab.production.corpus_queue.time.time", lambda: now[0])
    job = queue.enqueue(REPAIR, DEV, run_id="run", idempotency_key="job")
    first = queue.claim("worker-one", job_types=(REPAIR,), lease_seconds=10)
    assert first is not None
    assert queue.claim("worker-two", job_types=(REPAIR,)) is None
    queue.renew(job["event_id"], lease_token=first["lease_token"], lease_seconds=20)
    now[0] = 1011
    assert queue.claim("worker-two", job_types=(REPAIR,)) is None
    now[0] = 1020
    assert queue.queue_snapshot()["jobs"]["queued"] == 1
    second = queue.claim("worker-two", job_types=(REPAIR,))
    assert second is not None and second["lease_token"] != first["lease_token"]
    for operation in (queue.ack, queue.release, queue.renew):
        with pytest.raises(LeaseLostError):
            operation(job["event_id"], lease_token=first["lease_token"])
    queue.release(job["event_id"], lease_token=second["lease_token"])
    third = queue.claim("worker-three", job_types=(REPAIR,))
    queue.ack(job["event_id"], lease_token=third["lease_token"])
    queue.ack(job["event_id"], lease_token=third["lease_token"])
    assert queue.claim("worker-four", job_types=(REPAIR,)) is None
    assert queue.queue_snapshot()["jobs"] == {"queued": 0, "leased": 0, "completed": 1}


def test_snapshot_grouping_reflects_active_workers_and_expired_leases(queue: ContinuousWorkQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [2000.0]
    monkeypatch.setattr("evallab.production.corpus_queue.time.time", lambda: now[0])
    queue.enqueue(REPAIR, DEV, run_id="run", idempotency_key="repair")
    queue.enqueue(REBREAK, DEV, run_id="run", idempotency_key="rebreak")
    queue.claim("quality", job_types=(REPAIR,), lease_seconds=2)
    snapshot = queue.queue_snapshot()
    assert snapshot["depth"] == 2
    assert snapshot["jobs"] == {"queued": 1, "leased": 1, "completed": 0}
    assert snapshot["by_system"] == {"task_quality": 1, "reward_seeker": 1, "misalignment": 0}
    assert snapshot["by_job_type"][REPAIR]["leased"] == 1
    assert snapshot["active_workers"]["task_quality"] == ["quality"]
    assert snapshot["active_leases"][0]["worker_id"] == "quality"
    now[0] = 2002.0
    assert queue.queue_snapshot()["active_leases"] == []
    assert queue.queue_snapshot()["jobs"]["queued"] == 2


def test_concurrent_connections_deduplicate_results_and_exclusively_claim(database: str | Path, registry: CorpusRegistry) -> None:
    with ContinuousWorkQueue(database, registry=registry) as first, ContinuousWorkQueue(database, registry=registry) as second:
        event = _exploit()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(queue.append_event, event, idempotency_key="same-result") for queue in (first, second)]
            assert futures[0].result() == futures[1].result()
            claims = [executor.submit(queue.claim, str(index), job_types=(REPAIR,)) for index, queue in enumerate((first, second))]
            claimed = [future.result() for future in claims]
        assert sum(result is not None for result in claimed) == 1
        assert [row["seq"] for row in first.read_events()] == [1, 2]
        assert len(first.pending_outbox()) == 2


def test_concurrent_bootstrap_allocates_one_committed_global_order(database: str | Path, registry: CorpusRegistry) -> None:
    def start(index: int) -> int:
        with ContinuousWorkQueue(database, registry=registry) as queue:
            return queue.append_event(_event("task_quality.observed", {"index": index}))["seq"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(start, range(4))) == [1, 2, 3, 4]
    with ContinuousWorkQueue(database, registry=registry) as queue:
        assert [event["seq"] for event in queue.read_consumer("fresh-consumer")] == [1, 2, 3, 4]


def test_invalid_envelopes_and_nonfinite_leases_do_not_mutate_journal(queue: ContinuousWorkQueue) -> None:
    for change in ({"status": "invented"}, {"system": "other"}, {"prefix_step": -1}, {"ts": "2026-10-24T00:00:00"}, {"payload": []}, {"payload": {"bad": float("nan")}}):
        with pytest.raises(ValueError):
            queue.append_event({**_event("task_quality.observed", {}), **change})
    for duration in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            queue.claim("worker", job_types=(REPAIR,), lease_seconds=duration)
    with pytest.raises(ValueError):
        queue.claim("worker", job_types=REPAIR)
    with pytest.raises(ValueError):
        queue.read_events(limit=0)
    assert queue.read_events() == []


def test_sqlite_url_and_raw_sqlite_immutability(tmp_path: Path, registry: CorpusRegistry) -> None:
    path = tmp_path / "url.sqlite"
    with ContinuousWorkQueue(f"sqlite:///{path}", registry=registry) as queue:
        queue.append_event(_event("task_quality.observed", {}))
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
