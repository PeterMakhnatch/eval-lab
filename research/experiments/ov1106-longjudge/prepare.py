"""Freeze exact study membership and privately materialize judge input tables.

This command never calls a model. It uses existing cohort membership, not scores,
for selection. Public output contains identities and aggregate counts only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFAULT_PRIVATE = Path.home() / '.local/state/eval-lab/ov1106-longjudge'
DEFAULT_DETBENCH = Path('/private/tmp/ov1104/shared/detbench')
DEFAULT_MALT = Path.home() / '.local/state/eval-lab/ov1105-malt-060fefc3a'
DEFAULT_HACKBENCH = Path('/private/tmp/ov1105/hackbench/evaluation/sample-runs.jsonl')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    if len({r['run_key'] for r in rows}) != len(rows):
        raise ValueError(f'Duplicate identities in {path}')
    return rows


def prepare(private: Path, detbench: Path, malt: Path, hackbench: Path) -> dict[str, Any]:
    private = private.expanduser().resolve()
    if private.is_relative_to(ROOT) or any((p / '.git').exists() for p in (private, *private.parents)):
        raise ValueError('Private study inputs must remain outside Git')
    paths = {
        'detbench_runs': detbench / 'runs.jsonl',
        'detbench_j3_membership': detbench / 'signals/j3_actions_task_diff_terra_full.jsonl',
        'malt_runs': malt / 'detbench-frozen/runs.jsonl',
        'malt_sample': HERE.parent / 'ov1105-malt/sample.json',
        'hackbench_sample_runs': hackbench,
    }
    original = read_rows(paths['detbench_runs'])
    j3_keys = {r['run_key'] for r in read_rows(paths['detbench_j3_membership'])}
    malt_keys = set(json.loads(paths['malt_sample'].read_text()))
    cohorts = {
        'dev': [r for r in original if r['split'] == 'dev'],
        'heldout': [r for r in original if r['run_key'] in j3_keys and r['split'] == 'heldout'
                    and not r.get('decisive_rule') and r.get('pool') is not False],
        'malt': [r for r in read_rows(paths['malt_runs']) if r['run_key'] in malt_keys],
        'hackbench': read_rows(hackbench),
    }
    expected = {'dev': (690, 332), 'heldout': (1450, 344), 'malt': (400, 15), 'hackbench': (169, 169)}
    report: dict[str, Any] = {
        'schema': 'ov1106-longjudge-samples/v1',
        'selection': 'Original cohorts, all rows; order by SHA256("1106:" + run_key), independent of scores and labels.',
        'sources': {name: {'path': str(path), 'sha256': sha(path)} for name, path in paths.items()},
        'cohorts': {},
    }
    all_keys: set[str] = set()
    input_dir = private / 'inputs'
    input_dir.mkdir(parents=True, exist_ok=True)
    for name, source in cohorts.items():
        if (len(source), sum(r['label'] == 1 for r in source)) != expected[name]:
            raise ValueError(f'{name} cohort no longer matches the source freeze')
        source.sort(key=lambda r: hashlib.sha256(('1106:' + r['run_key']).encode()).hexdigest())
        if all_keys.intersection(r['run_key'] for r in source):
            raise ValueError('Study cohorts overlap')
        all_keys.update(r['run_key'] for r in source)
        materialized = []
        view_identities = []
        for row in source:
            view = Path(row['view'])
            identity = sha(view)
            view_identities.append([row['run_key'], identity])
            materialized.append({**row, 'view_sha256': identity})
        data = ''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in materialized)
        path = input_dir / f'{name}.jsonl'
        if path.exists() and path.read_text() != data:
            raise ValueError(f'Refusing to replace a changed prepared cohort: {path}')
        path.write_text(data)
        keys = [r['run_key'] for r in source]
        report['cohorts'][name] = {
            'n': len(source), 'positive': sum(r['label'] == 1 for r in source),
            'negative': sum(r['label'] == 0 for r in source),
            'task_clusters': len({r['cluster'] for r in source}),
            'sets': dict(sorted(Counter(r['set'] for r in source).items())),
            'run_keys': keys, 'identity_sha256': digest(sorted(keys)),
            'rows_sha256': sha(path), 'views_sha256': digest(view_identities),
            'private_input': str(path),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private', type=Path, default=DEFAULT_PRIVATE)
    parser.add_argument('--detbench', type=Path, default=DEFAULT_DETBENCH)
    parser.add_argument('--malt', type=Path, default=DEFAULT_MALT)
    parser.add_argument('--hackbench', type=Path, default=DEFAULT_HACKBENCH)
    parser.add_argument('--out', type=Path, default=HERE / 'samples.json')
    args = parser.parse_args()
    result = prepare(args.private, args.detbench, args.malt, args.hackbench)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({name: {k: v for k, v in cohort.items() if k not in ('run_keys', 'sets')}
                      for name, cohort in result['cohorts'].items()}, indent=2))


if __name__ == '__main__':
    main()
