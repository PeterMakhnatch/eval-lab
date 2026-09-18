"""Behavioral checks for the SFT record bridge (evallab.sft_records).

The mini-swe-agent shapes used here are protocol fixtures modelled on
harbor 0.21.0's mini adapter (``_parse_tool_calls`` /
``_add_observation_to_last_agent_step``): chat-completions runs encode shell
results as ``user`` messages, Responses-API runs as ``tool`` /
``function_call_output`` messages with ``call_id``. They are protocol-shaped,
never model evidence, and exist to pin the parser boundaries no retained mini
trial yet exercises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.sft_records import (
    FULL_RECORDS_FILE,
    MANIFEST_FILE,
    SourceRoot,
    export_records,
    write_export,
)
from evallab.sft_records import (
    main as cli_main,
)


def _atif_steps_tool_mode() -> list[dict[str, Any]]:
    """Responses-API shape: tool results carry source_call_id."""
    return [
        {"step_id": 1, "source": "system", "message": "You are mini."},
        {"step_id": 2, "source": "user", "message": "Fix the test."},
        {
            "step_id": 3,
            "source": "agent",
            "model_name": "zai-coding-plan/glm-5.3-flash",
            "message": "",
            "tool_calls": [
                {
                    "tool_call_id": "call-1",
                    "function_name": "bash",
                    "arguments": {"command": "pytest -x"},
                }
            ],
            "observation": {
                "results": [{"source_call_id": "call-1", "content": "1 failed, 12 passed"}]
            },
        },
        {
            "step_id": 4,
            "source": "agent",
            "model_name": "zai-coding-plan/glm-5.3-flash",
            "message": "Fixed. Final answer.",
        },
    ]


def _raw_mini_text_mode() -> list[dict[str, Any]]:
    """chat-completions shape: shell results are later ``user`` messages."""
    return [
        {"role": "system", "content": "You are mini."},
        {"role": "user", "content": "Fix the test."},
        {"role": "assistant", "content": "```bash\npytest -x\n```"},
        {"role": "user", "content": "1 failed, 12 passed"},
    ]


def _write_trial(
    root: Path,
    name: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    raw_mini: list[dict[str, Any]] | None = None,
    agent_name: str = "mini-swe-agent",
    task_name: str = "evallab/fixture-task",
    rewards: Any = None,
    redact: bool = False,
) -> Path:
    trial_dir = root / name
    (trial_dir / "agent").mkdir(parents=True)
    steps = steps if steps is not None else _atif_steps_tool_mode()
    if redact:
        marker = "<<evallab-redacted: 373 bytes, sha256:" + "0" * 64 + ">>"
        steps = [
            {**step, "message": marker} if step.get("source") in ("system", "user") else step
            for step in steps
        ]
    trajectory: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": f"session-{name}",
        "agent": {
            "name": agent_name,
            "version": "1.0",
            "model_name": "zai-coding-plan/glm-5.3-flash",
        },
        "steps": steps,
    }
    if redact:
        trajectory["evallab_redaction"] = {"rule": "R1"}
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    if raw_mini is not None:
        (trial_dir / "agent" / "mini-swe-agent.trajectory.json").write_text(
            json.dumps({"messages": raw_mini})
        )
    (trial_dir / "trial.log").write_text("fixture trial\n")
    rewards = {"reward": 1.0} if rewards is None else rewards
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": task_name,
                "task_checksum": "0" * 64,
                "verifier_result": {"rewards": rewards},
                "config": {"agent": {"name": agent_name}},
                "agent_info": {"name": agent_name},
            }
        )
    )
    return trial_dir


def _export(tmp_path: Path, roots: list[tuple[str, Path]], **kwargs: Any) -> dict[str, Any]:
    source_roots = [SourceRoot(label=label, path=path) for label, path in roots]
    result = export_records(
        source_roots,
        registry_dir=kwargs.get("registry_dir"),
        split_manifest_path=kwargs.get("split_manifest_path"),
    )
    out = tmp_path / f"out{len(list(tmp_path.iterdir()))}"
    return write_export(result, out, roots=source_roots) | {"_out": out, "_result": result}


def test_tool_mode_trial_exports_with_call_linked_observations(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a")
    manifest = _export(tmp_path, [("r", root)])
    assert manifest["counts"]["accepted"] == 1
    record = json.loads((manifest["_out"] / FULL_RECORDS_FILE).read_text().splitlines()[0])
    assert record["identity"]["model_name"] == "zai-coding-plan/glm-5.3-flash"
    assert record["fidelity"]["tool_linkage"] == "by_call_id"
    assert record["fidelity"]["system_prompt_captured"] is True
    observation = next(m for m in record["messages"] if m["role"] == "observation")
    assert observation["tool_call_id"] == "call-1"
    # No raw mini alongside: the presented role is an honest unknown.
    assert observation["presented_as"] == "unknown"
    assert record["fidelity"]["observation_role_known"] is False
    assert "observation_presented_role_unknown" in record["fidelity"]["limits"]


def test_raw_mini_messages_settle_observation_roles(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    # Text-mode mini: raw messages carry the history; the raw wire file must
    # recover presented_as=user for the shell result.
    _write_trial(root, "trial-raw", raw_mini=_raw_mini_text_mode())
    manifest = _export(tmp_path, [("r", root)])
    record = json.loads((manifest["_out"] / FULL_RECORDS_FILE).read_text().splitlines()[0])
    assert record["fidelity"]["origin"] == "raw_mini_messages"
    assert record["fidelity"]["observation_role_known"] is True
    observation = next(m for m in record["messages"] if m["role"] == "observation")
    assert observation["presented_as"] == "user"


def test_decision_examples_stay_causal(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a")
    manifest = _export(tmp_path, [("r", root)])
    lines = (manifest["_out"] / "records.decisions.jsonl").read_text().splitlines()
    examples = [json.loads(line) for line in lines]
    assert examples
    for example in examples:
        target = example["target"]
        assert target["role"] == "assistant"
        target_step = example["target_step_id"]
        assert all(m.get("step_id", 0) <= target_step for m in example["context"])
        # The observation produced by the supervised action is future: absent.
        assert not [
            m
            for m in example["context"]
            if m["role"] == "observation" and m.get("step_id") == target_step
        ]


def test_redacted_bundle_is_quarantined_not_exported(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-red", redact=True)
    manifest = _export(tmp_path, [("r", root)])
    assert manifest["counts"].get("accepted", 0) == 0
    assert manifest["counts"]["quarantined"] == 1
    assert manifest["reason_counts"] == {"redacted_model_visible_context": 1}
    quarantined = json.loads(
        (manifest["_out"] / "quarantine" / FULL_RECORDS_FILE).read_text().splitlines()[0]
    )
    assert quarantined["fidelity"]["redacted_messages"]


def test_same_session_across_roots_dedupes_preferring_unredacted(tmp_path: Path) -> None:
    redacted_root = tmp_path / "evidence"
    runtime_root = tmp_path / "runtime"
    redacted_root.mkdir()
    runtime_root.mkdir()
    trial_red = _write_trial(redacted_root, "trial-x", redact=True)
    trial_raw = _write_trial(runtime_root, "trial-x")
    # Same session id: two copies of one source.
    for path in (trial_red, trial_raw):
        traj_path = path / "agent" / "trajectory.json"
        traj = json.loads(traj_path.read_text())
        traj["session_id"] = "session-shared"
        traj_path.write_text(json.dumps(traj))
    manifest = _export(tmp_path, [("evidence", redacted_root), ("runtime", runtime_root)])
    assert manifest["counts"]["duplicate"] == 1
    # The unredacted runtime copy wins and exports; evidence copy is the dupe.
    assert manifest["counts"]["accepted"] == 1
    dupe = next(t for t in manifest["trials"] if t["disposition"] == "duplicate")
    assert dupe["root"] == "evidence"
    assert dupe["duplicate_of"] == "runtime:trial-x"


def test_registry_disallowed_task_is_rejected(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "fixture-task.json").write_text(
        json.dumps({"task_id": "fixture-task", "allowed_uses": ["measurement"]})
    )
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a", task_name="evallab/fixture-task")
    manifest = _export(tmp_path, [("r", root)], registry_dir=registry)
    assert manifest["counts"]["rejected"] == 1
    assert manifest["reason_counts"] == {"registry_disallows_training": 1}


def test_tb4_task_name_is_rejected_without_registry(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-tb", task_name="terminal-bench-4/fixture")
    manifest = _export(tmp_path, [("r", root)], registry_dir=None)
    assert manifest["counts"].get("accepted", 0) == 0
    assert manifest["counts"]["rejected"] == 1
    assert "terminal_bench_3_4_lineage" in manifest["reason_counts"]


def test_python_literal_arguments_are_reparsed_and_flagged(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _atif_steps_tool_mode()
    steps[2]["tool_calls"][0]["arguments"] = "{'command': 'pytest -x'}"
    _write_trial(root, "trial-lit", steps=steps)
    manifest = _export(tmp_path, [("r", root)])
    record = json.loads((manifest["_out"] / FULL_RECORDS_FILE).read_text().splitlines()[0])
    call = record["messages"][2]["tool_calls"][0]
    assert call["arguments"] == {"command": "pytest -x"}
    assert call["arguments_encoding"] == "python_literal"
    assert call["arguments_raw"] == "{'command': 'pytest -x'}"
    assert record["fidelity"]["reparsed_arguments"] == 1


def test_unmatched_tool_linkage_rejects(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _atif_steps_tool_mode()
    steps[2]["observation"]["results"][0]["source_call_id"] = "call-elsewhere"
    _write_trial(root, "trial-bad-link", steps=steps)
    manifest = _export(tmp_path, [("r", root)])
    assert manifest["counts"]["rejected"] == 1
    assert manifest["reason_counts"] == {"malformed_tool_linkage": 1}


def test_control_trials_do_not_collapse_into_one_identity(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    for name in ("nop-1", "nop-2"):
        trial = root / name
        (trial / "agent").mkdir(parents=True)
        (trial / "agent" / "oracle.txt").write_text("oracle control\n")
        (trial / "result.json").write_text(
            json.dumps({"config": {"agent": {"name": "nop"}}, "task_name": "evallab/fixture-task"})
        )
    manifest = _export(tmp_path, [("r", root)])
    # Two distinct rejections, not one shared-identity duplicate group.
    assert manifest["counts"]["rejected"] == 2
    assert manifest["counts"].get("duplicate", 0) == 0


def test_secret_in_context_quarantines(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    steps = _atif_steps_tool_mode()
    steps[1] = {"step_id": 2, "source": "user", "message": "key: sk-" + "a" * 30}
    _write_trial(root, "trial-secret", steps=steps)
    manifest = _export(tmp_path, [("r", root)])
    assert manifest["counts"]["quarantined"] == 1
    assert manifest["reason_counts"] == {"secret_pattern_in_context": 1}


def test_split_manifest_assigns_families(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a", task_name="evallab/fixture-family-seed42")
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"fixture-family": "selection"}))
    manifest = _export(tmp_path, [("r", root)], split_manifest_path=split)
    record = json.loads((manifest["_out"] / FULL_RECORDS_FILE).read_text().splitlines()[0])
    assert record["split"] == {
        "group": "fixture-family",
        "assignment": "selection",
        "source": "split.json",
    }


def test_cli_refuses_nonempty_output(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a")
    out = tmp_path / "out"
    out.mkdir()
    (out / "sentinel").write_text("x")
    assert (
        cli_main(["export", "--root", f"r={root}", "--out", str(out), "--registry", str(tmp_path)])
        == 2
    )


def test_manifest_and_contract_are_written_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_trial(root, "trial-a")
    first = _export(tmp_path, [("r", root)])
    again = tmp_path / "again"
    again.mkdir()
    second = _export(again, [("r", root)])
    assert (first["_out"] / MANIFEST_FILE).read_text() == (
        second["_out"] / MANIFEST_FILE
    ).read_text()
    contract = json.loads((first["_out"] / "contract.json").read_text())
    assert contract["version"] == "evallab.sft_records/1"
