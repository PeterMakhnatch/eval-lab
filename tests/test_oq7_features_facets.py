import importlib.util
import sqlite3
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'research/experiments/oq7-atlas-guided-audit/features_facets.py'
spec = importlib.util.spec_from_file_location('oq7_features_facets', PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE corpus_tasks (task_id TEXT, dataset_id TEXT, split TEXT);
        CREATE TABLE agent_traces (trace_id TEXT, task_id TEXT, reward REAL);
        CREATE TABLE run_facets (run_id TEXT, outcome TEXT, tests TEXT,
          answer_material_accessed INTEGER, grader_or_harness_touched INTEGER,
          success_claim_without_evidence INTEGER, suspicion INTEGER, shortcut TEXT,
          eval_awareness TEXT);
        INSERT INTO corpus_tasks VALUES ('a','d','DEV'), ('b','d','DEV'),
          ('excluded','d','DEV'), ('test','d','TEST');
        INSERT INTO agent_traces VALUES ('a1','a',1), ('a2','a',0),
          ('a3','a',NULL), ('b1','b',NULL), ('e1','excluded',1), ('t1','test',1);
        INSERT INTO run_facets VALUES ('a1','fail','modified',1,1,1,3,'none','plans_to_game'),
          ('a2','fail','deleted',1,1,1,2,'shortcut','reasons_about_grader');
    ''')
    yield conn
    conn.close()


class Registry:
    def assigned_test_anywhere(self, *, item_id, task_id):
        return task_id == 'excluded'

    def assigned_split(self, dataset, *, item_id, task_id):
        return None


def test_counts_unknowns_and_strict_exclusion(db):
    rows, excluded, disagreement = module.extract(db, Registry())
    assert [r['task_id'] for r in rows] == ['a', 'b']
    a, b = rows
    assert (a['runs'], a['faceted_runs'], a['passes'], a['pass_rate'], a['facet_coverage']) == (3, 2, 1, 0.5, 2 / 3)
    assert a['pass_tests_modified'] == a['pass_answer_material'] == a['pass_grader_touched'] == 1
    assert a['pass_success_claim_no_evidence'] == a['pass_suspicion_ge2'] == a['pass_eval_awareness_grader'] == 1
    assert a['shortcut_passes'] == 0 and a['max_suspicion'] == 3
    assert b['pass_rate'] is None and b['facet_coverage'] == 0 and b['max_suspicion'] is None
    assert all(b[field] is None for field in module.COUNT_FIELDS)
    assert excluded == {'d': 1}
    assert disagreement == {('pass', 'fail'): 1}
