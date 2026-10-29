import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('features_context', Path(__file__).resolve().parents[1] / 'research/experiments/oq7-atlas-guided-audit/features_context.py')
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Registry:
    def assigned_split(self, dataset, *, item_id, task_id):
        return 'test' if task_id == 'locked' else None

    def assigned_test_anywhere(self, *, item_id, task_id):
        return task_id == 'cross-key'


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE corpus_tasks(task_id TEXT,dataset_id TEXT,split TEXT);
        CREATE TABLE agent_traces(task_id TEXT,trace_id TEXT,reward REAL);
        CREATE TABLE run_facets(run_id TEXT,task_id TEXT,outcome TEXT);
        CREATE TABLE run_clusters(name TEXT,size INTEGER,centroid_text TEXT,run_ids TEXT,level INTEGER,label_rates TEXT);
    ''')
    yield conn
    conn.close()


def test_peer_scores_unknown_rewards_and_strict_exclusion(db):
    for task, rewards in {'a': [1, 0, 0, None], 'b': [1, 1, 0], 'c': [1, 1, 1], 'few': [1, 0], 'locked': [1], 'cross-key': [1], 'held': [1], 'no-traces': []}.items():
        db.execute('INSERT INTO corpus_tasks VALUES (?,?,?)', (task, 'd', 'held' if task == 'held' else 'DEV'))
        for i, reward in enumerate(rewards):
            db.execute('INSERT INTO agent_traces VALUES (?,?,?)', (task, f'{task}{i}', reward))
    rows, meta = module.compute(db, Registry())
    assert [r['task_id'] for r in rows] == ['a', 'b', 'c', 'few']
    a, b, c, few = rows
    assert a['runs'] == 4
    assert a['pass_rate_anomaly'] == pytest.approx(-3)
    assert b['pass_rate_anomaly'] == pytest.approx(0)
    assert c['pass_rate_anomaly'] == pytest.approx(3)
    assert few['pass_rate_anomaly'] is None
    assert all(r['cluster_suspect_share'] is None for r in rows)
    assert meta['excluded'] == 2


def test_clusters_deduplicate_facets_and_ignore_labels(db):
    db.execute("INSERT INTO corpus_tasks VALUES ('a','d','DEV')")
    for i, reward in enumerate([1, 0, 0, 0]):
        db.execute('INSERT INTO agent_traces VALUES (?,?,?)', ('a', f't{i}', reward))
        db.execute('INSERT INTO run_facets VALUES (?,?,?)', (f't{i}', 'a', 'pass' if reward else 'fail'))
    for name, ids, labels, level in [('Modified tests', ['t0', 't1'], '{}', 1), ('Hardcoded answer', ['t1'], '{"exploit":0}', 2), ('Normal work', ['t2'], '{"exploit":1}', 3), ('Hardcode outside scope', ['t3'], '{}', 4)]:
        db.execute('INSERT INTO run_clusters VALUES (?,?,?,?,?,?)', (name, len(ids), '', json.dumps(ids), level, labels))
    rows, meta = module.compute(db, Registry())
    assert rows[0]['cluster_suspect_share'] == .5
    assert rows[0]['low_pass_high_variance'] is True
    assert len(meta['clusters']) == 2
    assert meta['disagreements'] == 0
