"""Public corpus custody, sealed splits, and honest executable admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusRegistry,
    IdempotencyConflict,
)
from evallab.production.ingest import (
    ingest_corpus,
    resolve_public_trace,
    resolve_task_package,
)

TB4 = "tb4-audit-v1"
MIMO = "mimo-defect-map-v1"
CHEAT = "cheatbench-actions-v1"
HACK = "hacktrace-act-v1"
LABEL = "EVALUATOR_ONLY_MUST_NOT_REACH_QUEUE"


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _item(identity: str, task: str, split: str = "dev", **extra) -> dict:
    return {
        "item_id": identity,
        "split": split,
        "subject": {"kind": "task", "task_id": task},
        "expected_root_cause": LABEL,
        **extra,
    }


def _actor(results: Path, name: str) -> tuple[dict, str]:
    trial = results / name
    trajectory = trial / "agent/trajectory.json"
    trajectory.parent.mkdir(parents=True)
    raw = json.dumps({
        "schema_version": "ATIF-v1.6",
        "steps": [{"step_id": 1, "source": "agent", "message": "", "tool_calls": [
            {"tool_call_id": "a", "function_name": "terminal.exec", "arguments": {"cmd": "true"}}
        ]}],
    }).encode()
    trajectory.write_bytes(raw)
    return {"kind": "published", "path": name}, hashlib.sha256(raw).hexdigest()


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "corpus"
    results = tmp_path / "results"
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(results))
    keys = root / "research/answer-keys"
    _jsonl(keys / TB4 / "items.jsonl", [
        _item("finding-a", "terminal-bench/task-a"),
        _item("finding-tightened", "terminal-bench/other-heldout"),
        _item("finding-sealed", "terminal-bench/sealed", "test"),
        _item("benchmark-context", "__benchmark__/terminal-bench-4"),
    ])
    _jsonl(keys / TB4 / "tasks.jsonl", [
        _item("unused", "terminal-bench/task-a"),
        _item("unused", "terminal-bench/no-finding"),
        _item("unused", "terminal-bench/other-heldout"),
        _item("unused", "terminal-bench/sealed", "test"),
    ])
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(
        "key_id,item_id,split,task\n"
        "another-key,unrelated-id,heldout,other-heldout\n",
        encoding="utf-8",
    )
    ref, digest = _actor(results, "cheat-dev")
    test_ref, test_digest = _actor(results, "sealed-trace")
    _jsonl(keys / CHEAT / "items.jsonl", [
        _item("cheat-dev", "cheatbench/dev-family", actions_only_ref=ref,
              actions_only_sha256=digest, trial_ref=ref, trajectory_sha256=digest,
              source_revision="pinned-public-revision", source_labels=LABEL),
        _item("cheat-sealed", "cheatbench/sealed-family", "test", actions_only_ref=test_ref,
              actions_only_sha256=test_digest),
    ])
    corrupt_ref, _ = _actor(results, "corrupt")
    _jsonl(keys / HACK / "items.jsonl", [
        _item("hack-missing", "hacktrace/missing", actions_only_ref={"kind": "published", "path": "missing"},
              actions_only_sha256="a" * 64),
        _item("hack-corrupt", "hacktrace/corrupt", actions_only_ref=corrupt_ref,
              actions_only_sha256="b" * 64),
    ])
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": MIMO,
        "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
        "tasks": [
            {"record_id": "mimo-a@sha256:v1", "task_id": "mimo-a", "package_digest": "sha256:v1",
             "historical_semantic_verdict": LABEL},
            {"record_id": "mimo-a@sha256:v2", "task_id": "mimo-a", "package_digest": "sha256:v2"},
            {"record_id": "mimo-held@sha256:v1", "task_id": "mimo-held", "package_digest": "sha256:held"},
            {"record_id": "mimo-held@sha256:v2", "task_id": "mimo-held", "package_digest": "sha256:held2"},
        ],
    }), encoding="utf-8")
    return root, results


def test_all_admissible_native_records_persist_without_labels_or_bogus_jobs(corpus) -> None:
    root, _ = corpus
    # Even a registry with a frozen-eval token cannot implicitly seed TEST.
    registry = CorpusRegistry(root, frozen_eval_token="explicit-not-inherited")
    with ContinuousWorkQueue(":memory:", registry=registry) as queue:
        report = ingest_corpus(queue)
        events = queue.read_events(limit=100)
        admissions = [event for event in events if event["event_type"] == "queue.corpus_admitted"]
        native_ids = {(event["payload"]["dataset_id"], event["payload"]["item_id"]) for event in admissions}
        assert native_ids == {
            (TB4, "finding-a"), (TB4, "benchmark-context"),
            (TB4, "terminal-bench/task-a"), (TB4, "terminal-bench/no-finding"),
            (CHEAT, "cheat-dev"), (HACK, "hack-missing"), (HACK, "hack-corrupt"),
            (MIMO, "mimo-a@sha256:v1"), (MIMO, "mimo-a@sha256:v2"),
        }
        jobs = [event for event in events if event["event_type"].endswith("_task")]
        assert [(job["event_type"], job["payload"]["item_id"]) for job in jobs] == [
            ("misalignment.replay_task", "cheat-dev")
        ]
        assert queue.queue_snapshot()["depth"] == 1
        encoded = json.dumps({"report": report, "events": events})
        assert LABEL not in encoded
        for sealed in ("finding-sealed", "cheat-sealed", "mimo-held", "other-heldout", "sealed-trace"):
            assert sealed not in encoded
        assert all(event["payload"]["split"] == "dev" for event in events)
        assert report["datasets"][TB4]["record_kinds"]["tasks"] == {
            "source": 4, "source_dev": 3, "admitted_dev": 2, "excluded_due_cross_key_seal": 1,
        }
        assert report["datasets"][TB4]["benchmark_context_records"] == 1
        assert report["datasets"][TB4]["excluded_due_cross_key_seal"] == 2
        assert report["datasets"][MIMO]["historical_versions"] == {
            "package_records": 4, "task_ids": 2, "tasks_with_multiple_versions": 2,
            "source_dev_package_records": 2, "eligible_package_records": 2,
            "permanently_heldout_package_records": 2, "permanently_heldout_task_ids": 1,
            "canonical_sealed_package_records": 0, "excluded_package_records": 2,
        }
        split_source = root / report["datasets"][TB4]["split_authority"]["path"]
        assert report["datasets"][TB4]["split_authority"]["sha256"] == (
            "sha256:" + hashlib.sha256(split_source.read_bytes()).hexdigest()
        )
        task_source = root / "research/answer-keys" / TB4 / "tasks.jsonl"
        original_record = json.loads(task_source.read_text().splitlines()[0])
        custody = next(
            event["payload"]["preflight"]["source_record"]
            for event in admissions
            if event["payload"]["item_id"] == "terminal-bench/task-a"
        )
        assert custody["sha256"] == "sha256:" + hashlib.sha256(
            json.dumps(original_record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def test_repeated_seed_and_new_presentation_run_do_not_grow_durable_work(corpus) -> None:
    root, _ = corpus
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        first = ingest_corpus(queue)
        snapshot = queue.queue_snapshot()
        events = queue.read_events(limit=100)
        second = ingest_corpus(queue)
        assert second == first
        assert ingest_corpus(queue, run_id="new-operator-label")["totals"] == first["totals"]
        assert queue.queue_snapshot() == snapshot
        assert queue.read_events(limit=100) == events


def test_changed_source_under_native_identity_is_an_explicit_collision(corpus, tmp_path) -> None:
    root, _ = corpus
    database = tmp_path / "queue.sqlite"
    with ContinuousWorkQueue(database, registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[CHEAT])
        depth = queue.queue_snapshot()["depth"]
    source = root / "research/answer-keys" / CHEAT / "items.jsonl"
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    rows[0]["source_revision"] = "different-public-source-revision"
    _jsonl(source, rows)
    with ContinuousWorkQueue(database, registry=CorpusRegistry(root)) as queue:
        with pytest.raises(IdempotencyConflict, match="source/preflight changed"):
            ingest_corpus(queue, dataset_ids=[CHEAT])
        assert queue.queue_snapshot()["depth"] == depth


def test_missing_corrupt_and_unsafe_actor_refs_never_become_jobs(corpus) -> None:
    root, results = corpus
    registry = CorpusRegistry(root)
    with ContinuousWorkQueue(":memory:", registry=registry) as queue:
        report = ingest_corpus(queue, dataset_ids=[HACK])
        assert report["totals"]["admitted"] == report["totals"]["blocked"] == 2
        assert report["totals"]["queued"] == queue.queue_snapshot()["depth"] == 0
        reasons = {item["preflight"]["public_trajectory"]["reason"] for item in report["datasets"][HACK]["items"]}
        assert any("unavailable locally" in reason for reason in reasons)
        assert any("differs" in reason for reason in reasons)
    assert resolve_public_trace(registry.get(CHEAT, "cheat-sealed"), repo_root=root) is None
    item = registry.get(CHEAT, "cheat-dev")
    item.metadata["actions_only_ref"]["path"] = "../results/cheat-dev"
    assert resolve_public_trace(item, repo_root=root) is None
    item = registry.get(CHEAT, "cheat-dev")
    (results / "cheat-dev/agent/trajectory.json").write_text("changed bytes")
    # The unchanged full-view descriptor is not a corruption escape hatch.
    assert resolve_public_trace(item, repo_root=root) is None


def _package(path: Path, *, compose: bool = False) -> Path:
    (path / "environment").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "task.toml").write_text(
        '[task]\nname = "task-a"\n[environment]\ncpus = 1\nmemory_mb = 512\nstorage_mb = 512\n',
        encoding="utf-8",
    )
    (path / "instruction.md").write_text("Write the requested output.\n")
    (path / "environment/Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
    (path / "tests/test.sh").write_text("exit 0\n")
    if compose:
        (path / "environment/compose.yaml").write_text("services: {}\n")
    return path


@pytest.mark.parametrize("compose", [False, True])
def test_real_read_only_candidate_preflight_is_never_execution_proof(corpus, compose) -> None:
    from evallab.registry import task_directory_digest

    root, _ = corpus
    package = _package(root / "public-package", compose=compose)
    digest = task_directory_digest(package)
    path = root / "research/answer-keys" / TB4 / "tasks.jsonl"
    _jsonl(path, [_item("unused", "terminal-bench/task-a", package={"path": "public-package"},
                        subject={"kind": "task", "task_id": "terminal-bench/task-a", "task_digest": digest})])
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        report = ingest_corpus(queue, dataset_ids=[TB4])
        item = next(row for row in report["datasets"][TB4]["items"] if row["item_id"] == "terminal-bench/task-a")
        preflight = item["preflight"]
        assert preflight["task_package"]["identity_verified"] is True
        assert preflight["task_package"]["observed_digest"] == digest
        assert preflight["docker_local"]["state"] == ("blocked" if compose else "unknown")
        assert preflight["docker_local"]["executed"] is False
        assert preflight["docker_local"]["execution_proof"] is False
        assert preflight["docker_local"]["runtime_qualified"] is False
        if compose:
            assert "Compose" in preflight["docker_local"]["reason"]
        else:
            assert "unmeasured" in preflight["docker_local"]["reason"]
        assert preflight["capabilities"]["task_quality_audit"]["available"] is False
        assert preflight["capabilities"]["reward_seeker"]["available"] is False
        assert queue.queue_snapshot()["depth"] == 0
        assert str(root) not in json.dumps(preflight)


def test_latest_package_locator_cannot_substitute_a_historical_mimo_version(corpus) -> None:
    root, _ = corpus
    package = _package(root / "latest-package")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    document = json.loads(inventory.read_text())
    document["tasks"][0]["materialization"] = {
        "state": "verified_exact_package", "locator": str(package),
        "observed_package_digest": "sha256:v1",
    }
    inventory.write_text(json.dumps(document))
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        report = ingest_corpus(queue, dataset_ids=[MIMO])
        item = next(row for row in report["datasets"][MIMO]["items"] if row["item_id"] == "mimo-a@sha256:v1")
        assert item["preflight"]["task_package"]["available"] is False
        assert "historical digest" in item["preflight"]["task_package"]["reason"]
        assert item["preflight"]["docker_local"]["state"] == "blocked"
        assert report["totals"]["admitted"] == 2
        assert report["totals"]["queued"] == 0


def test_unknown_dataset_rejects_before_writing_events(corpus) -> None:
    root, _ = corpus
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        with pytest.raises(ValueError, match="unknown production dataset"):
            ingest_corpus(queue, dataset_ids=[CHEAT, "not-a-corpus"])
        assert queue.read_events() == []


def test_mimo_frozen_heldout_and_additional_canonical_seals_are_distinct(corpus) -> None:
    root, _ = corpus
    assignments = root / "research/answer-keys/splits/assignments.csv"
    assignments.write_text(
        assignments.read_text() + "another-key,extra-mimo-lock,heldout,mimo-a\n"
    )
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        report = ingest_corpus(queue, dataset_ids=[MIMO])
        dataset = report["datasets"][MIMO]
        assert dataset["source_dev"] == 2
        assert dataset["effective_admitted_dev"] == 0
        assert dataset["excluded_due_cross_key_seal"] == 2
        assert dataset["historical_versions"]["permanently_heldout_package_records"] == 2
        assert dataset["historical_versions"]["excluded_package_records"] == 4
        assert dataset["items"] == queue.read_events() == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"steps":[]}',
        b'{"steps":["not an ATIF step"]}',
        b'{"steps":[{"source":"user","message":"[CheatBench annotation] cheating"}]}',
        b'{"steps":[{"source":"agent","message":"{\\"OUTCOME\\": true}"}]}',
    ],
)
def test_hash_matched_but_nonreplayable_actor_bytes_are_admitted_without_jobs(corpus, raw) -> None:
    root, results = corpus
    actor = results / "cheat-dev/agent/trajectory.json"
    actor.write_bytes(raw)
    source = root / "research/answer-keys" / CHEAT / "items.jsonl"
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    rows[0]["actions_only_sha256"] = hashlib.sha256(raw).hexdigest()
    _jsonl(source, rows)
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        report = ingest_corpus(queue, dataset_ids=[CHEAT])
        assert report["totals"]["admitted"] == report["totals"]["blocked"] == 1
        assert report["totals"]["queued"] == queue.queue_snapshot()["depth"] == 0
        assert len(queue.read_events()) == 1


def test_runtime_package_resolution_requires_exact_registry_custody(corpus) -> None:
    from evallab.registry import task_directory_digest

    root, _ = corpus
    original = _package(root / "public-package")
    other = _package(root / "different-package")
    (other / "instruction.md").write_text("Different exact task version.\n")
    digest = task_directory_digest(original)
    source = root / "research/answer-keys" / TB4 / "tasks.jsonl"
    _jsonl(source, [_item(
        "unused", "terminal-bench/task-a", package={"path": "public-package"},
        subject={"task_id": "terminal-bench/task-a", "task_digest": digest},
    )])
    registry = CorpusRegistry(root)
    item = registry.admit(TB4, "terminal-bench/task-a")
    assert resolve_task_package(item, repo_root=root) == {
        "task_package": "public-package", "task_package_digest": digest,
    }
    item.metadata["package"]["path"] = "different-package"
    assert resolve_task_package(item, repo_root=root) is None
    (original / "instruction.md").write_text("Mutated after admission.\n")
    assert resolve_task_package(registry.admit(TB4, "terminal-bench/task-a"), repo_root=root) is None
