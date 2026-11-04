"""Collect frozen judge results and length signals after admission and freeze gates."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from evaluate import LANE, ROOT, digest, gates, keyed, load_contract, rows


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admitted', type=Path, default=LANE / 'ADMITTED.json')
    parser.add_argument('--frozen', type=Path, default=LANE / 'FROZEN.json')
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--judge-out', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    gate = gates(args.admitted, args.frozen)
    admitted = keyed(rows(Path(gate['admission']['cohort_manifest'])))
    runs = keyed(rows(args.runs))
    for path in [args.runs, *(Path(row['view']) for row in runs.values())]:
        if gate['freeze']['inputs'].get(str(path.resolve())) != digest(path):
            raise ValueError(f'unfrozen collection input: {path}')
    if set(runs) - set(admitted):
        raise ValueError('runs include nonadmitted keys')
    contract = load_contract()
    lib = load_module('frozen_detbench_lib', ROOT / 'research/experiments/ov1104-detbench/detbench_lib.py')
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ('length_steps', 'length_chars'):
        output = []
        for key, row in runs.items():
            view = json.loads(Path(row['view']).read_text())
            if view['run_key'] != key:
                raise ValueError(f'view identity mismatch: {key}')
            step_length, char_length = lib.view_size(view)
            output.append({'run_key': key, 'signal': name, 'version': contract['signals'][name]['version'],
                           'score': step_length if name == 'length_steps' else char_length,
                           'abstain': None, 'model': None})
        (args.out / f'{name}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in output))
    for name, model in (('j2b_actions_task_diff_v2_flash', 'google-antigravity/gemini-3.8-flash'),
                        ('j3_actions_task_diff_terra', 'openai-codex/gpt-6.1-terra')):
        batch = args.judge_out / ('j2b__' + model.replace('/', '__'))
        results = keyed([json.loads(path.read_text()) for path in sorted((batch / 'results').glob('*.json'))])
        output = []
        for key in runs:
            source = results.get(key, {})
            windows = source.get('windows') or []
            score = source.get('score')
            reason = source.get('abstain')
            bad_attestation = any(
                (w.get('model_served') is not None and w['model_served'] != model)
                or (not w.get('error') and w.get('model_served') != model) for w in windows)
            if source.get('model') != model or not windows or bad_attestation:
                score, reason = None, 'missing or mismatched served model attestation'
            if source.get('rubric_sha256') != '7e7cb3c4686423ad372bd6bb515fc0b3ab6a183f3d33b8ce45379e6a87f24472':
                score, reason = None, 'missing or mismatched frozen rubric'
            output.append({'run_key': key, 'signal': name, 'version': contract['signals'][name]['version'],
                           'score': score, 'abstain': reason, 'model': model if score is not None else None,
                           'partial_windows_failed': source.get('partial_windows_failed', []),
                           'window_failures': [{'window': w['window'], 'error': w['error']}
                                               for w in windows if w.get('error')]})
        (args.out / f'{name}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in output))
    print(json.dumps({'signals': str(args.out), 'runs': len(runs)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
