"""Offline correction inventory, source-free feedback and native-target custody."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "research/experiments/test-repair-performance-20261017"
_spec = importlib.util.spec_from_file_location("envcheck_partition_test", SCRIPTS / "partition.py")
assert _spec is not None and _spec.loader is not None
partition = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(partition)
_spec = importlib.util.spec_from_file_location("envcheck_correction_test", SCRIPTS / "correction_data.py")
assert _spec is not None and _spec.loader is not None
correction = importlib.util.module_from_spec(_spec)
_previous = sys.modules.get("partition")
sys.modules["partition"] = partition
try:
    _spec.loader.exec_module(correction)
finally:
    if _previous is None:
        del sys.modules["partition"]
    else:
        sys.modules["partition"] = _previous

TASK = "mimo-v2.6-rl/format-code-task-000226"
MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-b"


def trial(path: Path, *, ident: int = 1, reward: float = 1.0, prompt: str = "Task input.", patch: str = "incomplete", exception: dict | None = None) -> Path:
    path.mkdir(parents=True)
    result = {
        "id": str(UUID(int=ident)), "task_name": TASK, "trial_name": "observed-a1__native",
        "config": {"job_id": str(UUID(int=100)), "agent": {"name": "mimoagent", "model_name": MODEL}},
        "agent_info": {"name": "mimoagent"},
        "agent_result": {"metadata": {"native_exit_status": "Idle"}},
        "verifier_result": {"rewards": {"reward": reward}}, "exception_info": exception,
        "finished_at": "2026-10-17T00:00:01+00:00",
    }
    partition.write_json(path / "result.json", result)
    (path / "verifier").mkdir()
    (path / "verifier/agent.diff").write_text(patch)
    (path / "agent/mimoagent").mkdir(parents=True)
    partition.write_json(path / "agent/mimoagent/native-trajectory.json", {
        "trajs": {"main": {"messages": [
            {"role": "system", "content": "Native system."},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Actual generation.", "reasoning_content": "Actual reasoning."},
        ], "tools": []}},
    })
    return path


@pytest.mark.parametrize("extra", ["", "\nUnrecorded historical context."])
def test_actual_task_prompt_preserves_native_universal_newline_boundary(tmp_path: Path, extra: str) -> None:
    prompt = "Fix the following issue:\n\nPreserve the original task body.\n"
    source = partition.source_record(trial(tmp_path / "source", prompt=prompt + extra))
    package = tmp_path / "package"
    package.mkdir()
    (package / "instruction.md").write_bytes(prompt.replace("\n", "\r\n").encode())
    if extra:
        with pytest.raises(ValueError):
            correction.actual_task_prompt(source, package)
    else:
        actual, _ = correction.actual_task_prompt(source, package)
        assert actual == prompt


def source_for(path: Path) -> dict:
    source = partition.source_record(path)
    source["provenance"] = {
        "trial_uuid": source["execution_id"], "job_uuid": source["alias"]["job_uuid"],
        "source_result_sha256": source["alias"]["files"]["result"]["sha256"],
        "aliases": [source.pop("alias")],
    }
    return source


def pin(package: Path | None = None) -> dict:
    digest = "sha256:" + "a" * 64
    if package is not None:
        from evallab.task_variants import task_directory_digest

        digest = task_directory_digest(package)
    return {"task": TASK, "repaired_digest": digest, "package_path": str(package or "/unused/package"),
            "qualified": True, "measurement_eligible": True,
            "manifest_row": {"task": TASK, "status": "repaired"}}


def manifest_variant(root: Path, package: Path, status: str, scope: tuple[int, int, int]) -> dict:
    """Synthetic retained accepted-manifest inputs; never executes a grader."""
    root.mkdir(parents=True)
    controls = root / "v6-controls/trials"
    controls.mkdir(parents=True)
    package_pin = pin(package)["repaired_digest"]
    number_id = TASK.rsplit("-", 1)[-1]
    for role, expected in (("oracle", 1.0), ("nop", 0.0)):
        partition.write_json(controls / f"{number_id}-{role}.json", {
            "task": number_id, "role": role, "ported_digest": package_pin,
            "status": "ok", "reward": expected, "repeats": [expected] * 3, "error": None,
        })
    total, killed, declared = scope
    partition.write_json(root / "manifest.json", {"variants": [{
        "task": TASK, "status": status, "variant_digest": package_pin,
        "variant_package_path": str(package), "package_digest_verified": True,
        "v6_oracle_x3": [1.0] * 3, "v6_nop_x3": [0.0] * 3,
        "v6_shortcut_replays_total": total, "v6_shortcut_replays_killed": killed,
        "declared_survivors": declared,
    }]})
    return partition.variants(root / "manifest.json")[0]


def receipt(source: dict, variant: dict, reward: float | None, path: Path, **extra) -> list[dict]:
    row = {
        "engine": "qa-trace-counterfactual/2", "execution_id": source["execution_id"],
        "task": TASK, "source_digest": source["provenance"]["aliases"][0]["source_digest"],
        "grader_digest": variant["repaired_digest"], "recorded": source["original_reward"],
        "regraded": reward, "valid_grader": True, "controls": {"nop": 0.0, "reference": 1.0},
        "outcome": "stays" if reward == source["original_reward"] else "flips", **extra,
    }
    partition.write_json(path, {"rows": [row]})
    return partition.receipt_rows([path])


def test_archive_aliases_are_one_execution_and_keep_exception_bytes(tmp_path: Path) -> None:
    exception = {"exception_type": "TrialBudgetExhaustedError", "exception_message": "retained"}
    trial(tmp_path / "archive", exception=exception)
    trial(tmp_path / "retained-copy", exception=exception)
    rows, issues = partition.inventory([pin()], [tmp_path], [], include_nonpasses=False)
    assert not issues
    assert len(rows) == 1
    assert len(rows[0]["provenance"]["aliases"]) == 2
    assert rows[0]["original_reward"] == 1.0
    assert rows[0]["flags"]["exception_info"] == exception
    assert rows[0]["flags"]["fidelity"] == "native_messages"


def test_reward_disagreement_in_alias_is_not_hidden_by_pass_filter(tmp_path: Path) -> None:
    trial(tmp_path / "pass", reward=1.0)
    trial(tmp_path / "changed-copy", reward=0.0)
    rows, issues = partition.inventory([pin()], [tmp_path], [], include_nonpasses=False)
    assert not issues and len(rows) == 1
    assert "alias_original_reward_conflict" in rows[0]["flags"]["alias_conflicts"]
    evidence = receipt(rows[0], pin(), 1.0, tmp_path / "receipt.json")
    joined = partition.join_rows(rows, [pin()], evidence)[0]
    assert joined["partition"] == "unmeasured"
    assert joined["repaired_reward"] is None


@pytest.mark.parametrize(("reward", "valid"), [(None, True), (0.0, False)])
def test_missing_or_invalid_receipt_never_becomes_scored_zero(tmp_path: Path, reward: float | None, valid: bool) -> None:
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, pin(), reward, tmp_path / "receipt.json", valid_grader=valid)
    joined = partition.join_rows([source], [pin()], evidence)[0]
    assert joined["repaired_reward"] is None
    assert joined["partition"] == "unmeasured"
    assert joined["status"] == "invalid_receipt"
    assert joined["provenance"]["receipts"][0]["row"] == evidence[0]["row"]


def test_full_repair_label_cannot_replace_missing_native_control_custody(tmp_path: Path) -> None:
    package = tmp_path / "grader"
    package.mkdir()
    (package / "instruction.md").write_text("Task input.")
    accepted = tmp_path / "accepted"
    manifest_variant(accepted, package, "repaired", (1, 1, 0))
    (accepted / "v6-controls/trials/000226-nop.json").unlink()
    variant = partition.variants(accepted / "manifest.json")[0]
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, variant, 0.0, tmp_path / "receipt.json")

    joined = partition.join_rows([source], [variant], evidence)[0]

    assert joined["partition"] == "unmeasured"
    assert joined["repaired_reward"] is None
    assert joined["status"] == "invalid_receipt"
    assert "manifest_variant_not_admitted" in joined["provenance"]["receipts"][0]["problems"]



@pytest.mark.parametrize(("native_reward", "exception"), [
    (0.0, {"exception_type": "InterpreterSetupError", "exception_message": "pytest unavailable"}),
    (None, None), (1.0, None),
])
def test_native_replay_observation_overrides_lossy_summary_zero(
    tmp_path: Path, native_reward: float | None, exception: dict | None
) -> None:
    source = source_for(trial(tmp_path / "source"))
    replay = trial(tmp_path / "replay/native-trial", ident=2, reward=native_reward, exception=exception)
    evidence = receipt(source, pin(), 0.0, tmp_path / "receipt.json", job_dir=str(replay.parent))
    joined = partition.join_rows([source], [pin()], evidence)[0]
    assert joined["status"] == "invalid_receipt"
    assert joined["repaired_reward"] is None and joined["partition"] == "unmeasured"
    retained = joined["provenance"]["receipts"][0]
    assert retained["native_results"][0]["reward"] == native_reward
    assert retained["native_results"][0]["exception_info"] == exception
    expected_problem = "native_replay_exception" if exception else "native_replay_reward_missing_or_mismatched"
    assert expected_problem in retained["problems"]


@pytest.mark.parametrize("scope", [(10, 8, 2), (0, 0, 7)])
@pytest.mark.parametrize("reward", [0.0, 1.0])
def test_partial_scope_is_measurable_without_becoming_fully_qualified(
    tmp_path: Path, scope: tuple[int, int, int], reward: float
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "instruction.md").write_text("Task input.")
    variant = manifest_variant(tmp_path / "admission", package, "partial", scope)
    assert variant["measurement_eligible"] is True and variant["qualified"] is False
    assert not variant["measurement_custody"]["problems"]
    assert len(variant["measurement_custody"]["controls"]) == 2
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, variant, reward, tmp_path / "receipt.json")
    joined = partition.join_rows([source], [variant], evidence)[0]
    assert joined["status"] == "measured" and joined["repaired_reward"] == reward
    assert joined["partition"] == ("true_pass" if reward == 1.0 else "false_pass")
    assert joined["partition_scope"] == "mechanical_repaired_grader"
    original_scope = joined["variant"]["manifest_row"]
    assert original_scope["status"] == "partial"
    assert (original_scope["v6_shortcut_replays_total"], original_scope["v6_shortcut_replays_killed"],
            original_scope["declared_survivors"]) == scope
    moved = tmp_path / "expanded.json"
    partition.write_json(moved, {"variants": [original_scope | {
        "control_receipts_dir": str(tmp_path / "admission/v6-controls/trials"),
    }]})
    assert partition.variants(moved)[0]["measurement_eligible"] is True
    infra = receipt(source, variant, 0.0, tmp_path / "infra.json", execution_error="InterpreterSetupError: pytest unavailable")
    failed = partition.join_rows([source], [variant], infra)[0]
    assert failed["partition"] == "unmeasured" and failed["repaired_reward"] is None


@pytest.mark.parametrize(("status", "failure", "eligible"), [
    ("partial", None, True), ("partial", "infra", False),
    ("partial", "package", False), ("partial", "matrix", False),
    ("candidate", None, False), ("infra", None, False), ("unqualified", None, False),
])
def test_native_measurement_admission_requires_accepted_bound_noninfra_controls(
    tmp_path: Path, status: str, failure: str | None, eligible: bool
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "instruction.md").write_text("Task input.")
    variant = manifest_variant(tmp_path / "admission", package, status, (0, 0, 7))
    matrix = tmp_path / "matrix.json"
    matrix.write_text('{"retained": "matrix"}')
    matrix_sha = partition.file_ref(matrix)["sha256"].removeprefix("sha256:")
    checks = [
        {"role": role, "status": "ok", "expected_reward": expected,
         "matrix_sha256": matrix_sha, "repeats": [expected] * 3}
        for role, expected in (("reference", 1.0), ("nop", 0.0))
    ]
    if failure == "infra":
        checks[0].update(status="infra", repeats=[0.0], error="InterpreterSetupError: pytest unavailable")
    qualification = tmp_path / "qualification.json"
    partition.write_json(qualification, {
        "task": TASK, "package_digest": "sha256:" + "b" * 64 if failure == "package" else variant["repaired_digest"],
        "matrix": str(matrix), "matrix_sha256": matrix_sha, "checks": checks,
        "qualified": False,
    })
    if failure == "matrix":
        matrix.write_text('{"changed": "matrix"}')
    manifest = tmp_path / "admission/manifest.json"
    doc = partition.read_json(manifest)
    doc["variants"][0].update(qualification_receipt=str(qualification), qualified=True)
    partition.write_json(manifest, doc)
    variant = partition.variants(manifest)[0]
    assert variant["measurement_eligible"] is eligible
    assert variant["qualified"] is False  # Neither partial nor candidate/infra becomes certified.
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, variant, 0.0, tmp_path / "receipt.json")
    joined = partition.join_rows([source], [variant], evidence)[0]
    assert joined["partition"] == ("false_pass" if eligible else "unmeasured")
    assert joined["repaired_reward"] == (0.0 if eligible else None)


def test_exact_digest_and_source_bytes_are_required(tmp_path: Path) -> None:
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, pin(), 1.0, tmp_path / "bad-source.json", source_digest="b" * 64)
    joined = partition.join_rows([source], [pin()], evidence)[0]
    assert joined["status"] == "invalid_receipt"
    assert "source_bytes_mismatch" in joined["provenance"]["receipts"][0]["problems"]
    other = pin() | {"repaired_digest": "sha256:" + "a" * 63 + "b"}
    joined = partition.join_rows([source], [other], evidence)[0]
    assert joined["status"] == "missing_receipt" and joined["repaired_reward"] is None


def test_conflicting_valid_observations_are_not_selected(tmp_path: Path) -> None:
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, pin(), 0.0, tmp_path / "fail.json")
    evidence += receipt(source, pin(), 1.0, tmp_path / "pass.json")
    joined = partition.join_rows([source], [pin()], evidence)[0]
    assert joined["status"] == "conflicting_receipts"
    assert joined["partition"] == "unmeasured" and joined["receipt"] is None


def test_m3_original_zero_keeps_its_observed_repaired_score(tmp_path: Path) -> None:
    source = source_for(trial(tmp_path / "source", reward=0.0))
    joined = partition.join_rows([source], [pin()], receipt(source, pin(), 1.0, tmp_path / "receipt.json"))[0]
    assert joined["original_reward"] == 0.0 and joined["repaired_reward"] == 1.0
    assert joined["status"] == "measured" and joined["partition"] == "unmeasured"


def test_feedback_drops_all_test_source_paths_messages_and_captured_output() -> None:
    raw = """\x1b[31mFAILED tests/secret_reference.py::test_private - AssertionError: expected hidden_answer\x1b[0m
__________________ test_private __________________
    def test_private():
>       assert hidden_answer() == reference_source()
E       assert hidden_answer() == reference_source()
E       AssertionError: expected hidden_answer
Traceback (most recent call last):
  File "/solution/hidden_reference.py", line 9, in reference_source
    return reference_source()
Captured stdout call:
print('SECRET_SOURCE_BYTES')
=================== 1 failed, 2 passed in 0.12s ===================
"""
    safe, policy = correction.safe_feedback(raw)
    parsed = json.loads(safe)
    assert parsed["outcome_counts"] == [{"failed": 1, "passed": 2}]
    assert parsed["exception_classes"] == ["AssertionError"]
    assert len(parsed["failing_checks"]) == 1
    for forbidden in ("hidden_answer", "reference_source", "test_private", "/solution", "SECRET_SOURCE_BYTES", "assert ", "return "):
        assert forbidden not in safe
    assert policy["source_lines_forwarded"] == 0
    assert policy["safe_sha256"] == partition.sha256(safe.encode())


@pytest.mark.parametrize("raw", ["new custom grader output without structured failures", "def hidden_test():\n    assert secret()", "2 passed in 0.2s"])
def test_unknown_or_nonfailing_feedback_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError, match="safely recognized failing"):
        correction.safe_feedback(raw)


def test_unittest_failure_retains_only_outcomes_and_builtin_class() -> None:
    safe, _ = correction.safe_feedback("FAIL: test_private (reference.Secret)\nAssertionError: hidden source\nFAILED (failures=1, errors=2)\n")
    parsed = json.loads(safe)
    assert parsed["outcome_counts"] == [{"failures": 1, "errors": 2}]
    assert parsed["exception_classes"] == ["AssertionError"]
    assert "private" not in safe and "reference" not in safe and "hidden" not in safe


def test_heldout_exclusion_precedes_source_access_and_repeats_at_export(tmp_path: Path) -> None:
    row = {"task": TASK}
    with pytest.raises(ValueError, match="before_generation"):
        correction.request_for(row, {TASK}, {}, tmp_path)
    with pytest.raises(ValueError, match="at_export"):
        correction.checked_request(row, {TASK}, {})
    assert list(tmp_path.iterdir()) == []


def test_empty_export_reports_prerequisites_without_loading_tokenizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = tmp_path / "requests.jsonl"
    requests.write_text("")
    monkeypatch.setattr(partition, "frozen_heldout", lambda _: (set(), {"sha256": "frozen33"}))

    def forbidden(*args, **kwargs):
        raise AssertionError("empty export cannot import a tokenizer or execute a model")

    monkeypatch.setattr(partition, "load_script", forbidden)
    args = SimpleNamespace(heldout_manifest=tmp_path / "heldout", requests=requests,
                           receipt=[], completion_trial=[], out=tmp_path / "export", max_length=65536)
    assert correction.export(args) == 3
    summary = partition.read_json(args.out / "summary.json")
    assert not summary["export_ready"] and summary["training_targets"] == 0
    assert summary["missing_prerequisites"]
    assert (args.out / "conversations.jsonl").read_bytes() == b""
    assert (args.out / "provenance.jsonl").read_bytes() == b""


@pytest.mark.parametrize("tokenizer_available", [True, False])
@pytest.mark.parametrize("grader_status", ["repaired", "partial"])
def test_real_native_completion_targets_preserve_prefixes_and_last_only_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokenizer_available: bool, grader_status: str
) -> None:
    monkeypatch.setattr(partition, "REPO", tmp_path)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    heldout_ref = {"path": "synthetic-frozen33", "sha256": "frozen33"}
    monkeypatch.setattr(partition, "frozen_heldout", lambda _: (set(), heldout_ref))
    package = tmp_path / "package"
    package.mkdir()
    (package / "instruction.md").write_text("Task input.")
    (package / "task.toml").write_text("version = '1.0'\n")
    variant = manifest_variant(tmp_path / "admission", package, grader_status, (0, 0, 7))
    original = source_for(trial(tmp_path / "original"))
    replay = trial(tmp_path / "repaired-job/native-trial", ident=2, reward=0.0)
    (replay / "verifier/test_output.log").write_text("E AssertionError: hidden source\n1 failed in 0.1s\n")
    evidence = receipt(original, variant, 0.0, tmp_path / "original-regrade.json", job_dir=str(replay.parent))
    false_pass = partition.join_rows([original], [variant], evidence)[0]
    request_root = tmp_path / "runs/requests"
    request_root.mkdir(parents=True)
    request = correction.request_for(false_pass, set(), heldout_ref, request_root)
    request_path = request_root / "requests.jsonl"
    partition.write_jsonl(request_path, [request])
    extra = Path(request["provenance"]["correction"]["extra_instruction"]["path"]).read_text()
    completed = trial(tmp_path / "completed", ident=3, reward=0.0,
                      prompt="Task input.\n\n" + extra, patch="actually corrected")
    native = completed / "agent/mimoagent/native-trajectory.json"
    doc = partition.read_json(native)
    messages = doc["trajs"]["main"]["messages"]
    messages[-1]["tool_calls"] = [{"id": "call", "type": "function", "function": {"name": "bash", "arguments": '{"command":"true"}'}}]
    messages.extend([{"role": "tool", "content": "actual tool result", "tool_call_id": "call"},
                     {"role": "assistant", "content": "Actual corrected completion.", "reasoning_content": "Actual final reasoning."}])
    partition.write_json(native, doc)
    completed_source = source_for(completed)
    corrected_receipt = tmp_path / "corrected-regrade.json"
    receipt(completed_source, variant, 1.0, corrected_receipt)
    observed = []

    def flatten(_tok, row):
        observed.append(row)
        return [{k: m[k] for k in ("role", "content", "reasoning_content") if k in m} for m in row["messages"]]

    def render(_tok, flat, *, max_length, label, loss):
        assert loss == "last" and flat[-1]["role"] == "assistant"
        return SimpleNamespace(input_ids=[1, 2], trained=1)

    converter = SimpleNamespace(flatten=flatten, sft=SimpleNamespace(
        get_tokenizer=lambda: object(), render_and_mask=render, SftError=ValueError))
    monkeypatch.setattr(partition, "load_script", lambda *_: converter)
    if not tokenizer_available:
        def unavailable(*_args):
            raise OSError("synthetic tokenizer cache is absent")

        monkeypatch.setattr(partition, "load_script", unavailable)
    args = SimpleNamespace(heldout_manifest=tmp_path / "heldout", requests=request_path,
                           receipt=[corrected_receipt], completion_trial=[completed],
                           out=tmp_path / "export", max_length=65536)
    assert correction.export(args) == (0 if tokenizer_available else 3)
    if not tokenizer_available:
        summary = partition.read_json(args.out / "summary.json")
        assert summary["eligible_completed_corrections"] == 1 and summary["training_targets"] == 0
        assert any("tokenizer" in reason for reason in summary["missing_prerequisites"])
        assert len(partition.read_jsonl(args.out / "recorded.jsonl")) == 1
        assert (args.out / "conversations.jsonl").read_bytes() == b""
        return
    targets = partition.read_jsonl(args.out / "conversations.jsonl")
    provenance = partition.read_jsonl(args.out / "provenance.jsonl")
    assert len(targets) == 2 and all(row["loss"] == "last" for row in targets)
    assert [row["messages"] for row in observed] == [messages[:3], messages]
    assert [row["target_message_index"] for row in provenance] == [2, 4]
    assert all(row["source_execution_id"] == original["execution_id"] for row in provenance)
    assert all(row["corrected_patch"]["sha256"] == partition.sha256(b"actually corrected") for row in provenance)
    assert all(row["repaired_reward"] == 1.0 and row["original_reward"] == 0.0 for row in provenance)
    assert all(row["variant"]["qualified"] is (grader_status == "repaired") for row in provenance)
    assert all(row["variant"]["measurement_eligible"] is True for row in provenance)
    assert all(row["variant"]["manifest_row"]["declared_survivors"] == 7 for row in provenance)
    assert all(row["partition_scope"] == "mechanical_repaired_grader" for row in provenance)
    # The incomplete source patch was actual user input, never an invented assistant message.
    assert all("incomplete" not in row["messages"][-1]["content"] for row in targets)


def test_truncated_trial_name_uses_observed_native_job_attempt(tmp_path: Path) -> None:
    path = trial(tmp_path / "source")
    result = partition.read_json(path / "result.json")
    result["trial_name"] = "long-native-name-truncated__uuid"
    result["config"]["trials_dir"] = "/retained/native-job-a2"
    partition.write_json(path / "result.json", result)
    source = partition.source_record(path)
    assert source["attempt"] == 2
    assert source["flags"]["attempt_evidence"] == {"native-job-a2": 2}


def test_mutated_join_row_cannot_authorize_correction_feedback(tmp_path: Path) -> None:
    source = source_for(trial(tmp_path / "source"))
    evidence = receipt(source, pin(), 1.0, tmp_path / "receipt.json")[0]
    evidence["row"]["regraded"] = 0.0
    with pytest.raises(ValueError, match="untouched canonical repaired receipt"):
        correction.checked_evidence(evidence)


def test_same_target_input_match_rejects_model_substitution_and_history(tmp_path: Path) -> None:
    path = trial(tmp_path / "source")
    source = partition.source_record(path)
    prompt_file, extra_file = tmp_path / "prompt.txt", tmp_path / "extra.txt"
    prompt_file.write_text("Task input.")
    extra_file.write_text("Bound feedback.")
    request = {
        "task": TASK, "model": "different-model",
        "task_prompt": partition.file_ref(prompt_file),
        "provenance": {"correction": {"extra_instruction": partition.file_ref(extra_file)}},
    }
    messages = [{"role": "system", "content": "Native system."},
                {"role": "user", "content": "Task input.\n\nBound feedback."}]
    with pytest.raises(ValueError, match="does not match exactly one"):
        correction.match_request(source, [request], messages)
    request["model"] = MODEL
    request_match, boundary = correction.match_request(source, [request], messages)
    assert request_match == request and boundary == 1
    result = partition.read_json(path / "result.json")
    result["config"]["agent"]["load_trajectory"] = "historical-state"
    partition.write_json(path / "result.json", result)
    with pytest.raises(ValueError, match="not a reset-context"):
        correction.match_request(source, [request], messages)


def test_measured_subset_is_not_a_frozen_all33_training_exclusion(tmp_path: Path) -> None:
    path = tmp_path / "measured26.json"
    partition.write_json(path, {"heldout": [f"format-code-task-{i:06d}" for i in range(26)]})
    with pytest.raises(ValueError, match="exact frozen all33"):
        partition.frozen_heldout(path)


@pytest.fixture
def native_budget_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("envcheck_native_budget_test", SCRIPTS / "run_corrections.py")
    assert spec is not None and spec.loader is not None
    driver = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, driver)
    spec.loader.exec_module(driver)
    event = {"kind": "native_correction", "ordinal": 1, "source_execution_id": "source",
             "repaired_digest": "repaired", "model": MODEL, "purpose": "evaluation"}
    native = tmp_path / "native.json"
    partition.write_json(native, {
        "info": {"model_stats": {key: 0 for key in
                                ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens",
                                 "cache_creation_tokens", "total_tokens")}},
        "trajs": {"main": {"messages": [{"role": "user", "content": "Task and safe feedback."}]}},
    })
    calls = tmp_path / "model-calls.jsonl"
    partition.write_jsonl(calls, [{"response_status": 429, "usage": None,
                                 "proxy_budget_reason": "trial budget exhausted"}])
    execution = tmp_path / "executions.jsonl"
    partition.write_jsonl(execution, [{
        **{key: event[key] for key in ("source_execution_id", "repaired_digest", "model", "purpose")},
        "reservation": event, "status": "native_model_budget_stop",
        "native_exception_type": "TrialBudgetExhaustedError",
        "native_finished_at": "2026-10-17T00:00:01+00:00", "corrected_patch": None,
        "observed_model_requests": 1, "native_files": {"native_trajectory": partition.file_ref(native)},
    }])
    path = tmp_path / "budget.json"
    state = {"approval": "synthetic-parent", "cap_usd": .30, "usd_per_execution": .10,
             "executions": 1, "reserved_usd": .10, "charges": [event],
             "closed_zero_completion_reservations": [
                 {"ordinal": 1, "execution": partition.file_ref(execution),
                  "model_calls": partition.file_ref(calls)}]}
    partition.write_json(path, state)
    args = SimpleNamespace(budget=path, approval=state["approval"], max_usd=.30,
                           reserve_usd=.10, out=tmp_path)
    job = SimpleNamespace(selection=SimpleNamespace(request={
        "source_execution_id": "source",
        "source_partition": {"variant": {"repaired_digest": "repaired"}},
    }))
    return driver, args, state, job, native, execution, calls


def test_explicit_zero_completion_closure_never_refunds_the_consumed_dispatch(native_budget_failure) -> None:
    driver, args, state, job, *_ = native_budget_failure
    before = args.budget.read_bytes()
    budget = driver.NativeBudget(args)
    checked = budget.check([job])
    assert checked["executions"] == budget.native.executions == 1
    assert checked["reserved_usd"] == .10 and checked["charges"] == state["charges"]
    assert args.budget.read_bytes() == before
    other_jobs = [SimpleNamespace(selection=SimpleNamespace(request={
        "source_execution_id": f"other-{i}",
        "source_partition": {"variant": {"repaired_digest": "repaired"}},
    })) for i in range(3)]
    with pytest.raises(driver.Refusal, match="remaining shared subcap"):
        budget.check(other_jobs)
    state.pop("closed_zero_completion_reservations")
    partition.write_json(args.budget, state)
    with pytest.raises(driver.Refusal, match="no implicit retry"):
        driver.NativeBudget(args).check([job])


@pytest.mark.parametrize("mutation", ["usage", "assistant", "upstream_success", "patch"])
def test_sampled_answers_cannot_be_discarded_as_zero_completion_infrastructure(
    native_budget_failure, mutation: str
) -> None:
    driver, args, state, _, native, execution, calls = native_budget_failure
    row = partition.read_jsonl(execution)[0]
    if mutation in ("usage", "assistant"):
        doc = partition.read_json(native)
        if mutation == "usage":
            doc["info"]["model_stats"]["api_calls"] = 1
        else:
            doc["trajs"]["main"]["messages"].append({"role": "assistant", "content": "Sampled answer."})
        partition.write_json(native, doc)
        row["native_files"]["native_trajectory"] = partition.file_ref(native)
    elif mutation == "upstream_success":
        partition.write_jsonl(calls, [{"response_status": 200, "usage": None,
                                     "proxy_budget_reason": "trial budget exhausted"}])
        state["closed_zero_completion_reservations"][0]["model_calls"] = partition.file_ref(calls)
    else:
        row["corrected_patch"] = {"path": "a retained candidate"}
    partition.write_jsonl(execution, [row])
    state["closed_zero_completion_reservations"][0]["execution"] = partition.file_ref(execution)
    partition.write_json(args.budget, state)
    with pytest.raises(driver.Refusal, match="sampled|candidate patch|first-request pre-upstream"):
        driver.NativeBudget(args)


def test_native_join_accepts_only_a_bound_originally_passing_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(trial(tmp_path / "source"))
    variant = pin()
    receipt_path = tmp_path / "receipt.json"
    receipt(source, variant, 0.0, receipt_path)
    inventory = tmp_path / "inventory.jsonl"
    passing = tmp_path / "passes.jsonl"
    partition.write_jsonl(inventory, [source, {**source, "original_reward": 0.0}])
    partition.write_jsonl(passing, [source])
    manifest = {"inventory": partition.file_ref(inventory), "passes": partition.file_ref(passing),
                "variants": [variant], "heldout": {"path": "synthetic-frozen33", "sha256": "frozen33"}}
    partition.write_json(tmp_path / "manifest.json", manifest)
    output = tmp_path / "partition"
    arguments = ["partition.py", "join", "--inventory", str(passing),
                 "--receipt", str(receipt_path), "--out", str(output)]
    monkeypatch.setattr(sys, "argv", arguments)
    assert partition.main() == 0
    rows = partition.read_jsonl(output / "false_pass.jsonl")
    assert [(row["execution_id"], row["original_reward"], row["repaired_reward"])
            for row in rows] == [(source["execution_id"], 1.0, 0.0)]
    passing.write_text(passing.read_text() + "\n")
    with pytest.raises(ValueError, match="source manifest"):
        partition.main()
