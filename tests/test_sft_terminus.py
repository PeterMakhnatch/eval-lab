"""Behavioral checks for Terminus-2 trial export (evallab.sft_terminus).

Fixtures mirror the retained Harbor 0.21.0 Terminus-2 layout verified in
``harbor/agents/terminus_2/terminus_2.py``: ``result.json`` with verifier
reward, ``agent/trajectory.json`` (raw-content mode), optional
``trajectory.cont-N.json`` continuation segments with ``is_copied_context``
rewound history, and ``trajectory.summarization-*`` subagent files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.sft_split import DomainRoot, build_split, write_split
from evallab.sft_terminus import (
    CONVERSATIONS_FILE,
    SourceRoot,
    TraceError,
    export_conversations,
    write_export,
)
from evallab.sft_terminus import (
    main as cli_main,
)


def _freeze_split(tmp_path: Path, task_ids: list[str], *, heldout: list[str]) -> Path:
    """Seal a split manifest whose held-out set is exactly ``heldout``.

    Held-out membership is by hash rank, so the helper searches salts until
    the requested set is the sealed one (deterministic for fixed inputs).
    """
    dataset = tmp_path / "dataset" / "code"
    dataset.mkdir(parents=True)
    for task_id in task_ids:
        package = dataset / task_id
        package.mkdir()
        (package / "task.toml").write_text(f"[task]\nname = '{task_id}'\n")
    for salt_index in range(200):
        manifest = build_split(
            [DomainRoot(domain="code", path=dataset)],
            salt=f"test-salt-{salt_index}",
            source_dataset="FineEnvs/MiMo-V2.6-RL-harbor-code",
            source_revision="rev1",
            heldout_counts={"code": len(heldout)},
            heldout_fraction=None,
        )
        if set(manifest["heldout_task_ids"]) == set(heldout):
            break
    else:
        raise AssertionError(f"no salt found holding out {heldout}")
    path = tmp_path / "split.json"
    write_split(manifest, path)
    return path


def _terminus_steps(*, reasoning: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "step_id": 1,
            "source": "user",
            "message": "TASK: fix the bug. Terminal shows: $ ",
        },
        {
            "step_id": 2,
            "source": "agent",
            "message": '{"analysis":"inspect", "commands":[{"keystrokes":"ls"}]}',
            **({"reasoning_content": "I should list files first."} if reasoning else {}),
            "observation": {"results": [{"content": "file1\nfile2"}]},
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": '{"analysis":"done", "task_complete": true}',
            "observation": {"results": [{"content": "TASK COMPLETED confirmation"}]},
        },
    ]


def _continuation_steps() -> list[dict[str, Any]]:
    return [
        {
            "step_id": 1,
            "source": "user",
            "message": "TASK: fix the bug. Terminal shows: $ ",
            "is_copied_context": True,
        },
        {
            "step_id": 2,
            "source": "user",
            "message": "You are picking up work from a previous AI agent...",
            "is_copied_context": True,
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": "What state is the repo in?",
            "is_copied_context": True,
        },
        {"step_id": 4, "source": "user", "message": "Here are the answers... continue."},
        {
            "step_id": 5,
            "source": "agent",
            "message": '{"analysis":"resume", "commands":[{"keystrokes":"make test"}]}',
            "observation": {"results": [{"content": "all tests passed"}]},
        },
    ]


def _trajectory(steps: list[dict[str, Any]], *, session_id: str) -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.8",
        "session_id": session_id,
        "agent": {
            "name": "terminus-2",
            "version": "2.0.0",
            "model_name": "zai/glm-5.3",
        },
        "steps": steps,
    }


def _write_trial(
    root: Path,
    name: str,
    *,
    task_name: str = "mimo/task-a",
    reward: Any = 1.0,
    exception: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    agent_name: str = "terminus-2",
    continuations: list[list[dict[str, Any]]] | None = None,
    with_summarization: bool = False,
    session_id: str | None = None,
    main_trajectory: dict[str, Any] | None = None,
) -> Path:
    job = root / "job-x"
    trial = job / f"{name}__abc123"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    session = session_id or f"session-{name}"
    payload = main_trajectory or _trajectory(
        steps if steps is not None else _terminus_steps(), session_id=session
    )
    payload = {**payload, "agent": {**payload.get("agent", {}), "name": agent_name}}
    (agent / "trajectory.json").write_text(json.dumps(payload))
    for index, continuation_steps in enumerate(continuations or [], start=1):
        continuation = _trajectory(
            continuation_steps, session_id=f"{session}-cont-{index}"
        )
        continuation["agent"]["extra"] = {"continuation_index": index}
        (agent / f"trajectory.cont-{index}.json").write_text(json.dumps(continuation))
    if with_summarization:
        (agent / "trajectory.summarization-1-summary.json").write_text(
            json.dumps(_trajectory([{"step_id": 1, "source": "user", "message": "sum"}],
                                   session_id=f"{session}-summarization-1-summary"))
        )
    (trial / "trial.log").write_text("fixture\n")
    result: dict[str, Any] = {
        "task_name": task_name,
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward} if reward is not None else {}},
        "config": {"agent": {"name": agent_name}},
    }
    if exception is not None:
        result["exception_info"] = exception
    (trial / "result.json").write_text(json.dumps(result))
    return trial


def _export(
    tmp_path: Path,
    root: Path,
    split_path: Path,
    *,
    out_name: str = "out",
    **kwargs: Any,
) -> tuple[dict[str, Any], Path]:
    reward_threshold = kwargs.get("reward_threshold", 1.0)
    keep_reasoning = kwargs.get("keep_reasoning", False)
    source = SourceRoot(label="teacher", path=root)
    result = export_conversations(
        [source],
        split_manifest_path=split_path,
        reward_threshold=reward_threshold,
        keep_reasoning=keep_reasoning,
    )
    out = tmp_path / out_name
    manifest = write_export(
        result, out,
        roots=[source],
        split_manifest_path=split_path,
        reward_threshold=reward_threshold,
        keep_reasoning=keep_reasoning,
    )
    return manifest, out


def _rows(out: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (out / CONVERSATIONS_FILE).read_text().splitlines()
        if line.strip()
    ]


def test_export_matches_model_visible_conversation(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-ok", continuations=[_continuation_steps()], with_summarization=True)
    split = _freeze_split(tmp_path, ["task-a", "task-b"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    rows = _rows(out)
    assert len(rows) == 2
    main = rows[0]["messages"]
    # Exactly what the model saw: initial user prompt, assistant turns with
    # their terminal observations as user turns, terminal observation dropped.
    assert [message["role"] for message in main] == ["user", "assistant", "user", "assistant"]
    assert main[0]["content"].startswith("TASK: fix the bug.")
    assert main[2]["content"] == "file1\nfile2"
    assert all("TASK COMPLETED" not in message["content"] for message in main)
    # Reasoning dropped by default.
    assert all("list files first" not in message["content"] for message in main)

    continuation = rows[1]["messages"]
    assert [message["role"] for message in continuation] == [
        "user", "user", "assistant", "user", "assistant",
    ]
    assert continuation[3]["content"].startswith("Here are the answers")
    assert all("all tests passed" not in m["content"] for m in continuation)

    entry = manifest["conversations"][1]
    assert entry["segment"] == "continuation"
    assert entry["continuation_index"] == 1
    assert entry["copied_context_messages"] == 3
    assert manifest["counts"] == {
        "conversations": 2,
        "trials_selected": 1,
        "trials_excluded": 0,
        "duplicates": 0,
        "segments_main": 1,
        "segments_continuation": 1,
    }
    assert manifest["summarization_subagent_files"] == 1
    assert manifest["teacher_model"] == "zai/glm-5.3"
    assert manifest["reasoning_policy"] == "dropped"
    assert manifest["split_manifest"]["manifest_digest"] == json.loads(
        split.read_text()
    )["manifest_digest"]
    # Rewards and provenance live only in the manifest, never in the data.
    assert all(set(row) == {"messages"} for row in rows)
    assert all(set(message) == {"role", "content"} for row in rows for message in row["messages"])
    assert manifest["conversations"][0]["reward"] == 1.0
    assert manifest["conversations"][0]["character_count"] > 0
    assert manifest["conversations_sha256"].startswith("sha256:")


def test_heldout_task_refuses_whole_export(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-secret", task_name="mimo/task-held")
    _write_trial(root, "trial-ok")
    split = _freeze_split(tmp_path, ["task-a", "task-held"], heldout=["task-held"])
    with pytest.raises(TraceError, match="held-out task"):
        export_conversations(
            [SourceRoot(label="teacher", path=root)], split_manifest_path=split
        )
    # The CLI fails closed too, writing nothing.
    out = tmp_path / "refused"
    assert (
        cli_main(
            [
                "export",
                "--root", f"teacher={root}",
                "--split-manifest", str(split),
                "--out", str(out),
            ]
        )
        == 2
    )
    assert not out.exists() or not any(out.iterdir())


def test_reward_and_exception_filtering_counts_by_reason(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-failed-reward", task_name="mimo/task-a", reward=0.4)
    _write_trial(
        root,
        "trial-exception",
        task_name="mimo/task-b",
        reward=None,
        exception={"exception_type": "EnvironmentStartupError"},
    )
    _write_trial(root, "trial-unverified", task_name="mimo/task-c", reward=None)
    _write_trial(root, "trial-pass", task_name="mimo/task-d")
    split = _freeze_split(tmp_path, ["task-a", "task-b", "task-c", "task-d"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    assert manifest["exclusion_counts"] == {
        "reward_below_threshold": 1,
        "exception:EnvironmentStartupError": 1,
        "verifier_incomplete": 1,
    }
    assert manifest["counts"]["trials_selected"] == 1
    rows = _rows(out)
    assert len(rows) == 1
    assert manifest["conversations"][0]["task_id"] == "task-d"


def test_task_outside_sealed_split_is_excluded(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-unknown", task_name="mimo/task-elsewhere")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, _ = _export(tmp_path, root, split)
    assert manifest["exclusion_counts"] == {"task_not_in_split": 1}
    assert manifest["counts"]["conversations"] == 0


def test_parsed_tool_call_trajectory_is_excluded_not_reconstructed(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _terminus_steps()
    steps[1]["tool_calls"] = [
        {"tool_call_id": "call_0_1", "function_name": "bash_command",
         "arguments": {"keystrokes": "ls", "duration": 1}}
    ]
    steps[1]["message"] = "Analysis: inspect\nPlan: ls"
    _write_trial(root, "trial-parsed", steps=steps)
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, _ = _export(tmp_path, root, split)
    assert manifest["exclusion_counts"] == {"parsed_steps_not_raw_content": 1}


def test_reasoning_opt_in_keeps_think_prefix(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-think")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, out = _export(tmp_path, root, split, keep_reasoning=True, out_name="out-keep")
    assert manifest["reasoning_policy"] == "kept_as_think_prefix"
    rows = _rows(out)
    assistant = [m for m in rows[0]["messages"] if m["role"] == "assistant"]
    assert assistant[0]["content"].startswith("<think>\nI should list files first.\n</think>")
    assert '"analysis":"inspect"' in assistant[0]["content"]


def test_non_terminus_agent_is_excluded(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-mini", agent_name="mini-swe-agent")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, _ = _export(tmp_path, root, split)
    assert manifest["exclusion_counts"] == {"not_terminus_2": 1}


def test_duplicate_session_dedupes_across_copies(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    first = _write_trial(root, "trial-dup", session_id="sess-same")
    second_root = tmp_path / "runs-copy"
    second_root.mkdir()
    trial = _write_trial(second_root, "trial-dup", session_id="sess-same")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    result = export_conversations(
        [
            SourceRoot(label="a", path=root),
            SourceRoot(label="b", path=second_root),
        ],
        split_manifest_path=split,
    )
    duplicates = [d for d in result.dispositions if d.disposition == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0].duplicate_of == f"a:{first.relative_to(root).as_posix()}"
    assert result.selected_trials == 1
    assert len(result.conversations) == 1
    del trial, second_root


def test_export_is_deterministic(tmp_path: Path) -> None:
    root_a = tmp_path / "runs-a"
    root_b = tmp_path / "runs-b"
    root_a.mkdir()
    root_b.mkdir()
    _write_trial(root_a, "trial-1")
    _write_trial(root_b, "trial-1")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest_a, out_a = _export(tmp_path, root_a, split, out_name="det-a")
    manifest_b, out_b = _export(tmp_path, root_b, split, out_name="det-b")
    assert manifest_a["conversations_sha256"] == manifest_b["conversations_sha256"]
    assert (out_a / CONVERSATIONS_FILE).read_bytes() == (out_b / CONVERSATIONS_FILE).read_bytes()


def test_harness_tree_digest_binding_is_recorded_and_verified(tmp_path: Path) -> None:
    from evallab.evidence_store import evidence_tree_digest

    root = tmp_path / "runs"
    root.mkdir()
    trial = _write_trial(root, "trial-tree")
    job = trial.parent
    retained = job / "harness-tree" / "terminus"
    retained.mkdir(parents=True)
    (retained / "config.json").write_text('{"parser": "json"}\n')
    digest = evidence_tree_digest(job / "harness-tree")
    (job / "lab-metadata.json").write_text(
        json.dumps({"harness_tree": {"sha256": digest, "artifact_path": "harness-tree"}})
    )
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, _ = _export(tmp_path, root, split)
    assert manifest["harness_tree_digests"]["job-x"] == {"sha256": digest, "verified": True}

    # A recorded digest that no longer matches the retained bytes stays
    # unverified rather than silently relabeling the run.
    (retained / "config.json").write_text('{"parser": "xml"}\n')
    manifest2, _ = _export(tmp_path, root, split, out_name="out-drifted")
    recorded = manifest2["harness_tree_digests"]["job-x"]
    assert recorded["verified"] is False
    assert recorded["sha256"] == evidence_tree_digest(job / "harness-tree")
