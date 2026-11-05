"""Length-confounding diagnosis for existing judges; never fits on held-out labels.

CLI defaults reproduce the J3 diagnosis. ``analyze(rows, score_field, thresholds,
bin_edges, bootstrap, seed)`` also accepts J4 rows. Each row needs run_key,
cluster, label, n_chars, n_steps and a finite score in score_field. Thresholds
and bin edges must be frozen on DEV before a transfer evaluation. Only
aggregates and identity digests are emitted; input rows remain private.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DETBENCH = Path('/private/tmp/ov1104/shared/detbench')
MALT = Path.home() / '.local/state/eval-lab/ov1105-malt-060fefc3a'
HELDOUT_SETS = {
    'reflex-sentinel', 'caught-not-taught', 'flake-laundering', 'sleight-bench',
    'researcharena-benchmark-gaming', 'kernelbench-hard-runs', 'beam-tripwire',
    'latent-agent-sandbox-episodes', 'trusted-monitoring-logs',
    'resolvebench-support-agents', 'cape-bench',
}


def load_statistics() -> Any:
    directory = HERE.parent / 'ov1104-detbench'
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location('longjudge_detbench_stats', directory / 'score.py')
    if spec is None or spec.loader is None:
        raise ImportError('Cannot load existing DetBench statistics')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATS = load_statistics()


def read_rows(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            key = row['run_key']
            if key in result:
                raise ValueError(f'Duplicate identity in {path}')
            result[key] = row
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        for index in order[i:j]:
            result[index] = (i + j - 1) / 2 + 1
        i = j
    return result


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    xc = [v - mean_x for v in x]
    yc = [v - mean_y for v in y]
    xx, yy = sum(v*v for v in xc), sum(v*v for v in yc)
    if xx <= 1e-20 or yy <= 1e-20:
        return None
    return sum(a*b for a, b in zip(xc, yc, strict=True)) / math.sqrt(xx*yy)


def residual(values: list[float], controls: list[list[float]]) -> list[float]:
    """Orthogonal projection with an intercept; drops collinear controls."""
    if not values:
        return []
    basis = [[1 / math.sqrt(len(values))] * len(values)]
    for control in controls:
        vector = list(control)
        for q in basis:
            coefficient = sum(a*b for a, b in zip(vector, q, strict=True))
            vector = [a-coefficient*b for a, b in zip(vector, q, strict=True)]
        norm = math.sqrt(sum(v*v for v in vector))
        if norm > 1e-10:
            basis.append([v / norm for v in vector])
    result = list(values)
    for q in basis:
        coefficient = sum(a*b for a, b in zip(result, q, strict=True))
        result = [a-coefficient*b for a, b in zip(result, q, strict=True)]
    return result


def correlations(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    columns = {name: [float(r[key]) for r in rows] for name, key in
               [('label', 'label'), ('judge', field), ('length_chars', 'n_chars'), ('length_steps', 'n_steps')]}
    ranked = {key: ranks(value) for key, value in columns.items()}
    names = list(columns)
    pairs = {f'{a}__{b}': pearson(ranked[a], ranked[b])
             for i, a in enumerate(names) for b in names[i+1:]}
    logs = [[math.log1p(v) for v in columns[key]] for key in ('length_chars', 'length_steps')]
    controls = [ranks(v) for v in logs]
    return {
        'spearman': pairs,
        'partial_spearman_label_judge_given_rank_log1p_chars_steps': pearson(
            residual(ranked['label'], controls), residual(ranked['judge'], controls)),
        'sensitivity_rank_label_judge_given_unranked_log1p_chars_steps': pearson(
            residual(ranked['label'], logs), residual(ranked['judge'], logs)),
    }


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {'n': len(rows), 'positive': sum(r['label'] == 1 for r in rows),
            'negative': sum(r['label'] == 0 for r in rows),
            'task_clusters': len({r['cluster'] for r in rows}),
            'positive_task_clusters': len({r['cluster'] for r in rows if r['label'] == 1})}


def quintile_edges(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Linear empirical DEV quantiles; transfer bins are (-inf,e1], ... (e4,inf)."""
    result = {}
    for field in ('n_chars', 'n_steps'):
        values = sorted(float(r[field]) for r in rows)
        edges = []
        for q in (0.2, 0.4, 0.6, 0.8):
            index = (len(values)-1) * q
            lo = int(index)
            edges.append(values[lo] + (values[min(lo+1, len(values)-1)]-values[lo])*(index-lo))
        result[field] = edges
    return result


