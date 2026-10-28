"""Offline synthetic projection boundaries; optional real SDK, never real data/calls."""

from __future__ import annotations

import asyncio
import builtins
import copy
import importlib.util
import json
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

VERSION = "native-actor-v2"
BLIND_SCHEMA = "evallab.blind_run/v1"
BASE_COLUMNS = {
    "transcript_id",
    "messages",
    "events",
    "timelines",
    "events_data",
    "source_type",
    "source_id",
    "source_uri",
    "date",
    "task_set",
    "task_id",
    "task_repeat",
    "agent",
    "agent_args",
    "model",
    "model_options",
    "score",
    "score_explanation",
    "success",
    "message_count",
    "total_time",
    "total_tokens",
    "error",
    "limit",
}


@pytest.fixture
def projection(monkeypatch):
    here = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(here.parents[3] / "src"))
    spec = importlib.util.spec_from_file_location(
        "tt4_paid_projection_test", here / "paid_projection.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class Message:
    def __init__(self, value):
        self.value = copy.deepcopy(value)

    def model_dump(self, **options):
        assert options == {
            "mode": "json",
            "by_alias": False,
            "exclude_none": False,
            "exclude_unset": False,
            "exclude_defaults": False,
        }
        return copy.deepcopy(self.value)


def message():
    return {
        "id": "step-3-message-1",
        "role": "assistant",
        "source": None,
        "metadata": {"step_index": 3},
        "model": None,
        "content": [
            {"type": "reasoning", "reasoning": "synthetic rationale", "signature": None},
            {"type": "text", "text": "synthetic answer\nunchanged"},
        ],
        "tool_calls": [
            {
                "id": "call-3",
                "function": "bash",
                "type": "function",
                "arguments": {"command": "printf fixture", "nested": {"a": 1, "b": 2}},
            }
        ],
    }


def transcript(opaque, messages=None, metadata=None):
    return SimpleNamespace(
        transcript_id=opaque,
        messages=[Message(value) for value in (messages if messages is not None else [message()])],
        metadata=metadata
        if metadata is not None
        else {
            "blind_schema": BLIND_SCHEMA,
            "projection_version": VERSION,
        },
    )


def row(opaque, messages=None, **extra):
    return {
        "transcript_id": opaque,
        "messages": json.dumps(messages if messages is not None else [message()]),
        **extra,
    }


def snapshot(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def fake_runtime(projection, monkeypatch, tmp_path):
    class MessageAdapter:
        def validate_python(self, values, *, context):
            assert context == {"deserializing": True}
            return [Message(value) for value in values]

    adapter = SimpleNamespace(
        ACTOR_PROJECTION_VERSION=VERSION,
        BLIND_SCHEMA=BLIND_SCHEMA,
        insert_calls=[],
    )

    def check(directory, *, claim=False):
        marker = directory / "projection.json"
        if marker.is_file():
            if json.loads(marker.read_text()).get("projection_version") != VERSION:
                raise ValueError("stale")
        elif next(directory.rglob("*.parquet"), None) is not None:
            raise ValueError("unversioned")
        elif claim:
            directory.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"projection_version": VERSION}))

    async def insert(directory, transcripts):
        check(directory, claim=True)
        transcripts = list(transcripts)
        adapter.insert_calls.append([item.transcript_id for item in transcripts])
        rows = []
        for item in transcripts:
            encoded = {column: None for column in BASE_COLUMNS}
            encoded.update(
                {
                    "transcript_id": item.transcript_id,
                    "messages": projection._canonical_messages(item.messages).decode(),
                    "events": "[]",
                    "events_data": '{"messages":[],"calls":[]}',
                    **item.metadata,
                }
            )
            rows.append(encoded)
        (directory / "native-output.parquet").write_text(json.dumps(rows))

    def parquet_rows(paths, *, full=False):
        for path in paths:
            for item in json.loads(path.read_text()):
                yield (
                    item
                    if full
                    else {
                        "transcript_id": item.get("transcript_id"),
                        "messages": item.get("messages"),
                    }
                )

    adapter._check_transcripts, adapter._insert = check, insert
    monkeypatch.setattr(projection, "_native_adapter", lambda: adapter)
    monkeypatch.setattr(projection, "_native_columns", lambda: BASE_COLUMNS)
    monkeypatch.setattr(projection, "_message_adapter", lambda: MessageAdapter())
    monkeypatch.setattr(projection, "_parquet_rows", parquet_rows)
    current = {}
    monkeypatch.setattr(projection, "_project_current", lambda db, keys, sdk: current)

    def make(opaque_ids=("opaque-b", "opaque-a"), frozen_rows=None):
        frozen, output = tmp_path / "historical", tmp_path / f"transcripts-{VERSION}"
        frozen.mkdir()
        keys = [
            {"opaque_id": opaque, "run_id": f"private-run-{i}"}
            for i, opaque in enumerate(opaque_ids)
        ]
        rows = (
            frozen_rows
            if frozen_rows is not None
            else [row(opaque) for opaque in reversed(opaque_ids)]
        )
        (frozen / "old-native.parquet").write_text(json.dumps(rows))
        current.update({opaque: (transcript(opaque), None) for opaque in opaque_ids})

        def prepare(checker=lambda messages: True, *, selected=None, destination=None):
            return projection.prepare_projection(
                tmp_path / "source.sqlite",
                keys if selected is None else selected,
                frozen,
                output if destination is None else destination,
                secret_safe=checker,
            )

        return SimpleNamespace(
            frozen=frozen,
            output=output,
            keys=keys,
            current=current,
            adapter=adapter,
            prepare=prepare,
        )

    return make


