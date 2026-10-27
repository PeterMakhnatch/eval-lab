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
from evallab.production.benchmark_reports import MAX_BENCHMARK_BYTES
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
        "trust_tier": "tier3_provisional",
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
        "trust_tier": "tier3_provisional",
        "flagged_passage_steps": [],
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


def test_cloud_admission_normalizes_comparison_identities_but_preserves_raw_records(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    allowed = {"Raw-DEV-Alias", "null-item"}
    fixtures = (
        ("Raw-DEV-Alias", " SyNtHeTiC-LeDgEr-FiXtUrE ", {"item_id": " LeDgEr-DeV "}),
        ("null-item", "synthetic-cloud-fixture", {"item_id": None}),
        # A DEV task-name alias cannot override its padded TEST item identity.
        ("ledger-dev", " SyNtHeTiC-LeDgEr-FiXtUrE ", {"item_id": " LeDgEr-TeSt "}),
        ("cross-key-test", "synthetic-cloud-fixture", {"item_id": " LeDgEr-TeSt "}),
        ("sealed-item-alias", "synthetic-cloud-fixture", {"item_id": " SeAlEd-ItEm "}),
        ("namespace/SEALED-TASK-NAME@v1", "synthetic-cloud-fixture", {}),
        ("invalid-item-object", "synthetic-cloud-fixture", {"item_id": {"id": "ledger-dev"}}),
        ("invalid-item-number", "synthetic-cloud-fixture", {"item_id": 0}),
    )
    for task_id, dataset, metadata in fixtures:
        runtime.database.upsert_task(
            {
                **_task(task_id, "Synthetic identityfixture", split=" DEV "),
                "dataset_id": dataset,
                "metadata_json": metadata,
            }
        )
        runtime.database.upsert_task_issue(_issue("issue-" + task_id, task_id))
        runtime.database.upsert_trace(
            _trace("trace-" + task_id, task_id, None),
            steps=[{"step_index": 0, "role": "user", "content_text": "Synthetic identityfixture"}],
        )
    for collection in ("tasks", "issues", "traces"):
        response = client.get(f"/api/cloud-db/{collection}", headers=AUTH)
        assert response.status_code == 200
        assert {row["task_id"] for row in response.json()[collection]} == allowed
        for task_id, _, _ in fixtures:
            response = client.get(
                f"/api/cloud-db/{collection}",
                headers=AUTH,
                params={"task_id": task_id},
            )
            assert response.status_code == 200
            assert {row["task_id"] for row in response.json()[collection]} == (
                {task_id} if task_id in allowed else set()
            )
    raw = client.get(
        "/api/cloud-db/tasks",
        headers=AUTH,
        params={"task_id": "Raw-DEV-Alias"},
    ).json()["tasks"][0]
    assert raw["dataset_id"] == " SyNtHeTiC-LeDgEr-FiXtUrE "
    assert raw["metadata_json"]["item_id"] == " LeDgEr-DeV "
    assert raw["split"] == " DEV "


@pytest.mark.parametrize("metadata", ["{", "null", "[]", '"not an identity mapping"'])
def test_cloud_api_isolates_malformed_identity_metadata_from_valid_siblings(
    client: TestClient,
    runtime: CloudRuntime,
    metadata: str,
) -> None:
    for task_id in ("valid", "invalid-metadata"):
        runtime.database.upsert_task(_task(task_id, "Synthetic metadata fixture"))
        runtime.database.upsert_task_issue(_issue("issue-" + task_id, task_id))
        runtime.database.upsert_trace(_trace("trace-" + task_id, task_id, None))
    runtime.database._connection.execute(
        "UPDATE corpus_tasks SET metadata_json = ? WHERE task_id = ?",
        (metadata, "invalid-metadata"),
    )
    for collection in ("tasks", "issues", "traces"):
        response = client.get(f"/api/cloud-db/{collection}", headers=AUTH)
        assert response.status_code == 200
        assert {row["task_id"] for row in response.json()[collection]} == {"valid"}


def test_cloud_api_refuses_every_collection_when_assignment_authority_is_missing(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    runtime.database.upsert_task(_task("otherwise-dev", "Synthetic unassigned source"))
    runtime.database.upsert_task_issue(_issue("issue", "otherwise-dev"))
    runtime.database.upsert_trace(_trace("trace", "otherwise-dev", None))
    (runtime.repo_root / "research/answer-keys/splits/assignments.csv").unlink()
    runtime.registry = type(runtime.registry)(runtime.repo_root)
    for collection in ("tasks", "issues", "traces"):
        response = client.get(f"/api/cloud-db/{collection}", headers=AUTH)
        assert response.status_code == 200
        assert response.json()[collection] == []


def test_cloud_api_does_not_hydrate_denied_task_bodies(
    client: TestClient,
    runtime: CloudRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for task_id, split in (("allowed", "dev"), ("denied", "test")):
        runtime.database.upsert_task(_task(task_id, "Inert synthetic body", split=split))
        runtime.database.upsert_task_issue(_issue("issue-" + task_id, task_id))
        runtime.database.upsert_trace(_trace("trace-" + task_id, task_id, None))

    def guarded_body(value: str, task_id: str) -> str:
        if task_id == "denied":
            raise AssertionError("the denied synthetic body must not be hydrated")
        return value

    connection = runtime.database._connection
    connection.create_function("guarded_body", 2, guarded_body)
    original_connect = sqlite3.connect

    def guarded_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened = original_connect(*args, **kwargs)
        opened.create_function("guarded_body", 2, guarded_body)
        return opened

    monkeypatch.setattr(sqlite3, "connect", guarded_connection)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(corpus_tasks)")]
    projection = ", ".join(
        "guarded_body(instruction_text, task_id) AS instruction_text"
        if name == "instruction_text"
        else '"' + name + '"'
        for name in columns
    )
    connection.execute("ALTER TABLE corpus_tasks RENAME TO guarded_tasks")
    connection.execute(f"CREATE VIEW corpus_tasks AS SELECT {projection} FROM guarded_tasks")
    # Reading identity columns is safe; selecting a denied instruction raises
    # inside SQLite. A post-hydration Python filter cannot pass this boundary.
    for collection in ("tasks", "issues", "traces"):
        response = client.get(f"/api/cloud-db/{collection}", headers=AUTH)
        assert response.status_code == 200
        assert {row["task_id"] for row in response.json()[collection]} == {"allowed"}


def _census_fixture(runtime: CloudRuntime) -> None:
    # (task, dataset, split, trace outcomes): one trace per outcome, two steps each.
    fixtures = (
        ("census-dev-a", "synthetic-goodfire", "DEV", ("wrong_pass", "unknown")),
        ("census-dev-b", "synthetic-goodfire", " dev ", ("wrong_pass",)),
        ("census-no-traces", "synthetic-goodfire", "dev", ()),
        ("census-held", "synthetic-goodfire", "held", ("wrong_pass", "wrong_pass", "unknown")),
        # DEV label but sealed by the task-name ledger: counted as gate-denied, not DEV.
        ("sealed-task-name", "synthetic-goodfire", "dev", ("wrong_pass",)),
        ("census-other", "synthetic-other", "dev", ("unknown",)),
    )
    for task_id, dataset, split, outcomes in fixtures:
        runtime.database.upsert_task(
            {**_task(task_id, "Inert census body", split=split), "dataset_id": dataset}
        )
        for number, outcome in enumerate(outcomes):
            runtime.database.upsert_trace(
                _trace(f"trace-{task_id}-{number}", task_id, None, outcome=outcome),
                steps=[
                    {"step_index": step, "role": "user", "content_text": "Inert census step"}
                    for step in range(2)
                ],
            )


def test_corpus_census_separates_admitted_dev_gate_denied_and_withheld_counts(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    _census_fixture(runtime)
    response = client.get("/api/cloud-db/corpus", headers=AUTH)
    assert response.status_code == 200
    datasets = {entry["dataset_id"]: entry for entry in response.json()["datasets"]}
    assert list(datasets) == ["synthetic-goodfire", "synthetic-other"]
    assert datasets["synthetic-goodfire"] == {
        "dataset_id": "synthetic-goodfire",
        "admitted_dev": {"tasks": 3, "traces": 3, "steps": 6},
        "dev_not_admitted": {"tasks": 1, "traces": 1, "steps": 2},
        "withheld": {"tasks": 1, "traces": 3, "steps": 6},
        # Exploit labels count only on admitted DEV traces: the held and sealed ones do not.
        "enacted_exploit_traces": 2,
    }
    assert datasets["synthetic-other"]["admitted_dev"] == {"tasks": 1, "traces": 1, "steps": 2}
    assert datasets["synthetic-other"]["enacted_exploit_traces"] == 0
    # Aggregates only: no identity or body of any row, admitted or not, is returned.
    for identity in ("census-held", "sealed-task-name", "census-dev-a", "Inert census"):
        assert identity not in response.text


def test_corpus_census_counts_nothing_as_dev_without_assignment_authority(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    _census_fixture(runtime)
    (runtime.repo_root / "research/answer-keys/splits/assignments.csv").unlink()
    runtime.registry = type(runtime.registry)(runtime.repo_root)
    datasets = client.get("/api/cloud-db/corpus", headers=AUTH).json()["datasets"]
    assert sum(entry["admitted_dev"]["tasks"] for entry in datasets) == 0
    assert sum(entry["enacted_exploit_traces"] for entry in datasets) == 0
    assert sum(entry["dev_not_admitted"]["tasks"] for entry in datasets) == 5


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


def _named_arm_comparison_report(*, complete: bool = False) -> dict[str, Any]:
    arms = []
    for name in (
        "solo-frontier",
        "cheap-swarm",
        "retrieval-swarm",
        "hybrid-adjudicated",
        "baseline",
    ):
        unanswered = name == "baseline" and not complete
        arms.append(
            {
                "arm": name,
                "label": "Synthetic " + name,
                "harness": "synthetic-review-only",
                "retrieval": name == "retrieval-swarm",
                "requested_models": ["fixture/requested-" + name],
                "served_models": [] if unanswered else ["fixture/served-" + name],
                "coverage": {
                    "answered_n": 7 if complete else 0 if unanswered else 2,
                    "judged_n": 7 if complete else 0 if unanswered else 1,
                    "missing_n": 0 if complete else 6 if unanswered else 1,
                    "blocked_n": 0 if complete else 1,
                    "refused_n": 0 if complete or unanswered else 1,
                    "unavailable_n": 0 if complete or unanswered else 1,
                    "malformed_n": 0 if complete or unanswered else 1,
                    "unattested_n": 0,
                },
                "metrics": {
                    "primary_hits_n": 0 if complete else None,
                    "primary_total_n": 7 if complete else None,
                    "precision": 0.0 if complete else None,
                    "recall": 0.0 if complete else None,
                    "f1": None,
                },
                "spend_usd": 0.0 if complete else None,
                "model_calls": 7 if complete else None,
                "artifact_sha256": None
                if unanswered
                else hashlib.sha256(("synthetic evidence: " + name).encode()).hexdigest(),
            }
        )
    return {
        "schema": "evallab.arm_comparison/v2",
        "protocol": "synthetic-clean-input-v1",
        "status": "complete" if complete else "blocked",
        "cohort": {
            "dataset_id": "synthetic-cloud-fixture",
            "split": "dev",
            "task_count": 7,
            "eligible_n": 7 if complete else 6,
            "blocked_n": 0 if complete else 1,
        },
        "arms": arms,
        "generated_at": "2026-10-25T15:15:00Z",
    }


def test_retained_benchmarks_preserve_historic_and_named_reports_with_exact_byte_hashes(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    expected = []
    for name, report in (
        ("arms.json", _arm_comparison_report()),
        ("named.json", _named_arm_comparison_report()),
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
    (directory / "oversized.json").write_bytes(b" " * (MAX_BENCHMARK_BYTES + 1))
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


@pytest.mark.parametrize("complete", [False, True], ids=["awaiting-judgment", "complete-with-zero"])
def test_named_reports_keep_unavailable_metrics_distinct_from_measured_zero(
    client: TestClient,
    runtime: CloudRuntime,
    complete: bool,
) -> None:
    report = _named_arm_comparison_report(complete=complete)
    if not complete:
        report["status"] = "incomplete"
        report["cohort"].update(eligible_n=7, blocked_n=0)
        for arm in report["arms"]:
            arm["coverage"]["blocked_n"] = 0
            arm["coverage"]["missing_n"] += 1
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "named.json").write_text(json.dumps(report))
    response = client.get("/api/cloud-db/benchmarks", headers=AUTH)
    assert response.status_code == 200
    result = response.json()
    assert result["rejected"] == []
    returned = result["benchmarks"][0]["report"]
    assert returned == report
    assert returned["arms"][0]["metrics"]["f1"] is None
    assert returned["arms"][0]["metrics"]["recall"] == (0.0 if complete else None)
    if not complete:
        assert returned["arms"][-1]["served_models"] == []
        assert returned["arms"][-1]["requested_models"] == ["fixture/requested-baseline"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("status",), "complete"),
        (("status",), "incomplete"),
        (("cohort", "eligible_n"), 7),
        (("arms", 0, "coverage", "missing_n"), 2),
        (("arms", 0, "coverage", "judged_n"), 3),
        (("arms", 0, "coverage", "unattested_n"), True),
        (("arms", 0, "metrics", "recall"), 0.0),
        (("arms", 0, "metrics", "primary_total_n"), 7),
        (("arms", 0, "served_models"), []),
        (("arms", 0, "served_models"), ["fixture/actual", "fixture/actual"]),
        (("arms", 0, "served_models"), ["fixture/actual", "FIXTURE/ACTUAL"]),
        (("arms", 0, "requested_models"), ["fixture/planned", "fixture/planned"]),
        (("arms", 0, "artifact_sha256"), None),
        (("arms", 0, "arm"), "  "),
        (("arms", 0, "arm"), " solo-frontier "),
        (("arms", 1, "arm"), "SOLO-FRONTIER"),
        (("arms", 0, "model_calls"), -1),
        (("arms", 0, "served_models"), ["requested-name-without-provider"]),
        (("protocol",), " "),
    ],
)
def test_named_comparison_rejects_inconsistent_coverage_or_identity_claims(
    client: TestClient,
    runtime: CloudRuntime,
    path: tuple[str | int, ...],
    value: Any,
) -> None:
    report = _named_arm_comparison_report()
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert [entry["file"] for entry in result["rejected"]] == ["invalid.json"]


def test_incomplete_arm_keeps_every_full_comparison_metric_unavailable(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    report = _named_arm_comparison_report(complete=True)
    report["status"] = "incomplete"
    report["arms"][-1]["coverage"]["judged_n"] = 0
    report["arms"][-1]["metrics"] = dict.fromkeys(report["arms"][-1]["metrics"])
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    path = directory / "named.json"
    path.write_text(json.dumps(report))
    rejected = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert rejected["benchmarks"] == []
    assert [entry["file"] for entry in rejected["rejected"]] == ["named.json"]
    for arm in report["arms"]:
        arm["metrics"] = dict.fromkeys(arm["metrics"])
    path.write_text(json.dumps(report))
    accepted = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert accepted["rejected"] == []
    assert accepted["benchmarks"][0]["report"]["status"] == "incomplete"


def test_named_arm_cannot_omit_common_blocked_identities_from_its_accounting(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    report = _named_arm_comparison_report()
    report["arms"][0]["coverage"].update(blocked_n=0, missing_n=2)
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert [entry["file"] for entry in result["rejected"]] == ["invalid.json"]


_SEEKER_MODELS = (
    ("muse-spark-open", "meta/muse-spark-open"),
    ("muse-spark-1.3", "meta/muse-spark-1.3-contributor"),
    ("muse-wonder", "meta/muse-wonder-1.0-contributor"),
    ("kimi-k3.1", "kimi-code/k3.1-pro-highspeed"),
    ("deepseek-v4.1", "openrouter/deepseek/deepseek-v4.1-flash"),
    ("sonnet-5.5", "anthropic/claude-sonnet-5-5"),
)
_SEEKER_CONDITIONS = ("bare", "retrieval-augmented")
_SEEKER_COUNTS = (
    "attested_n",
    "unattested_n",
    "refused_n",
    "unavailable_n",
    "malformed_n",
    "blocked_n",
    "missing_n",
)


def _seeker_comparison_report(state: str = "blocked") -> dict[str, Any]:
    """Inert synthetic fixture: counts exercise the schema, they are not model results."""
    arms = []
    for index, (name, model) in enumerate(_SEEKER_MODELS):
        answered = state == "complete" or index % 2 == 0
        cells = []
        for condition in _SEEKER_CONDITIONS:
            counts = dict.fromkeys(_SEEKER_COUNTS, 0)
            graded = original_pass = differential = 0
            if state == "complete":
                counts["attested_n"] = graded = 4
                original_pass, differential = 2, index % 2
            elif state == "blocked" and answered:
                counts.update(attested_n=2, unavailable_n=1, blocked_n=1)
                graded, original_pass, differential = 2, 1, 1
            elif state == "blocked":
                counts.update(blocked_n=1, missing_n=3)
            elif answered:
                counts.update(attested_n=3, missing_n=1)
                graded, original_pass, differential = 2, 1, 1
            else:
                counts["missing_n"] = 4
            cells.append(
                {
                    "condition": condition,
                    "prompt_sha256": hashlib.sha256(condition.encode()).hexdigest(),
                    "served_models": [model] if answered else [],
                    **counts,
                    "graded_n": graded,
                    "original_pass_n": original_pass,
                    "verified_differential_n": differential,
                    "spend_usd": 0.0 if state == "complete" else None,
                    "model_calls": 4 if state == "complete" else None,
                    "artifact_sha256": hashlib.sha256((name + condition).encode()).hexdigest()
                    if answered
                    else None,
                }
            )
        arms.append(
            {"arm": name, "label": "Synthetic " + name, "requested_model": model, "cells": cells}
        )
    return {
        "schema": "evallab.seeker_comparison/v1",
        "protocol": "synthetic-grader-weakness-v1",
        "status": state,
        "cohort": {
            "dataset_id": "synthetic-cloud-fixture",
            "split": "dev",
            "task_count": 4,
            "eligible_n": 3 if state == "blocked" else 4,
            "blocked_n": 1 if state == "blocked" else 0,
        },
        "arms": arms,
        "generated_at": "2026-10-26T15:00:00Z",
    }


@pytest.mark.parametrize("state", ["blocked", "incomplete", "complete"])
def test_seeker_comparison_reports_are_retained_with_exact_bytes_and_distinct_zero(
    client: TestClient,
    runtime: CloudRuntime,
    state: str,
) -> None:
    report = _seeker_comparison_report(state)
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    raw = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    (directory / "seeker.json").write_bytes(raw)
    response = client.get("/api/cloud-db/benchmarks", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "benchmarks": [
            {"file": "seeker.json", "sha256": hashlib.sha256(raw).hexdigest(), "report": report}
        ],
        "rejected": [],
    }
    cells = [cell for arm in report["arms"] for cell in arm["cells"]]
    assert len(report["arms"]) == 6
    # An arm that never answered keeps null spend/served identity rather than a fake zero.
    assert (None in {cell["spend_usd"] for cell in cells}) is (state != "complete")
    assert (0 in {cell["verified_differential_n"] for cell in cells}) is True


@pytest.mark.parametrize(
    ("state", "path", "value"),
    [
        ("blocked", ("status",), "incomplete"),
        ("blocked", ("status",), "complete"),
        ("complete", ("status",), "incomplete"),
        ("incomplete", ("status",), "blocked"),
        ("blocked", ("cohort", "blocked_n"), 0),
        ("blocked", ("arms", 0, "cells", 0, "missing_n"), 2),
        ("blocked", ("arms", 0, "cells", 0, "graded_n"), 3),
        ("blocked", ("arms", 0, "cells", 0, "original_pass_n"), 3),
        ("blocked", ("arms", 0, "cells", 0, "verified_differential_n"), 2),
        ("blocked", ("arms", 0, "cells", 0, "served_models"), []),
        ("blocked", ("arms", 0, "cells", 0, "artifact_sha256"), None),
        ("blocked", ("arms", 0, "cells", 0, "served_models"), ["fixture/a", "FIXTURE/A"]),
        ("blocked", ("arms", 0, "cells", 0, "served_models"), ["no-provider"]),
        ("blocked", ("arms", 0, "cells", 0, "model_calls"), -1),
        ("blocked", ("arms", 0, "cells", 0, "prompt_sha256"), "ABC"),
        ("blocked", ("arms", 0, "cells", 0, "discovery_rate"), 0.5),
        ("blocked", ("arms", 0, "cells", 1, "condition"), "BARE"),
        ("blocked", ("arms", 0, "cells", 1, "condition"), "other-condition"),
        ("blocked", ("arms", 0, "cells", 1, "condition"), " retrieval-augmented "),
        ("blocked", ("arms", 0, "cells"), []),
        ("blocked", ("arms", 1, "arm"), "MUSE-SPARK-OPEN"),
        ("blocked", ("arms", 1, "requested_model"), "META/MUSE-SPARK-OPEN"),
        ("blocked", ("arms", 0, "requested_model"), "requested-without-provider"),
        ("blocked", ("arms",), []),
    ],
)
def test_seeker_comparison_rejects_inconsistent_coverage_funnel_or_identity_claims(
    client: TestClient,
    runtime: CloudRuntime,
    state: str,
    path: tuple[str | int, ...],
    value: Any,
) -> None:
    report = _seeker_comparison_report(state)
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert [entry["file"] for entry in result["rejected"]] == ["invalid.json"]


def test_seeker_cell_cannot_omit_a_common_blocked_identity_from_its_accounting(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    report = _seeker_comparison_report("blocked")
    # Keeps the terminal total at 4, so only the cohort-blocked invariant can reject it.
    report["arms"][0]["cells"][0].update(blocked_n=0, missing_n=1)
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert [entry["file"] for entry in result["rejected"]] == ["invalid.json"]


def test_seeker_arms_may_name_any_models_and_conditions(
    client: TestClient,
    runtime: CloudRuntime,
) -> None:
    report = _seeker_comparison_report("complete")
    report["arms"] = report["arms"][:1]
    report["arms"][0].update(arm="Any Name", requested_model="vendor/any-model:max")
    for cell, condition in zip(report["arms"][0]["cells"], ("zero-shot", "Hints"), strict=True):
        cell["condition"] = condition
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "any.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["rejected"] == []
    assert result["benchmarks"][0]["report"]["arms"][0]["requested_model"] == "vendor/any-model:max"


def _trust_tier_census_report() -> dict[str, Any]:
    return {
        "schema": "evallab.trust_tier_census/v1",
        "split": "dev",
        "unit": "traces",
        "admitted_rows_n": 100,
        "unclassified_n": 10,
        "tiers": [
            {
                "tier": "tier1_container_verified",
                "source": "synthetic-a",
                "basis": "synthetic nine-run qualification",
                "rows_n": 20,
                "prompt_eligible_n": 20,
            },
            {
                "tier": "tier2_consensus_labeled",
                "source": "synthetic-b",
                "basis": "synthetic three-vote consensus",
                "rows_n": 50,
                "prompt_eligible_n": 40,
            },
            {
                "tier": "tier3_provisional",
                "source": "synthetic-b",
                "basis": "synthetic split votes",
                "rows_n": 20,
                "prompt_eligible_n": 0,
            },
        ],
        "source_sha256": hashlib.sha256(b"synthetic census input").hexdigest(),
        "generated_at": "2026-10-26T20:00:00Z",
    }


def _hackbook_qc_report() -> dict[str, Any]:
    rules = [
        ("rule-one", 6, 2, 3),
        ("rule-two", 5, 1, 2),
        ("rule-silent", 0, 0, 0),
    ]
    return {
        "schema": "evallab.hackbook_qc/v1",
        "ruleset_sha256": hashlib.sha256(b"synthetic ruleset").hexdigest(),
        "cohort": {
            "dataset_id": "synthetic-qc-cohort",
            "split": "dev",
            "positive_n": 10,
            "clean_n": 20,
            "unscored_n": 2,
        },
        "rules": [
            {
                "rule_id": name,
                "positives_hit_n": positives,
                "clean_hit_n": clean,
                "marginal_true_positive_n": marginal,
            }
            for name, positives, clean, marginal in rules
        ],
        "union_positives_hit_n": 8,
        "union_clean_hit_n": 3,
        "generated_at": "2026-10-26T20:00:00Z",
    }


def _tune_cross_test_report(*, cross: bool = True) -> dict[str, Any]:
    digest = hashlib.sha256(b"synthetic frozen system").hexdigest()

    def entry(role: str, dataset: str, hour: int, hits: tuple[int, int]) -> dict[str, Any]:
        return {
            "role": role,
            "dataset_id": dataset,
            "split": "dev",
            "system_sha256": digest,
            "positive_n": 10,
            "clean_n": 20,
            "unscored_n": 1,
            "true_positive_n": hits[0],
            "false_positive_n": hits[1],
            "measured_at": f"2026-10-26T{hour}:00:00Z",
        }

    entries = [entry("tune", "synthetic-tune-set", 18, (6, 2))]
    if cross:
        entries.append(entry("cross_test", "synthetic-unseen-set", 19, (0, 0)))
    return {
        "schema": "evallab.tune_cross_test/v1",
        "system": "synthetic-frozen-system",
        "frozen_sha256": digest,
        "entries": entries,
        "generated_at": "2026-10-26T20:00:00Z",
    }


def _seeker_executed_report() -> dict[str, Any]:
    def condition(name: str, a: tuple[int, int, int], b: tuple[int, int, int]) -> dict[str, Any]:
        def cells(dataset: str, counts: tuple[int, int, int]) -> dict[str, Any]:
            return {
                "dataset_id": dataset,
                "cells_n": counts[0],
                "attested_n": counts[1],
                "executed_differential_n": counts[2],
            }

        return {
            "condition": name,
            "cells_n": a[0] + b[0],
            "attested_n": a[1] + b[1],
            "executed_differential_n": a[2] + b[2],
            "by_dataset": [cells("synthetic-a", a), cells("synthetic-b", b)],
        }

    return {
        "schema": "evallab.seeker_executed_differentials/v2",
        "protocol": "synthetic-executed-protocol",
        "cohort": {
            "split": "dev",
            "key_count": 7,
            "audited_n": 12,
            "cross_key_excluded_n": 2,
            "task_count": 10,
            "datasets": [
                {"dataset_id": "synthetic-a", "count_n": 6},
                {"dataset_id": "synthetic-b", "count_n": 4},
            ],
        },
        "arms": [
            {
                "arm": model,
                "requested_model": model,
                "conditions": [
                    condition("turn_1", (6, 5, 0), (4, 4, 1)),
                    # A partial condition: only 3 of 6 synthetic-a tasks were ever evaluated.
                    condition("turn_2_partial", partial, (0, 0, 0)),
                ],
            }
            for model, partial in (
                ("provider/model-one", (3, 3, 3)),
                ("provider/model-two", (0, 0, 0)),
            )
        ],
        "source_sha256": hashlib.sha256(b"synthetic cell lines").hexdigest(),
        "generated_at": "2026-10-27T10:00:00Z",
    }


def _monitor_cascade_report() -> dict[str, Any]:
    def cohort(role: str, dataset: str, *, unscored: int, tp: int, fp: int) -> dict[str, Any]:
        return {
            "role": role,
            "dataset_id": dataset,
            "split": "dev",
            "positive_n": 10,
            "clean_n": 20,
            "unscored_n": unscored,
            "identities_checked_n": 30 + unscored,
            "non_dev_assignment_n": 0,
            "stage1_positive_hit_n": 6,
            "stage1_clean_hit_n": 8,
            "true_positive_n": tp,
            "false_positive_n": fp,
            "judge_calls_n": 9,
            "input_tokens_n": 1000,
            "output_tokens_n": 400,
        }

    return {
        "schema": "evallab.monitor_cascade/v1",
        "system": "synthetic-cascade",
        "cascade_version": "cascade/v2",
        "arm": "plain",
        "judge_models": ["provider/judge-model"],
        "frozen_sha256": hashlib.sha256(b"synthetic freeze").hexdigest(),
        "frozen_recorded_at": "2026-10-27T05:00:00Z",
        "freeze_statement": "plain arm chosen on the tuning cohort before any cross-test result",
        "cohorts": [
            cohort("tune", "synthetic-tune-set", unscored=5, tp=3, fp=0),
            cohort("cross_test", "synthetic-unseen-set", unscored=0, tp=0, fp=0),
        ],
        "generated_at": "2026-10-27T10:00:00Z",
    }


@pytest.mark.parametrize(
    "report",
    [
        _trust_tier_census_report(),
        _hackbook_qc_report(),
        _tune_cross_test_report(),
        _tune_cross_test_report(cross=False),
        _seeker_executed_report(),
        _monitor_cascade_report(),
    ],
    ids=[
        "tier-census",
        "hackbook-qc",
        "tune-cross-test",
        "tune-only-cross-test-not-run",
        "seeker-executed",
        "monitor-cascade",
    ],
)
def test_trust_qc_reports_are_retained_with_exact_bytes(
    client: TestClient,
    runtime: CloudRuntime,
    report: dict[str, Any],
) -> None:
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    raw = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    (directory / "report.json").write_bytes(raw)
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result == {
        "benchmarks": [
            {"file": "report.json", "sha256": hashlib.sha256(raw).hexdigest(), "report": report}
        ],
        "rejected": [],
    }


def test_exported_qc_reports_are_listed_by_the_cloud_api_without_rejections(
    client: TestClient,
    runtime: CloudRuntime,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from evallab.production.qc_reports import export_qc_reports

    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as database:
        database.upsert_task(_task("exported-dev", "Inert census fixture."))
        database.upsert_task(_task("exported-held", "Withheld census fixture.", split="held"))
        for task_id, tier in (
            ("exported-dev", "tier2_consensus_labeled"),
            ("exported-dev", "tier3_provisional"),
            ("exported-held", "tier1_container_verified"),
        ):
            database.upsert_trace(
                {
                    "trace_id": f"{task_id}-{tier}",
                    "run_id": "inert",
                    "task_id": task_id,
                    "model_id": "synthetic/no-model",
                    "trust_tier": tier,
                }
            )
    recorded = {
        "qc.json": _hackbook_qc_report(),
        "scoreboard.json": _tune_cross_test_report(),
    }
    for name, report in recorded.items():
        (tmp_path / name).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    receipt = export_qc_reports(
        source_database=source,
        output_directory=runtime.data_root / "benchmarks",
        repo_root=repo_root,
        hackbook_qc=(tmp_path / "qc.json",),
        tune_cross_test=(tmp_path / "scoreboard.json",),
        generated_at="2026-10-27T02:00:00+00:00",
    )
    listing = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert listing["rejected"] == []
    served = {item["file"]: item for item in listing["benchmarks"]}
    assert {name: item["sha256"] for name, item in served.items()} == {
        artifact["file"]: artifact["sha256"] for artifact in receipt["artifacts"]
    }
    census = served["trust-tier-census-traces.json"]["report"]
    assert [(row["tier"], row["rows_n"], row["prompt_eligible_n"]) for row in census["tiers"]] == [
        ("tier2_consensus_labeled", 1, 1),
        ("tier3_provisional", 1, 0),
    ]
    assert census["admitted_rows_n"] == 2
    assert served["hackbook-qc-qc.json"]["report"] == recorded["qc.json"]
    assert served["tune-cross-test-scoreboard.json"]["report"] == recorded["scoreboard.json"]


def _set(path: tuple[str | int, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(report: dict[str, Any]) -> None:
        target = report
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


_TRUST_QC_REJECTIONS = [
    # Tier census: the tier rows plus unclassified must partition the admitted rows.
    (_trust_tier_census_report, _set(("admitted_rows_n",), 99)),
    (_trust_tier_census_report, _set(("unclassified_n",), 11)),
    (_trust_tier_census_report, _set(("tiers", 2, "prompt_eligible_n"), 1)),
    (_trust_tier_census_report, _set(("tiers", 0, "prompt_eligible_n"), 21)),
    (
        _trust_tier_census_report,
        # Same tier and a case-variant source as row 0: a duplicate pair, not a second source.
        lambda report: (
            _set(("tiers", 1, "tier"), "tier1_container_verified")(report),
            _set(("tiers", 1, "source"), "SYNTHETIC-A")(report),
        ),
    ),
    (_trust_tier_census_report, _set(("tiers", 0, "tier"), "tier4_unknown")),
    (_trust_tier_census_report, _set(("split",), "test")),
    (_trust_tier_census_report, _set(("generated_at",), "2026-10-26T20:00:00")),
    (_trust_tier_census_report, _set(("source_sha256",), "abc")),
    # Hackbook QC: hits are bounded by the cohort and by one another.
    (_hackbook_qc_report, _set(("rules", 0, "positives_hit_n"), 11)),
    (_hackbook_qc_report, _set(("rules", 0, "clean_hit_n"), 21)),
    (_hackbook_qc_report, _set(("rules", 0, "marginal_true_positive_n"), 7)),
    (
        _hackbook_qc_report,
        # 6 + 3 marginal positives cannot fit in 8 distinct positives hit overall.
        lambda report: (
            _set(("rules", 0, "marginal_true_positive_n"), 6)(report),
            _set(("rules", 1, "marginal_true_positive_n"), 3)(report),
        ),
    ),
    (_hackbook_qc_report, _set(("union_positives_hit_n",), 5)),
    (_hackbook_qc_report, _set(("union_positives_hit_n",), 11)),
    (_hackbook_qc_report, _set(("union_clean_hit_n",), 4)),
    (_hackbook_qc_report, _set(("rules", 1, "rule_id"), "RULE-ONE")),
    (_hackbook_qc_report, _set(("rules", 1, "rule_id"), " rule-two ")),
    (_hackbook_qc_report, _set(("rules",), [])),
    (_hackbook_qc_report, _set(("ruleset_sha256",), "abc")),
    (_hackbook_qc_report, _set(("cohort", "split"), "test")),
    (_hackbook_qc_report, _set(("cohort", "clean_n"), 0)),
    # Tune versus cross-test: one frozen digest, disjoint cohorts, cross-test after tuning.
    (_tune_cross_test_report, _set(("entries", 1, "system_sha256"), "0" * 64)),
    (_tune_cross_test_report, _set(("entries", 1, "dataset_id"), "SYNTHETIC-TUNE-SET")),
    (_tune_cross_test_report, _set(("entries", 1, "measured_at"), "2026-10-26T17:00:00Z")),
    (_tune_cross_test_report, _set(("entries", 0, "role"), "cross_test")),
    (_tune_cross_test_report, _set(("entries", 0, "true_positive_n"), 11)),
    (_tune_cross_test_report, _set(("entries", 0, "false_positive_n"), 21)),
    (_tune_cross_test_report, _set(("entries", 1, "split"), "test")),
    (_tune_cross_test_report, _set(("entries", 1, "role"), "holdout")),
    (_tune_cross_test_report, _set(("entries",), [])),
    (_tune_cross_test_report, _set(("frozen_sha256",), "0" * 64)),
    # Executed differentials: the audit and per-dataset splits must be arithmetic truths.
    (_seeker_executed_report, _set(("cohort", "audited_n"), 13)),
    (_seeker_executed_report, _set(("cohort", "datasets", 0, "count_n"), 7)),
    (_seeker_executed_report, _set(("cohort", "split"), "test")),
    # The count that VF-5 found published: executed above the arm's evaluated cells.
    (_seeker_executed_report, _set(("arms", 0, "conditions", 1, "executed_differential_n"), 5)),
    (
        _seeker_executed_report,
        _set(("arms", 0, "conditions", 1, "by_dataset", 0, "executed_differential_n"), 4),
    ),
    (_seeker_executed_report, _set(("arms", 1, "conditions", 1, "executed_differential_n"), 1)),
    (
        _seeker_executed_report,
        # A whole-cohort count for an arm that evaluated nothing in this condition.
        lambda report: (
            _set(("arms", 1, "conditions", 1, "cells_n"), 10)(report),
            _set(("arms", 1, "conditions", 1, "attested_n"), 10)(report),
            _set(("arms", 1, "conditions", 1, "executed_differential_n"), 10)(report),
        ),
    ),
    (_seeker_executed_report, _set(("arms", 0, "conditions", 0, "attested_n"), 10)),
    (_seeker_executed_report, _set(("arms", 0, "conditions", 0, "executed_differential_n"), 10)),
    (_seeker_executed_report, _set(("arms", 0, "conditions", 0, "cells_n"), 11)),
    (_seeker_executed_report, _set(("arms", 0, "conditions", 0, "by_dataset", 0, "cells_n"), 7)),
    (_seeker_executed_report, _set(("arms", 0, "conditions", 0, "by_dataset"), [])),
    (_seeker_executed_report, _set(("arms", 1, "conditions", 0, "condition"), "turn_9")),
    (_seeker_executed_report, _set(("arms", 1, "requested_model"), "PROVIDER/model-one")),
    (_seeker_executed_report, _set(("arms", 0, "requested_model"), "no-provider")),
    (_seeker_executed_report, _set(("source_sha256",), "abc")),
    (_seeker_executed_report, _set(("schema",), "evallab.seeker_executed_differentials/v1")),
    # Monitor cascade: every identity cross-key checked and clean, hits inside the cohort.
    (_monitor_cascade_report, _set(("cohorts", 0, "non_dev_assignment_n"), 1)),
    (_monitor_cascade_report, _set(("cohorts", 0, "identities_checked_n"), 30)),
    (_monitor_cascade_report, _set(("cohorts", 0, "true_positive_n"), 11)),
    (_monitor_cascade_report, _set(("cohorts", 1, "stage1_clean_hit_n"), 21)),
    (_monitor_cascade_report, _set(("cohorts", 1, "dataset_id"), "SYNTHETIC-TUNE-SET")),
    (_monitor_cascade_report, _set(("cohorts", 0, "role"), "cross_test")),
    (_monitor_cascade_report, _set(("cohorts", 1, "split"), "test")),
    (_monitor_cascade_report, _set(("judge_models",), [])),
    (_monitor_cascade_report, _set(("frozen_recorded_at",), "2026-10-27T05:00:00")),
    (_monitor_cascade_report, _set(("frozen_sha256",), "abc")),
]


@pytest.mark.parametrize(("build", "mutate"), _TRUST_QC_REJECTIONS)
def test_trust_qc_reports_reject_inconsistent_counts_or_broken_separation(
    client: TestClient,
    runtime: CloudRuntime,
    build: Callable[[], dict[str, Any]],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    report = build()
    mutate(report)
    directory = runtime.data_root / "benchmarks"
    directory.mkdir()
    (directory / "invalid.json").write_text(json.dumps(report))
    result = client.get("/api/cloud-db/benchmarks", headers=AUTH).json()
    assert result["benchmarks"] == []
    assert [entry["file"] for entry in result["rejected"]] == ["invalid.json"]


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
