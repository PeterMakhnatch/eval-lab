#!/usr/bin/env python3
"""Aggregate OQ-8 cell receipts; no model calls or hidden task data."""
import argparse
import json
import math
import random
import time
from pathlib import Path

GATES = ('old_0_8', 'new_verified_0_5')


def complete(c):
    a, j, p = c['auditor'], c['judge'], c['paired']
    return (a['status'] == 'answered' and not a['fallback'] and not a['protocol_violations']
            and not j['fallback'] and j['status'] not in ('invalid', 'unanswered')
            and p['complete'] is True)


def total(values):
    values = list(values)
    return None if not values or any(v is None for v in values) else sum(values)


def ratio(n, d, scale=1):
    return None if d is None or not d else scale * n / d


def interval(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return [values[int(.025 * (len(values)-1))], values[int(.975 * (len(values)-1))]]


def gate_counts(c, gate):
    g = c['paired']['gates'][gate]
    return g['eligible_findings'], len(set(g['matched_key_ids'])) if c['keyed'] else 0


def rates(cells, gate):
    matches = sum(gate_counts(c, gate)[1] for c in cells if complete(c))
    tokens = total(total((c['auditor'][k], c['judge'][k])) for c in cells for k in ('tokens_in', 'tokens_out'))
    seconds = total(total((c['auditor']['wall_seconds'], c['judge']['wall_seconds'])) for c in cells)
    return ratio(matches, tokens, 1_000_000), ratio(matches, seconds, 3600)


def summarize(cells):
    valid = [c for c in cells if complete(c)]
    result = {'tasks': len(cells), 'complete_cells': len(valid), 'invalid_cells': len(cells)-len(valid),
              'keyed_tasks': sum(c['keyed'] for c in cells), 'subscription_cash_usd': None}
    for role in ('auditor', 'judge'):
        result[role] = {k: total(c[role][k] for c in cells) for k in ('tokens_in', 'tokens_out', 'wall_seconds')}
    result['total_tokens_in'] = total(result[r]['tokens_in'] for r in ('auditor', 'judge'))
    result['total_tokens_out'] = total(result[r]['tokens_out'] for r in ('auditor', 'judge'))
    result['total_tokens'] = total((result['total_tokens_in'], result['total_tokens_out']))
    seconds = total(result[r]['wall_seconds'] for r in ('auditor', 'judge'))
    result['wall_hours_sum'] = None if seconds is None else seconds / 3600
    result['elapsed_if_sequential_hours'] = result['wall_hours_sum']
    result['gates'] = {}
    for gate in GATES:
        eligible = sum(gate_counts(c, gate)[0] for c in valid)
        matched = sum(gate_counts(c, gate)[1] for c in valid)
        rng = random.Random(0)
        boot = [rates([rng.choice(cells) for _ in cells], gate) for _ in range(10000)] if cells else []
        per_token, per_hour = rates(cells, gate)
        result['gates'][gate] = {'eligible_findings': eligible, 'matched_keys': matched,
            'keyed_tasks_hit': sum(gate_counts(c, gate)[1] > 0 for c in valid if c['keyed']),
            'key_backed_precision': ratio(matched, eligible),
            'confirmed_per_1m_tokens': per_token, 'confirmed_per_1m_tokens_ci95': interval(x[0] for x in boot) if per_token is not None else None,
            'confirmed_per_wall_hour': per_hour, 'confirmed_per_wall_hour_ci95': interval(x[1] for x in boot) if per_hour is not None else None}
    return result


def paired(a, b):
    aa = {c['case_id']: c for c in a if complete(c) and c['keyed']}
    bb = {c['case_id']: c for c in b if complete(c) and c['keyed']}
    ids = sorted(aa.keys() & bb.keys())
    out = {}
    for gate in GATES:
        ds = [gate_counts(bb[i], gate)[1] - gate_counts(aa[i], gate)[1] for i in ids]
        pos, neg = sum(x > 0 for x in ds), sum(x < 0 for x in ds)
        n = pos + neg
        p = min(1., 2 * sum(math.comb(n, i) for i in range(min(pos, neg)+1)) / 2**n) if n else 1.
        rng = random.Random(0)
        boot = [sum(rng.choice(ds) for _ in ds) / len(ds) for _ in range(10000)] if ds else []
        out[gate] = {'shared_complete_keyed_tasks': len(ds), 'mean_matched_keys_difference_B_minus_A': sum(ds)/len(ds) if ds else None,
                     'difference_ci95': interval(boot), 'B_wins': pos, 'A_wins': neg,
                     'ties': len(ds)-n, 'sign_test_two_sided_p': p if ds else None}
    return out


def aggregate(cells):
    seen = set()
    for c in cells:
        key = (c['stage'], c['arm'], c['case_id'])
        if key in seen:
            raise ValueError(f'duplicate cell: {key}')
        seen.add(key)
        if c['stage'] not in ('dev', 'cross') or c['arm'] not in ('A', 'B'):
            raise ValueError(f'unknown stage or arm: {key}')
        if c['keyed'] != (c['key_count'] > 0):
            raise ValueError(f'inconsistent key metadata: {key}')
    out = {'bootstrap': {'unit': 'task', 'seed': 0, 'replicates': 10000, 'interval': 'percentile'},
           'notes': ['Confirmed means matched to an admitted answer key, not an executed grader differential.',
                     'Unkeyed tasks are not negatives; their findings and costs remain in arm totals, but they are excluded from paired comparisons.',
                     'Costs include invalid cells; findings and matches include complete valid cells only.',
                     'Wall time is the sum of auditor and judge durations; sequential elapsed is this same sum, not observed calendar elapsed.',
                     'Subscription cash: N/A.'], 'stages': {}}
    for stage in ('dev', 'cross'):
        arms = {arm: [c for c in cells if c['stage'] == stage and c['arm'] == arm] for arm in ('A', 'B')}
        expected = 64 if stage == 'dev' else 12
        out['stages'][stage] = {'expected_tasks_per_arm': expected,
            'missing_cells_by_arm': {arm: max(0, expected-len(rows)) for arm, rows in arms.items()},
            'arms': {arm: summarize(rows) for arm, rows in arms.items()}, 'paired_B_minus_A': paired(arms['A'], arms['B'])}
    return out


def report(data):
    lines = ['# OQ-8 data-access metrics', '', *('- ' + n for n in data['notes']), '', 'Bootstrap: 10,000 task resamples, seed 0; percentile 95% intervals. Paired differences are mean matched keys per shared complete keyed task; sign test is exact and excludes ties.', '']
    for stage, s in data['stages'].items():
        lines += [f"## {stage}", '', f"Expected per arm: {s['expected_tasks_per_arm']}; missing cells: {s['missing_cells_by_arm']}.", '']
        for arm, a in s['arms'].items():
            lines += [f'### Arm {arm}', '', '```json', json.dumps(a, indent=2), '```', '']
        lines += ['### Paired B − A', '', '```json', json.dumps(s['paired_B_minus_A'], indent=2), '```', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cells', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 1200:
        parser.error('wait-seconds must be between 0 and 1200')
    deadline = time.monotonic() + args.wait_seconds
    while True:
        paths = sorted(args.cells.glob('*/*/*/cell.json'))
        if len(paths) >= 152 or time.monotonic() >= deadline:
            break
        time.sleep(min(10, max(0, deadline-time.monotonic())))
    cells = [json.loads(p.read_text()) for p in paths]
    data = aggregate(cells)
    data['source_cells'] = [str(p) for p in paths]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'results.json').write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    (args.output / 'report.md').write_text(report(data) + '\n')
    print(f'Aggregated {len(cells)} cells into {args.output}')


if __name__ == '__main__':
    main()
