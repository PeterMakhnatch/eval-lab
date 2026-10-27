"""Optional real Scout integration, with only synthetic local transcripts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_clouddb_blind import FORBIDDEN, OBSERVATION, REASONING
from test_clouddb_blind import corpus as corpus

from evallab.readers.clouddb_blind import blind_messages, opaque_id
from evallab.readers.scout_clouddb import (
    BLIND_SCHEMA,
    _insert,
    collect,
    scan,
    sync,
    to_transcript,
)


@pytest.fixture(autouse=True)
def scout_runtime() -> Any:
    return pytest.importorskip("inspect_scout")


def test_concrete_messages_keep_unbounded_reasoning_and_only_blind_metadata(corpus: tuple) -> None:
    _, _, con = corpus
    transcript = to_transcript(opaque_id("trace-goodfire"), blind_messages(con, "trace-goodfire"))
    assert transcript.metadata == {"blind_schema": BLIND_SCHEMA}
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
    prior = {"opaque_id": opaque_id("trace-tb2"), "run_id": "trace-tb2", "source": "cloud-trace-db"}
    keymap.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    transcripts = tmp_path / "transcripts"
    first = sync(db_path, transcripts, keymap, repo_root=root)
    assert (first["eligible"], first["inserted"], first["skipped"]) == (4, 3, 1)
    before = keymap.read_bytes()
    second = sync(db_path, transcripts, keymap, repo_root=root)
    assert (second["eligible"], second["inserted"], second["skipped"]) == (4, 0, 4)
    assert keymap.read_bytes() == before

    async def ids() -> set[str]:
        async with scout_runtime.transcripts_db(str(transcripts)) as db:
            return set(await db.transcript_ids())

    assert asyncio.run(ids()) == {opaque_id(trace) for trace in ("trace-goodfire", "trace-tb4", "trace-mimo")}
    mappings = [json.loads(line) for line in keymap.read_text().splitlines()]
    assert len(mappings) == 4
    assert all(set(row) == {"opaque_id", "run_id", "source"} for row in mappings)


def test_cheatbench_sync_uses_assignment_dev_and_full_trial_ref(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    assert (first["source"], first["eligible"], first["inserted"]) == ("cheatbench-actions-v1", 1, 1)
    second = sync(tmp_path / "does-not-exist.sqlite", transcripts, keymap, repo_root=root, cheatbench=True)
    assert second["inserted"] == 0
    assert json.loads(keymap.read_text())["run_id"] == "cb-dev"


def _message(index: int, role: str, content: str, **values: object) -> dict:
    return {
        "step_index": index, "role": role, "content": content, "reasoning": None,
        "tool_calls": [], "tool_call_id": None, **values,
    }


def test_native_scan_collect_thresholds_labels_and_assistant_only_reference_mapping(tmp_path: Path) -> None:
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
    }) + "\n", encoding="utf-8")
    results = tmp_path / "results.jsonl"
    assert collect(status.location, keymap, results) == 9
    rows = [json.loads(line) for line in results.read_text().splitlines()]
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
        assert set(row) == {
            "schema", "run_id", "source", "tool", "detector", "detector_version", "value",
            "flag", "evidence_steps", "scan", "at",
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
    }])
    monkeypatch.setattr(scout_runtime, "scan_results_df", lambda *args, **kwargs: SimpleNamespace(
        scanners={"repeated_assistant_message": frame},
    ))
    keymap = tmp_path / "keymap.jsonl"
    keymap.write_text(json.dumps({"opaque_id": opaque, "run_id": "failed-run", "source": "cloud-trace-db"}),
                      encoding="utf-8")
    results = tmp_path / "results.jsonl"
    assert collect("synthetic-scan", keymap, results) == 1
    record = json.loads(results.read_text())
    assert record["flag"] is None and record["value"] is None


def test_rule_scanners_cannot_run_without_trial_metadata(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deterministic scanners"):
        scan(tmp_path / "transcripts", tmp_path / "scans", ["rule_outcome"])
