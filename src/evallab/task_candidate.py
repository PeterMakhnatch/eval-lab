"""Instruction-scoped task-package candidates for the HAR-67 GEPA experiment.

A task-package candidate is a full copy of an immutable original task package
in which ONLY ``instruction.md`` differs. Environment, verifier (``tests/``),
hidden solution (``solution/``) and ``task.toml`` must be byte-identical, so
the reference reward keeps its meaning: a candidate can never win by easier
grading, and hidden verifier solutions never leak into the instruction.

Mutation boundary (HAR-67): candidates may ADD clarifying instruction text
after the original instruction verbatim. They may not change the environment,
the verifier, the solution, or task metadata. Candidate validity checks
(oracle passes, nop fails, verifier byte-identical) are separate from
optimization scores (model-trial rewards): validity gates admission of a
candidate into comparison; scores decide selection.

A materialized candidate package is byte-equivalent to running the original
package with the preamble replayed through Harbor's ``--extra-instruction-path``
(``ExperimentSpec.extra_instruction_path``): the agent sees the same
instruction bytes either way. ``materialization_matches_preamble`` proves it.

Live GEPA-loop support (LabEvaluator/proposer emitting task packages) is
intentionally out of scope: the loop's existing ``instructions`` kind already
evaluates preamble text on frozen task bytes, and no proposer route is
authorized. The replay helper in ``gepa_optimizer.intake`` accepts
``candidate_kind="task_package"`` so a retained original spec can be rebound
to a validated candidate package with run identity and prior authorization
cleared.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

INSTRUCTION_FILENAMES = ("instruction.md", "instructions.md")
FROZEN_SUBTREES = ("environment", "tests", "solution")
FROZEN_FILES = ("task.toml",)
DEFAULT_SEPARATOR = "\n\n---\n\nAdditional guidance:\n\n"
MAX_PREAMBLE_CHARS = 4000
MIN_SOLUTION_LINE_CHARS = 20


class CandidateInvalid(ValueError):
    """Raised when a task-package candidate violates the mutation boundary."""


@dataclass(frozen=True)
class TaskCandidateProvenance:
    """Pinned identities of one built candidate package."""

    original_package_digest: str
    candidate_package_digest: str
    original_instruction_sha256: str
    preamble_sha256: str
    candidate_instruction_sha256: str


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _no_symlinks(root: Path) -> list[str]:
    bad = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_symlink()]
    return bad


def instruction_path(package_dir: Path) -> Path:
    """Locate the instruction file, preferring ``instruction.md``."""
    for name in INSTRUCTION_FILENAMES:
        candidate = package_dir / name
        if candidate.is_file():
            return candidate
    raise CandidateInvalid(f"no instruction file in {package_dir}")


def _tree_files(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def trees_identical(first: Path, second: Path) -> bool:
    """Byte-compare two subtrees by relative path and content digest."""
    first_files = {p.relative_to(first).as_posix(): _sha256_file(p) for p in _tree_files(first)}
    second_files = {p.relative_to(second).as_posix(): _sha256_file(p) for p in _tree_files(second)}
    return first_files == second_files


def build_instruction_candidate(
    *,
    original_dir: Path | str,
    candidate_dir: Path | str,
    preamble_text: str,
    separator: str = DEFAULT_SEPARATOR,
) -> TaskCandidateProvenance:
    """Materialize a candidate package: original bytes plus appended preamble.

    Refuses to overwrite: candidate artifacts are immutable once built.
    """
    original = Path(original_dir)
    candidate = Path(candidate_dir)
    if candidate.exists():
        raise CandidateInvalid(f"candidate directory already exists: {candidate}")
    bad = _no_symlinks(original)
    if bad:
        raise CandidateInvalid(f"original package contains symlinks: {bad}")
    preamble = preamble_text.strip() + "\n"
    if not preamble.strip():
        raise CandidateInvalid("preamble text is empty")
    if len(preamble) > MAX_PREAMBLE_CHARS:
        raise CandidateInvalid(f"preamble exceeds {MAX_PREAMBLE_CHARS} chars")
    original_instruction_file = instruction_path(original)
    original_text = original_instruction_file.read_text(encoding="utf-8")
    candidate.mkdir(parents=True)
    shutil.copytree(original, candidate, dirs_exist_ok=True)
    target = candidate / original_instruction_file.name
    target.write_text(original_text.rstrip() + "\n" + separator + preamble, encoding="utf-8")
    from evallab.registry import task_directory_digest

    return TaskCandidateProvenance(
        original_package_digest=task_directory_digest(original),
        candidate_package_digest=task_directory_digest(candidate),
        original_instruction_sha256=_sha256_file(original_instruction_file),
        preamble_sha256=_sha256_text(preamble),
        candidate_instruction_sha256=_sha256_file(target),
    )


def preamble_of(
    *,
    original_instruction: str,
    candidate_instruction: str,
    separator: str = DEFAULT_SEPARATOR,
) -> str:
    """Extract the added preamble, requiring the original verbatim as a prefix."""
    prefix = original_instruction.rstrip() + "\n"
    if not candidate_instruction.startswith(prefix):
        raise CandidateInvalid("candidate instruction does not contain the original verbatim")
    added = candidate_instruction[len(prefix) :]
    if not added.startswith(separator):
        raise CandidateInvalid("candidate instruction does not use the preamble separator")
    return added[len(separator) :]


def _solution_lines(solution_dir: Path) -> set[str]:
    lines: set[str] = set()
    for path in _tree_files(solution_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            normalized = " ".join(line.split())
            if len(normalized) >= MIN_SOLUTION_LINE_CHARS:
                lines.add(normalized)
    return lines


def validate_task_candidate(
    *,
    original_dir: Path | str,
    candidate_dir: Path | str,
    forbidden_tokens: tuple[str, ...] = (),
    max_preamble_chars: int = MAX_PREAMBLE_CHARS,
    separator: str = DEFAULT_SEPARATOR,
) -> TaskCandidateProvenance:
    """Check the mutation boundary; return pinned provenance when valid.

    Checks, in order: no symlinks; frozen subtrees and task.toml identical
    (verifier unchanged); instruction differs but contains the original
    verbatim; the added text is bounded, leaks no solution line, and carries
    none of the caller-supplied forbidden tokens (hidden-solution material).
    """
    original = Path(original_dir)
    candidate = Path(candidate_dir)
    if not original.is_dir():
        raise CandidateInvalid(f"original directory missing: {original}")
    if not candidate.is_dir():
        raise CandidateInvalid(f"candidate directory missing: {candidate}")
    bad = _no_symlinks(candidate)
    if bad:
        raise CandidateInvalid(f"candidate package contains symlinks: {bad}")
    for subtree in FROZEN_SUBTREES:
        if not trees_identical(original / subtree, candidate / subtree):
            raise CandidateInvalid(
                f"frozen subtree differs: {subtree} (verifier must be unchanged)"
            )
    for name in FROZEN_FILES:
        if _sha256_file(original / name) != _sha256_file(candidate / name):
            raise CandidateInvalid(f"frozen file differs: {name}")
    original_bytes = instruction_path(original).read_bytes()
    candidate_bytes = instruction_path(candidate).read_bytes()
    original_text = original_bytes.decode("utf-8")
    candidate_text = candidate_bytes.decode("utf-8")
    if candidate_text == original_text:
        raise CandidateInvalid("candidate instruction is identical to the original")
    added = preamble_of(
        original_instruction=original_text,
        candidate_instruction=candidate_text,
        separator=separator,
    )
    if len(added) > max_preamble_chars:
        raise CandidateInvalid(f"added instruction exceeds {max_preamble_chars} chars")
    lowered = added.lower()
    for token in forbidden_tokens:
        if token.lower() in lowered:
            raise CandidateInvalid(f"added instruction contains forbidden token: {token!r}")
    solution_lines = _solution_lines(original / "solution")
    for line in added.splitlines():
        normalized = " ".join(line.split())
        if len(normalized) >= MIN_SOLUTION_LINE_CHARS and normalized in solution_lines:
            raise CandidateInvalid(
                f"added instruction repeats a solution line: {normalized[:60]!r}"
            )
    from evallab.registry import task_directory_digest

    return TaskCandidateProvenance(
        original_package_digest=task_directory_digest(original),
        candidate_package_digest=task_directory_digest(candidate),
        original_instruction_sha256="sha256:" + hashlib.sha256(original_bytes).hexdigest(),
        preamble_sha256=_sha256_text(added),
        candidate_instruction_sha256="sha256:" + hashlib.sha256(candidate_bytes).hexdigest(),
    )


def materialization_matches_preamble(
    *,
    original_dir: Path | str,
    candidate_dir: Path | str,
    preamble_text: str,
    separator: str = DEFAULT_SEPARATOR,
) -> bool:
    """Prove candidate instruction bytes equal original + replayed preamble."""
    original = Path(original_dir)
    candidate = Path(candidate_dir)
    original_text = instruction_path(original).read_text(encoding="utf-8")
    candidate_text = instruction_path(candidate).read_text(encoding="utf-8")
    expected = original_text.rstrip() + "\n" + separator + preamble_text.strip() + "\n"
    return candidate_text == expected