def analyze(rows: list[dict[str, Any]], score_field: str, thresholds: dict[str, dict[str, float]],
            bin_edges: dict[str, list[float]], bootstrap: int = 1000, seed: int = 1104) -> dict[str, Any]:
    """Compare a judge and lengths on identical rows, with fixed DEV operating points."""
    signals = {'judge': score_field, 'length_chars': 'n_chars', 'length_steps': 'n_steps'}
    by_signal = {name: {r['run_key']: (str(r['cluster']), int(r['label']), float(r[field]))
                        for r in rows} for name, field in signals.items()}
    result: dict[str, Any] = {'counts': counts(rows), 'identity_sha256': digest(sorted(r['run_key'] for r in rows)),
                              'correlations': correlations(rows, score_field), 'signals': {}, 'paired_auroc_differences': {}}
    for name, by_key in by_signal.items():
        triples = list(by_key.values())
        result['signals'][name] = {
            'auroc': STATS.auroc(triples), 'ci95': STATS.bootstrap_auroc(triples, bootstrap, seed),
            'frozen_dev_operating_points': {
                budget: {'threshold': threshold, **STATS.rates_at(triples, threshold)}
                for budget, threshold in thresholds[name].items()},
        }
    for baseline in ('length_chars', 'length_steps'):
        delta, low, high = STATS.bootstrap_auroc_diff(by_signal['judge'], by_signal[baseline], bootstrap, seed)
        result['paired_auroc_differences'][f'judge_minus_{baseline}'] = {'difference': delta, 'ci95': [low, high]}
    strata = {}
    for field, edges in bin_edges.items():
        bins = []
        for index in range(5):
            subset = [r for r in rows if sum(float(r[field]) > edge for edge in edges) == index]
            entry: dict[str, Any] = {'quintile': index+1, **counts(subset), 'signals': {}}
            for name, signal_field in signals.items():
                triples = [(str(r['cluster']), int(r['label']), float(r[signal_field])) for r in subset]
                entry['signals'][name] = {'auroc': STATS.auroc(triples),
                                          'ci95': STATS.bootstrap_auroc(triples, bootstrap, seed)}
            bins.append(entry)
        strata[field] = bins
    result['within_dev_length_quintiles'] = strata
    return result


