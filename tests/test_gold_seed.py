from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.failure_library.bank import BankContext, IssueBank, Provenance, new_item
from evallab.failure_library.bank.matrix import Cell, Finding, Identity, decide, summarize
from evallab.gold import load_rows, load_trajectory
from evallab.gold.projection import _extract_channels, cloud_events, project_events
from evallab.gold.seed import _impossible_map
from evallab.trace_anatomy import RefusedRun
from evallab.trajectory_ir import SourceEventRef, build_trajectory_ir


def _call(call_id="shared", command="first"):
    return {"tool_call_id": call_id, "function_name": "bash", "arguments": {"command": command}}


def _events():
    return [
        {"id": "root", "parent": None, "message": {"role": "system", "content": "task"}},
        {"id": "left", "parent": "root", "step_id": 0, "final": False,
         "message": {"role": "assistant", "tool_calls": [_call(command="left")]}},
        {"id": "right", "parent": "root", "step_id": 1, "final": True,
         "message": {"role": "assistant", "tool_calls": [_call(command="right")]}},
        {"id": "left-result", "parent": "left", "final": False,
         "message": {"role": "tool", "tool_call_id": "shared", "content": "left output"}},
        {"id": "right-result", "parent": "right", "final": True,
         "message": {"role": "tool", "tool_call_id": "shared", "content": "right output"}},
    ]


def test_zero_id_and_branch_local_result_folding():
    events = _events()
    events[0]["step_id"] = 10
    ir = build_trajectory_ir(project_events(events))
    assert [step.step_id for step in ir.steps] == [10, 0, 1]
    left, right = ir.steps[1:]
    assert left.tool_calls[0].arguments == {"command": "left"}
    assert right.tool_calls[0].arguments == {"command": "right"}
    assert left.observation_results[0].content == "left output"
    assert right.observation_results[0].content == "right output"
    refs = {ref.source_event_id: ref for ref in ir.source_event_map}
    assert refs["left-result"].normalized_step_id == 0
    assert refs["right-result"].normalized_step_id == 1
    assert refs["left-result"].branch_id != refs["right-result"].branch_id
    assert refs["left"].on_final_parent_path is False
    assert refs["right"].on_final_parent_path is True
    assert refs["left-result"].parent_event_id == "left"
    assert refs["left-result"].resolution == "result-to-call"


def test_explicit_zero_does_not_collide_with_one_and_duplicates_fail():
    assert [s.step_id for s in build_trajectory_ir({"steps": [
        {"step_id": 0}, {"step_id": 1},
    ]}).steps] == [0, 1]
    with pytest.raises(ValueError, match="Duplicate normalized"):
        build_trajectory_ir({"steps": [{"step_id": 0}, {"step_id": 0}]})


def test_result_cannot_fold_into_a_sibling_call():
    events = _events()[:3]
    events[0]["step_id"] = 10
    events.append({"id": "result", "parent": "root", "message": {
        "role": "tool", "tool_call_id": "shared", "content": "orphan",
    }})
    ir = build_trajectory_ir(project_events(events))
    assert not ir.steps[1].observation_results
    assert not ir.steps[2].observation_results
    assert ir.source_event_map[-1].resolution == "unmatched-result"


def _item(state, **fields):
    return new_item(
        source="goodfire-action-v1", subject_id=state, subject_type="run", kind="cheat",
        label_state=state, polarity={"executed-positive": "positive", "reviewed-control": "negative"}.get(state),
        label_type="consensus", label=state, granularity="run", declared_split="dev", split="DEV",
        provenance=Provenance(store="test", ref=state, digest="a" * 64), **fields,
    )


@pytest.mark.parametrize("state", ["attempted", "unjudged"])
def test_null_states_never_become_controls(state):
    item = _item(state)
    assert item.polarity is None
    restored = type(item).model_validate_json(item.model_dump_json())
    assert restored.label_state == state and restored.polarity is None
    with pytest.raises(ValidationError):
        type(item).model_validate({**item.model_dump(), "polarity": "negative"})
    for verdict in ("alarm", "clear"):
        finding = Finding(Identity("scout", "v1"), item.subject_id, verdict, "cheat")
        cell, reason, selected = decide(item, [finding])
        assert cell == "unknown"
        summary = summarize([Cell(item, "scout@v1", "run", cell, reason, selected)])
        assert all(row["n_neg"] == 0 and row["n_pos"] == 0 for row in summary)
        assert all(row["unjudged"] == 1 and row["far"] is None for row in summary)


