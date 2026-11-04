"""Execute only parent-frozen scale inputs; no implicit preparation or model fallback."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from prepare_scale import (
    HERE,
    OLD,
    ROOT,
    digest,
    dump,
    keyed,
    load,
    need,
    rows,
    sample,
    verify,
    write_rows,
)

sys.path.insert(0, str(OLD))
import evaluate as historical  # noqa: E402
from collect import frozen_rubric_sha, load_module  # noqa: E402
from judge_batch import MODELS, quota_value  # noqa: E402
from judge_runner import LaneCaller, judge_run, slug  # noqa: E402

LANE = Path('/private/tmp/ov1106/impscale')
CONFOUND = ('Strict views use submitted pre-G1 diffs; historical negative views use post-restoration diffs. '
            'This is an input-provenance confound, not paired all-task randomization.')


def asset_hashes(fixed):
    return {**fixed['historical_assets'],
            str((HERE.parent / 'scale_summary.py').relative_to(ROOT)): digest(HERE.parent / 'scale_summary.py'),
            **{str(p.relative_to(ROOT)): digest(p)
               for p in HERE.iterdir() if p.suffix in ('.py', '.json', '.md')}}


def gate(prepared, frozen_path):
    # This helper never invokes Git. The integration owner supplies exact-byte commit attestations.
    frozen = load(frozen_path)
    fixed = sample()
    need(frozen.get('pushed') is True, 'parent has not attested pushed preregistration')
    for field in ('preregistration_commit', 'observed_manifest_commit'):
        value = frozen.get(field, '')
        need(len(value) == 40 and all(c in '0123456789abcdef' for c in value), f'missing full {field}')
    need(frozen.get('historical_commit') == fixed['historical_commit'], 'historical freeze differs')
    assets = asset_hashes(fixed)
    need(frozen.get('assets') == assets and frozen.get('committed_assets') == assets,
         'parent committed-byte attestation or current asset digest differs')
    verify({str(ROOT / p): sha for p, sha in assets.items()})
    index = prepared / 'input-hashes.json'
    need(frozen.get('input_index_path') == str(index.resolve()), 'prepared input index differs')
    need(frozen.get('input_index_sha256') == digest(index) == frozen.get('committed_input_index_sha256'),
         'observed input index lacks committed digest attestation')
    inputs = load(index)
    need(frozen.get('inputs') == inputs, 'actual observed frozen input mapping differs')
    verify(inputs)
    for filename in ('judge-runs.jsonl', 'labels.jsonl', 'dispositions.jsonl', 'review-template.jsonl', 'native/spec.json'):
        p = prepared / filename
        need(inputs.get(str(p.resolve())) == digest(p), f'unfrozen prepared artifact: {p}')
    views = keyed(rows(prepared / 'judge-runs.jsonl'))
    labels = keyed(rows(prepared / 'labels.jsonl'))
    need(set(views) == set(labels), 'label/view sample differs')
    need({k for k, r in labels.items() if r['label'] == 0} == set(fixed['historical_negative_ids']),
         'negative sample differs')
    need({k for k, r in labels.items() if r['group'] == 'strict'} <= {r['id'] for r in fixed['intended']},
         'unintended strict input')
    need(all(r.get('review') is not None for r in labels.values()
             if r.get('disposition') == 'ambiguous_divergence'),
         'every admitted G1-pass/G2-fail needs a completed manual positive/ambiguous/exclude review')
    for key, row in views.items():
        need(set(row) == {'run_key', 'set', 'view'}, 'unexpected judge row fields')
        p = Path(row['view'])
        need(inputs.get(str(p.resolve())) == digest(p), 'unfrozen judge view')
        need(load(p)['run_key'] == key, 'view identity differs')
    need((prepared / 'native/empty-autoreview.jsonl').read_bytes() == b'', 'AutoReview cache is not empty')
    need((prepared / 'native/empty-verdicts.jsonl').read_bytes() == b'', 'station verdicts are not empty')
    return frozen


def request(args):
    fixed = sample()
    index = args.prepared / 'input-hashes.json'
    inputs = load(index)
    verify(inputs)
    assets = asset_hashes(fixed)
    value = {'historical_commit': fixed['historical_commit'], 'pushed': False,
             'preregistration_commit': None, 'observed_manifest_commit': None,
             'assets': assets, 'committed_assets': None,
             'input_index_path': str(index.resolve()), 'input_index_sha256': digest(index),
             'committed_input_index_sha256': None, 'inputs': inputs}
    need(not args.out.exists(), 'refuse existing freeze request')
    dump(args.out, value)
    print(json.dumps({'request': str(args.out), 'model_calls': 0}))


async def judge(args):
    destination = args.out / ('j2b__' + slug(MODELS[args.judge]))
    need(not destination.exists(), 'refuse judge result reuse or overwrite')
    results = destination / 'results'
    results.mkdir(parents=True)
    usage = subprocess.check_output(['omp', 'usage'], cwd=ROOT, text=True)
    (destination / 'quota.txt').write_text(usage)
    codex = quota_value(usage, 'Openai Codex —', r'7 days')
    gemini = quota_value(usage, 'Google Antigravity —', r'Gemini \(Weekly\)')
    short = quota_value(usage, 'Google Antigravity —', r'Gemini \(5 Hour\)')
    need(codex < 82 and gemini < 78 and (args.judge != 'j2b' or short < 85), 'quota ceiling reached')
    dump(destination / 'launch.json', {'judge': args.judge, 'model': MODELS[args.judge],
         'freeze_sha256': digest(args.frozen), 'ts': datetime.now(UTC).isoformat(),
         'config': '/private/tmp/ov1106/shared/nofallback.yml'})
    caller = LaneCaller(destination)
    try:
        await caller.start()
        for row in rows(args.prepared / 'judge-runs.jsonl'):
            # Frozen caller stops all pending/subsequent windows on exact-model mismatch.
            await judge_run(caller, judge='j2b', model=MODELS[args.judge], row=row,
                            window_chars=60000, seed=None, out_path=results / (slug(row['run_key']) + '.json'))
    finally:
        await caller.close()


def l0(args):
    args.out.mkdir(parents=True, exist_ok=False)
    raw = args.out / 'raw'
    subprocess.run([sys.executable, str(ROOT / 'research/experiments/ov1104-detbench/l0_rules.py'),
                    '--runs', str(args.prepared / 'judge-runs.jsonl'), '--view', 'full', '--workers', '1',
                    '--signals-dir', str(args.out), '--raw-dir', str(raw)], check=True, cwd=ROOT)
    native = keyed(rows(raw / 'l0v_full_raw.jsonl'))
    need(set(native) == set(keyed(rows(args.prepared / 'judge-runs.jsonl'))), 'L0 sample coverage differs')
    contract = load(HERE / 'contract.json')
    for name in ('l0v_full_l0_hard_gate_rule_hidden_info_read', 'l0v_full_any_hard'):
        output = []
        for key, row in native.items():
            fired = ('L0_HARD_GATE_RULE:hidden_info_read' in row['reasons'] if name.endswith('hidden_info_read')
                     else any(r.startswith('L0_HARD_') for r in row['reasons']))
            output.append({'run_key': key, 'version': contract['signals'][name]['version'],
                           'score': None if row.get('error') else int(fired), 'abstain': row.get('error'),
                           'model': None, 'raw_sha256': digest(raw / 'l0v_full_raw.jsonl')})
        write_rows(args.out / (name + '.jsonl'), output)


def collect(args):
    runs = keyed(rows(args.prepared / 'judge-runs.jsonl'))
    contract = load(HERE / 'contract.json')
    expected_rubric = frozen_rubric_sha()
    lib = load_module('scale_frozen_lib', ROOT / 'research/experiments/ov1104-detbench/detbench_lib.py')
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ('length_steps', 'length_chars'):
        output = []
        for key, row in runs.items():
            sizes = lib.view_size(load(row['view']))
            output.append({'run_key': key, 'version': contract['signals'][name]['version'],
                           'score': sizes[0 if name == 'length_steps' else 1], 'abstain': None, 'model': None})
        path = args.out / (name + '.jsonl')
        need(not path.exists(), 'refuse signal overwrite')
        write_rows(path, output)
    for name, spec in contract['signals'].items():
        if not spec.get('model'):
            continue
        model = spec['model']
        directory = args.judge_out / ('j2b__' + slug(model)) / 'results'
        results = keyed([load(p) for p in sorted(directory.glob('*.json'))])
        need(not set(results) - set(runs), 'unexpected judge result')
        output = []
        for key in runs:
            source = results.get(key, {})
            windows = source.get('windows') or []
            score, reason = source.get('score'), source.get('abstain')
            bad = any((w.get('model_served') is not None and w['model_served'] != model)
                      or (not w.get('error') and w.get('model_served') != model) for w in windows)
            if source.get('model') != model or not windows or bad:
                score, reason = None, 'missing or mismatched served model attestation'
            if source.get('rubric_sha256') != expected_rubric:
                score, reason = None, 'missing or mismatched frozen rubric'
            output.append({'run_key': key, 'version': spec['version'], 'score': score, 'abstain': reason,
                           'model': model if score is not None else None,
                           'partial_windows_failed': source.get('partial_windows_failed', []),
                           'window_failures': [{'window': w['window'], 'error': w['error']} for w in windows if w.get('error')]})
        path = args.out / (name + '.jsonl')
        need(not path.exists(), 'refuse signal overwrite')
        write_rows(path, output)


def native(args):
    from evallab.line.core import run_line
    destination = args.prepared / 'native/line-decisions.jsonl'
    need(not destination.exists(), 'refuse native decision overwrite')
    batch = run_line(args.prepared / 'native/spec.json', ROOT)
    traces = {r['run_id']: r for r in rows(batch / 'trace.jsonl')}
    version = load(HERE / 'contract.json')['line_version']
    output = [{'run_key': r['run_id'], 'version': version, 'decision': r['decision'],
               'reasons': r['reasons'], 'autoreview': traces[r['run_id']]['autoreview'],
               'batch': str(batch), 'decisions_sha256': digest(batch / 'decisions.jsonl')}
              for r in rows(batch / 'decisions.jsonl')]
    need({r['run_key'] for r in output} == set(keyed(rows(args.prepared / 'judge-runs.jsonl'))), 'native coverage differs')
    write_rows(destination, output)
    print(json.dumps({'batch': str(batch), 'rows': len(output)}))


def threshold(spec):
    return math.inf if spec['threshold'] == 'infinity' else spec['threshold']


def native_counts(selected, data, contract, metrics):
    for key, row in data.items():
        raw = Path(row['batch']) / 'decisions.jsonl'
        need(digest(raw) == row['decisions_sha256'], 'native raw decisions changed')
        original = {r['run_id']: r for r in rows(raw)}
        need(key in original and row['decision'] == original[key]['decision'], 'native export differs')
    result = {}
    for label, name in ((1, 'positive'), (0, 'negative')):
        group = [r for r in selected if r['label'] == label]
        counts = Counter()
        for row in group:
            reading = data.get(row['run_key'], {})
            value = reading.get('decision') if reading.get('version') == contract['line_version'] else None
            counts[value if value in ('keep', 'review', 'drop') else 'missing'] += 1
        result[name] = {'denominator': len(group), 'counts': dict(counts), 'rates': {}}
        for rule, n in (('drop_only', counts['drop']), ('review_or_drop', counts['drop'] + counts['review'])):
            result[name]['rates'][rule] = {'flagged': n, 'denominator': len(group),
                'value': n / len(group) if group else None, 'wilson95': metrics.wilson_ci(n, len(group))}
    return {'classes': result, 'auroc': None, 'autoreview_reasons': dict(Counter(
        reason for row in selected for reason in data.get(row['run_key'], {}).get('autoreview', {}).get('reasons', []))),
        'operating_condition': 'actual run_line; missing AutoReview empty cache and empty station; not deployed-model performance'}


def report(args, frozen):
    contract = load(HERE / 'contract.json')
    metrics = historical.load_metrics()
    audit = rows(args.prepared / 'labels.jsonl')
    selected = [r for r in audit if r['label'] is not None]
    dispositions = rows(args.prepared / 'dispositions.jsonl')
    signals, failures = {}, {}
    for name, spec in contract['signals'].items():
        path = args.signals / (name + '.jsonl')
        data = keyed(rows(path)) if path.exists() else {}
        need(not set(data) - {r['run_key'] for r in audit}, 'unexpected signal rows')
        signals[name], failures[name] = {}, []
        for row in selected:
            key = row['run_key']
            value, reason = historical.score_value(data.get(key), spec)
            signals[name][key] = value
            if reason or data.get(key, {}).get('window_failures'):
                failures[name].append({'run_key': key, 'reason': reason,
                                      'window_failures': data.get(key, {}).get('window_failures', [])})
    strict_tasks = {r['task_id'] for r in selected if r['label'] == 1}
    negative_tasks = {r['task_id'] for r in selected if r['label'] == 0}
    overlap = strict_tasks & negative_tasks
    subsets = {'primary_strict_vs_historical': selected,
               'strict_recall_only': [r for r in selected if r['label'] == 1],
               'task_matched': [r for r in selected if r['task_id'] in overlap],
               'all_signal_complete': [r for r in selected if all(signals[n][r['run_key']] is not None for n in signals)]}
    for field in ('model', 'mechanism'):
        for value in sorted({r.get(field) for r in selected if r['label'] == 1}):
            subsets[field + ':' + value] = [r for r in selected if r['label'] == 0 or r.get(field) == value]
    line_path = args.prepared / 'native/line-decisions.jsonl'
    line = keyed(rows(line_path)) if line_path.exists() else {}
    need(not set(line) - {r['run_key'] for r in audit}, 'unexpected native line rows')
    output = {}
    for subset_name, population in subsets.items():
        comparisons = {}
        for name, spec in contract['signals'].items():
            matched = [r for r in population if all(signals[n][r['run_key']] is not None
                       for n in (name, 'length_chars', 'length_steps'))]
            comparisons[name] = {'all_selected': historical.summarize(population, signals[name], threshold(spec), metrics, contract),
                'matched_with_lengths': {n: historical.summarize(matched, signals[n], threshold(contract['signals'][n]), metrics, contract)
                                         for n in dict.fromkeys((name, 'length_chars', 'length_steps'))}}
            if 'native_threshold' in spec:
                comparisons[name]['native_firing'] = historical.summarize(population, signals[name], spec['native_threshold'], metrics, contract)
        output[subset_name] = {'denominator': len(population), 'labels': dict(Counter(r['label'] for r in population)),
                              'signals': comparisons, 'full_line': native_counts(population, line, contract, metrics)}
    sys.path.insert(0, str(HERE.parent))
    from scale_summary import cluster_interval
    rates = {}
    for disposition in sorted({r['disposition'] for r in dispositions}):
        n = sum(r['disposition'] == disposition for r in dispositions)
        rates[disposition] = {'n': n, 'denominator': 100, 'rate': n / 100,
             'wilson95': metrics.wilson_ci(n, 100),
             'task_cluster95': cluster_interval(dispositions, lambda r, d=disposition: r['disposition'] == d)}
    def cohort_rates(population):
        counts = Counter(r['disposition'] for r in population)
        observed = [r for r in population if r['observed']]
        admitted = [r for r in population if r['admitted']]
        result = {'intended': len(population), 'observed': len(observed), 'admitted': len(admitted),
                  'driver_status': dict(Counter(r['outcome_status'] for r in observed)),
                  'dispositions': dict(counts), 'outcomes': {}}
        for name, predicate in (
                ('G1_pass', lambda r: r['G1_pass'] is True),
                ('G2_pass', lambda r: r['G2_pass'] is True),
                ('G1_pass_G2_fail', lambda r: r['G1_pass'] is True and r['G2_pass'] is False)):
            n = sum(predicate(r) for r in admitted)
            result['outcomes'][name] = {'n': n, 'admitted_denominator': len(admitted),
                'rate': n / len(admitted) if admitted else None,
                'wilson95': metrics.wilson_ci(n, len(admitted)),
                'task_cluster95': cluster_interval(admitted, predicate)}
        return result
    strict_groups = {'all_intended': cohort_rates(dispositions)}
    for field in ('model', 'task_id'):
        strict_groups[field] = {value: cohort_rates([r for r in dispositions if r[field] == value])
                                for value in sorted({r[field] for r in dispositions})}
    result = {'comparison': 'strict positives versus historical honest controls', 'input_provenance_confound': CONFOUND,
              'freeze_commits': {k: frozen[k] for k in ('historical_commit', 'preregistration_commit', 'observed_manifest_commit')},
              'strict_intended': 100, 'historical_honest_controls': 27, 'strict_dispositions': rates,
              'strict_groups': strict_groups,
              'task_overlap': sorted(overlap), 'subsets': output, 'failures': failures,
              'intent_claim': 'none; source-verified mechanisms do not prove intent',
              'inputs': {str(p): digest(p) for p in [*args.signals.glob('*.jsonl'), *([line_path] if line_path.exists() else [])]}}
    need(not args.out.exists(), 'refuse report overwrite')
    dump(args.out, result)
    print(json.dumps({'report': str(args.out), 'primary_denominator': len(selected), 'strict_intended': 100}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('freeze-request', 'gate', 'judge', 'l0', 'collect', 'native', 'report'))
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--frozen', type=Path, default=LANE / 'SCALE-FROZEN.json')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--judge', choices=tuple(MODELS))
    parser.add_argument('--judge-out', type=Path)
    parser.add_argument('--signals', type=Path)
    args = parser.parse_args()
    required = {'freeze-request': ('out',), 'judge': ('out', 'judge'), 'l0': ('out',),
                'collect': ('out', 'judge_out'), 'report': ('out', 'signals')}
    for name in required.get(args.mode, ()):
        if getattr(args, name) is None:
            parser.error(f'{args.mode} requires --{name.replace("_", "-")}')
    if args.mode == 'freeze-request':
        request(args)
        return
    frozen = gate(args.prepared, args.frozen)
    if args.mode == 'gate':
        print(json.dumps({'status': 'PASS', 'model_calls': 0}))
    elif args.mode == 'judge':
        asyncio.run(judge(args))
    elif args.mode == 'report':
        report(args, frozen)
    else:
        {'l0': l0, 'collect': collect, 'native': native}[args.mode](args)


if __name__ == '__main__':
    main()
