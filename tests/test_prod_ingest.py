"""Public corpus custody, sealed splits, and honest executable admission."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusRegistry,
    IdempotencyConflict,
    SplitLockError,
)
from evallab.production.ingest import (
    ingest_corpus,
    preflight_summary,
    refresh_corpus_preflight,
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
        f"{TB4},unrelated-id,heldout,other-heldout\n",
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
        assignments.read_text() + f"{TB4},extra-mimo-lock,heldout,mimo-a\n"
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


def _native_refresh_assets(root: Path) -> tuple[Path, Path, Path, str]:
    from evallab.candidate_port import PM_FIXED_PIN
    from evallab.registry import task_directory_digest
    from evallab.upstream_grader import _tree_digest

    original = _package(root / "canonical-export/task-a")
    config = original / "task.toml"
    config.write_text('artifacts = ["/app/result.txt"]\n' + config.read_text())
    fixed = root / "retained-port/task-a"
    fixed.mkdir(parents=True)
    files = {
        "Containerfile": "FROM scratch\nCOPY root_data/ /root_data/\n",
        "pyproject.toml": '[project]\nname="environment"\nversion="0.1.0"\n',
        "uv.lock": "version = 1\n",
        ".manifest.json": "{}\n",
        "root_data/.gitkeep": "",
        "src/environment/tasks/task_a/__init__.py": (
            'class NativeTask(Task):\n'
            '    id = "task-a"\n'
            '    def required_hardware(self):\n'
            '        return "cpus=1,memory_mb=512,storage_mb=512"\n'
            '    def judge(self):\n'
            '        return ExecutableJudge(["python", "scoring_script.py"])\n'
        ),
        "src/environment/tasks/task_a/scoring_script.py": "raise SystemExit(0)\n",
    }
    cache = root / "offline-assets"
    cache.mkdir()
    raw = b"immutable public setup input"
    digest = hashlib.sha256(raw).hexdigest()
    (cache / digest).write_bytes(raw)
    files["setup_data.py"] = (
        "def setup(ctx):\n"
        f'    ctx.fetch("https://example.invalid/public-asset", ctx.root_data / "blob.bin", "{digest}")\n'
    )
    for name, text in files.items():
        path = fixed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subject = {
        "kind": "task", "task_id": "terminal-bench/task-a",
        "task_digest": task_directory_digest(original),
    }
    package = {"path": original.relative_to(root).as_posix()}
    keys = root / "research/answer-keys" / TB4
    _jsonl(keys / "tasks.jsonl", [_item("unused", subject["task_id"], subject=subject, package=package)])
    _jsonl(keys / "items.jsonl", [_item("finding-a", subject["task_id"], subject=subject, package=package)])
    # The inventory's Git upstream is deliberately a different package. It is
    # only a port locator; it cannot replace the admitted canonical HF export.
    other = _package(root / "different-git-source/task-a")
    (other / "instruction.md").write_text("Different upstream Git version.\n")
    inventory = root / "research/experiments/candidate-execution-20261021/static-inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "schema": "evallab.candidate_execution_inventory/v1",
        "source_roots": {"port": str(fixed.parent), "upstream": str(other.parent)},
        "tasks": [{
            "id": "task-a", "port_task": str(fixed), "upstream_task": str(other),
            "port_exact_tree_digest": _tree_digest(fixed),
            "upstream_exact_tree_digest": _tree_digest(other),
            "source_pins": {"port_required": PM_FIXED_PIN},
            "fixed_port_native": {"status": "unsupported", "reason": LABEL},
        }],
    }))
    return original, fixed, cache, digest


def test_refresh_only_observes_existing_admissions_and_preserves_original_envelopes(corpus) -> None:
    from evallab.upstream_grader import _tree_digest

    root, _ = corpus
    original, _, cache, digest = _native_refresh_assets(root)
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        assert refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="not-a-seed")["totals"] == {
            "inspected": 0, "recorded": 0, "unchanged": 0,
        }
        assert queue.corpus_preflight_events() == []
        ingest_corpus(queue, dataset_ids=[TB4])
        baseline = queue.corpus_preflight_events()
        summary = preflight_summary(queue)
        assert summary["totals"]["native_port"]["not_observed"] == 2
        source = root / "research/answer-keys" / TB4 / "tasks.jsonl"
        rows = [json.loads(line) for line in source.read_text().splitlines()]
        _jsonl(source, [*rows, _item("unused", "terminal-bench/new-unadmitted")])
        first = refresh_corpus_preflight(
            queue, dataset_ids=[TB4], run_id="observed-one", asset_cache="offline-assets"
        )
        assert first["totals"] == {"inspected": 2, "recorded": 2, "unchanged": 0}
        assert queue.queue_snapshot()["depth"] == 0
        events = queue.corpus_preflight_events()
        assert events[:len(baseline)] == baseline
        assert all(event["event_type"] == "queue.corpus_preflight_updated" for event in events[len(baseline):])
        assert "new-unadmitted" not in json.dumps(first)
        assert LABEL not in json.dumps({"report": first, "events": events})
        for item in first["datasets"][TB4]["items"]:
            observation = item["preflight"]
            native = observation["native_port"]
            assert observation["schema"] == "production.corpus-preflight/v2"
            assert observation["task_package"]["path"] == "canonical-export/task-a"
            assert native["state"] == "described"
            assert native["upstream_exact_tree_digest"] == _tree_digest(original)
            assert native["setup_data"]["asset_count"] == 1
            assert native["setup_data"]["input_sha256s"] == [digest]
            assert set(native["adapter"]["sources"]) == {
                "evallab.candidate_port", "evallab.upstream_grader",
            }
            for flags in (first, observation, native):
                assert flags["execution_proof"] is flags["runtime_qualified"] is flags["executed"] is False
        second = refresh_corpus_preflight(
            queue, dataset_ids=[TB4], run_id="other-presentation", asset_cache=cache
        )
        assert second["totals"] == {"inspected": 2, "recorded": 0, "unchanged": 2}
        assert queue.corpus_preflight_events() == events
        assert preflight_summary(queue)["totals"]["native_port"] == {
            "described": 2, "blocked": 0, "unavailable": 0, "not_observed": 0,
        }


def test_refresh_never_substitutes_the_static_inventory_git_upstream(corpus) -> None:
    root, _ = corpus
    original, _, cache, _ = _native_refresh_assets(root)
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[TB4])
        original.rename(original.with_name("moved-task-a"))
        report = refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="missing-export", asset_cache=cache)
        for item in report["datasets"][TB4]["items"]:
            assert item["preflight"]["task_package"]["available"] is False
            assert item["preflight"]["native_port"]["state"] == "unavailable"
            assert "different-git-source" not in json.dumps(item)


def test_refresh_retains_aba_support_changes_without_new_work(corpus) -> None:
    root, _ = corpus
    _, _, cache, digest = _native_refresh_assets(root)
    blob = cache / digest
    data = blob.read_bytes()
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[TB4])
        before = queue.corpus_preflight_events()
        refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="A", asset_cache=cache)
        blob.unlink()
        blocked = refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="B", asset_cache=cache)
        assert {item["preflight"]["native_port"]["state"] for item in blocked["datasets"][TB4]["items"]} == {"blocked"}
        blob.write_bytes(data)
        restored = refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="A-again", asset_cache=cache)
        assert restored["totals"]["recorded"] == 2
        events = queue.corpus_preflight_events()
        assert events[:len(before)] == before
        for admission in before:
            history = [
                event for event in events
                if event["payload"].get("admission_event_id") == admission["event_id"]
            ]
            assert [event["payload"]["preflight"]["native_port"]["state"] for event in history] == [
                "described", "blocked", "described",
            ]
            assert history[0]["payload"]["observation_sha256"] == history[2]["payload"]["observation_sha256"]
            assert len({event["event_id"] for event in history}) == 3
            assert history[2]["payload"]["previous_event_id"] == history[1]["event_id"]
        assert queue.queue_snapshot()["depth"] == 0


def test_concurrent_identical_refreshes_share_one_durable_observation(corpus, tmp_path) -> None:
    root, _ = corpus
    _, _, cache, _ = _native_refresh_assets(root)
    database = tmp_path / "refresh.sqlite"
    with ContinuousWorkQueue(database, registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[TB4])
        before = queue.corpus_preflight_events()
    start = Barrier(2)

    def refresh(index: int) -> dict:
        with ContinuousWorkQueue(database, registry=CorpusRegistry(root)) as queue:
            start.wait(timeout=10)
            return refresh_corpus_preflight(
                queue, dataset_ids=[TB4], run_id=f"concurrent-{index}", asset_cache=cache
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        reports = list(workers.map(refresh, range(2)))
    assert sum(report["totals"]["recorded"] for report in reports) == 2
    assert sum(report["totals"]["unchanged"] for report in reports) == 2
    with ContinuousWorkQueue(database, registry=CorpusRegistry(root)) as queue:
        events = queue.corpus_preflight_events()
        assert len(events) == len(before) + 2
        assert events[:len(before)] == before
        assert preflight_summary(queue)["totals"]["observed"] == 2
        assert queue.queue_snapshot()["depth"] == 0


@pytest.mark.parametrize("change", ["source", "canonical-seal", "mimo-permanent-seal"])
def test_refresh_rechecks_current_source_custody_and_seals_on_an_existing_queue(corpus, change) -> None:
    root, _ = corpus
    dataset = MIMO if change == "mimo-permanent-seal" else TB4
    with ContinuousWorkQueue(
        ":memory:", registry=CorpusRegistry(root, frozen_eval_token="not-refresh-authority")
    ) as queue:
        ingest_corpus(queue, dataset_ids=[dataset])
        before = queue.corpus_preflight_events()
        if change == "source":
            path = root / "research/answer-keys" / TB4 / "items.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["expected_root_cause"] = "changed evaluator-only source record"
            _jsonl(path, rows)
            error = IdempotencyConflict
        elif change == "canonical-seal":
            path = root / "research/answer-keys/splits/assignments.csv"
            path.write_text(path.read_text() + f"{TB4},new-seal,test,task-a\n")
            error = SplitLockError
        else:
            path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
            document = json.loads(path.read_text())
            document["heldout_task_ids_excluded_from_training_all_versions"].append("mimo-a")
            path.write_text(json.dumps(document))
            error = SplitLockError
        with pytest.raises(error):
            refresh_corpus_preflight(queue, dataset_ids=[dataset], run_id="must-fail-closed")
        assert queue.corpus_preflight_events() == before


@pytest.mark.parametrize("change", ["canonical-package", "retained-port", "repinned-port", "source-mode"])
def test_refresh_never_versions_same_path_frozen_source_drift(corpus, change) -> None:
    from evallab.upstream_grader import _tree_digest

    root, _ = corpus
    original, fixed, cache, _ = _native_refresh_assets(root)
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[TB4])
        refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="first", asset_cache=cache)
        before = queue.corpus_preflight_events()
        if change == "canonical-package":
            (original / "instruction.md").write_text("Changed benchmark version.\n")
        elif change == "source-mode":
            path = original / "instruction.md"
            path.chmod(path.stat().st_mode ^ 0o100)
        else:
            (fixed / "uv.lock").write_text("version = 1\n# changed fixed scorer\n")
            if change == "repinned-port":
                path = root / "research/experiments/candidate-execution-20261021/static-inventory.json"
                document = json.loads(path.read_text())
                document["tasks"][0]["port_exact_tree_digest"] = _tree_digest(fixed)
                path.write_text(json.dumps(document))
        with pytest.raises(IdempotencyConflict):
            refresh_corpus_preflight(queue, dataset_ids=[TB4], run_id="not-a-new-version", asset_cache=cache)
        assert queue.corpus_preflight_events() == before


def test_summary_projects_more_than_one_hundred_items_without_probes_or_trace_reads(corpus) -> None:
    root, results = corpus
    tasks = [_item("unused", f"terminal-bench/bulk-{index:03d}") for index in range(130)]
    _jsonl(root / "research/answer-keys" / TB4 / "tasks.jsonl", tasks)
    with ContinuousWorkQueue(":memory:", registry=CorpusRegistry(root)) as queue:
        ingest_corpus(queue, dataset_ids=[TB4, CHEAT])
        admitted = len(queue.corpus_preflight_events())
        assert admitted > 100
        initial = preflight_summary(queue)
        assert initial["totals"]["admitted"] == admitted
        assert initial["totals"]["native_port"]["not_observed"] == admitted
        report = refresh_corpus_preflight(queue, dataset_ids=[TB4, CHEAT], run_id="bulk")
        assert report["totals"]["recorded"] == admitted
        observed = preflight_summary(queue)
        events = queue.corpus_preflight_events()
        trace = results / "cheat-dev/agent/trajectory.json"
        trace.unlink()
        queue.append_event({
            "event_id": "irrelevant-after-observation", "run_id": "noise", "system": "queue",
            "event_type": "queue.metadata", "status": "applied", "payload": {"irrelevant": True},
        })
        assert preflight_summary(queue) == observed
        assert queue.corpus_preflight_events() == events
        assert observed["totals"]["admitted"] == observed["totals"]["observed"] == admitted
        assert observed["totals"]["native_port"] == {
            "described": 0, "blocked": 0, "unavailable": admitted, "not_observed": 0,
        }
        assert observed["last_observed_at"] == events[-1]["ts"]