class _Gate:
    sealed = False

    def has_assignment_authority(self):
        return True

    def sealed_identity(self, **_):
        return "sealed" if self.sealed else None

    def assigned_test_anywhere(self, **_):
        return False


def test_bank_loader_round_trip_marks_only_positive_actions(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(BankContext, "registry", property(lambda self: gate))
    events = _events()
    events[0]["step_id"] = 10
    doc = project_events(events)
    path = tmp_path / "actor.json"
    path.write_text(json.dumps(doc))
    ref = build_trajectory_ir(doc).source_event_map[3]
    positive = _item("executed-positive", step_locators=(ref,), trajectory_available=True,
                     trajectory_ref=str(path), trajectory_digest=hashlib.sha256(path.read_bytes()).hexdigest())
    control = _item("reviewed-control", trajectory_available=True,
                    trajectory_ref=str(path), trajectory_digest=positive.trajectory_digest)
    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    bank.items_root.mkdir(parents=True)
    bank.items_path(positive.source).write_text("\n".join(
        row.model_dump_json() for row in (positive, control, _item("attempted"), _item("unjudged"))
    ))
    rows = load_rows(tmp_path, tmp_path / "state")
    assert [r.label_state for r in rows] == ["executed-positive", "reviewed-control", "attempted", "unjudged"]
    ir = load_trajectory(rows[0], repo_root=tmp_path)
    assert {step.step_id for step in ir.steps if step.is_cheating} == {0}
    assert ir.source_event_map == tuple(SourceEventRef(**entry) for entry in doc["source_event_map"])
    assert not any(step.is_cheating for step in load_trajectory(rows[1], repo_root=tmp_path).steps)
    gate.sealed = True
    assert all(row.split == "TEST" for row in load_rows(tmp_path, tmp_path / "state"))
    with pytest.raises(RefusedRun):
        load_trajectory(rows[0], repo_root=tmp_path)
    gate.sealed = False
    path.write_text("{}")
    with pytest.raises(ValueError, match="hash differs"):
        load_trajectory(rows[0], repo_root=tmp_path)


def test_cloud_native_call_ids_survive_result_folding():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE trace_steps (trace_id,step_index,role,tool_name,command_or_action,"
               "content_text,observation_preview,flags_json)")
    for index, role, parent, native_calls, result_id in [
        (0, "assistant", None, [{"id": "call-a", "name": "bash", "arguments": "{\"command\":\"pwd\"}"}], None),
        (1, "tool", 0, [], "call-a"),
    ]:
        db.execute("INSERT INTO trace_steps VALUES (?,?,?,?,?,?,?,?)", (
            "run", index, role, "bash", '{"command":"pwd"}', "ok", None,
            json.dumps({"native_parent": parent, "tool_calls": native_calls, "tool_call_id": result_id,
                        "on_final_parent_path": True}),
        ))
    ir = build_trajectory_ir(cloud_events(db, "run"))
    assert ir.steps[0].step_id == 0
    assert ir.steps[0].tool_calls[0].tool_call_id == "call-a"
    assert ir.steps[0].observation_results[0].source_call_id == "call-a"
    assert ir.source_event_map[1].normalized_step_id == 0
    db.close()


def test_impossible_file_line_maps_to_prepared_zero_action(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"id": "message-a", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "call-a", "name": "bash", "arguments": {"command": "pwd"}},
    ]}}) + "\n" + json.dumps({"id": "result-a", "parentId": "message-a", "message": {
        "role": "toolResult", "toolCallId": "call-a", "content": [],
    }}) + "\n")
    digest = hashlib.sha256(session.read_bytes()).hexdigest()
    receipt = {"session_files": [{"path": session.name, "sha256": digest}]}
    view = {"steps": [{"i": 0, "text": "", "tool_calls": [
        {"name": "bash", "args": '{"command":"pwd"}'},
    ], "observation": "ok"}]}
    ir = build_trajectory_ir(_impossible_map(view, tmp_path, receipt, {str(session): digest}))
    assert [(ref.source_line, ref.normalized_step_id) for ref in ir.source_event_map] == [(1, 0), (2, 0)]
    assert ir.source_event_map[0].source_sha256 == digest
    assert asdict(ir.source_event_map[1])["call_id"] == "call-a"
    view["steps"][0]["tool_calls"][0]["args"] = '{"command":"wrong"}'
    with pytest.raises(ValueError, match="differs from source"):
        _impossible_map(view, tmp_path, receipt, {str(session): digest})


