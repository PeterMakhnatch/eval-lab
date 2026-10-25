"""Verified exports exercise the real SQLite journal, corpus splits, and file bytes."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from evallab.production.asset_store import (
    AssetNotFound,
    ExportDisabled,
    SplitLeakError,
    VerifiedAssetStore,
)
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
from evallab.registry import task_directory_digest

DATASET = "tb4-audit-v1"
ITEM = "dev-item"
SEALED_ITEM = "sealed-item"
SEALED_TASK = "sealed-task"
SEALED_DIGEST = "sha256:" + "d" * 64
FROZEN_TOKEN = "synthetic-corpus-fixture-only"


@contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    def expired(_signum: int, _frame: Any) -> None:
        raise TimeoutError("asset operation exceeded its deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *timer)


def _corpus(root: Path, *, extra_rows: list[dict[str, Any]] | None = None) -> CorpusRegistry:
    keys = root / "research/answer-keys"
    for dataset in (DATASET, "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        rows = (
            [
                {"item_id": ITEM, "split": "dev", "subject": {"task_id": "dev-task"}},
                {
                    "item_id": SEALED_ITEM,
                    "split": "test",
                    "subject": {"task_id": SEALED_TASK, "task_digest": SEALED_DIGEST},
                },
                # A locally DEV row is effectively TEST because its task name
                # aliases a sealed task, even in another public corpus.
            ]
            if dataset == DATASET
            else [{"item_id": "alias-item", "split": "dev", "subject": {"task_id": SEALED_TASK}}]
            if dataset == "cheatbench-actions-v1"
            else []
        )
        if dataset == DATASET and extra_rows:
            rows.extend(extra_rows)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (keys / DATASET / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": ["synthetic-heldout"],
                "tasks": [
                    {
                        "record_id": "synthetic-heldout-record",
                        "task_id": "synthetic-heldout",
                        "package_digest": "sha256:" + "e" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return CorpusRegistry(root, frozen_eval_token=FROZEN_TOKEN)


class _ObservedQueue(ContinuousWorkQueue):
    """Record cursors without replacing the real queue's reads or transactions."""

    def __init__(self, path: Path, registry: CorpusRegistry) -> None:
        super().__init__(path, registry=registry)
        self.read_cursors: list[int] = []

    def read_events(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self.read_cursors.append(after_seq)
        return super().read_events(after_seq=after_seq, limit=limit)


@pytest.fixture
def registry(tmp_path: Path) -> CorpusRegistry:
    return _corpus(tmp_path / "corpus")


@pytest.fixture
def queue(tmp_path: Path, registry: CorpusRegistry) -> Iterator[_ObservedQueue]:
    with _ObservedQueue(tmp_path / "production.sqlite", registry) as journal:
        yield journal


def _event(
    queue: ContinuousWorkQueue,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str = "asset-run",
    status: str = "confirmed",
    prefix_step: int | None = None,
    frozen: bool = False,
) -> dict[str, Any]:
    return queue.append_event(
        {
            "event_id": event_id,
            "run_id": run_id,
            "system": "misalignment" if event_type.startswith("monitor.") else "task_quality",
            "event_type": event_type,
            "ts": "2026-10-24T17:00:00+00:00",
            "prefix_step": prefix_step,
            "status": status,
            "payload": payload,
        },
        frozen_eval_token=FROZEN_TOKEN if frozen else None,
    )


def _store(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> VerifiedAssetStore:
    return VerifiedAssetStore(
        queue,
        registry,
        export_root=tmp_path / "exports",
        allowed_roots=[tmp_path / "inputs"],
    )


def _package(tmp_path: Path, content: str = "assert output == 42\n") -> Path:
    package = tmp_path / "inputs/repaired"
    (package / "tests").mkdir(parents=True)
    (package / "task.toml").write_text('[task]\nname = "dev-task"\n', encoding="utf-8")
    (package / "tests/test_output.py").write_text(content, encoding="utf-8")
    return package


WITNESS_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _witness(queue: ContinuousWorkQueue, *, asset_id: str = "witness:dev", **extra: Any) -> None:
    """Emit a witness citing a journaled confirmed exploit unless ``source_event_id`` is given."""
    digest = extra.setdefault("candidate_digest", WITNESS_DIGEST)
    if "source_event_id" not in extra:
        extra["source_event_id"] = f"{asset_id}:confirmed"
        _event(
            queue,
            extra["source_event_id"],
            "seeker.exploit_confirmed",
            {"dataset_id": DATASET, "item_id": ITEM, "candidate_digest": digest},
        )
    _event(
        queue,
        asset_id,
        "asset.verified",
        {
            "asset_id": asset_id,
            "kind": "exploit_witness",
            "dataset_id": DATASET,
            "item_id": ITEM,
            "exploit_files": {"solve.sh": "printf 'wrong answer'\n"},
            "original_reward": 1.0,
            "repaired_reward": 0.0,
            **extra,
        },
    )


def _repair(queue: ContinuousWorkQueue, *, asset_id: str = "repair", **extra: Any) -> None:
    """Emit a repaired task citing a journaled confirmed qualification."""
    qualification = f"{asset_id}:qualified"
    _event(
        queue,
        qualification,
        "task_quality.repair_qualified",
        {"dataset_id": DATASET, "item_id": ITEM},
    )
    _event(
        queue,
        asset_id,
        "asset.verified",
        {
            "kind": "repaired_task",
            "asset_id": asset_id,
            "dataset_id": DATASET,
            "item_id": ITEM,
            "qualification_event_id": qualification,
            **extra,
        },
    )


def _assert_manifest(export: dict[str, Any]) -> Path:
    root = Path(export["export_path"])
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    assert manifest == export["manifest"]
    assert hashlib.sha256(raw).hexdigest() == export["manifest_sha256"]
    assert manifest["schema"] == "evallab.verified-asset/v1"
    assert manifest["split"] == "dev"
    assert manifest["dataset_id"] == DATASET
    assert manifest["item_id"] == ITEM
    assert manifest["operator"] == "fixture-operator"
    assert manifest["split_guard"] == export["split_guard"]
    assert manifest["split_guard"]["result"] == "pass"
    listed = {"manifest.json"}
    for entry in manifest["files"]:
        path = root / entry["path"]
        assert path.is_relative_to(root)
        assert not path.is_symlink()
        data = path.read_bytes()
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        listed.add(entry["path"])
    assert {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    } == listed
    assert not list(root.parent.parent.glob(".asset-*"))
    return root


def test_repaired_task_export_resolves_qualification_lineage(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    package = _package(tmp_path)
    controls = {"reference": [1.0] * 3, "nop": [0.0] * 3, "exploit": [0.0] * 3}
    evidence = {"reference": [{"evidence_ref": "local-control/result.json", "measured": True}]}
    _event(
        queue,
        "qualification",
        "task_quality.repair_qualified",
        {
            "dataset_id": DATASET,
            "item_id": ITEM,
            "control_rewards": controls,
            "control_evidence": evidence,
        },
    )
    _event(
        queue,
        "repair-asset",
        "asset.verified",
        {
            "asset_id": "repaired-task:dev",
            "kind": "repaired_task",
            "qualification_event_id": "qualification",
            "task_package": str(package),
            "task_package_digest": task_directory_digest(package),
        },
    )
    store = _store(tmp_path, queue, registry)
    listing = store.list_assets()
    assert listing["total"] == 1
    assert listing["by_kind"] == {"repaired_task": 1, "exploit_witness": 0, "labeled_trace": 0}
    assert listing["assets"][0]["exportable"] is True
    assert store.get_asset("repaired-task:dev")["item_id"] == ITEM
    exported = store.export(
        "repaired-task:dev", operator="fixture-operator", request_id="repair-export"
    )
    root = _assert_manifest(exported)
    assert task_directory_digest(root / "task_package") == task_directory_digest(package)
    assert (root / "task_package/tests/test_output.py").read_bytes() == (
        package / "tests/test_output.py"
    ).read_bytes()
    qualification = json.loads((root / "qualification.json").read_text())
    assert qualification["control_rewards"] == controls
    assert qualification["control_evidence"] == evidence
    assert set(exported["manifest"]["source_event_ids"]) == {"qualification", "repair-asset"}


def test_exploit_witness_export_contains_real_files_and_bounded_bundle(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    bundle = tmp_path / "inputs/submission"
    bundle.mkdir(parents=True)
    (bundle / "solve.sh").write_text("printf 0\n", encoding="utf-8")
    (bundle / "large.txt").write_bytes(b"x" * (64 * 1024 + 10))
    _event(
        queue,
        "differential",
        "seeker.exploit_confirmed",
        {
            "dataset_id": DATASET,
            "item_id": ITEM,
            "mechanism": "accepts any nonempty answer",
            "taxonomy_class": "weak-check",
            "candidate_digest": WITNESS_DIGEST,
            "passing_runs": ["original-trial"],
            "repaired_runs": ["fixed-trial"],
        },
        prefix_step=1,
    )
    _witness(
        queue,
        source_event_id="differential",
        solution_bundle=str(bundle),
        candidate_digest=WITNESS_DIGEST,
        shortcut_signature="fixture-shortcut",
    )
    store = _store(tmp_path, queue, registry)
    exported = store.export("witness:dev", operator="fixture-operator", request_id="witness-export")
    root = _assert_manifest(exported)
    witness = json.loads((root / "witness.json").read_text())
    assert witness["original_reward"] == 1.0
    assert witness["repaired_reward"] == 0.0
    assert witness["mechanism"] == "accepts any nonempty answer"
    assert witness["candidate_digest"] == "sha256:" + "a" * 64
    assert witness["shortcut_signature"] == "fixture-shortcut"
    assert witness["source_event_ids"] == ["differential", "witness:dev"]
    assert witness["solution_bundle_truncated"] == ["large.txt"]
    assert (root / "solution_bundle/large.txt").stat().st_size == 64 * 1024
    assert (root / "exploit_files/solve.sh").read_text() == "printf 'wrong answer'\n"
    assert (root / "solution_bundle/solve.sh").read_text() == "printf 0\n"


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "monitor.suspicion",
            {"judge_label": "exploit-pass", "evidence": [{"step": 1, "quote": "wrong"}]},
        ),
        ("seeker.exploit_confirmed", {"original_reward": 1.0, "repaired_reward": 0.0}),
        ("monitor.gate_decided", {"allow": False, "reason": "tripwire_blocked"}),
    ],
)
def test_labeled_trace_export_preserves_causal_cutoff_and_provenance(
    tmp_path: Path,
    queue: _ObservedQueue,
    registry: CorpusRegistry,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    _event(queue, "run-start", "monitor.started", {"dataset_id": DATASET, "item_id": ITEM})
    for step in (0, 1, 2):
        _event(
            queue,
            f"trace-{step}",
            "trace.step",
            {
                "step": {
                    "step_id": step,
                    "source": "agent",
                    "reasoning": f"reasoning {step}",
                    "tool_calls": [],
                }
            },
            status="provisional",
            prefix_step=step,
        )
    _event(queue, "label", event_type, payload, prefix_step=1)
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["by_kind"]["labeled_trace"] == 1
    exported = store.export(
        "labeled-trace:asset-run", operator="fixture-operator", request_id="trace-export"
    )
    root = _assert_manifest(exported)
    steps = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
    labels = json.loads((root / "labels.json").read_text())
    assert [step["step_id"] for step in steps] == [0, 1, 2]
    assert labels["cutoff_step"] == 1
    assert labels["causal_prefix_step_ids"] == [0]
    assert labels["labels"][0]["step_id"] == 1
    assert labels["labels"][0]["source_event_id"] == "label"
    assert labels["labels"][0]["status"] == "confirmed"
    assert set(labels["provenance_event_ids"]) == {
        "run-start",
        "trace-0",
        "trace-1",
        "trace-2",
        "label",
    }


def test_trace_requires_confirmed_citable_labels_and_updates_incrementally(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _event(
        queue,
        "steps",
        "rollout.observed",
        {"dataset_id": DATASET, "item_id": ITEM, "steps": [{"step_id": 0}, {"step_id": 1}]},
    )
    _event(queue, "provisional", "monitor.suspicion", {"step_citations": [1]}, status="provisional")
    _event(
        queue, "refuted", "monitor.gate_decided", {"allow": True}, status="refuted", prefix_step=1
    )
    _event(queue, "uncitable", "monitor.suspicion", {"summary": "no citations"})
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["total"] == 0
    _event(
        queue,
        "confirmed",
        "monitor.suspicion",
        {"step_citations": ["head#1"], "judge_label": "defect-fail"},
    )
    assert store.list_assets()["total"] == 1
    exported = store.export(
        "labeled-trace:asset-run", operator="fixture-operator", request_id="trace"
    )
    labels = json.loads((Path(exported["export_path"]) / "labels.json").read_text())["labels"]
    assert labels == [
        {
            "step_id": 1,
            "label": "defect-fail",
            "source_event_id": "confirmed",
            "status": "confirmed",
        }
    ]


@pytest.mark.parametrize(
    ("dataset", "item_id"),
    [
        (DATASET, SEALED_ITEM),
        ("cheatbench-actions-v1", "alias-item"),
        ("mimo-defect-map-v1", "synthetic-heldout-record"),
    ],
)
def test_effective_test_lineage_cannot_export(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, dataset: str, item_id: str
) -> None:
    assert registry.get(dataset, item_id).split == "test"
    _event(
        queue,
        "sealed-source",
        "task_quality.repair_qualified",
        {"dataset_id": dataset, "item_id": item_id},
        frozen=True,
    )
    _event(
        queue,
        "sealed-asset",
        "asset.verified",
        {
            "asset_id": "linked-asset",
            "kind": "repaired_task",
            "qualification_event_id": "sealed-source",
            "task_package": "/not/read",
        },
    )
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["assets"][0]["exportable"] is False
    guarded = store.guard("linked-asset")
    assert guarded["result"] == "blocked"
    assert any(
        match["field"] == "lineage" and match["reason"] == "test_split_item"
        for match in guarded["matches"]
    )
    with pytest.raises(SplitLeakError) as error:
        store.export("linked-asset", operator="fixture-operator", request_id="blocked")
    assert error.value.matches == guarded["matches"]
    assert not (tmp_path / "exports").exists()


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (SEALED_ITEM, "test_split_item"),
        (SEALED_TASK, "sealed_task_name"),
        (SEALED_DIGEST, "sealed_digest"),
        ("d" * 64, "sealed_digest"),
    ],
)
def test_sealed_identifiers_inside_exported_text_block_before_any_writes(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, token: str, reason: str
) -> None:
    package = _package(tmp_path, f"# sealed reference: {token}\n")
    _repair(
        queue,
        task_package=str(package),
        task_package_digest=task_directory_digest(package),
    )
    store = _store(tmp_path, queue, registry)
    guarded = store.guard("repair")
    assert guarded["result"] == "blocked"
    assert any(
        match["reason"] == reason and match["field"] == "file_content"
        for match in guarded["matches"]
    )
    with pytest.raises(SplitLeakError):
        store.export("repair", operator="fixture-operator", request_id="blocked")
    assert not (tmp_path / "exports").exists()


def test_nested_payload_and_file_names_are_guarded(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue, metadata={"nested": [{"note": f"mentions {SEALED_TASK}"}]})
    store = _store(tmp_path, queue, registry)
    assert any(match["field"] == "payload" for match in store.guard("witness:dev")["matches"])
    _witness(queue, asset_id="named-witness", exploit_files={f"{SEALED_ITEM}.sh": "printf 0\n"})
    with pytest.raises(SplitLeakError):
        store.export("named-witness", operator="fixture-operator", request_id="blocked")
    assert not (tmp_path / "exports").exists()


def test_guard_matches_whole_identifier_tokens_not_substrings(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(
        queue,
        exploit_files={
            "solve.sh": f"prefix-{SEALED_ITEM}-suffix other{SEALED_TASK} a{'d' * 64}b\n"
        },
    )
    store = _store(tmp_path, queue, registry)
    assert store.guard("witness:dev")["result"] == "pass"
    _assert_manifest(
        store.export("witness:dev", operator="fixture-operator", request_id="safe-tokens")
    )


def test_unresolvable_lineage_fails_closed_even_when_export_is_disabled(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _event(queue, "confirmed", "seeker.exploit_confirmed", {"candidate_digest": WITNESS_DIGEST})
    _event(
        queue,
        "unknown",
        "asset.verified",
        {
            "kind": "exploit_witness",
            "asset_id": "unknown",
            "source_event_id": "confirmed",
            "candidate_digest": WITNESS_DIGEST,
            "exploit_files": {"solve.sh": "printf 0\n"},
        },
    )
    store = _store(tmp_path, queue, registry)
    assert store.get_asset("unknown")["blocked_reason"] == "lineage_unresolved"
    assert store.guard("unknown")["matches"][0]["reason"] == "lineage_unresolved"
    disabled = VerifiedAssetStore(queue, registry, export_root=None, allowed_roots=[])
    for configured in (store, disabled):
        with pytest.raises(SplitLeakError):
            configured.export("unknown", operator="fixture-operator", request_id="blocked")
    assert not (tmp_path / "exports").exists()


@pytest.mark.parametrize(
    ("prior", "claim"),
    [
        pytest.param(
            [],
            {
                "kind": "exploit_witness",
                "source_event_id": "nowhere",
                "candidate_digest": WITNESS_DIGEST,
            },
            id="witness-source-not-journaled",
        ),
        pytest.param(
            [("seeker.exploit_confirmed", "confirmed", {"candidate_digest": WITNESS_DIGEST})],
            {"kind": "exploit_witness", "candidate_digest": WITNESS_DIGEST},
            id="witness-cites-nothing",
        ),
        pytest.param(
            [("seeker.exploit_confirmed", "confirmed", {"candidate_digest": WITNESS_DIGEST})],
            {"kind": "exploit_witness", "source_event_id": "prior-0", "candidate_digest": None},
            id="witness-without-candidate-digest",
        ),
        pytest.param(
            [("seeker.exploit_confirmed", "confirmed", {"candidate_digest": "sha256:abc"})],
            {
                "kind": "exploit_witness",
                "source_event_id": "prior-0",
                "candidate_digest": "sha256:abc",
            },
            id="witness-with-truncated-digest",
        ),
        pytest.param(
            [("seeker.exploit_confirmed", "confirmed", {"candidate_digest": OTHER_DIGEST})],
            {
                "kind": "exploit_witness",
                "source_event_id": "prior-0",
                "candidate_digest": WITNESS_DIGEST,
            },
            id="witness-digest-differs-from-confirmed-exploit",
        ),
        pytest.param(
            [("seeker.exploit_confirmed", "provisional", {"candidate_digest": WITNESS_DIGEST})],
            {
                "kind": "exploit_witness",
                "source_event_id": "prior-0",
                "candidate_digest": WITNESS_DIGEST,
            },
            id="witness-source-not-confirmed",
        ),
        pytest.param(
            [("seeker.attempt_started", "confirmed", {"candidate_digest": WITNESS_DIGEST})],
            {
                "kind": "exploit_witness",
                "source_event_id": "prior-0",
                "candidate_digest": WITNESS_DIGEST,
            },
            id="witness-source-is-not-an-exploit-confirmation",
        ),
        pytest.param([], {"kind": "repaired_task"}, id="repair-cites-nothing"),
        pytest.param(
            [],
            {"kind": "repaired_task", "qualification_event_id": "nowhere"},
            id="repair-qualification-not-journaled",
        ),
        pytest.param(
            [("task_quality.repair_rejected", "refuted", {})],
            {"kind": "repaired_task", "qualification_event_id": "prior-0"},
            id="repair-qualification-was-rejected",
        ),
        pytest.param(
            [("task_quality.repair_qualified", "provisional", {})],
            {"kind": "repaired_task", "qualification_event_id": "prior-0"},
            id="repair-qualification-not-confirmed",
        ),
    ],
)
def test_assets_without_a_journaled_receipt_are_never_indexed_or_exported(
    tmp_path: Path,
    queue: _ObservedQueue,
    registry: CorpusRegistry,
    prior: list[tuple[str, str, dict[str, Any]]],
    claim: dict[str, Any],
) -> None:
    for number, (event_type, status, payload) in enumerate(prior):
        _event(
            queue,
            f"prior-{number}",
            event_type,
            {"dataset_id": DATASET, "item_id": ITEM, **payload},
            status=status,
        )
    _event(
        queue,
        "claimed",
        "asset.verified",
        {
            "asset_id": "claimed",
            "dataset_id": DATASET,
            "item_id": ITEM,
            "exploit_files": {"solve.sh": "printf 0\n"},
            "original_reward": 1.0,
            "repaired_reward": 0.0,
            **claim,
        },
    )
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["total"] == 0
    with pytest.raises(AssetNotFound):
        store.get_asset("claimed")
    with pytest.raises(AssetNotFound):
        store.export("claimed", operator="fixture-operator", request_id="receiptless")
    assert not (tmp_path / "exports").exists()


@pytest.mark.parametrize("kind", ["repaired_task", "exploit_witness"])
def test_event_paths_outside_allowed_roots_are_refused(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not an allowed source", encoding="utf-8")
    if kind == "repaired_task":
        _repair(queue, asset_id="outside", task_package=str(outside))
    else:
        _witness(queue, asset_id="outside", solution_bundle=str(outside))
    store = _store(tmp_path, queue, registry)
    with pytest.raises(PermissionError, match="outside the allowed roots"):
        store.guard("outside")
    with pytest.raises(PermissionError, match="outside the allowed roots"):
        store.export("outside", operator="fixture-operator", request_id="blocked")
    assert not (tmp_path / "exports").exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "a//b", "a/./b", "\x00bad"]
)
def test_payload_file_names_cannot_traverse_export_directories(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, name: str
) -> None:
    _witness(queue, exploit_files={name: "untrusted"})
    store = _store(tmp_path, queue, registry)
    with pytest.raises(ValueError, match="unsafe asset file name"):
        store.export("witness:dev", operator="fixture-operator", request_id="blocked")
    assert not (tmp_path / "exports").exists()
    assert not (tmp_path / "escape").exists()


def test_export_is_idempotent_and_exported_state_survives_replay(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue)
    store = _store(tmp_path, queue, registry)
    first = store.export("witness:dev", operator="fixture-operator", request_id="first")
    _assert_manifest(first)
    assert store.export("witness:dev", operator="different-operator", request_id="retry") == first
    _witness(queue, asset_id="unrelated-witness")
    assert (
        store.export("witness:dev", operator="fixture-operator", request_id="after-other-asset")
        == first
    )
    restarted = _store(tmp_path, queue, registry)
    assert restarted.list_assets()["assets"][0]["exported"] == {
        "export_path": first["export_path"],
        "exported_at": first["manifest"]["exported_at"],
    }
    assert (
        restarted.export("witness:dev", operator="fixture-operator", request_id="after-restart")
        == first
    )
    exports = [
        event for event in queue.read_events(limit=100) if event["event_type"] == "asset.exported"
    ]
    assert {event["payload"]["request_id"] for event in exports} == {
        "first",
        "retry",
        "after-other-asset",
        "after-restart",
    }
    (Path(first["export_path"]) / "exploit_files/solve.sh").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        restarted.export("witness:dev", operator="fixture-operator", request_id="tampered")


def test_disabled_exports_and_unknown_assets_have_explicit_errors(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue)
    store = VerifiedAssetStore(
        queue, registry, export_root=None, allowed_roots=[tmp_path / "inputs"]
    )
    assert store.get_asset("witness:dev")["blocked_reason"] == "export_disabled"
    with pytest.raises(ExportDisabled):
        store.export("witness:dev", operator="fixture-operator", request_id="disabled")
    for operation in (store.get_asset, store.guard):
        with pytest.raises(AssetNotFound):
            operation("missing")
    with pytest.raises(AssetNotFound):
        store.export("missing", operator="fixture-operator", request_id="missing")
    assert not (tmp_path / "exports").exists()


def test_export_rechecks_files_instead_of_trusting_an_earlier_guard(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    package = _package(tmp_path)
    _repair(queue, task_package=str(package))
    store = _store(tmp_path, queue, registry)
    assert store.guard("repair")["result"] == "pass"
    (package / "tests/test_output.py").write_text(f"# {SEALED_TASK}\n", encoding="utf-8")
    with pytest.raises(SplitLeakError) as error:
        store.export("repair", operator="fixture-operator", request_id="changed")
    assert any(match["reason"] == "sealed_task_name" for match in error.value.matches)
    assert not (tmp_path / "exports").exists()


def test_package_digest_mismatch_is_refused(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    package = _package(tmp_path)
    digest = task_directory_digest(package)
    (package / "tests/test_output.py").write_text("changed verifier\n", encoding="utf-8")
    _repair(queue, task_package=str(package), task_package_digest=digest)
    with pytest.raises(ValueError, match="digest does not match"):
        _store(tmp_path, queue, registry).export(
            "repair", operator="fixture-operator", request_id="changed"
        )
    assert not [path for path in (tmp_path / "exports").rglob("*") if path.is_file()]
    assert not list((tmp_path / "exports").glob(".asset-*"))


def test_package_size_cap_and_symlink_skipping(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(f"private {SEALED_TASK}", encoding="utf-8")
    (package / "not-copied.txt").symlink_to(outside)
    _repair(queue, task_package=str(package))
    store = _store(tmp_path, queue, registry)
    root = _assert_manifest(
        store.export("repair", operator="fixture-operator", request_id="skip-links")
    )
    assert not (root / "task_package/not-copied.txt").exists()
    with (package / "too-large.bin").open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="50 MiB"):
        store.export("repair", operator="fixture-operator", request_id="too-large")
    assert len(list((tmp_path / "exports/repaired_task").iterdir())) == 1


def test_paged_index_is_incremental_and_safe_for_concurrent_callers(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    for number in range(501):
        _event(queue, f"noise-{number}", "queue.tick", {}, run_id="noise")
    _witness(queue)
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["total"] == 1
    assert queue.read_cursors == [0, 500]
    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(executor.map(lambda _: store.list_assets(), range(12)))
    assert all(snapshot["total"] == 1 for snapshot in snapshots)
    assert queue.read_cursors == [0, 500]
    snapshots[0]["assets"][0]["kind"] = "mutated"
    assert store.get_asset("witness:dev")["kind"] == "exploit_witness"
    _witness(queue, asset_id="second-witness")
    assert store.list_assets()["total"] == 2
    with ThreadPoolExecutor(max_workers=4) as executor:
        exports = list(
            executor.map(
                lambda number: store.export(
                    "witness:dev", operator="fixture-operator", request_id=f"parallel-{number}"
                ),
                range(4),
            )
        )
    assert all(export == exports[0] for export in exports)
    _assert_manifest(exports[0])


def test_missing_cross_run_lineage_is_reindexed_when_its_source_arrives(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _event(
        queue,
        "confirmed",
        "seeker.exploit_confirmed",
        {"candidate_digest": WITNESS_DIGEST, "source_event_id": "late-lineage"},
    )
    _event(
        queue,
        "waiting-asset",
        "asset.verified",
        {
            "asset_id": "waiting-asset",
            "kind": "exploit_witness",
            "source_event_id": "confirmed",
            "candidate_digest": WITNESS_DIGEST,
            "exploit_files": {"solve.sh": "printf 0\n"},
        },
    )
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["assets"][0]["blocked_reason"] == "lineage_unresolved"
    _event(
        queue,
        "late-lineage",
        "seeker.exploit_confirmed",
        {"dataset_id": DATASET, "item_id": ITEM, "original_reward": 1.0, "repaired_reward": 0.0},
        run_id="upstream-run",
    )
    assert store.list_assets()["assets"][0]["exportable"] is True
    _assert_manifest(
        store.export("waiting-asset", operator="fixture-operator", request_id="lineage-arrived")
    )


def test_existing_manifest_cannot_reintroduce_sealed_content(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue)
    store = _store(tmp_path, queue, registry)
    exported = store.export("witness:dev", operator="fixture-operator", request_id="first")
    manifest_path = Path(exported["export_path"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["operator"] = SEALED_TASK
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SplitLeakError) as error:
        store.export("witness:dev", operator="fixture-operator", request_id="retry")
    assert any(
        match["field"] == "manifest" and match["reason"] == "sealed_task_name"
        for match in error.value.matches
    )
    assert not list((tmp_path / "exports").glob(".asset-*"))


@pytest.mark.parametrize("event_type", ["seeker.exploit_confirmed", "monitor.gate_decided"])
def test_recorded_outcome_before_trace_still_derives_a_labeled_asset(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, event_type: str
) -> None:
    _event(
        queue, "outcome-first", event_type, {"dataset_id": DATASET, "item_id": ITEM, "allow": True}
    )
    _event(queue, "recorded-trace", "trace.step", {"steps": [{"step_id": 0}, {"step_id": 1}]})
    store = _store(tmp_path, queue, registry)
    exported = store.export(
        "labeled-trace:asset-run", operator="fixture-operator", request_id="recorded-trace"
    )
    root = _assert_manifest(exported)
    labels = json.loads((root / "labels.json").read_text())
    assert labels["cutoff_step"] == 1
    assert labels["causal_prefix_step_ids"] == [0]
    assert labels["labels"][0]["source_event_id"] == "outcome-first"


@pytest.mark.parametrize("status", ["confirmed", "applied"])
def test_only_verified_supported_kinds_are_indexed_with_stable_fallback_ids(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, status: str
) -> None:
    for ignored in ("provisional", "refuted"):
        _event(
            queue,
            f"not-verified-{ignored}",
            "asset.verified",
            {"kind": "exploit_witness", "dataset_id": DATASET, "item_id": ITEM},
            status=ignored,
        )
    _event(
        queue,
        "unsupported",
        "asset.verified",
        {"kind": "labeled_trace", "dataset_id": DATASET, "item_id": ITEM},
    )
    _event(
        queue,
        "verified-source",
        "seeker.exploit_confirmed",
        {"dataset_id": DATASET, "item_id": ITEM, "candidate_digest": WITNESS_DIGEST},
    )
    _event(
        queue,
        "verified-without-id",
        "asset.verified",
        {
            "kind": "exploit_witness",
            "dataset_id": DATASET,
            "item_id": ITEM,
            "source_event_id": "verified-source",
            "candidate_digest": WITNESS_DIGEST,
            "exploit_files": {"solve.sh": "printf 0\n"},
        },
        status=status,
    )
    store = _store(tmp_path, queue, registry)
    assert store.list_assets()["total"] == 1
    _assert_manifest(
        store.export(
            "exploit_witness:verified-without-id",
            operator="fixture-operator",
            request_id="fallback",
        )
    )


@pytest.mark.parametrize(
    ("text", "token", "reason"),
    [
        ("sealed/record.v2", "sealed/record.v2", "test_split_item"),
        ("folder/sealed.name@version", "folder/sealed.name@version", "sealed_task_name"),
        ("SHA256:" + "F" * 64, "sha256:" + "f" * 64, "sealed_digest"),
        ("f" * 64, "f" * 64, "sealed_digest"),
        (" \t ...\n", "...", "test_split_item"),
        ("prefix-sealed/record.v2-suffix", None, None),
        ("foo...bar", None, None),
    ],
)
def test_identifier_anchors_preserve_punctuation_case_and_token_boundaries(
    tmp_path: Path, text: str, token: str | None, reason: str | None
) -> None:
    registry = _corpus(
        tmp_path / "extra-corpus",
        extra_rows=[
            {
                "item_id": "sealed/record.v2",
                "split": "test",
                "subject": {
                    "task_id": "folder/sealed.name@version",
                    "task_digest": "sha256:" + "f" * 64,
                },
            },
            {"item_id": "...", "split": "test", "subject": {}},
        ],
    )
    with ContinuousWorkQueue(tmp_path / "anchors.sqlite", registry=registry) as journal:
        _witness(journal, exploit_files={"solve.sh": text})
        store = _store(tmp_path, journal, registry)
        guarded = store.guard("witness:dev")
        if token is None:
            assert guarded["result"] == "pass"
            _assert_manifest(
                store.export("witness:dev", operator="fixture-operator", request_id="whole-tokens")
            )
        else:
            assert guarded["result"] == "blocked"
            assert any(
                match["field"] == "payload"
                and match["reason"] == reason
                and set(match) == {"field", "reason"}
                for match in guarded["matches"]
            )
            with pytest.raises(SplitLeakError):
                store.export("witness:dev", operator="fixture-operator", request_id="blocked")
            assert not (tmp_path / "exports").exists()


@pytest.mark.parametrize("source", ["lineage", "file"])
def test_blocked_list_and_detail_only_expose_an_opaque_reference(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, source: str
) -> None:
    if source == "lineage":
        _event(
            queue,
            "sealed-origin",
            "task_quality.repair_qualified",
            {"dataset_id": DATASET, "item_id": SEALED_ITEM},
            run_id=f"run/{SEALED_TASK}",
            frozen=True,
        )
        asset_id = f"asset/{SEALED_ITEM}"
        _event(
            queue,
            f"event/{SEALED_DIGEST}",
            "asset.verified",
            {
                "asset_id": asset_id,
                "kind": "repaired_task",
                "qualification_event_id": "sealed-origin",
                "task_package": "/never/read",
                "summary": f"untrusted {SEALED_TASK}",
            },
            run_id=f"run/{SEALED_TASK}",
        )
    else:
        package = _package(tmp_path, f"# {SEALED_TASK}\n")
        asset_id = "file-contaminated"
        _repair(queue, asset_id=asset_id, task_package=str(package))
    store = _store(tmp_path, queue, registry)
    listing = store.list_assets()
    descriptor = listing["assets"][0]
    assert set(descriptor) == {"asset_id", "kind", "exportable", "blocked_reason"}
    assert descriptor["exportable"] is False
    assert descriptor["asset_id"] != asset_id
    assert store.get_asset(asset_id) == descriptor
    assert store.get_asset(descriptor["asset_id"]) == descriptor
    serialized = json.dumps([listing, store.get_asset(asset_id)])
    assert all(token not in serialized for token in (SEALED_ITEM, SEALED_TASK, SEALED_DIGEST))
    assert "preview" not in serialized


def test_split_errors_only_return_fixed_categories_and_reason_codes(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue, **{SEALED_ITEM: {"note": SEALED_TASK}})
    package = _package(tmp_path)
    (package / f"{SEALED_ITEM}.txt").write_text("safe content", encoding="utf-8")
    _repair(queue, asset_id="file-name", task_package=str(package))
    _event(
        queue,
        "manifest-confirmed",
        "seeker.exploit_confirmed",
        {"dataset_id": DATASET, "item_id": ITEM, "candidate_digest": WITNESS_DIGEST},
    )
    _event(
        queue,
        f"event/{SEALED_TASK}",
        "asset.verified",
        {
            "asset_id": "manifest-event",
            "kind": "exploit_witness",
            "dataset_id": DATASET,
            "item_id": ITEM,
            "source_event_id": "manifest-confirmed",
            "candidate_digest": WITNESS_DIGEST,
            "exploit_files": {},
        },
    )
    store = _store(tmp_path, queue, registry)
    fields: set[str] = set()
    for asset_id in ("witness:dev", "file-name", "manifest-event"):
        with pytest.raises(SplitLeakError) as error:
            store.export(asset_id, operator="fixture-operator", request_id=f"blocked-{asset_id}")
        for match in error.value.matches:
            assert set(match) == {"field", "reason"}
            fields.add(match["field"])
        assert all(
            token not in json.dumps(error.value.matches) for token in (SEALED_ITEM, SEALED_TASK)
        )
    assert fields == {"payload", "file_name", "manifest"}


@pytest.mark.parametrize("watermark", [True, False])
def test_refresh_stops_at_its_initial_watermark_while_the_journal_grows(
    tmp_path: Path, registry: CorpusRegistry, watermark: bool
) -> None:
    class GrowingQueue(_ObservedQueue):
        def __init__(self, path: Path) -> None:
            super().__init__(path, registry)
            self.snapshots = 0
            self.generated = 0

        def queue_snapshot(self) -> dict[str, Any]:
            self.snapshots += 1
            snapshot = super().queue_snapshot()
            return snapshot if watermark else {}

        def read_events(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
            events = super().read_events(after_seq=after_seq, limit=limit)
            for _ in range(limit):
                self.generated += 1
                _event(self, f"concurrent-{self.generated}", "queue.tick", {}, run_id="growth")
            return events

    with GrowingQueue(tmp_path / "growing.sqlite") as journal:
        for number in range(501):
            _event(journal, f"initial-{number}", "queue.tick", {}, run_id="growth")
        _witness(journal)
        store = _store(tmp_path, journal, registry)
        with _deadline(10):
            listing = store.list_assets()
        assert listing["total"] == (1 if watermark else 0)
        assert journal.snapshots == 1
        assert journal.read_cursors == ([0, 500] if watermark else [0])
        assert ContinuousWorkQueue.queue_snapshot(journal)["last_seq"] > 502


@pytest.mark.parametrize("relative", ["manifest.json", "exploit_files/solve.sh"])
def test_existing_export_fifos_are_refused_without_waiting_for_a_writer(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry, relative: str
) -> None:
    _witness(queue)
    store = _store(tmp_path, queue, registry)
    exported = store.export("witness:dev", operator="fixture-operator", request_id="fifo-retry")
    path = Path(exported["export_path"]) / relative
    path.unlink()
    os.mkfifo(path)
    with _deadline(2), pytest.raises(ValueError, match="not a regular file"):
        store.export("witness:dev", operator="fixture-operator", request_id="fifo-retry")


def test_sealed_identifier_in_export_root_parent_blocks_before_writes_or_journaling(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue)
    export_root = tmp_path / SEALED_ITEM / "otherwise-innocent-exports"
    store = VerifiedAssetStore(
        queue, registry, export_root=export_root, allowed_roots=[tmp_path / "inputs"]
    )
    with pytest.raises(SplitLeakError) as error:
        store.export("witness:dev", operator="fixture-operator", request_id="sealed-root")
    assert {"field": "destination", "reason": "test_split_item"} in error.value.matches
    assert not export_root.parent.exists()
    assert not [
        event for event in queue.read_events(limit=100) if event["event_type"] == "asset.exported"
    ]


def test_request_bindings_survive_replay_and_retries_return_the_original_content(
    tmp_path: Path, queue: _ObservedQueue, registry: CorpusRegistry
) -> None:
    _witness(queue)
    _witness(queue, asset_id="other-asset")
    store = _store(tmp_path, queue, registry)
    first = store.export("witness:dev", operator="fixture-operator", request_id="durable-operation")
    _event(
        queue,
        "later-verification",
        "asset.verified",
        {
            "asset_id": "witness:dev",
            "kind": "exploit_witness",
            "dataset_id": DATASET,
            "item_id": ITEM,
            "source_event_id": "witness:dev:confirmed",
            "candidate_digest": WITNESS_DIGEST,
            "exploit_files": {"solve.sh": "changed content\n"},
        },
    )
    assert (
        store.export("witness:dev", operator="fixture-operator", request_id="durable-operation")
        == first
    )
    changed = store.export("witness:dev", operator="fixture-operator", request_id="changed-content")
    assert changed["export_path"] != first["export_path"]
    assert changed["manifest_sha256"] != first["manifest_sha256"]
    alias = store.export("witness:dev", operator="fixture-operator", request_id="alias-operation")
    assert alias == changed
    restarted = _store(tmp_path, queue, registry)
    assert (
        restarted.export("witness:dev", operator="fixture-operator", request_id="durable-operation")
        == first
    )
    assert (
        restarted.export("witness:dev", operator="fixture-operator", request_id="alias-operation")
        == changed
    )
    for request_id in ("durable-operation", "changed-content", "alias-operation"):
        with pytest.raises(
            ValueError, match="^request_id already belongs to a different asset export$"
        ):
            restarted.export("other-asset", operator="fixture-operator", request_id=request_id)
    with pytest.raises(
        ValueError, match="^request_id already belongs to a different asset export$"
    ):
        restarted.export("witness:dev", operator="other-operator", request_id="durable-operation")
    with pytest.raises(SplitLeakError):
        restarted.export("other-asset", operator=SEALED_TASK, request_id="blocked-operator")
    with pytest.raises(SplitLeakError):
        restarted.export("other-asset", operator="fixture-operator", request_id=SEALED_ITEM)
    events = [
        event for event in queue.read_events(limit=100) if event["event_type"] == "asset.exported"
    ]
    assert {event["payload"]["request_id"] for event in events} == {
        "durable-operation",
        "changed-content",
        "alias-operation",
    }
    assert all(
        token not in json.dumps(events) for token in (SEALED_ITEM, SEALED_TASK, SEALED_DIGEST)
    )


def test_concurrent_stores_cannot_bind_one_request_to_different_assets(
    tmp_path: Path, registry: CorpusRegistry
) -> None:
    barrier = Barrier(2)

    class RacingQueue(ContinuousWorkQueue):
        def append_event(
            self,
            event: Mapping[str, Any],
            *,
            idempotency_key: str | None = None,
            frozen_eval_token: str | None = None,
        ) -> dict[str, Any]:
            if event["event_type"] == "asset.exported":
                barrier.wait(timeout=5)
            return super().append_event(
                event, idempotency_key=idempotency_key, frozen_eval_token=frozen_eval_token
            )

    database = tmp_path / "racing.sqlite"
    with (
        RacingQueue(database, registry=registry) as left,
        RacingQueue(database, registry=registry) as right,
    ):
        _witness(left, asset_id="left-asset")
        _witness(left, asset_id="right-asset")
        stores = [_store(tmp_path, journal, registry) for journal in (left, right)]
        results, errors = [], []
        with _deadline(10), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    store.export,
                    asset_id,
                    operator="fixture-operator",
                    request_id="shared-operation",
                )
                for store, asset_id in zip(stores, ("left-asset", "right-asset"), strict=True)
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except ValueError as error:
                    errors.append(str(error))
        assert errors == ["request_id already belongs to a different asset export"]
        assert len(results) == 1
        _assert_manifest(results[0])
        restarted = _store(tmp_path, left, registry)
        assert (
            restarted.export(
                results[0]["asset_id"], operator="fixture-operator", request_id="shared-operation"
            )
            == results[0]
        )
