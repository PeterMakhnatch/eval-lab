"""Deterministic checks for the p0f7 aggregate calculations; no model calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('p0f7_score', Path(__file__).with_name('score.py'))
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)


def test_summary_uses_each_label_denominator():
    rows = [{'enacted': True, 'alarm': True}, {'enacted': True, 'alarm': False},
            {'enacted': False, 'alarm': True}]
    result = score.summarize(rows, 'alarm')
    assert (result['recall']['k'], result['recall']['n']) == (1, 2)
    assert (result['far']['k'], result['far']['n']) == (1, 1)


def test_mh_difference_uses_stratum_weights_and_is_deterministic():
    rows = [
        {'tertile': 1, 'enacted': True, 'task': 'a', 'alarm': True},
        {'tertile': 1, 'enacted': False, 'task': 'a', 'alarm': False},
        {'tertile': 2, 'enacted': True, 'task': 'b', 'alarm': False},
        {'tertile': 2, 'enacted': False, 'task': 'b', 'alarm': True},
        {'tertile': 2, 'enacted': False, 'task': 'c', 'alarm': False},
    ]
    result = score.differences(rows, ('alarm',))
    # weights 1/2 and 2/3; differences +1 and -1/2.
    assert abs(result['mh_alarm']['point'] - 1 / 7) < 1e-12
    assert result['t3_alarm']['point'] is None
    assert result == score.differences(rows, ('alarm',))
