"""Send the complete long webdev brief, with content-hash provenance.

All 632 briefs exceeding the upstream 1,500-character slice fit comfortably
inside the pinned vision model's documented context. Removing the slice
preserves tail requirements rather than pretending keyword gates check them.
The extra token cost is explicit in the staged reliability specification.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "webdev-brief-explicit@1"

#: Marker comment identifying the inserted change (also the idempotence guard).
MARKER = "webdev-brief-explicit@1"

#: Package-relative path this transform rewrites.
GRADE_REL = "tests/grade.py"

#: Upstream threshold determines which tasks require the variant.
QUERY_CAP = 1500

#: Exact parent snippet (byte-identical across the pinned webdev snapshot).
_QUERY_OLD = 'build_prompt().format(query=cfg["query"][:1500])'

_QUERY_NEW = "build_prompt().format(query=_query_prompt)"

#: Insertion point: the screenshot is on disk, the judge body not yet built.
_PROVENANCE_ANCHOR = '(V / "screenshot.jpg").write_bytes(jpg)'

_PROVENANCE_BLOCK = """\
(V / "screenshot.jpg").write_bytes(jpg)
# webdev-brief-explicit@1: every requirement reaches the judge.
import hashlib as _hashlib
_query_prompt = cfg["query"]
(V / "brief.json").write_text(json.dumps({
    "query_chars": len(_query_prompt),
    "query_sha256": _hashlib.sha256(_query_prompt.encode("utf-8")).hexdigest(),
    "truncated": False,
    "sent_chars": len(_query_prompt),
}, indent=1))"""


def build_grade_py(parent_grade_py: str) -> str:
    """Parent ``grade.py`` with explicit brief handling. Idempotent."""
    if MARKER in parent_grade_py:
        return parent_grade_py
    if parent_grade_py.count(_PROVENANCE_ANCHOR) != 1:
        raise VariantInvalid(
            "grade.py has no unique screenshot anchor; refusing brief-explicit patch"
        )
    if parent_grade_py.count(_QUERY_OLD) != 1:
        raise VariantInvalid(
            "grade.py has no unique query-cap anchor; refusing brief-explicit patch"
        )
    text = parent_grade_py.replace(_PROVENANCE_ANCHOR, _PROVENANCE_BLOCK, 1)
    return text.replace(_QUERY_OLD, _QUERY_NEW, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, dict]:
    try:
        task_name = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))["task"]["name"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        grade_py = (parent_dir / GRADE_REL).read_text(encoding="utf-8")
        cfg = json.loads((parent_dir / "tests/grade.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise VariantInvalid(f"parent grader file is missing: {exc}") from exc
    if not task_name:
        raise VariantInvalid("parent task.toml names no task")
    return task_name, grade_py, cfg


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Refuses tasks whose brief fits the cap: the patch would be a no-op.
    """
    parent = Path(parent_dir)
    task_name, grade_py, cfg = _read_parent(parent)
    query = cfg.get("query", "")
    if len(query) <= QUERY_CAP:
        raise VariantInvalid(
            f"brief fits the {QUERY_CAP}-char cap ({len(query)} chars); nothing to make explicit"
        )
    new_py = build_grade_py(grade_py)
    if new_py == grade_py:
        raise VariantInvalid("parent grade.py already carries webdev-brief-explicit@1")
    changes: dict[str, bytes | None] = {GRADE_REL: new_py.encode("utf-8")}
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "query_chars": len(query),
        "upstream_query_cap": QUERY_CAP,
        "sent_chars": len(query),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "grade_before_sha256": f"sha256:{hashlib.sha256(grade_py.encode()).hexdigest()}",
        "grade_after_sha256": f"sha256:{hashlib.sha256(new_py.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_webdev_brief_explicit(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Send the full brief instead of silently dropping requirements after "
        "1500 characters, with complete-query hash and length provenance."
    ),
    created_by: str = "webdev-brief-explicit",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``webdev-brief-explicit@1`` variant of a webdev task package."""
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
    "GRADE_REL",
    "MARKER",
    "QUERY_CAP",
    "TRANSFORM_ID",
    "build_changes",
    "build_grade_py",
    "derive_webdev_brief_explicit",
]
