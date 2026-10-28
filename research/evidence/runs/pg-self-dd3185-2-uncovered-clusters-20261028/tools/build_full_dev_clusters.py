#!/usr/bin/env python3
"""Build a private label-free DEV-only cluster census over the current facet store."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/Users/petermakhnatch/Developer/eval-lab/.worktrees/ta-pg-self-dd3185-2-20261028')
STATE = Path('/Users/petermakhnatch/.local/state/evallab-worker-evidence/pg-self-dd3185-2-20261028')
LIVE = Path('/Users/petermakhnatch/.local/state/evallab-prod-live')
FACETS = LIVE / 'run-atlas/facets-v1.jsonl'
DATABASE = LIVE / 'cloud-trace-db.sqlite'
PREVIOUS = Path('/Users/petermakhnatch/.local/state/evallab-worker-evidence/tw11-20261028/artifacts')
BASE = '456b6eacc3f9ee1cecb02727b6b679fcbe789f62'
ASSIGNMENT = 'sha256:7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f'
FROZEN_FACETS = STATE / 'facets-v1-frozen.private.jsonl'
PRIVATE = STATE / 'full-dev-clusters.private.json'
SUMMARY = STATE / 'full-dev-clusters-summary.json'


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
        return {'path': str(path), 'bytes': path.stat().st_size,
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def write_exclusive(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), 'wb') as stream:
        stream.write(encoded(value) + b'\n')
    return pin(path)


def nearest_rank(values, rank):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(rank * len(ordered)) - 1)]


def load_helper():
    import importlib.util
    spec = importlib.util.spec_from_file_location('_pg2_cross_guard', PREVIOUS / 'export_cases.py')
    require(spec is not None and spec.loader is not None, 'Cannot load cross-test guard')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    require(os.environ.get('EVALLAB_DISABLE_LOCAL_DOCKER') == '1', 'Docker must be disabled')
    for path in (FROZEN_FACETS, PRIVATE, SUMMARY):
        require(not path.exists(), f'Refusing to overwrite {path.name}')
    baseline_path = STATE / 'baseline-freeze.json'
    baseline = json.loads(baseline_path.read_bytes())
    require(baseline['base_sha'] == BASE and baseline['assignments_digest'] == ASSIGNMENT,
            'Baseline freeze differs')
    source_pins = [pin(Path(row['path'])) for row in baseline['source_pins']]
    require(source_pins == baseline['source_pins'], 'Frozen source/helper hashes changed')
    registry_pins = [pin(Path(row['path'])) for row in baseline['registry_input_pins']]
    require(registry_pins == baseline['registry_input_pins'], 'Registry input hashes changed')
    sys.path.insert(0, str(ROOT / 'src'))
    from evallab.production import run_atlas as atlas
    from evallab.production import run_atlas_clusters as clusters
    from evallab.production import run_atlas_score as score
    from evallab.production.corpus_queue import CorpusRegistry

    helper = load_helper()
    registry = CorpusRegistry(ROOT)
    require(registry.has_assignment_authority() and registry.split_assignments_digest == ASSIGNMENT,
            'Native assignment authority differs')
    started = datetime.now(UTC).isoformat()
    with FACETS.open('rb') as source:
        fcntl.flock(source, fcntl.LOCK_SH)
        data = source.read()
        fcntl.flock(source, fcntl.LOCK_UN)
    require(data.endswith(b'\n') or not data, 'Live facet store ended mid-line')
    with os.fdopen(os.open(FROZEN_FACETS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), 'wb') as stream:
        stream.write(data)
    frozen_facets_pin = pin(FROZEN_FACETS)
    facets_by_id = atlas.load_facets(FROZEN_FACETS)
    facets = list(facets_by_id.values())
    require(facets and all(str(row.get('split')).upper() == 'DEV' for row in facets),
            'Frozen facet store is empty or contains non-DEV records')
    cross = sorted(row['run_id'] for row in facets if helper.cross_benchmark_source(str(row['source'])))
    tune_facets = [row for row in facets if not helper.cross_benchmark_source(str(row['source']))]
    require(tune_facets, 'No tune facets remain after cross-test exclusion')
    ids = sorted(row['run_id'] for row in tune_facets)
    with closing(atlas.connect_readonly(DATABASE)) as connection:
        connection.execute('PRAGMA query_only = ON')
        connection.execute('BEGIN')
        admitted = atlas.admitted_task_ids(connection, registry)
        allowed = set(atlas.cloud_trace_ids(connection, admitted=admitted, trace_ids=ids))
        require(allowed == set(ids), 'Frozen tune facets contain unadmitted or unavailable identities')
        identities = {}
        for row in connection.execute(
            'SELECT a.trace_id,a.task_id,c.dataset_id,c.split FROM agent_traces a'
            ' JOIN corpus_tasks c USING(task_id) WHERE a.trace_id IN ({})'.format(','.join('?' * len(ids))),
            ids,
        ):
            identities[row['trace_id']] = dict(row)
        require(set(identities) == set(ids), 'Tune facet identity join is incomplete')
        for row in tune_facets:
            native = identities[row['run_id']]
            require(native['dataset_id'] == row['source'] and native['split'].casefold() == 'dev'
                    and not helper.cross_benchmark_source(native['dataset_id']),
                    'Facet source/split differs from native identity')
        labels, _ = score._read_labels(connection, registry, required=set(ids), wanted=set(ids))
        require(set(labels).issubset(ids), 'Native labels escaped the tune scope')
        connection.rollback()
    built = clusters.cluster_facets(tune_facets, embedder=None, seed=0, min_size=5)
    build_id = 'pg2-' + digest({'facets': frozen_facets_pin['sha256'], 'ids': ids,
                                'source': source_pins, 'seed': 0, 'min_size': 5,
                                'embedder': 'HashingEmbedder default'})[:32]
    # Native label join happens after clustering; no label enters embedding text.
    scored = []
    for cluster in built:
        members = cluster['run_ids']
        positives = [x for x in members if x in labels and labels[x].positive is True]
        negatives = [x for x in members if x in labels and labels[x].positive is False]
        considered = [x for x in members if x in labels and labels[x].positive is None]
        scored.append({'cluster_id': 'pg2:' + cluster['cluster_id'], 'native_cluster_id': cluster['cluster_id'],
                       'level': cluster['level'], 'parent': ('pg2:' + cluster['parent']) if cluster['parent'] else None,
                       'size': cluster['size'], 'members': members, 'members_sha256': digest(sorted(members)),
                       'positives': positives, 'clean': negatives, 'considered_only': considered,
                       'unjudged_n': len(members) - len(positives) - len(negatives) - len(considered),
                       'positive_n': len(positives), 'clean_n': len(negatives),
                       'considered_only_n': len(considered), 'labelled_n': len(positives) + len(negatives),
                       'positive_rate': len(positives) / (len(positives) + len(negatives))
                       if positives or negatives else None,
                       'samples_private': cluster['samples'], 'centroid_text_private': cluster['centroid_text']})
    labelled = sorted(x for x in ids if x in labels and labels[x].positive is not None)
    hits = {}
    replay_ms = {}
    registry_after_clustering = CorpusRegistry(ROOT)
    require(registry_after_clustering.split_assignments_digest == ASSIGNMENT, 'Assignments changed')
    for run in atlas.cloud_runs(DATABASE, registry=registry_after_clustering, trace_ids=labelled):
        require(run.run_id in labelled and run.split == 'DEV', 'Unfrozen replay identity')
        first = time.perf_counter_ns()
        hit = clusters.rule_hits([run])
        replay_ms[run.run_id] = (time.perf_counter_ns() - first) / 1_000_000
        hits.update(hit)
    require(sorted(hits) == labelled, 'Replay did not retain every frozen labelled member')
    for row in scored:
        positives, negatives = row['positives'], row['clean']
        row['stage1_positive_hit_n'] = sum(hits[x] for x in positives)
        row['stage1_clean_hit_n'] = sum(hits[x] for x in negatives)
        row['stage1_recall'] = row['stage1_positive_hit_n'] / len(positives) if positives else None
        row['stage1_clean_fpr'] = row['stage1_clean_hit_n'] / len(negatives) if negatives else None
        strict = row['level'] == 1
        row['minimum_positive_rate'] = 0.5 if strict else 0.4
        row['maximum_stage1_recall'] = 0.2 if strict else 0.4
        row['qualifies_predeclared_l1_l2_rule'] = bool(
            row['level'] in (1, 2) and row['size'] >= 5 and row['labelled_n'] >= 5
            and row['positive_rate'] is not None and row['positive_rate'] >= row['minimum_positive_rate']
            and row['stage1_recall'] is not None and row['stage1_recall'] <= row['maximum_stage1_recall'])
        row['uncovered_positive_ids'] = [x for x in positives if not hits[x]]
        row['covered_positive_ids'] = [x for x in positives if hits[x]]
        row['clean_hit_ids'] = [x for x in negatives if hits[x]]
    qualifying = sorted((row for row in scored if row['qualifies_predeclared_l1_l2_rule']),
                        key=lambda row: (-row['positive_n'], row['cluster_id']))
    public_fields = ('cluster_id', 'level', 'parent', 'size', 'positive_n', 'clean_n',
                     'considered_only_n', 'unjudged_n', 'labelled_n', 'positive_rate',
                     'stage1_positive_hit_n', 'stage1_clean_hit_n', 'stage1_recall',
                     'stage1_clean_fpr', 'minimum_positive_rate', 'maximum_stage1_recall',
                     'qualifies_predeclared_l1_l2_rule', 'members_sha256')
    public_rows = [{key: row[key] for key in public_fields} for row in scored]
    private = {'schema': 'pg2-full-dev-clusters-private/v1', 'started_at': started,
               'finished_at': datetime.now(UTC).isoformat(), 'base_sha': BASE, 'build_id': build_id,
               'frozen_facets': frozen_facets_pin, 'native_cluster_method': 'run_atlas_clusters.cluster_facets(embedder=None, seed=0, min_size=5)',
               'embedder': 'HashingEmbedder default (CPU hash features; no model call)',
               'source_pins': source_pins, 'registry_input_pins': registry_pins,
               'native_admitted_task_n': len(admitted), 'frozen_unique_facet_n': len(facets),
               'cross_test_facets_excluded_n': len(cross), 'tune_facet_n': len(tune_facets),
               'source_counts': dict(Counter(row['source'] for row in tune_facets)),
               'cross_test_source_counts': dict(Counter(next(row['source'] for row in facets if row['run_id'] == x) for x in cross)),
               'labelled_n': len(labelled), 'positive_n': sum(labels[x].positive is True for x in labelled),
               'clean_n': sum(labels[x].positive is False for x in labelled),
               'considered_only_n': sum(labels[x].positive is None for x in ids if x in labels),
               'stage1_positive_hit_n': sum(hits[x] for x in labelled if labels[x].positive is True),
               'stage1_clean_hit_n': sum(hits[x] for x in labelled if labels[x].positive is False),
               'hits': {x: hits[x] for x in labelled}, 'replay_ms_by_run': replay_ms,
               'clusters': scored, 'qualifying_cluster_ids': [x['cluster_id'] for x in qualifying]}
    private_pin = write_exclusive(PRIVATE, private)
    summary = {key: private[key] for key in ('schema', 'started_at', 'finished_at', 'base_sha', 'build_id',
               'native_cluster_method', 'embedder', 'native_admitted_task_n', 'frozen_unique_facet_n',
               'cross_test_facets_excluded_n', 'tune_facet_n', 'source_counts', 'cross_test_source_counts',
               'labelled_n', 'positive_n', 'clean_n', 'considered_only_n',
               'stage1_positive_hit_n', 'stage1_clean_hit_n')}
    summary.update({'frozen_facets_sha256': frozen_facets_pin['sha256'],
                    'private_census_sha256': private_pin['sha256'],
                    'cluster_count_by_level': dict(Counter(row['level'] for row in scored)),
                    'qualifying_clusters': [{key: row[key] for key in public_fields} for row in qualifying],
                    'all_l1_l2_rows': [row for row in public_rows if row['level'] in (1, 2)],
                    'all_l3_rows': [row for row in public_rows if row['level'] == 3],
                    'selection_rule': 'unchanged from TW-12: strict L1 >=5 labelled, >=50% positive, Stage-1 recall <=20%; relaxed L2 >=5, >=40%, <=40%; L3 reported only',
                    'stage1_replay_latency_ms': {'n': len(replay_ms), 'median': nearest_rank(replay_ms.values(), 0.5),
                                                 'p95': nearest_rank(replay_ms.values(), 0.95)},
                    'parent_actor_body_reads': 0, 'paid_model_calls': 0})
    summary_pin = write_exclusive(SUMMARY, summary)
    print(json.dumps({'status': 'full_dev_clusters_frozen', 'build_id': build_id,
                      'facets_unique': len(facets), 'cross_test_excluded': len(cross),
                      'tune_facets': len(tune_facets), 'labelled': len(labelled),
                      'positive': private['positive_n'], 'clean': private['clean_n'],
                      'stage1_pos_hits': private['stage1_positive_hit_n'],
                      'stage1_clean_hits': private['stage1_clean_hit_n'],
                      'cluster_counts': summary['cluster_count_by_level'],
                      'qualifying': summary['qualifying_clusters'],
                      'summary_sha256': summary_pin['sha256'], 'private_sha256': private_pin['sha256'],
                      'facets_sha256': frozen_facets_pin['sha256']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
