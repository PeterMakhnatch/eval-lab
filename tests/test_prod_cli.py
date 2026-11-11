from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

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


def test_connection_error_receipt_redacts_encoded_credentials_and_environment_token(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    token = "sensitive-operator-token"
    database = "postgresql://user:password%2Fwith%20spaces@localhost/lab"
    monkeypatch.setenv("EVALLAB_OPERATOR_TOKEN", token)

    def broken_open(*_: Any) -> Any:
        raise OSError(f"{database} password/with spaces {token}")

    monkeypatch.setattr(production_cli, "_open_queue", broken_open)
    assert cli.run_cli([
        "prod", "ingest", "--database", database,
    ], workspace=corpus_root) == 2
    captured = capsys.readouterr()
    receipt = json.loads(captured.err)
    assert receipt["error"]["type"] == "OSError"
    for secret in (token, database, "password/with spaces"):
        assert secret not in captured.out + captured.err


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
    cas = S3CompatibleCAS(local_directory=cas_root)
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
        staging: Path, destination: Path, **kwargs: os.stat_result | None,
    ) -> None:
        original_publish(staging, destination, **kwargs)
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
        staging: Path, destination: Path, **kwargs: os.stat_result | None,
    ) -> None:
        real_publish(staging, destination, **kwargs)
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




@pytest.mark.parametrize("explicit", (False, True))
def test_status_missing_queue_never_creates_a_database_or_parent(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], explicit: bool,
) -> None:
    path = corpus_root / "missing-parent/queue.sqlite" if explicit else _path(corpus_root)
    options = ["--database", str(path)] if explicit else []
    assert not path.parent.exists()
    assert cli.run_cli(["prod", "status", *options], workspace=corpus_root) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "existing queue database" in json.loads(captured.err)["error"]["message"]
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("partial_queue", (False, True))
def test_status_non_queue_sqlite_is_byte_identical_and_has_no_new_tables_or_sidecars(
    corpus_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], partial_queue: bool,
) -> None:
    path = tmp_path / "cloud.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO corpus_tasks VALUES ('private-task', 'immutable evidence')")
        if partial_queue:
            connection.execute("CREATE TABLE queue_state (singleton INTEGER, last_seq INTEGER)")
        connection.commit()
    before = path.read_bytes()
    files_before = {item.name for item in tmp_path.iterdir()}
    assert cli.run_cli(["prod", "status", "--database", str(path)], workspace=corpus_root) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "queue database" in json.loads(captured.err)["error"]["message"]
    assert path.read_bytes() == before
    assert {item.name for item in tmp_path.iterdir()} == files_before
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert tables == ({"corpus_tasks", "queue_state"} if partial_queue else {"corpus_tasks"})


def test_status_real_queue_uses_existing_state_without_changing_any_bytes_or_creating_files(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment", "task_quality")
    path = _path(corpus_root)
    before = {item.name: item.read_bytes() for item in path.parent.iterdir() if item.is_file()}
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    assert receipt["backend"] == "sqlite" and receipt["queue"]["depth"] == 2
    assert receipt["queue"]["jobs"] == {"queued": 2, "leased": 0, "completed": 0}
    assert receipt["queue"]["last_seq"] == 2
    assert {item.name: item.read_bytes() for item in path.parent.iterdir() if item.is_file()} == before


def test_status_reads_an_open_queue_including_its_uncheckpointed_wal_without_writing(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment")
    path = _path(corpus_root)
    with ContinuousWorkQueue(path, registry=CorpusRegistry(corpus_root)) as queue:
        queue.append_event({
            "event_id": "live-status-job", "run_id": "cli-regression", "system": "task_quality",
            "event_type": JOB_TYPES["task_quality"], "status": "confirmed",
            "payload": {"dataset_id": DATASET, "item_id": DEV},
        })
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        assert wal.is_file() and shm.is_file()
        entries_before = set(path.parent.iterdir())
        database_before, wal_before = path.read_bytes(), wal.read_bytes()
        assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        receipt = json.loads(captured.out)
        assert receipt["backend"] == "sqlite" and receipt["queue"]["depth"] == 2
        assert receipt["queue"]["jobs"] == {"queued": 2, "leased": 0, "completed": 0}
        assert receipt["queue"]["last_seq"] == 2
        assert set(path.parent.iterdir()) == entries_before
        assert path.read_bytes() == database_before and wal.read_bytes() == wal_before


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_status_refuses_an_incomplete_wal_snapshot_without_creating_the_missing_sidecar(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], suffix: str,
) -> None:
    _seed(corpus_root, "misalignment")
    path = _path(corpus_root)
    path.with_name(path.name + suffix).write_bytes(b"unpaired sidecar")
    before = {entry.name: entry.read_bytes() for entry in path.parent.iterdir()}
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    message = json.loads(captured.err)["error"]["message"]
    assert "reading queue status" in message and "projection" not in message
    assert {entry.name: entry.read_bytes() for entry in path.parent.iterdir()} == before
