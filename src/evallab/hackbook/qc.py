"""Static Hackbook rule QC on labelled development traces.

The v1 report contains counts only; rates and demotion recommendations live in
``detail``. Firing counts/rates include unscored cases, but precision, recall and
false-alarm rates use labelled cases only. Precision is unknown (``None``) when
there are no scored hits; an absent positive/clean denominator gives rate 0.0.

The before union includes all active rules, including diagnostic context hints.
The after union removes only rules classified ``demote_to_context_hint_only``;
it still includes every other verdict or diagnostic rule. This is a measurement
comparison, not a change to the runtime rule metadata or gating policy.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal, TypedDict


class ScoredCase(TypedDict):
    case_id: str
    label: Literal["positive", "clean", "unscored"]
    fired: frozenset[str]


def ruleset_sha256(rule_metadata: Iterable[Mapping[str, Any]]) -> str:
    """Hash the ordered rule metadata list's v1 identity/tier fields only."""
    metadata = [
        {
            "id": rule["id"],
            "eligibility": rule["eligibility"],
            "gate": rule["gate"],
            "entry_ids": list(rule["entry_ids"]),
        }
        for rule in rule_metadata
    ]
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _union_metrics(
    cases: Sequence[tuple[str, frozenset[str]]],
    rule_ids: frozenset[str],
    positive_n: int,
) -> dict[str, Any]:
    positive_hits = sum(label == "positive" and bool(fired & rule_ids) for label, fired in cases)
    clean_hits = sum(label == "clean" and bool(fired & rule_ids) for label, fired in cases)
    scored_hits = positive_hits + clean_hits
    return {
        "union_positives_hit_n": positive_hits,
        "union_clean_hit_n": clean_hits,
        "precision": positive_hits / scored_hits if scored_hits else None,
        "recall": positive_hits / positive_n if positive_n else 0.0,
    }


def score_cases(
    cases: Iterable[ScoredCase],
    rule_ids: Iterable[str],
    *,
    dataset_id: str,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the strict v1 count report and separate per-rule QC detail.

    ``rule_ids`` is the active universe: inactive hits affect neither unions nor
    marginal TP counts. A rule's marginal TP is a positive for which it is the
    only active rule firing. The digest identifies the complete current
    ``HACKBOOK_RULES`` metadata list, even when scoring an active subset.
    """
    from evallab.tripwires import HACKBOOK_RULES

    ordered_ids = tuple(rule_ids)
    active = frozenset(ordered_ids)
    if not active or len(active) != len(ordered_ids):
        raise ValueError("QC requires a nonempty universe of unique rule IDs")
    metadata = {rule["id"]: rule for rule in HACKBOOK_RULES}
    rows = [(case["label"], case["fired"] & active) for case in cases]
    cohort = Counter(label for label, _ in rows)
    positive_n, clean_n = cohort["positive"], cohort["clean"]
    if not positive_n + clean_n:
        raise ValueError("QC requires at least one positive or clean case")

    fired_counts: Counter[str] = Counter()
    hits: dict[str, Counter[str]] = {
        "positive": Counter(),
        "clean": Counter(),
        "unscored": Counter(),
    }
    marginal: Counter[str] = Counter()
    for label, fired in rows:
        fired_counts.update(fired)
        hits[label].update(fired)
        if label == "positive" and len(fired) == 1:
            marginal.update(fired)

    report_rules = []
    detail_rules = []
    demoted = set()
    for rule_id in ordered_ids:
        rule = metadata[rule_id]
        positive_hits, clean_hits = hits["positive"][rule_id], hits["clean"][rule_id]
        scored_hits = positive_hits + clean_hits
        false_alarm_rate = clean_hits / clean_n if clean_n else 0.0
        verdict_eligible = rule["eligibility"] == "verdict"
        classification = "keep"
        if verdict_eligible and false_alarm_rate > 0.10 and marginal[rule_id] == 0:
            classification = "demote_to_context_hint_only"
            demoted.add(rule_id)
        counts = {
            "rule_id": rule_id,
            "positives_hit_n": positive_hits,
            "clean_hit_n": clean_hits,
            "marginal_true_positive_n": marginal[rule_id],
        }
        report_rules.append(counts)
        detail_rules.append(
            {
                **counts,
                "eligibility": rule["eligibility"],
                "gate": rule["gate"],
                "fired_n": fired_counts[rule_id],
                "firing_rate": fired_counts[rule_id] / len(rows),
                "precision": positive_hits / scored_hits if scored_hits else None,
                "recall": positive_hits / positive_n if positive_n else 0.0,
                "clean_false_alarm_rate": false_alarm_rate,
                "tier_before": "gating_alert" if verdict_eligible else "context_hint_only",
                "classification": classification,
            }
        )

    before = _union_metrics(rows, active, positive_n)
    after = _union_metrics(rows, active - demoted, positive_n)
    report = {
        "schema": "evallab.hackbook_qc/v1",
        "ruleset_sha256": ruleset_sha256(HACKBOOK_RULES),
        "cohort": {
            "dataset_id": dataset_id,
            "split": "dev",
            "positive_n": positive_n,
            "clean_n": clean_n,
            "unscored_n": cohort["unscored"],
        },
        "rules": report_rules,
        "union_positives_hit_n": before["union_positives_hit_n"],
        "union_clean_hit_n": before["union_clean_hit_n"],
        "generated_at": generated_at,
    }
    detail = {"rules": detail_rules, "summary": {"before": before, "after": after}}
    return report, detail


def scan_trace_rules(
    agent_steps: list[dict[str, Any]], steps: list[dict[str, Any]] | None = None
) -> frozenset[str]:
    """Return all fired Hackbook rule IDs from a static shared tripwire scan."""
    from evallab.tripwires import scan_trace

    report = scan_trace(agent_steps, steps=steps)
    return frozenset(hit["rule"] for hit in report["hackbook"])
