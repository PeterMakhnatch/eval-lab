#!/usr/bin/env python3
"""Retrospective DEV-key retrieval; rankings are fixed before labels are joined."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

# Prespecified, equal-weight signal list; never learn weights from keys.
SIGNALS = ("pass_tests_modified", "pass_answer_material", "pass_suspicion_ge2",
           "shortcut_passes", "pass_eval_awareness_grader", "pass_success_claim_no_evidence",
           "pass_rate_anomaly", "cluster_suspect_share", "max_suspicion")
SEED = 0
PERMUTATIONS = 10000
BOOTSTRAPS = 1000
CAVEAT = "Unkeyed tasks are not certified negatives; keyed yield is a lower bound on true defect yield."


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    keys = [(r['task_id'], r['dataset_id']) for r in rows]
    if len(keys) != len(set(keys)) or len({k[0] for k in keys}) != len(keys):
        raise ValueError(f"Duplicate task identity in {path}")
    return {key: row for key, row in zip(keys, rows, strict=True)}


def load(labels_path, features_path, context_path):
    labels, features, context = (read_jsonl(p) for p in (labels_path, features_path, context_path))
    if set(labels) != set(features) or set(labels) != set(context):
        raise ValueError("Input populations differ; refusing silent intersection")
    rows = []
    for key in sorted(labels):
        label, f, c = labels[key], features[key], context[key]
        if label['split'] != 'dev' or type(label['keyed']) is not bool or label['keyed'] != (label['key_count'] > 0):
            raise ValueError(f"Invalid DEV label: {key}")
        # Overlapping fields must agree; features remain label-free.
        for name in set(f) & set(c) - {'task_id', 'dataset_id'}:
            if f[name] != c[name]:
                raise ValueError(f"Conflicting feature {name}: {key}")
        row = {**f, **c, 'keyed': label['keyed'], 'key_count': label['key_count']}
        rows.append(row)
    if not rows:
        raise ValueError('Empty population')
    return rows


def ranks(rows):
    """Known values ranked descending; unknowns receive independent random percentile ranks.

    Seeded random tie keys break all ties. Unknowns interleave uniformly with known
    tasks rather than being treated as clean or being systematically placed last.
    """
    n = len(rows)
    rng = np.random.default_rng(SEED)
    tie = rng.random(n)
    result = {'S0_random': (np.argsort(tie), tie)}
    percentiles = []
    for name in SIGNALS + ('pass_rate', 'runs'):
        facet_signal = name not in ('pass_rate', 'runs')
        values = np.array([np.nan if (facet_signal and r['facet_coverage'] == 0) or r[name] is None
                           else abs(float(r[name])) if name == 'pass_rate_anomaly' else float(r[name])
                           for r in rows])
        known = np.flatnonzero(np.isfinite(values))
        unknown = np.flatnonzero(~np.isfinite(values))
        order = known[np.lexsort((tie[known], -values[known]))]
        # Known ranks spread over the entire percentile range, not just its upper end.
        score = np.empty(n)
        score[order] = (np.arange(len(order)) + 0.5) / max(len(order), 1)
        score[unknown] = rng.random(len(unknown))
        result[name] = (np.lexsort((tie, score)), score)
        if name in SIGNALS:
            percentiles.append(score)
    combined = np.mean(percentiles, axis=0)
    result['COMBINED'] = (np.lexsort((tie, combined)), combined)
    return result


def metrics(y, keys, cuts) -> dict[str, Any]:
    n = len(y)
    positives = int(np.sum(y))
    base = positives / n
    cum = np.cumsum(y)
    out = {}
    for k in cuts:
        v = float(cum[k - 1] / k)
        out[str(k)] = {'keyed_tasks': int(cum[k - 1]), 'yield': v,
                       'enrichment': v / base if base else None,
                       'key_level_yield': float(np.sum(keys[:k]) / k)}
    ap = float(np.sum(cum[y.astype(bool)] / (np.flatnonzero(y) + 1)) / positives) if positives else None
    negatives = n - positives
    auc = float((np.sum((n - 1 - np.flatnonzero(y))) - positives * (positives - 1) / 2) / (positives * negatives)) if positives and negatives else None
    cost = {str(t): int(np.flatnonzero(cum >= t)[0] + 1) if positives >= t else None for t in (5, 10)}
    return {'at_k': out, 'average_precision': ap, 'auroc': auc, 'cost': cost}


def evaluate_group(rows, orders, permutations=PERMUTATIONS, bootstraps=BOOTSTRAPS):
    n = len(rows)
    y = np.array([int(r['keyed']) for r in rows], dtype=int)
    keys = np.array([r['key_count'] for r in rows], dtype=int)
    positives = int(y.sum())
    cuts = sorted(set([k for k in (10, 20, 40, 80) if k <= n] + [n]))
    rng = np.random.default_rng(SEED)
    # Independent random order samples, one shared baseline per population.
    samples = {str(k): [] for k in cuts}
    ap_samples, auc_samples = [], []
    costs = {'5': [], '10': []}
    for _ in range(permutations):
        p = rng.permutation(n)
        m = metrics(y[p], keys[p], cuts)
        for k in cuts:
            samples[str(k)].append(m['at_k'][str(k)]['yield'])
        if m['average_precision'] is not None:
            ap_samples.append(m['average_precision'])
        if m['auroc'] is not None:
            auc_samples.append(m['auroc'])
        for t in costs:
            if m['cost'][t] is not None:
                costs[t].append(m['cost'][t])

    def interval(a):
        return [float(x) for x in np.quantile(a, [0.025, 0.975])] if len(a) else None

    baseline = {'exact_hypergeometric_expected_yield': positives / n,
                'exact_expected_key_level_yield': float(keys.sum() / n),
                'permutations': permutations,
                'yield': {k: {'mean': float(np.mean(v)), 'interval_95': interval(v)} for k, v in samples.items()},
                'average_precision_mean': float(np.mean(ap_samples)) if ap_samples else None,
                'auroc_mean': float(np.mean(auc_samples)) if auc_samples else None,
                'cost': {t: {'exact_expectation': int(t) * (n + 1) / (positives + 1) if positives >= int(t) else None,
                             'permutation_mean': float(np.mean(v)) if v else None,
                             'permutation_interval_95': interval(v)} for t, v in costs.items()}}
    schemes = {}
    # Bootstrap tasks with replacement; retain the prespecified original ranking,
    # including seeded tie decisions. This measures sampling uncertainty conditional on that ranking.
    draws = rng.integers(0, n, size=(bootstraps, n))
    for name, order in orders.items():
        m = metrics(y[order], keys[order], cuts)
        pos = np.empty(n, dtype=int)
        pos[order] = np.arange(n)
        ci = {'average_precision': [], 'auroc': [], 'cost_first_5': [], 'cost_first_10': []}
        for k in cuts:
            for field in ('yield', 'enrichment', 'key_level_yield'):
                ci[f'{field}@{k}'] = []
        for draw in draws:
            d = draw[np.argsort(pos[draw], kind='stable')]
            b = metrics(y[d], keys[d], cuts)
            for field in ('average_precision', 'auroc'):
                if b[field] is not None:
                    ci[field].append(b[field])
            for target in ('5', '10'):
                if b['cost'][target] is not None:
                    ci[f'cost_first_{target}'].append(b['cost'][target])
            for k in cuts:
                for field in ('yield', 'enrichment', 'key_level_yield'):
                    val = b['at_k'][str(k)][field]
                    if val is not None:
                        ci[f'{field}@{k}'].append(val)
        m['bootstrap_ci_95'] = {k: interval(v) for k, v in ci.items()}
        schemes[name] = m
    return {'n': n, 'keyed_tasks': positives, 'key_count': int(keys.sum()),
            'base_rate': positives / n, 'cutoffs': cuts, 'random_baseline': baseline, 'schemes': schemes}


def main():
    p = argparse.ArgumentParser()
    for arg in ('labels', 'features', 'context', 'output'):
        p.add_argument('--' + arg, required=True, type=Path)
    args = p.parse_args()
    rows = load(args.labels, args.features, args.context)
    ranking = ranks(rows)  # Labels never influence rank scores.
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / 'ranking.jsonl').open('w', encoding='utf-8') as f:
        for name, (order, score) in ranking.items():
            for i, idx in enumerate(order):
                f.write(json.dumps({'task_id': rows[idx]['task_id'], 'scheme': name, 'rank': i + 1, 'score': float(score[idx])}) + '\n')
    groups = {'all': np.arange(len(rows)), 'facets_only': np.array([i for i, r in enumerate(rows) if r['facet_coverage'] > 0], dtype=int)}
    for dataset in sorted({r['dataset_id'] for r in rows}):
        groups['dataset:' + dataset] = np.array([i for i, r in enumerate(rows) if r['dataset_id'] == dataset], dtype=int)
        groups['dataset_facets_only:' + dataset] = np.array([i for i, r in enumerate(rows) if r['dataset_id'] == dataset and r['facet_coverage'] > 0], dtype=int)
    results: dict[str, Any] = {'caveat': CAVEAT, 'seed': SEED, 'signals': list(SIGNALS), 'bootstrap_replicates': BOOTSTRAPS,
               'method': 'Unknown facet-dependent signals receive seeded random percentile ranks; tied observed values use seeded random tie keys. Combined is mean descending percentile rank. Group rankings are restrictions of the global prespecified ranking. Bootstrap resamples tasks and retains their global ranking. K values greater than population size are omitted.',
               'inputs_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.labels, args.features, args.context)}, 'groups': {}}
    for group, indices in groups.items():
        if not len(indices):
            continue
        mapping = {int(idx): i for i, idx in enumerate(indices)}
        orders = {name: np.array([mapping[int(i)] for i in order if int(i) in mapping], dtype=int) for name, (order, _) in ranking.items()}
        results['groups'][group] = evaluate_group([rows[i] for i in indices], orders)
    (args.output / 'results.json').write_text(json.dumps(results, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    lines = ['# OQ-7 evaluator', '', CAVEAT, '', results['method'], '', f"Seed {SEED}; {PERMUTATIONS} random permutations and {BOOTSTRAPS} task bootstraps per population. Ranks and signals are prespecified; no weights are fit to labels.", '', 'Combined signals: ' + ', '.join(SIGNALS) + '.', '']
    lines += [f"Inputs: `{args.labels}`, `{args.features}`, `{args.context}`; SHA-256 digests are in results.json. Run: `python {Path(__file__)} --labels {args.labels} --features {args.features} --context {args.context} --output {args.output}`.", '',
              "See A/report.md, B1/report.md, and B2/report.md for population construction, strict DEV exclusions, feature definitions, and source caveats. Not all eligible answer-key cases mapped to tasks; keys can describe the same underlying defect. Incomplete facet coverage is not evidence of absence. Dataset and facets-only rankings restrict the global order; empty strata are omitted. AUROC uses the seeded tie-broken audit order, not half-credit for score ties. Bootstrap intervals are conditional on the fixed global ranking and exclude undefined replicates (including costs when too few keyed tasks are sampled).", '']
    for name, g in results['groups'].items():
        lines += [f"## {name}", f"Tasks: {g['n']}; keyed tasks: {g['keyed_tasks']}; keys: {g['key_count']}; base rate: {g['base_rate']:.4f}.", '', '| Scheme | AP | AUROC | Yield@10 | Yield@20 | Cost first 5 | Cost first 10 |', '|---|---:|---:|---:|---:|---:|---:|']
        def fmt(x):
            return 'NA' if x is None else f'{x:.3f}' if isinstance(x, float) else str(x)

        for scheme, m in g['schemes'].items():
            lines.append('| ' + ' | '.join([scheme, fmt(m['average_precision']), fmt(m['auroc']), fmt(m['at_k'].get('10', {}).get('yield')), fmt(m['at_k'].get('20', {}).get('yield')), fmt(m['cost']['5']), fmt(m['cost']['10'])]) + ' |')
        b = g['random_baseline']
        lines += ['', f"Random exact expected yield: {b['exact_hypergeometric_expected_yield']:.4f}; expected cost first 5/10: {fmt(b['cost']['5']['exact_expectation'])}/{fmt(b['cost']['10']['exact_expectation'])}. Full cutoffs, enrichment, key-level yields, permutation distributions, and 95% bootstrap CIs are in results.json.", '']
    (args.output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"Wrote {len(rows)} tasks, {len(ranking)} schemes, {len(results['groups'])} populations to {args.output}")


if __name__ == '__main__':
    main()
