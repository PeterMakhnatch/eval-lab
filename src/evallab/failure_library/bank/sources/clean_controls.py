"""Criterion-scoped human negatives from OpenAI's SWE-bench annotations.

Only criteria explicitly rated zero by all three distinct human raters qualify.
A negative is evidence about that criterion, never a task-wide clean bill.
The annotation release states no licence. Preserve ``license=None`` and local
custody, exactly as the existing positive-annotation collector does.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence

from evallab.failure_library.bank import BankContext, IssueItem, Kind, new_item
from evallab.failure_library.sources.swe_family import (
    OPENAI_SHA,
    SplitPolicy,
    pinned_bytes,
    swe_task,
)

SOURCE = "clean-controls"

_COLLECTION = "openai-swebench-verified-annotations"
_REVISION = "2024-08-12"
_ARCHIVE = "swe-bench-annotation-results.zip"
_MEMBER = "samples_with_3_annotations_public.csv"

_CRITERIA: dict[str, Kind] = {
    "underspecified": "grader_unfair_fail",
    "false_negative": "grader_unfair_fail",
    "other_major_issues": "task_defect",
}
_ALLOWED_SCORES = {
    "underspecified": {0, 1, 2, 3},
    "false_negative": {0, 1, 2, 3},
    "other_major_issues": {0, 1},
}
_NOTE = (
    "All three distinct human raters rated this criterion 0; criterion-scoped negative only, "
    "not task-wide cleanliness. Association-only task binding; audited task-package bytes are "
    "unpinned."
)


def _score(row: Mapping[str, str], criterion: str) -> float | None:
    text = (row.get(criterion) or "").strip()
    if not text:
        return None
    value = float(text)
    if value not in _ALLOWED_SCORES[criterion]:
        raise ValueError(f"Unknown OpenAI {criterion} score: {value}")
    return value


def _unanimous_zero(rows: Sequence[Mapping[str, str]], criterion: str) -> bool:
    """Three complete, distinct annotations are required for a certified negative."""
    if len(rows) != 3:
        return False
    raters = {(row.get("user_id") or "").strip() for row in rows}
    if len(raters) != 3 or "" in raters:
        return False
    scores = [_score(row, criterion) for row in rows]
    return all(score == 0 for score in scores)


def build(ctx: BankContext) -> Iterator[IssueItem]:
    """Yield unanimous-zero criterion negatives from the already-pinned ZIP only."""

    # No URL is supplied: a missing cache entry is an error, never a download.
    raw = pinned_bytes(
        _COLLECTION,
        _REVISION,
        _ARCHIVE,
        OPENAI_SHA,
        state_root=ctx.state_root,
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        text = archive.read(_MEMBER).decode("utf-8-sig")

    groups: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text)):
        groups[row["instance_id"]].append(row)

    policy = SplitPolicy(ctx.repo_root)
    for task_id in sorted(groups):
        annotations = groups[task_id]
        clean_criteria = [
            criterion for criterion in _CRITERIA if _unanimous_zero(annotations, criterion)
        ]
        if not clean_criteria:
            continue

        task = swe_task(task_id, original=True)
        declared = policy.get(task_id, "swe-bench/" + task_id)
        split = ctx.strict_split(
            declared,
            task_id=task.task_id,
            task_digest=task.task_version_digest,
        )
        subject_id = f"{task.source_id}/{task.task_id}@{task.upstream_revision}"
        for criterion in clean_criteria:
            locator = f"{_MEMBER}/{task_id}/{criterion}"
            yield new_item(
                source=SOURCE,
                kind=_CRITERIA[criterion],
                polarity="negative",
                label_type="consensus",
                label=f"{criterion}=0; 3/3 human raters",
                granularity="task",
                subject_type="task",
                subject_id=subject_id,
                task_id=task.task_id,
                locator=locator,
                declared_split=declared,
                split=split,
                license=None,
                actor_transcript=None,
                harness_selector=None,
                provenance={
                    "store": f"failure-library:raw/{_COLLECTION}/{_REVISION}",
                    "ref": locator,
                    "digest": OPENAI_SHA,
                    "note": _NOTE,
                },
            )
