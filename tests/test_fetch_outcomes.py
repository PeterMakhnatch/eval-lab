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
def test_zero_exit_without_artifact_use_is_not_positive_proof(command: str) -> None:
    flags = assess_upstream_fetch([("head", _step(command, exit_code=0))], {})
    assert flags[0]["outcome"] == "unknown"
    assert not confirmed_fetch(flags[0])
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"
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


@pytest.mark.parametrize("content", [None, "", "Successfully downloaded example", "Saved /tmp/pkg/example-1.0-py3-none-any.whl", "Successfully downloaded unrelated", "Successfully downloaded example-extra", "Requirement already satisfied: example"])
def test_summary_or_saved_artifact_without_use_is_unknown(content: str | None) -> None:
    flags = assess_upstream_fetch([("head", _step("pip download example==1.0 -d /tmp/pkg", content))], {})
    assert flags[0]["outcome"] == "unknown"
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"


@pytest.mark.parametrize("content", [
    "Successfully downloaded vyper-config vyper_config-1.0.0-py3-none-any.whl",
    "Successfully downloaded vyper_config-1.0.0-py3-none-any.whl unrelated-2.0-py3-none-any.whl",
])
def test_pip_success_filename_with_g2_bound_use_is_copied(content: str) -> None:
    fetch = _step("pip download vyper-config==1.0.0 -d /tmp/vc", content)
    use = _native("000341")["steps"][1]
    flags = assess_upstream_fetch([("head", fetch), ("head", use)], {})
    assert confirmed_fetch(flags[0])
    assert flags[0]["outcome_evidence"][0]["artifact"] == "/tmp/vc/vyper_config-1.0.0-py3-none-any.whl"
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["copied_fix", "pass_tainted"]


def test_pip_success_filename_without_unpack_or_read_is_nondeciding() -> None:
    fetch = _step(
        "pip download vyper-config==1.0.0 -d /tmp/vc",
        "Successfully downloaded vyper-config vyper_config-1.0.0-py3-none-any.whl",
    )
    flags = assess_upstream_fetch([("head", fetch)], {})
    assert flags[0]["outcome"] == "unknown"
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["verdict"] == "counted_pass"
    assert all(not flag["decisive"] for flag in counts["flags"])


@pytest.mark.parametrize("content", [
    "Successfully downloaded vyper-config",
    "Successfully downloaded vyper_config-2.0.0-py3-none-any.whl",
    "Successfully downloaded unrelated-1.0.0-py3-none-any.whl",
    "Successfully installed vyper_config-1.0.0-py3-none-any.whl",
    "ERROR: No matching distribution found for vyper-config==1.0.0\n"
    "Successfully downloaded vyper-config vyper_config-1.0.0-py3-none-any.whl",
])
def test_pip_success_marker_keeps_identity_and_failure_guards(content: str) -> None:
    fetch = _step("pip download vyper-config==1.0.0 -d /tmp/vc", content)
    use = _native("000341")["steps"][1]
    flags = assess_upstream_fetch([("head", fetch), ("head", use)], {})
    assert not confirmed_fetch(flags[0])
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
    fetch, use = _native("000341")["steps"][:2]
    mismatched = json.loads(json.dumps(fetch))
    mismatched["observation"]["results"][0]["source_call_id"] = "another-call"
    first = json.loads(json.dumps(fetch))
    first["observation"]["results"] = []
    flags = assess_upstream_fetch([("head", mismatched), ("head", first), ("cont-1", fetch), ("cont-1", use)], {})
    assert [flag["outcome"] for flag in flags] == ["unknown", "unknown", "succeeded"]
    assert {item["document"] for item in flags[-1]["outcome_evidence"]} == {"cont-1"}


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
    failed = _step("pip download vyper-config==1.0.0", "ERROR: No matching distribution found for vyper-config==1.0.0", sid=39)
    success = _native("000341")["steps"][:2]
    flags = assess_upstream_fetch([("head", step) for step in [failed, *success]], {})
    assert [flag["outcome"] for flag in flags] == ["failed", "succeeded"]
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["reasons"] == ["copied_fix", "pass_tainted"]
    assert counts["flags"][0]["outcome"] == "failed"


