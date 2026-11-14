"""Synthetic known-answer checks; no corpus or detector calls."""
from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import length_metrics as lm


def row(key, task, y, score, chars=10, steps=1):
    return lm.MetricRow(key, task, y, score, chars, steps)


def test_auc_ties_and_one_class():
    assert lm.auroc([0, 1, 0, 1], [0, 1, 1, 1]) == 0.75
    assert lm.auroc([1, 1], [0, 1]) is None
    assert lm.auroc([], []) is None
    with pytest.raises(ValueError):
        lm.auroc([0, 1], [1])
    with pytest.raises(ValueError):
        lm.auroc([2, 1], [1, 2])


def test_deciles_do_not_split_ties_and_coverage_is_explicit():
    rows = [row(str(i), str(i), i % 2, i % 2, chars=10 if i < 18 else 20)
            for i in range(20)]
    assert lm.fit_length_strata(rows) == (10,)
    rows[-1] = row('19', '19', 0, 1, chars=20)
    result = lm.evaluate(rows, bootstrap=0)
    stratified = result['scores']['signal']['length_stratified']
    assert stratified['auroc'] == 1
    assert stratified['eligible_rows'] == 18
    assert stratified['coverage'] == 0.9
    assert stratified['eligible_groups'] == 1
    assert stratified['total_groups'] == 2
    assert result['scores']['signal']['within_task']['auroc'] is None


def test_group_auc_is_row_weighted_not_pair_or_task_weighted():
    rows = [row('a0', 'a', 0, 0), row('a1', 'a', 1, 1)]
    rows += [row(f'b{i}', 'b', i % 2, 1 - i % 2) for i in range(4)]
    rows += [row('c', 'c', 1, 100)]
    result = lm.evaluate(rows, strata_edges=(), bootstrap=0)
    within = result['scores']['signal']['within_task']
    assert within['auroc'] == pytest.approx(1 / 3)
    assert within['eligible_rows'] == 6
    assert within['eligible_groups'] == 2
    assert within['total_groups'] == 3


def test_threshold_respects_whole_ties_at_five_percent():
    labels = [0] * 20 + [1, 1, 1]
    scores = [5, 5] + [0] * 18 + [6, 5, 4]
    threshold = lm.choose_threshold(labels, scores)
    assert threshold == 6
    assert lm.choose_threshold([0, 0, 1], [5, 5, 5]) == math.inf
    assert lm.choose_threshold([1, 1], [0, 1]) is None
    assert lm.choose_threshold([0] * 20 + [1], [5] + [0] * 19 + [4]) == 4


def test_positive_only_uses_dev_fixed_threshold_not_empirical_fpr():
    result = lm.evaluate([row('a', 'a', 1, 2), row('b', 'b', 1, 0)],
                         dev_thresholds={'signal': 1}, bootstrap=20)
    signal = result['scores']['signal']
    assert signal['raw_auroc'] is None
    assert signal['empirical_operating_point']['recall'] is None
    assert signal['dev_fixed_operating_point']['recall'] == 0.5
    assert signal['dev_fixed_operating_point']['fpr'] is None
    assert result['intervals']['signal.raw_auroc']['low'] is None


def test_dev_only_logistic_fit_constant_length_and_direction():
    dev = [{'run_key': str(i), 'split': 'dev', 'label': i % 2, 'n_chars': 10}
           for i in range(20)]
    constant = lm.fit_length_model(dev)
    assert constant.predict(10) == pytest.approx(0.5)
    assert constant.slope == pytest.approx(0)
    for r in dev:
        r['n_chars'] = 100 if r['label'] else 1
    model = lm.fit_length_model(dev)
    assert model.predict(100) > 0.99
    assert model.predict(1) < 0.01
    assert lm.fit_length_model(dev[:1]).predict(10) is None
    with pytest.raises(ValueError, match='only split'):
        lm.fit_length_model([{**dev[0], 'split': 'heldout'}])
    with pytest.raises(ValueError):
        lm.fit_length_model([{**dev[0], 'n_chars': None}])


