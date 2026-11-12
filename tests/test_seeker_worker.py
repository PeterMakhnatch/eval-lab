"""Stored witness replay and immutable custody, without an attacker or live queue."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from evallab.production.seeker_worker import (
    ResolvedSeekerTask,
    SeekerAttemptCandidate,
    SeekerEvidenceError,
    SeekerRefusalError,
    _evidence_custody_key,
    _parse_omp_json_stdout,
    _pin_evidence,
    _stream_served_route,
    compute_candidate_digest,
    require_cross_key_dev_only,
    verify_differential_replay,
)




def _make_task(root: Path, name: str) -> Path:
    task = root / name
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[task]\nname = "fixture"\n')
    return task


def _task(tmp_path: Path, kind="package") -> ResolvedSeekerTask:
    return ResolvedSeekerTask(
        run_id="stored-witness", job_type="reward_seeker.seek_task", dataset_id="synthetic",
        item_id="fixture", task_id="fixture", split="dev", measurable=True,
        original_task=_make_task(tmp_path, "original"),
        repaired_task=_make_task(tmp_path, "repaired"), repaired_grader_kind=kind,
    )


def _candidate() -> SeekerAttemptCandidate:
    solve = "#!/bin/bash\nprintf x > /app/output.json\n"
    return SeekerAttemptCandidate(
        files={"solve.sh": solve}, mechanism="stored witness", model_route="test/synthetic",
        llm_stdout_sha256=hashlib.sha256(b"stored response").hexdigest(),
        llm_solve_sha256=hashlib.sha256(solve.encode()).hexdigest(),
        served_provider="test", served_model="synthetic",
    )








def test_reference_digest_refuses_before_grading(tmp_path):
    task, candidate = _task(tmp_path), _candidate()
    task = replace(task, reference_digest=compute_candidate_digest(candidate.ensure_submission_files()))

    def forbidden(*args, **kwargs):
        pytest.fail("Reference payload must not run")

    grade = verify_differential_replay(task, candidate, attempt_dir=tmp_path / "replay", grader_fn=forbidden)
    assert grade.failure_kind == "identical_to_reference"
    assert not grade.differential_confirmed


def test_candidate_digest_ignores_notes_and_comments_but_binds_companions():
    first = {"solve.sh": "#!/bin/bash\n# first\nprintf x\n", "notes.json": "first"}
    second = {"solve.sh": "#!/bin/bash\n# second\n  printf x  \n", "notes.json": "second"}
    assert compute_candidate_digest(first) == compute_candidate_digest(second)
    assert compute_candidate_digest(first) != compute_candidate_digest({**first, "data.txt": "changed"})




def test_split_guard_requires_affirmative_dev_and_refuses_cross_key_test(tmp_path):
    ledger = tmp_path / "assignments.csv"
    ledger.write_text("key_id,split,item_id\nfirst,dev,fixture\nsecond,test,other\n")
    require_cross_key_dev_only("fixture", assignments_csv=ledger, require_affirmative_dev=True)
    with pytest.raises(SeekerRefusalError, match="no affirmative DEV"):
        require_cross_key_dev_only("unknown", assignments_csv=ledger, require_affirmative_dev=True)
    other = tmp_path / "cross.csv"
    other.write_text("key_id,split,item_id\nfirst,dev,fixture\nsecond,test,fixture\n")
    with pytest.raises(SeekerRefusalError, match="non-DEV"):
        require_cross_key_dev_only("fixture", assignments_csv=other, require_affirmative_dev=True)


def test_omp_json_receipt_captures_final_text_and_served_model():
    message = {"role": "assistant", "provider": "test", "model": "synthetic", "content": [{"type": "text", "text": "stored"}]}
    raw = json.dumps({"type": "agent_end", "messages": [message]})
    assert _parse_omp_json_stdout(raw) == ("stored", "test", "synthetic")
    assert _stream_served_route([message, {**message, "model": "fallback"}], "test/synthetic") == ("test", "fallback")


def _trial_tree(root: Path, name: str, marker: str) -> Path:
    trial = root / name
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))
    (trial / "verifier/reward.json").write_text('{"reward": 1.0}')
    (trial / "marker.txt").write_text(marker)
    return trial


def test_same_candidate_on_changed_package_or_receipt_pins_fresh_evidence(tmp_path):
    task = _task(tmp_path)
    receipt = _trial_tree(tmp_path, "trial", "first")
    sources = {"exploit_run": str(receipt)}

    def custody():
        return _evidence_custody_key(
            job_event_id="same-job", run_id=task.run_id, task_id=task.task_id,
            candidate_digest=compute_candidate_digest(_candidate().files),
            original_task=task.original_task, repaired_task=str(task.repaired_task), sources=sources,
        )

    first = custody()
    pinned_first = Path(_pin_evidence(str(receipt), tmp_path / first))
    (task.repaired_task / "changed.txt").write_text("changed package bytes")
    second = custody()
    (receipt / "marker.txt").write_text("second")
    third = custody()
    pinned_third = Path(_pin_evidence(str(receipt), tmp_path / third))
    assert len({first, second, third}) == 3
    assert (pinned_first / "marker.txt").read_text() == "first"
    assert (pinned_third / "marker.txt").read_text() == "second"


def test_missing_declared_receipt_keeps_custody_unavailable(tmp_path):
    task = _task(tmp_path)
    with pytest.raises(SeekerEvidenceError, match="missing"):
        _evidence_custody_key(
            job_event_id="job", run_id=task.run_id, task_id=task.task_id,
            candidate_digest=compute_candidate_digest(_candidate().files),
            original_task=task.original_task, repaired_task=str(task.repaired_task),
            sources={"exploit_run": str(tmp_path / "gone")},
        )


def test_pin_evidence_refuses_symlinks_missing_and_conflicting_bytes(tmp_path):
    src = _trial_tree(tmp_path, "trial-src", "first-bytes")
    destination = tmp_path / "witness/exploit_run"
    pinned = _pin_evidence(str(src), destination)
    assert pinned == str(destination)
    assert _pin_evidence(str(src), destination) == pinned
    (destination / "marker.txt").write_text("stale-bytes")
    with pytest.raises(SeekerEvidenceError, match="conflicts"):
        _pin_evidence(str(src), destination)
    assert (destination / "marker.txt").read_text() == "stale-bytes"
    with pytest.raises(SeekerEvidenceError, match="missing"):
        _pin_evidence(str(tmp_path / "gone"), tmp_path / "w2")
    with pytest.raises(SeekerEvidenceError):
        _pin_evidence(None, tmp_path / "w2")
    link = tmp_path / "dest-link"
    link.symlink_to(src, target_is_directory=True)
    with pytest.raises(SeekerEvidenceError):
        _pin_evidence(str(src), link)
    with pytest.raises(SeekerEvidenceError):
        _pin_evidence(str(link), tmp_path / "w3")
    linked_src = _trial_tree(tmp_path, "trial-linked", "linked")
    (linked_src / "escape").symlink_to(tmp_path / "original")
    with pytest.raises(SeekerEvidenceError, match="symlink"):
        _pin_evidence(str(linked_src), tmp_path / "w4")


def test_pin_evidence_interrupted_copy_leaves_no_partial_destination(tmp_path, monkeypatch):
    src = _trial_tree(tmp_path, "trial-src", "full-bytes")
    destination = tmp_path / "witness/exploit_run"
    real_copy2 = shutil.copy2
    calls = []

    def interrupted(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("simulated copy interruption")
        return real_copy2(source, target)

    monkeypatch.setattr(shutil, "copy2", interrupted)
    with pytest.raises(OSError, match="simulated copy interruption"):
        _pin_evidence(str(src), destination)
    assert not destination.exists()
    assert list(destination.parent.glob("*.tmp")) == []
    monkeypatch.setattr(shutil, "copy2", real_copy2)
    assert _pin_evidence(str(src), destination) == str(destination)
    assert (destination / "marker.txt").read_text() == "full-bytes"
    assert (destination / "verifier/reward.json").read_text() == '{"reward": 1.0}'


def test_pin_file_refuses_conflicts_and_preserves_bytes(tmp_path):
    source = tmp_path / "receipt.json"
    source.write_text('{"reward": 0.0}')
    target = Path(_pin_evidence(str(source), tmp_path / "witness"))
    assert target.read_bytes() == source.read_bytes()
    source.write_text('{"reward": 1.0}')
    with pytest.raises(SeekerEvidenceError, match="conflicts"):
        _pin_evidence(str(source), target.parent)
    assert target.read_text() == '{"reward": 0.0}'
