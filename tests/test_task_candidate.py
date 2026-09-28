"""Focused tests for instruction-scoped task-package candidates (HAR-67).

Since the task-variant cutover, ``build_instruction_candidate`` derives its
candidate through ``evallab.task_variants.derive_task``; these tests pin the
cutover (record + materialized package) and the mutation-boundary policy that
still lives here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.gepa_optimizer.intake import replay_spec_for_candidate
from evallab.schemas import ExperimentSpec
from evallab.task_candidate import (
    INSTRUCTION_CANDIDATE_TRANSFORM,
    CandidateInvalid,
    build_instruction_candidate,
    materialization_matches_preamble,
    preamble_of,
    trees_identical,
    validate_task_candidate,
)
from evallab.task_variants import VariantExistsError, verify

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


def _scratch(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / "library" / "task-variants").mkdir(parents=True)
    variants = tmp_path / "task-store" / "variants"
    variants.mkdir(parents=True)
    return repo, repo / "library" / "task-variants", variants


def _build(
    original: Path,
    tmp_path: Path,
    *,
    preamble: str = PREAMBLE,
):
    repo, records, variants = _scratch(tmp_path)
    return build_instruction_candidate(
        original_dir=original,
        preamble_text=preamble,
        repo_root=repo,
        records_dir=Path("library/task-variants"),
        variants_root=variants,
    ), records, variants, repo


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


def test_build_changes_only_instruction_and_records_lineage(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    provenance, records, variants, repo = _build(original, tmp_path)
    candidate = provenance.package_dir
    assert candidate is not None and candidate.is_dir()
    assert trees_identical(original / "environment", candidate / "environment")
    assert trees_identical(original / "tests", candidate / "tests")
    assert trees_identical(original / "solution", candidate / "solution")
    assert (original / "task.toml").read_bytes() == (candidate / "task.toml").read_bytes()
    original_text = (original / "instruction.md").read_text(encoding="utf-8")
    candidate_text = (candidate / "instruction.md").read_text(encoding="utf-8")
    assert candidate_text != original_text
    assert candidate_text.startswith(original_text)
    assert PREAMBLE in candidate_text
    assert provenance.original_package_digest != provenance.candidate_package_digest
    assert provenance.record is not None
    record = provenance.record
    assert record.transform == INSTRUCTION_CANDIDATE_TRANSFORM == "instruction-candidate@1"
    assert record.components_changed == ["instruction"]
    assert record.parent.digest == provenance.original_package_digest
    assert record.variant_digest == provenance.candidate_package_digest
    assert provenance.record_path is not None and provenance.record_path.is_file()
    assert provenance.record_path == records / record.task_slug / f"{record.digest12}.json"
    assert json.loads(provenance.record_path.read_text("utf-8"))["schema"] == (
        "evallab.task_variant/v1"
    )
    assert materialization_matches_preamble(
        original_dir=original, candidate_dir=candidate, preamble_text=PREAMBLE
    )
    assert verify(record, parent_dir=original, repo_root=repo, variants_root=variants) == []


def test_build_refuses_rebuild_and_empty_preamble(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    repo, _records, variants = _scratch(tmp_path)
    provenance = build_instruction_candidate(
        original_dir=original,
        preamble_text=PREAMBLE,
        repo_root=repo,
        records_dir=Path("library/task-variants"),
        variants_root=variants,
    )
    with pytest.raises(VariantExistsError, match="already exists"):
        build_instruction_candidate(
            original_dir=original,
            preamble_text=PREAMBLE,
            repo_root=repo,
            records_dir=Path("library/task-variants"),
            variants_root=variants,
        )
    assert provenance.package_dir is not None
    other = tmp_path / "elsewhere"
    (other / "library" / "task-variants").mkdir(parents=True)
    with pytest.raises(CandidateInvalid, match="empty"):
        build_instruction_candidate(
            original_dir=original,
            preamble_text="   \n",
            repo_root=other,
            records_dir=Path("library/task-variants"),
            variants_root=other / "variants",
        )


def test_validate_accepts_clean_candidate(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    provenance, _records, _variants, _repo = _build(original, tmp_path)
    assert provenance.package_dir is not None
    validated = validate_task_candidate(
        original_dir=original, candidate_dir=provenance.package_dir, forbidden_tokens=FORBIDDEN
    )
    from evallab.registry import task_directory_digest

    assert validated.candidate_package_digest == task_directory_digest(provenance.package_dir)


def test_validate_rejects_verifier_change(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    provenance, _records, _variants, _repo = _build(original, tmp_path)
    candidate = provenance.package_dir
    assert candidate is not None
    (candidate / "tests" / "test.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="verifier must be unchanged"):
        validate_task_candidate(original_dir=original, candidate_dir=candidate)


def test_validate_rejects_metadata_change(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    provenance, _records, _variants, _repo = _build(original, tmp_path)
    candidate = provenance.package_dir
    assert candidate is not None
    (candidate / "task.toml").write_text('version = "2.0"\n', encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="task.toml"):
        validate_task_candidate(original_dir=original, candidate_dir=candidate)


def test_validate_rejects_identical_and_rewritten_instruction(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    with pytest.raises(CandidateInvalid, match="identical"):
        validate_task_candidate(original_dir=original, candidate_dir=original)
    provenance, _records, _variants, _repo = _build(original, tmp_path)
    rewritten = provenance.package_dir
    assert rewritten is not None
    (rewritten / "instruction.md").write_text("Totally rewritten instructions.\n", encoding="utf-8")
    with pytest.raises(CandidateInvalid, match="verbatim"):
        validate_task_candidate(original_dir=original, candidate_dir=rewritten)


def test_validate_rejects_solution_leak_and_forbidden_token(tmp_path: Path) -> None:
    original = _mini_package(tmp_path)
    leaky, _r1, _v1, _g1 = _build(
        original, tmp_path / "leak-case", preamble="First step:\ndecrypt with XOR key 0x42\nthen proceed"
    )
    assert leaky.package_dir is not None
    with pytest.raises(CandidateInvalid, match="solution line"):
        validate_task_candidate(original_dir=original, candidate_dir=leaky.package_dir)
    hinted, _r2, _v2, _g2 = _build(
        original, tmp_path / "hint-case", preamble="Hint: try the 0x42-flavoured approach today"
    )
    assert hinted.package_dir is not None
    with pytest.raises(CandidateInvalid, match="forbidden token"):
        validate_task_candidate(
            original_dir=original, candidate_dir=hinted.package_dir, forbidden_tokens=FORBIDDEN
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
