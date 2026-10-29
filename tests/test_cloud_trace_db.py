"""Synthetic storage fixtures only: no shared database, cloud I/O, or Docker.

The PostgreSQL variant uses an isolated native server when its binaries exist.
All S3 calls are injected; environment credentials are cleared for every test.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import boto3
import psycopg
import pytest
from psycopg import sql

from evallab.production import cloud_trace_db
from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    S3CompatibleCAS,
)
from evallab.production.corpus_queue import CorpusRegistry

_ENVIRONMENT = (
    "DATABASE_URL", "EVALLAB_CLOUD_DATABASE_URL", "EVALLAB_S3_BUCKET",
    "EVALLAB_S3_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    run = subprocess.run

    def without_docker(command: Any, *args: Any, **kwargs: Any) -> Any:
        executable = command if isinstance(command, str) else command[0]
        assert Path(executable).name not in {"docker", "podman"}, "containers are forbidden"
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", without_docker)


@pytest.fixture(scope="module")
def postgres_server() -> Iterator[str]:
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        candidates = list(Path("/usr/lib/postgresql").glob("*/bin"))
        candidates += list(Path("/opt/homebrew/opt").glob("postgresql@*/bin"))
        candidates.sort(
            key=lambda path: tuple(int(part) for part in path.parent.name.removeprefix("postgresql@").split(".")
                                   if part.isdigit()),
            reverse=True,
        )
        for directory in candidates:
            if (directory / "initdb").is_file() and (directory / "pg_ctl").is_file():
                initdb, pg_ctl = str(directory / "initdb"), str(directory / "pg_ctl")
                break
    if initdb is None or pg_ctl is None:
        pytest.skip("native PostgreSQL server binaries are not installed")
    with tempfile.TemporaryDirectory(prefix="cloud-trace-pg-") as directory:
        root = Path(directory).resolve()
        data = root / "data"
        subprocess.run(
            [initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "--no-locale", "--encoding=UTF8"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [pg_ctl, "-D", str(data), "-l", str(root / "server.log"), "-o", f"-F -h '' -k {root} -p 5432", "-w", "start"],
            check=True, capture_output=True, text=True,
        )
        try:
            yield f"postgresql://postgres@/postgres?host={quote(str(root), safe='')}"
        finally:
            subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "fast", "-w", "stop"],
                check=True, capture_output=True, text=True,
            )


@pytest.fixture(params=("sqlite-fts", "sqlite-like", "postgres"))
def target(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str | Path, bool]]:
    if request.param != "postgres":
        yield tmp_path / "traces.sqlite", request.param == "sqlite-fts"
        return
    server = request.getfixturevalue("postgres_server")
    schema = "cloud_trace_" + uuid4().hex
    with psycopg.connect(server, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield server + "&options=" + quote(f"-csearch_path={schema}", safe=""), True
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(target: tuple[str | Path, bool]) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(target[0], sqlite_fts=target[1]) as database:
        yield database


def task(task_id: str = "synthetic-task", **extra: Any) -> dict[str, Any]:
    return {
        "task_id": task_id, "dataset_id": "synthetic-fixture", "split": "dev",
        "instruction_text": "Compute a bounded checksum and preserve Unicode café.", **extra,
    }


def issue(issue_id: str = "synthetic-issue", **extra: Any) -> dict[str, Any]:
    return {
        "issue_id": issue_id, "task_id": "synthetic-task", "defect_class": "insufficient_checking",
        "direction": "wrong_pass", "status": "confirmed", "original_reward": 1.0,
        "repaired_reward": 0.0, "citation_file": "tests/check.py", "citation_line": 23,
        "witness_digest": "sha256:" + "a" * 64,
        "reproducer_script": "printf 'synthetic incorrect result' > result.txt\n", **extra,
    }


def trace(trace_id: str = "trace-1", **extra: Any) -> dict[str, Any]:
    return {
        "trace_id": trace_id, "run_id": "run-1", "task_id": "synthetic-task",
        "model_id": "synthetic-agent", "outcome": "wrong_pass", "reward": 1.0,
        "is_exploit": True, "verified_receipt_json": {"status": "verified", "verifier": "synthetic"},
        **extra,
    }


def steps(word: str = "observation") -> list[dict[str, Any]]:
    return [
        {"step_index": 4, "role": "tool", "tool_name": "terminal", "observation_preview": word},
        {"step_index": 1, "role": "assistant", "tool_name": "terminal", "command_or_action": "cat result.txt", "content_text": "inspect checksum", "flags_json": ["fixture"]},
        {"step_index": 0, "role": "user", "content_text": "Please compute the checksum."},
    ]


def seed(db: CloudTraceDatabase) -> None:
    db.upsert_task(task())
    db.upsert_task_issue(issue())
    db.upsert_trace(trace(), steps=steps())


def test_complete_task_issue_trace_and_raw_cas_roundtrip(db: CloudTraceDatabase, tmp_path: Path) -> None:
    cas = S3CompatibleCAS(local_directory=tmp_path / "cas")
    raw = {"messages": [{"role": "tool", "content": "完整观察\n" * 20_000}], "tool_calls": [{"command": "cat result.txt"}]}
    raw_uri = cas.put_bundle(raw)
    package_uri = cas.put_bytes(b"synthetic tar package\x00\xff")
    receipt = {"status": "confirmed", "source_digest": hashlib.sha256(cas.get_bytes(raw_uri)).hexdigest()}
    db.upsert_task(task(
        original_grader_digest="sha256:" + "b" * 64, repaired_grader_digest="sha256:" + "c" * 64,
        cas_uri=package_uri, metadata_json={"lineage": ["original", "repaired"], "source": "fixture"},
    ))
    db.upsert_task_issue(issue(cas_uri=cas.put_bundle({"reproducer": "synthetic witness"})))
    full_steps = steps()
    full_steps[0].update(content_text=raw["messages"][0]["content"], observation_cas_uri=raw_uri)
    db.upsert_trace(trace(raw_cas_uri=raw_uri, verified_receipt_json=receipt), steps=full_steps)
    found = db.search_traces("checksum", defect_class="insufficient_checking", tool_name="terminal", only_verified=True)
    assert len(found) == 1
    result = found[0]
    assert result["task"]["cas_uri"] == package_uri
    assert result["task"]["metadata_json"]["lineage"] == ["original", "repaired"]
    assert result["issues"][0]["citation_line"] == 23
    assert result["issues"][0]["reproducer_script"] == issue()["reproducer_script"]
    assert result["verified_receipt_json"] == receipt
    assert result["is_exploit"] is True and result["is_task_defect_fail"] is False
    assert result["step_count"] == 3
    assert [step["step_index"] for step in result["steps"]] == [0, 1, 4]
    assert result["steps"][2]["content_text"] == raw["messages"][0]["content"]
    assert result["steps"][1]["flags_json"] == ["fixture"]
    assert cas.get_bundle(result["raw_cas_uri"]) == raw
    assert cas.get_bytes(result["task"]["cas_uri"]) == b"synthetic tar package\x00\xff"
    assert "search_vector" not in result and "search_vector" not in result["task"]


def test_issue_queries_join_tasks_and_preserve_statuses(db: CloudTraceDatabase) -> None:
    db.upsert_task(task())
    db.upsert_task(task("other-task", split="test"))
    db.upsert_task_issue(issue())
    db.upsert_task_issue(issue("unfair", direction="unfair_fail", original_reward=0, repaired_reward=1))
    db.upsert_task_issue(issue("tentative", status="provisional"))
    db.upsert_task_issue(issue("disproved", status="refuted"))
    db.upsert_task_issue(issue("other", task_id="other-task"))
    assert [row["issue_id"] for row in db.query_task_issues("synthetic-task")] == ["synthetic-issue", "unfair"]
    assert [row["issue_id"] for row in db.query_task_issues(direction="unfair_fail")] == ["unfair"]
    assert len(db.query_task_issues(confirmed_only=False)) == 5
    other = db.query_task_issues("other-task")[0]
    assert other["task"]["split"] == "test"  # A storage read is not training admission.
    assert db.query_task_issues("missing") == []
    with pytest.raises(ValueError, match="direction"):
        db.query_task_issues(direction="not-a-direction")


@pytest.mark.parametrize("query", ["bounded", "synthetic incorrect", "inspect checksum", "observation", "synthetic-agent"])
def test_search_matches_task_issue_action_observation_and_model(db: CloudTraceDatabase, query: str) -> None:
    seed(db)
    assert [row["trace_id"] for row in db.search_traces(query)] == ["trace-1"]


def test_search_filters_are_exact_and_do_not_duplicate_traces(db: CloudTraceDatabase) -> None:
    seed(db)
    db.upsert_task_issue(issue("second-issue"))
    db.upsert_trace(trace("trace-2", verified_receipt_json=None), steps=[{"step_index": 0, "role": "tool", "tool_name": "browser"}])
    assert len(db.search_traces("bounded")) == 2
    assert len(db.search_traces("bounded", only_verified=True)) == 1
    assert len(db.search_traces("", defect_class="insufficient_checking", tool_name="terminal")) == 1
    assert db.search_traces("", tool_name="term") == []
    assert db.search_traces("", defect_class="unrelated") == []
    assert db.search_traces("checksum missingword") == []
    assert db.search_traces("% ' ; --") == []
    assert db.search_traces("checksum", tool_name="terminal' OR 1=1 --") == []
    assert db.search_traces("checksum", defect_class="' OR 1=1 --") == []
    assert db.query_task_issues("' OR 1=1 --") == []
    assert db.get_task("synthetic-task") is not None


def test_only_verified_requires_explicit_receipt_status(db: CloudTraceDatabase) -> None:
    db.upsert_task(task())
    for index, receipt in enumerate((None, {}, {"note": "claimed verified"}, {"status": "provisional"}, {"status": "refuted"}, {"status": "verified"}, {"status": "confirmed"})):
        db.upsert_trace(trace(f"trace-{index}", verified_receipt_json=receipt))
    assert [row["trace_id"] for row in db.search_traces("", only_verified=True)] == ["trace-5", "trace-6"]
    assert len(db.search_traces("")) == 7


def test_upserts_refresh_full_text_and_replace_steps_atomically(db: CloudTraceDatabase) -> None:
    seed(db)
    db.upsert_task(task(instruction_text="replacement prompt"))
    assert db.search_traces("bounded") == []
    assert len(db.search_traces("replacement prompt")) == 1
    db.upsert_task_issue(issue(reproducer_script="newreproducer"))
    assert db.search_traces("synthetic incorrect") == []
    assert len(db.search_traces("newreproducer")) == 1
    db.upsert_trace(trace(model_id="replacement-model"), steps=[{"step_index": 0, "role": "tool", "content_text": "newstep"}])
    assert db.search_traces("observation") == []
    assert len(db.search_traces("newstep")) == 1
    result = db.get_trace("trace-1")
    assert result is not None and result["step_count"] == 1
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        db.upsert_trace(trace(task_id="missing-task"), steps=[])
    result = db.get_trace("trace-1")
    assert result is not None
    assert result["model_id"] == "replacement-model"
    assert result["steps"][0]["content_text"] == "newstep"
    db.replace_trace_steps("trace-1", [])
    result = db.get_trace("trace-1")
    assert result is not None and result["step_count"] == 0
    assert db.search_traces("newstep") == []
    with pytest.raises(KeyError):
        db.replace_trace_steps("missing", [])


def test_metadata_update_preserves_steps_and_count(db: CloudTraceDatabase) -> None:
    seed(db)
    db.upsert_trace(trace(outcome="reviewed"))
    result = db.get_trace("trace-1")
    assert result is not None
    assert result["step_count"] == 3 and len(result["steps"]) == 3
    assert result["outcome"] == "reviewed"
    assert db.get_trace("missing") is None
    assert db.get_task("missing") is None


def test_foreign_keys_reject_detached_evidence(db: CloudTraceDatabase) -> None:
    for write in (lambda: db.upsert_task_issue(issue()), lambda: db.upsert_trace(trace(), steps=steps())):
        with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
            write()
    assert db.query_task_issues(confirmed_only=False) == []
    assert db.search_traces("") == []


@pytest.mark.parametrize("patch", [
    {"direction": "sideways"}, {"status": "maybe"}, {"citation_line": 0},
    {"original_reward": float("nan")}, {"repaired_reward": float("inf")},
    {"original_reward": True}, {"defect_class": ""}, {"unknown": "value"},
])
def test_invalid_issue_records_are_rejected(db: CloudTraceDatabase, patch: dict[str, Any]) -> None:
    db.upsert_task(task())
    with pytest.raises(ValueError):
        db.upsert_task_issue(issue(**patch))
    assert db.query_task_issues(confirmed_only=False) == []


@pytest.mark.parametrize("patch", [
    {"reward": float("nan")}, {"is_exploit": "false"}, {"step_count": -1},
    {"verified_receipt_json": []}, {"verified_receipt_json": "not-json"},
    {"run_id": ""}, {"unexpected": True},
])
def test_invalid_trace_records_are_rejected(db: CloudTraceDatabase, patch: dict[str, Any]) -> None:
    db.upsert_task(task())
    with pytest.raises(ValueError):
        db.upsert_trace(trace(**patch))
    assert db.search_traces("") == []


@pytest.mark.parametrize("bad_steps", [
    [{"step_index": 0, "role": "user"}, {"step_index": 0, "role": "tool"}],
    [{"step_index": -1, "role": "user"}],
    [{"step_index": True, "role": "user"}],
    [{"step_index": 0, "role": ""}],
    [{"trace_id": "another", "step_index": 0, "role": "user"}],
    [{"step_index": 0, "role": "tool", "flags_json": {"value": float("nan")}}],
])
def test_invalid_step_replacement_leaves_original_trace(db: CloudTraceDatabase, bad_steps: list[dict]) -> None:
    seed(db)
    with pytest.raises(ValueError):
        db.upsert_trace(trace(model_id="must-not-persist"), steps=bad_steps)
    result = db.get_trace("trace-1")
    assert result is not None
    assert result["model_id"] == "synthetic-agent"
    assert len(result["steps"]) == 3
    with pytest.raises(ValueError, match="step_count"):
        db.upsert_trace(trace(step_count=99), steps=[])


def test_json_text_input_and_defensive_read_results(db: CloudTraceDatabase) -> None:
    db.upsert_task(task(metadata_json='{"source":{"name":"fixture"}}'))
    db.upsert_trace(trace(verified_receipt_json='{"status":"verified"}'))
    first = db.get_trace("trace-1")
    assert first is not None
    first["task"]["metadata_json"]["source"]["name"] = "mutated"
    stored_task = db.get_task("synthetic-task")
    assert stored_task is not None
    assert stored_task["metadata_json"] == {"source": {"name": "fixture"}}
    assert len(db.search_traces("", only_verified=True)) == 1
    with pytest.raises(ValueError):
        db.upsert_task(task(metadata_json={"bad": float("nan")}))


def test_reopening_persists_records_and_search_indexes(db: CloudTraceDatabase, target: tuple[str | Path, bool]) -> None:
    seed(db)
    with CloudTraceDatabase(target[0], sqlite_fts=target[1]) as reopened:
        result = reopened.get_trace("trace-1")
        assert result is not None and result["step_count"] == 3
        assert len(reopened.search_traces("observation")) == 1
        reopened.upsert_task(task(instruction_text="persistent replacement"))
    assert len(db.search_traces("persistent")) == 1
    assert db.search_traces("bounded") == []


def test_concurrent_connections_preserve_complete_traces(db: CloudTraceDatabase, target: tuple[str | Path, bool]) -> None:
    db.upsert_task(task())

    def write(index: int) -> None:
        with CloudTraceDatabase(target[0], sqlite_fts=target[1]) as writer:
            writer.upsert_trace(trace(f"concurrent-{index}"), steps=steps())

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(write, range(12)))
    results = db.search_traces("observation")
    assert len(results) == 12
    assert all(row["step_count"] == len(row["steps"]) == 3 for row in results)


def test_same_trace_concurrent_replacements_never_mix_step_sets(
    db: CloudTraceDatabase, target: tuple[str | Path, bool],
) -> None:
    seed(db)

    def replace(index: int) -> None:
        with CloudTraceDatabase(target[0], sqlite_fts=target[1]) as writer:
            if index % 3 == 0:
                writer.upsert_trace(trace(outcome="metadata-only"))
                return
            writer.replace_trace_steps("trace-1", [
                {"step_index": index * 20 + step, "role": "tool", "content_text": f"writer-{index}"}
                for step in range(index + 1)
            ])

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(replace, range(12)))
    result = db.get_trace("trace-1")
    assert result is not None
    assert result["step_count"] == len(result["steps"])
    assert len({step["content_text"] for step in result["steps"]}) == 1


def test_native_full_text_indexes_are_used_when_available(db: CloudTraceDatabase) -> None:
    seed(db)
    if db._postgres:
        indexes = db._execute("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()").fetchall()
        assert sum("USING gin (search_vector)" in row[0] for row in indexes) == 4
        vector_type = db._execute("SELECT pg_typeof(search_vector)::text FROM corpus_tasks").fetchone()
        assert vector_type[0] == "tsvector"
        assert len(db.search_traces("computing")) == 1
    elif db._fts:
        assert db._execute("SELECT count(*) FROM corpus_tasks_fts WHERE corpus_tasks_fts MATCH ?", ('"checksum"',)).fetchone()[0] == 1
    else:
        assert len(db.search_traces("checksum")) == 1


@pytest.fixture(scope="module")
def oversized_document() -> str:
    # Distinct lexemes exceed PostgreSQL's 1 MiB tsvector limit, not just a
    # repeated word whose native vector is small despite a large source TEXT.
    return "headmarker " + " ".join(f"entry{index:06d}" for index in range(70_000)) + " tailmarker running crème"


@pytest.mark.parametrize("field", ("instruction_text", "reproducer_script", "model_id", "content_text"))
def test_large_documents_preserve_and_search_both_ends(
    db: CloudTraceDatabase, oversized_document: str, field: str,
) -> None:
    seed(db)
    if field == "instruction_text":
        db.upsert_task(task(instruction_text=oversized_document))
    elif field == "reproducer_script":
        db.upsert_task_issue(issue(reproducer_script=oversized_document))
    elif field == "model_id":
        db.upsert_trace(trace(model_id=oversized_document))
    else:
        db.replace_trace_steps("trace-1", [
            {"step_index": 0, "role": "tool", "content_text": oversized_document},
        ])
    result = db.search_traces("headmarker tailmarker")
    assert len(result) == 1
    assert len(db.search_traces("entry069999")) == 1
    assert db.search_traces("headmarker absentneedle") == []
    record = (
        result[0]["task"] if field == "instruction_text"
        else result[0]["issues"][0] if field == "reproducer_script"
        else result[0]["steps"][0] if field == "content_text"
        else result[0]
    )
    assert record[field] == oversized_document


def test_postgres_migrates_unbounded_vectors_without_losing_text(
    postgres_server: str, oversized_document: str,
) -> None:
    schema = "cloud_trace_legacy_" + uuid4().hex
    with psycopg.connect(postgres_server, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        target = postgres_server + "&options=" + quote(f"-csearch_path={schema}", safe="")
        try:
            with CloudTraceDatabase(target) as db:
                seed(db)
            for table, columns in cloud_trace_db._SEARCH_COLUMNS.items():
                identifier = sql.Identifier(schema, table)
                fields = sql.SQL(" || ' ' || ").join(
                    sql.SQL("coalesce({}, '')").format(sql.Identifier(column)) for column in columns
                )
                connection.execute(sql.SQL("ALTER TABLE {} DROP COLUMN search_vector").format(identifier))
                connection.execute(sql.SQL(
                    "ALTER TABLE {} ADD COLUMN search_vector TSVECTOR "
                    "GENERATED ALWAYS AS (to_tsvector('english', {})) STORED"
                ).format(identifier, fields))
                connection.execute(sql.SQL("CREATE INDEX {} ON {} USING GIN(search_vector)").format(
                    sql.Identifier(table + "_search"), identifier,
                ))
            with CloudTraceDatabase(target) as db:
                assert db.get_trace("trace-1") is not None
                db.replace_trace_steps("trace-1", steps(oversized_document))
                result = db.search_traces("headmarker tailmarker")
                assert len(result) == 1
                assert result[0]["steps"][-1]["observation_preview"] == oversized_document
                assert len(db.search_traces("runs crème")) == 1
                assert db.search_traces("the and") == []
                assert db._execute(
                    "SELECT search_vector IS NULL FROM trace_steps WHERE step_index = 4"
                ).fetchone()[0]
                indexes = db._execute(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                ).fetchall()
                assert sum("USING gin (search_vector)" in row[0] for row in indexes) == 4
                db.replace_trace_steps("trace-1", steps("tiny marker"))
                assert db.search_traces("tailmarker") == []
                assert len(db.search_traces("tiny marker")) == 1
                assert not db._execute(
                    "SELECT search_vector IS NULL FROM trace_steps WHERE step_index = 4"
                ).fetchone()[0]
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_sqlite_automatically_falls_back_when_fts5_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connect = sqlite3.connect

    class WithoutFTS(sqlite3.Connection):
        def execute(self, statement: str, *args: Any, **kwargs: Any) -> Any:
            if statement.startswith("CREATE VIRTUAL TABLE"):
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(cloud_trace_db.sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=WithoutFTS))
    with CloudTraceDatabase(tmp_path / "fallback.sqlite") as db:
        seed(db)
        assert not db._fts
        assert len(db.search_traces("checksum")) == 1
        db.upsert_task(task(instruction_text="under_score"))
        db.upsert_task(task("other", instruction_text="underXscore"))
        db.upsert_trace(trace("other", task_id="other"))
        assert [row["trace_id"] for row in db.search_traces("under_score")] == ["trace-1"]


def test_missing_database_configuration_and_environment_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        CloudTraceDatabase()
    default, preferred, explicit = [tmp_path / f"{name}.sqlite" for name in ("default", "preferred", "explicit")]
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{default}")
    with CloudTraceDatabase() as db:
        db.upsert_task(task())
    monkeypatch.setenv("EVALLAB_CLOUD_DATABASE_URL", f"sqlite:///{preferred}")
    with CloudTraceDatabase() as db:
        assert db.get_task("synthetic-task") is None
        db.upsert_task(task("preferred"))
    with CloudTraceDatabase(explicit) as db:
        assert db.get_task("preferred") is None
    with pytest.raises(ValueError, match="database"):
        CloudTraceDatabase("mysql://unsupported/db")


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[dict] = []
        self.gets: list[dict] = []
        self.last_body: io.BytesIO | None = None

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        self.objects[kwargs["Bucket"], kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict:
        self.gets.append(kwargs)
        self.last_body = io.BytesIO(self.objects[kwargs["Bucket"], kwargs["Key"]])
        return {"Body": self.last_body}


def test_local_cas_deterministic_bundles_and_binary_bytes(tmp_path: Path) -> None:
    cas = S3CompatibleCAS(local_directory=tmp_path)
    uri = cas.put_bundle({"z": [1, "café"], "a": True})
    assert cas.put_bundle({"a": True, "z": [1, "café"]}) == uri
    assert cas.get_bundle(uri) == {"a": True, "z": [1, "café"]}
    for data in (b"", b"\x00\xff\x01\n", b"x" * 150_000):
        uri = cas.put_bytes(data)
        assert uri == "cas://sha256/" + hashlib.sha256(data).hexdigest()
        assert cas.get_bytes(uri) == data
        assert cas.put_bytes(data) == uri
    assert not list((tmp_path / "sha256").glob("tmp*"))


def test_s3_upload_download_and_local_read_through_mirror(tmp_path: Path) -> None:
    client = MemoryS3()
    cas = S3CompatibleCAS(bucket="fixture-bucket", client=client)
    uri = cas.put_bytes(b"full raw trace")
    digest = uri.rsplit("/", 1)[1]
    assert client.puts[0]["Key"] == f"sha256/{digest}"
    assert client.puts[0]["Metadata"] == {"sha256": digest}
    reader = S3CompatibleCAS(bucket="fixture-bucket", client=client, local_directory=tmp_path)
    assert reader.get_bytes(uri) == b"full raw trace"
    assert client.last_body is not None and client.last_body.closed
    assert reader.get_bytes(uri) == b"full raw trace"
    assert len(client.gets) == 1
    assert (tmp_path / "sha256" / digest).read_bytes() == b"full raw trace"
    mirrored = reader.put_bundle({"task": "fixture"})
    assert reader.get_bundle(mirrored) == {"task": "fixture"}
    assert len(client.puts) == 2


def test_corrupted_remote_and_local_objects_are_never_accepted(tmp_path: Path) -> None:
    client = MemoryS3()
    cas = S3CompatibleCAS(bucket="fixture", client=client, local_directory=tmp_path)
    uri = cas.put_bytes(b"original")
    path = tmp_path / "sha256" / uri.rsplit("/", 1)[1]
    path.write_bytes(b"corrupt local")
    with pytest.raises(ValueError, match="integrity"):
        cas.get_bytes(uri)
    with pytest.raises(ValueError, match="integrity"):
        cas.put_bytes(b"original")
    path.unlink()
    client.objects["fixture", client.puts[0]["Key"]] = b"corrupt remote"
    with pytest.raises(ValueError, match="integrity"):
        cas.get_bytes(uri)
    assert client.last_body is not None and client.last_body.closed
    assert not path.exists()


@pytest.mark.parametrize("uri", [
    "s3://bucket/key", "cas://sha256/../escape", "cas://sha256/" + "A" * 64,
    "cas://sha256/" + "a" * 63, "cas://sha256/" + "a" * 64 + "?query",
    "cas://md5/" + "a" * 64,
])
def test_cas_rejects_noncanonical_uris(tmp_path: Path, uri: str) -> None:
    with pytest.raises(ValueError, match="cas://sha256"):
        S3CompatibleCAS(local_directory=tmp_path).get_bytes(uri)


def test_cas_missing_objects_and_remote_errors_propagate(tmp_path: Path) -> None:
    cas = S3CompatibleCAS(local_directory=tmp_path)
    with pytest.raises(FileNotFoundError):
        cas.get_bytes("cas://sha256/" + "0" * 64)

    class BrokenS3(MemoryS3):
        def put_object(self, **kwargs: Any) -> None:
            raise OSError("synthetic transport failure")

    remote = S3CompatibleCAS(bucket="fixture", client=BrokenS3(), local_directory=tmp_path)
    with pytest.raises(OSError, match="transport"):
        remote.put_bytes(b"not uploaded")
    assert not (tmp_path / "sha256").exists()
    with pytest.raises(ValueError):
        cas.put_bundle({"not_json": float("nan")})


def test_s3_environment_configuration_uses_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "EVALLAB_S3_BUCKET": "fixture", "EVALLAB_S3_ENDPOINT_URL": "https://fixture.r2.example",
        "AWS_ACCESS_KEY_ID": "fixture-access", "AWS_SECRET_ACCESS_KEY": "fixture-secret",
        "AWS_SESSION_TOKEN": "fixture-session", "AWS_REGION": "fixture-region",
    }.items():
        monkeypatch.setenv(key, value)
    captured: dict[str, Any] = {}
    client = MemoryS3()

    def create(service: str, **kwargs: Any) -> MemoryS3:
        assert service == "s3"
        captured.update(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", create)
    cas = S3CompatibleCAS()
    assert cas.get_bytes(cas.put_bytes(b"roundtrip")) == b"roundtrip"
    assert captured["endpoint_url"] == "https://fixture.r2.example"
    assert captured["aws_access_key_id"] == "fixture-access"
    assert captured["aws_secret_access_key"] == "fixture-secret"
    assert captured["aws_session_token"] == "fixture-session"
    assert captured["region_name"] == "fixture-region"
    assert captured["config"].signature_version == "s3v4"
    assert captured["config"].s3["addressing_style"] == "path"


def test_cas_configuration_never_probes_ambient_credential_chains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected SDK initialization")

    monkeypatch.setattr(boto3, "client", forbidden)
    with pytest.raises(ValueError, match="configure"):
        S3CompatibleCAS()
    with pytest.raises(ValueError, match="bucket"):
        S3CompatibleCAS(client=MemoryS3())
    with pytest.raises(ValueError, match="AWS_ACCESS_KEY_ID"):
        S3CompatibleCAS(bucket="fixture")
    local = S3CompatibleCAS(local_directory=tmp_path)
    assert local.get_bytes(local.put_bytes(b"offline")) == b"offline"


@pytest.mark.parametrize("fts", (True, False), ids=("sqlite-fts", "sqlite-like"))
def test_task_scope_filters_before_sqlite_rows_materialize(tmp_path: Path, fts: bool) -> None:
    with CloudTraceDatabase(tmp_path / "scoped.sqlite", sqlite_fts=fts) as database:
        seed(database)
        database.upsert_task_issue(issue("provisional-issue", status="provisional"))
        database.upsert_task(task("denied-task", split="test"))
        database.upsert_task_issue(issue("denied-issue", task_id="denied-task"))
        database.upsert_trace(trace("denied-trace", task_id="denied-task"), steps=steps())
        allowed = frozenset({"synthetic-task"})
        materialized = []

        def guarded_row(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> tuple[Any, ...]:
            fields = {column[0]: position for position, column in enumerate(cursor.description)}
            if "task_id" in fields:
                assert row[fields["task_id"]] in allowed, "denied body materialized in SQLite"
                materialized.append(row[fields["task_id"]])
            if "trace_id" in fields:
                assert row[fields["trace_id"]] == "trace-1", "denied step/trace materialized"
            return row

        database._connection.execute("PRAGMA query_only = ON")
        database._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        database._connection.row_factory = guarded_row
        try:
            traces = database.search_traces(
                "checksum", defect_class="insufficient_checking", tool_name="terminal",
                only_verified=True, allowed_task_ids=allowed,
            )
            assert [row["trace_id"] for row in traces] == ["trace-1"]
            assert traces[0]["task"]["task_id"] == "synthetic-task"
            assert {row["issue_id"] for row in traces[0]["issues"]} == {
                "synthetic-issue", "provisional-issue",
            }
            assert [row["step_index"] for row in traces[0]["steps"]] == [0, 1, 4]
            assert [row["issue_id"] for row in database.query_task_issues(
                allowed_task_ids=allowed,
            )] == ["synthetic-issue"]
            assert [row["issue_id"] for row in database.query_task_issues(
                confirmed_only=False, allowed_task_ids=allowed,
            )] == ["provisional-issue", "synthetic-issue"]
            assert database.query_task_issues("denied-task", allowed_task_ids=allowed) == []
            assert database.search_traces("", allowed_task_ids={" SYNTHETIC-TASK "}) == []
            assert database.query_task_issues(allowed_task_ids={" SYNTHETIC-TASK "}) == []
            large_scope = allowed | {f"missing-{index}" for index in range(1500)}
            assert [row["trace_id"] for row in database.search_traces(
                "checksum", allowed_task_ids=large_scope,
            )] == ["trace-1"]
            assert [row["issue_id"] for row in database.query_task_issues(
                allowed_task_ids=large_scope,
            )] == ["synthetic-issue"]
            assert materialized
        finally:
            database._connection.row_factory = None
        assert {row["trace_id"] for row in database.search_traces("")} == {
            "trace-1", "denied-trace",
        }
        assert {row["issue_id"] for row in database.query_task_issues()} == {
            "synthetic-issue", "denied-issue",
        }


@pytest.mark.parametrize("fts", (True, False), ids=("sqlite-fts", "sqlite-like"))
def test_empty_task_scope_never_reads_sqlite_bodies(tmp_path: Path, fts: bool) -> None:
    with CloudTraceDatabase(tmp_path / "empty-scope.sqlite", sqlite_fts=fts) as database:
        seed(database)
        reads = []

        def no_reads(
            action: int, table: str | None, column: str | None,
            source: str | None, trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database._connection.set_authorizer(no_reads)
        try:
            assert database.search_traces("checksum", allowed_task_ids=frozenset()) == []
            assert database.search_traces("", allowed_task_ids=[]) == []
            assert database.query_task_issues(allowed_task_ids=frozenset()) == []
            assert database.query_task_issues(
                "synthetic-task", confirmed_only=False, allowed_task_ids=[],
            ) == []
            assert reads == []
        finally:
            database._connection.set_authorizer(None)


def test_task_scope_preserves_query_status_and_filter_semantics(db: CloudTraceDatabase) -> None:
    seed(db)
    db.upsert_task_issue(issue("provisional", status="provisional"))
    db.upsert_task_issue(issue("unfair", direction="unfair_fail"))
    db.upsert_trace(trace("unverified", verified_receipt_json=None), steps=steps())
    db.upsert_task(task("other-task"))
    db.upsert_task_issue(issue("other-issue", task_id="other-task", direction="unfair_fail"))
    db.upsert_trace(trace("other-trace", task_id="other-task"), steps=steps())
    scope = frozenset({"synthetic-task"})
    assert [row["trace_id"] for row in db.search_traces(
        "checksum", only_verified=True, allowed_task_ids=scope,
    )] == ["trace-1"]
    assert [row["trace_id"] for row in db.search_traces(
        "", defect_class="insufficient_checking", tool_name="terminal", allowed_task_ids=scope,
    )] == ["trace-1", "unverified"]
    assert db.search_traces("", defect_class="absent", allowed_task_ids=scope) == []
    assert db.search_traces("", tool_name="absent", allowed_task_ids=scope) == []
    assert [row["issue_id"] for row in db.query_task_issues(
        direction="wrong_pass", allowed_task_ids=scope,
    )] == ["synthetic-issue"]
    assert [row["issue_id"] for row in db.query_task_issues(
        direction="wrong_pass", confirmed_only=False, allowed_task_ids=scope,
    )] == ["provisional", "synthetic-issue"]
    assert [row["issue_id"] for row in db.query_task_issues(
        direction="unfair_fail", allowed_task_ids=scope,
    )] == ["unfair"]
    assert db.query_task_issues("other-task", allowed_task_ids=scope) == []
    assert {row["trace_id"] for row in db.search_traces("", allowed_task_ids=None)} == {
        "trace-1", "unverified", "other-trace",
    }


class DevRegistry(CorpusRegistry):
    """Valid split authority that admits the synthetic non-canonical DEV corpus."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


