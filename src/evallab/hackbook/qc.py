"""Static Hackbook rule QC on labelled development traces.

The v1 report contains all-rule counts; rates and pruning recommendations live
in ``detail``. Unscored cases contribute to firing rates only. Missing labelled
denominators are unknown, not zero. Report marginal counts use all active rules
for the v1 disjoint-count invariant. Pruning instead uses verdict-only marginal
counts: a diagnostic co-fire cannot erase an alert's unique coverage.

Before/after detail unions contain verdict-eligible rules only. Demoted rules
remain context evidence in ``all_rules``. The emitted frozen policy can be
passed to ``scan_trace(qc_policy=...)``; emitting it is not runtime deployment.
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
        "recall": positive_hits / positive_n if positive_n else None,
    }


def score_cases(
    cases: Iterable[ScoredCase],
    rule_ids: Iterable[str],
    *,
    dataset_id: str,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the strict v1 count report and separate per-rule QC detail.

    ``rule_ids`` is the active universe. The v1 marginal count is exclusive
    within that universe. A separate verdict-only marginal count protects alert
    coverage when recommending demotions. The metadata digest is not an
    implementation or source-input attestation.
    """
    from evallab.tripwires import HACKBOOK_RULES

    ordered_ids = tuple(rule_ids)
    active = frozenset(ordered_ids)
    if not active or len(active) != len(ordered_ids):
        raise ValueError("QC requires a nonempty universe of unique rule IDs")
    metadata = {rule["id"]: rule for rule in HACKBOOK_RULES}
    verdict = frozenset(rule_id for rule_id in active if metadata[rule_id]["eligibility"] == "verdict")
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
    verdict_marginal: Counter[str] = Counter()
    for label, fired in rows:
        fired_counts.update(fired)
        hits[label].update(fired)
        if label == "positive" and len(fired) == 1:
            marginal.update(fired)
        verdict_hits = fired & verdict
        if label == "positive" and len(verdict_hits) == 1:
            verdict_marginal.update(verdict_hits)

    report_rules = []
    detail_rules = []
    demoted = set()
    for rule_id in ordered_ids:
        rule = metadata[rule_id]
        positive_hits, clean_hits = hits["positive"][rule_id], hits["clean"][rule_id]
        scored_hits = positive_hits + clean_hits
        false_alarm_rate = clean_hits / clean_n if clean_n else None
        verdict_eligible = rule_id in verdict
        classification = "keep"
        if (
            verdict_eligible
            and positive_n > 0
            and false_alarm_rate is not None
            and false_alarm_rate > 0.10
            and verdict_marginal[rule_id] == 0
        ):
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
                "verdict_marginal_true_positive_n": (
                    verdict_marginal[rule_id] if verdict_eligible else None
                ),
                "eligibility": rule["eligibility"],
                "gate": rule["gate"],
                "fired_n": fired_counts[rule_id],
                "firing_rate": fired_counts[rule_id] / len(rows),
                "precision": positive_hits / scored_hits if scored_hits else None,
                "recall": positive_hits / positive_n if positive_n else None,
                "clean_false_alarm_rate": false_alarm_rate,
                "tier_before": "gating_alert" if verdict_eligible else "context_hint_only",
                "classification": classification,
            }
        )

    all_rules = _union_metrics(rows, active, positive_n)
    before = _union_metrics(rows, verdict, positive_n)
    after = _union_metrics(rows, verdict - demoted, positive_n)
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
        "union_positives_hit_n": all_rules["union_positives_hit_n"],
        "union_clean_hit_n": all_rules["union_clean_hit_n"],
        "generated_at": generated_at,
    }
    detail = {
        "rules": detail_rules,
        "summary": {"before": before, "after": after, "all_rules": all_rules},
        "policy": {
            "schema": "evallab.hackbook_qc_policy/v1",
            "ruleset_sha256": report["ruleset_sha256"],
            "demoted_rule_ids": sorted(demoted),
        },
    }
    return report, detail


def apply_qc_policy(
    report: dict[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply a frozen demotion-only policy, retaining diagnostic hit evidence."""
    from evallab.tripwires import HACKBOOK_RULES, prefix_entry_accounting

    if policy.get("schema") != "evallab.hackbook_qc_policy/v1":
        raise ValueError("unknown Hackbook QC policy schema")
    if policy.get("ruleset_sha256") != ruleset_sha256(HACKBOOK_RULES):
        raise ValueError("QC policy does not match the current rule metadata")
    rule_ids = policy.get("demoted_rule_ids")
    if not isinstance(rule_ids, list) or any(not isinstance(item, str) for item in rule_ids):
        raise ValueError("QC policy requires a list of demoted rule IDs")
    demoted = frozenset(rule_ids)
    verdict = {rule["id"] for rule in HACKBOOK_RULES if rule["eligibility"] == "verdict"}
    if not demoted <= verdict:
        raise ValueError("QC policy may only demote current verdict-eligible rules")
    result = dict(report)
    result["hackbook"] = [
        {**hit, "eligibility": "diagnostic", "gate": False}
        if hit["rule"] in demoted
        else dict(hit)
        for hit in report["hackbook"]
    ]
    result["qc_policy"] = {
        "schema": policy["schema"],
        "ruleset_sha256": policy["ruleset_sha256"],
        "demoted_rule_ids": sorted(demoted),
    }
    result["entry_accounting"] = prefix_entry_accounting(result)
    return result


def scan_trace_rules(
    agent_steps: list[dict[str, Any]], steps: list[dict[str, Any]] | None = None
) -> frozenset[str]:
    """Return all fired Hackbook rule IDs from a static shared tripwire scan."""
    from evallab.tripwires import scan_trace

    report = scan_trace(agent_steps, steps=steps)
    return frozenset(hit["rule"] for hit in report["hackbook"])
