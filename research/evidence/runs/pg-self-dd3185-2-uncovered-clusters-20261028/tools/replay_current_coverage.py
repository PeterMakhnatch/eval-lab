#!/usr/bin/env python3
"""Replay current native Stage-1 on admitted DEV labelled members; no model or judge."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/Users/petermakhnatch/Developer/eval-lab/.worktrees/ta-pg-self-dd3185-2-20261028')
STATE = Path('/Users/petermakhnatch/.local/state/evallab-worker-evidence/pg-self-dd3185-2-20261028')
DATABASE = Path('/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite')
BASE = '456b6eacc3f9ee1cecb02727b6b679fcbe789f62'
ASSIGNMENT = 'sha256:7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f'
OUT_PRIVATE = STATE / 'current-stage1-coverage.private.json'
OUT_SUMMARY = STATE / 'current-stage1-coverage-summary.json'


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pin(path):
    with path.open('rb') as stream:
        return {'path': str(path), 'bytes': path.stat().st_size,
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def write_exclusive(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), 'wb') as stream:
        stream.write(encoded(value) + b'\n')
    return pin(path)


def percentile(values, rank):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(rank * len(ordered)) - 1)]


def main():
    require(os.environ.get('EVALLAB_DISABLE_LOCAL_DOCKER') == '1', 'Docker must be disabled')
    require(not OUT_PRIVATE.exists() and not OUT_SUMMARY.exists(), 'Refusing to overwrite coverage')
    metadata_path = STATE / 'metadata-freeze.private.json'
    baseline_path = STATE / 'baseline-freeze.json'
    metadata_pin = pin(metadata_path)
    baseline_pin = pin(baseline_path)
    metadata = json.loads(metadata_path.read_bytes())
    baseline = json.loads(baseline_path.read_bytes())
    require(baseline['metadata_freeze'] == metadata_pin and baseline['base_sha'] == BASE,
            'Frozen metadata or baseline pin differs')
    require(metadata['assignments_digest'] == ASSIGNMENT, 'Frozen assignment ledger differs')
    sys.path.insert(0, str(ROOT / 'src'))
    from evallab.production import run_atlas as atlas
    from evallab.production import run_atlas_clusters as clusters
    from evallab.production.corpus_queue import CorpusRegistry

    source_pins = [pin(Path(row['path'])) for row in baseline['source_pins']]
    require(source_pins == baseline['source_pins'], 'Frozen source/helper hashes differ')
    registry_pins = [pin(Path(row['path'])) for row in baseline['registry_input_pins']]
    require(registry_pins == baseline['registry_input_pins'], 'Registry input hashes differ')
    registry = CorpusRegistry(ROOT)
    require(registry.has_assignment_authority() and registry.split_assignments_digest == ASSIGNMENT,
            'Current native assignment authority differs')
    labelled = {row['trace_id']: row for row in metadata['identities'] if row['label_present']
                and row['positive'] is not None}
    ordered = sorted(labelled)
    require(ordered, 'No labelled admitted DEV member')
    started = datetime.now(UTC).isoformat()
    hits = {}
    latency_ms = {}
    loaded = []
    for run in atlas.cloud_runs(DATABASE, registry=registry, trace_ids=ordered):
        require(run.run_id in labelled and run.split == 'DEV', 'Native runtime returned unfrozen identity')
        loaded.append(run.run_id)
        first = time.perf_counter_ns()
        value = clusters.rule_hits([run])
        latency_ms[run.run_id] = (time.perf_counter_ns() - first) / 1_000_000
        hits.update(value)
    require(sorted(loaded) == ordered and sorted(hits) == ordered,
            'Native replay did not retain every frozen labelled member')
    private_clusters = []
    summaries = []
    for cluster in metadata['clusters']:
        members = [x for x in cluster['admitted_tune_members'] if x in labelled]
        positives = [x for x in members if labelled[x]['positive'] is True]
        negatives = [x for x in members if labelled[x]['positive'] is False]
        recall = sum(hits[x] for x in positives) / len(positives) if positives else None
        clean_fpr = sum(hits[x] for x in negatives) / len(negatives) if negatives else None
        labelled_n = len(positives) + len(negatives)
        rate = len(positives) / labelled_n if labelled_n else None
        strict = cluster['level'] != 2
        min_rate = 0.5 if strict else 0.4
        max_recall = 0.2 if strict else 0.4
        qualifies = (cluster['level'] in (1, 2) and len(cluster['admitted_tune_members']) >= 5
                     and labelled_n >= 5 and rate is not None and rate >= min_rate
                     and recall is not None and recall <= max_recall)
        row = {'cluster_id': cluster['cluster_id'], 'level': cluster['level'],
               'parent': cluster['parent'], 'admitted_tune_n': len(cluster['admitted_tune_members']),
               'positive_n': len(positives), 'clean_n': len(negatives), 'labelled_n': labelled_n,
               'positive_rate': rate, 'stage1_positive_hit_n': sum(hits[x] for x in positives),
               'stage1_clean_hit_n': sum(hits[x] for x in negatives), 'stage1_recall': recall,
               'stage1_clean_fpr': clean_fpr, 'minimum_positive_rate': min_rate,
               'maximum_stage1_recall': max_recall,
               'qualifies_predeclared_l1_l2_rule': qualifies,
               'members_sha256': cluster['members_sha256']}
        summaries.append(row)
        private_clusters.append({**row, 'uncovered_positive_ids': [x for x in positives if not hits[x]],
                                 'covered_positive_ids': [x for x in positives if hits[x]],
                                 'clean_ids': negatives,
                                 'clean_hit_ids': [x for x in negatives if hits[x]]})
    qualifying = sorted((row for row in summaries if row['qualifies_predeclared_l1_l2_rule']),
                        key=lambda row: (-row['positive_n'], row['cluster_id']))
    private = {'schema': 'pg2-current-stage1-coverage-private/v1', 'started_at': started,
               'finished_at': datetime.now(UTC).isoformat(), 'base_sha': BASE,
               'metadata_freeze': metadata_pin, 'baseline_freeze': baseline_pin,
               'runtime': {'method': 'run_atlas_clusters.rule_hits', 'judge': None,
                           'alert_playbook_cards': 0, 'replay': 'first suspicion stops each run',
                           'frozen_cm6_coverage_reused': False},
               'labelled_n': len(ordered), 'positive_n': sum(labelled[x]['positive'] is True for x in ordered),
               'clean_n': sum(labelled[x]['positive'] is False for x in ordered),
               'stage1_positive_hit_n': sum(hits[x] for x in ordered if labelled[x]['positive'] is True),
               'stage1_clean_hit_n': sum(hits[x] for x in ordered if labelled[x]['positive'] is False),
               'hits': {x: hits[x] for x in ordered}, 'replay_ms_by_run': latency_ms,
               'clusters': private_clusters, 'qualifying_cluster_ids': [x['cluster_id'] for x in qualifying],
               'source_pins': source_pins, 'registry_input_pins': registry_pins}
    private_pin = write_exclusive(OUT_PRIVATE, private)
    summary = {key: private[key] for key in ('schema', 'started_at', 'finished_at', 'base_sha',
               'labelled_n', 'positive_n', 'clean_n', 'stage1_positive_hit_n', 'stage1_clean_hit_n')}
    summary.update({'runtime': private['runtime'], 'qualifying_clusters': qualifying,
                    'all_l1_l2_rate_and_recall_rows': [x for x in summaries if x['level'] in (1, 2)],
                    'metadata_freeze_sha256': metadata_pin['sha256'],
                    'private_coverage_sha256': private_pin['sha256'],
                    'stage1_replay_latency_ms': {'n': len(latency_ms),
                        'median': percentile(latency_ms.values(), 0.5),
                        'p95': percentile(latency_ms.values(), 0.95),
                        'definition': 'nearest-rank full-run native Stage-1 replay, including worker overhead; not Hackbook callback-only latency'},
                    'selection_rule': 'strict L1: >=5 labelled, >=50% positive, Stage1 recall <=20%; relaxed L2: >=5, >=40%, <=40%; ranked by positive count then ID'})
    summary_pin = write_exclusive(OUT_SUMMARY, summary)
    print(json.dumps({'status': 'coverage_frozen', 'labelled': len(ordered),
                      'positive': private['positive_n'], 'clean': private['clean_n'],
                      'stage1_positive_hits': private['stage1_positive_hit_n'],
                      'stage1_clean_hits': private['stage1_clean_hit_n'],
                      'qualifying': qualifying, 'summary_sha256': summary_pin['sha256'],
                      'private_sha256': private_pin['sha256'],
                      'latency': summary['stage1_replay_latency_ms']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
