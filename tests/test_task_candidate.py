"""Focused tests for instruction-scoped task-package candidates (HAR-67).

Deterministic; no Docker, no network, no credentials. A synthetic mini task
package stands in for the vendored db-wal-recovery original.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.gepa_optimizer.intake import replay_spec_for_candidate
from evallab.registry import task_directory_digest
from evallab.schemas import ExperimentSpec
from evallab.task_candidate import (
    CandidateInvalid,
    build_instruction_candidate,
    materialization_matches_preamble,
    preamble_of,
    trees_identical,
    validate_task_candidate,
)

PREAMBLE = "Check the WAL magic bytes with xxd before assuming corruption."
FORBIDDEN = ("0x42", "xor_decrypt")


def _mini_package(root: Path) -> Path:
    pkg = root / "original"
    (pkg / "environment").mkdir(parents=True)
    (pkg / "tests").mkdir()
    (pkg / "solution").mkdir()
    (pkg / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
    (pkg / "instruction.md").write_text("Recover the database.\n", encoding="utf-8")
    (pkg / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    (pkg / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (pkg / "solution" / "solve.sh").write_text(
        "#!/bin/bash\ndecrypt with XOR key 0x42\n", encoding="utf-8"
    )
    return pkg


def _base_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="retained-original-run",
        hypothesis="original run",
        purpose="comparison",
        task="research/exp/original",
        task_path="research/exp/original",
        task_id="db-wal-recovery",
        task_package_digest="sha256:" + "a" * 64,
        verifier_digest="sha256:" + "b" * 64,
        agent="oracle",
        submitted_by="operator",
        jobs_dir="runs",
    )


def test_build_changes_only_instruction(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    candidate = tmp_path / "candidate-v1"
    provenance = build_instruction_candidate(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    assert trees_identical(original / "environment", candidate / "environment")
    assert trees_identical(original / "tests", candidate / "tests")
    assert trees_identical(original / "solution", candidate / "solution")
    assert (original / "task.toml").read_bytes() == (candidate / "task.toml").read_bytes()
    original_text = (original / "instruction.md").read_text(encoding="utf-8")
    candidate_text = (candidate / "instruction.md").read_text(encoding="utf-8")
    assert candidate_text != original_text
    assert candidate_text.startswith(original_text)
    assert PREAMBLE in candidate_text
    assert provenance.original_package_digest == task_directory_digest(original)
    assert provenance.candidate_package_digest == task_directory_digest(candidate)
    assert provenance.original_package_digest != provenance.candidate_package_digest
    assert materialization_matches_preamble(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )


def test_build_refuses_existing_dir_and_empty_preamble(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    candidate = tmp_path / "candidate-v1"
    build_instruction_candidate(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    with pytest.raises(CandidateInvalid):
        build_instruction_candidate(
            original_dir=original, candidate_dir=candidate, preamble_text="other"
        )
    with pytest.raises(CandidateInvalid):
        build_instruction_candidate(
            original_dir=original,
            candidate_dir=tmp_path / "candidate-v2",
            preamble_text="   \n",
        )


def test_validate_accepts_clean_candidate(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    candidate = tmp_path / "candidate-v1"
    build_instruction_candidate(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    provenance = validate_task_candidate(
        original_dir=original, candidate_dir=candidate, forbidden_tokens=FORBIDDEN
    )
    assert provenance.candidate_package_digest == task_directory_digest(candidate)


def test_validate_rejects_verifier_change(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    candidate = tmp_path / "candidate-v1"
    build_instruction_candidate(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    (candidate / "tests" / "test.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="verifier must be unchanged"):
        validate_task_candidate(original_dir=original, candidate_dir=candidate)


def test_validate_rejects_metadata_change(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    candidate = tmp_path / "candidate-v1"
    build_instruction_candidate(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    (candidate / "task.toml").write_text('version = "2.0"\n', encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="task.toml"):
        validate_task_candidate(original_dir=original, candidate_dir=candidate)


def test_validate_rejects_identical_and_rewritten_instruction(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    with pytest.raises(CandidateInvalid, match="identical"):
        validate_task_candidate(original_dir=original, candidate_dir=original)
    rewritten = tmp_path / "rewritten"
    build_instruction_candidate(
        original_dir=original, candidate_dir=rewritten, preamble_text=PREAMBLE
    )
    (rewritten / "instruction.md").write_text("Totally rewritten instructions.\n", encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="verbatim"):
        validate_task_candidate(original_dir=original, candidate_dir=rewritten)


def test_validate_rejects_solution_leak_and_forbidden_token(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    leaky = tmp_path / "leaky"
    build_instruction_candidate(
        original_dir=original,
        candidate_dir=leaky,
        preamble_text="First step:\ndecrypt with XOR key 0x42\nthen proceed",
    )
    with pytest.raises(CandidateInvalid, match="solution line"):
        validate_task_candidate(original_dir=original, candidate_dir=leaky)
    hinted = tmp_path / "hinted"
    build_instruction_candidate(
        original_dir=original,
        candidate_dir=hinted,
        preamble_text="Hint: try the 0x42-flavoured approach today",
    )
    with pytest.raises(CandidateInvalid, match="forbidden token"):
        validate_task_candidate(
            original_dir=original, candidate_dir=hinted, forbidden_tokens=FORBIDDEN
        )


def test_preamble_of_requires_verbatim_prefix() -> None:
    with pytest.raises(CandidateInvalid):
        preamble_of(original_instruction="abc\n", candidate_instruction="xyz abc\n")


def test_replay_task_package_rebinds_task(tmp_path: Path) -> None:
    _ = tmp_path
    base = _base_spec()
    candidate_dir = Path("research/exp/candidate-v1")
    candidate_digest = "sha256:" + "c" * 64
    verifier_digest = "sha256:" + "b" * 64
    replayed = replay_spec_for_candidate(
        base,
        campaign_name="har67-paired",
        candidate_path=candidate_dir,
        candidate_sha256=candidate_digest,
        jobs_dir="runs",
        candidate_kind="task_package",
        candidate_verifier_digest=verifier_digest,
    )
    assert replayed.task == candidate_dir.as_posix()
    assert replayed.task_path == candidate_dir.as_posix()
    assert replayed.task_package_digest == candidate_digest
    assert replayed.verifier_digest == verifier_digest
    assert replayed.task_id == "db-wal-recovery"
    assert replayed.extra_instruction_path is None
    assert replayed.extra_instruction_sha256 is None
    assert replayed.toolbox_path is None
    assert replayed.toolbox_sha256 is None
    assert replayed.spec_id is None
    assert replayed.submitted_at is None
    assert replayed.submitted_by == "gepatask-replay"
    assert replayed.model == base.model
    assert replayed.agent == base.agent
    assert candidate_digest[:16] in replayed.hypothesis


def test_replay_task_package_rejects_bad_verifier_digest() -> None:
    base = _base_spec()
    with pytest.raises(ValueError, match="verifier_digest"):
        replay_spec_for_candidate(
            base,
            campaign_name="har67-paired",
            candidate_path=Path("research/exp/candidate-v1"),
            candidate_sha256="sha256:" + "c" * 64,
            jobs_dir="runs",
            candidate_kind="task_package",
            candidate_verifier_digest="not-a-digest",
        )
