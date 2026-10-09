"""Strict ``answer.md`` evidence (``general-strict-answer@1``).

``verify.py::_evidence_sc`` falls back to ``locate_deliverable(ws, *kws)``
when a listed evidence file is absent, and the keyword-hit branch returns a
match without requiring uniqueness. For ``answer.md`` the stem keyword is
``answer``, so on a null run every judge item can be graded against a
*shipped* same-keyword file (measured: a workspace PDF
``cicpa_answer_11_accounting_estimates.pdf`` resolves the ``answer`` lookup).
The agent's own text is canonical -- chat output is materialized into
``answer.md`` by ``_materialize_answer`` -- so a missing ``answer.md`` must
stay "文件缺失" (score 0), never a fuzzy hit.

The patch skips the fallback for exactly ``answer.md``; renamed real
deliverables keep the fallback. Behavior changes only when ``answer.md``
is absent *and* a shipped file matches its keywords.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "general-strict-answer@1"

#: Marker comment identifying the inserted change (also the idempotence guard).
MARKER = "general-strict-answer@1"

#: Package-relative path of the verifier this transform rewrites.
VERIFY_REL = "tests/verifier/verify.py"

#: Exact parent snippet (byte-identical across the pinned general snapshot).
_FALLBACK_OLD = """    for f in files or []:
        p = ws / f
        if not p.exists():
            kws = [w for w in _re.split(r"[·\\s_./]+", Path(f).stem) if len(w) >= 2]"""

#: Replacement: no fuzzy fallback for the canonical answer file.
_FALLBACK_NEW = """    for f in files or []:
        p = ws / f
        # general-strict-answer@1: answer.md is canonical (chat output is materialized
        # into it); a missing answer.md stays missing, never a shipped same-keyword file.
        if not p.exists() and Path(f).name != "answer.md":
            kws = [w for w in _re.split(r"[·\\s_./]+", Path(f).stem) if len(w) >= 2]"""


def build_verify_py(parent_verify_py: str) -> str:
    """Parent ``verify.py`` with the strict-answer guard. Idempotent."""
    if MARKER in parent_verify_py:
        return parent_verify_py
    if parent_verify_py.count(_FALLBACK_OLD) != 1:
        raise VariantInvalid(
            "verify.py has no unique answer-fallback anchor; refusing strict-answer patch"
        )
    return parent_verify_py.replace(_FALLBACK_OLD, _FALLBACK_NEW, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str]:
    try:
        task_name = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))["task"]["name"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        verify_py = (parent_dir / VERIFY_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {VERIFY_REL} is missing: {exc}") from exc
    if not task_name:
        raise VariantInvalid("parent task.toml names no task")
    return task_name, verify_py


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``tests/verifier/verify.py`` changes; setup and task metadata are
    byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, verify_py = _read_parent(parent)
    new_verify = build_verify_py(verify_py)
    if new_verify == verify_py:
        raise VariantInvalid("parent verify.py already carries general-strict-answer@1")
    changes: dict[str, bytes | None] = {VERIFY_REL: new_verify.encode("utf-8")}
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "verify_before_sha256": f"sha256:{hashlib.sha256(verify_py.encode()).hexdigest()}",
        "verify_after_sha256": f"sha256:{hashlib.sha256(new_verify.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_general_strict_answer(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "A missing answer.md must score as missing, never as a fuzzy match "
        "against a shipped same-keyword file. Chat output is materialized "
        "into answer.md, so the fallback only ever graded someone else's "
        "content. Other deliverables keep the renamed-file fallback."
    ),
    created_by: str = "general-strict-answer",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``general-strict-answer@1`` variant of a general task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "MARKER",
    "TRANSFORM_ID",
    "VERIFY_REL",
    "build_changes",
    "build_verify_py",
    "derive_general_strict_answer",
]
