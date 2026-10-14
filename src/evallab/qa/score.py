"""Score findings against an answer key; append rows to the scoreboard.

Definitions (per split): an item is *flagged* when any finding on its
subject carries verdict ``fail``. Recall counts flagged positives;
root-cause match additionally requires a flagged finding in the
expected category; the false-alarm rate counts flagged negatives.
Paired discrimination (trace keys with ``pair_id``) is the faulty-item
flag rate minus the clean-item flag rate. Findings on subjects outside
the key are reported as unmatched, never silently dropped.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from evallab.qa.answer_key import AnswerKey
from evallab.qa.models import AnswerKeyItem, Finding, subject_key
from evallab.schemas import ContractModel

SCOREBOARD_COLUMNS = (
    "key_id",
    "split",
    "agent",
    "items",
    "positives",
    "negatives",
    "unknown_items",
    "findings",
    "unmatched_findings",
    "recall",
    "root_cause_match",
    "false_alarm_rate",
    "paired_discrimination",
    "cost_per_item",
    "tokens_per_item",
    "scored_at",
)


class ScoreRow(ContractModel):
    """One scoreboard row: one key, one split, one findings directory."""

    key_id: str
    split: str
    agent: str = ""
    items: int
    positives: int
    negatives: int
    unknown_items: int
    findings: int
    unmatched_findings: int
    recall: float | None = None
    root_cause_match: float | None = None
    false_alarm_rate: float | None = None
    paired_discrimination: float | None = None
    cost_per_item: float = 0.0
    tokens_per_item: float = 0.0
    scored_at: str


def _flagged(findings: list[Finding]) -> bool:
    return any(finding.verdict == "fail" for finding in findings)


def _score_split(
    key: AnswerKey,
    items: list[AnswerKeyItem],
    by_subject: dict[tuple[str, str, str], list[Finding]],
    *,
    split: str,
    scored_at: str,
    total_findings: int,
) -> ScoreRow:
    positives = [item for item in items if item.expected_verdict == "fail"]
    negatives = [item for item in items if item.expected_verdict == "pass"]
    unknown = [item for item in items if item.expected_verdict == "unknown"]

    def findings_for(item: AnswerKeyItem) -> list[Finding]:
        return by_subject.get(subject_key(item.subject), [])

    recall_hits = sum(1 for item in positives if _flagged(findings_for(item)))
    root_hits = sum(
        1
        for item in positives
        if any(
            finding.verdict == "fail" and finding.category == item.expected_category
            for finding in findings_for(item)
        )
    )
    false_alarms = sum(1 for item in negatives if _flagged(findings_for(item)))

    paired_faulty = [i for i in positives if i.pair_id]
    paired_clean = [i for i in negatives if i.pair_id]
    paired = None
    if paired_faulty and paired_clean:
        paired = (
            sum(1 for i in paired_faulty if _flagged(findings_for(i))) / len(paired_faulty)
            - sum(1 for i in paired_clean if _flagged(findings_for(i))) / len(paired_clean)
        )

    matched = [finding for item in items for finding in findings_for(item)]
    n_items = len(items)
    agents = Counter(finding.reviewer.agent for finding in matched)
    return ScoreRow(
        key_id=key.key_id,
        split=split,
        agent=agents.most_common(1)[0][0] if agents else "",
        items=n_items,
        positives=len(positives),
        negatives=len(negatives),
        unknown_items=len(unknown),
        findings=total_findings,
        unmatched_findings=total_findings - len(matched),
        recall=(recall_hits / len(positives)) if positives else None,
        root_cause_match=(root_hits / len(positives)) if positives else None,
        false_alarm_rate=(false_alarms / len(negatives)) if negatives else None,
        paired_discrimination=paired,
        cost_per_item=(sum(f.reviewer.cost_usd for f in matched) / n_items) if n_items else 0.0,
        tokens_per_item=(
            sum(f.reviewer.tokens or 0 for f in matched) / n_items if n_items else 0.0
        ),
        scored_at=scored_at,
    )


def score_findings(
    key: AnswerKey,
    findings: list[Finding],
    *,
    split: str | None = None,
    scored_at: str | None = None,
) -> list[ScoreRow]:
    """Score findings against a key, one row per split (or one named split)."""
    when = scored_at or datetime.now(UTC).isoformat(timespec="seconds")
    by_subject: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        by_subject[subject_key(finding.subject)].append(finding)
    splits = [split] if split else sorted({item.split for item in key.items})
    rows: list[ScoreRow] = []
    for name in splits:
        items = [item for item in key.items if item.split == name]
        if not items:
            raise ValueError(f"answer key {key.key_id!r} has no items in split {name!r}")
        rows.append(
            _score_split(
                key,
                items,
                by_subject,
                split=name,
                scored_at=when,
                total_findings=len(findings),
            )
        )
    return rows


def append_scoreboard(rows: list[ScoreRow], scoreboard_path: Path | str) -> Path:
    """Append score rows to the scoreboard CSV (header created once)."""
    path = Path(scoreboard_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SCOREBOARD_COLUMNS))
        if not exists:
            writer.writeheader()
        for row in rows:
            data = row.model_dump()
            writer.writerow(
                {
                    column: ("" if data.get(column) is None else data[column])
                    for column in SCOREBOARD_COLUMNS
                }
            )
    return path