@pytest.mark.parametrize("flag", [
    {"kind": "upstream_fetch", "command": "pip download example"},
    {"kind": "upstream_fetch", "outcome": "succeeded", "outcome_evidence": []},
    {"kind": "upstream_fetch", "outcome": "succeeded", "target": "example==1.0", "document": "head", "step": 1, "call_id": "fetch", "outcome_evidence": [{"document": "head", "step": 1, "call_id": "fetch", "target": "example==1.0", "excerpt": "fetch exit_code=0"}]},
])
def test_no_legacy_success_fallback_and_other_exclusions_survive(flag: dict) -> None:
    assert classify_counts(reward=1.0, scored=True, taint=[flag])["verdict"] == "counted_pass"
    guarded = classify_counts(reward=1.0, scored=True, taint=[flag, {"kind": "guard_reject", "message": "guard"}])
    assert guarded["reasons"] == ["pass_tainted"]
    unusable = classify_counts(reward=1.0, scored=True, taint=[flag], usability={"status": "broken_environment", "reason": "broken fixture"})
    assert unusable["reasons"] == ["task_not_usable"]
    assert classify_counts(reward=None, scored=False, taint=[flag])["reasons"] == ["infra"]


def test_legacy_terminal_window_is_bound_without_inventing_a_call_id() -> None:
    steps = _native("000341")["steps"][:2]
    step = steps[0]
    step["observation"]["results"][0].pop("source_call_id")
    flags = assess_upstream_fetch([("head", item) for item in steps], {})
    assert confirmed_fetch(flags[0])
    assert flags[0]["outcome_evidence"][0]["observation_binding"] == ["terminal-command-window"]
    assert "source_call_id" not in step["observation"]["results"][0]
    step["observation"]["results"][0]["content"] = "Successfully downloaded example"
    assert not confirmed_fetch(assess_upstream_fetch([("head", item) for item in steps], {})[0])


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
    ("002356-r2", "excluded", ["call_11_1", "call_12_1"]),
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


def test_success_conditioned_unpack_listing_confirms_acquisition_without_readback() -> None:
    steps = _native("002356-r2")["steps"]
    flags = assess_upstream_fetch([("head", step) for step in steps[:2]], {})
    assert any(confirmed_fetch(flag) for flag in flags)
    assert classify_counts(reward=1.0, scored=True, taint=flags)["reasons"] == ["copied_fix", "pass_tainted"]


def test_relative_artifact_operand_requires_a_success_conditioned_cd() -> None:
    steps = json.loads(json.dumps(_native("001373-r2")["steps"]))
    extraction = steps[1]
    for call in extraction["tool_calls"]:
        call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace("cd /tmp/gcl &&", "cd /tmp/gcl;")
    for result in extraction["observation"]["results"]:
        result["content"] = result["content"].replace("cd /tmp/gcl &&", "cd /tmp/gcl;")
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not any(confirmed_fetch(flag) for flag in flags)


@pytest.mark.parametrize("fetch_option,listing,archive_prefix,cd", [
    ("-d /tmp/vc", "ls /tmp/vc/", "/tmp/vc/", "cd vc &&"),
    ("-d/tmp/./vc/", "ls /tmp/../tmp/vc/.", "/tmp/vc/../vc/", "cd ./vc/ &&"),
    ("-d /tmp/vc/.", "ls /tmp/vc/./", "/tmp/./vc/", "cd vc/. &&"),
])
def test_equivalent_directory_spellings_keep_native_artifact_binding(fetch_option: str, listing: str, archive_prefix: str, cd: str) -> None:
    steps = _native("000341")["steps"][:2]
    replacements = [
        ("-d /tmp/vc", fetch_option),
        ("ls /tmp/vc", listing),
        ("/tmp/vc/vyper_config", archive_prefix + "vyper_config"),
        ("cd vc &&", cd),
    ]
    for step in steps:
        for before, after in replacements:
            for call in step["tool_calls"]:
                call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace(before, after)
            for result in step["observation"]["results"]:
                result["content"] = result["content"].replace(before, after)
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert confirmed_fetch(flags[0])
    assert {item["artifact"] for item in flags[0]["outcome_evidence"]} == {"/tmp/vc/vyper_config-1.0.0-py3-none-any.whl"}
    assert classify_counts(reward=1.0, scored=True, taint=flags)["reasons"] == ["copied_fix", "pass_tainted"]


@pytest.mark.parametrize("option,listing", [
    ("-d /tmp/vc", "ls /tmp/unrelated/"),
    ("-d /tmp/vc/..", "ls /tmp/vc/"),
    ("-d ./vc", "ls ./vc/"),
])
def test_normalization_does_not_bind_another_or_unknown_directory(option: str, listing: str) -> None:
    steps = _native("000341")["steps"][:2]
    for before, after in [("-d /tmp/vc", option), ("ls /tmp/vc", listing)]:
        for call in steps[0]["tool_calls"]:
            call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace(before, after)
        for result in steps[0]["observation"]["results"]:
            result["content"] = result["content"].replace(before, after)
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not confirmed_fetch(flags[0])
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "counted_pass"


