"""Deterministic, reviewable statistical leads, not automated conclusions.

Scores rank review urgency: numeric/recompute mismatches and explicit contradictions
4; correction cascades 3.5; impossible paired significance 3; extreme small samples
and low-discordance nulls 2; missing coverage 1. Drift is min(5, -log10(Fisher p)).
Multiplicity scores 2.5. Null warnings use fewer than 20 discordant pairs, a warning
heuristic, not a retrospective power estimate. Paired significance feasibility uses
the exact all-one-way minimum. Numeric tolerances match the claim checker: value
0.006, CI 0.02, p absolute 0.002 AND relative 15%. Values with explicit percentage
units are converted to fractions. Only current, non-check-failed claims participate.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from itertools import combinations

from evallab.insights.models import (
    ClaimRecord,
    SignalKind,
    SignalRecord,
    normalize_experiment_id,
    normalize_text,
    signal_identity,
)
from evallab.insights.stats import (
    bh,
    mcnemar_exact,
    min_n_mcnemar,
    rule_of_three,
    two_proportion,
    wilson,
)

_NULL = re.compile(
    r"\b(no (?:clear |statistically )?(?:significant )?difference|not (?:statistically )?significant|"
    r"no (?:clear |significant )?(?:improvement|effect)|no evidence of (?:a )?difference)\b"
)
_DIFFERENCE = re.compile(
    r"\b(difference|better|higher|lower|outperform\w*|improv\w*|worse|increase\w*|"
    r"decrease\w*|significant|beats|superior)\b"
)
_METRICS = {"recall", "false_alarm", "precision", "auroc"}


def _normalized(value: str | None) -> str:
    return normalize_text(value or "").casefold()


def _metric(value: str | None) -> str:
    metric = _normalized(value).replace("-", "_").replace(" ", "_")
    return {"false_alarm_rate": "false_alarm", "fpr": "false_alarm",
            "false_positive_rate": "false_alarm", "tpr": "recall"}.get(metric, metric)


def _scale(record: ClaimRecord) -> float:
    unit = _normalized(record.claim.unit)
    return 100.0 if unit in {"%", "percent", "percentage", "percentage points", "pp"} else 1.0


def detect_signals(claims: Sequence[ClaimRecord], *, now: datetime) -> list[SignalRecord]:
    """Return stable-id signals, sorted by descending urgency then id."""
    current = sorted(
        (record for record in claims if not record.stale and record.check.status != "fail"),
        key=lambda record: record.claim_id,
    )
    signals: dict[str, SignalRecord] = {}

    def emit(
        kind: SignalKind,
        records: Sequence[ClaimRecord],
        title: str,
        score: float,
        detail: dict[str, object],
        *,
        key: str = "",
    ) -> None:
        ids = sorted({record.claim_id for record in records})
        identity = signal_identity(kind, ids, key)
        signals[identity] = SignalRecord(
            signal_id=identity, kind=kind, score=score, title=title[:300], claim_ids=ids,
            detail=detail, created_at=now,
        )

    by_experiment: dict[str, list[ClaimRecord]] = defaultdict(list)
    comparable: dict[tuple[str, str, str, str], list[ClaimRecord]] = defaultdict(list)
    coverage: dict[str, dict[tuple[str, str], list[ClaimRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    minimum = min_n_mcnemar()["n"]
    for record in current:
        claim = record.claim
        by_experiment[claim.experiment].append(record)
        counts = claim.k is not None and claim.n is not None and claim.n > 0
        comparison = claim.comparison
        scale = _scale(record)
        if counts:
            # Narrow explicitly: the boolean above is not a type guard.
            assert claim.k is not None and claim.n is not None
            expected = claim.k / claim.n
            interval = wilson(claim.k, claim.n)
            if claim.value is not None and abs(claim.value / scale - expected) > 0.006:
                emit("value_mismatch", [record],
                     f"{claim.experiment}: stated value does not match the counts", 4,
                     {"stated": claim.value / scale, "expected": expected})
            low, high = interval["low"], interval["high"]
            if (claim.ci_low is not None and claim.ci_high is not None
                    and low is not None and high is not None
                    and (abs(claim.ci_low / scale - low) > 0.02
                         or abs(claim.ci_high / scale - high) > 0.02)):
                emit("ci_mismatch", [record],
                     f"{claim.experiment}: stated interval differs from the count-based interval", 4,
                     {"stated": [claim.ci_low / scale, claim.ci_high / scale],
                      "expected": [low, high]})
            if (claim.kind in {"measurement", "comparison"} and claim.n < 30
                    and claim.k in {0, claim.n}):
                upper = rule_of_three(claim.n)["upper95"]
                assert upper is not None
                zero = claim.k == 0
                label = "false alarms" if _metric(claim.metric) == "false_alarm" else "events"
                title = (f"0 of {claim.n} {label} still allows up to {upper:.1%}" if zero else
                         f"{claim.n} of {claim.n} successes still allows up to {upper:.1%} failures")
                emit("extreme_small_n", [record], title, 2,
                     {"k": claim.k, "n": claim.n, "unobserved_upper95": upper})
        if (comparison is not None and comparison.test == "mcnemar_exact"
                and comparison.b is not None and comparison.c is not None):
            b, c = comparison.b, comparison.c
            discordant = b + c
            expected_p = mcnemar_exact(b, c)["p_value"]
            p = comparison.p_value
            if p is not None and expected_p is not None:
                delta = abs(p - expected_p)
                if delta > 0.002 and delta > 0.15 * max(expected_p, 1e-300):
                    emit("p_mismatch", [record],
                         f"{claim.experiment}: stated p-value does not match the paired counts", 4,
                         {"stated": p, "expected": expected_p, "b": b, "c": c})
            statement = _normalized(claim.statement)
            null = _NULL.search(statement) is not None
            if p is not None and p >= 0.05 and null and discordant < 20:
                emit("underpowered_null", [record],
                     f"{claim.experiment}: no-difference claim has only "
                     f"{discordant} discordant pairs",
                     2, {"discordant": discordant, "p_value": p,
                         "warning_threshold": 20, "minimum_for_significance": minimum})
            if (claim.kind == "comparison" and not null and _DIFFERENCE.search(statement)
                    and discordant < minimum):
                emit("underpowered_claim", [record],
                     f"{claim.experiment}: {discordant} discordant pairs cannot reach p < 0.05", 3,
                     {"discordant": discordant, "minimum_for_significance": minimum})
        if record.recompute is not None and record.recompute.status == "differs":
            emit("recompute_differs", [record],
                 f"{claim.experiment}: receipt recomputation differs from the claim", 4,
                 {"stated": record.recompute.stated, "recomputed": record.recompute.value,
                  "detail": record.recompute.detail})
        metric, subject, dataset = _metric(claim.metric), _normalized(claim.subject), _normalized(
            claim.dataset
        )
        if metric and subject and dataset:
            if counts:
                comparable[(metric, subject, dataset, _normalized(claim.condition))].append(record)
            if metric in _METRICS:
                coverage[metric][(subject, dataset)].append(record)

    for group in comparable.values():
        for left, right in combinations(group, 2):
            a, b = left.claim, right.claim
            if a.experiment == b.experiment:
                continue
            assert a.k is not None and a.n is not None and b.k is not None and b.n is not None
            result = two_proportion(a.k, a.n, b.k, b.n)
            p = result["p_value"]
            if p is not None and p < 0.01:
                emit("drift", [left, right],
                     f"{a.subject} {a.metric}: {a.k} of {a.n} versus "
                     f"{b.k} of {b.n} across experiments",
                     min(5, -math.log10(max(p, 1e-300))),
                     {"p_value": p, "difference": result["diff"], "test": "fisher_exact"})

    corrections: dict[str, list[ClaimRecord]] = defaultdict(list)
    for record in current:
        claim = record.claim
        if claim.status in {"corrected", "refuted"}:
            corrections[claim.experiment].append(record)
        for relation in claim.relations:
            target = normalize_experiment_id(relation.target_experiment)
            if target == claim.experiment:
                continue
            if relation.relation in {"corrects", "refutes", "contradicts"}:
                emit("contradiction", [record, *by_experiment.get(target, [])],
                     f"{claim.experiment} {relation.relation} {target}", 4,
                     {"source_experiment": claim.experiment, "target_experiment": target,
                      "relation": relation.relation}, key=f"{target}:{relation.relation}")
            if relation.relation in {"corrects", "refutes"}:
                corrections[target].append(record)
    for record in current:
        for relation in record.claim.relations:
            target = normalize_experiment_id(relation.target_experiment)
            if (relation.relation in {"extends", "confirms"} and corrections.get(target)
                    and record.claim.experiment != target):
                emit("correction_cascade", [record, *corrections[target],
                                            *by_experiment.get(target, [])],
                     f"{record.claim.experiment} relies on corrected or refuted {target}", 3.5,
                     {"dependent_experiment": record.claim.experiment, "target_experiment": target,
                      "relation": relation.relation}, key=target)

    gaps = 0
    for metric, cells in sorted(coverage.items()):
        subjects: dict[str, set[str]] = defaultdict(set)
        datasets: dict[str, set[str]] = defaultdict(set)
        for subject, dataset in cells:
            subjects[subject].add(dataset)
            datasets[dataset].add(subject)
        for subject, observed in sorted(subjects.items()):
            for dataset, evaluated in sorted(datasets.items()):
                if (gaps >= 20 or len(observed) < 2 or len(evaluated) < 2
                        or (subject, dataset) in cells):
                    continue
                support = [record for (s, d), group in cells.items()
                           if s == subject or d == dataset for record in group]
                emit("coverage_gap", support, f"Missing {metric} check: {subject} on {dataset}", 1,
                     {"metric": metric, "subject": subject, "dataset": dataset},
                     key=f"{metric}:{subject}:{dataset}")
                gaps += 1

    for experiment, records in sorted(by_experiment.items()):
        tested = [record for record in records if record.claim.comparison is not None
                  and record.claim.comparison.p_value is not None]
        if len(tested) < 5:
            continue
        ps = []
        for record in tested:
            assert record.claim.comparison is not None
            assert record.claim.comparison.p_value is not None
            ps.append(record.claim.comparison.p_value)
        adjusted = bh(ps)["adjusted"]
        lost = [record.claim_id for record, p, q in zip(tested, ps, adjusted, strict=True)
                if p < 0.05 and float(q) >= 0.05]
        if lost:
            emit("multiplicity", tested,
                 f"{experiment}: {len(lost)} raw significant results "
                 "do not survive BH correction",
                 2.5, {"tests": len(tested), "lost_claim_ids": lost,
                       "p_values": ps, "adjusted": adjusted, "q": 0.05})
    return sorted(signals.values(), key=lambda signal: (-signal.score, signal.signal_id))
