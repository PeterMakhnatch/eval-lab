"""Issue-bank source: every Failure Library ``Finding`` as a task-level item.

A finding is a claim that one pinned task version is broken, so it is a
``positive`` item. The one place the library marks a claim as contradicted is the
source-native outcome that the MiMo defect-map importer copies into the summary
(``issue outcome: refuted``); those claims are real ``negative`` items.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

from evallab.failure_library.bank import BankContext, IssueItem, Kind, LabelType, new_item
from evallab.failure_library.records import Direction, EvidenceKind, Finding
from evallab.failure_library.store import LibraryStore

SOURCE = "library-findings"

_KIND_BY_DIRECTION: dict[Direction, Kind] = {
    "wrong_pass": "grader_wrong_pass",
    "unfair_fail": "grader_unfair_fail",
    "other": "task_defect",
}

#: Evidence kind -> bank label type, strongest first (the tier order of the library).
_LABEL_TYPE_BY_EVIDENCE: tuple[tuple[EvidenceKind, LabelType], ...] = (
    ("executed", "executed"),
    ("upstream_fix", "upstream_fix"),
    ("human_label", "human"),
    ("construction", "construction"),
    ("judge_label", "judge"),
    ("static_finding", "static"),
)

_REFUTED = re.compile(r"issue outcome:\s*refuted\b", re.IGNORECASE)
#: A pytest node id in the locator names one specific test.
_TEST_LOCATOR = re.compile(r"\S::\S")


def label_type_for(finding: Finding) -> LabelType:
    """The label type of the strongest piece of evidence on ``finding``."""
    kinds = {evidence.kind for evidence in finding.evidence}
    return next(label for kind, label in _LABEL_TYPE_BY_EVIDENCE if kind in kinds)


def is_refuted(finding: Finding) -> bool:
    """True when the source itself records the claim as refuted (not a defect)."""
    return bool(_REFUTED.search(finding.summary))


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(ctx: BankContext) -> Iterator[IssueItem]:
    store = LibraryStore.for_repo(ctx.repo_root, state_root=ctx.state_root)
    for collection in store.collections("findings"):
        digest = _file_digest(store.findings_path(collection))
        for finding in store.iter_findings(collection):
            task = finding.task
            subject_id = f"{task.source_id}/{task.task_id}@{task.upstream_revision}"
            yield new_item(
                source=SOURCE,
                kind=_KIND_BY_DIRECTION[finding.direction],
                polarity="negative" if is_refuted(finding) else "positive",
                label_type=label_type_for(finding),
                label=f"{finding.defect_class} {finding.tier}",
                granularity="test" if _TEST_LOCATOR.search(finding.locator) else "task",
                subject_type="task",
                subject_id=subject_id,
                task_id=task.task_id,
                locator=finding.finding_id,
                declared_split=finding.split,
                split=ctx.strict_split(
                    finding.split,
                    task_id=task.task_id,
                    task_digest=task.task_version_digest,
                ),
                license=finding.license,
                actor_transcript=None,
                harness_selector=None,
                provenance={
                    "store": f"failure-library:findings/{collection}",
                    "ref": finding.finding_id,
                    "digest": digest,
                },
            )
