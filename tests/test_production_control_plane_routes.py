"""Operator-facing asset export and inspection routes over the real corpus registry."""

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
def registry() -> CorpusRegistry:
    return CorpusRegistry()


@pytest.fixture
def queue(tmp_path: Path, registry: CorpusRegistry) -> Iterator[ContinuousWorkQueue]:
    with ContinuousWorkQueue(tmp_path / "production.sqlite", registry=registry) as journal:
        yield journal


def _witness(queue: ContinuousWorkQueue, item_id: str, asset_id: str, note: str) -> None:
    queue.append_event(
        {
            "event_id": f"{asset_id}-event",
            "run_id": f"run-{asset_id}",
            "system": "reward_seeker",
            "event_type": "asset.verified",
            "status": "confirmed",
            "payload": {
                "asset_id": asset_id,
                "kind": "exploit_witness",
                "dataset_id": DATASET,
                "item_id": item_id,
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
