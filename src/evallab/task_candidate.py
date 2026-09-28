"""Instruction-scoped task-package candidates for the HAR-67 GEPA experiment.

A task-package candidate is a derived task package in which ONLY
``instruction.md`` differs. Environment, verifier (``tests/``), hidden
solution (``solution/``) and ``task.toml`` must be byte-identical, so the
reference reward keeps its meaning: a candidate can never win by easier
grading, and hidden verifier solutions never leak into the instruction.

Since the task-variant cutover, candidates are created through
``evallab.task_variants.derive_task`` (transform ``instruction-candidate@1``,
``components_changed == ["instruction"]``): the package materializes into the
shared variants store and a git-tracked lineage record links it to its
original. There is exactly one way to create a derived task; this module adds
only the HAR-67 *policy* on top:

- ``build_instruction_candidate`` computes the candidate instruction bytes
  (original verbatim + separator + bounded preamble) and derives the variant.
- ``validate_task_candidate`` enforces the mutation boundary on the
  materialized package (frozen subtrees identical, original instruction
  verbatim, no solution leak, no forbidden tokens).

Mutation boundary (HAR-67): candidates may ADD clarifying instruction text
after the original instruction verbatim. Candidate validity checks (oracle
passes, nop fails, verifier byte-identical) are separate from optimization
scores (model-trial rewards): validity gates admission of a candidate into
comparison; scores decide selection.

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
from dataclasses import dataclass
from pathlib import Path

from evallab.task_variants import (
    RECORDS_DIRNAME,
    VariantRecord,
    derive_task,
)

INSTRUCTION_FILENAMES = ("instruction.md", "instructions.md")
FROZEN_SUBTREES = ("environment", "tests", "solution")
FROZEN_FILES = ("task.toml",)
DEFAULT_SEPARATOR = "\n\n---\n\nAdditional guidance:\n\n"
MAX_PREAMBLE_CHARS = 4000
MIN_SOLUTION_LINE_CHARS = 20

#: Transform identity recorded in the lineage record of every candidate.
INSTRUCTION_CANDIDATE_TRANSFORM = "instruction-candidate@1"


class CandidateInvalid(ValueError):
    """Raised when a task-package candidate violates the mutation boundary."""


@dataclass(frozen=True)
class TaskCandidateProvenance:
    """Pinned identities of one built candidate package.

    ``record``/``package_dir``/``record_path`` are set by
    ``build_instruction_candidate`` (which derives a variant); the path-only
    ``validate_task_candidate`` policy check leaves them unset.
    """

    original_package_digest: str
    candidate_package_digest: str
    original_instruction_sha256: str
    preamble_sha256: str
    candidate_instruction_sha256: str
    record: VariantRecord | None = None
    package_dir: Path | None = None
    record_path: Path | None = None


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
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def trees_identical(first: Path, second: Path) -> bool:
    """Byte-compare two subtrees by relative path and content digest."""
    first_files = {p.relative_to(first).as_posix(): _sha256_file(p) for p in _tree_files(first)}
    second_files = {p.relative_to(second).as_posix(): _sha256_file(p) for p in _tree_files(second)}
    return first_files == second_files


def candidate_instruction_text(
    *, original_instruction: str, preamble_text: str, separator: str = DEFAULT_SEPARATOR
) -> str:
    """The candidate instruction: original verbatim + separator + preamble."""
    preamble = preamble_text.strip() + "\n"
    if not preamble.strip():
        raise CandidateInvalid("preamble text is empty")
    if len(preamble) > MAX_PREAMBLE_CHARS:
        raise CandidateInvalid(f"preamble exceeds {MAX_PREAMBLE_CHARS} chars")
    return original_instruction.rstrip() + "\n" + separator + preamble


def build_instruction_candidate(
    *,
    original_dir: Path | str,
    preamble_text: str,
    separator: str = DEFAULT_SEPARATOR,
    created_by: str = "gepa-proposer",
    rationale: str | None = None,
    inputs: dict | None = None,
    repo_root: Path | str | None = None,
    records_dir: Path | str = RECORDS_DIRNAME,
    variants_root: Path | str | None = None,
) -> TaskCandidateProvenance:
    """Derive a candidate package through ``task_variants.derive_task``.

    The candidate instruction is the original verbatim plus the bounded
    preamble; everything else is copied unchanged, so the variant's
    ``components_changed`` is exactly ``["instruction"]``. Refuses to
    overwrite: re-deriving the same preamble raises because the record and
    package already exist.
    """
    original = Path(original_dir)
    if not original.is_dir():
        raise CandidateInvalid(f"original directory missing: {original}")
    bad = _no_symlinks(original)
    if bad:
        raise CandidateInvalid(f"original package contains symlinks: {bad}")
    original_instruction_file = instruction_path(original)
    original_text = original_instruction_file.read_text(encoding="utf-8")
    candidate_text = candidate_instruction_text(
        original_instruction=original_text, preamble_text=preamble_text, separator=separator
    )
    record = derive_task(
        original,
        changes={original_instruction_file.name: candidate_text.encode("utf-8")},
        transform=INSTRUCTION_CANDIDATE_TRANSFORM,
        rationale=rationale
        or (
            "Instruction candidate: append a bounded clarifying preamble to the "
            "original instruction; environment, verifier and solution unchanged."
        ),
        created_by=created_by,
        inputs={
            "preamble_sha256": _sha256_text(preamble_text.strip() + "\n"),
            "separator": separator,
            **(inputs or {}),
        },
        parent_source={"kind": "local", "path": str(original.resolve())},
        repo_root=repo_root,
        records_dir=records_dir,
        variants_root=variants_root,
    )
    if record.components_changed != ["instruction"]:
        raise CandidateInvalid(
            "candidate derivation changed components beyond the instruction: "
            f"{record.components_changed}"
        )
    from evallab.storage.paths import shared_checkout_root

    root = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    records_path = Path(records_dir) if Path(records_dir).is_absolute() else root / Path(records_dir)
    variants_store = (
        Path(variants_root)
        if variants_root is not None
        else (
            shared_checkout_root(root) / "derived" / "task-store" / "variants"
        )
    )
    variants_store = variants_store if variants_store.is_absolute() else root / variants_store
    package_dir = variants_store / record.task_slug / record.digest12
    return TaskCandidateProvenance(
        original_package_digest=record.parent.digest,
        candidate_package_digest=record.variant_digest,
        original_instruction_sha256=_sha256_file(original_instruction_file),
        preamble_sha256=_sha256_text(preamble_text.strip() + "\n"),
        candidate_instruction_sha256=_sha256_text(candidate_text),
        record=record,
        package_dir=package_dir,
        record_path=records_path / record.task_slug / f"{record.digest12}.json",
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
