#!/usr/bin/env python3
"""Freeze current native DEV cluster metadata; never load trajectories or facets."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/Users/petermakhnatch/Developer/eval-lab/.worktrees/ta-pg-self-dd3185-2-20261028')
STATE = Path('/Users/petermakhnatch/.local/state/evallab-worker-evidence/pg-self-dd3185-2-20261028')
DATABASE = Path('/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite')
PREVIOUS = Path('/Users/petermakhnatch/.local/state/evallab-worker-evidence/tw11-20261028/artifacts')
BASE = '456b6eacc3f9ee1cecb02727b6b679fcbe789f62'
EXPECTED_BUILD = 'b4bb653727324adc9684a0fbdebee894'
EXPECTED_ASSIGNMENT = 'sha256:7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f'


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pin(path):
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha}


def write_exclusive(name, value):
    path = STATE / name
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), 'wb') as stream:
        stream.write(encoded(value) + b'\n')
    return pin(path)


def original_helper():
    path = PREVIOUS / 'export_cases.py'
    spec = importlib.util.spec_from_file_location('_pg2_original_intake_helpers', path)
    require(spec is not None and spec.loader is not None, 'Missing original helper loader')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def body_access_forbidden(*args, **kwargs):
    raise RuntimeError('Actor/facet/trajectory access is forbidden in metadata admission')


def main():
    require(os.environ.get('EVALLAB_DISABLE_LOCAL_DOCKER') == '1', 'Docker must be disabled')
    for name in ('metadata-freeze.private.json', 'metadata-summary.json', 'baseline-freeze.json'):
        require(not (STATE / name).exists(), 'Refusing to overwrite any metadata observation')
    started = datetime.now(UTC).isoformat()
    sys.path.insert(0, str(ROOT / 'src'))
    from evallab import tripwires
    from evallab.production import run_atlas as atlas
    from evallab.production import run_atlas_score as score
    from evallab.production.corpus_queue import CorpusRegistry

    require(Path(tripwires.__file__).resolve() == ROOT / 'src/evallab/tripwires.py',
            'Runtime imported another checkout')
    helper = original_helper()
    registry_pins = [pin(ROOT / relative) for relative in helper.REGISTRY_INPUTS]
    source_paths = [Path(__file__), PREVIOUS / 'export_cases.py',
                    ROOT / 'src/evallab/tripwires.py', ROOT / 'src/evallab/edit_signals.py',
                    ROOT / 'src/evallab/production/corpus_queue.py',
                    ROOT / 'src/evallab/production/cloud_trace_db.py',
                    ROOT / 'src/evallab/production/run_atlas.py',
                    ROOT / 'src/evallab/production/run_atlas_score.py',
                    ROOT / 'src/evallab/production/run_atlas_clusters.py']
    source_pins = [pin(path) for path in source_paths]
    registry = CorpusRegistry(ROOT)
    require(registry.has_assignment_authority(), 'Missing native assignment authority')
    require(registry.split_assignments_digest == EXPECTED_ASSIGNMENT,
            'Loaded assignment ledger differs')
    roster = []
    for rule in tripwires._HACKBOOK_RULE_TABLE:
        roster.append({'id': rule.id, 'entry_ids': list(rule.entry_ids),
                       'source_links': list(rule.source_links), 'signature': rule.signature,
                       'scope': rule.scope, 'eligibility': rule.eligibility, 'gate': rule.gate,
                       'callback': rule.run.__name__,
                       'callback_source_sha256': hashlib.sha256(
                           inspect.getsource(rule.run).encode('utf-8')).hexdigest(),
                       'stream': rule.stream.__name__ if rule.stream is not None else None,
                       'stream_source_sha256': hashlib.sha256(
                           inspect.getsource(rule.stream).encode('utf-8')).hexdigest()
                       if rule.stream is not None else None})
    require(len({row['id'] for row in roster}) == len(roster), 'Duplicate native rule IDs')
    atlas.cloud_doc = body_access_forbidden
    atlas.cloud_runs = body_access_forbidden
    selected_columns = set()
    phase = ['identity']

    def authorize(action, table, column, database_name, trigger):
        if action == sqlite3.SQLITE_READ:
            selected_columns.add((table, column))
            if table in {'trace_steps', 'run_facets', 'trace_step_layers'}:
                return sqlite3.SQLITE_DENY
            if table == 'agent_traces' and column == 'verified_receipt_json' and phase[0] != 'labels':
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(atlas.connect_readonly(DATABASE)) as connection:
        connection.execute('PRAGMA query_only = ON')
        connection.set_authorizer(authorize)
        connection.execute('BEGIN')
        snapshot_version = connection.execute('PRAGMA data_version').fetchone()[0]
        admitted = atlas.admitted_task_ids(connection, registry)
        clusters = []
        for row in connection.execute(
            "SELECT cluster_id,level,parent,size,run_ids,"
            " CASE WHEN json_valid(provenance_json)"
            " THEN json_extract(provenance_json,'$.build_id') END AS build_id"
            " FROM run_clusters ORDER BY level,cluster_id"
        ):
            members = json.loads(row['run_ids'])
            require(isinstance(members, list) and all(isinstance(x, str) and x for x in members)
                    and len(set(members)) == len(members) == row['size'],
                    'Invalid native cluster member metadata')
            require(row['build_id'] == EXPECTED_BUILD, 'Native current cluster build changed')
            require(row['level'] in (1, 2, 3), 'Unexpected cluster level')
            clusters.append({'cluster_id': row['cluster_id'], 'level': row['level'],
                             'parent': row['parent'], 'build_id': row['build_id'],
                             'stored_size': row['size'], 'stored_members': sorted(members)})
        require(clusters, 'Native database has no cluster build')
        members = sorted({member for cluster in clusters for member in cluster['stored_members']})
        identities = {}
        for row in connection.execute(
            'SELECT a.trace_id,a.task_id,c.dataset_id,c.split'
            ' FROM json_each(?) m JOIN agent_traces a ON a.trace_id=m.value'
            ' JOIN corpus_tasks c ON c.task_id=a.task_id ORDER BY a.trace_id',
            (json.dumps(members),),
        ):
            identities[row['trace_id']] = dict(row)
        admitted_members = {trace_id for trace_id, row in identities.items()
                            if row['task_id'] in admitted and row['split'].casefold() == 'dev'}
        tune = {trace_id for trace_id in admitted_members
                if not helper.cross_benchmark_source(identities[trace_id]['dataset_id'])}
        # Native scoring receipts are read only after exact task/source admission,
        # and only for the tune set. No cross-test label or body is requested.
        phase[0] = 'labels'
        labels, checked = score._read_labels(connection, registry, required=tune, wanted=tune)
        require(set(labels).issubset(tune), 'Native labels escaped the admitted tune scope')
        phase[0] = 'metadata'
        summaries = []
        private_clusters = []
        for cluster in clusters:
            stored = set(cluster['stored_members'])
            allowed = sorted(stored & tune)
            positive = sum(labels[trace_id].positive is True for trace_id in allowed if trace_id in labels)
            clean = sum(labels[trace_id].positive is False for trace_id in allowed if trace_id in labels)
            considered = sum(labels[trace_id].positive is None for trace_id in allowed if trace_id in labels)
            unknown = len(allowed) - positive - clean - considered
            minimum_rate = 0.4 if cluster['level'] == 2 else 0.5
            maximum_recall = 0.4 if cluster['level'] == 2 else 0.2
            rate = positive / (positive + clean) if positive + clean else None
            potentially_eligible = (len(allowed) >= 5 and positive + clean >= 5
                                    and rate is not None and rate >= minimum_rate)
            summary = {'cluster_id': cluster['cluster_id'], 'level': cluster['level'],
                       'parent': cluster['parent'], 'stored_size': cluster['stored_size'],
                       'native_admitted_n': len(stored & admitted_members),
                       'cross_benchmark_excluded_n': len((stored & admitted_members) - tune),
                       'unavailable_or_refused_n': len(stored - admitted_members),
                       'admitted_tune_n': len(allowed), 'positive_n': positive, 'clean_n': clean,
                       'considered_only_n': considered, 'unjudged_n': unknown,
                       'labelled_n': positive + clean, 'positive_rate': rate,
                       'minimum_labelled': 5, 'minimum_positive_rate': minimum_rate,
                       'maximum_rule_recall': maximum_recall,
                       'rate_eligible_pending_fresh_native_coverage': potentially_eligible,
                       'members_sha256': digest(allowed),
                       'stored_members_sha256': digest(cluster['stored_members'])}
            summaries.append(summary)
            private_clusters.append({**summary, 'stored_members': cluster['stored_members'],
                                     'admitted_tune_members': allowed})
        identities_private = []
        for trace_id in sorted(tune):
            label = labels.get(trace_id)
            identities_private.append({**identities[trace_id],
                                       'label_present': label is not None,
                                       'positive': label.positive if label is not None else None,
                                       'stratum': label.stratum if label is not None else 'unjudged'})
        connection.rollback()

    require([pin(ROOT / relative) for relative in helper.REGISTRY_INPUTS] == registry_pins,
            'Registry inputs changed during native metadata gate')
    require([pin(path) for path in source_paths] == source_pins, 'Source changed during native gate')
    frozen = {'schema': 'pg2-current-cluster-metadata/v1', 'started_at': started,
              'finished_at': datetime.now(UTC).isoformat(), 'base_sha': BASE,
              'build_id': EXPECTED_BUILD, 'database': str(DATABASE),
              'snapshot': 'one SQLite read-only transaction, no whole-DB file digest claimed',
              'sqlite_data_version': snapshot_version,
              'assignments_digest': registry.split_assignments_digest,
              'native_admitted_task_n': len(admitted),
              'stored_cluster_n': len(clusters), 'stored_unique_member_n': len(members),
              'admitted_member_n': len(admitted_members), 'admitted_tune_member_n': len(tune),
              'source_counts': dict(Counter(identities[x]['dataset_id'] for x in sorted(tune))),
              'clusters': private_clusters, 'identities': identities_private,
              'registry_input_pins': registry_pins, 'source_pins': source_pins,
              'sqlite_read_columns': sorted([list(item) for item in selected_columns]),
              'trajectory_or_facet_table_reads_allowed': False,
              'parent_trajectory_body_reads': 0,
              'worker_error_disclosure_ref': pin(STATE / 'pre-admission-disclosure.json')}
    private_pin = write_exclusive('metadata-freeze.private.json', frozen)
    summary = {key: frozen[key] for key in ('schema', 'started_at', 'finished_at', 'base_sha',
               'build_id', 'assignments_digest', 'native_admitted_task_n', 'stored_cluster_n',
               'stored_unique_member_n', 'admitted_member_n', 'admitted_tune_member_n', 'source_counts')}
    summary.update({'clusters': summaries, 'metadata_freeze_sha256': private_pin['sha256'],
                    'scoring_scope': 'native DEV-admitted tuning sources only; cross-test sources excluded',
                    'selection': 'Native strict 5-labelled/50%-positive/20%-recall criteria for L1/L3; previously declared L2 40%/40% fallback; coverage not yet measured',
                    'parent_actor_body_reads': 0, 'paid_model_calls': 0, 'recorded_code_executed': False})
    summary_pin = write_exclusive('metadata-summary.json', summary)
    baseline = {'schema': 'pg2-baseline-freeze/v1', 'created_at': datetime.now(UTC).isoformat(),
                'base_sha': BASE, 'worktree': str(ROOT), 'assignments_digest': EXPECTED_ASSIGNMENT,
                'source_archive': pin(STATE / 'baseline-source.tar'),
                'source_pins': source_pins, 'registry_input_pins': registry_pins,
                'roster': roster, 'roster_sha256': digest(roster),
                'rule_n': len(roster), 'verdict_rule_n': sum(x['eligibility'] == 'verdict' for x in roster),
                'metadata_freeze': private_pin, 'metadata_summary': summary_pin,
                'actor_derivation_started': False, 'actor_error_disclosure_preserved': True,
                'no_cm6_cached_coverage_reused': True}
    baseline_pin = write_exclusive('baseline-freeze.json', baseline)
    eligible = [row for row in summaries if row['rate_eligible_pending_fresh_native_coverage']]
    print(json.dumps({'status': 'metadata_admission_frozen', 'build': EXPECTED_BUILD,
                      'native_admitted_tasks': len(admitted), 'stored_clusters': len(clusters),
                      'stored_members': len(members), 'admitted_tune_members': len(tune),
                      'sources': summary['source_counts'],
                      'rate_eligible_pending_fresh_coverage': eligible,
                      'baseline_freeze_sha256': baseline_pin['sha256'],
                      'parent_actor_body_reads': 0}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
