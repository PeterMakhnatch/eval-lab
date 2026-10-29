import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('oq8_metrics', Path(__file__).resolve().parents[1] / 'research/experiments/oq8-data-access-ab/metrics.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def cell(case, arm, keys, matched=(), eligible=1):
    role = {'status': 'answered', 'fallback': False, 'tokens_in': 100, 'tokens_out': 50, 'wall_seconds': 30}
    return {'case_id': case, 'stage': 'dev', 'arm': arm, 'keyed': bool(keys), 'key_count': keys,
            'auditor': {**role, 'protocol_violations': 0}, 'judge': role.copy(),
            'paired': {'complete': True, 'gates': {g: {'eligible_findings': eligible, 'matched_key_ids': list(matched)} for g in m.GATES}}}


def test_costs_invalid_and_unkeyed_are_not_negatives():
    a = cell('a', 'A', 1, ['k'])
    u = cell('u', 'A', 0, [], 2)
    invalid = cell('bad', 'A', 1, ['k'])
    invalid['auditor']['fallback'] = True
    s = m.summarize([a, u, invalid])
    g = s['gates']['old_0_8']
    assert (s['complete_cells'], s['invalid_cells'], s['total_tokens']) == (2, 1, 900)
    assert (g['matched_keys'], g['eligible_findings'], g['keyed_tasks_hit']) == (1, 3, 1)
    assert g['key_backed_precision'] == 1/3
    assert g['confirmed_per_wall_hour'] == 20
    invalid['auditor']['tokens_in'] = None
    s = m.summarize([a, invalid])
    assert s['total_tokens'] is None
    assert s['gates']['old_0_8']['confirmed_per_1m_tokens_ci95'] is None


def test_paired_only_complete_keyed_tasks_and_sign_test():
    a = [cell(str(i), 'A', 1) for i in range(3)] + [cell('u', 'A', 0)]
    b = [cell(str(i), 'B', 1, ['k']) for i in range(3)] + [cell('u', 'B', 0)]
    p = m.paired(a, b)['old_0_8']
    assert p['shared_complete_keyed_tasks'] == 3
    assert p['mean_matched_keys_difference_B_minus_A'] == 1
    assert p['difference_ci95'] == [1, 1]
    assert p['sign_test_two_sided_p'] == .25
    b[0]['auditor']['protocol_violations'] = 1
    assert m.paired(a, b)['old_0_8']['shared_complete_keyed_tasks'] == 2
