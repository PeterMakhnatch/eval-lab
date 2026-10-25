"""Operator-facing exports and task-scoped inspection over isolated real SQLite queues."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from evallab.production.asset_store import VerifiedAssetStore
from evallab.production.control_plane import create_app
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
from evallab.production.inspector import RunInspector

DATASET = "tb4-audit-v1"
TOKEN = "route-operator-capability"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> CorpusRegistry:
    root = tmp_path_factory.mktemp("operator-corpus")
    keys = root / "research/answer-keys"
    rows = [
        {"item_id": "route-dev-a", "split": "dev", "subject": {"task_id": "route-task-a"}},
        {"item_id": "route-dev-b", "split": "dev", "subject": {"task_id": "route-task-b"}},
        {
            "item_id": "route-synthetic-sealed",
            "split": "test",
            "subject": {"task_id": "route-synthetic-heldout"},
        },
    ]
    for dataset in (DATASET, "cheatbench-actions-v1", "hacktrace-act-v1"):
        directory = keys / dataset
        directory.mkdir(parents=True)
        (directory / "items.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows) if dataset == DATASET else "",
            encoding="utf-8",
        )
    (keys / DATASET / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": [],
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    return CorpusRegistry(root)


@pytest.fixture
def queue(tmp_path: Path, registry: CorpusRegistry) -> Iterator[ContinuousWorkQueue]:
    with ContinuousWorkQueue(tmp_path / "production.sqlite", registry=registry) as journal:
        yield journal


def _witness(queue: ContinuousWorkQueue, item_id: str, asset_id: str, note: str) -> None:
    run_id, digest = f"run-{asset_id}", "sha256:" + "a" * 64
    queue.append_event(
        {
            "event_id": f"{asset_id}-confirmed",
            "run_id": run_id,
            "system": "reward_seeker",
            "event_type": "seeker.exploit_confirmed",
            "status": "confirmed",
            "payload": {"dataset_id": DATASET, "item_id": item_id, "candidate_digest": digest},
        }
    )
    queue.append_event(
        {
            "event_id": f"{asset_id}-event",
            "run_id": run_id,
            "system": "reward_seeker",
            "event_type": "asset.verified",
            "status": "confirmed",
            "payload": {
                "asset_id": asset_id,
                "kind": "exploit_witness",
                "dataset_id": DATASET,
                "item_id": item_id,
                "source_event_id": f"{asset_id}-confirmed",
                "candidate_digest": digest,
                "exploit_files": {"solve.sh": note},
                "original_reward": 1.0,
                "repaired_reward": 0.0,
            },
        }
    )


def _client(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry, *, export: bool = True
) -> TestClient:
    roots = [tmp_path]
    return TestClient(
        create_app(
            queue,
            operator_tokens={TOKEN: "route-operator"},
            asset_store=VerifiedAssetStore(
                queue,
                registry,
                export_root=tmp_path / "exports" if export else None,
                allowed_roots=roots,
            ),
            inspector=RunInspector(queue, registry, allowed_roots=roots),
        )
    )


def _dev_and_sealed(registry: CorpusRegistry) -> tuple[str, str]:
    dev = next(item for item in registry.items(dataset_id=DATASET, split="dev"))
    sealed = next(item for item in registry.items(dataset_id=DATASET, split="test"))
    return dev.item_id, sealed.item_id


def test_receiptless_witness_is_not_listed_or_exportable_over_http(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    dev_item, _ = _dev_and_sealed(registry)
    queue.append_event(
        {
            "event_id": "bare-claim",
            "run_id": "run-bare-claim",
            "system": "reward_seeker",
            "event_type": "asset.verified",
            "status": "confirmed",
            "payload": {
                "asset_id": "witness:bare",
                "kind": "exploit_witness",
                "dataset_id": DATASET,
                "item_id": dev_item,
                "source_event_id": "nonexistent",
                "candidate_digest": None,
                "exploit_files": {"solve.sh": "echo claimed\n"},
                "original_reward": 1.0,
                "repaired_reward": 0.0,
            },
        }
    )
    with _client(tmp_path, queue, registry) as client:
        assert client.get("/api/assets").json()["assets"] == []
        assert (
            client.get("/api/assets/detail", params={"asset_id": "witness:bare"}).status_code == 404
        )
        exported = client.post(
            "/api/assets/export",
            headers=AUTH,
            json={"asset_id": "witness:bare", "request_id": "export-bare"},
        )
        assert exported.status_code == 404
    assert not (tmp_path / "exports").exists()


def test_dev_export_is_written_and_a_sealed_identifier_blocks_without_leaking(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    dev_item, sealed_item = _dev_and_sealed(registry)
    _witness(queue, dev_item, "witness:clean", "echo clean\n")
    _witness(queue, dev_item, "witness:leaky", f"# copied from {sealed_item}\n")
    with _client(tmp_path, queue, registry) as client:
        assert (
            client.post("/api/assets/export", json={"asset_id": "witness:clean"}).status_code == 401
        )

        clean = client.post(
            "/api/assets/export",
            headers=AUTH,
            json={"asset_id": "witness:clean", "request_id": "export-clean"},
        )
        assert clean.status_code == 200, clean.text
        manifest = json.loads((Path(clean.json()["export_path"]) / "manifest.json").read_text())
        assert manifest["split"] == "dev" and manifest["operator"] == "route-operator"

        leaky = client.post(
            "/api/assets/export",
            headers=AUTH,
            json={"asset_id": "witness:leaky", "request_id": "export-leaky"},
        )
        assert leaky.status_code == 409
        assert leaky.json()["detail"]["code"] == "split_leak"
        assert leaky.json()["detail"]["matches"]
        assert all(set(m) == {"field", "reason"} for m in leaky.json()["detail"]["matches"])
        blocked = next(
            event
            for event in queue.read_events(limit=1000)
            if event["event_type"] == "asset.export_blocked"
        )
        assert blocked["status"] == "refuted"
        # Neither the operator response nor the durable journal may carry the sealed id.
        assert sealed_item not in leaky.text
        assert sealed_item not in json.dumps(blocked)
        assert len(list((tmp_path / "exports" / "exploit_witness").iterdir())) == 1

        listing = client.get("/api/assets")
        listed = {a["asset_id"]: a for a in listing.json()["assets"]}
        assert listed["witness:clean"]["exported"] is not None
        # A blocked asset stays visible but opaque and non-exportable.
        assert sealed_item not in listing.text
        assert sealed_item not in client.get("/api/assets/detail?asset_id=witness:leaky").text
        assert [a["exportable"] for a in listed.values()].count(False) == 1


def test_request_ids_are_never_journaled_raw_and_cannot_span_assets(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    dev_item, sealed_item = _dev_and_sealed(registry)
    _witness(queue, dev_item, "witness:one", "echo one\n")
    _witness(queue, dev_item, "witness:two", "echo two\n")
    with _client(tmp_path, queue, registry) as client:
        # A client-chosen request id that is itself a sealed identifier is refused, not logged.
        smuggled = client.post(
            "/api/assets/export",
            headers=AUTH,
            json={"asset_id": "witness:one", "request_id": sealed_item},
        )
        assert smuggled.status_code == 409
        assert smuggled.json()["detail"]["code"] == "split_leak"
        journal = json.dumps(queue.read_events(limit=1000))
        assert sealed_item not in journal

        first = client.post(
            "/api/assets/export", headers=AUTH, json={"asset_id": "witness:one", "request_id": "r1"}
        )
        assert first.status_code == 200
        retry = client.post(
            "/api/assets/export", headers=AUTH, json={"asset_id": "witness:one", "request_id": "r1"}
        )
        assert retry.json()["export_path"] == first.json()["export_path"]
        other = client.post(
            "/api/assets/export", headers=AUTH, json={"asset_id": "witness:two", "request_id": "r1"}
        )
        assert other.status_code == 409
        assert other.json()["detail"]["code"] == "request_conflict"


def test_export_without_a_configured_root_is_a_distinct_error(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    dev_item, _ = _dev_and_sealed(registry)
    _witness(queue, dev_item, "witness:clean", "echo clean\n")
    with _client(tmp_path, queue, registry, export=False) as client:
        response = client.post(
            "/api/assets/export", headers=AUTH, json={"asset_id": "witness:clean"}
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "export_disabled"
        missing = client.post("/api/assets/export", headers=AUTH, json={"asset_id": "nope"})
        assert missing.status_code == 404


def test_timeline_and_grader_diff_report_unknown_runs_and_missing_components(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    dev_item, _ = _dev_and_sealed(registry)
    _witness(queue, dev_item, "witness:clean", "echo clean\n")
    with _client(tmp_path, queue, registry) as client:
        timeline: dict[str, Any] = client.get("/api/runs/run-witness:clean/timeline").json()
        assert timeline["run_id"] == "run-witness:clean"
        assert timeline["item_id"] == dev_item and timeline["split"] == "dev"
        assert client.get("/api/runs/no-such-run/timeline").status_code == 404
        assert client.get("/api/grader-diff").status_code == 400
        assert client.get("/api/grader-diff?run_id=a&asset_id=b").status_code == 400

    bare = TestClient(create_app(queue))
    assert bare.get("/api/assets").status_code == 503
    assert bare.get("/api/runs/x/timeline").status_code == 503


def test_campaign_items_have_separate_rows_timelines_and_grader_pairs(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    campaign = "multi-task-campaign"
    pairs = {}
    for letter in ("a", "b"):
        item_id = f"route-dev-{letter}"
        original = tmp_path / letter / "original"
        repaired = tmp_path / letter / "repaired"
        for package, text in (
            (original, f"original_{letter}\n"),
            (repaired, f"repaired_{letter}\n"),
        ):
            (package / "tests").mkdir(parents=True)
            (package / "tests/check.py").write_text(text, encoding="utf-8")
        payload = {
            "dataset_id": DATASET,
            "item_id": item_id,
            "original_task": str(original),
            "repaired_task": str(repaired),
            "original_reward": 1.0 if letter == "a" else 0.0,
            "repaired_reward": 0.0,
        }
        queue.enqueue(
            "task_quality.repair_task",
            payload,
            run_id=campaign,
            idempotency_key=f"repair-{letter}",
        )
        queue.append_event(
            {
                "event_id": f"worker-complete-{letter}",
                "run_id": campaign,
                "system": "reward_seeker",
                "event_type": "worker.completed",
                "status": "confirmed",
                "payload": payload,
            }
        )
        queue.append_event(
            {
                "event_id": f"trace-{letter}",
                "run_id": campaign,
                "system": "reward_seeker",
                "event_type": "trace.step",
                "status": "provisional",
                "prefix_step": 1,
                "payload": {
                    "dataset_id": DATASET,
                    "item_id": item_id,
                    "step": {"step_id": 1, "source": "agent", "message": f"inspect-{letter}"},
                },
            }
        )
        pairs[item_id] = payload
    queue.append_event(
        {
            "event_id": "asset-a",
            "run_id": campaign,
            "system": "reward_seeker",
            "event_type": "asset.verified",
            "status": "confirmed",
            "payload": {"asset_id": "witness:a", "kind": "exploit_witness", **pairs["route-dev-a"]},
        }
    )
    with _client(tmp_path, queue, registry) as client:
        rows = client.get("/api/state").json()["run_items"]["recent"]
        assert {row["item_id"]: row["original_reward"] for row in rows} == {
            "route-dev-a": 1.0,
            "route-dev-b": 0.0,
        }
        for path in (f"/api/runs/{campaign}/timeline", f"/api/grader-diff?run_id={campaign}"):
            response = client.get(path)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "ambiguous_run"
        assert (
            client.get(
                f"/api/runs/{campaign}/timeline", params={"item_id": "route-dev-a"}
            ).status_code
            == 400
        )
        for letter in ("a", "b"):
            selector = {"dataset_id": DATASET, "item_id": f"route-dev-{letter}"}
            timeline = client.get(f"/api/runs/{campaign}/timeline", params=selector)
            assert timeline.status_code == 200, timeline.text
            assert [step["message"] for step in timeline.json()["steps"]] == [f"inspect-{letter}"]
            diff = client.get("/api/grader-diff", params={"run_id": campaign, **selector})
            assert diff.status_code == 200, diff.text
            check = next(file for file in diff.json()["files"] if file["path"] == "tests/check.py")
            assert check["original"] == f"original_{letter}\n"
            assert check["repaired"] == f"repaired_{letter}\n"
        by_asset = client.get("/api/grader-diff", params={"asset_id": "witness:a"})
        assert by_asset.status_code == 200, by_asset.text
        assert by_asset.json()["item_id"] == "route-dev-a"


def test_seeker_phase_summaries_cannot_become_trace_evidence(
    tmp_path: Path, queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    for index, kind in enumerate(
        ("seeker.attempt_started", "seeker.grader_feedback", "seeker.exploit_confirmed")
    ):
        payload = {"dataset_id": DATASET, "item_id": "route-dev-a"}
        if kind != "seeker.exploit_confirmed":
            payload["step"] = {"step_id": 1, "phase": kind, "summary": "Attempt telemetry"}
        queue.append_event(
            {
                "event_id": f"phase-{index}",
                "run_id": "phase-only-campaign",
                "system": "reward_seeker",
                "event_type": kind,
                "status": "confirmed" if kind == "seeker.exploit_confirmed" else "provisional",
                "prefix_step": 1,
                "payload": payload,
            }
        )
    with _client(tmp_path, queue, registry) as client:
        state = client.get("/api/state").json()
        assert state["partial_traces"] == []
        assert state["run_items"]["recent"][0]["has_trace"] is False
        assert client.get("/api/assets").json()["assets"] == []