def test_saturated_probabilities_preserve_logit_ranks_and_fixed_recall():
    dev = [{'run_key': str(i), 'split': 'dev', 'label': i % 2,
            'n_chars': 100 if i % 2 else 1} for i in range(20)]
    model = lm.fit_length_model(dev)
    rows = [row('negative', 'task', 0, 0, chars=10_000_000),
            row('positive', 'task', 1, 1, chars=100_000_000)]
    assert [model.predict(r.n_chars) for r in rows] == [1.0, 1.0]
    logits = [model.predict_logit(r.n_chars) for r in rows]
    assert logits[0] < logits[1]
    threshold = lm.choose_threshold(
        [r['label'] for r in dev], [model.predict_logit(r['n_chars']) for r in dev])
    result = lm.evaluate(rows, length_model=model, strata_edges=(), bootstrap=0,
                         dev_thresholds={'length_model': threshold})
    control = result['scores']['length_model']
    assert control['raw_auroc'] == 1
    assert control['length_stratified']['auroc'] == 1
    assert control['within_task']['auroc'] == 1
    assert control['empirical_operating_point']['recall'] == 1
    assert control['empirical_operating_point']['fpr'] == 0
    assert result['paired_gain'] == 0
    assert control['dev_fixed_operating_point']['recall'] == 1
    assert control['dev_fixed_operating_point']['fpr'] == 1
    # A supplied logit cut between the lengths must retain only the positive.
    separated = lm.evaluate(rows, length_model=model, bootstrap=0,
                            dev_thresholds={'length_model': sum(logits) / 2})
    operating = separated['scores']['length_model']['dev_fixed_operating_point']
    assert operating['recall'] == 1
    assert operating['fpr'] == 0


def test_bootstrap_paired_gain_freezes_model_and_edges():
    # Signal is perfect, model and character length are reverse-perfect.
    rows = [row(f'{t}:{y}', str(t), y, y, chars=10 if y == 0 else 1, steps=y)
            for t in range(4) for y in (0, 1)]
    model = lm.LengthModel(0, 1, 0, 1, 20)
    a = lm.evaluate(rows, length_model=model, strata_edges=(), bootstrap=30, seed=42)
    b = lm.evaluate(rows, length_model=model, strata_edges=(), bootstrap=30, seed=42)
    assert a == b
    assert a['paired_gain'] == 1
    assert a['intervals']['paired_gain'] == {
        'low': 1, 'high': 1, 'valid_draws': 30, 'requested_draws': 30}
    assert a['scores']['characters']['raw_auroc'] == 0
    assert a['scores']['steps']['raw_auroc'] == 1
    assert a['strata_edges'] == []
    assert a['intervals']['signal.within_task.auroc']['low'] == 1


def test_bootstrap_task_copies_are_distinct_with_correct_row_weight():
    rows = [row('a0', 'a', 0, 0), row('a1', 'a', 1, 1)]
    rows += [row(f'b{i}', 'b', i % 2, 1 - i % 2) for i in range(4)]
    rows += [row('c0', 'c', 0, 0), row('c1', 'c', 1, 1)]
    clusters = [[0, 1], [2, 3, 4, 5], [6, 7]]
    # Find a deterministic draw with a duplicated cluster, without relying on
    # Python's exact Random.choice sequence as a public data contract.
    for seed in range(100):
        sampled, indices = lm._resample(rows, clusters, random.Random(seed))
        if indices.count(0) == 2 and indices.count(2) == 1:
            break
    else:
        raise AssertionError('no expected bootstrap draw')
    result = lm._group_auc(sampled, [r.score for r in sampled], [r.task for r in sampled])
    assert result['total_groups'] == 3
    assert result['eligible_groups'] == 3
    assert result['auroc'] == 0.5  # 2 perfect 2-row copies, 1 reversed 4-row copy.


def test_join_matches_all_baselines_and_reports_missing_values():
    runs = {str(i): {'run_key': str(i), 'set': 's', 'cluster': 't',
                     'label': i % 2, 'n_chars': i + 1, 'n_steps': i}
            for i in range(6)}
    runs['2']['label'] = None
    runs['3']['n_chars'] = None
    runs['4']['n_steps'] = None
    scores = {'0': 0, '1': {'score': 1}, '2': 2, '3': 3, '4': 4, 'orphan': 1}
    rows, coverage = lm.prepare_rows(runs, scores)
    assert [r.run_key for r in rows] == ['0', '1']
    assert coverage == {'input_rows': 6, 'matched_rows': 2, 'excluded': {
        'missing_label': 1, 'missing_n_chars': 1, 'missing_n_steps': 1, 'missing_score': 1},
        'scores_without_runs': 1}
    result = lm.evaluate(rows, bootstrap=0)
    for name in ('signal', 'characters', 'steps'):
        assert result['scores'][name]['raw_auroc'] == 1
    runs['0']['n_chars'] = -1
    with pytest.raises(ValueError):
        lm.prepare_rows(runs, scores)


@pytest.mark.parametrize('field,value', [('label', 3), ('score', math.nan), ('n_chars', -1),
                                        ('n_steps', math.inf), ('task', '')])
def test_invalid_row_fields_rejected(field, value):
    values = dict(run_key='r', task='t', label=1, score=1, n_chars=10, n_steps=1)
    values[field] = value
    with pytest.raises(ValueError):
        lm.MetricRow(**values)


