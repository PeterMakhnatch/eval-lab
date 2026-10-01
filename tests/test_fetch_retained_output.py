"""Retained G2 output can restore proof, never relax its acquisition guards."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from evallab.counts import classify_counts
from evallab.loopfix import cap_output
from evallab.process_job import _taint_flags
from evallab.upstream_fetch import assess_upstream_fetch, confirmed_fetch

FIXTURE = Path(__file__).parent / "fixtures" / "upstream_fetch" / "g2-000341.json"
ARTIFACT = "/tmp/vc/vyper_config-1.0.0-py3-none-any.whl"
CAP_LIMIT = 320


def _native() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["steps"]


def _command(step: dict) -> str:
    return step["tool_calls"][0]["arguments"]["keystrokes"]


def _result(step: dict) -> dict:
    return step["observation"]["results"][0]


def _retained(step: dict, trial_dir: Path, *, marker: str | None = None, limit: int = CAP_LIMIT) -> dict:
    """Keep full output; use the production cap, with its echo omitted."""
    full = _result(step)["content"]
    filename = f"step-{step['step_id']:04d}.txt"
    relative = Path("agent") / "evallab-output" / filename
    path = trial_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = full.encode("utf-8")
    path.write_bytes(raw)
    sandbox_path = marker or f"/logs/agent/evallab-output/{filename}"
    clipped = cap_output(full, sandbox_path, limit=limit)
    assert re.sub(r"\s+", "", _command(step)) not in re.sub(r"\s+", "", clipped)
    _result(step)["content"] = clipped
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()}


def _assess(steps: list[dict], trial_dir: Path | None) -> list[dict]:
    return assess_upstream_fetch([("head", step) for step in steps], {}, trial_dir=trial_dir)


def _nondeciding(flags: list[dict], *, outcome: str = "unknown") -> None:
    assert flags[0]["outcome"] == outcome
    assert not any(confirmed_fetch(flag) for flag in flags)
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["verdict"] == "counted_pass"
    assert counts["reasons"] == []
    assert all(not flag["decisive"] for flag in counts["flags"])


def _qualified_unpack(*, output_directory: str = "/tmp/vc2") -> dict:
    # Synthetic filename-qualified grep shape, using only the G2 package/path.
    # It is not asserted to be an additional native observation.
    step = _native()[1]
    command = f"unzip -o -q {ARTIFACT} -d /tmp/vc2 && grep -n 'class' /tmp/vc2/vyper.py\n"
    step["tool_calls"][0]["arguments"]["keystrokes"] = command
    _result(step)["content"] = (
        "New Terminal Output:\n"
        f"root@g2:/tmp/vc# {command}"
        f"{output_directory}/vyper.py:19:class Vyper:\n"
        "root@g2:/tmp/vc#\n"
    )
    return step


@pytest.mark.parametrize("clipped_indices", [(0,), (1,), (0, 1)])
def test_source_bound_recovery_restores_acquisition_and_hashes(tmp_path: Path, clipped_indices: tuple[int, ...]) -> None:
    steps = _native()[:2]
    retained = {
        steps[index]["tool_calls"][0]["tool_call_id"]: _retained(steps[index], tmp_path)
        for index in clipped_indices
    }
    # Both missing trial context and a different trial must preserve uncertainty.
    _nondeciding(_assess(steps, None))
    _nondeciding(_assess(steps, tmp_path / "other-trial"))

    flags = _assess(steps, tmp_path)
    assert confirmed_fetch(flags[0])
    proof = flags[0]["outcome_evidence"]
    assert {item["artifact"] for item in proof} == {ARTIFACT}
    assert [item["call_id"] for item in proof] == ["call_38_1", "call_39_1"]
    assert {item["document"] for item in proof} == {"head"}
    for item in proof:
        if item["call_id"] in retained:
            assert item["retained_output"] == [retained[item["call_id"]]]
    counts = classify_counts(reward=1.0, scored=True, taint=flags)
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["copied_fix", "pass_tainted"]
    assert counts["evidence"][0]["observations"] == proof


def test_production_taint_assessment_recovers_retained_proof(tmp_path: Path) -> None:
    steps = _native()[:2]
    retained = _retained(steps[1], tmp_path)
    flags = _taint_flags([("head", step) for step in steps], {}, tmp_path)
    success = next(flag for flag in flags if confirmed_fetch(flag))
    assert success["outcome_evidence"][1]["retained_output"] == [retained]
    assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "excluded"


@pytest.mark.parametrize("damage", ["missing", "visible-byte", "omitted-length", "different-full-output"])
def test_missing_or_nonmatching_spill_remains_nondeciding(tmp_path: Path, damage: str) -> None:
    steps = _native()[:2]
    original = _result(steps[1])["content"]
    retained = _retained(steps[1], tmp_path)
    spill = tmp_path / retained["path"]
    if damage == "missing":
        spill.unlink()
    elif damage == "visible-byte":
        spill.write_text("X" + original[1:], encoding="utf-8")
    elif damage == "omitted-length":
        # Head/tail remain identical; the marker's omitted count does not.
        midpoint = len(original) // 2
        spill.write_text(original[:midpoint] + "extra" + original[midpoint:], encoding="utf-8")
    else:
        spill.write_text(_result(_native()[2])["content"], encoding="utf-8")
    _nondeciding(_assess(steps, tmp_path))


@pytest.mark.parametrize("damage", ["absent", "wrong-omitted-count"])
def test_spill_presence_without_exact_cap_marker_is_not_proof(tmp_path: Path, damage: str) -> None:
    steps = _native()[:2]
    _retained(steps[1], tmp_path)
    clipped = _result(steps[1])["content"]
    if damage == "absent":
        clipped = re.sub(r"\n\[\.\.\. output limited to [^\n]*\]\n", "\n[output clipped]\n", clipped)
    else:
        clipped = re.sub(r"\d+ characters omitted", "0 characters omitted", clipped)
    _result(steps[1])["content"] = clipped
    _nondeciding(_assess(steps, tmp_path))


@pytest.mark.parametrize("marker", [
    "/logs/agent/evallab-output/../evallab-output/step-0041.txt",
    "/logs/agent/evallab-output/../../outside/step-0041.txt",
    "/logs/agent/evallab-output/step-41.txt",
    "/logs/agent/other-output/step-0041.txt",
    "/tmp/evallab-output/step-0041.txt",
])
def test_only_exact_sandbox_spill_markers_are_eligible(tmp_path: Path, marker: str) -> None:
    steps = _native()[:2]
    _retained(steps[1], tmp_path, marker=marker)
    _nondeciding(_assess(steps, tmp_path))


@pytest.mark.parametrize("linked", ["file", "directory"])
def test_spill_symlinks_cannot_escape_trial(tmp_path: Path, linked: str) -> None:
    trial = tmp_path / "trial"
    steps = _native()[:2]
    retained = _retained(steps[1], trial)
    spill = trial / retained["path"]
    raw = spill.read_bytes()
    spill.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / spill.name
    target.write_bytes(raw)
    if linked == "file":
        spill.symlink_to(target)
    else:
        spill.parent.rmdir()
        spill.parent.symlink_to(outside, target_is_directory=True)
    _nondeciding(_assess(steps, trial))


@pytest.mark.parametrize("mismatch", ["source-call", "current-command", "duplicate-call", "ambiguous-command-window"])
def test_retained_output_preserves_call_and_command_binding(tmp_path: Path, mismatch: str) -> None:
    steps = _native()[:2]
    _retained(steps[1], tmp_path)
    call = steps[1]["tool_calls"][0]
    if mismatch == "source-call":
        _result(steps[1])["source_call_id"] = "different-call"
    elif mismatch == "current-command":
        call["arguments"]["keystrokes"] = _command(steps[1]).replace("/tmp/vc/", "/tmp/current/")
    else:
        duplicate = copy.deepcopy(call)
        if mismatch == "ambiguous-command-window":
            _result(steps[1]).pop("source_call_id")
            duplicate["tool_call_id"] = "another-call-with-the-same-command"
        steps[1]["tool_calls"].append(duplicate)
    _nondeciding(_assess(steps, tmp_path))


def test_unique_legacy_command_window_can_be_recovered_without_inventing_call_ids(tmp_path: Path) -> None:
    steps = _native()[:2]
    _result(steps[1]).pop("source_call_id")
    retained = _retained(steps[1], tmp_path)
    flags = _assess(steps, tmp_path)
    assert confirmed_fetch(flags[0])
    proof = flags[0]["outcome_evidence"][1]
    assert proof["observation_binding"] == ["terminal-command-window"]
    assert proof["retained_output"] == [retained]
    assert "source_call_id" not in _result(steps[1])


@pytest.mark.parametrize("boundary", ["document", "fresh-attempt", "copied-context"])
def test_recovered_output_cannot_cross_document_or_acquisition_episode(tmp_path: Path, boundary: str) -> None:
    steps = _native()[:2]
    _retained(steps[1], tmp_path)
    seq = [("head", step) for step in steps]
    if boundary == "document":
        seq[1] = ("cont-1", steps[1])
    elif boundary == "fresh-attempt":
        retry = copy.deepcopy(steps[0])
        retry["step_id"] = 99
        retry["tool_calls"][0]["tool_call_id"] = "retry"
        retry["observation"]["results"] = []
        seq.insert(1, ("head", retry))
    else:
        steps[1]["is_copied_context"] = True
    _nondeciding(assess_upstream_fetch(seq, {}, trial_dir=tmp_path))


@pytest.mark.parametrize("acquisition", ["failed", "summary-only", "wrong-artifact"])
def test_recovery_does_not_promote_failed_or_insufficient_acquisition(tmp_path: Path, acquisition: str) -> None:
    steps = _native()[:2]
    content = _result(steps[0])["content"]
    if acquisition == "failed":
        replacement = "ERROR: No matching distribution found for vyper-config==1.0.0"
    elif acquisition == "summary-only":
        replacement = "Successfully downloaded vyper-config"
    else:
        replacement = "vyper_config-2.0.0-py3-none-any.whl"
    _result(steps[0])["content"] = content.replace("vyper_config-1.0.0-py3-none-any.whl", replacement)
    _retained(steps[0], tmp_path)
    _retained(steps[1], tmp_path)
    _nondeciding(_assess(steps, tmp_path), outcome="failed" if acquisition == "failed" else "unknown")


@pytest.mark.parametrize("output_directory,confirmed", [
    ("/tmp/vc2", True), ("/tmp/old-vc", False), ("/tmp/vc20", False),
])
def test_qualified_grep_proof_is_bound_to_just_unpacked_destination(tmp_path: Path, output_directory: str, confirmed: bool) -> None:
    steps = [_native()[0], _qualified_unpack(output_directory=output_directory)]
    # The short synthetic shape also loses its echo under a real smaller cap.
    retained = _retained(steps[1], tmp_path, limit=40)
    flags = _assess(steps, tmp_path)
    if not confirmed:
        _nondeciding(flags)
    else:
        assert confirmed_fetch(flags[0])
        proof = flags[0]["outcome_evidence"][1]
        assert proof["extracted_to"] == "/tmp/vc2"
        assert proof["acquisition_proof"] == "artifact_read"
        assert proof["retained_output"] == [retained]
        assert classify_counts(reward=1.0, scored=True, taint=flags)["verdict"] == "excluded"


@pytest.mark.parametrize("qualified", [False, True])
def test_read_only_preexisting_tree_cannot_prove_this_download(tmp_path: Path, qualified: bool) -> None:
    native = _native()
    if qualified:
        read = _qualified_unpack()
        read["tool_calls"][0]["arguments"]["keystrokes"] = "grep -n 'class' /tmp/vc2/vyper.py\n"
        _result(read)["content"] = (
            "New Terminal Output:\n"
            f"root@g2:/tmp/vc# {_command(read)}"
            "/tmp/vc2/vyper.py:19:class Vyper:\n"
            "root@g2:/tmp/vc#\n"
        )
        # Quiet extraction in an earlier call plus a separate source read is
        # still not observed success-conditioned use of this download.
        unpack = copy.deepcopy(native[1])
        unpack["tool_calls"][0]["arguments"]["keystrokes"] = f"unzip -o -q {ARTIFACT} -d /tmp/vc2\n"
        _result(unpack)["content"] = ""
        read["step_id"] = 42
        read["tool_calls"][0]["tool_call_id"] = "separate-read"
        _result(read)["source_call_id"] = "separate-read"
        steps = [native[0], unpack, read]
        _retained(read, tmp_path, limit=40)
    else:
        # Native G2's later source reads, without its bound unzip call.
        steps = [native[0], native[2]]
        _retained(steps[1], tmp_path)
    _nondeciding(_assess(steps, tmp_path))
