"""Optional real Scout integration, with only synthetic local transcripts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_clouddb_blind import FORBIDDEN, OBSERVATION, REASONING
from test_clouddb_blind import corpus as corpus

from evallab.readers import ACTOR_PROJECTION_VERSION, scout_clouddb
from evallab.readers.clouddb_blind import (
    ActorHistoryUnavailable,
    blind_messages,
    opaque_id,
)
from evallab.readers.scout_clouddb import (
    BLIND_SCHEMA,
    DEFAULT_CHEATBENCH_TRANSCRIPTS,
    DEFAULT_DB,
    DEFAULT_KEYMAP,
    DEFAULT_RESULTS,
    DEFAULT_SCANS,
    DEFAULT_STATE,
    DEFAULT_TRANSCRIPTS,
    PROJECTION_MARKER,
    TRACE_TOOLS_STATE,
    StaleProjectionError,
    _check_transcripts,
    _insert,
    collect,
    read_keymap,
    require_current_results,
    scan,
    scout_paths,
    sync,
    to_transcript,
)

STALE = "native-actor-v1"
ALL_TRACES = ("trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4")


@pytest.fixture
def scout_runtime() -> Any:
    return pytest.importorskip("inspect_scout")


def _transcript_ids(runtime: Any, transcripts: Path) -> set[str]:
    async def ids() -> set[str]:
        async with runtime.transcripts_db(str(transcripts)) as db:
            return set(await db.transcript_ids())

    return asyncio.run(ids())


def _keymap_row(trace_id: str, **changes: object) -> dict[str, Any]:
    return {
        "opaque_id": opaque_id(trace_id), "run_id": trace_id, "source": "cloud-trace-db",
        "projection_version": ACTOR_PROJECTION_VERSION, **changes,
    }


def test_concrete_messages_keep_unbounded_reasoning_and_only_blind_metadata(
    corpus: tuple, scout_runtime: Any,
) -> None:
    _, _, con = corpus
    transcript = to_transcript(opaque_id("trace-goodfire"), blind_messages(con, "trace-goodfire"))
    assert transcript.metadata == {
        "blind_schema": BLIND_SCHEMA, "projection_version": ACTOR_PROJECTION_VERSION,
    }
    assert [message.metadata for message in transcript.messages] == [
        {"step_index": index} for index in (0, 1, 3, 4)
    ]
    assistant, tool = transcript.messages[2:]
    assert assistant.content[0].type == "reasoning"
    assert assistant.content[0].reasoning == REASONING
    assert assistant.content[1].text == "I will inspect."
    assert tool.content == OBSERVATION
    assert assistant.tool_calls[0].id == tool.tool_call_id == "call-native"
    assert assistant.tool_calls[0].arguments == {"command": "printf café"}
    dumped = transcript.model_dump(exclude_none=True)
    assert not ({"source_uri", "source_id", "model", "score", "success", "task_id", "task_set"} & dumped.keys())
    for forbidden in FORBIDDEN:
        assert forbidden not in json.dumps(dumped, ensure_ascii=False)


def test_incremental_sync_never_inserts_already_keyed_ids(
    corpus: tuple, tmp_path: Path, scout_runtime: Any,
) -> None:
    root, db_path, _ = corpus
    keymap = tmp_path / "private/keymap.jsonl"
    keymap.parent.mkdir()
    keymap.write_text(json.dumps(_keymap_row("trace-tb2")) + "\n", encoding="utf-8")
    transcripts = tmp_path / "transcripts"
    first = sync(db_path, transcripts, keymap, repo_root=root)
    assert (first["eligible"], first["inserted"], first["unchanged"], first["unavailable"]) == (
        4, 3, 1, 0,
    )
    assert first["projection_version"] == ACTOR_PROJECTION_VERSION
    assert json.loads((transcripts / PROJECTION_MARKER).read_text()) == {
        "projection_version": ACTOR_PROJECTION_VERSION,
    }
    before = keymap.read_bytes()
    second = sync(db_path, transcripts, keymap, repo_root=root)
    assert (second["eligible"], second["inserted"], second["unchanged"], second["unavailable"]) == (
        4, 0, 4, 0,
    )
    assert keymap.read_bytes() == before
    assert _transcript_ids(scout_runtime, transcripts) == {
        opaque_id(trace) for trace in ("trace-goodfire", "trace-tb4", "trace-mimo")
    }
    mappings = [json.loads(line) for line in keymap.read_text().splitlines()]
    assert len(mappings) == 4
    assert all(
        set(row) == {"opaque_id", "run_id", "source", "projection_version"} for row in mappings
    )
    assert all(row["projection_version"] == ACTOR_PROJECTION_VERSION for row in mappings)


def test_mixed_batch_quarantines_only_unavailable_actor_histories(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    root, db_path, _ = corpus
    real = scout_clouddb.blind_messages

    def partially_available(con: Any, trace_id: str) -> list[dict[str, Any]]:
        if trace_id == "trace-mimo":
            raise ActorHistoryUnavailable(trace_id, step_index=2, reason="no_actor_rows")
        return real(con, trace_id)

    monkeypatch.setattr(scout_clouddb, "blind_messages", partially_available)
    keymap, transcripts = tmp_path / "keymap.jsonl", tmp_path / "transcripts"
    report = sync(db_path, transcripts, keymap, repo_root=root)
    # Full admission stays visible; unavailable is neither clean nor inserted.
    assert (report["eligible"], report["inserted"], report["unchanged"], report["unavailable"]) == (
        4, 3, 0, 1,
    )
    assert report["eligible"] == (
        report["inserted"] + report["unchanged"] + report["unavailable"]
    )
    assert report["quarantined"] == [{
        "run_id": "trace-mimo", "opaque_id": opaque_id("trace-mimo"),
        "category": "actor_history_unavailable", "step_index": 2, "reason": "no_actor_rows",
    }]
    assert _transcript_ids(scout_runtime, transcripts) == {
        opaque_id(trace) for trace in ("trace-goodfire", "trace-tb2", "trace-tb4")
    }
    assert set(read_keymap(keymap)) == {
        opaque_id(trace) for trace in ("trace-goodfire", "trace-tb2", "trace-tb4")
    }

    # A quarantined run is retried, not poisoned: later actor history is inserted
    # with every prior transcript left exactly as it was.
    prior = keymap.read_bytes()
    monkeypatch.setattr(scout_clouddb, "blind_messages", real)
    retried = sync(db_path, transcripts, keymap, repo_root=root)
    assert (retried["eligible"], retried["inserted"], retried["unchanged"], retried["unavailable"]) == (
        4, 1, 3, 0,
    )
    assert keymap.read_bytes().startswith(prior)
    assert set(read_keymap(keymap)) == {opaque_id(trace) for trace in ALL_TRACES}
    assert _transcript_ids(scout_runtime, transcripts) == {
        opaque_id(trace) for trace in ALL_TRACES
    }


def test_all_unavailable_batch_keys_nothing_and_never_fakes_an_empty_transcript(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    root, db_path, _ = corpus

    def unavailable(con: Any, trace_id: str) -> list[dict[str, Any]]:
        raise ActorHistoryUnavailable(trace_id)

    monkeypatch.setattr(scout_clouddb, "blind_messages", unavailable)
    keymap, transcripts = tmp_path / "keymap.jsonl", tmp_path / "transcripts"
    report = sync(db_path, transcripts, keymap, repo_root=root)
    assert (report["eligible"], report["inserted"], report["unchanged"], report["unavailable"]) == (
        4, 0, 0, 4,
    )
    assert [row["run_id"] for row in report["quarantined"]] == list(ALL_TRACES)
    assert not keymap.exists()
    assert not list(transcripts.glob("*.parquet"))
    assert _transcript_ids(scout_runtime, transcripts) == set()


def test_cheatbench_sync_uses_assignment_dev_and_full_trial_ref(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    root, _, _ = corpus
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    keys = root / "research/answer-keys"
    items = [
        {"item_id": item_id, "split": "test", "expected_verdict": "fixture-publisher-label",
         "actions_only_ref": {"kind": "published", "path": "not-readable-actions"},
         "trial_ref": {"kind": "published", "path": f"published/full/{item_id}"}}
        for item_id in ("cb-dev", "cb-test", "cb-uppercase")
    ]
    (keys / "cheatbench-actions-v1/items.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in items), encoding="utf-8",
    )
    with (keys / "splits/assignments.csv").open("a", encoding="utf-8") as stream:
        stream.write("cheatbench-actions-v1,cb-dev,,dev\ncheatbench-actions-v1,cb-test,,test\n"
                     "cheatbench-actions-v1,cb-uppercase,,DEV\n")
    trajectory = Path.home() / "Developer/eval-lab-results/published/full/cb-dev/agent/trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(json.dumps({"metadata": {"label": "fixture-publisher-label"}, "steps": [
        {"step_id": 10, "source": "user", "message": "Full instruction."},
        {"step_id": 11, "source": "agent", "message": "Full answer.", "reasoning_content": REASONING},
    ]}), encoding="utf-8")
    keymap, transcripts = tmp_path / "cb-keymap.jsonl", tmp_path / "cb-transcripts"
    first = sync(tmp_path / "does-not-exist.sqlite", transcripts, keymap, repo_root=root, cheatbench=True)
    assert (first["source"], first["eligible"], first["inserted"], first["unavailable"]) == (
        "cheatbench-actions-v1", 1, 1, 0,
    )
    second = sync(tmp_path / "does-not-exist.sqlite", transcripts, keymap, repo_root=root, cheatbench=True)
    assert (second["inserted"], second["unchanged"]) == (0, 1)
    assert json.loads(keymap.read_text())["run_id"] == "cb-dev"


def _message(index: int, role: str, content: str, **values: object) -> dict:
    return {
        "step_index": index, "role": role, "content": content, "reasoning": None,
        "tool_calls": [], "tool_call_id": None, **values,
    }


def test_native_scan_collect_thresholds_labels_and_assistant_only_reference_mapping(
    tmp_path: Path, scout_runtime: Any,
) -> None:
    opaque = opaque_id("private-collection-run")
    repeated = "same <tool_call><function=bash>"
    messages = [
        _message(10, "system", "System text."),
        _message(20, "user", "Instruction text."),
        _message(31, "assistant", repeated, tool_calls=[{
            "id": "call-collection", "function": "bash_command", "arguments": {"command": "printf hello"},
        }]),
        _message(37, "tool", "Missing required fields\nMissing required fields", tool_call_id="call-collection"),
        _message(41, "assistant", repeated),
    ]
    transcripts = tmp_path / "transcripts"
    asyncio.run(_insert(transcripts, [to_transcript(opaque, messages)]))
    status = scan(transcripts, tmp_path / "scans")
    assert status.complete and not status.errors
    keymap = tmp_path / "private-keymap.jsonl"
    keymap.write_text(json.dumps({
        "opaque_id": opaque, "run_id": "private-collection-run", "source": "cloud-trace-db",
        "projection_version": ACTOR_PROJECTION_VERSION,
    }) + "\n", encoding="utf-8")
    results = tmp_path / "results.jsonl"
    # Prior evidence, including another tool's measurement, is never rewritten.
    prior = (json.dumps({"tool": "laminar", "run_id": "other-run", "value": 1}) + "\n").encode()
    results.write_bytes(prior)
    assert collect(status.location, keymap, results) == 9
    assert results.read_bytes().startswith(prior)
    rows = [json.loads(line) for line in results.read_text().splitlines()[1:]]
    by_detector = {row["detector"]: row for row in rows}
    loop = by_detector["repeated_assistant_message"]
    assert (loop["value"], loop["flag"], loop["evidence_steps"]) == (2, True, [31, 41])
    parse = by_detector["harness_parse_errors:missing_required_fields"]
    assert (parse["value"], parse["flag"], parse["evidence_steps"]) == (2, True, [37])
    assert by_detector["harness_parse_errors:no_valid_json"]["flag"] is False
    for label in ("tool_call_tag", "function_marker"):
        native = by_detector[f"native_tool_call_markup:{label}"]
        assert (native["value"], native["flag"], native["evidence_steps"]) == (2, True, [31, 41])
    restored = by_detector["restored_tool_calls:bash_command"]
    assert restored["value"] == 1 and restored["flag"] is None
    assert by_detector["restored_tool_calls:mark_task_complete"]["flag"] is None
    for row in rows:
        assert row["run_id"] == "private-collection-run" and row["scan"] == status.location
        assert row["schema"] == "evallab.trace_tool_result/v1"
        assert row["projection_version"] == ACTOR_PROJECTION_VERSION
        assert set(row) == {
            "schema", "run_id", "source", "projection_version", "tool", "detector",
            "detector_version", "value", "flag", "evidence_steps", "scan", "at",
        }


def test_scan_errors_are_abstentions_not_negative_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    from types import SimpleNamespace

    import pandas as pd

    opaque = opaque_id("failed-run")
    frame = pd.DataFrame([{
        "transcript_id": opaque, "label": None, "value": float("nan"),
        "scan_error": "synthetic read failure", "message_references": "[]",
        "transcript_metadata": json.dumps({"projection_version": ACTOR_PROJECTION_VERSION}),
    }])
    monkeypatch.setattr(scout_runtime, "scan_results_df", lambda *args, **kwargs: SimpleNamespace(
        scanners={"repeated_assistant_message": frame},
    ))
    keymap = tmp_path / "keymap.jsonl"
    keymap.write_text(json.dumps(_keymap_row("failed-run")), encoding="utf-8")
    results = tmp_path / "results.jsonl"
    assert collect("synthetic-scan", keymap, results) == 1
    record = json.loads(results.read_text())
    assert record["flag"] is None and record["value"] is None


def test_collect_refuses_scans_from_another_projection_without_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    from types import SimpleNamespace

    import pandas as pd

    def frame(**values: object) -> Any:
        return pd.DataFrame([{
            "transcript_id": opaque_id("scan-run"), "label": None, "value": 0,
            "scan_error": None, "message_references": "[]", **values,
        }])

    keymap = tmp_path / "keymap.jsonl"
    keymap.write_text(json.dumps(_keymap_row("scan-run")), encoding="utf-8")
    results = tmp_path / "results.jsonl"
    scans = {
        # A legacy scan without projection metadata, a scan of the previous
        # projection, and a current transcript that was never keyed here must all
        # be refused before anything is appended.
        "legacy": frame(transcript_metadata=json.dumps({"blind_schema": BLIND_SCHEMA})),
        "stale": frame(transcript_metadata=json.dumps({"projection_version": STALE})),
        "unkeyed": frame(
            transcript_id=opaque_id("unkeyed-run"),
            transcript_metadata=json.dumps({"projection_version": ACTOR_PROJECTION_VERSION}),
        ),
    }
    for name, scanned in scans.items():
        monkeypatch.setattr(
            scout_runtime, "scan_results_df",
            lambda *args, _frame=scanned, **kwargs: SimpleNamespace(
                scanners={"repeated_assistant_message": _frame},
            ),
        )
        with pytest.raises(StaleProjectionError, match="actor history|not a keyed"):
            collect(f"synthetic-{name}", keymap, results)
        assert not results.exists()


def test_rule_scanners_cannot_run_without_trial_metadata(
    tmp_path: Path, scout_runtime: Any,
) -> None:
    with pytest.raises(ValueError, match="deterministic scanners"):
        scan(tmp_path / "transcripts", tmp_path / "scans", ["rule_outcome"])


def test_scan_subset_excludes_old_runs_and_quotes_opaque_ids(tmp_path: Path, scout_runtime: Any) -> None:
    selected, other = "run-selected'quoted", "run-not-selected"
    transcripts = tmp_path / "transcripts"
    asyncio.run(_insert(transcripts, [
        to_transcript(selected, [_message(10, "assistant", "Single message.")]),
        to_transcript(other, [_message(20, "assistant", "Repeated."),
                              _message(21, "assistant", "Repeated.")]),
    ]))
    status = scan(transcripts, tmp_path / "scans", transcript_ids=[selected])
    assert status.complete and not status.errors
    results = scout_runtime.scan_results_df(status.location, rows="results")
    frame = results.scanners["repeated_assistant_message"]
    assert frame["transcript_id"].tolist() == [selected]
    assert frame["value"].tolist() == [0]


def test_scan_rejects_explicit_empty_subset(tmp_path: Path, scout_runtime: Any) -> None:
    with pytest.raises(ValueError, match="transcript_ids must not be empty"):
        scan(tmp_path / "transcripts", tmp_path / "scans", transcript_ids=[])


def test_legacy_keymap_is_refused_byte_for_byte(tmp_path: Path) -> None:
    legacy = tmp_path / "keymap-v1.jsonl"
    legacy.write_text(json.dumps({
        "opaque_id": opaque_id("trace-tb2"), "run_id": "trace-tb2", "source": "cloud-trace-db",
    }) + "\n", encoding="utf-8")
    before = legacy.read_bytes()
    with pytest.raises(StaleProjectionError, match="key-map"):
        read_keymap(legacy)
    assert legacy.read_bytes() == before

    previous = tmp_path / "scout-native-actor-v1.jsonl"
    previous.write_text(json.dumps(_keymap_row("trace-tb2", projection_version=STALE)) + "\n")
    before = previous.read_bytes()
    with pytest.raises(StaleProjectionError, match="key-map"):
        read_keymap(previous)
    assert previous.read_bytes() == before

    current = tmp_path / "current.jsonl"
    current.write_text(json.dumps(_keymap_row("trace-tb2")) + "\n", encoding="utf-8")
    assert set(read_keymap(current)) == {opaque_id("trace-tb2")}


def test_unversioned_results_are_refused_while_versioned_lineage_is_retained(
    tmp_path: Path,
) -> None:
    current_row = {
        "tool": "scout", "run_id": "current", "projection_version": ACTOR_PROJECTION_VERSION,
        "value": 0,
    }
    legacy_row = {"tool": "scout", "run_id": "legacy", "value": 0}
    previous_row = {"tool": "scout", "run_id": "previous", "projection_version": STALE, "value": 0}
    results = tmp_path / "results-v1.jsonl"
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in (current_row, legacy_row)),
        encoding="utf-8",
    )
    before = results.read_bytes()
    with pytest.raises(StaleProjectionError, match="unversioned Scout results"):
        require_current_results(results)
    assert results.read_bytes() == before

    # A measured row naming an earlier projection is retained lineage, not a
    # reason to rewrite the ledger; the completion ledger still ignores it.
    lineage = tmp_path / "lineage.jsonl"
    lineage.write_text(
        "".join(json.dumps(row) + "\n" for row in (
            {"tool": "laminar", "run_id": "x", "value": 1}, current_row, previous_row,
        )),
        encoding="utf-8",
    )
    require_current_results(lineage)
    assert json.loads(lineage.read_text().splitlines()[2])["projection_version"] == STALE


def test_unversioned_transcript_cache_is_refused_and_preserved(tmp_path: Path) -> None:
    legacy = tmp_path / "transcripts_clouddb_dev"
    legacy.mkdir()
    (legacy / "0000.parquet").write_bytes(b"legacy-bytes")
    with pytest.raises(StaleProjectionError, match="unversioned transcript cache"):
        _check_transcripts(legacy)
    assert (legacy / "0000.parquet").read_bytes() == b"legacy-bytes"
    assert not (legacy / PROJECTION_MARKER).exists()

    previous = tmp_path / "transcripts-native-actor-v1"
    previous.mkdir()
    (previous / PROJECTION_MARKER).write_text(json.dumps({"projection_version": STALE}))
    with pytest.raises(StaleProjectionError, match="transcript cache for projection"):
        _check_transcripts(previous, claim=True)
    assert json.loads((previous / PROJECTION_MARKER).read_text())["projection_version"] == STALE

    fresh = tmp_path / "transcripts-current"
    _check_transcripts(fresh, claim=True)
    assert json.loads((fresh / PROJECTION_MARKER).read_text()) == {
        "projection_version": ACTOR_PROJECTION_VERSION,
    }
    _check_transcripts(fresh)


def test_sync_refuses_a_stale_keymap_or_cache_before_writing(
    corpus: tuple, tmp_path: Path, scout_runtime: Any,
) -> None:
    root, db_path, _ = corpus
    legacy_keymap = tmp_path / "keymap-v1.jsonl"
    legacy_keymap.write_text(json.dumps({
        "opaque_id": opaque_id("trace-tb2"), "run_id": "trace-tb2", "source": "cloud-trace-db",
    }) + "\n", encoding="utf-8")
    before = legacy_keymap.read_bytes()
    transcripts = tmp_path / "transcripts"
    with pytest.raises(StaleProjectionError, match="key-map"):
        sync(db_path, transcripts, legacy_keymap, repo_root=root)
    assert legacy_keymap.read_bytes() == before
    assert not transcripts.exists()

    legacy_transcripts = tmp_path / "transcripts_clouddb_dev"
    legacy_transcripts.mkdir()
    (legacy_transcripts / "0000.parquet").write_bytes(b"legacy-bytes")
    keymap = tmp_path / "current-keymap.jsonl"
    with pytest.raises(StaleProjectionError, match="unversioned transcript cache"):
        sync(db_path, legacy_transcripts, keymap, repo_root=root)
    assert (legacy_transcripts / "0000.parquet").read_bytes() == b"legacy-bytes"
    assert not keymap.exists()


def test_default_paths_are_projection_versioned_and_never_alias_legacy_caches() -> None:
    for path in (
        DEFAULT_STATE, DEFAULT_KEYMAP, DEFAULT_RESULTS,
        DEFAULT_TRANSCRIPTS, DEFAULT_CHEATBENCH_TRANSCRIPTS, DEFAULT_SCANS,
    ):
        assert any(ACTOR_PROJECTION_VERSION in part for part in path.parts[-2:]), path
    # The shared database and the legacy evidence roots stay unversioned and unread.
    assert DEFAULT_DB.name == "cloud-trace-db.sqlite"
    assert DEFAULT_STATE.parent.parent / "cloud-trace-db.sqlite" == DEFAULT_DB
    assert DEFAULT_KEYMAP != TRACE_TOOLS_STATE / "keymap-v1.jsonl"
    assert DEFAULT_RESULTS != TRACE_TOOLS_STATE / "results-v1.jsonl"
    assert DEFAULT_TRANSCRIPTS.name != "transcripts_clouddb_dev"
    assert DEFAULT_CHEATBENCH_TRANSCRIPTS.name != "transcripts_cheatbench_dev_blind"
    assert DEFAULT_SCANS.name != "trace-tools"
    assert DEFAULT_KEYMAP.parent == DEFAULT_RESULTS.parent == DEFAULT_STATE

    custom = scout_paths(Path("/tmp/trace-tools"), Path("/tmp/viewer"), projection_version="unit-v9")
    assert custom.keymap == Path("/tmp/trace-tools/scout-unit-v9/keymap-v1.jsonl")
    assert custom.transcripts == Path("/tmp/viewer/data/transcripts_clouddb_dev-unit-v9")
    assert custom.scans == Path("/tmp/viewer/scans/trace-tools-unit-v9")