def matched(runs: dict[str, dict[str, Any]], signal: dict[str, dict[str, Any]], keys: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    reasons: Counter[str] = Counter()
    missing_classes: Counter[str] = Counter()
    for key in sorted(keys):
        score = signal.get(key, {}).get('score')
        if score is None or not math.isfinite(float(score)):
            reasons['missing_signal_row' if key not in signal else 'null_or_nonfinite_score'] += 1
            missing_classes['positive' if runs[key]['label'] else 'negative'] += 1
            continue
        row = runs[key]
        if any(row.get(field) is None or not math.isfinite(float(row[field])) or float(row[field]) < 0
               for field in ('n_chars', 'n_steps')):
            raise ValueError('Invalid length baseline')
        rows.append({**row, 'judge_score': float(score)})
    coverage = {'selected': counts([runs[k] for k in keys]), 'scored': counts(rows),
                'excluded_reasons': dict(reasons), 'excluded_classes': dict(missing_classes),
                'selected_identity_sha256': digest(sorted(keys)),
                'scored_numeric_rows_sha256': digest([[r[k] for k in ('run_key', 'cluster', 'label', 'n_chars', 'n_steps', 'judge_score')] for r in rows]),
                'sets': {s: {'selected': counts([runs[k] for k in keys if runs[k]['set'] == s]),
                              'scored': counts([r for r in rows if r['set'] == s])}
                         for s in sorted({runs[k]['set'] for k in keys})}}
    return rows, coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--detbench', type=Path, default=DETBENCH)
    parser.add_argument('--malt', type=Path, default=MALT)
    parser.add_argument('--bootstrap', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=1104)
    parser.add_argument('--out', type=Path, default=HERE / 'diagnosis.json')
    args = parser.parse_args()
    if args.bootstrap < 20:
        parser.error('--bootstrap must be at least 20')
    paths = {'detbench_runs': args.detbench / 'runs.jsonl',
             'detbench_j3': args.detbench / 'signals/j3_actions_task_diff_terra_full.jsonl',
             'malt_runs': args.malt / 'detbench-frozen/runs.jsonl',
             'malt_j3': args.malt / 'scores/signals/j3_actions_task_diff_terra.jsonl',
             'thresholds': args.malt / 'scores/thresholds.json'}
    runs, signal = read_rows(paths['detbench_runs']), read_rows(paths['detbench_j3'])
    malt_runs, malt_signal = read_rows(paths['malt_runs']), read_rows(paths['malt_j3'])
    dev_keys = [k for k, r in runs.items() if r['split'] == 'dev']
    heldout_keys = [k for k, r in runs.items() if r['split'] == 'heldout' and r['set'] in HELDOUT_SETS
                    and not r.get('decisive_rule') and r.get('pool') is not False]
    if (len(dev_keys), len(heldout_keys), len(malt_signal)) != (690, 1450, 400):
        raise ValueError('Source cohort size differs from DEV690/heldout1450/MALT400')
    if set(malt_signal) - set(malt_runs):
        raise ValueError('MALT scores have unknown identities')
    frozen = json.loads(paths['thresholds'].read_text())['signals']
    thresholds = {name: frozen[source]['thresholds'] for name, source in
                  [('judge', 'j3_actions_task_diff_terra'), ('length_chars', 'length_chars'), ('length_steps', 'length_steps')]}
    edges = quintile_edges([runs[k] for k in dev_keys])
    report = {
        'purpose': 'Existing J3 diagnosis only; held-out results must not tune J4.',
        'method': {'bootstrap': args.bootstrap, 'seed': args.seed, 'cluster': 'source task cluster, as recorded',
                   'auroc_ci': '95% task-cluster percentile bootstrap; single-class resamples omitted; existing DetBench implementation',
                   'proportion_ci': 'Wilson 95%; conditional on scored subset; not cluster-adjusted',
                   'partial_spearman': 'Pearson correlation of label and judge midrank residuals after intercept and ranked log1p lengths',
                   'sensitivity': 'same ranked outcomes residualized on unranked log1p lengths',
                   'bins': 'linear quintiles on all 690 DEV rows, labels unused; upper boundary inclusive; no transfer refitting'},
        'inputs': {name: file_identity(path) for name, path in paths.items()},
        'code': file_identity(Path(__file__)),
        'statistics_code': file_identity(HERE.parent / 'ov1104-detbench/score.py'),
        'command_argv': sys.argv,
        'signal_identities': {'detbench': sorted({(r.get('version'), r.get('model')) for r in signal.values()}, key=str),
                              'malt': sorted({(r.get('version'), r.get('model')) for r in malt_signal.values()}, key=str)},
        'frozen_dev_thresholds': thresholds, 'dev_length_quintile_edges': edges, 'cohorts': {},
    }
    for name, source_runs, source_signal, keys in [
        ('DEV690', runs, signal, dev_keys), ('heldout1450', runs, signal, heldout_keys),
        ('MALT400', malt_runs, malt_signal, list(malt_signal)),
    ]:
        rows, coverage = matched(source_runs, source_signal, keys)
        result = analyze(rows, 'judge_score', thresholds, edges, args.bootstrap, args.seed)
        result['coverage'] = coverage
        result['recorded_model_counts_scored'] = dict(Counter(
            str(source_signal[r['run_key']].get('model')) for r in rows))
        terra_only = [r for r in rows if source_signal[r['run_key']].get('model') == 'openai-codex/gpt-6.1-terra']
        if len(terra_only) != len(rows):
            result['exact_terra_model_field_sensitivity'] = analyze(
                terra_only, 'judge_score', thresholds, edges, args.bootstrap, args.seed)
            result['exact_terra_model_field_sensitivity']['warning'] = (
                'Historical signal model field only; not independent served-route attestation. '
                'Subset changes the cohort; no threshold or bin refit.')
        result['per_set_correlations'] = {s: correlations([r for r in rows if r['set'] == s], 'judge_score')
                                          for s in sorted({r['set'] for r in rows})}
        report['cohorts'][name] = result
        print(f"{name}: scored={len(rows)}/{len(keys)} positives={result['counts']['positive']} "
              f"J3={result['signals']['judge']['auroc']} chars={result['signals']['length_chars']['auroc']} "
              f"steps={result['signals']['length_steps']['auroc']} "
              f"partial_spearman={result['correlations']['partial_spearman_label_judge_given_rank_log1p_chars_steps']}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(f'aggregate_output={args.out} sha256={hashlib.sha256(args.out.read_bytes()).hexdigest()}', flush=True)


if __name__ == '__main__':
    main()
