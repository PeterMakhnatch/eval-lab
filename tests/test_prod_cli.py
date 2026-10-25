from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import evallab.production.cli as production_cli
from evallab import cli
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry

DATASET = "tb4-audit-v1"
DEV = "public-dev"
SEALED = "sealed-test-never-export"
JOB_TYPES = {
    "task_quality": "task_quality.audit_task",
    "reward_seeker": "reward_seeker.seek_task",
    "misalignment": "misalignment.replay_task",
}


@pytest.fixture
def corpus_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    for dataset in (DATASET, "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"item_id": DEV, "split": "dev", "subject": {"task_id": "unmaterialized-dev"}},
            {"item_id": SEALED, "split": "test", "subject": {"task_id": "sealed-task"}},
        ] if dataset == DATASET else []
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (keys / DATASET / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [],
        "tasks": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.delenv("EVALLAB_OPERATOR_TOKEN", raising=False)
    return root


def _path(root: Path) -> Path:
    return root / "derived/production/prod-queue.sqlite"


def _seed(root: Path, *systems: str) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuousWorkQueue(path, registry=CorpusRegistry(root)) as queue:
        for system in systems:
            queue.append_event({
                "event_id": "cli-job:" + system,
                "run_id": "cli-regression",
                "system": system,
                "event_type": JOB_TYPES[system],
                "status": "confirmed",
                "payload": {"dataset_id": DATASET, "item_id": DEV},
            })


def _state(root: Path) -> dict[str, Any]:
    with ContinuousWorkQueue(_path(root), registry=CorpusRegistry(root)) as queue:
        return queue.queue_snapshot()


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail("A read-only command reached dispatch or a broker")


def _runtime_seam(monkeypatch: pytest.MonkeyPatch, runtime_type: type) -> None:
    from evallab.production import runtime

    monkeypatch.setattr(runtime, "ProductionRuntime", runtime_type)


def _relay_seam(monkeypatch: pytest.MonkeyPatch, relay_type: type) -> None:
    from evallab.production import relay

    monkeypatch.setattr(relay, "ProductionOutboxRelay", relay_type)


def _server_seam(monkeypatch: pytest.MonkeyPatch, server_type: type) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", server_type)


def test_prod_help_does_not_open_runtime_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(production_cli, "_open_queue", _forbidden)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.run_cli(["prod", "--help"], workspace=corpus_root)
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert all(command in help_text for command in ("ingest", "status", "run-once", "serve"))
    assert "TEST/heldout" in help_text
    assert not _path(corpus_root).exists()


def test_ingest_persists_admission_idempotently_and_reports_sealed_exclusions(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["prod", "ingest", "--dataset", DATASET]
    assert cli.run_cli(argv, workspace=corpus_root) == 0
    first_text = capsys.readouterr().out
    first = json.loads(first_text)
    assert first["ingest"]["totals"]["admitted"] == 1
    assert first["ingest"]["totals"]["excluded"] == 1
    assert first["ingest"]["totals"]["blocked"] == 1
    assert SEALED not in first_text
    before = _state(corpus_root)
    assert before["depth"] == 0
    assert cli.run_cli(argv, workspace=corpus_root) == 0
    second_text = capsys.readouterr().out
    assert SEALED not in second_text
    assert _state(corpus_root) == before
    with ContinuousWorkQueue(_path(corpus_root), registry=CorpusRegistry(corpus_root)) as queue:
        events = queue.read_events(limit=100)
    assert any(event["event_type"] == "queue.corpus_admitted" for event in events)
    assert SEALED not in json.dumps(events)


def test_status_reads_persistent_jobs_without_claiming_or_publishing(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment")
    before = _state(corpus_root)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["backend"] == "sqlite"
    assert receipt["queue"]["depth"] == 1
    assert receipt["queue"]["jobs"] == {"queued": 1, "leased": 0, "completed": 0}
    assert receipt["queue"]["active_leases"] == []
    assert _state(corpus_root) == before


def test_explicit_root_and_relative_database_work_on_either_side_of_subcommand(
    corpus_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    options = ["--repo-root", str(corpus_root), "--database", "queues/explicit.sqlite"]
    assert cli.run_cli(["prod", *options, "ingest", "--dataset", DATASET], workspace=workspace) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["queue"]["last_seq"] > 0
    assert cli.run_cli(["prod", "status", *options], workspace=workspace) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["queue"] == first["queue"]
    assert (corpus_root / "queues/explicit.sqlite").is_file()
    assert not (workspace / "queues/explicit.sqlite").exists()




@pytest.mark.parametrize("driver", ["gptoss", "ollama", "mlx", "local", "arbitrary/model"])
def test_non_subscription_drivers_are_rejected_before_opening_queue(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, driver: str,
) -> None:
    monkeypatch.setattr(production_cli, "_open_queue", _forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.run_cli(["prod", "run-once", "--seeker-driver", driver], workspace=corpus_root)
    assert stopped.value.code == 2


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "inf", "-inf"])
def test_invalid_cloud_spend_cap_is_rejected_before_opening_queue(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, amount: str,
) -> None:
    monkeypatch.setattr(production_cli, "_open_queue", _forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.run_cli(["prod", "run-once", f"--max-usd={amount}"], workspace=corpus_root)
    assert stopped.value.code == 2


def test_worker_failure_is_nonzero_but_relay_still_runs_and_resources_close(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []

    class FailedRuntime:
        def run_once(self) -> dict[str, Any]:
            return {"ok": False, "failures": {"misalignment": {"error": "bad trace"}}}

        def close(self) -> None:
            closed.append("runtime")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            closed.append("relayed")
            return {"delivered": 0}

        async def close(self) -> None:
            closed.append("relay")

    monkeypatch.setattr(production_cli, "_runtime", lambda *_: FailedRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    assert cli.run_cli(["prod", "run-once"], workspace=corpus_root) == 1
    assert json.loads(capsys.readouterr().out)["runtime"]["ok"] is False
    assert closed == ["relayed", "relay", "runtime"]


def test_dashboard_default_serves_real_asgi_without_dispatch_or_broker_connection(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment")
    before = _state(corpus_root)
    observed: dict[str, Any] = {}

    class DashboardServer:
        def __init__(self, config: Any):
            self.config = config
            self.started, self.should_exit = False, False
            observed["host"] = config.host

        async def serve(self) -> None:
            self.started = True
            with TestClient(self.config.app) as client:
                assert client.get("/").status_code == 200
                observed["state"] = client.get("/api/state").json()
                response = client.post("/api/intervene", json={
                    "action": "pause", "run_id": "cli-regression", "reason": "not authorized",
                })
                observed["intervention_status"] = response.status_code

    _server_seam(monkeypatch, DashboardServer)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    monkeypatch.setenv("EVALLAB_SEEKER_LIVE_MODEL", "1")
    assert cli.run_cli(
        ["prod", "serve", "--nats-url", "nats://must-not-connect.invalid:4222"],
        workspace=corpus_root,
    ) == 0
    receipts = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert receipts[0]["workers"] is False
    assert receipts[-1]["event"] == "stopped"
    assert observed["host"] == "127.0.0.1"
    assert observed["state"]["queue"]["depth"] == 1
    assert observed["intervention_status"] == 503
    assert _state(corpus_root) == before


def test_background_worker_crash_stops_http_and_closes_resources_without_secrets(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []
    observed = SimpleNamespace(server=None)
    secret = "operator-secret-never-print"
    password = "database-secret-never-print"

    class CrashRuntime:
        def run_once(self) -> dict[str, Any]:
            raise RuntimeError(f"worker crashed {secret} postgresql://user:{password}@host/db")

        def close(self) -> None:
            closed.append("runtime")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            return {"delivered": 0}

        async def close(self) -> None:
            closed.append("relay")

    class RunningServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False
            observed.server = self

        async def serve(self) -> None:
            self.started = True
            try:
                while not self.should_exit:
                    await asyncio.sleep(0.001)
            finally:
                closed.append("http")

    _server_seam(monkeypatch, RunningServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: CrashRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    monkeypatch.setenv("EVALLAB_OPERATOR_TOKEN", secret)
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error"]["type"] == "RuntimeError"
    assert "worker crashed" in error["error"]["message"]
    assert secret not in captured.out + captured.err
    assert password not in captured.out + captured.err
    assert observed.server.should_exit is True
    assert closed == ["http", "relay", "runtime"]


def test_http_startup_failure_never_dispatches_and_closes_worker_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []

    class NeverRuntime:
        run_once = staticmethod(_forbidden)

        def close(self) -> None:
            closed.append("runtime")

    class NeverRelay:
        run_once = staticmethod(_forbidden)

        async def close(self) -> None:
            closed.append("relay")

    class BindFailureServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        async def serve(self) -> None:
            raise SystemExit(1)

    _server_seam(monkeypatch, BindFailureServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: NeverRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: NeverRelay())
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 2
    assert "failed to start" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert closed == ["relay", "runtime"]


def test_connection_error_receipt_redacts_encoded_credentials_and_environment_token(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    token = "sensitive-operator-token"
    database = "postgresql://user:password%2Fwith%20spaces@localhost/lab"
    broker = "nats://broker-token@localhost:4222"
    monkeypatch.setenv("EVALLAB_OPERATOR_TOKEN", token)

    def broken_open(*_: Any) -> Any:
        raise OSError(f"{database} password/with spaces {broker} broker-token {token}")

    monkeypatch.setattr(production_cli, "_open_queue", broken_open)
    assert cli.run_cli([
        "prod", "run-once", "--database", database, "--nats-url", broker,
    ], workspace=corpus_root) == 2
    captured = capsys.readouterr()
    receipt = json.loads(captured.err)
    assert receipt["error"]["type"] == "OSError"
    for secret in (token, database, broker, "password/with spaces", "broker-token"):
        assert secret not in captured.out + captured.err


def test_graceful_signal_restores_handlers_without_replaying_before_cleanup(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    class SignalServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        def capture_signals(self) -> Any:
            pytest.fail("The CLI must own the graceful signal lifecycle")

        def handle_exit(self, sig: int, frame: Any) -> None:
            observed["signal"] = sig
            self.should_exit = True

        async def serve(self) -> None:
            with self.capture_signals():
                self.started = True
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
                assert self.should_exit

    _server_seam(monkeypatch, SignalServer)
    monkeypatch.setattr(signal, "raise_signal", _forbidden)
    assert cli.run_cli(["prod", "serve"], workspace=corpus_root) == 0
    assert observed["signal"] == signal.SIGTERM
    assert all(signal.getsignal(sig) == handler for sig, handler in originals.items())
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["event"] == "stopped"


def test_shutdown_waits_for_inflight_consumer_before_closing_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class FinishingRuntime:
        def run_once(self) -> dict[str, Any]:
            started.set()
            assert release.wait(timeout=2)
            calls.append("completed")
            return {"ok": True}

        def close(self) -> None:
            assert "completed" in calls
            calls.append("runtime-closed")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            calls.append("relay")
            return {"delivered": 0}

        async def close(self) -> None:
            calls.append("relay-closed")

    class StoppingServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        async def serve(self) -> None:
            self.started = True
            while not started.is_set():
                await asyncio.sleep(0.001)
            self.should_exit = True
            release.set()
            calls.append("http-stopped")

    _server_seam(monkeypatch, StoppingServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: FinishingRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 0
    assert calls == ["http-stopped", "completed", "relay", "relay-closed", "runtime-closed"]


def test_real_async_seeker_cycle_returns_success_and_closes_from_cli(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    entries = corpus_root / "research/hackbook/entries.jsonl"
    entries.parent.mkdir(parents=True)
    entries.write_text("", encoding="utf-8")
    _path(corpus_root).parent.mkdir(parents=True, exist_ok=True)
    before = _state(corpus_root)
    assert cli.run_cli(
        ["prod", "run-once", "--systems", "reward_seeker"], workspace=corpus_root,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["runtime"]["ok"] is True
    assert receipt["runtime"]["workers"]["reward_seeker"]["status"] == "idle"
    assert _state(corpus_root) == before


def _actor_source(root: Path) -> tuple[Path, Path, str, bytes]:
    from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS

    ledger = root / "research/answer-keys/splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "key_id,item_id,task,split\n"
        f"{DATASET},{DEV},unmaterialized-dev,dev\n"
        f"{DATASET},{SEALED},sealed-task,test\n",
        encoding="utf-8",
    )
    parent = root.parent.resolve()
    source, cas_root = parent / "actor-source.sqlite", parent / "source-cas"
    cas = S3CompatibleCAS(local_directory=cas_root, bucket="")
    body = b"An inert, operator-approved evidence object.\n"
    uri = cas.put_bytes(body)
    denied_uri = cas.put_bytes(b"Unapproved object, not for the actor projection.\n")
    with CloudTraceDatabase(source) as database:
        database.upsert_task({
            "task_id": "unmaterialized-dev", "dataset_id": DATASET, "split": "DEV",
            "instruction_text": "Describe this inert record.", "cas_uri": uri,
            "metadata_json": {"item_id": DEV},
        })
        database.upsert_task({
            "task_id": "sealed-task", "dataset_id": DATASET, "split": "DEV",
            "instruction_text": "This deliberately mislabeled synthetic row must stay out.",
            "cas_uri": denied_uri, "metadata_json": {"item_id": SEALED},
        })
        database.upsert_trace({
            "trace_id": "allowed-trace", "run_id": "inert-run",
            "task_id": "unmaterialized-dev", "model_id": "synthetic/no-model",
            "raw_cas_uri": uri,
        }, steps=[{
            "step_index": 0, "role": "assistant",
            "content_text": "An inert, preserved observation.",
            "observation_cas_uri": uri,
        }])
    return source, cas_root, uri, body


def test_project_actor_stages_a_registry_admitted_bundle_without_queue_side_effects(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from evallab.production.cloud_trace_db import CloudTraceDatabase

    source, source_cas, uri, body = _actor_source(corpus_root)
    with CloudTraceDatabase(source) as database:
        database.upsert_task({
            "task_id": "unrelated-invalid-json", "dataset_id": "unknown-fixture",
            "split": "DEV", "metadata_json": {},
            "instruction_text": "Inert unrelated malformed metadata fixture.",
        })
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "UPDATE corpus_tasks SET split = ? WHERE task_id = ?",
            ("\t DEV \r\n", "unmaterialized-dev"),
        )
        connection.execute(
            "UPDATE corpus_tasks SET metadata_json = ? WHERE task_id = ?",
            ("{malformed", "unrelated-invalid-json"),
        )
    allowlist = source.parent / "cas-allowlist.json"
    allowlist.write_text(json.dumps([uri]), encoding="utf-8")
    output = source.parent / "actor-bundle"
    assert cli.run_cli([
        "prod", "project-actor", "--source-database", str(source),
        "--source-cas", str(source_cas), "--cas-allowlist", str(allowlist),
        "--output-directory", str(output), "--max-bytes", str(len(body)),
    ], workspace=corpus_root) == 0
    result = json.loads(capsys.readouterr().out)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["schema"] == "evallab.actor_bundle/v1"
    assert result["projection"]["admitted_task_ids"] == ["unmaterialized-dev"]
    assert manifest["cas"]["object_count"] == 1
    assert manifest["labels_independently_verified"] is False
    assert manifest["projection"]["admitted_identities_sha256"] is not None
    assert manifest["projection"]["database_bytes"] == (output / "cloud-trace-db.sqlite").stat().st_size
    assert (output / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (output / "cloud-trace-db.sqlite").stat().st_mode & 0o777 == 0o600
    assert (output / "cas/sha256" / uri.rsplit("/", 1)[-1]).read_bytes() == body
    assert sorted(path.name for path in output.iterdir()) == [
        "cas", "cloud-trace-db.sqlite", "manifest.json",
    ]
    with CloudTraceDatabase(output / "cloud-trace-db.sqlite") as projected:
        assert projected.get_task("sealed-task") is None
        trace = projected.get_trace("allowed-trace")
        assert trace is not None
        assert trace["steps"][0]["content_text"] == "An inert, preserved observation."
    assert not _path(corpus_root).exists()


def test_read_allowlist_refuses_symlinked_and_hard_linked_documents(tmp_path: Path) -> None:
    target = tmp_path / "approvals.json"
    target.write_text("[]", encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)

    assert production_cli._read_allowlist(target) == b"[]"
    with pytest.raises(OSError):
        production_cli._read_allowlist(alias)

    link = tmp_path / "link.json"
    os.link(target, link)
    for aliased in (link, target):
        with pytest.raises(ValueError, match="hard-linked"):
            production_cli._read_allowlist(aliased)


def test_project_actor_never_removes_an_entry_recreated_at_the_staging_path(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evallab.production import actor_cas

    source, source_cas, uri, body = _actor_source(corpus_root)
    allowlist = source.parent / "cas-allowlist.json"
    allowlist.write_text(json.dumps([uri]), encoding="utf-8")
    output = source.parent / "actor-bundle"
    original_publish = actor_cas.publish_staged_directory
    recreated: list[Path] = []

    def publish_then_recreate(
        staging: Path, destination: Path, **kwargs: object,
    ) -> None:
        original_publish(staging, destination, **kwargs)  # type: ignore[arg-type]
        staging.mkdir()
        (staging / "competitor").write_text("retain", encoding="utf-8")
        recreated.append(staging)

    monkeypatch.setattr(actor_cas, "publish_staged_directory", publish_then_recreate)
    assert cli.run_cli([
        "prod", "project-actor", "--source-database", str(source),
        "--source-cas", str(source_cas), "--cas-allowlist", str(allowlist),
        "--output-directory", str(output), "--max-bytes", str(len(body)),
    ], workspace=corpus_root) == 0
    capsys.readouterr()
    assert (output / "manifest.json").exists()
    assert len(recreated) == 1
    assert (recreated[0] / "competitor").read_text(encoding="utf-8") == "retain"


def test_project_actor_keeps_a_bundle_whose_publication_is_uncertain(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evallab.production import actor_cas

    source, source_cas, uri, body = _actor_source(corpus_root)
    allowlist = source.parent / "cas-allowlist.json"
    allowlist.write_text(json.dumps([uri]), encoding="utf-8")
    output = source.parent / "actor-bundle"
    real_publish = actor_cas.publish_staged_directory

    def publish_then_fail(
        staging: Path, destination: Path, **kwargs: object,
    ) -> None:
        real_publish(staging, destination, **kwargs)  # type: ignore[arg-type]
        raise actor_cas.PublicationUncertainError("simulated post-rename failure")

    monkeypatch.setattr(actor_cas, "publish_staged_directory", publish_then_fail)
    assert cli.run_cli([
        "prod", "project-actor", "--source-database", str(source),
        "--source-cas", str(source_cas), "--cas-allowlist", str(allowlist),
        "--output-directory", str(output), "--max-bytes", str(len(body)),
    ], workspace=corpus_root) == 2
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "PublicationUncertainError"
    assert (output / "manifest.json").exists()
    assert (output / "cloud-trace-db.sqlite").exists()
    assert (output / "cas" / "sha256" / uri.rsplit("/", 1)[-1]).read_bytes() == body


@pytest.mark.parametrize("failure", ["unadmitted-task", "unapproved-cas"])
def test_project_actor_never_publishes_a_partial_or_unadmitted_bundle(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    source, source_cas, uri, body = _actor_source(corpus_root)
    allowlist = source.parent / "cas-allowlist.json"
    allowlist.write_text(json.dumps([] if failure == "unapproved-cas" else [uri]))
    output = source.parent / "actor-bundle"
    argv = [
        "prod", "project-actor", "--source-database", str(source),
        "--source-cas", str(source_cas), "--cas-allowlist", str(allowlist),
        "--output-directory", str(output), "--max-bytes", str(len(body)),
    ]
    if failure == "unadmitted-task":
        argv.extend(["--task-id", "sealed-task"])
    assert cli.run_cli(argv, workspace=corpus_root) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["error"]["type"] == "ValueError"
    assert not output.exists()
    assert list(source.parent.glob(".actor-bundle-*")) == []
    assert not _path(corpus_root).exists()


def test_project_actor_refuses_output_under_the_source_cas_before_writing(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, source_cas, uri, body = _actor_source(corpus_root)
    allowlist = source.parent / "cas-allowlist.json"
    allowlist.write_text(json.dumps([uri]), encoding="utf-8")
    output = source_cas / "sha256" / "actor-bundle"
    before = {
        path.relative_to(corpus_root): (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in corpus_root.rglob("*")
    }
    assert cli.run_cli([
        "prod", "project-actor", "--source-database", str(source),
        "--source-cas", str(source_cas), "--cas-allowlist", str(allowlist),
        "--output-directory", str(output), "--max-bytes", str(len(body)),
    ], workspace=corpus_root) == 2
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "ValueError"
    assert not output.exists()
    assert {
        path.relative_to(corpus_root): (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in corpus_root.rglob("*")
    } == before
