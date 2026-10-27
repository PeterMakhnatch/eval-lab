"""DEV-only, local exports preserve evidence without publishing source identities."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from evallab.cheatbench_fidelity import ANNOTATION_MARKER
from evallab.production import cloud_trace_export
from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import CorpusRegistry

FORMATS = ("harbor", "atif", "scout")
DATASET = "mimo-defect-map-v1"
SOURCE_KEY = "synthetic-source-assignment"
TASKS = (
    ("synthetic/dev-café-Δ-Straße-İ", "dev", "export-item-alpha"),
    ("synthetic/dev-beta", "DEV", "export-item-beta"),
    ("synthetic/test-only", "test", "export-item-test"),
    ("synthetic/registry-sealed-dev", "dev", "export-item-sealed"),
    ("synthetic/heldout-only", "heldout", "export-item-heldout"),
    ("synthetic/source-assigned-test", "dev", "export-item-source-test"),
)
ADMITTED_TASK_IDS = frozenset(task_id for task_id, _split, _item in TASKS[:2])
ADMITTED_TRACES = {
    "trace-alpha-a-private": TASKS[0][0],
    "trace-alpha-b-private": TASKS[0][0],
    "trace-beta-a-private": TASKS[1][0],
}
REFUSED_TRACES = {
    "trace-test-private": TASKS[2][0],
    "trace-sealed-private": TASKS[3][0],
    "trace-heldout-private": TASKS[4][0],
    "trace-source-test-private": TASKS[5][0],
}
REWARDS = dict(zip(ADMITTED_TRACES, (1.0, 0.0, None), strict=True))
STEP_ORIGINS = [2, 7, 13, 19, 31]
USER_TEXT = "Preserve Unicode: café and Δ.\nKeep this second line and spaces  intact."
ASSISTANT_TEXT = "I will inspect the fixture, then report the literal output."
COMMAND = "printf 'alpha  beta\\n'"
TOOL_TEXT = "Tool stdout: alpha  beta\n"
TOOL_PREVIEW = "Return code: 0.\n"
ENVIRONMENT_TEXT = "Environment heartbeat: ready."
ENVIRONMENT_PREVIEW = "Working directory unchanged."
FINAL_TEXT = "The literal output has two spaces between alpha and beta."
PRIVATE_CAS = "cas://sha256/" + "a" * 64
PRIVATE_FIELDS = (
    "task_id", "dataset_id", "trace_id", "model_id", "run_id", "is_exploit",
    "is_task_defect_fail", "flags_json", "flagged_passage_steps", "trust_tier",
    "verified_receipt_json", "raw_cas_uri", "observation_cas_uri", "cas_uri",
)
DISCARDED_SECRETS = (
    "private-task-body-secret", "private-flag-secret", "private-label-secret",
    "private-receipt-secret", "private-issue-secret", "private-outcome-secret",
    "private-annotation-secret", "refused-body-must-never-be-read",
)


def _run_id(trace_id: str) -> str:
    return "private-run-for-" + trace_id


def _model_id(trace_id: str) -> str:
    return "private-provider/model-for-" + trace_id


class SyntheticRegistry(CorpusRegistry):
    """A populated split ledger, including a TEST lock under another source key."""

    def __init__(self) -> None:
        self._assignments = {
            (DATASET, item_id): "test" if split == "test" else "dev"
            for _task_id, split, item_id in TASKS
        }
        self._assignments[(SOURCE_KEY, TASKS[5][2])] = "test"
        authority = "key_id,item_id,split\n" + "".join(
            f"{key},{item},{split}\n"
            for (key, item), split in sorted(self._assignments.items())
        )
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority.encode()).hexdigest()
        self._assignment_keys = frozenset(key for key, _item in self._assignments)
        self._assignment_names = {}
        self._assigned_test_ids = {TASKS[2][2], TASKS[5][2]}
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = {TASKS[3][2]}


def _steps(trace_id: str, task_id: str) -> list[dict[str, Any]]:
    item_id = next(item for task, _split, item in TASKS if task == task_id)
    private_text = " ".join((
        task_id, item_id, DATASET, trace_id, _model_id(trace_id), _run_id(trace_id),
        *PRIVATE_FIELDS, PRIVATE_CAS, "GOODFIRE", "tb4", "MiMo",
    ))
    rows: list[dict[str, Any]] = [
        {"step_index": 2, "role": "user", "content_text": USER_TEXT + "\n" + private_text},
        {
            "step_index": 7, "role": "assistant", "content_text": ASSISTANT_TEXT,
            "tool_name": "terminal", "command_or_action": COMMAND,
        },
        {
            "step_index": 13, "role": "tool", "tool_name": "terminal",
            "command_or_action": COMMAND, "content_text": TOOL_TEXT,
            "observation_preview": TOOL_PREVIEW,
        },
        {
            "step_index": 19, "role": "environment", "content_text": ENVIRONMENT_TEXT,
            "observation_preview": ENVIRONMENT_PREVIEW,
        },
        {
            "step_index": 23, "role": "user",
            "content_text": ANNOTATION_MARKER + "]\nlabel: private-annotation-secret",
        },
        {"step_index": 31, "role": "assistant", "content_text": FINAL_TEXT},
    ]
    for row in rows:
        row["flags_json"] = {"label": "private-label-secret", "flag": "private-flag-secret"}
        row["observation_cas_uri"] = PRIVATE_CAS
    # Insertion order is deliberately not source order or projected ATIF order.
    return [rows[index] for index in (5, 0, 4, 1, 3, 2)]


@pytest.fixture
def registry() -> SyntheticRegistry:
    return SyntheticRegistry()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    directory = tmp_path / "source"
    directory.mkdir()
    path = directory / "cloud.sqlite"
    with CloudTraceDatabase(path, sqlite_fts=False) as database:
        for task_id, split, item_id in TASKS:
            database.upsert_task({
                "task_id": task_id, "dataset_id": DATASET, "split": split,
                "instruction_text": "private-task-body-secret",
                "cas_uri": PRIVATE_CAS,
                "metadata_json": {
                    "item_id": item_id, "labels": "private-label-secret",
                    "cheatbench_label": "private-label-secret", "label_source": "goodfire",
                    "rationale": "private-receipt-secret", "source": "tb4/mimo",
                },
            })
            database.upsert_task_issue({
                "issue_id": task_id + "/private-issue", "task_id": task_id,
                "defect_class": "private-issue-secret", "direction": "wrong_pass",
                "status": "confirmed", "reproducer_script": "private-issue-secret",
                "cas_uri": PRIVATE_CAS,
            })
        for trace_id, task_id in {**ADMITTED_TRACES, **REFUSED_TRACES}.items():
            admitted = trace_id in ADMITTED_TRACES
            database.upsert_trace({
                "trace_id": trace_id, "task_id": task_id, "run_id": _run_id(trace_id),
                "model_id": _model_id(trace_id), "reward": REWARDS.get(trace_id),
                "outcome": "private-outcome-secret", "is_exploit": True,
                "is_task_defect_fail": True, "raw_cas_uri": PRIVATE_CAS,
                "verified_receipt_json": {"label": "private-receipt-secret"},
                "trust_tier": "tier1_container_verified", "flagged_passage_steps": [7] if admitted else [],
            }, steps=_steps(trace_id, task_id) if admitted else [{
                "step_index": 101, "role": "assistant",
                "content_text": "refused-body-must-never-be-read " + trace_id,
                "flags_json": {"label": "private-label-secret"},
            }])
    return path


def _default_manifest(output: Path) -> Path:
    return output.with_name(output.name + ".manifest.json")


def _read_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    for blind_id, entry in value.items():
        assert re.fullmatch(r"run-[0-9a-f]{16,64}", blind_id)
        assert set(entry) == {"trace_id", "step_origin", "reward"}
        assert entry["step_origin"] == STEP_ORIGINS
        assert entry["reward"] == REWARDS[entry["trace_id"]]
    return value


def _tree(directory: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(directory).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in directory.rglob("*")
    }


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _assert_content(container: Any, *expected: str) -> None:
    strings = _strings(container)
    for text in expected:
        assert any(text in value for value in strings), text


def _assert_blinded(output: Path) -> None:
    identities = [DATASET, SOURCE_KEY, *(task_id for task_id, _split, _item in TASKS)]
    identities.extend(item for _task_id, _split, item in TASKS)
    for trace_id in {**ADMITTED_TRACES, **REFUSED_TRACES}:
        identities.extend((trace_id, _run_id(trace_id), _model_id(trace_id)))
    forbidden = (
        *identities, *PRIVATE_FIELDS, *DISCARDED_SECRETS, "cas://", "goodfire", "tb4", "mimo",
        "cheatbench_label", "label_source", "rationale", "flags", "labels", ANNOTATION_MARKER,
        "tier1_container_verified", "tier2_consensus_labeled", "tier3_provisional",
        "defect-map-v1",  # A shorter "mimo" alias must not leave the dataset suffix behind.
        PRIVATE_CAS.rsplit("/", 1)[-1],
    )
    for path in output.rglob("*"):
        visible = path.relative_to(output).as_posix()
        if path.is_file():
            visible += "\n" + path.read_text(encoding="utf-8")
        lowered = visible.casefold()
        for token in forbidden:
            assert token.casefold() not in lowered, (path.relative_to(output), token)


def _assert_receipt(
    receipt: dict[str, Any], output: Path, manifest: Path, *, format: str,
    admitted: int, exported: int, refused: int, before: datetime, after: datetime,
) -> None:
    assert receipt["admitted"] == admitted
    assert receipt["exported"] == exported
    assert receipt["refused"] == refused
    assert receipt["format"] == format
    assert Path(receipt["output_path"]) == output.resolve()
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    timestamp = datetime.fromisoformat(receipt["recorded_at_utc"].replace("Z", "+00:00"))
    offset = timestamp.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert before <= timestamp <= after


def _atif_document(output: Path, blind_id: str, format: str) -> dict[str, Any]:
    path = output / blind_id / "agent/trajectory.json" if format == "harbor" else output / f"{blind_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("format", FORMATS)
def test_formats_export_only_admitted_traces_and_keep_the_scoring_join_private(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    output = tmp_path / ("published-" + format)
    manifest = tmp_path / ("sealed-" + format + ".json")
    before = datetime.now(UTC)
    receipt = cloud_trace_export.export_runs(source, output, format=format, registry=registry, manifest=manifest)
    after = datetime.now(UTC)
    joins = _read_manifest(manifest)
    assert {entry["trace_id"] for entry in joins.values()} == set(ADMITTED_TRACES)
    assert len(joins) == len(ADMITTED_TRACES)
    assert not manifest.is_relative_to(output)
    _assert_receipt(
        receipt, output, manifest, format=format, admitted=3, exported=3, refused=4,
        before=before, after=after,
    )
    _assert_blinded(output)
    if format == "harbor":
        expected_files = {
            f"{blind_id}/{suffix}"
            for blind_id in joins for suffix in ("agent/trajectory.json", "result.json")
        }
        for blind_id in joins:
            result = json.loads((output / blind_id / "result.json").read_text(encoding="utf-8"))
            assert result == {
                "trial_name": blind_id,
                "verifier_result": {"rewards": {"reward": None}},
            }
    elif format == "atif":
        expected_files = {f"{blind_id}.json" for blind_id in joins}
    else:
        expected_files = {"transcripts.jsonl"}
        lines = (output / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert {json.loads(line)["transcript_id"] for line in lines} == set(joins)
    assert {
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    } == expected_files


@pytest.mark.parametrize("format", ("harbor", "atif"))
def test_atif_preserves_sources_tool_calls_and_standalone_observations_with_sparse_origins(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    output = tmp_path / format
    cloud_trace_export.export_runs(source, output, format=format, registry=registry)
    joins = _read_manifest(_default_manifest(output))
    for blind_id in joins:
        document = _atif_document(output, blind_id, format)
        assert document["session_id"] == blind_id
        assert "metadata" not in document
        steps = document["steps"]
        assert [step["step_id"] for step in steps] == list(range(1, len(STEP_ORIGINS) + 1))
        assert [step["source"] for step in steps] == ["user", "agent", "agent", "agent", "agent"]
        assert USER_TEXT in steps[0]["message"]
        assert steps[1]["message"] == ASSISTANT_TEXT
        assert steps[-1]["message"] == FINAL_TEXT
        calls = steps[1]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["function_name"] == "terminal"
        assert calls[0]["arguments"] == {"command": COMMAND}
        assert isinstance(calls[0]["tool_call_id"], str) and calls[0]["tool_call_id"]
        assert sum(len(step.get("tool_calls", [])) for step in steps) == 1
        assert "tool_calls" not in steps[2] and "tool_calls" not in steps[3]
        _assert_content(steps[2]["observation"]["results"], TOOL_TEXT, TOOL_PREVIEW)
        _assert_content(steps[3]["observation"]["results"], ENVIRONMENT_TEXT, ENVIRONMENT_PREVIEW)
        assert len(steps[2]["observation"]["results"]) == 2
        assert len(steps[3]["observation"]["results"]) == 2
        observations = [
            result for step in steps for result in step.get("observation", {}).get("results", [])
        ]
        assert len(observations) == 4
        assert all(result["source_call_id"] is None for result in observations)
        assert all("extra" not in step for step in steps)
        assert "private-annotation-secret" not in json.dumps(document)


def test_scout_jsonl_always_uses_documented_inspect_chat_message_shapes(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
) -> None:
    output = tmp_path / "scout"
    receipt = cloud_trace_export.export_runs(source, output, format="scout", registry=registry)
    joins = _read_manifest(_default_manifest(output))
    assert receipt["scout"]["format"] == "jsonl"
    assert receipt["scout"]["loader"] == "Transcript.model_validate_json"
    assert isinstance(receipt["scout"]["inspect_scout_installed"], bool)
    for line in (output / "transcripts.jsonl").read_text(encoding="utf-8").splitlines():
        transcript = json.loads(line)
        blind_id = transcript["transcript_id"]
        assert blind_id in joins
        assert set(transcript) == {"transcript_id", "metadata", "messages"}
        assert transcript["metadata"] == {"item_id": blind_id}
        messages = transcript["messages"]
        assert messages[0]["role"] == "user"
        assert USER_TEXT in messages[0]["content"]
        assert messages[-1] == {"role": "assistant", "content": FINAL_TEXT}
        for message in messages:
            assert message["role"] in {"system", "user", "assistant", "tool"}
            assert isinstance(message["content"], str)
            assert set(message) <= {"role", "content", "tool_calls", "tool_call_id"}
            if message["role"] == "tool":
                assert message.get("tool_call_id") is None
            for call in message.get("tool_calls", []):
                assert message["role"] == "assistant"
                assert set(call) == {"id", "function", "arguments", "type"}
                assert isinstance(call["id"], str) and call["id"]
                assert call["function"] == "terminal"
                assert call["arguments"] == {"command": COMMAND}
                assert call["type"] == "function"
        assert len(messages) == 7
        assert sum(message["role"] == "assistant" for message in messages) == 2
        assert sum(message["role"] == "tool" for message in messages) == 4
        assert sum(len(message.get("tool_calls", [])) for message in messages) == 1
        _assert_content(messages, ASSISTANT_TEXT, TOOL_TEXT, TOOL_PREVIEW, ENVIRONMENT_TEXT, ENVIRONMENT_PREVIEW)
    _assert_blinded(output)


def test_scout_receipt_reports_an_actual_missing_optional_import(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "inspect_scout", None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("inspect_scout")
    output = tmp_path / "scout-without-sdk"
    receipt = cloud_trace_export.export_runs(source, output, format="scout", registry=registry)
    assert receipt["scout"]["inspect_scout_installed"] is False
    assert receipt["scout"]["loader"] == "Transcript.model_validate_json"
    assert receipt["scout"]["format"] == "jsonl"
    assert len((output / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_scout_roundtrips_through_the_optional_transcript_json_loader(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
) -> None:
    inspect_scout = pytest.importorskip("inspect_scout")
    output = tmp_path / "scout-sdk"
    receipt = cloud_trace_export.export_runs(source, output, format="scout", registry=registry)
    assert receipt["scout"]["inspect_scout_installed"] is True
    joins = _read_manifest(_default_manifest(output))
    for line in (output / "transcripts.jsonl").read_bytes().splitlines():
        transcript = inspect_scout.Transcript.model_validate_json(line)
        assert transcript.transcript_id in joins
        assert transcript.metadata == {"item_id": transcript.transcript_id}
        assert transcript.messages[0].role == "user"
        assert transcript.messages[-1].role == "assistant"


@dataclass
class SqlAudit:
    statements: list[str] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)
    denied: list[tuple[int, str | None, str | None]] = field(default_factory=list)
    connections: list[sqlite3.Connection] = field(default_factory=list)


def _audit_connections(monkeypatch: pytest.MonkeyPatch) -> SqlAudit:
    audit = SqlAudit()
    connect = sqlite3.connect
    forbidden_columns = {
        "corpus_tasks": {"instruction_text", "cas_uri"},
        "agent_traces": {
            "is_exploit", "is_task_defect_fail", "raw_cas_uri", "verified_receipt_json",
            "trust_tier", "flagged_passage_steps", "outcome",
        },
        "trace_steps": {"flags_json", "observation_cas_uri"},
    }
    protected_tables = {"corpus_tasks", "agent_traces", "trace_steps", "task_issues"}

    def authorize(
        action: int, table: str | None, column: str | None,
        _database: str | None, _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ:
            audit.reads.append((table or "", column or ""))
            if table == "task_issues" or column in forbidden_columns.get(table or "", set()):
                audit.denied.append((action, table, column))
                return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and table in protected_tables:
            audit.denied.append((action, table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def audited_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        uri = os.fspath(database)
        assert uri.startswith("file:")
        assert kwargs.get("uri") is True
        assert parse_qs(uri.partition("?")[2]).get("mode") == ["ro"]
        connection = connect(database, *args, **kwargs)
        connection.set_trace_callback(audit.statements.append)
        connection.set_authorizer(authorize)
        audit.connections.append(connection)
        return connection

    monkeypatch.setattr(cloud_trace_export.sqlite3, "connect", audited_connect)
    return audit


def _assert_admitted_body_queries(audit: SqlAudit, expected_traces: set[str]) -> None:
    metadata_queries = [
        statement for statement in audit.statements
        if re.search(r'\b(?:from|join)\s+"?agent_traces"?\b', statement, re.IGNORECASE)
        and re.search(r"\b(?:model_id|run_id|reward)\b", statement, re.IGNORECASE)
    ]
    assert metadata_queries
    for query in metadata_queries:
        assert re.search(r"\bwhere\b", query, re.IGNORECASE)
        assert re.search(r"\btask_id\b", query, re.IGNORECASE)
        assert any(task_id in query for task_id in ADMITTED_TASK_IDS)
    queries = [
        statement for statement in audit.statements
        if re.search(r'\b(?:from|join)\s+"?trace_steps"?\b', statement, re.IGNORECASE)
    ]
    assert queries
    observed = set()
    for query in queries:
        assert re.search(r"\bwhere\b", query, re.IGNORECASE)
        assert re.search(r"\btask_id\b", query, re.IGNORECASE)
        assert any(task_id in query for task_id in ADMITTED_TASK_IDS)
        trace_ids = {trace_id for trace_id in {**ADMITTED_TRACES, **REFUSED_TRACES} if trace_id in query}
        assert trace_ids and trace_ids <= expected_traces
        assert not any(trace_id in query for trace_id in REFUSED_TRACES)
        observed.update(trace_ids)
    assert observed == expected_traces
    assert audit.denied == []
    assert ("trace_steps", "content_text") in audit.reads
    assert not any(table == "task_issues" for table, _column in audit.reads)


@pytest.mark.parametrize("format", FORMATS)
def test_new_read_only_connections_gate_body_sql_and_never_read_private_columns(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, format: str,
) -> None:
    original = source.read_bytes()
    audit = _audit_connections(monkeypatch)
    output = tmp_path / format
    receipt = cloud_trace_export.export_runs(source, output, format=format, registry=registry)
    assert receipt["admitted"] == 3 and receipt["refused"] == 4
    _assert_admitted_body_queries(audit, set(ADMITTED_TRACES))
    assert source.read_bytes() == original
    assert audit.connections
    for connection in audit.connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("format", FORMATS)
def test_source_database_and_its_directory_can_be_physically_read_only(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    before = _tree(source.parent)
    source.chmod(0o444)
    source.parent.chmod(0o555)
    try:
        output = tmp_path / format
        receipt = cloud_trace_export.export_runs(source, output, format=format, registry=registry)
        assert receipt["exported"] == 3
        assert _tree(source.parent) == before
    finally:
        source.parent.chmod(0o755)
        source.chmod(0o644)


def test_blind_ids_are_stable_across_formats_and_limits_count_candidates_before_limit(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
) -> None:
    reference: dict[str, str] | None = None
    for format in FORMATS:
        for limit in (None, 1):
            output = tmp_path / f"{format}-{limit}"
            before = datetime.now(UTC)
            receipt = cloud_trace_export.export_runs(source, output, format=format, registry=registry, limit=limit)
            after = datetime.now(UTC)
            manifest = _default_manifest(output)
            joins = _read_manifest(manifest)
            by_trace = {entry["trace_id"]: blind_id for blind_id, entry in joins.items()}
            assert len(by_trace) == (3 if limit is None else 1)
            if reference is None:
                reference = by_trace
            assert all(reference[trace_id] == blind_id for trace_id, blind_id in by_trace.items())
            assert set(by_trace) <= set(ADMITTED_TRACES)
            _assert_receipt(
                receipt, output, manifest, format=format, admitted=3,
                exported=3 if limit is None else 1, refused=4, before=before, after=after,
            )


@pytest.mark.parametrize("format", FORMATS)
def test_trace_ids_restrict_candidates_and_cannot_admit_test_sealed_or_heldout_rows(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, format: str,
) -> None:
    selected = next(iter(ADMITTED_TRACES))
    requested = [selected, *REFUSED_TRACES, "requested-trace-does-not-exist", selected]
    output = tmp_path / format
    audit = _audit_connections(monkeypatch)
    before = datetime.now(UTC)
    receipt = cloud_trace_export.export_runs(
        source, output, format=format, registry=registry, trace_ids=requested, limit=1,
    )
    after = datetime.now(UTC)
    manifest = _default_manifest(output)
    joins = _read_manifest(manifest)
    assert [entry["trace_id"] for entry in joins.values()] == [selected]
    _assert_receipt(
        receipt, output, manifest, format=format, admitted=1, exported=1, refused=5,
        before=before, after=after,
    )
    _assert_admitted_body_queries(audit, {selected})
    _assert_blinded(output)


@pytest.mark.parametrize("selection", ([], ["missing-only"], list(REFUSED_TRACES)))
def test_empty_or_entirely_refused_selections_publish_no_trace_bodies(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, selection: list[str],
) -> None:
    output = tmp_path / "empty-selection"
    audit = _audit_connections(monkeypatch)
    receipt = cloud_trace_export.export_runs(source, output, format="atif", registry=registry, trace_ids=selection)
    assert receipt["admitted"] == receipt["exported"] == 0
    assert receipt["refused"] == len(selection)
    assert _read_manifest(_default_manifest(output)) == {}
    assert _tree(output) == {}
    assert not any(table == "trace_steps" for table, _column in audit.reads)


def test_limit_zero_preserves_admission_counts_without_reading_any_step_body(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "zero-limit"
    audit = _audit_connections(monkeypatch)
    receipt = cloud_trace_export.export_runs(source, output, format="atif", registry=registry, limit=0)
    assert (receipt["admitted"], receipt["exported"], receipt["refused"]) == (3, 0, 4)
    assert _read_manifest(_default_manifest(output)) == {}
    assert _tree(output) == {}
    assert not any(table == "trace_steps" for table, _column in audit.reads)


def test_missing_split_authority_fails_closed_before_any_step_body_is_read(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry._assignments = {}
    output = tmp_path / "no-authority"
    audit = _audit_connections(monkeypatch)
    receipt = cloud_trace_export.export_runs(source, output, format="atif", registry=registry)
    assert (receipt["admitted"], receipt["exported"], receipt["refused"]) == (0, 0, 7)
    assert _read_manifest(_default_manifest(output)) == {}
    assert not any(table == "trace_steps" for table, _column in audit.reads)


@pytest.mark.parametrize("existing", (
    "output-directory", "output-empty-directory", "output-file",
    "manifest-file", "manifest-directory", "manifest-empty-directory",
))
def test_existing_destinations_are_refused_without_overwriting_or_leaving_staging_files(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, existing: str,
) -> None:
    directory = tmp_path / "destinations"
    directory.mkdir()
    output = directory / "runs"
    manifest = directory / "sealed.json"
    target = output if existing.startswith("output") else manifest
    if existing.endswith("directory"):
        target.mkdir()
        if "empty" not in existing:
            (target / "retain").write_bytes(b"someone else's evidence")
    else:
        target.write_bytes(b"someone else's evidence")
    before = _tree(directory)
    with pytest.raises((FileExistsError, ValueError, OSError)):
        cloud_trace_export.export_runs(source, output, format="atif", registry=registry, manifest=manifest)
    assert _tree(directory) == before


@pytest.mark.parametrize("nested", ("same-path", "direct-child", "deep-child"))
def test_manifest_cannot_be_published_inside_the_actor_visible_output(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, nested: str,
) -> None:
    directory = tmp_path / "destinations"
    directory.mkdir()
    output = directory / "runs"
    manifest = {
        "same-path": output, "direct-child": output / "manifest.json",
        "deep-child": output / "private/manifest.json",
    }[nested]
    with pytest.raises(ValueError, match="manifest"):
        cloud_trace_export.export_runs(source, output, format="atif", registry=registry, manifest=manifest)
    assert _tree(directory) == {}


@pytest.mark.parametrize("format", FORMATS)
def test_write_failure_discards_the_entire_staged_export_and_manifest(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, format: str,
) -> None:
    directory = tmp_path / "destinations"
    directory.mkdir()
    output = directory / "runs"
    manifest = directory / "sealed.json"
    original_open = Path.open
    writes = 0

    def fail_second_write(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        if any(flag in mode for flag in "wax"):
            writes += 1
            if writes == 2:
                raise OSError("synthetic export write failure")
        return original_open(path, mode, *args, **kwargs)

    original = source.read_bytes()
    monkeypatch.setattr(Path, "open", fail_second_write)
    with pytest.raises(OSError, match="synthetic export write failure"):
        cloud_trace_export.export_runs(source, output, format=format, registry=registry, manifest=manifest)
    assert writes == 2
    assert _tree(directory) == {}
    assert source.read_bytes() == original


def test_failure_after_join_link_creation_rolls_back_the_private_join_and_staged_runs(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "destinations"
    directory.mkdir()
    output = directory / "runs"
    manifest = directory / "sealed.json"
    link = os.link
    linked = False

    def fail_after_link(source_path: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal linked
        link(source_path, destination, *args, **kwargs)
        if Path(destination) == manifest:
            linked = True
            raise OSError("synthetic join publication failure")

    monkeypatch.setattr(cloud_trace_export.os, "link", fail_after_link)
    with pytest.raises(OSError, match="synthetic join publication failure"):
        cloud_trace_export.export_runs(source, output, format="atif", registry=registry, manifest=manifest)
    assert linked
    assert _tree(directory) == {}


@pytest.fixture
def isolated_cli(
    registry: SyntheticRegistry, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from evallab import cli
    from evallab.production import cli as production_cli
    from evallab.production import corpus_queue

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("export-runs opened a queue, runtime, or broker")

    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    monkeypatch.setenv("EVALLAB_CLOUD_DATABASE_URL", "postgresql://must-not-be-contacted.invalid/private")
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(corpus_queue, "CorpusRegistry", lambda _root: registry)
    for name in ("_open_queue", "_runtime", "_relay"):
        monkeypatch.setattr(production_cli, name, forbidden)
    return cli


@pytest.mark.parametrize("format", FORMATS)
def test_cli_exports_the_explicit_local_database_without_opening_a_queue(
    source: Path, tmp_path: Path, isolated_cli: Any,
    capsys: pytest.CaptureFixture[str], format: str,
) -> None:
    output = tmp_path / ("cli-" + format)
    manifest = tmp_path / ("private-cli-" + format + ".json")
    code = isolated_cli.run_cli([
        "prod", "export-runs", "--source-database", source.relative_to(tmp_path).as_posix(),
        "--output-directory", output.name, "--format", format, "--manifest", manifest.name,
    ], workspace=tmp_path)
    assert code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["command"] == "export-runs"
    assert (receipt["admitted"], receipt["exported"], receipt["refused"]) == (3, 3, 4)
    assert receipt["output_path"] == str(output)
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert {entry["trace_id"] for entry in _read_manifest(manifest).values()} == set(ADMITTED_TRACES)
    _assert_blinded(output)
    assert not (tmp_path / "derived/production/prod-queue.sqlite").exists()


def test_cli_requires_an_explicit_private_manifest_before_creating_output(
    source: Path, tmp_path: Path, isolated_cli: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "must-not-be-created"
    with pytest.raises(SystemExit) as stopped:
        isolated_cli.run_cli([
            "prod", "export-runs", "--source-database", str(source),
            "--output-directory", str(output), "--format", "atif",
        ], workspace=tmp_path)
    assert stopped.value.code == 2
    assert "--manifest" in capsys.readouterr().err
    assert not output.exists()
    assert not _default_manifest(output).exists()
    assert not (tmp_path / "derived/production/prod-queue.sqlite").exists()


def test_cli_passes_trace_restrictions_and_limit_without_widening_admission(
    source: Path, tmp_path: Path, isolated_cli: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    selected = tuple(ADMITTED_TRACES)[1]
    refused = next(iter(REFUSED_TRACES))
    output = tmp_path / "cli-subset"
    manifest = tmp_path / "private-cli-subset.json"
    code = isolated_cli.run_cli([
        "prod", "export-runs", "--source-database", str(source),
        "--output-directory", str(output), "--format", "atif", "--manifest", str(manifest),
        "--trace-id", selected, "--trace-id", refused, "--trace-id", "missing-cli-trace", "--limit", "1",
    ], workspace=tmp_path)
    assert code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert (receipt["admitted"], receipt["exported"], receipt["refused"]) == (1, 1, 2)
    assert [entry["trace_id"] for entry in _read_manifest(manifest).values()] == [selected]
    assert not (tmp_path / "derived/production/prod-queue.sqlite").exists()


def test_system_and_agent_aliases_and_unpaired_tool_output_retain_their_origins(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
) -> None:
    trace_id, task_id = next(iter(ADMITTED_TRACES.items()))
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        database.upsert_trace({
            "trace_id": trace_id, "task_id": task_id,
            "run_id": _run_id(trace_id), "model_id": _model_id(trace_id),
            "flagged_passage_steps": [],
        }, steps=[
            {"step_index": 1, "role": "tool", "content_text": "Initial environment output."},
            {"step_index": 4, "role": "system", "content_text": "Follow the task instruction."},
            {
                "step_index": 10, "role": "agent", "content_text": "Inspect the workspace.",
                "tool_name": "terminal", "command_or_action": COMMAND,
            },
        ])
    output = tmp_path / "role-aliases"
    receipt = cloud_trace_export.export_runs(
        source, output, format="atif", registry=registry, trace_ids=[trace_id],
    )
    assert receipt["exported"] == 1
    joins = json.loads(_default_manifest(output).read_text(encoding="utf-8"))
    blind_id, join = next(iter(joins.items()))
    assert join["step_origin"] == [1, 4, 10]
    document = _atif_document(output, blind_id, "atif")
    assert [step["source"] for step in document["steps"]] == ["agent", "system", "agent"]
    _assert_content(document["steps"][0]["observation"], "Initial environment output.")
    assert document["steps"][1]["message"] == "Follow the task instruction."
    assert document["steps"][2]["tool_calls"][0]["arguments"] == {"command": COMMAND}
    _assert_blinded(output)


@pytest.mark.parametrize("format", FORMATS)
def test_nullable_text_and_tool_calls_only_rows_export_as_text_messages(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    trace_id, task_id = next(iter(ADMITTED_TRACES.items()))
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        database.upsert_trace({
            "trace_id": trace_id, "task_id": task_id,
            "run_id": _run_id(trace_id), "model_id": _model_id(trace_id),
            "flagged_passage_steps": [],
        }, steps=[
            {"step_index": 0, "role": "system"},
            {"step_index": 1, "role": "user"},
            {
                "step_index": 2, "role": "assistant",
                "tool_name": "terminal", "command_or_action": COMMAND,
            },
            {"step_index": 3, "role": "tool", "tool_name": "terminal"},
            {"step_index": 4, "role": "environment"},
        ])
    output = tmp_path / ("nullable-" + format)
    receipt = cloud_trace_export.export_runs(
        source, output, format=format, registry=registry, trace_ids=[trace_id],
    )
    assert receipt["exported"] == 1
    joins = json.loads(_default_manifest(output).read_text(encoding="utf-8"))
    blind_id, join = next(iter(joins.items()))
    assert join["step_origin"] == [0, 1, 2, 3, 4]
    if format == "scout":
        document = json.loads((output / "transcripts.jsonl").read_text(encoding="utf-8"))
        assert all(message["content"] == "" for message in document["messages"])
        assert any(message.get("tool_calls") for message in document["messages"])
    else:
        document = _atif_document(output, blind_id, format)
        assert all(step["message"] == "" for step in document["steps"])
        assert document["steps"][2]["tool_calls"][0]["arguments"] == {"command": COMMAND}
        assert document["steps"][3]["observation"]["results"][0]["content"] == ""
    _assert_blinded(output)


@pytest.mark.parametrize("format", ("harbor", "atif"))
def test_external_atif_validates_with_the_real_harbor_trajectory(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    trajectory_type = pytest.importorskip("harbor.models.trajectories.trajectory").Trajectory
    output = tmp_path / ("native-" + format)
    cloud_trace_export.export_runs(source, output, format=format, registry=registry)
    joins = _read_manifest(_default_manifest(output))
    for blind_id in joins:
        path = output / blind_id / "agent/trajectory.json" if format == "harbor" else output / f"{blind_id}.json"
        trajectory = trajectory_type.model_validate_json(path.read_bytes())
        assert trajectory.session_id == blind_id
        assert [step.step_id for step in trajectory.steps] == [1, 2, 3, 4, 5]
        assert sum(len(step.tool_calls or []) for step in trajectory.steps) == 1
        assert sum(
            len(step.observation.results) if step.observation is not None else 0
            for step in trajectory.steps
        ) == 4
        for step in trajectory.steps:
            calls = {call.tool_call_id for call in step.tool_calls or []}
            if step.observation is not None:
                assert all(
                    result.source_call_id is None or result.source_call_id in calls
                    for result in step.observation.results
                )


@pytest.mark.parametrize("format", FORMATS)
def test_duplicate_previews_and_tool_metadata_do_not_invent_actions_or_observations(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    trace_id, task_id = next(iter(ADMITTED_TRACES.items()))
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        database.upsert_trace({
            "trace_id": trace_id, "task_id": task_id,
            "run_id": _run_id(trace_id), "model_id": _model_id(trace_id),
            "flagged_passage_steps": [],
        }, steps=[
            {
                "step_index": 1, "role": "assistant", "content_text": ASSISTANT_TEXT,
                "tool_name": "terminal", "command_or_action": COMMAND,
                "observation_preview": TOOL_PREVIEW,
            },
            {
                "step_index": 2, "role": "tool", "tool_name": "terminal",
                "command_or_action": COMMAND, "content_text": TOOL_TEXT,
                "observation_preview": TOOL_TEXT,
            },
            {
                "step_index": 3, "role": "environment", "tool_name": "terminal",
                "command_or_action": COMMAND, "content_text": ENVIRONMENT_TEXT,
                "observation_preview": ENVIRONMENT_TEXT,
            },
        ])
    output = tmp_path / ("deduplicated-" + format)
    cloud_trace_export.export_runs(
        source, output, format=format, registry=registry, trace_ids=[trace_id],
    )
    joins = json.loads(_default_manifest(output).read_text(encoding="utf-8"))
    blind_id, join = next(iter(joins.items()))
    assert join["step_origin"] == [1, 2, 3]
    if format == "scout":
        transcript = json.loads((output / "transcripts.jsonl").read_text(encoding="utf-8"))
        messages = transcript["messages"]
        assert len(messages) == 4
        assert sum(len(message.get("tool_calls", [])) for message in messages) == 1
        observations = [message for message in messages if message["role"] == "tool"]
        assert [message["content"] for message in observations] == [
            TOOL_PREVIEW, TOOL_TEXT, ENVIRONMENT_TEXT,
        ]
        call_id = messages[0]["tool_calls"][0]["id"]
        assert observations[0]["tool_call_id"] == call_id
        assert all("tool_call_id" not in observation for observation in observations[1:])
    else:
        document = _atif_document(output, blind_id, format)
        steps = document["steps"]
        assert sum(len(step.get("tool_calls", [])) for step in steps) == 1
        observations = [step["observation"]["results"] for step in steps]
        assert [len(results) for results in observations] == [1, 1, 1]
        assert [results[0]["content"] for results in observations] == [
            TOOL_PREVIEW, TOOL_TEXT, ENVIRONMENT_TEXT,
        ]
        call_id = steps[0]["tool_calls"][0]["tool_call_id"]
        assert observations[0][0]["source_call_id"] == call_id
        assert all(results[0]["source_call_id"] is None for results in observations[1:])
    _assert_blinded(output)


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("role,field", (
    ("user", "content_text"),
    ("assistant", "content_text"),
    ("assistant", "tool_name"),
    ("assistant", "command_or_action"),
    ("assistant", "observation_preview"),
    ("tool", "content_text"),
    ("tool", "observation_preview"),
    ("environment", "content_text"),
    ("environment", "observation_preview"),
))
def test_surviving_annotations_are_refused_before_redaction_can_hide_the_marker(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    format: str, role: str, field: str,
) -> None:
    trace_id, task_id = next(iter(ADMITTED_TRACES.items()))
    row = {
        "step_index": 1, "role": role, "content_text": "Routine evidence.",
        "tool_name": "terminal", "command_or_action": COMMAND,
    }
    row[field] = "Evidence before annotation.\n" + ANNOTATION_MARKER + "]\nlabel: private-annotation-secret"
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        database.upsert_trace({
            "trace_id": trace_id, "task_id": task_id,
            "run_id": _run_id(trace_id), "model_id": _model_id(trace_id),
            "flagged_passage_steps": [],
        }, steps=[row])
    directory = tmp_path / "annotation-destinations"
    directory.mkdir()
    with pytest.raises(ValueError, match="evaluator-only"):
        cloud_trace_export.export_runs(
            source, directory / "runs", format=format, registry=registry, trace_ids=[trace_id],
        )
    assert _tree(directory) == {}


@pytest.mark.parametrize("format", FORMATS)
def test_verifier_outcomes_are_sealed_and_cannot_change_any_exported_bytes(
    source: Path, registry: SyntheticRegistry, tmp_path: Path, format: str,
) -> None:
    first = tmp_path / ("original-outcomes-" + format)
    cloud_trace_export.export_runs(source, first, format=format, registry=registry)
    first_joins = _read_manifest(_default_manifest(first))
    changed_rewards = dict(zip(ADMITTED_TRACES, (0.0, 1.0, 0.75), strict=True))
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        for trace_id, reward in changed_rewards.items():
            database._connection.execute(
                "UPDATE agent_traces SET reward = ? WHERE trace_id = ?", (reward, trace_id),
            )
    second = tmp_path / ("changed-outcomes-" + format)
    cloud_trace_export.export_runs(source, second, format=format, registry=registry)
    second_joins = json.loads(_default_manifest(second).read_text(encoding="utf-8"))
    assert _tree(first) == _tree(second)
    assert {
        entry["trace_id"]: entry["reward"] for entry in first_joins.values()
    } == REWARDS
    assert {
        entry["trace_id"]: entry["reward"] for entry in second_joins.values()
    } == changed_rewards


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("steps", (
    [],
    [{"step_index": 1, "role": "user", "content_text": ANNOTATION_MARKER + "] evaluator-only"}],
))
def test_native_exports_refuse_traces_without_any_actor_visible_steps(
    source: Path, registry: SyntheticRegistry, tmp_path: Path,
    format: str, steps: list[dict[str, Any]],
) -> None:
    trace_id, task_id = next(iter(ADMITTED_TRACES.items()))
    with CloudTraceDatabase(source, sqlite_fts=False) as database:
        database.upsert_trace({
            "trace_id": trace_id, "task_id": task_id,
            "run_id": _run_id(trace_id), "model_id": _model_id(trace_id),
            "flagged_passage_steps": [],
        }, steps=steps)
    directory = tmp_path / "empty-trace-destinations"
    directory.mkdir()
    with pytest.raises(ValueError, match="no actor-visible steps"):
        cloud_trace_export.export_runs(
            source, directory / "runs", format=format, registry=registry, trace_ids=[trace_id],
        )
    assert _tree(directory) == {}
