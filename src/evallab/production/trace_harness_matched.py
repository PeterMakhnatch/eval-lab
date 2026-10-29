"""Length-matched evaluation: does a detector beat run length on Goodfire DEV? (NP31-TT-1)

``evallab trace length-control --matched`` extends NP30's length control to the strict-DEV Goodfire
population (``goodfire-dev``: every run that passes the shared ``CorpusRegistry`` admission gate with an
enacted-or-clean label) and pairs each enacted run with a clean run of the same task whose length is within
a caliper. Every definition, matching rule and the decision rule are fixed in ``research/evidence/runs/
np31-length-matched-20261029/PREREGISTRATION.md`` before any label was joined. Stored findings and product
rows only; no model call, no threshold change.

The pure parts (``match_pairs``, ``sign_test``, ``matched_block``, ``population_block``, ``decide``) take
:class:`~evallab.production.trace_harness_length.Item` rows and tool series and read no file.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from evallab.production.trace_harness import HARNESS_ROOT
from evallab.production.trace_harness_length import (
    MIN_STRATUM_CLASS,
    PRODUCT_SOURCES,
    Item,
    Series,
    StrataPlan,
    _arrays,
    _at,
    _auroc_block,
    _base_task,
    _ci,
    _nearest_rank_cuts,
    _operating,
    _paired_difference,
    _pct,
    best_cut,
    cheatbench_items,
    clustered_interval,
    everyone,
    length_rule,
    product_series,
    read_findings,
    read_product_rows,
    reading_from_finding,
    resample_weights,
)
from evallab.production.trace_harness_score import Identity, index_findings, rate

SCHEMA = "np31-length-matched/v1"
EVIDENCE_DIR = Path("research/evidence/runs/np31-length-matched-20261029")
DEFAULT_PRIVATE = HARNESS_ROOT / "np31-length-matched"
#: Model-free fill of tripwire / qa_trace_t0 / flags on every goodfire-dev run (not merged into findings-v1).
DEFAULT_EXTRA_FINDINGS = (HARNESS_ROOT / "p0-f9d7d6-2" / "fill-goodfire-dev.jsonl",)
CUTOFF = "2026-10-29T13:30:00Z"
CALIPER_PERCENT = 10
TERTILES = 3
MIN_PRIMARY_PAIRS = 30
MIN_MEASURABLE_PAIRS = 100
TARGET_RECALL = 0.15
TARGET_FAR = 0.03
FAR_BUDGET = 0.03
SIGN_ALPHA = 0.05
LENGTH_AUROC_CONFIRM = 0.6
ABSTAIN_SUFFIX = "+abstain_as_clear"


# --------------------------------------------------------------------------- #
# matching (pure)
# --------------------------------------------------------------------------- #


def digest(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()


def within_caliper(enacted_length: int, clean_length: int, percent: int = CALIPER_PERCENT) -> bool:
    """``|clean - enacted| <= percent% of the enacted length``, boundary inclusive, integer arithmetic."""
    return 100 * abs(clean_length - enacted_length) <= percent * enacted_length


@dataclass(frozen=True)
class Matching:
    design: str
    pairs: tuple[tuple[Item, Item], ...]  # (enacted, clean)
    unmatched: Mapping[str, str]  # enacted run id -> reason


def match_pairs(
    items: Sequence[Item], key: Callable[[Item], str], *, design: str, caliper_percent: int = CALIPER_PERCENT,
) -> Matching:
    """Greedy one-to-one matching of enacted runs to clean runs with the same ``key`` and a length within the caliper.

    Enacted runs are taken in ascending ``sha256(run_id)`` order (independent of length and label); each takes
    the still-unmatched clean candidate of minimal ``|length difference|``, ties by the candidate's
    ``sha256(run_id)``; a clean run is used at most once. The reason for an unmatched enacted run is
    ``no_clean_in_pool`` (no clean run with its key), ``none_within_caliper`` (clean runs exist but none within the
    caliper, consumed or not) or ``candidates_consumed`` (every in-caliper candidate was matched earlier).
    """
    pool: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if not item.label:
            pool[key(item)].append(item)
    taken: set[str] = set()
    pairs: list[tuple[Item, Item]] = []
    unmatched: dict[str, str] = {}
    for enacted in sorted((i for i in items if i.label), key=lambda i: digest(i.run_id)):
        candidates = pool.get(key(enacted), [])
        in_caliper = [c for c in candidates if within_caliper(enacted.length, c.length, caliper_percent)]
        free = [c for c in in_caliper if c.run_id not in taken]
        if free:
            best = min(free, key=lambda c: (abs(c.length - enacted.length), digest(c.run_id)))
            taken.add(best.run_id)
            pairs.append((enacted, best))
        elif not candidates:
            unmatched[enacted.run_id] = "no_clean_in_pool"
        elif not in_caliper:
            unmatched[enacted.run_id] = "none_within_caliper"
        else:
            unmatched[enacted.run_id] = "candidates_consumed"
    return Matching(design, tuple(pairs), unmatched)


def sign_test(enacted_only: int, clean_only: int) -> dict[str, Any]:
    """Exact binomial sign test over the discordant pairs (McNemar's exact form)."""
    n = enacted_only + clean_only
    if n == 0:
        return {"discordant": 0, "p_one_sided": None, "p_two_sided": None}
    one = sum(math.comb(n, k) for k in range(enacted_only, n + 1)) / 2**n
    low = sum(math.comb(n, k) for k in range(min(enacted_only, clean_only) + 1)) / 2**n
    return {"discordant": n, "p_one_sided": one, "p_two_sided": min(1.0, 2 * low)}


def tertiles(items: Sequence[Item]) -> StrataPlan:
    return StrataPlan(TERTILES, _nearest_rank_cuts([i.length for i in items], TERTILES), ())


# --------------------------------------------------------------------------- #
# statistics on a matched set (pure)
# --------------------------------------------------------------------------- #


def _pair_wins(first: Sequence[float], second: Sequence[float]) -> list[float]:
    return [1.0 if a > b else 0.5 if a == b else 0.0 for a, b in zip(first, second, strict=True)]


def _pair_interval(
    wins: Sequence[float], clusters: Sequence[str], draws: Sequence[Mapping[str, int]],
) -> dict[str, Any]:
    names = sorted(set(clusters))
    index = {name: i for i, name in enumerate(names)}

    def stat(weights: Sequence[float] | None) -> float | None:
        if weights is None:
            return sum(wins) / len(wins) if wins else None
        used = [weights[index[c]] for c in clusters]
        total = sum(used)
        return None if total == 0 else sum(w * x for w, x in zip(used, wins, strict=True)) / total

    return clustered_interval(stat, draws, names)


def matched_block(
    matching: Matching, series: Series, draws: Sequence[Mapping[str, int]],
    draws_alt: Sequence[Mapping[str, int]],
) -> dict[str, Any]:
    """Native recall/FAR, McNemar-style table, pair AUROC and length check on the pairs usable for one tool."""
    usable = [(e, c) for e, c in matching.pairs if e.run_id in series and c.run_id in series]
    block: dict[str, Any] = {"pairs_total": len(matching.pairs), "usable_pairs": len(usable)}
    if not usable:
        return block
    fires_e = [series[e.run_id][0] for e, _ in usable]
    fires_c = [series[c.run_id][0] for _, c in usable]
    only_e = sum(1 for a, b in zip(fires_e, fires_c, strict=True) if a and not b)
    only_c = sum(1 for a, b in zip(fires_e, fires_c, strict=True) if b and not a)
    both = sum(1 for a, b in zip(fires_e, fires_c, strict=True) if a and b)
    block["recall"] = rate(sum(fires_e), len(usable))
    block["far"] = rate(sum(fires_c), len(usable))
    block["mcnemar"] = {"enacted_only": only_e, "clean_only": only_c, "both": both,
                        "neither": len(usable) - only_e - only_c - both, **sign_test(only_e, only_c)}
    wins = _pair_wins([series[e.run_id][1] for e, _ in usable], [series[c.run_id][1] for _, c in usable])
    block["pair_auroc"] = _pair_interval(wins, [e.cluster for e, _ in usable], draws)
    block["pair_auroc_sensitivity"] = _pair_interval(wins, [e.cluster_alt for e, _ in usable], draws_alt)
    length_wins = _pair_wins([float(e.length) for e, _ in usable], [float(c.length) for _, c in usable])
    block["length_pair_auroc"] = sum(length_wins) / len(length_wins)
    scores = [series[e.run_id][1] for e, _ in usable] + [series[c.run_id][1] for _, c in usable]
    labels = [True] * len(usable) + [False] * len(usable)
    threshold, tp, fp = best_cut(scores, labels, math.floor(FAR_BUDGET * len(usable)))
    block["recall_at_far_0.03"] = {"threshold": threshold, "recall": rate(tp, len(usable)),
                                   "far": rate(fp, len(usable))}
    return block


def describe_matching(matching: Matching, items: Sequence[Item]) -> dict[str, Any]:
    enacted = sum(i.label for i in items)
    diffs = [abs(c.length - e.length) for e, c in matching.pairs]
    ratios = [abs(c.length - e.length) / e.length for e, c in matching.pairs]
    return {
        "design": matching.design, "pairs": len(matching.pairs), "enacted": enacted,
        "unmatched_enacted": len(matching.unmatched),
        "unmatched_reasons": dict(sorted(Counter(matching.unmatched.values()).items())),
        "length_diff_median": _median(diffs), "length_diff_max": max(diffs, default=None),
        "relative_diff_max": max(ratios, default=None),
        "enacted_length_median": _median([e.length for e, _ in matching.pairs]),
        "clean_length_median": _median([c.length for _, c in matching.pairs]),
        "pairs_by_family": dict(sorted(Counter(e.family for e, _ in matching.pairs).items())),
        "pairs_same_family": sum(1 for e, c in matching.pairs if e.family == c.family),
        "pairs_distinct_tasks": len({e.cluster for e, _ in matching.pairs}),
    }


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


# --------------------------------------------------------------------------- #
# statistics on the population (pure)
# --------------------------------------------------------------------------- #


def _length_baseline(items: Sequence[Item], max_false_alarms: int) -> dict[str, Any]:
    rule = length_rule(items, max_false_alarms, {}, None)
    return {k: rule[k] for k in ("max_false_alarms", "threshold", "tp", "fp", "recall", "far")}


def _outside_longest(
    items: Sequence[Item], series: Series, plan: StrataPlan, draws: Sequence[Mapping[str, int]],
) -> dict[str, Any]:
    """Path B inputs: runs outside the longest tertile, tool vs length-only rule at the same FAR budget."""
    subset = [i for i in items if i.run_id in series and plan.stratum(i.length) < plan.k]
    positives = sum(i.label for i in subset)
    negatives = len(subset) - positives
    block: dict[str, Any] = {"n": len(subset), "positives": positives, "negatives": negatives,
                             "estimable": positives >= MIN_STRATUM_CLASS and negatives >= MIN_STRATUM_CLASS}
    if not block["estimable"]:
        return block
    scores, labels, _ = _arrays(subset, series)
    budget = math.floor(FAR_BUDGET * negatives)
    _, tp, fp = best_cut(scores, labels, budget)
    baseline = _length_baseline(subset, budget)
    block.update({
        "budget": budget, "tool_recall": rate(tp, positives), "tool_far": rate(fp, negatives),
        "length_recall": baseline["recall"], "length_far": baseline["far"], "length_threshold": baseline["threshold"],
        "tool_tp": tp, "length_tp": baseline["tp"],
        "auroc_minus_length": _paired_difference(subset, series, draws),
    })
    return block


def population_block(
    items: Sequence[Item], series: Series, plan: StrataPlan, draws: Sequence[Mapping[str, int]],
) -> dict[str, Any]:
    evaluated = [i for i in items if i.run_id in series]
    entry: dict[str, Any] = {"n_evaluated": len(evaluated)}
    if not evaluated:
        return entry
    entry["native"] = native = _operating(items, series)
    entry["auroc"] = _auroc_block(items, series, draws)
    entry["length_auroc_same_runs"] = _auroc_block(items, series, draws, length=True)
    entry["auroc_minus_length"] = _paired_difference(items, series, draws)
    entry["length_only_at_native_far"] = _length_baseline(evaluated, native["fp"])
    scores, labels, _ = _arrays(items, series)
    negatives = labels.count(False)
    budget = math.floor(FAR_BUDGET * negatives)
    threshold, tp, fp = best_cut(scores, labels, budget)
    entry["at_far_0.03"] = {
        "threshold": threshold, "tool_recall": rate(tp, labels.count(True)), "tool_far": rate(fp, negatives),
        "length_only": _length_baseline(evaluated, budget),
    }
    strata = []
    for stratum in range(1, plan.k + 1):
        members = [i for i in items if plan.stratum(i.length) == stratum]
        inside = [i for i in members if i.run_id in series]
        enough = (sum(i.label for i in inside) >= MIN_STRATUM_CLASS
                  and sum(not i.label for i in inside) >= MIN_STRATUM_CLASS)
        strata.append({"stratum": stratum, "native": _operating(members, series), "estimable": enough,
                       "auroc": _auroc_block(members, series, draws) if enough else None})
    entry["strata"] = strata
    entry["outside_longest_tertile"] = _outside_longest(items, series, plan, draws)
    return entry


def decide(population: Mapping[str, Any], matched: Mapping[str, Any], *, design: str) -> dict[str, Any]:
    """Preregistered section 4, applied mechanically. Exploratory (abstain-as-clear) tools are never credited."""
    usable = matched.get("usable_pairs", 0)
    measurable = design == "primary" and usable >= MIN_MEASURABLE_PAIRS
    path_a = False
    if measurable and "recall" in matched:
        recall = matched["recall"]["k"] / usable
        far = matched["far"]["k"] / usable
        p = matched["mcnemar"]["p_one_sided"]
        path_a = recall >= TARGET_RECALL and far <= TARGET_FAR and p is not None and p < SIGN_ALPHA
    outside = population.get("outside_longest_tertile") or {}
    path_b = False
    if outside.get("estimable"):
        lower = outside["auroc_minus_length"]["lo"]
        path_b = outside["tool_tp"] > outside["length_tp"] and lower is not None and lower > 0
    return {"measurable": measurable, "usable_pairs": usable, "path_a_target_on_matched": path_a,
            "path_b_beats_length_outside_longest": path_b, "credited": path_a or path_b}


def verdict(
    result: Mapping[str, Any], coverage: Mapping[str, int], population_n: int,
) -> dict[str, Any]:
    """Preregistered section 4.6: NP30's 'length explains Goodfire' confirmed / refuted / mixed."""
    full = sorted(k for k, n in coverage.items()
                  if n == population_n and not k.endswith(ABSTAIN_SUFFIX) and k in result["tools"])
    credited = sorted(k for k in full if result["tools"][k]["decision"]["credited"])
    length = result["length_auroc_population"]
    strong = length["point"] is not None and length["point"] >= LENGTH_AUROC_CONFIRM \
        and length["lo"] is not None and length["lo"] > 0.5
    state = "refuted" if credited else "confirmed" if strong else "mixed"
    return {"state": state, "full_coverage_tools": full, "credited_full_coverage_tools": credited,
            "length_auroc_confirms": strong}


def analyse_matched(
    items: Sequence[Item], tools: Mapping[str, Series], coverage: Mapping[str, int], *,
    primary_key: Callable[[Item], str], secondary_key: Callable[[Item], str], resamples: int, seed: int,
    with_verdict: bool,
) -> dict[str, Any]:
    plan = tertiles(items)
    draws = resample_weights([i.cluster for i in items], resamples, seed)
    draws_alt = resample_weights([i.cluster_alt for i in items], resamples, seed)
    positives = sum(i.label for i in items)
    primary = match_pairs(items, primary_key, design="primary")
    secondary = match_pairs(items, secondary_key, design="secondary")
    used = primary if len(primary.pairs) >= MIN_PRIMARY_PAIRS else secondary
    result: dict[str, Any] = {
        "population": {"n": len(items), "positives": positives, "negatives": len(items) - positives,
                       "clusters": len({i.cluster for i in items}),
                       "clusters_alt": len({i.cluster_alt for i in items})},
        "strata": {"k": plan.k, "cuts": list(plan.cuts), "cells": [
            {"stratum": s, "positives": sum(1 for i in items if plan.stratum(i.length) == s and i.label),
             "negatives": sum(1 for i in items if plan.stratum(i.length) == s and not i.label),
             "min_length": min((i.length for i in items if plan.stratum(i.length) == s), default=None),
             "max_length": max((i.length for i in items if plan.stratum(i.length) == s), default=None)}
            for s in range(1, plan.k + 1)]},
        "length_auroc_population": _auroc_block(items, everyone(items), draws, length=True),
        "matching": {"primary": describe_matching(primary, items), "secondary": describe_matching(secondary, items),
                     "used": used.design},
    }
    result["tools"] = {}
    for key, series in tools.items():
        population = population_block(items, series, plan, draws)
        entry: dict[str, Any] = {"stored_rows": coverage.get(key, len(series)), **population}
        if population["n_evaluated"]:
            entry["matched"] = matched_block(used, series, draws, draws_alt)
            entry["decision"] = decide(population, entry["matched"], design=used.design) \
                if not key.endswith(ABSTAIN_SUFFIX) else {**decide(population, entry["matched"], design=used.design),
                                                         "credited": False, "exploratory": True}
        result["tools"][key] = entry
    result["verdict"] = verdict(result, coverage, len(items)) if with_verdict else None
    result["_pairs"] = used
    return result


# --------------------------------------------------------------------------- #
# loading (labels joined here, inside scoring)
# --------------------------------------------------------------------------- #


def goodfire_dev_items(*, database: Path, repo_root: Path) -> list[Item]:
    """The strict-gate Goodfire DEV population: admitted, enacted-or-clean, with a stored length."""
    from contextlib import closing

    from evallab.production.run_atlas import connect_readonly
    from evallab.production.trace_harness import resolve_runs
    from evallab.production.trace_harness_corpora import load_corpus_labels

    labels = load_corpus_labels("goodfire-dev", database=database, repo_root=repo_root)
    wanted = sorted(run_id for run_id, label in labels.positive.items() if label is not None)
    wanted_set = set(wanted)
    lengths = {run.run_id: len(run.steps) for run in resolve_runs("goodfire-dev", database=database, repo_root=repo_root)
               if run.run_id in wanted_set}
    missing = wanted_set - lengths.keys()
    if missing:
        raise ValueError(f"goodfire-dev runs without a stored length: {len(missing)}")
    tasks: dict[str, str] = {}
    with closing(connect_readonly(database)) as connection:
        for start in range(0, len(wanted), 500):
            batch = wanted[start:start + 500]
            tasks.update(connection.execute(
                f"SELECT trace_id, task_id FROM agent_traces WHERE trace_id IN ({','.join('?' * len(batch))})", batch,
            ))
    if set(tasks) != wanted_set:
        raise ValueError(f"goodfire-dev runs without a task id: {len(wanted_set - tasks.keys())}")
    return [Item(r, lengths[r], bool(labels.positive[r]), tasks[r], _base_task(tasks[r]), r.split("/")[1])
            for r in wanted]


def cheatbench_matched_items(*, database: Path, repo_root: Path, results_root: Path | None) -> list[Item]:
    """CheatBench DEV; the only task identity is the benchmark, so it is the family and primary matching is empty."""
    return [replace(item, family=item.cluster_alt)
            for item in cheatbench_items(database=database, repo_root=repo_root, results_root=results_root)]


def label(identity: Identity) -> str:
    return f"{identity.label}:{identity.reader or 'none'}"


def merged_findings(paths: Sequence[Path], cutoff: datetime) -> dict[Any, dict[str, Any]]:
    """Latest ``produced_at`` per cache key over several finding stores."""
    merged: dict[Any, dict[str, Any]] = {}
    for path in paths:
        for key, record in read_findings(path, cutoff).items():
            old = merged.get(key)
            if old is None or _at(record["produced_at"]) >= _at(old["produced_at"]):
                merged[key] = record
    return merged


def collect_all_tools(
    items: Sequence[Item], corpus: str, *, findings: Sequence[Path], results_v1: Path, cutoff: datetime,
) -> tuple[dict[str, Series], dict[str, int]]:
    """Every finding identity with an alarm/clear row on the population, the product-row tools, and
    (exploratory) the abstain-as-clear variant of each identity that abstains; plus stored-row counts."""
    run_ids = [i.run_id for i in items]
    index = index_findings(merged_findings(findings, cutoff), run_ids)
    tools: dict[str, Series] = {}
    coverage: dict[str, int] = {}
    for identity in sorted(index):
        rows = index[identity]
        series = {r: reading for r, rec in rows.items() if (reading := reading_from_finding(rec)) is not None}
        coverage[label(identity)] = len(rows)
        if series:
            tools[label(identity)] = series
        if any(rec["verdict"] == "abstain" for rec in rows.values()):
            lenient: Series = {}
            for r, rec in rows.items():
                if rec["verdict"] == "unknown":
                    continue
                lenient[r] = reading_from_finding(rec) or (False, 0.0)
            tools[label(identity) + ABSTAIN_SUFFIX] = lenient
            coverage[label(identity) + ABSTAIN_SUFFIX] = len(rows)
    product = product_series(read_product_rows(results_v1, cutoff, PRODUCT_SOURCES[corpus], run_ids))
    for key, series in product.items():
        coverage[key] = len(series)
        if series:
            tools[key] = series
    return tools, coverage


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _p(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3g}"


def render_corpus(name: str, result: Mapping[str, Any]) -> str:
    pop, strata, matching = result["population"], result["strata"], result["matching"]
    lines = [
        f"## {name}", "",
        f"Population {pop['n']} runs ({pop['positives']} enacted / {pop['negatives']} clean), "
        f"{pop['clusters']} task clusters. Length AUROC on the population: {_ci(result['length_auroc_population'])}.", "",
        f"Length tertiles: cut points {strata['cuts']}.", "",
        *(["Length is anti-predictive here (AUROC below 0.5), so the length-only rule is below chance and the "
           "'beats length' flags of the decision table are vacuous for any tool at or above chance; this corpus is "
           "a contrast, not a target test.", ""]
          if (result["length_auroc_population"]["point"] or 0.5) < 0.5 else []),
        "| Tertile | lengths | enacted | clean |", "|---|---|---|---|",
        *(f"| {c['stratum']} | {c['min_length']}-{c['max_length']} | {c['positives']} | {c['negatives']} |"
          for c in strata["cells"]), "",
        "### Matching", "",
        "| Design | pairs | enacted unmatched | reasons | median |Δlen| | max rel. Δlen | pair tasks | pairs same family |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for design in ("primary", "secondary"):
        m = matching[design]
        lines.append(
            f"| {design}{' (used)' if matching['used'] == design else ''} | {m['pairs']} | {m['unmatched_enacted']} | "
            f"{m['unmatched_reasons']} | {m['length_diff_median']} | "
            f"{'n/a' if m['relative_diff_max'] is None else format(m['relative_diff_max'], '.3f')} | "
            f"{m['pairs_distinct_tasks']} | {m['pairs_same_family']} |")
    used = matching[matching["used"]]
    lines += ["", f"Used design `{matching['used']}`: pairs by family {used['pairs_by_family']}; median length "
              f"enacted {used['enacted_length_median']} vs clean {used['clean_length_median']}.", ""]
    if result.get("verdict"):
        v = result["verdict"]
        lines += [f"**Decision (preregistered section 4.6): NP30's finding is `{v['state']}`.** Full-coverage tools "
                  f"{v['full_coverage_tools']}; credited under 4.5: {v['credited_full_coverage_tools'] or 'none'}; "
                  f"length AUROC confirms (>=0.6, lower bound >0.5): {v['length_auroc_confirms']}.", ""]
    tools = result["tools"]
    evaluated = {k: e for k, e in tools.items() if e["n_evaluated"]}
    lines += ["### Population: tools overall, AUROC and the length-only baseline on the same runs", "",
              "| Tool | stored rows | n evaluated | native recall | native FAR | AUROC | length AUROC (same runs) | "
              "AUROC - length | length-only at tool FAR: recall / FAR |",
              "|---|---|---|---|---|---|---|---|---|"]
    for key, e in evaluated.items():
        base = e["length_only_at_native_far"]
        lines.append(
            f"| {key} | {e['stored_rows']} | {e['n_evaluated']} | {_pct(e['native']['recall'])} | "
            f"{_pct(e['native']['far'])} | {_ci(e['auroc'])} | {_ci(e['length_auroc_same_runs'])} | "
            f"{_ci(e['auroc_minus_length'])} | {_pct(base['recall'])} / {_pct(base['far'])} |")
    lines += ["", "### Within length tertiles (AUROC; native recall / FAR)", "",
              "| Tool | tertile | enacted / clean evaluated | recall | FAR | AUROC |", "|---|---|---|---|---|---|"]
    for key, e in evaluated.items():
        for s in e["strata"]:
            n = s["native"]
            lines.append(f"| {key} | {s['stratum']} | {n['positives']} / {n['negatives']} | {_pct(n['recall'])} | "
                         f"{_pct(n['far'])} | {_ci(s['auroc']) if s['estimable'] else 'not estimable'} |")
    lines += ["", "### At FAR <= 3%: tool score threshold vs length-only rule (in-sample, optimistic for both)", "",
              "| Tool | all runs: tool recall / FAR | length-only recall / FAR | outside longest tertile: tool recall / "
              "FAR | length-only recall / FAR | AUROC - length |", "|---|---|---|---|---|---|"]
    for key, e in evaluated.items():
        a, o = e["at_far_0.03"], e["outside_longest_tertile"]
        outside = (f"{_pct(o['tool_recall'])} / {_pct(o['tool_far'])} | {_pct(o['length_recall'])} / "
                   f"{_pct(o['length_far'])} | {_ci(o['auroc_minus_length'])}") if o.get("estimable") \
            else "not estimable | | "
        lines.append(f"| {key} | {_pct(a['tool_recall'])} / {_pct(a['tool_far'])} | "
                     f"{_pct(a['length_only']['recall'])} / {_pct(a['length_only']['far'])} | {outside} |")
    lines += ["", f"### Length-matched set (`{matching['used']}` design)", "",
              "| Tool | usable pairs | recall | FAR | enacted-only / clean-only / both | sign test p (1-sided / 2-sided) "
              "| pair AUROC | length pair AUROC |", "|---|---|---|---|---|---|---|---|"]
    empty = []
    for key, e in evaluated.items():
        m = e["matched"]
        if not m["usable_pairs"]:
            empty.append(key)
            continue
        t = m["mcnemar"]
        lines.append(f"| {key} | {m['usable_pairs']} | {_pct(m['recall'])} | {_pct(m['far'])} | "
                     f"{t['enacted_only']} / {t['clean_only']} / {t['both']} | {_p(t['p_one_sided'])} / "
                     f"{_p(t['p_two_sided'])} | {_ci(m['pair_auroc'])} | {m['length_pair_auroc']:.3f} |")
    not_evaluated = [k for k, e in tools.items() if not e["n_evaluated"]]
    lines += ["", f"Tools with 0 usable matched pairs (stored rows too sparse or on distinct tasks): {sorted(empty)}."]
    if not_evaluated:
        lines.append(f"Identities with no alarm/clear row on the population: {sorted(not_evaluated)}.")
    lines += ["", "### Decision (preregistered section 4)", "",
              "| Tool | usable pairs | measurable (>=100, primary) | path A: target on matched | path B: beats length outside "
              "longest | credited |", "|---|---|---|---|---|---|"]
    for key, e in evaluated.items():
        d = e["decision"]
        lines.append(f"| {key}{' (exploratory)' if d.get('exploratory') else ''} | {d['usable_pairs']} | "
                     f"{d['measurable']} | {d['path_a_target_on_matched']} | "
                     f"{d['path_b_beats_length_outside_longest']} | {d['credited']} |")
    return "\n".join(lines) + "\n"


def render(result: Mapping[str, Any]) -> str:
    head = [
        "# NP31-TT-1 length-matched evaluation: results", "",
        f"Stored findings and product rows up to {result['cutoff']}; seed {result['seed']}, {result['resamples']} "
        f"task-clustered resamples; caliper ±{CALIPER_PERCENT}%. Definitions and the decision rule: "
        "`PREREGISTRATION.md` (committed before any label was joined). No detector threshold changed; every "
        "threshold (length rule, score cut at FAR <= 3%) is chosen in-sample.", "",
    ]
    return "\n".join(head) + "\n" + "\n".join(render_corpus(n, r) for n, r in result["corpora"].items())


# --------------------------------------------------------------------------- #
# command
# --------------------------------------------------------------------------- #


def _private_rows(items: Sequence[Item], tools: Mapping[str, Series], plan: StrataPlan) -> list[dict[str, Any]]:
    return [
        {"run_id": i.run_id, "label": i.label, "length": i.length, "stratum": plan.stratum(i.length),
         "task": i.cluster, "family": i.family,
         "tools": {key: {"alarm": s[i.run_id][0], "score": s[i.run_id][1]} for key, s in tools.items()
                   if i.run_id in s}}
        for i in items
    ]


def _pair_rows(matching: Matching) -> list[dict[str, Any]]:
    return [{"design": matching.design, "enacted": e.run_id, "clean": c.run_id, "task": e.cluster,
             "enacted_length": e.length, "clean_length": c.length} for e, c in matching.pairs]


def _task_key(item: Item) -> str:
    return item.cluster


def _family_key(item: Item) -> str:
    return item.family


def run_matched(
    corpora: Sequence[str], *, findings: Sequence[Path], results_v1: Path, database: Path, repo_root: Path,
    results_root: Path | None, cutoff: str, resamples: int, seed: int,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    when = _at(cutoff)
    out: dict[str, Any] = {}
    private: dict[str, list[dict[str, Any]]] = {}
    for corpus in corpora:
        if corpus == "goodfire-dev":
            items = goodfire_dev_items(database=database, repo_root=repo_root)
        else:
            items = cheatbench_matched_items(database=database, repo_root=repo_root, results_root=results_root)
        tools, coverage = collect_all_tools(
            items, "goodfire" if corpus == "goodfire-dev" else "cheatbench",
            findings=findings, results_v1=results_v1, cutoff=when)
        result = analyse_matched(
            items, tools, coverage, primary_key=_task_key, secondary_key=_family_key,
            resamples=resamples, seed=seed, with_verdict=corpus == "goodfire-dev")
        pairs: Matching = result.pop("_pairs")
        result["population"]["ids_sha256"] = hashlib.sha256(
            "\n".join(sorted(i.run_id for i in items)).encode()).hexdigest()
        out[corpus] = result
        private[corpus] = _private_rows(items, tools, tertiles(items))
        private[f"{corpus}-pairs"] = _pair_rows(pairs)
    return {"schema": SCHEMA, "cutoff": cutoff, "seed": seed, "resamples": resamples, "corpora": out}, private


def write_outputs(result: Mapping[str, Any], private: Mapping[str, Sequence[Mapping[str, Any]]],
                  out_dir: Path, private_dir: Path) -> str:
    markdown = render(result)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "RESULTS.md").write_text(markdown, encoding="utf-8")
    private_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in private.items():
        with (private_dir / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    return markdown