def test_retrieval_and_pointer_expansion_return_stored_step_flags_on_every_backend(db: CloudTraceDatabase) -> None:
    db.upsert_task(task("labeled", instruction_text="pytest grader"))
    db.upsert_trace(trace(
        "labeled/trace", task_id="labeled", trust_tier="tier2_consensus_labeled", flagged_passage_steps=[2],
    ), steps=[{
        "step_index": index, "role": "user" if index == 0 else "assistant", "content_text": f"pytest grader {index}",
        "flags_json": ["named"] if index == 1 else {"native_node_index": index},
    } for index in range(4)])
    retriever = CloudTraceRetriever(db, registry=DevRegistry())
    (example,) = retriever.retrieve_similar_examples("pytest grader", "reward_seeker", 3)
    assert [(step["step_index"], step["flagged"]) for step in example.trace_steps] == [(1, True), (2, True), (3, False)]
    assert example.trace_steps[0]["flags"] == '["named"]'
    assert retriever.expand_pointer(example.pointer)["steps"] == example.trace_steps


def test_read_only_open_reads_the_same_store_and_refuses_writes(target: tuple[str | Path, bool]) -> None:
    # The PostgreSQL target scopes its schema through the URL's ``options``; a read-only session must keep it.
    with CloudTraceDatabase(target[0], sqlite_fts=target[1]) as writer:
        seed(writer)
    with CloudTraceDatabase(target[0], read_only=True) as reader:
        assert reader.get_trace("trace-1")["task_id"] == "synthetic-task"
        with pytest.raises((sqlite3.OperationalError, psycopg.errors.ReadOnlySqlTransaction)):
            reader.upsert_task(task("other-task"))


def test_postgres_read_only_preserves_environment_options(
    postgres_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = "cloud_trace_" + uuid4().hex
    with psycopg.connect(postgres_server, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            monkeypatch.setenv("PGOPTIONS", f"-csearch_path={schema} -cdefault_transaction_read_only=off")
            with CloudTraceDatabase(postgres_server) as writer:
                seed(writer)
            with CloudTraceDatabase(postgres_server, read_only=True) as reader:
                assert reader.get_trace("trace-1")["task_id"] == "synthetic-task"
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    reader.upsert_task(task("other-task"))
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
