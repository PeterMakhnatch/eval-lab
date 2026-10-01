"""Acquisition, not a command attempt, decides copied-fix exclusions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.counts import attach_counts, classify_counts
from evallab.process_job import _taint_flags
from evallab.trial_decision import build_decision
from evallab.upstream_fetch import assess_upstream_fetch, confirmed_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "upstream_fetch"


def _native(task: str) -> dict:
    return json.loads((FIXTURES / f"g2-{task}.json").read_text())


def _step(command: str, content: str | None = None, *, sid: int = 1, call_id: str = "fetch", exit_code: int | None = None) -> dict:
    result = {"source_call_id": call_id, "content": content}
    if exit_code is not None:
        result["exit_code"] = exit_code
    return {
        "step_id": sid,
        "source": "agent",
        "tool_calls": [{"tool_call_id": call_id, "function_name": "terminal", "arguments": {"keystrokes": command}}],
        "observation": {"results": [result] if content is not None or exit_code is not None else []},
    }


@pytest.mark.parametrize("task,verdict,reasons", [
    ("001870", "counted_pass", []),
    ("000341", "excluded", ["copied_fix", "pass_tainted"]),
])
def test_native_g2_producer_to_counts_and_decision(tmp_path: Path, task: str, verdict: str, reasons: list[str]) -> None:
    native = _native(task)
    result = native["result"]
    reward = result["verifier_result"]["rewards"]["reward"]
    assert reward == 1.0
    flags = _taint_flags([("head", step) for step in native["steps"]], {}, tmp_path)
    record = {"reward": reward, "scored": True, "taint": flags, "trial_name": result["trial_name"], "task_name": result["task_name"]}
    counts = attach_counts(record, result, label_root=None)
    assert counts["verdict"] == verdict
    assert counts["reasons"] == reasons
    assert counts["raw_reward"] == reward
    decision = build_decision(tmp_path, reward=reward, scored=True, outcome=None, first_failure=None, grader_evidence=None, taint=flags, token_flow=None, counts=counts)
    assert decision["pass_tainted"]["flagged"] == (task == "000341")
    assert decision["fetched_fix"]["fetched"] == (task == "000341")
    if task == "001870":
        assert {flag["step"] for flag in flags} == {13, 14, 15}
        assert all(not confirmed_fetch(flag) for flag in flags)
        assert len(counts["flags"]) == 3
        assert all(not flag["decisive"] for flag in counts["flags"])
    else:
        success = next(flag for flag in flags if confirmed_fetch(flag))
        assert success["call_id"] == "call_38_1"
        assert [item["call_id"] for item in success["outcome_evidence"]] == ["call_38_1", "call_39_1"]
        assert {item["artifact"] for item in success["outcome_evidence"]} == {"/tmp/vc/vyper_config-1.0.0-py3-none-any.whl"}
        assert counts["evidence"][0]["observations"] == success["outcome_evidence"]


@pytest.mark.parametrize("command", [
    "pip download example==1.0", "uv pip download example==1.0", "python -m pip download example==1.0",
    "curl https://example.org/source", "wget https://example.org/source", "http https://example.org/source",
    "git clone https://example.org/repo", "git fetch https://example.org/repo", "git pull https://example.org/repo", "apt-get source example",
])
def test_recorded_status_of_isolated_acquisition_command(command: str) -> None:
    flags = assess_upstream_fetch([("head", _step(command, exit_code=0))], {})
    assert len(flags) == 1 and confirmed_fetch(flags[0])
    failed = assess_upstream_fetch([("head", _step(command, exit_code=1))], {})
    assert failed[0]["outcome"] == "failed"
    assert classify_counts(reward=1.0, scored=True, taint=failed)["verdict"] == "counted_pass"


@pytest.mark.parametrize("command", [
    "pip download example==1.0 | tail -3",
    "pip download example==1.0; ls /tmp",
    "pip download example==1.0 || true",
    "pip download example==1.0; echo success",
    "pip index versions example",
    "git remote add https://example.org/repo",
    "python -c 'import requests; requests.get(\"https://example.org/source\")'",
    "node -e 'fetch(\"https://example.org/source\")'",
])
def test_compound_or_nonacquisition_status_is_not_success(command: str) -> None:
    flags = assess_upstream_fetch([("head", _step(command, exit_code=0))], {})
    assert flags and all(not confirmed_fetch(flag) for flag in flags)
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["verdict"] == "counted_pass"
    assert all(not flag["decisive"] for flag in counts["flags"])


@pytest.mark.parametrize("content", [None, "", "Successfully downloaded unrelated", "Successfully downloaded example-extra", "Requirement already satisfied: example"])
def test_missing_or_other_package_output_is_unknown(content: str | None) -> None:
    flags = assess_upstream_fetch([("head", _step("pip download example==1.0", content))], {})
    assert flags[0]["outcome"] == "unknown"
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"


def test_echo_and_buffered_success_do_not_prove_current_call() -> None:
    command = "pip download example==1.0; echo 'Successfully downloaded example'"
    flags = assess_upstream_fetch([("head", _step(command, "Successfully downloaded example"))], {})
    assert not confirmed_fetch(flags[0])
    command = "pip download example==1.0"
    delayed = "New Terminal Output:\nSuccessfully downloaded example\nroot@host:/repo# " + command + "\n"
    flags = assess_upstream_fetch([("head", _step(command, delayed))], {})
    assert flags[0]["outcome"] == "unknown"


def test_call_document_and_reused_step_identity_are_not_join_keys() -> None:
    mismatched = _step("pip download example==1.0", "Successfully downloaded example")
    mismatched["observation"]["results"][0]["source_call_id"] = "another-call"
    first = _step("pip download example==1.0", sid=1)
    success = _step("pip download example==1.0", "Successfully downloaded example", sid=1)
    flags = assess_upstream_fetch([("head", mismatched), ("head", first), ("cont-1", success)], {})
    assert [flag["outcome"] for flag in flags] == ["unknown", "unknown", "succeeded"]
    assert flags[-1]["outcome_evidence"][0]["document"] == "cont-1"


def test_artifact_listing_needs_target_bound_observed_use() -> None:
    native = _native("000341")["steps"]
    assert not confirmed_fetch(assess_upstream_fetch([("head", native[0])], {})[0])
    wrong_use = json.loads(json.dumps(native[1]))
    wrong_use["tool_calls"][0]["arguments"]["keystrokes"] = wrong_use["tool_calls"][0]["arguments"]["keystrokes"].replace("/tmp/vc/vyper_config", "/tmp/other/vyper_config")
    assert not confirmed_fetch(assess_upstream_fetch([("head", native[0]), ("head", wrong_use)], {})[0])
    # A continuation document cannot supply proof for the head's episode.
    assert not confirmed_fetch(assess_upstream_fetch([("head", native[0]), ("cont-1", native[1])], {})[0])
    failed_listing = _step("pip download example==1.0 -d /tmp/pkg; ls /tmp/pkg", "ERROR: No matching distribution found for example==1.0\nexample-1.0-py3-none-any.whl")
    assert assess_upstream_fetch([("head", failed_listing)], {})[0]["outcome"] == "failed"


def test_failure_does_not_suppress_independent_success() -> None:
    failed = _step("pip download example==1.0", "ERROR: No matching distribution found for example==1.0")
    success = _step("pip download example==1.0", "Successfully downloaded example", sid=2, call_id="second")
    flags = assess_upstream_fetch([("head", failed), ("head", success)], {})
    assert [flag["outcome"] for flag in flags] == ["failed", "succeeded"]
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["reasons"] == ["copied_fix", "pass_tainted"]
    assert counts["flags"][0]["outcome"] == "failed"


@pytest.mark.parametrize("flag", [
    {"kind": "upstream_fetch", "command": "pip download example"},
    {"kind": "upstream_fetch", "outcome": "succeeded", "outcome_evidence": []},
])
def test_no_legacy_success_fallback_and_other_exclusions_survive(flag: dict) -> None:
    assert classify_counts(reward=1.0, scored=True, taint=[flag])["verdict"] == "counted_pass"
    guarded = classify_counts(reward=1.0, scored=True, taint=[flag, {"kind": "guard_reject", "message": "guard"}])
    assert guarded["reasons"] == ["pass_tainted"]
    unusable = classify_counts(reward=1.0, scored=True, taint=[flag], usability={"status": "broken_environment", "reason": "broken fixture"})
    assert unusable["reasons"] == ["task_not_usable"]
    assert classify_counts(reward=None, scored=False, taint=[flag])["reasons"] == ["infra"]


def test_legacy_terminal_window_is_bound_without_inventing_a_call_id() -> None:
    command = "pip download example==1.0"
    step = _step(command, "New Terminal Output:\nroot@host:/repo# " + command + "\nSuccessfully downloaded example\nroot@host:/repo# ")
    step["observation"]["results"][0].pop("source_call_id")
    flags = assess_upstream_fetch([("head", step)], {})
    assert confirmed_fetch(flags[0])
    assert flags[0]["outcome_evidence"][0]["observation_binding"] == ["terminal-command-window"]
    assert "source_call_id" not in step["observation"]["results"][0]
    step["observation"]["results"][0]["content"] = "Successfully downloaded example"
    assert not confirmed_fetch(assess_upstream_fetch([("head", step)], {})[0])


def test_terminal_window_ends_before_another_calls_output() -> None:
    command = "pip download example==1.0"
    content = "New Terminal Output:\nroot@host:/repo# " + command + "\nroot@host:/repo# cat old-log\nSuccessfully downloaded example\n"
    step = _step(command, content)
    assert not confirmed_fetch(assess_upstream_fetch([("head", step)], {})[0])


def test_duplicate_call_ids_and_copied_context_never_supply_proof() -> None:
    step = _step("pip download example==1.0", "Successfully downloaded example")
    step["tool_calls"].append(json.loads(json.dumps(step["tool_calls"][0])))
    assert all(not confirmed_fetch(flag) for flag in assess_upstream_fetch([("head", step)], {}))
    copied = _step("pip download example==1.0", "Successfully downloaded example")
    copied["is_copied_context"] = True
    assert assess_upstream_fetch([("cont-1", copied)], {}) == []


@pytest.mark.parametrize("task,verdict,chain", [
    ("001269-r2", "counted_pass", []),
    ("001373-r2", "excluded", ["call_7_1", "call_8_1"]),
    ("002356-r2", "excluded", ["call_11_1", "call_12_1", "call_13_1"]),
])
def test_native_r2_companion_and_pinned_glob_acquisition(tmp_path: Path, task: str, verdict: str, chain: list[str]) -> None:
    native = _native(task)
    result = native["result"]
    reward = result["verifier_result"]["rewards"]["reward"]
    flags = _taint_flags([("head", step) for step in native["steps"]], {}, tmp_path)
    counts = attach_counts({"reward": reward, "scored": True, "taint": flags}, result, label_root=None)
    assert counts["raw_reward"] == 1.0
    assert counts["verdict"] == verdict
    assert counts["reasons"] == (["copied_fix", "pass_tainted"] if chain else [])
    decision = build_decision(tmp_path, reward=reward, scored=True, outcome=None, first_failure=None, grader_evidence=None, taint=flags, token_flow=None, counts=counts)
    assert decision["pass_tainted"]["flagged"] == bool(chain)
    assert decision["fetched_fix"]["fetched"] == bool(chain)
    successes = [flag for flag in flags if confirmed_fetch(flag)]
    if chain:
        assert [item["call_id"] for item in successes[0]["outcome_evidence"]] == chain
    else:
        # The failed download's output is delayed into the next local call:
        # retain attribution uncertainty instead of inventing acquisition.
        assert not successes
        assert flags[0]["target"] == "responses==0.15.0"
        assert counts["flags"][0]["decisive"] is False


def test_ambiguous_companion_summary_needs_observed_exact_artifact_use() -> None:
    steps = _native("001373-r2")["steps"]
    flags = assess_upstream_fetch([("head", steps[0])], {})
    assert not confirmed_fetch(flags[0])
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"


@pytest.mark.parametrize("replacement", ["*.whl", "black-24.*.whl", "black-23.1.0-*.whl", "/tmp/other/black-24.4.2-*.whl"])
def test_glob_cannot_borrow_another_package_version_or_directory(replacement: str) -> None:
    # Synthetic boundary perturbations of exact native observations, not new
    # experimental traces: keep echo and command synchronized.
    steps = json.loads(json.dumps(_native("002356-r2")["steps"]))
    extraction = steps[1]
    for call in extraction["tool_calls"]:
        call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace("black-24.4.2-*.whl", replacement)
    for result in extraction["observation"]["results"]:
        result["content"] = result["content"].replace("black-24.4.2-*.whl", replacement)
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not any(confirmed_fetch(flag) for flag in flags)
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"


def test_observed_unpack_listing_without_readback_is_not_acquisition_proof() -> None:
    steps = _native("002356-r2")["steps"]
    flags = assess_upstream_fetch([("head", step) for step in steps[:2]], {})
    assert not any(confirmed_fetch(flag) for flag in flags)


def test_separate_readback_must_stay_inside_the_observed_extraction_destination() -> None:
    steps = json.loads(json.dumps(_native("002356-r2")["steps"]))
    read = steps[2]
    for call in read["tool_calls"]:
        call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace("/tmp/black24", "/tmp/unrelated")
    for result in read["observation"]["results"]:
        result["content"] = result["content"].replace("/tmp/black24", "/tmp/unrelated")
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not any(confirmed_fetch(flag) for flag in flags)


def test_relative_artifact_operand_requires_a_success_conditioned_cd() -> None:
    steps = json.loads(json.dumps(_native("001373-r2")["steps"]))
    extraction = steps[1]
    for call in extraction["tool_calls"]:
        call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace("cd /tmp/gcl &&", "cd /tmp/gcl;")
    for result in extraction["observation"]["results"]:
        result["content"] = result["content"].replace("cd /tmp/gcl &&", "cd /tmp/gcl;")
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not any(confirmed_fetch(flag) for flag in flags)


def test_another_archive_replacing_extraction_destination_ends_readback_proof() -> None:
    steps = _native("002356-r2")["steps"]
    replacement = _step("unzip -o -q /tmp/unrelated.whl -d /tmp/black24", "inflating: /tmp/black24/black/numerics.py", sid=15, call_id="replacement")
    flags = assess_upstream_fetch([("head", step) for step in [*steps[:2], replacement, steps[2]]], {})
    assert not any(confirmed_fetch(flag) for flag in flags)
