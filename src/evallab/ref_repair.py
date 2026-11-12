"""Repair weak MiMo verifiers flagged by the ref-subset probe (QA Task S2).

The ref-subset probe (``evallab.ref_subset``) flags a possible weak verifier by
exhibiting a *shortcut submission*: a proper subset of the reference fix that
still scores reward 1 (``minimal.patch`` in the report directory, plus every
intermediate drop-file / drop-block grade that passed). This module turns
such a report into a test-only task variant and re-grades it under the
2026-10-15 acceptance rule:

* the full reference fix passes **3 of 3** grades,
* the do-nothing (base) submission fails **3 of 3** grades, and
* **every** shortcut submission that passed before now fails.

Anything else is ``rejected`` (controls broke, or a shortcut still escapes),
``unrepairable`` (the proposer declines, with reasons), or ``blocked``
(infrastructure / unscored grades). When every escaping shortcut was declared
equivalent or out-of-scope with a per-ID reason (a build-config or test-fixture
file the probe counted as code, say), the genuine tests are kept and the verdict
is ``partial`` — reported, never silently ``repaired``.

Test additions come from the ChatGPT-subscription Codex proposer shared with
``verifier_repair`` (same proposal schema, same fingerprint bans, same quota
gate: ordinary usage allowed and weekly consumption at most 60%). Grades run
in the ref-subset sandbox: one throwaway Docker container at a time, no
network. Original task packages are never edited; repairs are derived
variants via ``tasks derive``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evallab.schemas import ContractModel

#: Grades of the full fix and of the do-nothing submission required by the
#: 2026-10-15 acceptance rule.
CONTROL_REPETITIONS = 3


class RefRepairError(ValueError):
    """A ref-repair step violated its contract; fail closed."""


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


class RefRepairCheck(ContractModel):
    """One sandbox grade of the repaired verifier."""

    name: str
    role: Literal["reference", "nop", "shortcut"]
    status: str
    reward: float | None = None
    seconds: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class Shortcut:
    """A previously passing partial submission.

    Either a proper subset of the reference fix (``keep`` blocks, from the
    ref-subset probe) or a real agent partial fix (``patch``: the trial's
    ``verifier/agent.diff`` against the task base).
    """

    id: str
    label: str
    keep: frozenset[str] = frozenset()
    patch: str | None = None


# --------------------------------------------------------------------------- #
# Report intake: which submissions passed before?
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Proposal context and prompt
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Validation in the ref-subset sandbox
# --------------------------------------------------------------------------- #


#: Artefacts an agent leaves in its diff that are not part of any fix attempt.
_JUNK_PATH = re.compile(
    r"(^|/)(\.venv|venv|node_modules|__pycache__|\.pytest_cache|\.git|\.mypy_cache|\.ruff_cache)/"
    r"|\.(pyc|pyo|log)$"
)


def source_only_patch(patch: str) -> str:
    """Drop environment/log artefacts from an agent diff; keep every real source edit."""
    blocks = re.split(r"(?m)^(?=diff --git )", patch)
    kept = []
    for block in blocks:
        match = re.match(r"diff --git a/(.+?) b/(.+)", block)
        if match and _JUNK_PATH.search(match.group(2)):
            continue
        kept.append(block)
    return "".join(kept)


def _checked_patch_paths(patch: str) -> list[str]:
    """Reject patch features the byte-only replay cannot preserve safely."""
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/([A-Za-z0-9_./-]+) b/([A-Za-z0-9_./-]+)", line)
            if match is None or match[1] != match[2]:
                raise RefRepairError(
                    "quoted, renamed or unsupported patch paths cannot be replayed"
                )
            path = Path(match[1])
            if path.is_absolute() or any(part in {"..", ".git"} for part in path.parts):
                raise RefRepairError("patch path must stay inside the task source tree")
            if match[1] not in paths:
                paths.append(match[1])
        elif line.startswith(
            ("old mode ", "new mode ", "rename ", "copy ", "GIT binary patch", "Binary files ")
        ):
            raise RefRepairError("mode, rename, copy or binary patches cannot be replayed")
        elif line.startswith(("new file mode ", "deleted file mode ")):
            if not line.endswith(" 100644"):
                raise RefRepairError("only regular non-executable file changes can be replayed")
        elif line.startswith("index ") and line.endswith((" 120000", " 160000")):
            raise RefRepairError("symlink or gitlink patches cannot be replayed")
    if not paths:
        raise RefRepairError("patch contains no supported source changes")
    return paths


def grade_keep_sets(
    *,
    parent_dir: Path,
    variant_dir: Path,
    report: Mapping[str, Any],
    shortcuts: Sequence[Shortcut],
    repetitions: int = CONTROL_REPETITIONS,
    grade_timeout: float = 1_800,
    log: Callable[[str], None] = print,
) -> list[RefRepairCheck]:
    """Grade full (xN), nop (xN) and each shortcut (x1) on the variant.

    Uses the existing ref-subset sandbox, with at most one Docker container
    at a time. Unscored / timed-out grades stay visible as non-scored checks.
    """
    from evallab.ref_subset import (
        Package,
        Sandbox,
        apply_reference,
        compose,
        file_change,
    )

    patch_path = Path(str((report.get("reference") or {}).get("patch") or ""))
    try:
        patch = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefRepairError(f"reference patch is missing: {exc}") from exc
    parent = Package.load(parent_dir)
    variant = Package.load(variant_dir)
    if (parent.task_id, parent.image, parent.workdir, parent.tests) != (
        variant.task_id,
        variant.image,
        variant.workdir,
        variant.tests,
    ):
        raise RefRepairError("variant does not preserve the parent task environment")
    if report.get("task_id") != parent.task_id:
        raise RefRepairError("report task differs from the grading package")
    paths = _checked_patch_paths(patch)
    checks: list[RefRepairCheck] = []

    def files_for(keep: set[str]) -> dict[str, Any]:
        return {c.path: compose(c, keep) for c in changes if c.path not in variant.tests}

    def files_for_patch(shortcut_patch: str) -> dict[str, Any]:
        cleaned = source_only_patch(shortcut_patch)
        paths = _checked_patch_paths(cleaned)
        base_files = box.read(paths)
        try:
            applied = apply_reference(cleaned, base_files)
        except ValueError as exc:
            raise RefRepairError(f"partial fix does not apply to the task base: {exc}") from exc
        return {p: d for p, d in applied.items() if p not in variant.tests}

    box = Sandbox(variant)
    try:
        box.prepare()
        base = box.read(paths)
        fixed = apply_reference(patch, base)
        changes = [file_change(path, base[path], fixed[path], set(variant.tests)) for path in paths]
        changes = [c for c in changes if c.base != c.fixed]
        block_ids = {
            block.id for change in changes for block in change.blocks if block.kind != "test"
        }
        recorded_ids = {
            str(block["id"]) for block in report.get("blocks", []) if block.get("kind") != "test"
        }
        if recorded_ids and recorded_ids != block_ids:
            raise RefRepairError("reference blocks no longer match the recorded probe")
        for shortcut in shortcuts:
            if shortcut.patch is None and (not shortcut.keep or not shortcut.keep <= block_ids):
                raise RefRepairError(f"shortcut {shortcut.id} does not match the reference blocks")
        jobs: list[
            tuple[str, Literal["reference", "nop", "shortcut"], Callable[[], dict[str, Any]], int]
        ] = [
            (
                "reference",
                "reference",
                lambda: {c.path: c.fixed for c in changes if c.path not in variant.tests},
                repetitions,
            ),
            (
                "nop",
                "nop",
                lambda: {c.path: c.base for c in changes if c.path not in variant.tests},
                repetitions,
            ),
        ]
        for s in shortcuts:
            if s.patch is not None:
                jobs.append((s.id, "shortcut", lambda s=s: files_for_patch(s.patch or ""), 1))
            else:
                jobs.append((s.id, "shortcut", lambda s=s: files_for(set(s.keep)), 1))
        for label, role, files_fn, times in jobs:
            for attempt in range(times):
                name = label if times == 1 else f"{label}-{attempt + 1}"
                try:
                    files = files_fn()
                except RefRepairError as exc:
                    checks.append(
                        RefRepairCheck(
                            name=name,
                            role=role,
                            status="error",
                            error=str(exc),
                        )
                    )
                    continue
                grade = box.grade(name, [], files, grade_timeout, tail_chars=8_000)
                log(
                    f"  {variant.task_id} {name}: reward={grade.reward} "
                    f"{grade.status} {grade.seconds:.0f}s"
                )
                expected = 1.0 if role == "reference" else 0.0
                unexpected = grade.status == "scored" and grade.reward != expected
                checks.append(
                    RefRepairCheck(
                        name=name,
                        role=role,
                        status=grade.status,
                        reward=grade.reward,
                        seconds=round(grade.seconds, 1),
                        # Failing output is the refinement evidence: why the
                        # reference rejected a new test, or what let a shortcut pass.
                        error=(grade.tail or grade.status)
                        if grade.status != "scored" or unexpected
                        else None,
                    )
                )
    finally:
        box.close()
    return checks


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

#: Grades a derived variant; injectable so tests never touch Docker.
#: Derives a test-only variant; defaults to ``tasks derive`` via verifier_repair.
#: One subscription proposal turn (prompt, context) -> (raw text, usage).


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


