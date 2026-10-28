"""Task-level grader issues from the cloud trace DB ``task_issues`` table.

One item per ``task_issues`` row. The trust tier decides how the label was
established; ``status`` decides polarity: ``confirmed`` and ``provisional``
rows claim the issue is present (positive), ``refuted`` rows are real negatives
(someone claimed the issue and it did not hold up).

The tier 1 + tier 2 ``confirmed`` rows are the "verified grader issues" count.
The reproducer script body is never copied into the bank, only whether one
exists; the row stays readable through ``provenance.ref`` (the ``issue_id``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

from evallab.failure_library.bank import (
    BankContext,
    Granularity,
    IssueItem,
    LabelType,
    Provenance,
    SourceSummary,
    new_item,
)
from evallab.failure_library.records import Split

SOURCE = "cloud-task-issues"

_STORE = "cloud-trace-db:task_issues"
_KIND = {"wrong_pass": "grader_wrong_pass", "unfair_fail": "grader_unfair_fail"}
_POLARITY = {"confirmed": "positive", "provisional": "positive", "refuted": "negative"}
_LABEL_TYPE: dict[str, LabelType] = {
    "tier1_container_verified": "executed",
    "tier2_consensus_labeled": "consensus",
    "tier3_provisional": "judge",
}
# Cloud corpus_tasks spells the held-out split "held"; the bank calls it "heldout".
_DECLARED_SPLIT = {"dev": "DEV", "test": "TEST", "held": "heldout", "heldout": "heldout"}
# Most restrictive first: a task is only as DEV as its least DEV identity.
_SPLIT_RESTRICTION: tuple[Split, ...] = ("TEST", "heldout", "cross_test", "unassigned", "DEV")
_TEST_DIRS = frozenset({"tests", "test"})
_TEST_NAME = re.compile(r"^(test_.*|.*_tests?\..*|tests?\.[a-z0-9]+|test_outputs\..*)$", re.IGNORECASE)
_CODE_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".js", ".ts", ".rs", ".go", ".c", ".cc", ".cpp", ".scala", ".java", ".rb"}
)

_QUERY = """
SELECT i.issue_id, i.task_id, i.defect_class, i.direction, i.status, i.witness_digest,
       i.original_reward, i.repaired_reward, i.citation_file, i.citation_line,
       i.reproducer_script IS NOT NULL AND i.reproducer_script != '' AS has_reproducer,
       i.cas_uri, i.trust_tier, c.dataset_id, c.split, c.metadata_json
FROM task_issues AS i LEFT JOIN corpus_tasks AS c ON c.task_id = i.task_id
ORDER BY i.issue_id
"""


def citation_granularity(citation_file: str | None) -> Granularity:
    """``test`` when the citation points at a test file, otherwise ``task``."""
    if not citation_file:
        return "task"
    path = PurePosixPath(citation_file)
    if _TEST_NAME.match(path.name):
        return "test"
    in_test_dir = any(part in _TEST_DIRS for part in path.parts[:-1])
    return "test" if in_test_dir and path.suffix.lower() in _CODE_SUFFIXES else "task"


def _variant_identities(metadata_json: str | None) -> list[tuple[str, str | None]]:
    """``(record id, package digest)`` of every admitted variant of a corpus task.

    MiMo records are keyed by task plus historical package digest, so the cloud
    task id alone under-identifies them; every admitted variant is checked.
    """
    if not metadata_json:
        return []
    try:
        metadata = json.loads(metadata_json)
    except ValueError:
        return []
    found: list[tuple[str, str | None]] = []
    variants = metadata.get("variants") if isinstance(metadata, dict) else None
    for variant in variants or []:
        admission = variant.get("admission") if isinstance(variant, dict) else None
        if isinstance(admission, dict) and isinstance(admission.get("item_id"), str):
            digest = admission.get("source_task_digest")
            found.append((admission["item_id"], digest if isinstance(digest, str) else None))
    return found


def _strict_split(
    ctx: BankContext,
    declared: str,
    dataset_id: str | None,
    task_id: str,
    identities: list[tuple[str, str | None]],
) -> Split:
    """The most restrictive strict split over the task and every admitted variant."""
    key_id = dataset_id or None
    results = [ctx.strict_split(declared, key_id=key_id, item_id=task_id, task_id=task_id)]
    results += [
        ctx.strict_split(
            declared, key_id=key_id, item_id=item_id, task_id=task_id, task_digest=digest
        )
        for item_id, digest in identities
    ]
    return min(results, key=_SPLIT_RESTRICTION.index)


def _note(row: sqlite3.Row) -> str:
    parts = [
        "witness_digest present" if row["witness_digest"] else "no witness_digest",
        "reproducer present" if row["has_reproducer"] else "no reproducer",
    ]
    if row["dataset_id"]:
        parts.append(f"dataset {row['dataset_id']}")
    if row["original_reward"] is not None and row["repaired_reward"] is not None:
        parts.append(f"reward {row['original_reward']:g}->{row['repaired_reward']:g}")
    return "; ".join(parts)


def _item(ctx: BankContext, row: sqlite3.Row, identities: list[tuple[str, str | None]]) -> IssueItem:
    declared = _DECLARED_SPLIT.get((row["split"] or "").lower(), "unassigned")
    label_status = row["status"]
    label = f"{row['trust_tier']}:{label_status}:{row['defect_class']}"
    locator = row["issue_id"]
    return new_item(
        source=SOURCE,
        kind=_KIND[row["direction"]],
        polarity=_POLARITY[label_status],
        label_type=_LABEL_TYPE[row["trust_tier"]],
        label=label,
        granularity=citation_granularity(row["citation_file"]),
        subject_type="task",
        subject_id=row["task_id"],
        task_id=row["task_id"],
        locator=locator,
        declared_split=declared,
        split=_strict_split(ctx, declared, row["dataset_id"], row["task_id"], identities),
        license=None,
        actor_transcript=None,
        harness_selector=None,
        provenance=Provenance(store=_STORE, ref=row["issue_id"], note=_note(row)),
    )


def build(ctx: BankContext) -> Iterator[IssueItem | SourceSummary]:
    connection = sqlite3.connect(f"file:{ctx.cloud_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    identity_cache: dict[str, list[tuple[str, str | None]]] = {}
    try:
        for row in connection.execute(_QUERY):
            task_id: Any = row["task_id"]
            if task_id not in identity_cache:
                identity_cache[task_id] = _variant_identities(row["metadata_json"])
            yield _item(ctx, row, identity_cache[task_id])
    finally:
        connection.close()