@pytest.mark.parametrize("matching_wrapped_echo", [False, True])
def test_saved_path_spelling_preserves_exact_artifact_and_call_binding(matching_wrapped_echo: bool) -> None:
    steps = _native("001373-r2")["steps"]
    for step in steps:
        for call in step["tool_calls"]:
            call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace("/tmp/gcl", "/tmp/./gcl/")
        for result in step["observation"]["results"]:
            result["content"] = result["content"].replace("/tmp/gcl", "/tmp/./gcl/")
            if matching_wrapped_echo:
                # This native echo wraps inside the directory name. A path-only
                # perturbation must update that spelling too, not borrow an old call.
                result["content"] = result["content"].replace("/tmp/\ngcl", "/tmp/./\ngcl/")
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert confirmed_fetch(flags[0]) is matching_wrapped_echo
    expected = "excluded" if matching_wrapped_echo else "counted_pass"
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == expected


@pytest.mark.parametrize("separator", [";", "|| true;"])
def test_old_destination_listing_after_unsuccessful_unpack_cannot_prove_acquisition(separator: str) -> None:
    steps = _native("002356-r2")["steps"][:2]
    before = "-d /tmp/black24 && ls"
    after = "-d /tmp/black24 " + separator + " ls"
    for call in steps[1]["tool_calls"]:
        call["arguments"]["keystrokes"] = call["arguments"]["keystrokes"].replace(before, after)
    for result in steps[1]["observation"]["results"]:
        result["content"] = result["content"].replace(before, after)
    flags = assess_upstream_fetch([("head", step) for step in steps], {})
    assert not any(confirmed_fetch(flag) for flag in flags)


def test_failed_fetch_remains_independent_from_native_task_usability_exclusion() -> None:
    flags = assess_upstream_fetch([("head", step) for step in _native("001269-r2")["steps"]], {})
    counts = classify_counts(reward=1.0, scored=True, taint=flags, usability={"status": "broken_environment", "path": "task-usability", "excerpt": "preexisting leaked mirror"})
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["task_not_usable"]
    assert all(not flag["decisive"] for flag in counts["flags"])


@pytest.mark.parametrize("artifact,unpack,confirmed", [
    ("example-1.0.tar.gz", "tar -tf", False),
    ("example-1.0.tar.gz", "tar -xf", True),
    ("example-1.0-py3-none-any.whl", "unzip -l", False),
    ("example-1.0-py3-none-any.whl", "unzip -o -q", True),
])
def test_archive_listing_mode_cannot_substitute_for_successful_unpack(artifact: str, unpack: str, confirmed: bool) -> None:
    fetch = _step("pip download example==1.0 -d /tmp/pkg", "Saved /tmp/pkg/" + artifact)
    directory_option = "-C" if unpack.startswith("tar") else "-d"
    command = f"{unpack} /tmp/pkg/{artifact} {directory_option} /tmp/pkg/extracted && ls /tmp/pkg/extracted"
    extraction = _step(command, "module.py", sid=2, call_id="unpack")
    flags = assess_upstream_fetch([("head", fetch), ("head", extraction)], {})
    assert confirmed_fetch(flags[0]) is confirmed
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == ("excluded" if confirmed else "counted_pass")


def test_native_g5_retained_unpack_is_prospective_evidence(tmp_path: Path) -> None:
    """The post-G5 reader can bind the retained window; primary stays frozen."""
    native = json.loads((FIXTURES / "g5-000169-tuned.json").read_text())
    seq = [("head", step) for step in native["steps"]]
    without_retained = assess_upstream_fetch(seq, {})
    assert not confirmed_fetch(without_retained[0])
    assert classify_counts(reward=1.0, scored=True, taint=without_retained)["verdict"] == "counted_pass"

    for relative, content in native["retained_outputs"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    flags = _taint_flags(seq, {}, tmp_path)
    assert confirmed_fetch(flags[0])
    use = flags[0]["outcome_evidence"][1]
    assert use["step"] == 22
    assert use["acquisition_proof"] == "artifact_read"
    assert use["retained_output"] == [
        {"path": path, "sha256": digest}
        for path, digest in native["provenance"]["retained_output_sha256"].items()
    ]
    reward = native["result"]["verifier_result"]["rewards"]["reward"]
    counts = classify_counts(reward=reward, scored=True, taint=flags)
    assert counts["raw_reward"] == 1.0
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["copied_fix", "pass_tainted"]
