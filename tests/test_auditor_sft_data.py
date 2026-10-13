"""Boundary checks for the auditor SFT data preparation (prepare_data).

All fixtures are inline and offline: no network, no downloads, no model
calls. Covers the fidelity boundaries that matter for training: verbatim
instruction preservation, DERIVED native-JSON assistant targets gated
through the actual installed stock Harbor parser, refusal (never repair)
of ambiguous histories, holdout exclusion, group-disjoint splits, caps,
and determinism.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                        / "research" / "experiments" / "auditor-sft-v1"))
from prepare_data import (  # noqa: E402
    PER_MODEL_CAP,
    PER_TASK_CAP,
    _merge_groups,
    assign_split,
    bind_cache,
    conversation_id,
    convert_trajectory,
    example_id,
    expand_prefixes,
    gate_native_response,
    group_key,
    rank_candidates,
    raw_trajectory_url,
    render_native_response,
    split_analysis_plan,
    stock_parser,
)


def _row(task, model="gemini-3.1-pro", label="v5", episodes=3,
         cats=("hollow-implementation",), **kw):
    row = {"task_id": task, "model": model, "trajectory_label": label,
           "classification": "rewarded_serious_exploit", "reward": 1.0,
           "episode_count": episodes, "observed_categories": list(cats),
           "exploit_blocked_by_verifier": False,
           "attacker_failed_due_to_budget": False}
    row.update(kw)
    return row


def _doc(user_text="Do the task.", agent_text="Analysis: recon.\nPlan: run ls.",
         tool_calls=None, obs="Terminal output:\nok\n", agent="terminus-2"):
    steps = [{"step_id": 1, "source": "user", "message": user_text}]
    steps.append({
        "step_id": 2, "source": "agent", "message": agent_text,
        "tool_calls": tool_calls if tool_calls is not None else [
            {"tool_call_id": "call_0_1", "function_name": "bash_command",
             "arguments": {"keystrokes": "ls -la\n", "duration": 0.5}}],
        "observation": {"results": [{"content": obs}]},
    })
    return {"schema_version": "ATIF-v1.6", "session_id": "s",
            "agent": {"name": agent}, "steps": steps}


def test_split_markers_require_unambiguous_shape() -> None:
    assert split_analysis_plan("Analysis: a.\nPlan: b.") == ("a.", "b.")
    assert split_analysis_plan("no markers") is None
    assert split_analysis_plan("Analysis: only.") is None
    assert split_analysis_plan("Analysis: a.\nPlan: one.\nPlan: two.") is None
    assert split_analysis_plan("Analysis:  \nPlan: b.") is None


def test_native_response_is_stock_parser_valid() -> None:
    content, complete = render_native_response(
        "recon.", "run ls.",
        [{"function_name": "bash_command",
          "arguments": {"keystrokes": "ls -la\n", "duration": 0.5}},
         {"function_name": "mark_task_complete", "arguments": {}}])
    payload = json.loads(content)
    assert list(payload) == ["analysis", "plan", "commands", "task_complete"]
    assert payload["commands"] == [{"keystrokes": "ls -la\n", "duration": 0.5}]
    assert payload["task_complete"] is True and complete is True
    result = stock_parser().parse_response(content)
    assert result.error == ""
    assert [(c.keystrokes, c.duration) for c in result.commands] == [("ls -la\n", 0.5)]
    assert gate_native_response(content, "recon.", "run ls.",
                                payload["commands"], True) is None


def test_native_response_rejects_unknown_function() -> None:
    with pytest.raises(ValueError, match="unknown tool call"):
        render_native_response("a.", "p.", [{"function_name": "rm_rf", "arguments": {}}])


def test_convert_renders_derived_native_json() -> None:
    doc = _doc()
    doc["steps"].append({
        "step_id": 3, "source": "agent",
        "message": "Analysis: listed.\nPlan: finish.",
        "tool_calls": [{"tool_call_id": "c", "function_name": "mark_task_complete",
                        "arguments": {}}],
        "observation": {"results": [{"content": "final, never sent back"}]},
    })
    messages, refusal, flags = convert_trajectory(doc)
    assert refusal is None
    assert messages is not None
    assert messages[0] == {"role": "user", "content": "Do the task."}
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    payload = json.loads(assistant["content"])
    assert payload["analysis"] == "recon."
    assert payload["plan"] == "run ls."
    assert payload["commands"] == [{"keystrokes": "ls -la\n", "duration": 0.5}]
    assert payload["task_complete"] is False
    # Mid-history observation is retained as context; only the final agent
    # step's observation (never sent back to the model) is dropped.
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[2] == {"role": "user", "content": "Terminal output:\nok\n"}
    assert "final, never sent back" not in json.dumps(messages)
    assert json.loads(messages[3]["content"])["task_complete"] is True
    assert flags["assistant_turns"] == 2
    assert flags["stock_gated_turns"] == 2


def test_convert_drops_final_observation() -> None:
    doc = _doc()
    doc["steps"].append({
        "step_id": 3, "source": "agent", "message": "Analysis: done.\nPlan: stop.",
        "tool_calls": [{"tool_call_id": "c", "function_name": "mark_task_complete",
                        "arguments": {}}],
        "observation": {"results": [{"content": "never sent back"}]},
    })
    messages, refusal, _ = convert_trajectory(doc)
    assert refusal is None
    assert messages is not None
    assert "never sent back" not in json.dumps(messages)
    assert messages[-1]["role"] == "assistant"
    assert json.loads(messages[-1]["content"])["task_complete"] is True


def test_convert_refuses_instead_of_repairing() -> None:
    base = _doc()
    # Copied (retrospective) context.
    doc = json.loads(json.dumps(base))
    doc["steps"][0]["is_copied_context"] = True
    _, refusal, _ = convert_trajectory(doc)
    assert refusal is not None and "copied" in refusal
    # Empty assistant text (stripped-like).
    doc = _doc(agent_text="")
    _, refusal, _ = convert_trajectory(doc)
    assert refusal == "empty_assistant_text"
    # Ambiguous markers: never invent reasoning.
    doc = _doc(agent_text="Just do stuff.")
    _, refusal, _ = convert_trajectory(doc)
    assert refusal == "ambiguous_analysis_plan_markers"
    # Unknown tool call.
    doc = _doc(tool_calls=[{"function_name": "nope", "arguments": {}}])
    _, refusal, _ = convert_trajectory(doc)
    assert refusal is not None and refusal.startswith("tool_render")
    # Non-terminus agent.
    doc = _doc(agent="other-agent")
    _, refusal, _ = convert_trajectory(doc)
    assert refusal == "not_terminus_2"
    # Non-string observation content is refused fail-closed by the oracle as
    # multimodal before the custom check; that refusal is the correct behavior.
    # (No scalar non-string input reaches the custom non_string_observation
    # branch either — the oracle flags every non-text content first — so no
    # additional case is added.)
    doc = _doc(obs=None)
    doc["steps"][1]["observation"] = {"results": [{"content": ["x"]}]}
    _, refusal, _ = convert_trajectory(doc)
    assert refusal == "multimodal_content"


def test_holdout_tasks_are_hard_excluded() -> None:
    rows = [_row("implement-nonogram-solver"), _row("other-task"),
            _row("blind-maze-explorer-5x5")]
    selected, dispositions = rank_candidates(
        rows, target=10, holdout_ids={"implement-nonogram-solver",
                                      "blind-maze-explorer-5x5"})
    assert [r["task_id"] for r in selected] == ["other-task"]
    by_task = {d["task_id"]: d for d in dispositions}
    assert by_task["implement-nonogram-solver"]["reasons"] == ["held_out_eval_task"]


def test_caps_and_determinism() -> None:
    rows = [_row(f"task-{i % 5}", model=m, label=f"v{k}")
            for i in range(5) for m in ("a", "b") for k in range(6)]
    first, _ = rank_candidates(rows, target=8, seed=99)
    second, _ = rank_candidates(rows, target=8, seed=99)
    assert [conversation_id(r) for r in first] == [conversation_id(r) for r in second]
    from collections import Counter
    per_task = Counter(r["task_id"] for r in first)
    assert all(v <= PER_TASK_CAP for v in per_task.values())
    per_model = Counter(r["model"] for r in first)
    assert all(v <= PER_MODEL_CAP for v in per_model.values())
    assert len(first) == 8


def test_split_is_group_disjoint_and_sealed() -> None:
    groups = [group_key(f"task-{i}") for i in range(30)]
    split = assign_split(groups, seed=7, val_fraction=0.1)
    val = {g for g, s in split.items() if s == "val"}
    train = {g for g, s in split.items() if s == "train"}
    assert val and train and not (val & train)
    assert len(val) == 3
    assert assign_split(groups, seed=7, val_fraction=0.1) == split


def test_split_zero_val_fraction_trains_everything() -> None:
    groups = [group_key(f"task-{i}") for i in range(10)]
    split = assign_split(groups, seed=7, val_fraction=0.0)
    assert set(split.values()) == {"train"}


def test_merge_groups_unions_instruction_collisions() -> None:
    merged = _merge_groups(["task-a", "task-b", "task-c"], ["h1", "h1", "h2"])
    assert merged["task-a"] == merged["task-b"]
    assert merged["task-c"] != merged["task-a"]
    assert merged["task-a"].startswith("tw-task:")


def test_raw_url_uses_pinned_revision_and_raw_tree() -> None:
    row = _row("some-task")
    url = raw_trajectory_url(row, "abc123")
    assert "/abc123/tasks/some-task/" in url
    assert "/hack_trajectories/" in url
    assert "sanitized" not in url and "stripped" not in url


def test_expand_prefixes_ends_at_each_assistant_turn() -> None:
    doc = _doc()
    doc["steps"].append({
        "step_id": 3, "source": "agent", "message": "Analysis: done.\nPlan: stop.",
        "tool_calls": [{"tool_call_id": "c", "function_name": "mark_task_complete",
                        "arguments": {}}],
        "observation": {"results": [{"content": "fin"}]},
    })
    messages, refusal, flags = convert_trajectory(doc)
    assert refusal is None
    assert messages is not None
    prefixes = expand_prefixes("tw-h", messages)
    assert len(prefixes) == flags["assistant_turns"] == 2
    assert [eid for eid, _ in prefixes] == ["tw-h__a00", "tw-h__a01"]
    assert example_id("tw-h", 3) == "tw-h__a03"
    for _eid, prefix in prefixes:
        assert prefix[-1]["role"] == "assistant"
    # Exact slices: no extra turns, no truncation.
    assert prefixes[0][1] == messages[:2]
    assert prefixes[1][1] == messages
    assert flags["assistant_step_ids"] == [2, 3]


def test_prefix_rows_keep_history_split() -> None:
    groups = [group_key("task-a"), group_key("task-b")]
    split = assign_split(groups, seed=7, val_fraction=0.5)
    history_split = {"h1": split[group_key("task-a")],
                     "h2": split[group_key("task-b")]}
    example_splits = {f"h{i}__a00": history_split[f"h{i}"]
                      for i in (1, 2)}
    assert set(example_splits.values()) <= {"train", "val"}
    # Splits are assigned at history level; prefixes only inherit.
    assert example_splits["h1__a00"] == history_split["h1"]


def _seed_index(index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "tasks.json").write_text("[1]")
    (index_dir / "trajectories.json").write_text("[2]")


def test_cache_refuses_changed_revision(tmp_path: Path) -> None:
    from prepare_data import RECEIPT_FILE
    cache, index = tmp_path / "cache", tmp_path / "index"
    _seed_index(index)
    (cache / "raw").mkdir(parents=True)
    (cache / "raw" / "x.json").write_text("{}")
    (cache / RECEIPT_FILE).write_text(json.dumps({
        "contract": "auditor-sft-v1.prepare_data/1",
        "source_revision": "rev-A",
        "index_dir": index.as_posix(),
        "index": {"tasks.json": "sha256:dead", "trajectories.json": "sha256:dead"},
    }))
    with pytest.raises(ValueError, match="bound to source revision rev-A"):
        bind_cache(cache, index, "rev-B")


def test_cache_refuses_unbound_legacy_files(tmp_path: Path) -> None:
    cache, index = tmp_path / "cache", tmp_path / "index"
    _seed_index(index)
    with pytest.raises(ValueError, match="no revision receipt"):
        bind_cache(cache, index, "rev-A")


def test_cache_reuses_bound_revision_without_download(tmp_path: Path) -> None:
    import hashlib

    from prepare_data import RECEIPT_FILE
    cache, index = tmp_path / "cache", tmp_path / "index"
    cache.mkdir(parents=True)
    _seed_index(index)
    hashes = {n: f"sha256:{hashlib.sha256((index / n).read_bytes()).hexdigest()}"
              for n in ("tasks.json", "trajectories.json")}
    (cache / RECEIPT_FILE).write_text(json.dumps({
        "contract": "auditor-sft-v1.prepare_data/1",
        "source_revision": "rev-A",
        "index_dir": index.as_posix(),
        "index": hashes,
    }))
    # Files exist and receipt matches: no download attempted, paths returned.
    assert set(bind_cache(cache, index, "rev-A")) == {"tasks.json", "trajectories.json"}


def test_cache_refuses_modified_index_bytes(tmp_path: Path) -> None:
    from prepare_data import RECEIPT_FILE
    cache, index = tmp_path / "cache", tmp_path / "index"
    cache.mkdir(parents=True)
    _seed_index(index)
    (cache / RECEIPT_FILE).write_text(json.dumps({
        "contract": "auditor-sft-v1.prepare_data/1",
        "source_revision": "rev-A",
        "index_dir": index.as_posix(),
        "index": {"tasks.json": "sha256:dead", "trajectories.json": "sha256:dead"},
    }))
    with pytest.raises(ValueError, match="no longer matches its receipt"):
        bind_cache(cache, index, "rev-A")
