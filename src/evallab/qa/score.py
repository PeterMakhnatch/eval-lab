"""Score findings against an answer key; append rows to the scoreboard.

Definitions (shared with the rows already on the board):

- An item is *measured* when some finding on its subject has
  ``measurement == "measured"`` and a ``pass``/``fail`` verdict. An
  unmeasured item is never counted as a pass: it only lowers
  ``n_measured`` (shown next to every number).
- An item is *flagged* when a measured finding on it says ``fail``.
- ``recall`` = flagged / measured positives (expected ``fail``).
- ``false_alarm_rate`` = flagged / measured negatives (expected ``pass``).
- ``root_cause_match`` = among flagged positives that carry a labelled
  root cause (:func:`~evallab.qa.models.expected_root_causes`), the share
  with a ``fail`` finding whose ``root_cause`` names one of them
  (normalized). ``n/a`` when the findings name no root causes at all (the
  reviewer does not attempt it — not measured, never 0) or no flagged
  positive has a labelled cause. Once a reviewer names causes anywhere, a
  flagged positive it names none for counts as a miss.
- ``category_match`` (in ``notes``) = among flagged positives, the share
  with a ``fail`` finding in the expected category.
- ``paired_discrimination`` = mean over pairs with both sides measured of
  ``flag(child) - flag(parent)`` (``pairs.jsonl``; ``n/a`` without pairs).
- ``cost_per_item_usd`` = reviewer cost of the matched findings / measured
  items.

Findings on subjects outside the key are reported in ``notes``, never
silently dropped. Rows are produced per split (``all`` plus each split in
the key) and per category (``all`` plus each expected category). A
category slice holds the items labelled with that category plus both
members of every pair of that category (a pair's category is its
``category`` or its child's), so per-category paired discrimination keeps
every pair.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from evallab.qa.answer_key import AnswerKey, item_id_of
from evallab.qa.models import (
    AnswerKeyItem,
    Finding,
    KeyPair,
    expected_root_causes,
    root_cause_hit,
    subject_key,
)
from evallab.schemas import ContractModel

SCOREBOARD_COLUMNS = (
    "date_utc",
    "key_id",
    "split",
    "category",
    "agent",
    "system",
    "recall",
    "false_alarm_rate",
    "root_cause_match",
    "paired_discrimination",
    "cost_per_item_usd",
    "n_items",
    "n_measured",
    "n_pairs",
    "n_pairs_measured",
    "status",
    "notes",
)

NOT_AVAILABLE = "n/a"


class ScoreRow(ContractModel):
    """One scoreboard row: one key, one split, one category slice."""

    date_utc: str
    key_id: str
    split: str
    category: str
    agent: str = ""
    system: str = ""
    recall: float | None = None
    false_alarm_rate: float | None = None
    root_cause_match: float | None = None
    paired_discrimination: float | None = None
    cost_per_item_usd: float = 0.0
    n_items: int
    n_measured: int
    n_pairs: int
    n_pairs_measured: int
    status: str
    notes: str = ""


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _score_slice(
    key: AnswerKey,
    items: list[AnswerKeyItem],
    pairs: list[KeyPair],
    by_subject: dict[tuple[str, str, str], list[Finding]],
    *,
    split: str,
    category: str,
    when: str,
    agent: str,
    system: str,
    notes: str,
    status: str | None,
    names_causes: bool,
) -> ScoreRow:
    by_id = {item_id_of(key.key_id, item): item for item in items}

    def measured_findings(item: AnswerKeyItem) -> list[Finding]:
        return [
            finding
            for finding in by_subject.get(subject_key(item.subject), [])
            if finding.measurement == "measured" and finding.verdict in ("pass", "fail")
        ]

    def fail_findings(item: AnswerKeyItem) -> list[Finding]:
        return [finding for finding in measured_findings(item) if finding.verdict == "fail"]

    def flagged(item: AnswerKeyItem) -> bool:
        return bool(fail_findings(item))

    measured = [item for item in items if measured_findings(item)]
    positives = [item for item in measured if item.expected_verdict == "fail"]
    negatives = [item for item in measured if item.expected_verdict == "pass"]
    flagged_positives = [item for item in positives if flagged(item)]
    category_hits = sum(
        1
        for item in flagged_positives
        if any(finding.category == item.expected_category for finding in fail_findings(item))
    )
    labelled = [
        (item, causes)
        for item in flagged_positives
        if (causes := expected_root_causes(item.model_dump(by_alias=True)))
    ]
    root_hits = sum(
        1
        for item, causes in labelled
        if any(root_cause_hit(finding.root_cause, causes) for finding in fail_findings(item))
    )

    pairs = [pair for pair in pairs if pair.parent in by_id and pair.child in by_id]
    measured_pairs = [
        pair
        for pair in pairs
        if measured_findings(by_id[pair.parent]) and measured_findings(by_id[pair.child])
    ]
    paired = (
        round(
            sum(
                int(flagged(by_id[pair.child])) - int(flagged(by_id[pair.parent]))
                for pair in measured_pairs
            )
            / len(measured_pairs),
            4,
        )
        if measured_pairs
        else None
    )

    cost = sum(
        finding.reviewer.cost_usd for item in measured for finding in measured_findings(item)
    )
    agents = Counter(
        finding.reviewer.agent for item in measured for finding in measured_findings(item)
    )
    models = Counter(
        finding.reviewer.model
        for item in measured
        for finding in measured_findings(item)
        if finding.reviewer.model
    )
    unmeasured_positives = sum(1 for item in items if item.expected_verdict == "fail") - len(
        positives
    )
    default_status = (
        "complete"
        if len(measured) == len(items)
        else f"partial: {len(measured)} of {len(items)} items measured"
    )
    note_parts = [notes] if notes else []
    if unmeasured_positives:
        note_parts.append(f"{unmeasured_positives} positive(s) unmeasured, not counted as misses")
    if flagged_positives:
        note_parts.append(f"category_match={category_hits}/{len(flagged_positives)}")
    if not names_causes:
        note_parts.append("root_cause_match n/a: findings name no root causes")
    elif labelled:
        note_parts.append(
            f"root_cause_match={root_hits}/{len(labelled)} labelled flagged positives"
        )
    return ScoreRow(
        date_utc=when,
        key_id=key.key_id,
        split=split,
        category=category,
        agent=agent or (agents.most_common(1)[0][0] if agents else ""),
        system=system or (models.most_common(1)[0][0] if models else ""),
        recall=_ratio(len(flagged_positives), len(positives)),
        false_alarm_rate=_ratio(sum(1 for item in negatives if flagged(item)), len(negatives)),
        root_cause_match=_ratio(root_hits, len(labelled)) if names_causes else None,
        paired_discrimination=paired,
        cost_per_item_usd=round(cost / len(measured), 4) if measured else 0.0,
        n_items=len(items),
        n_measured=len(measured),
        n_pairs=len(pairs),
        n_pairs_measured=len(measured_pairs),
        status=status or default_status,
        notes="; ".join(note_parts),
    )


def score_findings(
    key: AnswerKey,
    findings: list[Finding],
    *,
    split: str | None = None,
    agent: str = "",
    system: str = "",
    status: str | None = None,
    notes: str = "",
    scored_at: str | None = None,
) -> list[ScoreRow]:
    """Score findings against a key: one row per split x category slice."""
    when = scored_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_subject: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        by_subject[subject_key(finding.subject)].append(finding)
    matched_subjects = {subject_key(item.subject) for item in key.items}
    outside = sum(1 for finding in findings if subject_key(finding.subject) not in matched_subjects)
    all_notes = "; ".join(
        part for part in (notes, f"{outside} finding(s) outside the key" if outside else "") if part
    )

    names_causes = any(finding.root_cause for finding in findings)
    items_by_id = {item_id_of(key.key_id, item): item for item in key.items}

    def pair_category(pair: KeyPair) -> str | None:
        if pair.category:
            return pair.category
        child = items_by_id.get(pair.child)
        return child.expected_category if child else None

    split_names = [split] if split else ["all", *sorted({item.split for item in key.items})]
    categories = ["all", *sorted({i.expected_category for i in key.items if i.expected_category})]
    rows: list[ScoreRow] = []
    for split_name in split_names:
        in_split = [item for item in key.items if split_name in ("all", item.split)]
        if not in_split:
            raise ValueError(f"answer key {key.key_id!r} has no items in split {split_name!r}")
        for category in categories:
            slice_pairs = [
                pair for pair in key.pairs if category == "all" or pair_category(pair) == category
            ]
            members = {member for pair in slice_pairs for member in (pair.parent, pair.child)}
            slice_items = [
                item
                for item in in_split
                if category == "all"
                or item.expected_category == category
                or item_id_of(key.key_id, item) in members
            ]
            if not slice_items:
                continue
            rows.append(
                _score_slice(
                    key,
                    slice_items,
                    slice_pairs,
                    by_subject,
                    split=split_name,
                    category=category,
                    when=when,
                    agent=agent,
                    system=system,
                    notes=all_notes,
                    status=status,
                    names_causes=names_causes,
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
                    column: (NOT_AVAILABLE if data.get(column) is None else data[column])
                    for column in SCOREBOARD_COLUMNS
                }
            )
    return path
