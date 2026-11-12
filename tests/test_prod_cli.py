from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import evallab.production.cli as production_cli
from evallab import cli

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












