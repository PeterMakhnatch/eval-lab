"""Stage label-free DEV inputs, then execute the native categorical line after freeze."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluate import LANE, ROOT, check_admission, digest, gates, keyed, rows

POLICY_SOURCE = Path('/Users/petermakhnatch/Developer/eval-lab-results/line/replay-v1/spec.json')
sys.path.insert(0, str(ROOT / 'research/experiments/ov1104-detbench'))
from detbench_lib import view_to_atif  # noqa: E402


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(args):
    admission = check_admission(args.admitted)
    manifest = keyed(rows(Path(admission['cohort_manifest'])))
    views = keyed(rows(args.runs))
    if set(views) != set(manifest):
        raise ValueError('native line requires exactly the admitted view sample')
    args.out.mkdir(parents=True, exist_ok=False)
    rollout_dir = args.out / 'rollouts'
    rollout_dir.mkdir()
    inputs, native_rows = {}, []
    for key, row in views.items():
        view_path = Path(row['view'])
        view = json.loads(view_path.read_text())
        source = manifest[key]
        path = rollout_dir / key
        path.mkdir()
        task_id = source['task_id'] + '/' + source['variant']
        trajectory = view_to_atif(view)
        dump(path / 'trajectory.json', trajectory)
        dump(path / 'run.json', {'task_id': task_id, 'split': 'dev'})
        (path / 'final.diff').write_text(view['final']['diff'])
        (path / 'prompt.md').write_text(view['task_text'])
        native_rows.append({'id': key, 'task_id': task_id, 'path': str(path.resolve())})
        for artifact in path.iterdir():
            inputs[str(artifact.resolve())] = digest(artifact)
    native_manifest = args.out / 'manifest.jsonl'
    native_manifest.write_text(''.join(json.dumps(row) + '\n' for row in native_rows))
    cache = args.out / 'empty-autoreview.jsonl'
    cache.write_text('')
    station = args.out / 'empty-verdicts.jsonl'
    station.write_text('')
    base = json.loads(POLICY_SOURCE.read_text())
    if base['autoreview']['mode'] != 'cached':
        raise ValueError('reference replay no longer uses cached AutoReview')
    spec = {'schema': 'evallab.line_spec/v1', 'batch_id': 'ov1106-impscale', 'split': 'dev',
            'sources': [{'corpus': 'ov1106-impscale', 'rollouts': str(native_manifest.resolve())}],
            'task_status': None, 'task_action': base['task_action'],
            'task_policy': base.get('task_policy', {}),
            'autoreview': {'mode': 'cached', 'cache': str(cache.resolve()), 'max_usd': 0},
            'models': {'allowed': []}, 'audit': base['audit'],
            'judge': {'queue': str((args.out / 'judge-queue.jsonl').resolve()),
                      'write': False, 'verdicts': str(station.resolve())},
            'labels_policy': base['labels_policy'], 'out_root': str((args.out / 'batches').resolve())}
    spec_path = args.out / 'spec.json'
    dump(spec_path, spec)
    policy_copy = args.out / 'reference-policy.json'
    policy_copy.write_bytes(POLICY_SOURCE.read_bytes())
    for path in (native_manifest, cache, station, spec_path, policy_copy):
        inputs[str(path.resolve())] = digest(path)
    dump(args.out / 'input-hashes.json', inputs)
    print(json.dumps({'staged': len(native_rows), 'spec': str(spec_path),
                      'autoreview': 'missing readings remain native CACHE_MISS; no live calls'}))


def run(args):
    gate = gates(args.admitted, args.frozen)
    index = args.out / 'input-hashes.json'
    if gate['freeze']['inputs'].get(str(index.resolve())) != digest(index):
        raise ValueError('native line input index is not frozen')
    expected = json.loads((args.out / 'input-hashes.json').read_text())
    for name, sha in expected.items():
        if gate['freeze'].get('inputs', {}).get(name) != sha or digest(Path(name)) != sha:
            raise ValueError(f'unfrozen native line input: {name}')
    from evallab.line.core import run_line
    batch = run_line(args.out / 'spec.json', ROOT)
    output = []
    contract = json.loads(Path(__file__).with_name('contract.json').read_text())
    traces = {row['run_id']: row for row in rows(batch / 'trace.jsonl')}
    for row in rows(batch / 'decisions.jsonl'):
        output.append({'run_key': row['run_id'], 'version': contract['line_version'],
                       'decision': row['decision'], 'reasons': row['reasons'],
                       'autoreview': traces[row['run_id']]['autoreview'],
                       'batch': str(batch), 'decisions_sha256': digest(batch / 'decisions.jsonl')})
    destination = args.out / 'line-decisions.jsonl'
    destination.write_text(''.join(json.dumps(row) + '\n' for row in output))
    print(json.dumps({'batch': str(batch), 'decisions': str(destination), 'rows': len(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('--admitted', type=Path, default=LANE / 'ADMITTED.json')
    parser.add_argument('--frozen', type=Path, default=LANE / 'FROZEN.json')
    parser.add_argument('--runs', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare' and args.runs is None:
        parser.error('prepare requires --runs')
    (prepare if args.mode == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