def test_extract_channels_splits_narration_call_output_and_caps() -> None:
    """Synthetic with all three kinds; asserts split + full char totals (B2b req).
    Verifies cap marker, no labels.
    """
    asst_row = {
        "step_index": 0,
        "role": "assistant",
        "content_text": "I will edit the file now.",
        "observation_preview": "",
        "command_or_action": {"cmd": "sed -i 's/foo/bar/' f.py"},
        "flags_json": '{"reasoning_content": "Plan: use search replace to cheat."}',
    }
    tool_row = {
        "step_index": 1,
        "role": "tool",
        "content_text": "file edited, test fails (no real run).",
        "observation_preview": "",
        "command_or_action": "",
        "flags_json": "{}",
    }
    long_asst = {
        **asst_row,
        "step_index": 2,
        "content_text": "x" * 5000,
        "command_or_action": "",
        "flags_json": "{}",
    }

    chans0 = _extract_channels(asst_row)
    chans1 = _extract_channels(tool_row)
    chans2 = _extract_channels(long_asst)

    all_ch = chans0 + chans1 + chans2
    kinds = [c["k"] for c in all_ch]
    assert "narration" in kinds and "call" in kinds and "output" in kinds
    assert kinds.count("narration") == 2
    assert kinds.count("call") == 1
    assert kinds.count("output") == 1

    nar_total = sum(c["l"] for c in all_ch if c["k"] == "narration")
    call_total = sum(c["l"] for c in all_ch if c["k"] == "call")
    out_total = sum(c["l"] for c in all_ch if c["k"] == "output")
    assert nar_total > 5000 and call_total > 10 and out_total > 10

    long_ch = [c for c in chans2 if c["k"] == "narration"][0]
    assert "truncated" in long_ch["t"] and len(long_ch["t"]) < 4100
    assert long_ch["l"] == 5000

    for c in all_ch:
        assert "is_exploit" not in str(c) and "label" not in str(c).lower()
    assert nar_total + call_total + out_total == sum(c["l"] for c in all_ch)


def test_channel_projection_reads_sqlite_columns_not_row_values() -> None:
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT 7 AS step_index, 'assistant' AS role, 'answer' AS content_text,"
            " 'echo ok' AS command_or_action, '{}' AS flags_json"
        ).fetchone()
    assert _extract_channels(row) == [
        {"i": 7, "k": "narration", "l": 6, "t": "answer"},
        {"i": 7, "k": "call", "l": 7, "t": "echo ok"},
    ]


@pytest.mark.parametrize("source", ["assistant-axis", "metr-malt"])
def test_excluded_source_refuses_rows_before_any_bank_read(tmp_path, monkeypatch, source):
    import evallab.gold as gold
    from evallab.gold.exclusions import source_exclusion

    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    bank.items_root.mkdir(parents=True)
    bank.items_path("goodfire-action-v1").write_text("not JSON: must not be read\n")
    bank.items_path(source).write_text("not JSON: must not be read\n")
    monkeypatch.setattr(gold, "SOURCES", ("goodfire-action-v1", source))
    exclusion = source_exclusion(source)
    with pytest.raises(ValueError) as refused:
        load_rows(tmp_path, tmp_path / "state")
    assert str(refused.value) == f"Excluded gold source {exclusion['name']}: {exclusion['reason']}"


@pytest.mark.parametrize("source,store", [
    ("assistant-axis", "test"),
    ("library-runs", "failure-library:runs/metr-malt"),
])
@pytest.mark.parametrize("state", [None, "reviewed-control"])
def test_excluded_trajectory_refuses_legacy_and_gold_rows_before_access(tmp_path, source, store, state):
    row = new_item(
        source=source, subject_id="legacy", subject_type="run", kind="cheat",
        polarity="negative", label_type="judge", label="native-negative", granularity="run",
        declared_split="dev", split="TEST", provenance=Provenance(store=store, ref="legacy"),
        trajectory_available=True, trajectory_ref=str(tmp_path / "must-not-read.json"),
        trajectory_digest="a" * 64,
    )
    # A stale in-memory row can predate explicit Gold-state validation.
    row = row.model_copy(update={"label_state": state})
    exclusion = row.gold_exclusion()
    with pytest.raises(ValueError) as refused:
        load_trajectory(row, repo_root=tmp_path)
    assert str(refused.value) == f"Excluded gold source {exclusion['name']}: {exclusion['reason']}"
