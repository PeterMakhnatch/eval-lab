"""Conservative statistical leads, not extraction-QA errors or conclusions.

Unknown quantity, interval method, dependence or coverage is not evidence of a
research discrepancy. Identity describes the issue independently of its current
support. Null warnings use <20 discordant pairs as a heuristic, not power.
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
from evallab.insights.units import as_fraction, normalize_unit, unit_kind

_NULL = re.compile(
    r"\b(no (?:clear |statistically )?(?:significant )?difference|"
    r"no (?:clear |significant )?(?:improvement|effect)|equivalent|equivalence|"
    r"not significantly (?:better|higher|worse|lower)|did not differ|insignificant|"
    r"n\.s\.|non.?significant|not statistically significant)\b"
)
_EQUIVALENCE = re.compile(r"\b(?:interchangeable|equivalent|equivalence)\b")
_DIFFERENCE = re.compile(
    r"\b(difference|better|higher|lower|outperform\w*|improv\w*|worse|increase\w*|"
    r"decrease\w*|significant|beats|superior)\b"
)
_METRICS = {"recall", "false_alarm", "precision", "auroc"}
_RATE = re.compile(r"recall|precision|(?:^|_)rate(?:\b|_)|capture|detection|survival|accuracy|agreement|asr")
_ADMIN = re.compile(
    r"\b(?:tests?|checks?|rubric|points?|score|files?|packages?|cost|usd|cache.hits?|"
    r"resolved|attest\w*|requests? served|workers? adopted|commands?|recount reproduction)\b"
)
_DEPENDENCE = re.compile(r"\b(?:relies on|relying on|uses? (?:the )?result|adopts?|assumes?|based on)\b")
_INDEPENDENT = re.compile(r"\b(?:independent|re-?run|rerun|replicat\w*|review|resolv\w*|"
                          r"does not (?:use|rely)|not (?:using|relying))\b")
_ABSENT = re.compile(r"\b(?:not (?:yet )?(?:measured|evaluated|tested)|unmeasured)\b")


def _normalized(value: str | None) -> str:
    return normalize_text(value or "").casefold()


def _metric(value: str | None) -> str:
    metric = _normalized(value).replace("-", "_").replace(" ", "_")
    return {"false_alarm_rate": "false_alarm", "fpr": "false_alarm",
            "false_positive_rate": "false_alarm", "tpr": "recall"}.get(metric, metric)


def _reader(value: str | None) -> str:
    text = _normalized(value).replace("`", "")
    return {"length baseline": "len_tokens", "length-baseline": "len_tokens",
            "token length": "len_tokens", "autoreview fixed rubric recount": "ar_g38_fixed"}.get(
                text, text)


def _rate(record: ClaimRecord) -> bool:
    claim = record.claim
    metric = _metric(claim.metric)
    # A raw count can have a rate-like label; the declared unit wins.
    if normalize_unit(claim.unit) and unit_kind(claim.unit) not in {"percent", "fraction"}:
        return False
    return metric != "auroc" and bool(_RATE.search(metric) or metric == "false_alarm")


def _fraction(record: ClaimRecord, value: float, *, interval: bool = False) -> float | None:
    # Legacy extraction sometimes dropped a percent unit from an entire CI, so the
    # quote's percent sign is the only accepted evidence for an out-of-range value.
    return as_fraction(value, record.claim.unit, quote=record.claim.quote, interval=interval)


def _context(record: ClaimRecord) -> tuple[str, ...]:
    claim = record.claim
    return (_metric(claim.metric), _reader(claim.subject), _normalized(claim.dataset),
            _normalized(claim.condition), claim.split or "unknown")


def _property(record: ClaimRecord) -> str:
    # Qualitative relations have no metric field in the v1 extraction contract.
    # Their proposition, not all target-experiment claims, identifies the property.
    return "|".join(_context(record)) if record.claim.metric else _normalized(record.claim.statement)


def _recipe_matches(record: ClaimRecord) -> bool:
    claim = record.claim
    recipe = claim.recipe
    if recipe is None:
        return False
    if _rate(record):
        if recipe.op != "ratio":
            return False
        leaves = [str(part) for subrecipe in (recipe.num, recipe.den) if subrecipe
                  for part in ((subrecipe.pointer or "").rsplit("/", 1)[-1], subrecipe.field)]
        return any(_RATE.search(_metric(leaf)) for leaf in leaves)
    if recipe.op in {"count", "distinct_count"}:
        return _normalized(claim.unit) in {"count", "items", "cases", "rows"}
    leaf = _metric(recipe.field or (recipe.pointer or "").rsplit("/", 1)[-1])
    return bool(leaf and leaf == _metric(claim.metric))


def _family(record: ClaimRecord) -> tuple[str, ...] | None:
    claim = record.claim
    comparison = claim.comparison
    if comparison is None or not claim.metric or not claim.dataset:
        return None
    condition = _normalized(claim.condition)
    explicit = re.search(r"(?:hypothesis[ _-])?family\s*[:=]\s*([^;]+)", condition)
    if explicit:
        scope = explicit.group(1)
    elif "payload" in condition or "injection variant" in condition:
        scope = "payload comparisons"
    elif "defense" in condition:
        scope = "defense comparisons"
    elif "pooled" in condition and "injection" in condition:
        scope = "pooled injection"
    elif _metric(claim.metric) == "paired_positive_surfacing":
        scope = "policy and budget comparisons"
    else:
        # Same endpoint, cohort and reference define a reader comparison family.
        # Unknown conditions are not silently merged with a stated condition.
        scope = condition
    return (claim.experiment, _metric(claim.metric), _normalized(claim.dataset),
            claim.split or "unknown", comparison.test, scope,
            "" if explicit or scope == "policy and budget comparisons"
            else _normalized(comparison.baseline))


def detect_signals(claims: Sequence[ClaimRecord], *, now: datetime) -> list[SignalRecord]:
    """Return issue-stable signals, sorted by descending review urgency then id."""
    current = sorted(
        (record for record in claims if not record.stale and record.check.status != "fail"),
        key=lambda record: record.claim_id,
    )
    signals: dict[str, SignalRecord] = {}

    def emit(
        kind: SignalKind, records: Sequence[ClaimRecord], title: str, score: float,
        detail: dict[str, object], *, key: str = "",
    ) -> None:
        ids = sorted({record.claim_id for record in records})
        issue = key or "||".join(sorted({record.claim.experiment + ":" + _property(record)
                                         for record in records}))
        identity = signal_identity(kind, [], issue)
        previous = signals.get(identity)
        if previous is not None:
            ids = sorted(set(ids) | set(previous.claim_ids))
        signals[identity] = SignalRecord(
            signal_id=identity, kind=kind, score=score, title=title[:300], claim_ids=ids,
            detail=detail, created_at=now,
        )

    by_experiment: dict[str, list[ClaimRecord]] = defaultdict(list)
    comparable: dict[tuple[str, ...], list[ClaimRecord]] = defaultdict(list)
    coverage: dict[tuple[str, str, str], dict[tuple[str, str], list[ClaimRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    small: dict[tuple[str, ...], list[ClaimRecord]] = defaultdict(list)
    families: dict[tuple[str, ...], list[ClaimRecord]] = defaultdict(list)
    minimum = min_n_mcnemar()["n"]
    for record in current:
        claim = record.claim
        by_experiment[claim.experiment].append(record)
        counts = claim.k is not None and claim.n is not None and claim.n > 0
        comparison = claim.comparison
        if counts and _rate(record):
            assert claim.k is not None and claim.n is not None
            expected = claim.k / claim.n
            stated = None if claim.value is None else _fraction(record, claim.value)
            if stated is not None and abs(stated - expected) > 0.006:
                emit("value_mismatch", [record],
                     f"{claim.experiment}: stated rate does not match the counts", 4,
                     {"stated": stated, "expected": expected})
            method_text = _normalized(" ".join([claim.quote, *claim.caveats]))
            if (claim.ci_low is not None and claim.ci_high is not None
                    and "wilson" in method_text and "bootstrap" not in method_text):
                interval = wilson(claim.k, claim.n)
                low, high = interval["low"], interval["high"]
                stated_low = _fraction(record, claim.ci_low, interval=True)
                stated_high = _fraction(record, claim.ci_high, interval=True)
                if (low is not None and high is not None and stated_low is not None
                        and stated_high is not None
                        and (abs(stated_low - low) > 0.02 or abs(stated_high - high) > 0.02)):
                    emit("ci_mismatch", [record],
                         f"{claim.experiment}: stated Wilson interval differs from the counts", 4,
                         {"stated": [stated_low, stated_high], "expected": [low, high]})
            text = _normalized(" ".join([claim.metric or "", claim.quote]))
            bounded = claim.ci_low is not None or bool(re.search(r"\d+\s*[–-]\s*\d+\s*%", claim.quote))
            if (claim.kind in {"measurement", "comparison"} and claim.n < 30
                    and claim.k in {0, claim.n} and not _ADMIN.search(text) and not bounded):
                context = _context(record)
                # Explicit shared cohort identities allow cross-report deduplication;
                # absent cohort identities must stay experiment-local.
                cohort = re.sub(r"^[^|]*\b(b4 frozen cohort \(76 items\))\b.*$", r"\1",
                                _normalized(claim.dataset))
                if claim.experiment == "FIX-HACKDROP-1729" and "capture" in _metric(claim.metric):
                    cohort = "b4 frozen cohort (76 items)"
                elif not cohort:
                    cohort_match = re.search(r"\b([^,;]+(?:cohort|rollouts?)\s*\([^)]*\))\b", context[3])
                    cohort = cohort_match.group(1) if cohort_match else ""
                condition = re.sub(r"^(?:multi-stage,\s*)?(?:default\s+)?task (?:review|drop),?\s*", "",
                                   context[3])
                if cohort and context[3].endswith(cohort):
                    condition = re.sub(re.escape(cohort) + r"\s*$", "", condition).rstrip(", ")
                key = ("" if cohort else claim.experiment, *context[:2], cohort,
                       condition, *context[4:], str(claim.k), str(claim.n))
                small[key].append(record)
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
            if p is not None and p >= 0.05 and null and _EQUIVALENCE.search(statement) and discordant < 20:
                emit("underpowered_null", [record],
                     f"{claim.experiment}: no-difference claim has only {discordant} discordant pairs",
                     2, {"discordant": discordant, "p_value": p, "warning_threshold": 20,
                         "minimum_for_significance": minimum})
            if (claim.kind == "comparison" and not null and _DIFFERENCE.search(statement)
                    and discordant < minimum):
                emit("underpowered_claim", [record],
                     f"{claim.experiment}: {discordant} discordant pairs cannot reach p < 0.05", 3,
                     {"discordant": discordant, "minimum_for_significance": minimum})
        if (record.recompute is not None and record.recompute.status == "differs"
                and record.recompute.value is not None and claim.value is not None
                and _recipe_matches(record)):
            stated = claim.value
            actual = record.recompute.value
            if _rate(record):
                stated = _fraction(record, stated)
                # Ratio recipes return fractions, not the report's percentage scale.
                actual = actual if claim.recipe and claim.recipe.op == "ratio" else _fraction(
                    record, actual)
            if stated is not None and actual is not None and abs(stated - actual) > 0.006:
                emit("recompute_differs", [record],
                     f"{claim.experiment}: same-quantity receipt recomputation differs", 4,
                     {"stated": stated, "recomputed": actual, "detail": record.recompute.detail})
        metric, subject, dataset, condition, split = _context(record)
        if metric and subject and dataset:
            if counts and _rate(record):
                comparable[(metric, subject, dataset, condition, split)].append(record)
            if (metric in _METRICS and claim.kind in {"measurement", "comparison"}
                    and (counts or claim.value is not None)):
                coverage[(metric, condition, split)][(subject, dataset)].append(record)
        family = _family(record)
        if family is not None and comparison is not None and comparison.p_value is not None:
            families[family].append(record)

    small_by_experiment: dict[str, list[tuple[float, tuple[str, ...], list[ClaimRecord]]]] = defaultdict(list)
    for key, records in small.items():
        record = records[0]
        relevant = any(re.search(r"\b(?:gate|decision|adopt|drop|capture|threshold|recommend)\w*\b",
                                 _normalized(r.claim.statement + " " + (r.claim.metric or "")))
                       for r in records)
        score = 2.5 if relevant else 2.0
        small_by_experiment[record.claim.experiment].append((score, key, records))
    for candidates in small_by_experiment.values():
        for score, key, records in sorted(candidates, key=lambda item: (-item[0], item[1]))[:3]:
            claim = records[0].claim
            assert claim.n is not None
            upper = rule_of_three(claim.n)["upper95"]
            assert upper is not None
            label = "false alarms" if _metric(claim.metric) == "false_alarm" else "events"
            title = (f"0 of {claim.n} {label} still allows up to {upper:.1%}" if claim.k == 0 else
                     f"{claim.n} of {claim.n} successes still allows up to {upper:.1%} failures")
            emit("extreme_small_n", records, title, score,
                 {"k": claim.k, "n": claim.n, "metric": claim.metric,
                  "unobserved_upper95": upper, "decision_relevant": score > 2}, key="|".join(key))

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
                     f"{a.subject} {a.metric}: {a.k} of {a.n} versus {b.k} of {b.n} across experiments",
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
            note = _normalized(relation.note)
            if re.search(r"not a (?:refutation|contradiction)|design contrast", note):
                continue
            if relation.relation in {"corrects", "refutes", "contradicts"}:
                emit("contradiction", [record, *by_experiment.get(target, [])],
                     f"{claim.experiment} {relation.relation} {target}", 4,
                     {"source_experiment": claim.experiment, "target_experiment": target,
                      "relation": relation.relation},
                     key=f"{claim.experiment}:{target}:{relation.relation}:{_property(record)}")
            if relation.relation in {"corrects", "refutes"}:
                corrections[target].append(record)
    for record in current:
        claim = record.claim
        if not claim.metric or claim.kind == "correction" or claim.status in {"corrected", "refuted"}:
            continue
        for relation in claim.relations:
            target = normalize_experiment_id(relation.target_experiment)
            text = _normalized(claim.statement + " " + (relation.note or ""))
            if (relation.relation not in {"extends", "confirms"} or claim.experiment == target
                    or not _DEPENDENCE.search(text) or _INDEPENDENT.search(text)):
                continue
            corrected = [r for r in corrections.get(target, []) if r.claim.metric
                         and _context(r) == _context(record) and r.claim.experiment != claim.experiment]
            if corrected:
                emit("correction_cascade", [record, *corrected],
                     f"{claim.experiment} relies on corrected {target} {claim.metric}", 3.5,
                     {"dependent_experiment": claim.experiment, "target_experiment": target,
                      "metric": claim.metric, "relation": relation.relation},
                     key=f"{claim.experiment}:{target}:{relation.relation}:{_property(record)}")

    gaps = 0
    for (metric, condition, split), cells in sorted(coverage.items()):
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
                # Missing extraction is not a missing experiment. Until a measured-
                # cell inventory exists, require explicit source evidence of absence.
                absent = [r for r in current if _context(r) == (metric, subject, dataset, condition, split)
                          and _ABSENT.search(_normalized(r.claim.statement))]
                if not absent:
                    continue
                emit("coverage_gap", absent, f"Unmeasured {metric}: {subject} on {dataset}", 1,
                     {"metric": metric, "subject": subject, "dataset": dataset,
                      "condition": condition, "split": split, "absence": "explicit source statement"},
                     key=f"{metric}:{subject}:{dataset}:{condition}:{split}")
                gaps += 1

    for family, records in sorted(families.items()):
        tests: dict[tuple[object, ...], ClaimRecord] = {}
        for record in records:
            claim = record.claim
            comparison = claim.comparison
            assert comparison is not None
            identity = (_reader(claim.subject), _normalized(claim.condition), comparison.baseline,
                        comparison.b, comparison.c, comparison.k2, comparison.n2, claim.k, claim.n)
            tests.setdefault(identity, record)
        tested = list(tests.values())
        if len(tested) < 2:
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
                 f"{family[0]}: {len(lost)} raw significant results do not survive family-scoped BH",
                 2.5, {"tests": len(tested), "lost_claim_ids": lost, "p_values": ps,
                       "adjusted": adjusted, "q": 0.05, "family": list(family),
                       "family_provenance": "explicit family tag or shared endpoint/cohort/comparison scope"},
                 key="|".join(family))
    return sorted(signals.values(), key=lambda signal: (-signal.score, signal.signal_id))