def test_invalid_evaluation_controls_rejected():
    rows = [row('a', 'a', 0, 0)]
    for kwargs in ({'strata_edges': [2, 1]}, {'target_fpr': 1.1}, {'bootstrap': -1},
                   {'dev_thresholds': {'signal': math.nan}}):
        with pytest.raises(ValueError):
            lm.evaluate(rows, **kwargs)
    with pytest.raises(ValueError, match='unique'):
        lm.evaluate(rows * 2)


def test_prepare_rows_prefers_actual_task_over_run_specific_cluster():
    runs = {
        'a': dict(set='s', task='actual', task_id='other', cluster='run-a',
                  label=0, n_chars=10, n_steps=1),
        'b': dict(set='s', task_id='actual', cluster='run-b',
                  label=1, n_chars=10, n_steps=1),
        'c': dict(set='s', task=None, task_id=None, cluster='fallback',
                  label=1, n_chars=10, n_steps=1),
        'd': dict(set='different', task='actual', label=1, n_chars=10, n_steps=1),
    }
    rows, _ = lm.prepare_rows(runs, dict(a=0, b=1, c=0, d=1))
    assert rows[0].task == rows[1].task == '1:s:actual'
    assert rows[2].task == '1:s:fallback'
    assert rows[3].task != rows[0].task
    result = lm.evaluate(rows, bootstrap=0)
    assert result['n_tasks'] == 3
    assert result['scores']['signal']['within_task']['auroc'] == 1
    runs['a']['task'] = ''
    with pytest.raises(ValueError, match='task identity'):
        lm.prepare_rows(runs, dict(a=0))


@pytest.mark.parametrize('target_fpr', [0, 0.05, 0.5, 1])
def test_multiplicity_bootstrap_matches_slow_reference_all_scalars(target_fpr):
    rows = [
        row('a0', 'a', 0, 1, 10, 2), row('a1', 'a', 1, 1, 10, 1),
        row('b0', 'b', 0, 2, 20, 1), row('b1', 'b', 1, 0, 20, 2),
        row('b2', 'b', 1, 2, 30, 2),
        row('c0', 'c', 0, 0, 40, 0), row('c1', 'c', 0, 1, 40, 0),
        row('d0', 'd', 1, 3, 50, 3),
    ]
    model = lm.LengthModel(0, -0.7, 0, 1, 20)
    vectors = dict(signal=[r.score for r in rows], characters=[r.n_chars for r in rows],
                   steps=[r.n_steps for r in rows],
                   length_model=[model.predict_logit(r.n_chars) for r in rows])
    edges = (10, 20, 30, 40, 50, 60)  # Includes empty and single-class strata.
    thresholds = dict(signal=1, characters=math.inf, steps=None,
                      length_model=math.log(0.1 / 0.9))
    clusters = [[0, 1], [2, 3, 4], [5, 6], [7]]
    plan = lm._BootstrapPlan(rows, vectors, edges, thresholds, target_fpr)
    rng = random.Random(1106)
    originals = lm._scalars(lm._point(rows, vectors, edges, thresholds, target_fpr))
    valid = {key: [] for key in originals}

    class FixedDraw:
        def __init__(self, tasks):
            self.tasks = iter(tasks)

        def choice(self, groups):
            return groups[next(self.tasks)]

    # Explicit one-class draws, missing groups, and unequal repeated task copies.
    for draw in ([2] * 4, [3] * 4, [0, 0, 1, 3]):
        sampled, indices = lm._resample(rows, clusters, FixedDraw(draw))
        slow = lm._scalars(lm._point(
            sampled, {name: [v[i] for i in indices] for name, v in vectors.items()},
            edges, thresholds, target_fpr))
        assert plan.scalars([draw.count(t) for t in range(4)]) == pytest.approx(slow)

    fast_rng = random.Random(1106)
    for _ in range(40):
        sampled, indices = lm._resample(rows, clusters, rng)
        counts = [0] * 4
        for _ in range(4):
            counts[fast_rng.randrange(4)] += 1
        slow = lm._scalars(lm._point(
            sampled, {name: [v[i] for i in indices] for name, v in vectors.items()},
            edges, thresholds, target_fpr))
        assert plan.scalars(counts) == pytest.approx(slow)
        for key, value in slow.items():
            if value is not None:
                valid[key].append(value)

    result = lm.evaluate(rows, length_model=model, strata_edges=edges,
                         dev_thresholds=thresholds, bootstrap=40, seed=1106,
                         target_fpr=target_fpr)
    for key, values in valid.items():
        values.sort()
        enough = originals[key] is not None and len(values) >= 20
        expected = dict(low=values[int(0.025 * (len(values) - 1))] if enough else None,
                        high=values[int(0.975 * (len(values) - 1))] if enough else None,
                        valid_draws=len(values), requested_draws=40)
        assert result['intervals'][key] == pytest.approx(expected)
