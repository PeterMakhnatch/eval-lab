"""Behavioral checks for Terminus-2 trial export (evallab.sft_terminus).

Fixtures mirror the retained Harbor 0.21.0 Terminus-2 layout verified in
``harbor/agents/terminus_2/terminus_2.py``: ``result.json`` with verifier
reward, ``agent/trajectory.json`` (raw-content mode), optional
``trajectory.cont-N.json`` continuation segments with ``is_copied_context``
rewound history, and ``trajectory.summarization-*`` subagent files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.sft_split import CatalogTask, build_split, write_split
from evallab.sft_terminus import (
    CONVERSATIONS_FILE,
    SourceRoot,
    TraceError,
    export_conversations,
    load_selection,
    write_export,
)
from evallab.sft_terminus import (
    main as cli_main,
)


def _digest(seed: str) -> str:
    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


def _catalog_rows(
    task_ids: list[str], *, digest_of: dict[str, str] | None = None
) -> list[CatalogTask]:
    """Catalog rows with singleton groups, mirroring the real catalog."""
    return [
        CatalogTask(
            domain="code",
            task_id=task_id,
            task_name=f"mimo-v2.6-rl/{task_id}",
            split_group=f"code:{task_id}",
            task_version_digest=(digest_of or {}).get(task_id, _digest(f"code/{task_id}")),
            source_repo="FineEnvs/MiMo-V2.6-RL-harbor-code",
            source_revision="r" * 40,
        )
        for task_id in task_ids
    ]


def _freeze_manifest(
    rows: list[CatalogTask], *, heldout_count: int, salt: str = "test-salt"
) -> dict[str, Any]:
    return build_split(
        rows,
        salt=salt,
        catalog_table="test",
        catalog_digest=_digest("table"),
        catalog_rows=len(rows),
        heldout_counts={"code": heldout_count},
    )


def _freeze_split(tmp_path: Path, task_ids: list[str], *, heldout: list[str]) -> Path:
    """Seal a split manifest whose held-out set is exactly ``heldout``.

    Held-out membership is by hash rank, so the helper searches salts until
    the requested set is the sealed one (deterministic for fixed inputs).
    """
    rows = _catalog_rows(task_ids)
    for salt_index in range(200):
        manifest = _freeze_manifest(
            rows, heldout_count=len(heldout), salt=f"test-salt-{salt_index}"
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
    task_name: str = "mimo-v2.6-rl/task-a",
    reward: Any = 1.0,
    exception: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    agent_name: str = "terminus-2",
    continuations: list[list[dict[str, Any]]] | None = None,
    with_summarization: bool = False,
    session_id: str | None = None,
    main_trajectory: dict[str, Any] | None = None,
    summarization_count: int | None = None,
    continuation_sessions: list[str] | None = None,
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
        if continuation_sessions is not None:
            session = continuation_sessions[index - 1]
        else:
            session = f"{session}-cont-{index}"
        continuation = _trajectory(continuation_steps, session_id=session)
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
    if summarization_count is not None:
        result["agent_result"] = {"metadata": {"summarization_count": summarization_count}}
    (trial / "result.json").write_text(json.dumps(result))
    return trial


def test_unsplit_summarization_refuses_trial(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    unsplit = [
        {"step_id": 1, "source": "user", "message": "TASK: keep going."},
        {
            "step_id": 2,
            "source": "agent",
            "message": '{"analysis":"again", "commands":[{"keystrokes":"pwd"}]}',
            "observation": {"results": [{"content": "/app"}]},
        },
    ]
    _write_trial(
        root,
        "trial-unsplit",
        steps=_terminus_steps(reasoning=False),
        continuations=[unsplit],
        continuation_sessions=["session-trial-unsplit"],
        summarization_count=31,
    )
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    assert manifest["exclusion_counts"] == {"unsplit_summarization": 1}
    assert manifest["counts"]["conversations"] == 0
    (trial,) = manifest["trials"]
    assert trial["disposition"] == "excluded"
    assert trial["summarization_attempts"] == 31
    assert trial["summarization_splits"] == 0


def test_split_summarization_still_selected(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(
        root,
        "trial-split",
        continuations=[_continuation_steps()],
        summarization_count=1,
    )
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    assert manifest["counts"]["conversations"] == 2
    (trial,) = manifest["trials"]
    assert trial["disposition"] == "selected"
    assert trial["summarization_attempts"] == 1
    assert trial["summarization_splits"] == 1


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
    task_store_root = kwargs.get("task_store_root")
    source = SourceRoot(label="teacher", path=root)
    result = export_conversations(
        [source],
        split_manifest_path=split_path,
        reward_threshold=reward_threshold,
        keep_reasoning=keep_reasoning,
        task_store_root=task_store_root,
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
    assert (
        manifest["split_manifest"]["manifest_digest"]
        == json.loads(split.read_text())["manifest_digest"]
    )
    # Rewards and provenance live only in the manifest, never in the data.
    assert all(set(row) == {"messages"} for row in rows)
    assert all(set(message) == {"role", "content"} for row in rows for message in row["messages"])
    assert manifest["conversations"][0]["reward"] == 1.0
    assert manifest["conversations"][0]["character_count"] > 0
    assert manifest["conversations_sha256"].startswith("sha256:")


def test_heldout_task_refuses_whole_export(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-secret", task_name="mimo-v2.6-rl/task-held")
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
    _write_trial(root, "trial-failed-reward", task_name="mimo-v2.6-rl/task-a", reward=0.4)
    _write_trial(
        root,
        "trial-exception",
        task_name="mimo-v2.6-rl/task-b",
        reward=None,
        exception={"exception_type": "EnvironmentStartupError"},
    )
    _write_trial(root, "trial-unverified", task_name="mimo-v2.6-rl/task-c", reward=None)
    _write_trial(root, "trial-pass", task_name="mimo-v2.6-rl/task-d")
    # Graded after the agent timed out: scored, so a pass is exported.
    _write_trial(
        root,
        "trial-timeout-pass",
        task_name="mimo-v2.6-rl/task-e",
        exception={"exception_type": "AgentTimeoutError"},
    )
    split = _freeze_split(tmp_path, ["task-a", "task-b", "task-c", "task-d", "task-e"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    assert manifest["exclusion_counts"] == {
        "reward_below_threshold": 1,
        "exception:EnvironmentStartupError": 1,
        "verifier_incomplete": 1,
    }
    assert manifest["counts"]["trials_selected"] == 2
    rows = _rows(out)
    assert len(rows) == 2
    assert [c["task_id"] for c in manifest["conversations"]] == ["task-d", "task-e"]
    timed_out = next(t for t in manifest["trials"] if t["task_id"] == "task-e")
    assert timed_out["exception_type"] == "AgentTimeoutError"


def _curation(path: Path, flags: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema": "evallab.sft_curation/1", "flags": flags}))
    return path


def _selection(path: Path, trials: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema": "evallab.sft_selection/1", "trials": trials}))
    return path

def test_curation_flag_excludes_a_passing_trial_and_keeps_its_reward(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-tainted", task_name="mimo-v2.6-rl/task-a")
    _write_trial(root, "trial-clean", task_name="mimo-v2.6-rl/task-b")
    split = _freeze_split(tmp_path, ["task-a", "task-b"], heldout=[])
    decision = "decision on the card"
    curation = _curation(
        tmp_path / "curation.json",
        [
            {"job": "job-x", "trial": "trial-tainted__abc123", "flag": "pass_tainted",
             "source": decision},
            {"job": "job-gone", "trial": "t__1", "flag": "pass_tainted", "source": decision},
        ],
    )
    out = tmp_path / "out"
    assert cli_main(
        ["export", "--root", f"teacher={root}", "--split-manifest", str(split),
         "--out", str(out), "--curation", str(curation)]
    ) == 0

    manifest = json.loads((out / "manifest.json").read_text())
    tainted = next(t for t in manifest["trials"] if "trial-tainted" in t["trial"])
    assert tainted["disposition"] == "excluded"
    assert tainted["reasons"] == ["curation:pass_tainted"]
    assert tainted["reward"] == 1.0
    assert tainted["curation_flags"] == [{"flag": "pass_tainted", "source": decision}]
    assert [c["task_id"] for c in manifest["conversations"]] == ["task-b"]
    assert manifest["exclusion_counts"] == {"curation:pass_tainted": 1}
    assert [(u["job"], u["trial"]) for u in manifest["curation"]["unmatched"]] == [
        ("job-gone", "t__1")
    ]


def test_curation_flag_without_source_refuses_export(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    curation = _curation(
        tmp_path / "curation.json",
        [{"job": "job-x", "trial": "trial-a__abc123", "flag": "pass_tainted"}],
    )
    out = tmp_path / "out"
    assert cli_main(
        ["export", "--root", f"teacher={root}", "--split-manifest", str(split),
         "--out", str(out), "--curation", str(curation)]
    ) == 2
    assert not out.exists() or not any(out.iterdir())


_ASAN_REPORT = (
    "==42==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000011\n"
    "    #0 0x4c2f1a in ZSTD_decompressBlock /src/zstd/lib/decompress/zstd_decompress_block.c:1502\n"
    "    #1 0x4c1d2b in LLVMFuzzerTestOneInput /src/zstd/tests/fuzz/block_decompress.c:41\n"
    "DEDUP_TOKEN: ZSTD_decompressBlock--LLVMFuzzerTestOneInput--main\n"
    "SUMMARY: AddressSanitizer: heap-buffer-overflow\n"
)


@pytest.mark.parametrize(
    ("observation", "selected"),
    [
        (_ASAN_REPORT, True),
        (json.dumps(_ASAN_REPORT), True),
        (_ASAN_REPORT + "export API_TOKEN=abcd1234efgh5678ijkl\n", False),
        ("DEDUP_TOKEN=abcd1234efgh5678ijkl\n", False),
        ("MY_DEDUP_TOKEN: abcd1234efgh5678ijkl\n", False),
        ("dedup_token: abcd1234efgh5678ijkl\n", False),
    ],
    ids=[
        "asan-report",
        "asan-report-json-escaped",
        "asan-report-plus-real-token",
        "dedup-token-assignment",
        "prefixed-dedup-token",
        "lowercase-dedup-token",
    ],
)
def test_sanitizer_dedup_token_is_not_a_secret(
    tmp_path: Path, observation: str, selected: bool
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _terminus_steps(reasoning=False)
    steps[1]["observation"] = {"results": [{"content": observation}]}
    _write_trial(root, "trial-asan", steps=steps)
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, _ = _export(tmp_path, root, split)

    (trial,) = manifest["trials"]
    if selected:
        assert trial["disposition"] == "selected"
        assert manifest["counts"]["conversations"] == 1
    else:
        assert trial["disposition"] == "excluded"
        assert trial["reasons"] == ["secret_pattern_in_context"]


def _fallback_step(step_id: int) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": "Technical difficulties. Please continue with the task.",
        "observation": {"results": [{"content": "Previous response had parsing errors"}]},
    }


def test_harbor_fallback_reply_ends_the_segment(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _terminus_steps(reasoning=False)[:2] + [
        _fallback_step(3),
        {
            "step_id": 4,
            "source": "agent",
            "message": '{"analysis":"retry", "commands":[{"keystrokes":"pwd"}]}',
            "observation": {"results": [{"content": "/app"}]},
        },
        _fallback_step(5),
    ]
    fallback_only = [
        {"step_id": 1, "source": "user", "message": "You are picking up work..."},
        _fallback_step(2),
    ]
    _write_trial(root, "trial-fallback", steps=steps, continuations=[fallback_only])
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    rows = _rows(out)
    assert len(rows) == 1
    main = rows[0]["messages"]
    assert [m["role"] for m in main] == ["user", "assistant"]
    assert all("Technical difficulties" not in m["content"] for m in main)
    assert manifest["conversations"][0]["fallback_truncated_agent_steps"] == 3
    (trial,) = manifest["trials"]
    assert trial["disposition"] == "selected"
    assert trial["skipped_segments"] == {"trajectory.cont-1.json": "harbor_fallback_only"}


def test_continuation_repeating_an_exported_segment_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-dup", continuations=[_terminus_steps()])
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    manifest, out = _export(tmp_path, root, split)

    assert len(_rows(out)) == 1
    assert manifest["conversations"][0]["segment"] == "main"
    (trial,) = manifest["trials"]
    assert trial["skipped_segments"] == {"trajectory.cont-1.json": "duplicate_of:trajectory.json"}


def test_task_outside_sealed_split_is_excluded(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-unknown", task_name="mimo-v2.6-rl/task-elsewhere")
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


def _snapshot_task_dir(
    store: Path, repo: str, revision: str, task_id: str
) -> Path:
    from evallab.task_catalog import snapshot_dir_name

    org, _, repo_name = repo.partition("/")
    task_dir = store / "hf" / snapshot_dir_name(org, repo_name, revision) / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(f"[task]\nname = '{task_id}'\n")
    return task_dir


def test_store_digest_verified_match_selects(tmp_path: Path) -> None:
    from evallab.registry import task_directory_digest

    store = tmp_path / "store"
    revision = "r" * 40
    task_dir = _snapshot_task_dir(
        store, "FineEnvs/MiMo-V2.6-RL-harbor-code", revision, "task-a"
    )
    digest = task_directory_digest(task_dir)
    rows = _catalog_rows(["task-a"], digest_of={"task-a": digest})
    rows[0] = CatalogTask(
        domain=rows[0].domain, task_id=rows[0].task_id,
        task_name=rows[0].task_name, split_group=rows[0].split_group,
        task_version_digest=digest, source_repo=rows[0].source_repo,
        source_revision=revision,
    )
    manifest = _freeze_manifest(rows, heldout_count=0)
    split = tmp_path / "split.json"
    write_split(manifest, split)
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-ok")
    result = export_conversations(
        [SourceRoot(label="teacher", path=root)],
        split_manifest_path=split,
        task_store_root=store,
    )
    assert result.selected_trials == 1
    assert result.dispositions[0].task_version_digest == digest


def test_store_digest_drift_is_excluded(tmp_path: Path) -> None:
    from evallab.registry import task_directory_digest

    store = tmp_path / "store"
    revision = "r" * 40
    task_dir = _snapshot_task_dir(
        store, "FineEnvs/MiMo-V2.6-RL-harbor-code", revision, "task-a"
    )
    sealed_digest = task_directory_digest(task_dir)
    (task_dir / "planted.txt").write_text("drift\n")
    assert task_directory_digest(task_dir) != sealed_digest
    rows = _catalog_rows(["task-a"], digest_of={"task-a": sealed_digest})
    rows[0] = CatalogTask(
        domain=rows[0].domain, task_id=rows[0].task_id,
        task_name=rows[0].task_name, split_group=rows[0].split_group,
        task_version_digest=sealed_digest, source_repo=rows[0].source_repo,
        source_revision=revision,
    )
    manifest = _freeze_manifest(rows, heldout_count=0)
    split = tmp_path / "split.json"
    write_split(manifest, split)
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-drift")
    manifest_out, _ = _export(tmp_path, root, split, task_store_root=store)
    assert manifest_out["counts"]["trials_selected"] == 0
    assert manifest_out["exclusion_counts"] == {"task_version_drift": 1}


def test_selection_excludes_unlisted_as_not_selected(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-selected", task_name="mimo-v2.6-rl/task-a")
    _write_trial(root, "trial-unselected", task_name="mimo-v2.6-rl/task-b")
    split = _freeze_split(tmp_path, ["task-a", "task-b"], heldout=[])
    selection = _selection(
        tmp_path / "selection.json",
        [
            {
                "job": "job-x",
                "trial": "trial-selected__abc123",
                "source": "card-decision",
                "cut_step_id": None,
                "cut_file": None,
            },
            {
                "job": "job-phantom",
                "trial": "trial-phantom__xyz",
                "source": "card-decision",
                "cut_step_id": None,
                "cut_file": None,
            },
        ],
    )
    out = tmp_path / "out"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--selection",
                str(selection),
            ]
        )
        == 0
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["exclusion_counts"] == {"not_selected": 1}
    assert manifest["counts"]["trials_selected"] == 1
    assert [c["task_id"] for c in manifest["conversations"]] == ["task-a"]
    unselected = next(t for t in manifest["trials"] if "trial-unselected" in t["trial"])
    assert unselected["disposition"] == "excluded"
    assert unselected["reasons"] == ["not_selected"]
    assert manifest["selection"]["sha256"].startswith("sha256:")
    assert manifest["selection_unmatched"] == [
        {
            "job": "job-phantom",
            "trial": "trial-phantom__xyz",
            "source": "card-decision",
            "cut_step_id": None,
            "cut_file": None,
        }
    ]
    entry = manifest["conversations"][0]
    assert entry["cut_step_id"] is None
    assert entry["cut_file"] == "trajectory.json"


def test_selection_cut_truncates_steps_and_drops_later_segments(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(
        root,
        "trial-cut",
        continuations=[_continuation_steps()],
    )
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    selection = _selection(
        tmp_path / "selection.json",
        [
            {
                "job": "job-x",
                "trial": "trial-cut__abc123",
                "source": "cut-review",
                "cut_step_id": 2,
                "cut_file": "trajectory.json",
            }
        ],
    )
    out = tmp_path / "out"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--selection",
                str(selection),
            ]
        )
        == 0
    )
    rows = _rows(out)
    assert len(rows) == 1
    messages = rows[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert '{"analysis":"inspect", "commands":[{"keystrokes":"ls"}]}' in messages[1]["content"]
    assert all("file1\nfile2" not in m["content"] for m in messages)
    assert all("task_complete" not in m["content"] for m in messages)

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["conversations"] == 1
    (trial,) = manifest["trials"]
    assert trial["skipped_segments"] == {"trajectory.cont-1.json": "after_cut"}
    entry = manifest["conversations"][0]
    assert entry["cut_step_id"] == 2
    assert entry["cut_file"] == "trajectory.json"


def test_selection_bad_cut_step_or_file_refuses(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-ok")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])

    bad_step = _selection(
        tmp_path / "bad_step.json",
        [
            {
                "job": "job-x",
                "trial": "trial-ok__abc123",
                "source": "rev",
                "cut_step_id": 9999,
                "cut_file": "trajectory.json",
            }
        ],
    )
    out1 = tmp_path / "out1"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out1),
                "--selection",
                str(bad_step),
            ]
        )
        == 2
    )

    bad_file = _selection(
        tmp_path / "bad_file.json",
        [
            {
                "job": "job-x",
                "trial": "trial-ok__abc123",
                "source": "rev",
                "cut_step_id": 2,
                "cut_file": "nonexistent.json",
            }
        ],
    )
    out2 = tmp_path / "out2"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out2),
                "--selection",
                str(bad_file),
            ]
        )
        == 2
    )


def test_heldout_trial_refuses_even_if_not_selected(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-heldout", task_name="mimo-v2.6-rl/task-held")
    _write_trial(root, "trial-train", task_name="mimo-v2.6-rl/task-train")
    split = _freeze_split(tmp_path, ["task-train", "task-held"], heldout=["task-held"])
    selection = _selection(
        tmp_path / "selection.json",
        [
            {
                "job": "job-x",
                "trial": "trial-train__abc123",
                "source": "rev",
                "cut_step_id": None,
                "cut_file": None,
            }
        ],
    )
    out = tmp_path / "out"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--selection",
                str(selection),
            ]
        )
        == 2
    )
    assert not out.exists() or not any(out.iterdir())


def test_step_layers_raw_message_recovered_for_parsed_tool_calls(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps_with_layers = [
        {"step_id": 1, "source": "user", "message": "TASK: run tool."},
        {
            "step_id": 2,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "ls"}}
            ],
            "extra": {
                "step_layers": {
                    "schema": "evallab.step_layers/v1",
                    "provenance": "recorded",
                    "proposed": {
                        "message": '{"analysis": "listing", "command": "ls"}',
                        "reasoning": "Need to check directory contents.",
                    },
                }
            },
            "observation": {"results": [{"content": "fileA\nfileB"}]},
        },
    ]
    _write_trial(root, "trial-layers", steps=steps_with_layers)

    steps_without_layers = [
        {"step_id": 1, "source": "user", "message": "TASK: run tool."},
        {
            "step_id": 2,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {"tool_call_id": "c2", "function_name": "bash", "arguments": {"command": "pwd"}}
            ],
            "observation": {"results": [{"content": "/app"}]},
        },
    ]
    _write_trial(root, "trial-no-layers", steps=steps_without_layers)

    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    out = tmp_path / "out"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--keep-reasoning",
            ]
        )
        == 0
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["exclusion_counts"] == {"parsed_steps_not_raw_content": 1}
    assert manifest["counts"]["trials_selected"] == 1
    assert manifest["counts"]["conversations"] == 1
    rows = _rows(out)
    assert len(rows) == 1
    assistant = [m for m in rows[0]["messages"] if m["role"] == "assistant"]
    assert "<think>\nNeed to check directory contents.\n</think>" in assistant[0]["content"]
    assert '{"analysis": "listing", "command": "ls"}' in assistant[0]["content"]
    entry = manifest["conversations"][0]
    assert entry["step_layers_raw_messages"] == 1


def test_selection_validation_refuses_malformed(tmp_path: Path) -> None:
    # Wrong schema
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_text(json.dumps({"schema": "wrong/1", "trials": []}))
    with pytest.raises(TraceError, match="is not evallab.sft_selection/1"):
        load_selection(bad_schema)

    # Missing field
    missing_field = tmp_path / "missing_field.json"
    missing_field.write_text(
        json.dumps(
            {
                "schema": "evallab.sft_selection/1",
                "trials": [{"job": "j1", "trial": "t1"}],
            }
        )
    )
    with pytest.raises(TraceError, match="lacks source"):
        load_selection(missing_field)

    # Duplicate (job, trial)
    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps(
            {
                "schema": "evallab.sft_selection/1",
                "trials": [
                    {"job": "j1", "trial": "t1", "source": "s"},
                    {"job": "j1", "trial": "t1", "source": "s"},
                ],
            }
        )
    )
    with pytest.raises(TraceError, match="duplicate selection entry"):
        load_selection(dup)

    # Invalid cut_step_id type
    bad_cut = tmp_path / "bad_cut.json"
    bad_cut.write_text(
        json.dumps(
            {
                "schema": "evallab.sft_selection/1",
                "trials": [{"job": "j1", "trial": "t1", "source": "s", "cut_step_id": "two"}],
            }
        )
    )
    with pytest.raises(TraceError, match="cut_step_id must be int or null"):
        load_selection(bad_cut)


def test_per_turn_stride_and_final_turn_inclusion(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = [
        {"step_id": 1, "source": "user", "message": "TASK: multi-step work."},
        {
            "step_id": 2,
            "source": "agent",
            "message": '{"cmd":"step1"}',
            "reasoning_content": "think1",
            "metrics": {"prompt_tokens": 10, "completion_tokens": 5},
            "observation": {"results": [{"content": "out1"}]},
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": '{"cmd":"step2"}',
            "reasoning_content": "think2",
            "metrics": {"prompt_tokens": 20, "completion_tokens": 8},
            "observation": {"results": [{"content": "out2"}]},
        },
        {
            "step_id": 4,
            "source": "agent",
            "message": '{"cmd":"step3"}',
            "reasoning_content": "think3",
            "metrics": {"prompt_tokens": 30, "completion_tokens": 12},
            "observation": {"results": [{"content": "out3"}]},
        },
        {
            "step_id": 5,
            "source": "agent",
            "message": '{"cmd":"step4"}',
            "reasoning_content": "think4",
            "metrics": {"prompt_tokens": 40, "completion_tokens": 15},
        },
    ]
    _write_trial(root, "trial-turns", steps=steps)
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    out = tmp_path / "out-pt"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--per-turn-stride",
                "2",
            ]
        )
        == 0
    )
    # 4 assistant turns (0, 1, 2, 3), stride 2: kept turns are 1 and 3 (total 2 rows)
    raw_lines = [
        json.loads(line)
        for line in (out / CONVERSATIONS_FILE).read_text().splitlines()
        if line.strip()
    ]
    assert len(raw_lines) == 2
    assert all(row.get("loss") == "last" for row in raw_lines)

    # Row 0: target is assistant turn 1 (step 3)
    row0 = raw_lines[0]
    msgs0 = row0["messages"]
    # History: user (step 1), assistant turn 0 (step 2, raw, NO think), user obs (out1), target assistant (step 3, with think)
    assert [m["role"] for m in msgs0] == ["user", "assistant", "user", "assistant"]
    assert msgs0[1]["content"] == '{"cmd":"step1"}'
    assert "think1" not in msgs0[1]["content"]
    assert msgs0[2]["content"] == "out1"
    assert msgs0[3] == {
        "role": "assistant",
        "content": '{"cmd":"step2"}',
        "reasoning_content": "think2",
    }
    assert all("reasoning_content" not in m for m in msgs0[:3])

    # Row 1: target is assistant turn 3 (step 5)
    row1 = raw_lines[1]
    msgs1 = row1["messages"]
    # All prior assistant messages in history have raw content without think
    assts1 = [m for m in msgs1 if m["role"] == "assistant"]
    assert len(assts1) == 4
    assert assts1[0]["content"] == '{"cmd":"step1"}'
    assert assts1[1]["content"] == '{"cmd":"step2"}'
    assert assts1[2]["content"] == '{"cmd":"step3"}'
    assert assts1[3] == {
        "role": "assistant",
        "content": '{"cmd":"step4"}',
        "reasoning_content": "think4",
    }

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["per_turn"] == {"stride": 2, "rows": 2}
    assert manifest["conversations"][0]["turn_rows"] == [1, 3]
    assert len(manifest["rows"]) == 2
    r0 = manifest["rows"][0]
    assert r0["turn_index"] == 1
    assert r0["step_id_of_target"] == 3
    assert r0["prompt_tokens_recorded"] == 20
    assert r0["completion_tokens_recorded"] == 8
    r1 = manifest["rows"][1]
    assert r1["turn_index"] == 3
    assert r1["step_id_of_target"] == 5
    assert r1["prompt_tokens_recorded"] == 40
    assert r1["completion_tokens_recorded"] == 15


def test_per_turn_always_includes_final_turn_even_if_not_stride_boundary(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    # 3 assistant turns (0, 1, 2)
    steps = [
        {"step_id": 1, "source": "user", "message": "TASK: three turns."},
        {
            "step_id": 2,
            "source": "agent",
            "message": '{"cmd":"one"}',
            "reasoning_content": "think1",
            "observation": {"results": [{"content": "out1"}]},
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": '{"cmd":"two"}',
            "reasoning_content": "think2",
            "observation": {"results": [{"content": "out2"}]},
        },
        {
            "step_id": 4,
            "source": "agent",
            "message": '{"cmd":"three"}',
            # No reasoning_content -> the target carries an empty reasoning_content
        },
    ]
    _write_trial(root, "trial-three", steps=steps)
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    out = tmp_path / "out-three"
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out),
                "--per-turn-stride",
                "2",
            ]
        )
        == 0
    )
    # Stride 2 on 3 turns (0, 1, 2): kept are 1 (1 % 2 == 1) and 2 (final turn, 2 % 2 == 0)
    raw_lines = [
        json.loads(line)
        for line in (out / CONVERSATIONS_FILE).read_text().splitlines()
        if line.strip()
    ]
    assert len(raw_lines) == 2
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["conversations"][0]["turn_rows"] == [1, 2]
    # Final turn has empty reasoning: the target's reasoning_content is ""
    last_row_msgs = raw_lines[1]["messages"]
    assert last_row_msgs[-1]["content"] == '{"cmd":"three"}'
    assert last_row_msgs[-1]["reasoning_content"] == ""


def test_per_turn_mutual_exclusion_with_keep_reasoning(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-ex")
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])

    out1 = tmp_path / "out1"
    # Both --keep-reasoning and --per-turn-stride
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out1),
                "--keep-reasoning",
                "--per-turn-stride",
                "1",
            ]
        )
        == 2
    )

    with pytest.raises(TraceError, match="cannot specify both"):
        export_conversations(
            [SourceRoot(label="teacher", path=root)],
            split_manifest_path=split,
            keep_reasoning=True,
            per_turn_stride=1,
        )

    out2 = tmp_path / "out2"
    # Invalid stride < 1
    assert (
        cli_main(
            [
                "export",
                "--root",
                f"teacher={root}",
                "--split-manifest",
                str(split),
                "--out",
                str(out2),
                "--per-turn-stride",
                "0",
            ]
        )
        == 2
    )

    with pytest.raises(TraceError, match="stride must be >= 1"):
        export_conversations(
            [SourceRoot(label="teacher", path=root)],
            split_manifest_path=split,
            per_turn_stride=0,
        )


def _per_turn_export(tmp_path: Path, root: Path, stride: int = 1) -> Path:
    split = _freeze_split(tmp_path, ["task-a"], heldout=[])
    out = tmp_path / "out-turns"
    code = cli_main(
        [
            "export",
            "--root",
            f"teacher={root}",
            "--split-manifest",
            str(split),
            "--out",
            str(out),
            "--per-turn-stride",
            str(stride),
        ]
    )
    assert code == 0
    return out


def test_per_turn_never_targets_replayed_copied_context(tmp_path: Path) -> None:
    # cont-1 replays assistant step 3 (is_copied_context) before its fresh step 5.
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(
        root, "trial-split", continuations=[_continuation_steps()], summarization_count=1
    )
    out = _per_turn_export(tmp_path, root)
    targets = [row["messages"][-1]["content"] for row in _rows(out)]
    assert "What state is the repo in?" not in targets
    assert any('"analysis":"resume"' in target for target in targets)
    # The replayed turn stays in the continuation's history.
    resume = next(r for r in _rows(out) if '"analysis":"resume"' in r["messages"][-1]["content"])
    assert "What state is the repo in?" in [m["content"] for m in resume["messages"][:-1]]


def test_per_turn_excludes_a_trial_with_a_redaction_marker_in_reasoning(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    marker = "<<evallab-redacted: 12 bytes, sha256:" + "0" * 64 + ">>"
    steps = _terminus_steps()
    steps[1]["reasoning_content"] = f"use the key {marker}"
    _write_trial(root, "trial-redacted", steps=steps)
    out = _per_turn_export(tmp_path, root)
    manifest = json.loads((out / "manifest.json").read_text())
    assert _rows(out) == []
    assert manifest["counts"]["trials_excluded"] == 1
