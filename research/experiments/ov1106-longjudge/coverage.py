#!/usr/bin/env python3
"""Reproduce existing action-judge coverage without calls or per-run exports.

Run: python3 research/experiments/ov1106-longjudge/coverage.py --output
research/experiments/ov1106-longjudge/coverage.json
Optional --reference compares the previous private audit, not an input to measurements.
Payload means normalized action arguments/observations, not native transcript bytes.
Input manifests hash sorted [path, byte SHA256] pairs; identities hash sorted run keys.
Only passage fingerprints, coordinates, and visibility flags leave the private inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGES = HERE.parent / 'ov1104-judges'
sys.path.insert(0, str(JUDGES))
from prompts import j2_prompt  # noqa: E402
from views import (  # noqa: E402
    DIFF_CHARS,
    STEP_ARGS_CHARS,
    STEP_OBS_CHARS,
    TASK_CHARS,
    WINDOW_CHARS,
    render_windows,
)

TERRA = 'openai-codex/gpt-6.1-terra'
SHARED = Path('/private/tmp/ov1104/shared/detbench')
ORIGINAL = Path('/private/tmp/ov1104/judges/out')
MALT = Path('/private/tmp/ov1105/malt')
HACK = Path('/private/tmp/ov1105/hackbench/evaluation')
J2 = 'j2b_actions_task_diff_v2_flash'
J3 = 'j3_actions_task_diff_terra'
PASSAGES = [
    ('342625', 92, 'args', 227, 26, '20848e053c22dd5fda7ccc9486bc796e465fb128454a5a029d9b24cd64febe0b'),
    ('342625', 93, 'observation', 434, 28, '9585eb5d02b961503e844dbe04a88abf005dd35531df3db09a1cf981b06014ea'),
    ('345756', 140, 'args', 860, 17, '812a25ba76acedd592817e8080761c147af459a18a34e8d92742767ff0f41070'),
    ('345756', 145, 'observation', 497, 31, 'b18aa2f4fb1477aadff4c3d364578bf73a7240d02dbd9c2f0d67574033fa395e'),
    ('342367', 76, 'args', 2079, 25, '40d327c105064f391c3ff9cd915c6f720f41789bd831672b8184939ffeccf229'),
    ('342367', 76, 'args', 2496, 36, '65511a6c8cd17117e440cb518916abdf05e3e31f88b5920bb665db8d2d7094be'),
    ('342624', 116, 'args', 3366, 21, 'b542e5e64c3bc7e7baee6e9958febe47bdf6fa18a0d72a1e8339b8fc1fcfc548'),
    ('342624', 117, 'observation', 698, 40, '4eb550291d75487a62cb9a1503b65e2a2e80a694515de52c2d7eb519d8703b7e'),
    ('342624', 117, 'observation', 793, 48, 'a7cc6936e972f5925b7a09059cd4eb3a8dc17d514cf5bb7a356225df93a14dc9'),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def index(records):
    result = {r['run_key']: r for r in records}
    if len(result) != len(records):
        raise ValueError('Duplicate run identities')
    return result


def manifest(files):
    return {'files': len(files), 'path_sha256_manifest_sha256': digest(sorted(files))}


def load_results(root):
    result = {}
    for path in sorted(root.glob('*.json')):
        data = path.read_bytes()
        record = json.loads(data)
        # HackBench also saves an unkeyed quota-stop stub, not a completed run.
        if 'run_key' not in record and set(record) == {'score', 'abstain'}:
            continue
        key = record['run_key']
        if key in result:
            raise ValueError(f'Duplicate result in {root}')
        result[key] = (record, str(path), sha(data))
    return result


def measure_view(view):
    c = Counter(n_runs=1, n_view_steps=len(view.get('steps') or []))
    windows = render_windows(view, actions_only=True)
    c['n_windows'] = len(windows)
    c['rendered_steps'] = sum(len(w['steps']) for w in windows)
    c['rendered_chars'] = sum(len(w['text']) for w in windows)
    c['empty_actions_runs'] = int(not any(w['steps'] for w in windows))
    for step in view.get('steps') or []:
        if step.get('role') in ('user', 'system'):
            continue
        c['agent_prose_chars_excluded'] += len(step.get('text') or '')
        fields = [('args', call.get('args') or '', STEP_ARGS_CHARS)
                  for call in step.get('tool_calls') or []]
        if step.get('observation'):
            fields.append(('obs', step['observation'], STEP_OBS_CHARS))
        for kind, value, limit in fields:
            c[f'{kind}_fields'] += 1
            c[f'{kind}_chars'] += len(value)
            c[f'{kind}_kept_source_chars'] += min(len(value), limit)
            c[f'{kind}_elided_fields'] += int(len(value) > limit)
            c[f'{kind}_chars_lost'] += max(0, len(value) - limit)
    c['runs_with_elision'] = int(c['args_elided_fields'] + c['obs_elided_fields'] > 0)
    for kind, value, limit in [('task', view.get('task_text') or '', TASK_CHARS),
                               ('diff', (view.get('final') or {}).get('diff') or '', DIFF_CHARS)]:
        c[f'{kind}_chars'] = len(value)
        c[f'{kind}_chars_lost'] = max(0, len(value) - limit)
        c[f'{kind}_elided_runs'] = int(len(value) > limit)
        c[f'{kind}_missing_runs'] = int(not value)
    return c, windows


def check_judge(record, windows, hashes, c, usage, models):
    c['result_files'] += 1
    c['expected_windows'] += len(windows)
    saved = record.get('windows') or []
    c['saved_windows'] += len(saved)
    scored = record.get('score') is not None
    c['scored'] += scored
    c['abstained'] += not scored
    indices = [w.get('window', i) for i, w in enumerate(saved)]
    if len(indices) != len(set(indices)):
        raise ValueError('Duplicate saved window index')
    failures = 0
    matches = 0
    valid_scores = []
    for i, window in zip(indices, saved, strict=True):
        if not isinstance(i, int) or not 0 <= i < len(windows):
            raise ValueError('Saved window index is outside reconstruction')
        matches += window.get('prompt_sha256') == hashes[i]
        c['step_range_matches'] += window.get('steps') == ([windows[i]['steps'][0], windows[i]['steps'][-1]] if windows[i]['steps'] else [])
        failed = bool(window.get('error')) or window.get('parsed') is None
        failures += failed
        models[str(window.get('model_served'))] += 1
        if not failed:
            valid_scores.append(window['parsed']['suspicion'])
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            usage[name] += (window.get('usage') or {}).get(name, 0) or 0
    c['failed_windows'] += failures
    c['prompt_hash_matches'] += matches
    all_windows = len(saved) == len(windows) and set(indices) == set(range(len(windows)))
    c['complete_prompt_checks_runs'] += all_windows and matches == len(windows)
    c['complete_scored_runs'] += scored and all_windows and not failures
    c['scored_with_failed_windows_runs'] += scored and failures > 0
    c['scored_with_missing_windows_runs'] += scored and not all_windows
    c['no_saved_windows_runs'] += not saved
    if scored:
        c['max_valid_window_score_matches'] += bool(valid_scores) and record['score'] == max(valid_scores)


def passage_visibility(row, view, windows, hashes, results):
    output = []
    for run_id, step_id, field, offset, length, expected_hash in PASSAGES:
        if str(row.get('run_id')) != run_id:
            continue
        step = next(s for s in view['steps'] if s['i'] == step_id)
        fields = [c.get('args') or '' for c in step.get('tool_calls') or []] if field == 'args' else [step.get('observation') or '']
        candidates = [s for s in fields if sha(s[offset:offset + length].encode()) == expected_hash]
        if len(candidates) != 1:
            raise ValueError('Worked passage fingerprint is missing or ambiguous')
        needle = candidates[0][offset:offset + length]
        visible = [i for i, w in enumerate(windows) if step_id in w['steps'] and needle in w['text']]
        prompt_checks = {}
        for judge, records in results.items():
            saved = records[row['run_key']][0]['windows']
            by_index = {w.get('window', i): w for i, w in enumerate(saved)}
            prompt_checks[judge] = bool(visible) and all(i in by_index and by_index[i]['prompt_sha256'] == hashes[i] for i in visible)
        output.append({'run_id': run_id, 'step': step_id, 'field': field,
                       'source_offset': offset, 'source_field_chars': len(candidates[0]),
                       'needle_sha256': expected_hash, 'needle_chars': length,
                       'source_present': True, 'visible_window_indices': visible,
                       'saved_prompt_matches': prompt_checks})
    return output


def audit_cohort(selected, result_roots, signal_paths):
    results = {j: load_results(root) for j, root in result_roots.items()}
    signals = {j: index(rows(path)) for j, path in signal_paths.items()}
    counts, distribution = Counter(), Counter()
    jc = {j: Counter() for j in results}
    usage = {j: Counter() for j in results}
    models = {j: Counter() for j in results}
    result_files = {j: [] for j in results}
    view_files, passages = [], []
    max_window = 0
    for row in selected:
        path = Path(row['view'])
        data = path.read_bytes()
        view_files.append((str(path), sha(data)))
        view = json.loads(data)
        c, windows = measure_view(view)
        counts.update(c)
        distribution[len(windows)] += 1
        max_window = max(max_window, max(len(w['text']) for w in windows))
        hashes = [sha(j2_prompt(view, w['text'], v2=True).encode()) for w in windows]
        for judge, records in results.items():
            out = jc[judge]
            out['selected_runs'] += 1
            signal = signals[judge].get(row['run_key'])
            out['missing_signal_runs'] += signal is None
            out['signal_scored_runs'] += signal is not None and signal.get('score') is not None
            if row['run_key'] not in records:
                out['missing_result_runs'] += 1
                continue
            record, filename, filehash = records[row['run_key']]
            result_files[judge].append((filename, filehash))
            check_judge(record, windows, hashes, out, usage[judge], models[judge])
            out['signal_raw_score_matches'] += signal is not None and signal.get('score') == record.get('score')
        if any(str(row.get('run_id')) == p[0] for p in PASSAGES):
            passages.extend(passage_visibility(row, view, windows, hashes, results))
    payload = counts['args_chars'] + counts['obs_chars']
    kept = counts['args_kept_source_chars'] + counts['obs_kept_source_chars']
    return {'counts': dict(counts), 'windows_distribution': dict(sorted(distribution.items())),
            'max_window_chars': max_window, 'payload_kept_fraction': kept / payload if payload else None,
            'selected_run_keys_sha256': digest(sorted(r['run_key'] for r in selected)),
            'view_manifest': manifest(view_files),
            'actual_judges': {j: {'counts': dict(jc[j]), 'usage': dict(usage[j]),
                                  'saved_window_model_served_counts': dict(models[j]),
                                  'raw_root': str(result_roots[j]), 'result_manifest': manifest(result_files[j])}
                              for j in results}}, passages


def audit_lineage(full):
    """Distinguish failed fallback windows from score-producing Terra windows."""
    mixed = [r for r in full if r.get('score') is not None and 'muse' in str(r.get('model', '')).lower()]
    output = {}
    for cohort, root in [('DEV690', ORIGINAL / 'j2b__openai-codex__gpt-6.1-terra'),
                         ('heldout1450', ORIGINAL / 'j2b__openai-codex__gpt-6.1-terra__heldout')]:
        results = load_results(root / 'results')
        calls = rows(root / 'calls.jsonl')
        c, served, errors = Counter(), Counter(), Counter()
        for signal in mixed:
            key = signal['run_key']
            if key not in results:
                continue
            record = results[key][0]
            windows = record['windows']
            served_field = ','.join(sorted({w['model_served'] for w in windows if w.get('model_served')}))
            c['signal_model_field_reconstruction_matches'] += signal.get('model') == served_field
            c['mixed_signal_scored_runs'] += 1
            c['signal_raw_score_matches'] += signal['score'] == record['score']
            valid = [w for w in windows if not w.get('error') and w.get('parsed') is not None]
            c['all_score_producing_windows_attest_terra_runs'] += bool(valid) and all(w.get('model_served') == TERRA for w in valid)
            c['max_valid_terra_score_matches_runs'] += bool(valid) and record['score'] == max(w['parsed']['suspicion'] for w in valid)
            for w in windows:
                served[str(w.get('model_served'))] += 1
                if w.get('model_served') != TERRA:
                    c['nonterra_windows'] += 1
                    c['nonterra_windows_failed_unparsed'] += bool(w.get('error')) and w.get('parsed') is None
                matching = [call for call in calls if call.get('run_key') == key and call.get('window') == w.get('window') and call.get('prompt_sha256') == w.get('prompt_sha256') and call.get('model_served') == w.get('model_served') and call.get('error') == w.get('error')]
                c['windows_with_matching_call_receipt'] += bool(matching)
                c['saved_windows'] += 1
                if w.get('error'):
                    errors[w['error']] += 1
        output[cohort] = {'counts': dict(c), 'saved_window_model_served_counts': dict(served),
                          'window_errors': dict(errors), 'calls_path': str(root / 'calls.jsonl'),
                          'calls_sha256': sha((root / 'calls.jsonl').read_bytes())}
    output['interpretation'] = ('signals.py:66-72 joins model_served from all saved windows, including failures. '
        'Raw windows and call receipts attest Terra on score-producing windows, with rejected Muse fallback windows. '
        'A partial max score is not a complete Terra evaluation. '
        'Reconstructed scores and model fields match the combined signal; no different signal is needed to explain the mixed field. '
        'These receipts do not establish historical file-copy operations.')
    return output


def compare_reference(output, path):
    reference = json.loads(path.read_text())
    checked = 0
    mismatches = []
    for name, previous in reference['coverage'].items():
        current = output['coverage'][name]
        groups = [('counts', previous['counts'], current['counts'])]
        for judge, old in previous['actual_judges'].items():
            groups.append((judge, old['counts'], current['actual_judges'][judge]['counts']))
            groups.append((judge + '_usage', old['usage'], current['actual_judges'][judge]['usage']))
        for group, before, after in groups:
            for key, value in before.items():
                checked += 1
                lookup = 'missing_result_runs' if key == 'missing_result' else key
                if after.get(lookup, 0) != value:
                    mismatches.append({'cohort': name, 'group': group, 'metric': key,
                                       'reference': value, 'recomputed': after.get(lookup, 0)})
    return {'reference_sha256': sha(path.read_bytes()), 'metrics_checked': checked, 'mismatches': mismatches}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE / 'coverage.json')
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    full_path = SHARED / 'signals/j3_actions_task_diff_terra_full.jsonl'
    full = rows(full_path)
    membership = {r['run_key'] for r in full}
    sample_path = HERE.parent / 'ov1105-malt/sample.json'
    sample = set(json.loads(sample_path.read_text()))
    inputs = {'DEV690': Path('/private/tmp/ov1104/judges/dev/runs.jsonl'),
              'heldout1450': SHARED / 'runs.jsonl', 'MALT400': MALT / 'detbench-frozen/runs.jsonl',
              'HackBench169': HACK / 'sample-runs.jsonl'}
    selected = {name: rows(path) for name, path in inputs.items()}
    selected['heldout1450'] = [r for r in selected['heldout1450'] if r['run_key'] in membership and r['split'] == 'heldout' and not r.get('decisive_rule') and r.get('pool', True) is not False]
    selected['MALT400'] = [r for r in selected['MALT400'] if r['run_key'] in sample]
    for name, count in [('DEV690', 690), ('heldout1450', 1450), ('MALT400', 400), ('HackBench169', 169)]:
        if len(index(selected[name])) != count:
            raise ValueError(f'Unexpected cohort size: {name}')
    roots = {
        'DEV690': {'j2b': ORIGINAL / 'j2b__google-antigravity__gemini-3.8-flash/results', 'j3': ORIGINAL / 'j2b__openai-codex__gpt-6.1-terra/results'},
        'heldout1450': {'j2b': ORIGINAL / 'j2b__google-antigravity__gemini-3.8-flash/results', 'j3': ORIGINAL / 'j2b__openai-codex__gpt-6.1-terra__heldout/results'},
        'MALT400': {'j2b': MALT / 'scores/raw' / J2 / 'results', 'j3': MALT / 'scores/raw' / J3 / 'results'},
        'HackBench169': {'j2b': HACK / 'judges' / J2, 'j3': HACK / 'judges' / J3},
    }
    signals = {name: {'j2b': SHARED / 'signals' / (J2 + '.jsonl'), 'j3': full_path} for name in ('DEV690', 'heldout1450')}
    signals.update({name: {'j2b': base / (J2 + '.jsonl'), 'j3': base / (J3 + '.jsonl')} for name, base in [('MALT400', MALT / 'scores/signals'), ('HackBench169', HACK / 'signals')]})
    output = {'schema_version': 1, 'scope': 'Existing-judge coverage only; no calls or tuning; no source labels exported.',
              'source_sha256': sha(Path(__file__).read_bytes()),
              'definitions': {'payload': 'Normalized non-user/system argument and observation characters; excludes prose, reasoning, task, diff, markers and headers.',
                              'retained': 'Sum of min(field length, field budget), not native transcript coverage.',
                              'budgets': {'args': STEP_ARGS_CHARS, 'observation': STEP_OBS_CHARS, 'task': TASK_CHARS, 'diff': DIFF_CHARS, 'window': WINDOW_CHARS},
                              'complete_scored_runs': 'Numeric score, every expected window saved, no failed/unparsed windows.',
                              'complete_prompt_checks_runs': 'Every expected window saved with a matching reconstructed prompt hash; independent of score success.',
                              'missing_result_runs': 'No completed result file; signal abstentions are not zero scores.',
                              'expected_windows': 'Expected windows for rows with a result file; n_windows includes all selected rows, including no-trajectory placeholders.',
                              'heldout_selection': 'J3-full membership AND heldout AND not decisive_rule AND pool is not False'},
              'coverage': {}, 'malt_worked_passage_visibility': []}
    for name in selected:
        aggregate, passages = audit_cohort(selected[name], roots[name], signals[name])
        output['coverage'][name] = aggregate
        output['malt_worked_passage_visibility'].extend(passages)
    output['j3_mixed_model_lineage'] = audit_lineage(full)
    files = set(inputs.values()) | {sample_path, full_path, JUDGES / 'views.py', JUDGES / 'prompts.py', JUDGES / 'judge.py', JUDGES / 'signals.py'}
    files.update(path for group in signals.values() for path in group.values())
    output['input_file_sha256'] = {str(path): sha(path.read_bytes()) for path in sorted(files)}
    if args.reference:
        output['reference_comparison'] = compare_reference(output, args.reference)
        if output['reference_comparison']['mismatches']:
            raise ValueError(json.dumps(output['reference_comparison']))
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'output': str(args.output), 'source_sha256': output['source_sha256'],
                      'cohort_runs': {n: c['counts']['n_runs'] for n, c in output['coverage'].items()},
                      'reference_comparison': output.get('reference_comparison')}, indent=2))


if __name__ == '__main__':
    main()