def test_module_and_normalization_do_not_import_optional_sdks(monkeypatch):
    here = Path(__file__).resolve().parent
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"inspect_ai", "inspect_scout", "pyarrow", "pydantic"}:
            pytest.fail("pure import attempted an optional SDK")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location(
        "tt4_projection_lazy", here / "paid_projection.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads(module._canonical_messages([Message(message())])) == [message()]


def test_canonical_dump_normalizes_keys_but_not_message_order(projection):
    first = message()
    reordered = dict(reversed(list(copy.deepcopy(first).items())))
    reordered["tool_calls"][0]["arguments"]["nested"] = {"b": 2, "a": 1}
    assert projection._canonical_messages([Message(first)]) == projection._canonical_messages(
        [Message(reordered)]
    )
    second = {**message(), "id": "step-4-message-2", "content": "second"}
    assert projection._canonical_messages(
        [Message(first), Message(second)]
    ) != projection._canonical_messages(
        [
            Message(second),
            Message(first),
        ]
    )
    assert json.loads(projection._canonical_messages([Message(first)]))[0]["source"] is None
    with pytest.raises(projection._MissingHistory):
        projection._canonical_messages([])
    invalid = {**message(), "metadata": {"nonfinite": float("nan")}}
    with pytest.raises(ValueError):
        projection._canonical_messages([Message(invalid)])


@pytest.mark.parametrize(
    "field",
    [
        "role",
        "content",
        "reasoning",
        "tool_arguments",
        "tool_function",
        "tool_id",
        "message_id",
        "step_metadata",
        "source",
        "model",
        "tool_call_id",
        "default_null",
    ],
)
def test_full_message_drift_abstains_without_writing_history(fake_runtime, field):
    scenario = fake_runtime(("opaque-only",))
    changed = message()
    if field == "role":
        changed["role"] = "user"
    elif field == "content":
        changed["content"][1]["text"] += " "
    elif field == "reasoning":
        changed["content"][0]["reasoning"] += " changed"
    elif field == "tool_arguments":
        changed["tool_calls"][0]["arguments"]["nested"]["a"] = 9
    elif field == "tool_function":
        changed["tool_calls"][0]["function"] = "other"
    elif field == "tool_id":
        changed["tool_calls"][0]["id"] = "other-call"
    elif field == "message_id":
        changed["id"] = "step-3-message-2"
    elif field == "step_metadata":
        changed["metadata"]["step_index"] = 4
    elif field == "source":
        changed["source"] = "generate"
    elif field == "model":
        changed["model"] = "different-model"
    elif field == "tool_call_id":
        changed["tool_call_id"] = "other-observation"
    else:
        del changed["source"]
    scenario.current["opaque-only"] = transcript("opaque-only", [changed]), None
    receipt = scenario.prepare()
    assert receipt["eligible_opaque_ids"] == []
    assert receipt["rejected"] == [{"opaque_id": "opaque-only", "category": "message_drift"}]
    hashes = receipt["message_hashes"][0]
    assert hashes["source_messages_sha256"] != hashes["current_messages_sha256"]
    assert not list(scenario.output.rglob("*.parquet"))
    assert scenario.adapter.insert_calls == []


def test_source_rows_are_not_deduplicated_and_missing_histories_fail_closed(fake_runtime):
    conflict = {**message(), "content": "conflicting history"}
    scenario = fake_runtime(
        ("missing", "empty", "invalid", "duplicate", "conflict", "eligible"),
        frozen_rows=[
            row("empty", []),
            {"transcript_id": "invalid", "messages": "not-json"},
            row("duplicate"),
            row("duplicate"),
            row("conflict"),
            row("conflict", [conflict]),
            row("conflict"),
            row("eligible"),
        ],
    )
    receipt = scenario.prepare()
    assert receipt["eligible_opaque_ids"] == ["eligible"]
    assert receipt["rejected"] == [
        {"opaque_id": "missing", "category": "frozen_id_missing"},
        {"opaque_id": "empty", "category": "frozen_history_missing"},
        {"opaque_id": "invalid", "category": "frozen_history_invalid"},
        {"opaque_id": "duplicate", "category": "frozen_id_duplicate"},
        {"opaque_id": "conflict", "category": "frozen_id_conflict"},
    ]
    assert [item["opaque_id"] for item in receipt["message_hashes"]] == [
        key["opaque_id"] for key in scenario.keys
    ]
    assert receipt["message_hashes"][4]["source_messages_sha256"] is None
    assert scenario.adapter.insert_calls == [["eligible"]]


@pytest.mark.parametrize("raw", [None, "", "null", "[]"])
def test_missing_native_history_is_not_decoded_as_empty(fake_runtime, projection, raw):
    fake_runtime(("opaque-only",))
    with pytest.raises(projection._MissingHistory):
        projection._decode_native_messages(raw)


@pytest.mark.parametrize(
    "raw", ["{}", '[{"content":"no role"}]', '[{"role":"assistant"}]', "[1]", []]
)
def test_malformed_native_history_has_no_fabricated_defaults(fake_runtime, projection, raw):
    fake_runtime(("opaque-only",))
    with pytest.raises(ValueError):
        projection._decode_native_messages(raw)


@pytest.mark.parametrize("failure", ["unavailable", "empty", "invalid", "metadata"])
def test_current_history_failures_abstain(fake_runtime, failure):
    scenario = fake_runtime(("opaque-only",))
    if failure == "unavailable":
        scenario.current["opaque-only"] = None, "actor_history_unavailable"
        category = "actor_history_unavailable"
    elif failure == "empty":
        scenario.current["opaque-only"] = transcript("opaque-only", []), None
        category = "actor_history_unavailable"
    elif failure == "invalid":
        scenario.current["opaque-only"] = SimpleNamespace(messages=[object()]), None
        category = "current_history_invalid"
    else:
        scenario.current["opaque-only"] = (
            transcript("opaque-only", metadata={"blind_schema": BLIND_SCHEMA}),
            None,
        )
        category = "current_projection_metadata_invalid"
    receipt = scenario.prepare()
    assert receipt["rejected"] == [{"opaque_id": "opaque-only", "category": category}]
    assert receipt["eligible_opaque_ids"] == []
    assert scenario.adapter.insert_calls == []


def test_secret_checker_is_required_before_sdk_or_filesystem_access(
    projection, tmp_path, monkeypatch
):
    monkeypatch.setattr(projection, "_native_adapter", lambda: pytest.fail("SDK should not load"))
    arguments = (tmp_path / "db", [], tmp_path / "frozen", tmp_path / "output")
    with pytest.raises(TypeError):
        projection.prepare_projection(*arguments)
    with pytest.raises(projection.ProjectionError, match="^secret_checker_required$"):
        projection.prepare_projection(*arguments, secret_safe=None)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("value", [False, None, 1, "true", {"safe": True}])
def test_secret_checker_requires_true_not_truthiness(fake_runtime, value):
    scenario = fake_runtime(("opaque-only",))
    receipt = scenario.prepare(lambda messages: value)
    assert receipt["rejected"] == [{"opaque_id": "opaque-only", "category": "secret_unsafe"}]
    assert receipt["output_parquet_sha256"] == {}
    assert scenario.adapter.insert_calls == []


def test_secret_exception_is_content_free_and_checker_cannot_mutate_native_messages(fake_runtime):
    scenario = fake_runtime(("opaque-only",))

    def unsafe(messages):
        raise RuntimeError("sensitive actor body private-run-0")

    receipt = scenario.prepare(unsafe)
    assert receipt["rejected"] == [{"opaque_id": "opaque-only", "category": "secret_check_failed"}]
    assert "sensitive actor" not in json.dumps(receipt)


def test_checker_receives_full_detached_typed_dumps(fake_runtime):
    scenario = fake_runtime(("opaque-only",))
    observed = []

    def safe(messages):
        observed.append(copy.deepcopy(messages))
        messages[0]["content"][0]["reasoning"] = "mutated checker copy"
        messages[0]["tool_calls"] = []
        return True

    receipt = scenario.prepare(safe)
    assert observed == [[message()]]
    assert receipt["eligible_opaque_ids"] == ["opaque-only"]
    output = json.loads((scenario.output / "native-output.parquet").read_text())
    assert json.loads(output[0]["messages"]) == [message()]


def test_order_blinding_hashes_and_exact_cache_reuse(fake_runtime, projection):
    scenario = fake_runtime(
        ("opaque-z", "opaque-a", "opaque-m"),
        frozen_rows=[
            row(opaque, label="PUBLISHER-SECRET", family="FAMILY-SECRET")
            for opaque in (
                "opaque-m",
                "opaque-a",
                "opaque-z",
            )
        ],
    )
    before = snapshot(scenario.frozen)
    first = scenario.prepare()
    output_before = snapshot(scenario.output)
    observed = []
    second = scenario.prepare(lambda messages: observed.append(messages) is None)
    assert first == second
    assert first["eligible_opaque_ids"] == ["opaque-z", "opaque-a", "opaque-m"]
    assert len(observed) == 3
    assert scenario.adapter.insert_calls == [["opaque-z", "opaque-a", "opaque-m"]]
    assert snapshot(scenario.frozen) == before
    assert snapshot(scenario.output) == output_before
    assert first["projection_version"] == VERSION
    assert first["output_parquet_sha256"]
    assert all(
        len(name) == len(digest) == 64 for name, digest in first["source_parquet_sha256"].items()
    )
    serialized = json.dumps(first)
    for forbidden in (
        "PUBLISHER-SECRET",
        "FAMILY-SECRET",
        "private-run-",
        "synthetic answer",
        "synthetic rationale",
    ):
        assert forbidden not in serialized
    marker = json.loads((scenario.output / "projection.json").read_text())
    assert marker == {"projection_version": VERSION}
    assert (scenario.output / projection.RECEIPT_NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["order", "mapping", "current", "secret", "frozen_bytes"])
def test_existing_output_rejects_input_drift_without_rebinding(fake_runtime, projection, mutation):
    scenario = fake_runtime()
    scenario.prepare()
    original = snapshot(scenario.output)
    selected, checker = scenario.keys, lambda messages: True
    if mutation == "order":
        selected = list(reversed(scenario.keys))
    elif mutation == "mapping":
        selected = [{**scenario.keys[0], "run_id": "another-private-run"}, scenario.keys[1]]
    elif mutation == "current":
        changed = {**message(), "content": "changed current message"}
        scenario.current["opaque-b"] = transcript("opaque-b", [changed]), None
    elif mutation == "secret":

        def checker(messages):
            return False
    else:
        path = scenario.frozen / "old-native.parquet"
        path.write_text(path.read_text() + "\n")
    with pytest.raises(projection.ProjectionError, match="^projection_input_drift$"):
        scenario.prepare(checker, selected=selected)
    assert snapshot(scenario.output) == original
    assert len(scenario.adapter.insert_calls) == 1


@pytest.mark.parametrize("mutation", ["bytes", "extra_file", "missing_parquet", "marker"])
def test_existing_output_rejects_artifact_drift(fake_runtime, projection, mutation):
    scenario = fake_runtime()
    scenario.prepare()
    parquet = scenario.output / "native-output.parquet"
    if mutation == "bytes":
        parquet.write_text(parquet.read_text() + "\n")
    elif mutation == "extra_file":
        (scenario.output / "foreign.idx").write_bytes(b"foreign")
    elif mutation == "missing_parquet":
        parquet.unlink()
    else:
        (scenario.output / "projection.json").write_text(
            json.dumps({"projection_version": "retired"})
        )
    original = snapshot(scenario.output)
    category = "stale_projection_cache" if mutation == "marker" else "projection_output_drift"
    with pytest.raises(projection.ProjectionError, match=f"^{category}$"):
        scenario.prepare()
    assert snapshot(scenario.output) == original
    assert len(scenario.adapter.insert_calls) == 1


@pytest.mark.parametrize("mutation", ["messages", "labels", "source", "duplicate"])
def test_semantic_cache_validation_is_not_just_receipt_hash_comparison(
    fake_runtime, projection, mutation
):
    scenario = fake_runtime()
    scenario.prepare()
    path = scenario.output / "native-output.parquet"
    rows = json.loads(path.read_text())
    if mutation == "messages":
        values = json.loads(rows[0]["messages"])
        values[0]["metadata"]["step_index"] = 99
        rows[0]["messages"] = json.dumps(values)
    elif mutation == "labels":
        rows[0]["cohort"] = "PUBLISHER-LABEL"
    elif mutation == "source":
        rows[0]["source_id"] = "private-run-0"
    else:
        rows.append(rows[0])
    path.write_text(json.dumps(rows))
    receipt_path = scenario.output / projection.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["output_parquet_sha256"] = projection._manifest(scenario.output, parquet_only=True)
    receipt["output_file_sha256"] = projection._manifest(scenario.output)
    receipt_path.write_text(json.dumps(receipt))
    original = snapshot(scenario.output)
    with pytest.raises(
        projection.ProjectionError, match="^projection_output_(metadata_)?mismatch$"
    ):
        scenario.prepare()
    assert snapshot(scenario.output) == original


@pytest.mark.parametrize("kind", ["unversioned", "old_version", "unbound", "empty"])
def test_old_or_unbound_outputs_are_never_claimed(fake_runtime, projection, kind):
    scenario = fake_runtime()
    scenario.output.mkdir()
    if kind != "empty":
        (scenario.output / "legacy.parquet").write_text(json.dumps([row("opaque-b")]))
    if kind in {"old_version", "unbound"}:
        (scenario.output / "projection.json").write_text(
            json.dumps(
                {
                    "projection_version": "native-actor-v1" if kind == "old_version" else VERSION,
                }
            )
        )
    original = snapshot(scenario.output)
    category = (
        "stale_projection_cache"
        if kind in {"unversioned", "old_version"}
        else "unbound_projection_cache"
    )
    with pytest.raises(projection.ProjectionError, match=f"^{category}$"):
        scenario.prepare()
    assert snapshot(scenario.output) == original
    assert scenario.adapter.insert_calls == []


@pytest.mark.parametrize("relative", ["same", "child", "parent", "symlink"])
def test_frozen_and_output_roots_cannot_overlap(fake_runtime, projection, relative):
    scenario = fake_runtime()
    if relative == "same":
        destination = scenario.frozen
    elif relative == "child":
        destination = scenario.frozen / VERSION
    elif relative == "parent":
        destination = scenario.frozen.parent
    else:
        destination = scenario.frozen.parent / "alias"
        destination.symlink_to(scenario.frozen, target_is_directory=True)
    original = snapshot(scenario.frozen)
    with pytest.raises(projection.ProjectionError, match="^projection_directories_overlap$"):
        scenario.prepare(destination=destination)
    assert snapshot(scenario.frozen) == original


@pytest.mark.parametrize(
    "keys",
    [
        [{"opaque_id": "x", "run_id": "r", "label": "do-not-accept"}],
        [{"opaque_id": "x"}],
        [{"opaque_id": "x", "run_id": None}],
        [{"opaque_id": "x", "run_id": "r"}, {"opaque_id": "x", "run_id": "s"}],
        [{"opaque_id": "x", "run_id": "r"}, {"opaque_id": "y", "run_id": "r"}],
    ],
)
def test_key_boundary_refuses_labels_missing_identity_or_duplicates(projection, tmp_path, keys):
    with pytest.raises(projection.ProjectionError, match="^(invalid|duplicate)_projection_keys$"):
        projection.prepare_projection(
            tmp_path / "db", keys, tmp_path / "old", tmp_path / "new", secret_safe=lambda _: True
        )
    assert not (tmp_path / "new").exists()


def test_native_sdk_full_typed_equality_and_blinded_metadata(projection, tmp_path):
    scout = pytest.importorskip("inspect_scout")
    pytest.importorskip("inspect_ai")
    pytest.importorskip("pyarrow")
    if version("inspect-scout") != "0.5.4":
        pytest.skip("fixture is pinned to inspect-scout==0.5.4")
    from evallab.readers import scout_clouddb as adapter

    database, frozen, output = (
        tmp_path / "source.sqlite",
        tmp_path / "old",
        tmp_path / f"new-{VERSION}",
    )
    run_id, opaque = "native-private-run-fixture", "opaque-native-fixture"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, model_id TEXT);
            CREATE TABLE trace_steps (
                trace_id TEXT, step_index INTEGER, role TEXT, tool_name TEXT,
                command_or_action TEXT, content_text TEXT, observation_preview TEXT, flags_json TEXT
            );
        """)
        connection.execute("INSERT INTO agent_traces VALUES (?, ?)", (run_id, "fixture-model"))
        for index, role, content, command, flags in (
            (0, "system", "native system fixture", None, {}),
            (1, "user", "native user fixture", None, {}),
            (
                2,
                "assistant",
                "native answer fixture",
                "printf fixture",
                {"reasoning_content": "native rationale fixture"},
            ),
            (3, "tool", "native observation fixture", None, {"tool_call_id": "call-2"}),
        ):
            connection.execute(
                "INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    index,
                    role,
                    "bash" if command else None,
                    command,
                    content,
                    None,
                    json.dumps(
                        {
                            **flags,
                            "label": "PUBLISHER-LABEL-NEVER-COPY",
                            "family": "FAMILY-NEVER-COPY",
                        }
                    ),
                ),
            )
        old = adapter.to_transcript(opaque, adapter.blind_messages(connection, run_id))
    frozen_native = old.model_copy(
        update={
            "metadata": {"blind_schema": BLIND_SCHEMA, "cohort": "COHORT-NEVER-COPY"},
            "source_id": run_id,
            "score": 1,
        }
    )

    async def write_old():
        async with scout.transcripts_db(str(frozen)) as db:
            await db.insert([frozen_native])

    asyncio.run(write_old())
    assert not (frozen / adapter.PROJECTION_MARKER).exists()
    original = snapshot(frozen)
    checked = []

    def safe(messages):
        checked.append(copy.deepcopy(messages))
        return True

    keys = [{"opaque_id": opaque, "run_id": run_id}]
    receipt = projection.prepare_projection(database, keys, frozen, output, secret_safe=safe)

    async def read_new():
        async with scout.transcripts_from(str(output)).reader() as reader:
            return [
                await reader.read(info, scout.TranscriptContent(messages="all", metadata=True))
                async for info in reader.index()
            ]

    actual = asyncio.run(read_new())
    assert len(actual) == 1
    assert projection._canonical_messages(actual[0].messages) == projection._canonical_messages(
        old.messages
    )
    assert checked == [json.loads(projection._canonical_messages(old.messages))]
    assert any(isinstance(item["content"], list) for item in checked[0])
    assert any(item.get("tool_calls") for item in checked[0])
    assert any(item.get("tool_call_id") for item in checked[0])
    assert actual[0].metadata == {"blind_schema": BLIND_SCHEMA, "projection_version": VERSION}
    assert actual[0].source_id is None and actual[0].score is None
    serialized = actual[0].model_dump_json() + json.dumps(receipt)
    for forbidden in (
        run_id,
        "PUBLISHER-LABEL-NEVER-COPY",
        "FAMILY-NEVER-COPY",
        "COHORT-NEVER-COPY",
    ):
        assert forbidden not in serialized
    assert receipt["eligible_opaque_ids"] == [opaque]
    assert receipt["rejected"] == []
    assert (
        receipt["message_hashes"][0]["source_messages_sha256"]
        == receipt["message_hashes"][0]["current_messages_sha256"]
    )
    assert snapshot(frozen) == original
    assert (
        projection.prepare_projection(database, keys, frozen, output, secret_safe=safe) == receipt
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trace_steps SET content_text = ? WHERE step_index = 3",
            ("changed native observation",),
        )
    retained_output = snapshot(output)
    with pytest.raises(projection.ProjectionError, match="^projection_input_drift$"):
        projection.prepare_projection(database, keys, frozen, output, secret_safe=safe)
    assert snapshot(output) == retained_output
    assert snapshot(frozen) == original


def test_native_decoder_retains_null_ids_and_full_defaults(projection):
    pytest.importorskip("inspect_ai")
    from inspect_ai.model import ChatMessageAssistant, ContentReasoning, ContentText
    from inspect_ai.tool import ToolCall

    message = ChatMessageAssistant(
        id="native-message-id",
        source="generate",
        model="fixture-model",
        metadata={"step_index": 8},
        content=[ContentReasoning(reasoning="fixture rationale"), ContentText(text="fixture body")],
        tool_calls=[
            ToolCall(id="native-call-id", function="bash", arguments={"command": "printf fixture"})
        ],
    )
    payload = projection._canonical_messages([message])
    assert projection._canonical_messages(projection._decode_native_messages(payload)) == payload
    missing_id = json.loads(payload)
    missing_id[0]["id"] = None
    decoded = projection._decode_native_messages(json.dumps(missing_id))
    assert decoded[0].id is None
    assert projection._canonical_messages(decoded) != payload
