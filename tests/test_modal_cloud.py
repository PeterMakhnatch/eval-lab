"""Offline integration coverage for authenticated, Volume-backed Modal production."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import socket
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from evallab.execution_backend import ExecutionBackendConfig
from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS
from evallab.production.corpus_queue import ContinuousWorkQueue

if TYPE_CHECKING:
    from evallab.production.modal_cloud import CloudRuntime

TOKEN = "unit-cloud-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PRIMARY = "cloud-primary"
SECONDARY = "cloud-secondary"
VERIFIED = "trace-verified"
PROVISIONAL = "trace-provisional"
REFUTED = "trace-refuted"
COOKIE = "evallab_cloud_session"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(home / ".modal.toml"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for name in (
        "DATABASE_URL",
        "EVALLAB_CLOUD_DATABASE_URL",
        "EVALLAB_S3_BUCKET",
        "EVALLAB_S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "EVALLAB_CLOUD_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("cloud runtime tests must not use network, subprocesses, or providers")

    class ForbiddenProcess(subprocess.Popen):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            forbidden()

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", ForbiddenProcess)
    monkeypatch.setattr(importlib.import_module("boto3"), "client", forbidden)
    monkeypatch.setattr(importlib.import_module("psycopg"), "connect", forbidden)


@pytest.fixture
def modal_cloud() -> ModuleType:
    # Import under the same offline/home isolation as runtime construction.
    return importlib.import_module("evallab.production.modal_cloud")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        directory = keys / dataset
        directory.mkdir(parents=True)
        (directory / "items.jsonl").write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/items.jsonl").write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {
                    "item_id": "cloud-dev-item",
                    "split": "dev",
                    "subject": {"task_id": "cloud-dev-task"},
                },
                {
                    "item_id": "sealed-item",
                    "split": "test",
                    "subject": {"task_id": "sealed-task-name"},
                },
            )
        ),
        encoding="utf-8",
    )
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    ledger = keys / "splits/assignments.csv"
    ledger.parent.mkdir()
    ledger.write_text(
        "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
        "synthetic-ledger-fixture,ledger-dev,ledger-dev,,dev,fail,\n"
        "synthetic-ledger-fixture,ledger-test,ledger-test,,test,fail,\n",
        encoding="utf-8",
    )
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
    return root


@pytest.fixture
def runtime(modal_cloud: ModuleType, tmp_path: Path, repo_root: Path) -> Iterator[CloudRuntime]:
    instance = modal_cloud.CloudRuntime(
        data_root=tmp_path / "data",
        repo_root=repo_root,
        token=TOKEN,
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def client(runtime: CloudRuntime) -> Iterator[TestClient]:
    with TestClient(runtime.app, base_url="https://cloud.test") as transport:
        yield transport


def _task(task_id: str, instruction: str, *, split: str = "dev") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "dataset_id": "synthetic-cloud-fixture",
        "split": split,
        "instruction_text": instruction,
        "original_grader_digest": "sha256:" + "a" * 64,
        "repaired_grader_digest": "sha256:" + "b" * 64,
        "cas_uri": None,
        "metadata_json": {"source_status": "provisional", "lineage": ["retained", "source"]},
    }


def _issue(
    issue_id: str,
    task_id: str,
    *,
    status: str = "confirmed",
    direction: str = "wrong_pass",
    defect_class: str = "insufficient_checking",
    reproducer: str = "printf 'synthetic fixture'\n",
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "task_id": task_id,
        "defect_class": defect_class,
        "direction": direction,
        "status": status,
        "witness_digest": "sha256:" + "c" * 64,
        "original_reward": 1.0,
        "repaired_reward": 0.0,
        "citation_file": "tests/check_fixture.py",
        "citation_line": 27,
        "reproducer_script": reproducer,
        "cas_uri": None,
    }


def _trace(
    trace_id: str,
    task_id: str,
    receipt: dict[str, Any] | None,
    *,
    outcome: str = "unknown",
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "run_id": "run-" + trace_id,
        "task_id": task_id,
        "model_id": "synthetic-cloud-model",
        "outcome": outcome,
        "reward": 1.0 if outcome == "wrong_pass" else 0.0,
        "is_exploit": outcome == "wrong_pass",
        "is_task_defect_fail": outcome == "unfair_fail",
        "raw_cas_uri": None,
        "verified_receipt_json": receipt,
    }


@pytest.fixture
def records(runtime: CloudRuntime) -> dict[str, Any]:
    instruction = "Retained instruction café 完整\n" * 4096 + "primaryneedle"
    reproducer = "# retained synthetic witness\n" * 4096 + "printf 'witnessneedle'\n"
    observation = "完整观察 café retained output\n" * 4096 + "observationtail"
    raw_bundle = {"messages": [{"role": "tool", "content": observation}]}
    raw_uri = runtime.cas.put_bundle(raw_bundle)
    package_uri = runtime.cas.put_bytes(b"synthetic task package\x00\xff")
    issue_uri = runtime.cas.put_bundle({"source_reproducer": reproducer})
    tasks = {
        PRIMARY: {**_task(PRIMARY, instruction), "cas_uri": package_uri},
        SECONDARY: _task(SECONDARY, "secondaryinstruction retained fixture"),
    }
    issues = {
        "issue-confirmed-primary": {
            **_issue("issue-confirmed-primary", PRIMARY, reproducer=reproducer),
            "cas_uri": issue_uri,
        },
        "issue-provisional-primary": _issue(
            "issue-provisional-primary",
            PRIMARY,
            status="provisional",
            defect_class="answer_format",
            reproducer="printf 'provisionalneedle'\n",
        ),
        "issue-refuted-secondary": _issue(
            "issue-refuted-secondary",
            SECONDARY,
            status="refuted",
            defect_class="coverage",
        ),
        "issue-confirmed-secondary": _issue(
            "issue-confirmed-secondary",
            SECONDARY,
            direction="unfair_fail",
            defect_class="bad_oracle",
        ),
    }
    receipts = {
        VERIFIED: {
            "status": "verified",
            "source": "retained-fixture",
            "checks": [{"status": "refuted"}],
        },
        PROVISIONAL: {"status": "provisional", "note": "source claim, not freshly verified"},
        REFUTED: {"status": "refuted", "note": "source rejection retained"},
    }
    traces = {
        VERIFIED: {
            **_trace(VERIFIED, PRIMARY, receipts[VERIFIED], outcome="wrong_pass"),
            "raw_cas_uri": raw_uri,
        },
        PROVISIONAL: _trace(PROVISIONAL, PRIMARY, receipts[PROVISIONAL]),
        REFUTED: _trace(REFUTED, SECONDARY, receipts[REFUTED], outcome="unfair_fail"),
    }
    steps = {
        VERIFIED: [
            {
                "step_index": 9,
                "role": "tool",
                "tool_name": "terminal",
                "content_text": observation,
                "observation_preview": "retained observation",
                "observation_cas_uri": raw_uri,
                "flags_json": {"status": "provisional", "source": "retained-fixture"},
            },
            {"step_index": 0, "role": "user", "content_text": "Synthetic bounded fixture request."},
            {
                "step_index": 2,
                "role": "assistant",
                "tool_name": "terminal",
                "command_or_action": "printf 'synthetic fixture'",
                "content_text": "actionneedle",
                "flags_json": ["retained"],
            },
        ],
        PROVISIONAL: [
            {
                "step_index": 0,
                "role": "tool",
                "tool_name": "browser",
                "content_text": "browserneedle",
            }
        ],
        REFUTED: [
            {
                "step_index": 5,
                "role": "tool",
                "tool_name": "python",
                "content_text": "secondarytrace",
            }
        ],
    }
    for task in tasks.values():
        runtime.database.upsert_task(task)
    for issue in issues.values():
        runtime.database.upsert_task_issue(issue)
    for trace_id, trace in traces.items():
        trace["step_count"] = len(steps[trace_id])
        runtime.database.upsert_trace(trace, steps=steps[trace_id])
    return {
        "tasks": tasks,
        "issues": issues,
        "traces": traces,
        "steps": steps,
        "raw_uri": raw_uri,
        "raw_bundle": raw_bundle,
    }


def test_modal_definitions_are_local_handles(modal_cloud: ModuleType) -> None:
    import modal

    assert isinstance(modal_cloud.app, modal.App)
    assert modal_cloud.app.name == "evallab-cloud-production"
    assert isinstance(modal_cloud.store_volume, modal.Volume)
    assert modal_cloud.VOLUME_NAME == "evallab-cloud-store"
    assert Path("/data") == modal_cloud.DATA_ROOT


def test_repository_root_uses_fixed_cloud_mount_for_flattened_modal_import(
    modal_cloud: ModuleType,
    tmp_path: Path,
) -> None:
    assert Path("/opt/eval-lab") == modal_cloud._repository_root(
        "/root/modal_cloud.py",
        local=False,
    )
    assert tmp_path == modal_cloud._repository_root(
        str(tmp_path / "src/evallab/production/modal_cloud.py"),
        local=True,
    )


def test_all_runtime_stores_use_data_root(runtime: CloudRuntime, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    assert isinstance(runtime.queue, ContinuousWorkQueue)
    assert isinstance(runtime.database, CloudTraceDatabase)
    assert isinstance(runtime.cas, S3CompatibleCAS)
    for store, filename in (
        (runtime.queue, "prod.sqlite"),
        (runtime.database, "cloud-trace-db.sqlite"),
    ):
        paths = {
            Path(row[2]).resolve()
            for row in store._connection.execute("PRAGMA database_list")
            if row[2]
        }
        assert paths == {(data_root / filename).resolve()}
    assert runtime.cas.local_directory == data_root / "cas"
    assert not runtime.cas.bucket
    uri = runtime.cas.put_bytes(b"exact local CAS bytes\x00\xff")
    assert runtime.cas.get_bytes(uri) == b"exact local CAS bytes\x00\xff"
    assert (data_root / "cas/sha256" / uri.rsplit("/", 1)[-1]).is_file()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/state",
        "/api/stream",
        "/api/cloud-db/stats",
        "/api/cloud-db/tasks",
        "/api/cloud-db/issues",
        "/api/cloud-db/traces",
        "/api/cloud-db/benchmarks",
        "/api/not-a-route",
    ],
)
def test_dashboard_and_all_api_paths_require_auth(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}]
)
def test_query_token_and_invalid_credentials_do_not_authorize(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    response = client.get("/api/state", params={"token": TOKEN}, headers=headers)
    assert response.status_code == 401
    assert TOKEN not in response.text
    assert "set-cookie" not in response.headers


def test_bearer_serves_protected_dashboard_and_state(client: TestClient) -> None:
    dashboard = client.get("/", headers=AUTH)
    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"].startswith("text/html")
    assert TOKEN not in dashboard.text
    assert client.get("/api/state", headers=AUTH).status_code == 200


def test_auth_fragment_page_is_public_without_token_or_data(
    client: TestClient,
    records: dict[str, Any],
) -> None:
    response = client.get(f"/auth#token={TOKEN}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert TOKEN not in response.text
    assert "primaryneedle" not in response.text
    assert "witnessneedle" not in response.text
    assert "set-cookie" not in response.headers
    assert client.get("/api/state").status_code == 401


def test_login_rejects_bad_token_and_sets_secure_session_cookie(client: TestClient) -> None:
    denied = client.post("/auth", json={"token": "wrong-cloud-token"})
    assert denied.status_code == 401
    assert "set-cookie" not in denied.headers
    assert client.get("/api/state").status_code == 401

    accepted = client.post("/auth", json={"token": TOKEN})
    assert accepted.status_code == 200
    assert TOKEN not in accepted.text
    cookies = SimpleCookie()
    cookies.load(accepted.headers["set-cookie"])
    session = cookies[COOKIE]
    assert session["secure"] is True
    assert session["httponly"] is True
    assert session["samesite"].lower() == "lax"
    assert session["path"] == "/"
    assert client.get("/").status_code == 200
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/cloud-db/stats").status_code == 200
    assert client.get("http://cloud.test/api/state").status_code == 401


def test_stats_count_real_stores_and_cas_objects(
    client: TestClient,
    records: dict[str, Any],
    tmp_path: Path,
) -> None:
    response = client.get("/api/cloud-db/stats", headers=AUTH)
    assert response.status_code == 200
    stats = response.json()
    assert stats["backend"] == "modal"
    assert stats["database"] == str(tmp_path / "data/cloud-trace-db.sqlite")
    expected = {"corpus_tasks": 2, "task_issues": 4, "agent_traces": 3, "trace_steps": 5}
    for table, count in expected.items():
        assert type(stats[table]) is int
        assert stats[table] == count
    assert type(stats["cas_objects"]) is int
    assert stats["cas_objects"] == 3


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {PRIMARY, SECONDARY}),
        ({"task_id": PRIMARY}, {PRIMARY}),
        ({"q": "PRIMARYNEEDLE"}, {PRIMARY}),
        ({"task_id": PRIMARY, "q": "secondaryinstruction"}, set()),
        ({"q": "missingneedle"}, set()),
    ],
)
def test_task_queries_return_complete_source_records(
    client: TestClient,
    records: dict[str, Any],
    params: dict[str, str],
    expected: set[str],
) -> None:
    response = client.get("/api/cloud-db/tasks", headers=AUTH, params=params)
    assert response.status_code == 200
    rows = response.json()["tasks"]
    assert {row["task_id"] for row in rows} == expected
    assert rows == [records["tasks"][task_id] for task_id in sorted(expected)]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {},
            {
                "issue-confirmed-primary",
                "issue-provisional-primary",
                "issue-refuted-secondary",
                "issue-confirmed-secondary",
            },
        ),
        ({"only_verified": "true"}, {"issue-confirmed-primary", "issue-confirmed-secondary"}),
        ({"task_id": PRIMARY}, {"issue-confirmed-primary", "issue-provisional-primary"}),
        ({"direction": "unfair_fail"}, {"issue-confirmed-secondary"}),
        ({"defect_class": "insufficient_checking"}, {"issue-confirmed-primary"}),
        ({"defect_class": "insuff"}, set()),
        ({"q": "WITNESSNEEDLE"}, {"issue-confirmed-primary"}),
        ({"q": "primaryneedle"}, {"issue-confirmed-primary", "issue-provisional-primary"}),
        ({"q": "provisionalneedle", "only_verified": "true"}, set()),
        (
            {
                "task_id": PRIMARY,
                "direction": "wrong_pass",
                "defect_class": "insufficient_checking",
                "q": "witnessneedle",
                "only_verified": "true",
            },
            {"issue-confirmed-primary"},
        ),
        ({"task_id": "missing-task"}, set()),
    ],
)
def test_issue_queries_preserve_full_evidence_nested_task_and_status(
    client: TestClient,
    records: dict[str, Any],
    params: dict[str, str],
    expected: set[str],
) -> None:
    response = client.get("/api/cloud-db/issues", headers=AUTH, params=params)
    assert response.status_code == 200
    rows = response.json()["issues"]
    assert {row["issue_id"] for row in rows} == expected
    for row in rows:
        source = records["issues"][row["issue_id"]]
        assert row == {**source, "task": records["tasks"][source["task_id"]]}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {VERIFIED, PROVISIONAL, REFUTED}),
        ({"only_verified": "true"}, {VERIFIED}),
        ({"only_verified": "false"}, {VERIFIED, PROVISIONAL, REFUTED}),
        ({"task_id": PRIMARY}, {VERIFIED, PROVISIONAL}),
        ({"direction": "unfair_fail"}, {REFUTED}),
        ({"defect_class": "insufficient_checking"}, {VERIFIED, PROVISIONAL}),
        ({"tool_name": "terminal"}, {VERIFIED}),
        ({"tool_name": "term"}, set()),
        ({"q": "actionneedle"}, {VERIFIED}),
        ({"q": "observationtail"}, {VERIFIED}),
        ({"q": "witnessneedle"}, {VERIFIED, PROVISIONAL}),
        ({"q": "browserneedle"}, {PROVISIONAL}),
        ({"task_id": SECONDARY, "direction": "wrong_pass", "defect_class": "bad_oracle"}, set()),
        (
            {
                "q": "primaryneedle",
                "task_id": PRIMARY,
                "direction": "wrong_pass",
                "defect_class": "insufficient_checking",
                "tool_name": "terminal",
                "only_verified": "true",
            },
            {VERIFIED},
        ),
        ({"q": "missingneedle"}, set()),
    ],
)
def test_trace_queries_return_fully_hydrated_untruncated_ordered_source_data(
    client: TestClient,
    runtime: CloudRuntime,
    records: dict[str, Any],
    params: dict[str, str],
    expected: set[str],
) -> None:
    response = client.get("/api/cloud-db/traces", headers=AUTH, params=params)
    assert response.status_code == 200
    rows = response.json()["traces"]
    assert {row["trace_id"] for row in rows} == expected
    for row in rows:
        trace_id = row["trace_id"]
        source = records["traces"][trace_id]
        assert {key: row[key] for key in source} == source
        assert row["task"] == records["tasks"][source["task_id"]]
        assert row["issues"] == [
            issue
            for issue_id, issue in sorted(records["issues"].items())
            if issue["task_id"] == source["task_id"]
        ]
        ordered = sorted(records["steps"][trace_id], key=lambda step: step["step_index"])
        assert [step["step_index"] for step in row["steps"]] == [
            step["step_index"] for step in ordered
        ]
        for actual, original in zip(row["steps"], ordered, strict=True):
            assert {key: actual[key] for key in original} == original
        if trace_id == VERIFIED:
            assert runtime.cas.get_bundle(row["raw_cas_uri"]) == records["raw_bundle"]


def test_database_endpoints_do_not_clip_result_collections(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    count = 137
    for index in range(count):
        identifier = f"bulk-{index:03}"
        runtime.database.upsert_task(_task(identifier, "Synthetic collection fixture"))
        runtime.database.upsert_task_issue(_issue(identifier, identifier))
        runtime.database.upsert_trace(
            _trace(identifier, identifier, None),
            steps=[{"step_index": 0, "role": "user", "content_text": identifier}],
        )
    for collection, identifier_key in (
        ("tasks", "task_id"),
        ("issues", "issue_id"),
        ("traces", "trace_id"),
    ):
        response = client.get(f"/api/cloud-db/{collection}", headers=AUTH)
        assert response.status_code == 200
        rows = response.json()[collection]
        assert len(rows) == count
        assert {row[identifier_key] for row in rows} == {
            f"bulk-{index:03}" for index in range(count)
        }


def test_cloud_db_serves_only_dev_tasks_admitted_by_label_seal_and_ledger(
    client: TestClient,
    runtime: CloudRuntime,
    records: dict[str, Any],
) -> None:
    served = {PRIMARY, SECONDARY, "ledger-dev", "mixed-case"}
    withheld = {"label-test", "sealed-task-name", "ledger-test", "ledger-unassigned"}
    for task_id, dataset, split in (
        ("ledger-dev", "synthetic-ledger-fixture", "dev"),
        ("mixed-case", "synthetic-cloud-fixture", "DEV"),
        ("label-test", "synthetic-cloud-fixture", "test"),
        # Labelled DEV, but a canonical TEST row seals the task name.
        ("sealed-task-name", "synthetic-cloud-fixture", "dev"),
        # Labelled DEV in the database, TEST (or absent) in the split ledger.
        ("ledger-test", "synthetic-ledger-fixture", "dev"),
        ("ledger-unassigned", "synthetic-ledger-fixture", "dev"),
    ):
        runtime.database.upsert_task(
            {**_task(task_id, f"{task_id} withheldneedle"), "dataset_id": dataset, "split": split}
        )
        runtime.database.upsert_task_issue(_issue(f"issue-{task_id}", task_id))
        runtime.database.upsert_trace(
            _trace(f"trace-{task_id}", task_id, None),
            steps=[{"step_index": 0, "role": "user", "content_text": f"{task_id} withheldneedle"}],
        )
    for collection in ("tasks", "issues", "traces"):
        for params in ({}, {"split": "dev"}, {"q": "withheldneedle"}):
            response = client.get(f"/api/cloud-db/{collection}", headers=AUTH, params=params)
            assert response.status_code == 200
            owners = {row["task_id"] for row in response.json()[collection]}
            assert owners <= served
            assert not owners & withheld
            if params.get("q"):
                assert owners == {"ledger-dev", "mixed-case"}
        for withheld_task in withheld:
            rows = client.get(
                f"/api/cloud-db/{collection}", headers=AUTH, params={"task_id": withheld_task}
            ).json()[collection]
            assert rows == []
        for split in ("test", "heldout", "held", "all", ""):
            refused = client.get(
                f"/api/cloud-db/{collection}", headers=AUTH, params={"split": split}
            )
            assert refused.status_code == 400
            assert "withheldneedle" not in refused.text


def _swarm_benchmark_report() -> dict[str, Any]:
    return {
        "schema": "evallab.swarm_bench/v1",
        "split": "dev",
        "budget_per_task": 3,
        "paid_api_spend_usd": 0.0,
        "model_calls": 0,
        "worker_telemetry": {"fixture": {"served_model": "fixture/cheap-model"}},
        "strategies": {
            "synthetic_strategy": {
                "selected_count": 1,
                "primary_hits_n": 0,
                "primary_total_n": 1,
                "secondary_hits_n": 0,
                "secondary_total_n": 1,
                "unique_key_precision": 0.0,
                "secondary_f1": 0.0,
                "within_task_budget": True,
                "selected_prediction_ids": ["synthetic-only"],
            },
        },
        "tracks": [{"track": 1, "status": "available"}],
    }


def _arm_comparison_report() -> dict[str, Any]:
    return {
        "schema": "evallab.arm_comparison/v1",
        "cohort": {"dataset_id": "synthetic-cloud-fixture", "split": "dev", "task_count": 1},
        "arms": [
            {
                "arm": arm,
                "label": "Synthetic fixture " + arm,
                "served_models": ["fixture/model-" + arm.lower()],
                "harness": "synthetic-fixture-harness",
                "retrieval": arm == "A",
                "metrics": {
                    "primary_hits_n": 0,
                    "primary_total_n": 1,
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1": 0.0,
                },
                "spend_usd": 0.0,
                "model_calls": 0,
                "artifact_sha256": arm.lower() * 64,
            }
            for arm in ("A", "B", "C")
        ],
        "generated_at": "2026-10-25T12:00:00Z",
    }


def test_retained_benchmarks_list_both_schemas_and_hash_exact_document_bytes(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    expected = []
    for name, report in (
        ("arms.json", _arm_comparison_report()),
        ("swarm.json", _swarm_benchmark_report()),
    ):
        raw = (json.dumps(report, indent=2) + "\n").encode("utf-8")
        (directory / name).write_bytes(raw)
        expected.append({"file": name, "sha256": hashlib.sha256(raw).hexdigest(), "report": report})
    (directory / "unrelated.txt").write_text("not a benchmark", encoding="utf-8")
    response = client.get("/api/cloud-db/benchmarks", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"benchmarks": expected, "rejected": []}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("directory_exists", [False, True], ids=["missing", "empty"])
def test_no_retained_benchmark_data_is_an_empty_listing_without_creating_directory(
    client: TestClient,
    runtime: CloudRuntime,
    directory_exists: bool,
) -> None:
    directory = runtime.data_root / "benchmarks"
    if directory_exists:
        directory.mkdir()
    assert client.get("/api/cloud-db/benchmarks", headers=AUTH).json() == {
        "benchmarks": [],
        "rejected": [],
    }
    assert directory.exists() is directory_exists


def test_invalid_benchmark_files_are_each_rejected_with_a_reason(
    client: TestClient,
    runtime: CloudRuntime,
    modal_cloud: ModuleType,
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    sealed = _arm_comparison_report()
    sealed["cohort"]["split"] = "test"
    missing_models = _arm_comparison_report()
    del missing_models["arms"][0]["served_models"]
    unknown_arm = _arm_comparison_report()
    unknown_arm["arms"][0]["arm"] = "D"
    sealed_swarm = _swarm_benchmark_report()
    sealed_swarm["split"] = "heldout"
    for name, report in (
        ("sealed.json", sealed),
        ("missing-models.json", missing_models),
        ("unknown-arm.json", unknown_arm),
        ("sealed-swarm.json", sealed_swarm),
    ):
        (directory / name).write_text(json.dumps(report), encoding="utf-8")
    (directory / "malformed.json").write_text('{"schema":', encoding="utf-8")
    (directory / "oversized.json").write_bytes(b" " * (modal_cloud._MAX_BENCHMARK_BYTES + 1))
    response = client.get("/api/cloud-db/benchmarks", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["benchmarks"] == []
    rejected = {item["file"]: item["reason"] for item in response.json()["rejected"]}
    assert set(rejected) == {
        "sealed.json",
        "missing-models.json",
        "unknown-arm.json",
        "sealed-swarm.json",
        "malformed.json",
        "oversized.json",
    }
    assert "cohort.split" in rejected["sealed.json"]
    assert "served_models" in rejected["missing-models.json"]
    assert "arms.0.arm" in rejected["unknown-arm.json"]
    assert "split" in rejected["sealed-swarm.json"]
    assert "Expecting" in rejected["malformed.json"]
    assert "byte limit" in rejected["oversized.json"]
    assert all(rejected.values())


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("cohort", "task_count"), True, "task_count"),
        (("arms", 0, "served_models"), [], "served_models"),
        (("arms", 0, "served_models"), ["model-without-provider"], "served_models"),
        (("arms", 0, "metrics", "precision"), 1.1, "precision"),
        (("arms", 0, "metrics", "primary_hits_n"), 2, "primary_hits_n"),
        (("arms", 0, "spend_usd"), -1.0, "spend_usd"),
        (("arms", 0, "artifact_sha256"), "not-a-digest", "artifact_sha256"),
        (("generated_at",), "2026-10-25T12:00:00", "generated_at"),
        (("schema",), "unknown/v1", "Unknown benchmark schema"),
        (("arms", 1, "arm"), "A", "arm identifiers must be unique"),
        (("extra",), "not-in-the-contract", "extra"),
    ],
)
def test_arm_comparison_schema_is_strict(
    client: TestClient,
    runtime: CloudRuntime,
    path: tuple[str | int, ...],
    value: Any,
    reason: str,
) -> None:
    report = _arm_comparison_report()
    target: Any = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report), encoding="utf-8")
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert len(result["rejected"]) == 1
    assert reason in result["rejected"][0]["reason"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"evallab.swarm_bench/v1","schema":"evallab.arm_comparison/v1"}',
        b'{"schema":"evallab.arm_comparison/v1","spend_usd":NaN}',
        b'{"schema":"evallab.swarm_bench/v1","extra":1e400}',
        b"\xff",
        b"[]",
    ],
)
def test_nonstandard_benchmark_json_is_rejected(
    client: TestClient,
    runtime: CloudRuntime,
    raw: bytes,
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_bytes(raw)
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert result["rejected"][0]["file"] == "invalid.json"
    assert result["rejected"][0]["reason"]


def test_benchmark_listing_rejects_symlinks_and_nonregular_json_entries(
    client: TestClient,
    runtime: CloudRuntime,
    tmp_path: Path,
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_arm_comparison_report()), encoding="utf-8")
    (directory / "escape.json").symlink_to(outside)
    (directory / "subdirectory.json").mkdir()
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert {item["file"] for item in result["rejected"]} == {"escape.json", "subdirectory.json"}
    assert all(item["reason"] for item in result["rejected"])


def test_benchmark_listing_does_not_follow_a_symlinked_directory(
    client: TestClient,
    runtime: CloudRuntime,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-benchmarks"
    outside.mkdir()
    (outside / "private.json").write_text(json.dumps(_arm_comparison_report()), encoding="utf-8")
    (runtime.data_root / "benchmarks").symlink_to(outside, target_is_directory=True)
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert result["rejected"][0]["file"] == "benchmarks"
    assert result["rejected"][0]["reason"]


def _append_live_event(
    queue: ContinuousWorkQueue,
    event_id: str,
    *,
    event_type: str = "trace.partial",
    status: str = "provisional",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return queue.append_event(
        {
            "event_id": event_id,
            "run_id": "cloud-live-run",
            "system": "task_quality",
            "event_type": event_type,
            "status": status,
            "prefix_step": 2,
            "ts": "2026-10-24T12:00:00+00:00",
            "payload": payload
            if payload is not None
            else {"step": {"step_id": 2, "message": event_id}},
        }
    )


def test_cloud_runtime_refuses_receiptless_asset_via_the_store(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    _append_live_event(
        runtime.queue,
        "bare-asset",
        event_type="asset.verified",
        status="confirmed",
        payload={
            "dataset_id": "tb4-audit-v1",
            "item_id": "cloud-dev-item",
            "asset_id": "witness:bare",
            "kind": "exploit_witness",
            "source_event_id": "not-journaled",
            "candidate_digest": None,
            "original_reward": 1.0,
            "repaired_reward": 0.0,
        },
    )
    response = client.get("/api/assets", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["assets"] == []
    assert (
        client.get(
            "/api/assets/detail", headers=AUTH, params={"asset_id": "witness:bare"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/assets/export", headers=AUTH, json={"asset_id": "witness:bare"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("outside_roots", [False, True])
def test_cloud_runtime_inspects_replay_diffs_only_inside_evidence_roots(
    client: TestClient,
    runtime: CloudRuntime,
    tmp_path: Path,
    outside_roots: bool,
) -> None:
    snapshots = (tmp_path / "outside" if outside_roots else runtime.data_root) / "snapshots"
    original, repaired = snapshots / "original", snapshots / "repaired"
    for directory, body in (
        (original, "accepts_any = True\n"),
        (repaired, "accepts_any = False\n"),
    ):
        directory.mkdir(parents=True)
        (directory / "verifier.py").write_text(body, encoding="utf-8")
    _append_live_event(
        runtime.queue,
        "replay-observation",
        event_type="seeker.replay_observation",
        payload={
            "dataset_id": "tb4-audit-v1",
            "item_id": "cloud-dev-item",
            "original_task": str(original),
            "repaired_task": str(repaired),
            "snapshot_status": "snapshot_unverified",
            "recorded_rewards": {"original": 1.0, "repaired": 0.0},
        },
    )
    response = client.get("/api/grader-diff", headers=AUTH, params={"run_id": "cloud-live-run"})
    if outside_roots:
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "path_outside_roots"
        return
    assert response.status_code == 200
    diff = response.json()
    assert diff["original"]["reward"] is None and diff["repaired"]["reward"] is None
    verifier = next(file for file in diff["files"] if file["path"] == "verifier.py")
    assert verifier["original"] == "accepts_any = True\n"
    assert verifier["repaired"] == "accepts_any = False\n"
    assert "+accepts_any = False" in verifier["unified_diff"]


def test_state_keeps_existing_control_plane_shape_and_projects_live_events(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    _append_live_event(runtime.queue, "live-prefix")
    finding = _append_live_event(
        runtime.queue,
        "live-finding",
        event_type="qa.finding/v1",
        payload={
            "finding_id": "retained-finding",
            "summary": "Synthetic source finding",
            "severity": "high",
        },
    )
    response = client.get("/api/state", headers=AUTH)
    assert response.status_code == 200
    state = response.json()
    assert {
        "cursor",
        "last_seq",
        "queue",
        "systems",
        "run_items",
        "partial_traces",
        "suspicion_cards",
        "interventions",
        "verified_assets",
    } <= state.keys()
    assert set(state["systems"]) == {"task_quality", "reward_seeker", "misalignment"}
    assert state["cursor"] == finding["event_id"]
    assert state["last_seq"] == finding["seq"]
    assert state["partial_traces"][0]["steps"] == [{"step_id": 2, "message": "live-prefix"}]
    assert state["suspicion_cards"][0]["status"] == "provisional"

    latest = _append_live_event(
        runtime.queue,
        "live-refutation",
        event_type="qa.finding/v1",
        status="refuted",
        payload={"finding_id": "retained-finding", "summary": "Source refutation retained"},
    )
    updated = client.get("/api/state", headers=AUTH).json()
    assert updated["last_seq"] == latest["seq"]
    assert updated["suspicion_cards"][0]["status"] == "refuted"


def _stream(
    app: FastAPI,
    *,
    stop: Callable[[str], bool],
    cursor: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Disconnect the actual ASGI stream after a frame, never buffer it forever."""

    async def run() -> tuple[dict[str, Any], str]:
        disconnected = asyncio.Event()
        request_sent = False
        bodies: list[str] = []
        response: dict[str, Any] = {}

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response.update(message)
            elif message["type"] == "http.response.body":
                bodies.append(message.get("body", b"").decode())
                if stop("".join(bodies)):
                    disconnected.set()

        headers = [(b"authorization", AUTH["Authorization"].encode())]
        if cursor is not None:
            headers.append((b"last-event-id", cursor.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/stream",
            "raw_path": b"/api/stream",
            "query_string": b"",
            "root_path": "",
            "server": ("cloud.test", 443),
            "client": ("testclient", 1),
            "headers": headers,
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return response, "".join(bodies)

    return asyncio.run(run())


def _frames(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.split("\n\n"):
        frame = {}
        for line in block.splitlines():
            if line.startswith(("id: ", "event: ", "data: ")):
                key, value = line.split(": ", 1)
                frame[key] = json.loads(value) if key == "data" else value
        if "data" in frame:
            frames.append(frame)
    return frames


def test_authenticated_sse_serves_snapshot_and_journal_replay_without_hanging(
    runtime: CloudRuntime,
) -> None:
    first = _append_live_event(runtime.queue, "stream-first")
    latest = _append_live_event(runtime.queue, "stream-next")
    response, body = _stream(runtime.app, stop=lambda text: bool(_frames(text)))
    assert response["status"] == 200
    assert dict(response["headers"])[b"content-type"].startswith(b"text/event-stream")
    initial = _frames(body)[0]
    assert initial["event"] == "state"
    assert initial["data"]["last_seq"] == latest["seq"]

    response, body = _stream(
        runtime.app,
        cursor=first["event_id"],
        stop=lambda text: any(frame.get("id") == latest["event_id"] for frame in _frames(text)),
    )
    assert response["status"] == 200
    assert _frames(body) == [{"id": latest["event_id"], "data": latest}]


@pytest.mark.parametrize("failure", [False, True], ids=["success", "step-error"])
def test_explicit_supervisor_step_shares_modal_policy_closes_workers_and_commits(
    modal_cloud: ModuleType,
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    created: dict[str, Any] = {}
    calls: list[str] = []
    committed: list[tuple[str, ...]] = []
    report = {"idle": True, "step": 1}
    original_publisher = modal_cloud.LocalJetStreamAdapter

    class Monitor:
        def __init__(self, **kwargs: Any) -> None:
            created["monitor"] = self
            created["monitor_kwargs"] = kwargs

        def close(self) -> None:
            calls.append("monitor.close")

    class Quality:
        def __init__(self, queue: ContinuousWorkQueue, **kwargs: Any) -> None:
            created["quality"] = self
            created["quality_queue"] = queue
            created["quality_kwargs"] = kwargs

    class Seeker:
        def __init__(self, queue: ContinuousWorkQueue, **kwargs: Any) -> None:
            created["seeker"] = self
            created["seeker_queue"] = queue
            created["seeker_kwargs"] = kwargs

    def publisher(storage: Path) -> Any:
        created["publisher_path"] = storage
        instance = original_publisher(storage)
        original_close = instance.close

        def close() -> None:
            calls.append("publisher.close")
            original_close()

        monkeypatch.setattr(instance, "close", close)
        return instance

    class Relay:
        def __init__(self, queue: ContinuousWorkQueue, publisher: Any, **kwargs: Any) -> None:
            created["relay"] = self
            self.publisher = publisher
            created["relay_queue"] = queue

        def close(self) -> None:
            calls.append("relay.close")
            self.publisher.close()

    class Supervisor:
        def __init__(self, queue: ContinuousWorkQueue, **kwargs: Any) -> None:
            created["supervisor_queue"] = queue
            created["supervisor_kwargs"] = kwargs

        def run_step(self) -> dict[str, Any]:
            calls.append("supervisor.run_step")
            if failure:
                raise RuntimeError("synthetic supervisor step failure")
            return report

        def close(self) -> None:
            calls.append("supervisor.close")

    for name, replacement in (
        ("MisalignmentLiveWorker", Monitor),
        ("TaskQualityLiveWorker", Quality),
        ("RewardSeekerLiveWorker", Seeker),
        ("LocalJetStreamAdapter", publisher),
        ("OutboxRelay", Relay),
        ("ProductionSupervisor", Supervisor),
    ):
        monkeypatch.setattr(modal_cloud, name, replacement)

    def commit() -> None:
        committed.append(tuple(calls))
        calls.append("commit")

    data_root = tmp_path / "data"
    with closing(
        modal_cloud.CloudRuntime(
            data_root=data_root,
            repo_root=repo_root,
            token=TOKEN,
            commit=commit,
        )
    ) as instance:
        with TestClient(instance.app, base_url="https://cloud.test") as transport:
            for path in ("/", "/api/state", "/api/cloud-db/stats", "/auth"):
                assert transport.get(path, headers=AUTH).status_code == 200
        assert created == {}
        assert calls == []
        assert committed == []

        if failure:
            with pytest.raises(RuntimeError, match="synthetic supervisor step failure"):
                instance.run_supervisor_step(approval="unit approved CPU execution", max_usd=2.75)
        else:
            result = instance.run_supervisor_step(
                approval="unit approved CPU execution", max_usd=2.75
            )
            assert result == {"backend": "modal", "allow_local_docker": False, **report}

        quality_kwargs = created["quality_kwargs"]
        seeker_kwargs = created["seeker_kwargs"]
        config = quality_kwargs["execution_config"]
        assert isinstance(config, ExecutionBackendConfig)
        assert seeker_kwargs["execution_config"] is config
        assert config.backend == "modal"
        assert config.allow_local_docker is False
        assert config.approval == "unit approved CPU execution"
        assert config.max_usd == 2.75
        assert Path(config.spend_ledger) == data_root / "cloud-spend.json"
        assert config.billing_tag == "cloud-offload-20261024"
        assert config.modal_app_name.startswith("cloud-offload-20261024")
        assert quality_kwargs["repo_root"] == repo_root
        assert seeker_kwargs["repo_root"] == repo_root
        assert Path(seeker_kwargs["work_root"]).is_relative_to(data_root)
        assert seeker_kwargs["allow_paid_lanes"] is False
        assert created["monitor_kwargs"].get("judge") is None
        assert created["monitor_kwargs"]["registry"] is instance.queue.registry
        assert created["publisher_path"] == data_root / "outbox.sqlite"
        for name in ("quality_queue", "seeker_queue", "relay_queue", "supervisor_queue"):
            assert created[name] is instance.queue
        for name, key in (
            ("monitor", "monitor"),
            ("task_quality", "quality"),
            ("seeker", "seeker"),
            ("relay", "relay"),
        ):
            assert created["supervisor_kwargs"][name] is created[key]
        assert calls.count("supervisor.run_step") == 1
        assert len(committed) == 1
        for name in ("supervisor.close", "monitor.close", "relay.close", "publisher.close"):
            assert name in committed[0]
        assert calls[-1] == "commit"


@pytest.mark.parametrize(
    ("approval", "cap"),
    [
        ("", 1.0),
        ("   ", 1.0),
        ("unit approval", 0.0),
        ("unit approval", -1.0),
        ("unit approval", float("inf")),
        ("unit approval", float("nan")),
        ("unit approval", True),
    ],
)
def test_missing_approval_or_invalid_cap_never_constructs_workers(
    runtime: CloudRuntime,
    modal_cloud: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    approval: str,
    cap: float,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unapproved execution must not construct a worker")

    for name in (
        "MisalignmentLiveWorker",
        "TaskQualityLiveWorker",
        "RewardSeekerLiveWorker",
        "LocalJetStreamAdapter",
        "OutboxRelay",
        "ProductionSupervisor",
    ):
        monkeypatch.setattr(modal_cloud, name, forbidden)
    with pytest.raises(ValueError):
        runtime.run_supervisor_step(approval=approval, max_usd=cap)


def test_close_persists_data_after_closing_local_stores(
    modal_cloud: ModuleType,
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    data_root = tmp_path / "data"
    instance = modal_cloud.CloudRuntime(
        data_root=data_root,
        repo_root=repo_root,
        token=TOKEN,
        commit=lambda: calls.append("commit"),
    )
    closed = False
    try:
        event = _append_live_event(instance.queue, "persisted-event")
        task = _task("persisted-task", "Exact persistent instruction café")
        instance.database.upsert_task(task)
        uri = instance.cas.put_bytes(b"persistent CAS fixture")
        original_queue_close = instance.queue.close
        original_database_close = instance.database.close

        def close_queue() -> None:
            original_queue_close()
            calls.append("queue.close")

        def close_database() -> None:
            original_database_close()
            calls.append("database.close")

        monkeypatch.setattr(instance.queue, "close", close_queue)
        monkeypatch.setattr(instance.database, "close", close_database)
        instance.close()
        closed = True
        assert set(calls[:-1]) == {"queue.close", "database.close"}
        assert calls[-1] == "commit"
        assert calls.count("commit") == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            instance.queue.queue_snapshot()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            instance.database.get_task("persisted-task")
        with ContinuousWorkQueue(
            data_root / "prod.sqlite", registry=instance.queue.registry
        ) as reopened:
            assert reopened.get_event(event["event_id"]) == event
        with CloudTraceDatabase(data_root / "cloud-trace-db.sqlite") as reopened_database:
            assert reopened_database.get_task("persisted-task") == task
        assert (
            S3CompatibleCAS(local_directory=data_root / "cas").get_bytes(uri)
            == b"persistent CAS fixture"
        )
    finally:
        if not closed:
            instance.close()
