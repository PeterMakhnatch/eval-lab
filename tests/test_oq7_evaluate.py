import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

PATH = Path(__file__).parents[1] / 'research/experiments/oq7-atlas-guided-audit/evaluate.py'
spec = importlib.util.spec_from_file_location('oq7_evaluate', PATH)
assert spec is not None
assert spec.loader is not None
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)


def row(i, keyed=False, coverage=1) -> dict[str, Any]:
    return {'task_id': str(i), 'dataset_id': 'd', 'keyed': keyed, 'key_count': int(keyed),
            'facet_coverage': coverage, 'pass_rate': i / 20, 'runs': i + 1,
            **{name: i for name in e.SIGNALS}}


def test_known_metrics_and_random_expectation():
    rows = [row(i, i in (1, 3)) for i in range(4)]
    m = e.metrics(np.array([1, 0, 1, 0]), np.array([2, 0, 1, 0]), [2, 4])
    assert m['at_k']['2']['yield'] == .5
    assert m['at_k']['2']['key_level_yield'] == 1
    assert m['average_precision'] == pytest.approx((1 + 2/3) / 2)
    assert m['auroc'] == .75
    g = e.evaluate_group(rows, {'test': np.arange(4)}, permutations=100, bootstraps=50)
    assert g['random_baseline']['exact_hypergeometric_expected_yield'] == .5
    assert g['schemes']['test']['bootstrap_ci_95']['yield@4'] is not None
    assert g['schemes']['test']['cost']['5'] is None


def test_ranking_label_independent_and_unknown_not_clean():
    rows = [row(i, coverage=0 if i == 2 else 1) for i in range(5)]
    first = e.ranks(rows)
    for r in rows:
        r['keyed'] = not r['keyed']
    second = e.ranks(rows)
    for name in first:
        assert first[name][0].tolist() == second[name][0].tolist()
    assert first['pass_tests_modified'][1][2] != 1.0
    assert first['pass_rate'][0].tolist() == [4, 3, 2, 1, 0]


def test_load_rejects_mismatched_populations_and_non_dev(tmp_path):
    paths = [tmp_path / str(i) for i in range(3)]
    label: dict[str, Any] = {'task_id': 'a', 'dataset_id': 'd', 'split': 'dev', 'keyed': False, 'key_count': 0, 'key_ids': [], 'key_sources': []}
    feature = {k: v for k, v in row(0).items() if k not in ('keyed', 'key_count')}
    feature['task_id'] = 'a'
    for path, data in zip(paths, [label, feature, {'task_id': 'a', 'dataset_id': 'd'}], strict=True):
        path.write_text(json.dumps(data) + '\n')
    assert len(e.load(*paths)) == 1
    paths[2].write_text('{}\n')
    with pytest.raises((ValueError, KeyError)):
        e.load(*paths)
    label['split'] = 'test'
    paths[0].write_text(json.dumps(label) + '\n')
    paths[2].write_text(json.dumps({'task_id': 'a', 'dataset_id': 'd'}) + '\n')
    with pytest.raises(ValueError):
        e.load(*paths)


def test_end_to_end_synthetic(tmp_path, monkeypatch):
    labels, features, context = [], [], []
    for i in range(12):
        r = row(i, i % 3 == 0, int(i > 1))
        ident = {'task_id': r['task_id'], 'dataset_id': r['dataset_id']}
        labels.append({**ident, 'split': 'dev', 'keyed': r['keyed'],
                       'key_count': r['key_count'], 'key_ids': [], 'key_sources': []})
        features.append({k: v for k, v in r.items() if k not in ('keyed', 'key_count')})
        context.append(ident)
    paths = [tmp_path / x for x in ('labels', 'features', 'context')]
    for path, records in zip(paths, (labels, features, context), strict=True):
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    original = e.evaluate_group
    monkeypatch.setattr(e, 'evaluate_group', lambda rows, orders: original(rows, orders, 100, 30))
    monkeypatch.setattr('sys.argv', ['evaluate', '--labels', str(paths[0]), '--features',
                                    str(paths[1]), '--context', str(paths[2]),
                                    '--output', str(tmp_path / 'out')])
    e.main()
    result = json.loads((tmp_path / 'out/results.json').read_text())
    assert result['groups']['all']['keyed_tasks'] == 4
    assert result['groups']['facets_only']['n'] == 10
    assert len((tmp_path / 'out/ranking.jsonl').read_text().splitlines()) == 12 * 13
    assert 'not certified negatives' in (tmp_path / 'out/report.md').read_text()
