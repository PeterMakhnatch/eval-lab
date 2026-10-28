#!/usr/bin/env python3
"""Replay Stage-1 again after applying only the measured TW-13 input-shape repair."""
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
ASSIGNMENT = 'sha256:7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f'
EDIT_SIGNALS = 'b425fe991567663bd01306fe3ba614f846bb0bb478e0c682effca5f3cdf7ebb1'
TRIPWIRES = '714969c55146d3a4648055573e245d3c70ef238527f38da55723e17bbd468279'
OUT = STATE / 'full-dev-coverage-with-tw13-shapes.private.json'
SUMMARY = STATE / 'full-dev-coverage-with-tw13-shapes-summary.json'


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def pin(path):
    with path.open('rb') as stream:
        return {'path': str(path), 'bytes': path.stat().st_size,
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def write_new(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), 'wb') as stream:
        stream.write(encoded(value) + b'\n')
    return pin(path)


def nearest_rank(values, rank):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(rank * len(ordered)) - 1)]


def main():
    require(os.environ.get('EVALLAB_DISABLE_LOCAL_DOCKER') == '1', 'Docker must be disabled')
    require(not OUT.exists() and not SUMMARY.exists(), 'Refusing to overwrite comparison')
    require(pin(ROOT / 'src/evallab/edit_signals.py')['sha256'] == EDIT_SIGNALS
            and pin(ROOT / 'src/evallab/tripwires.py')['sha256'] == TRIPWIRES,
            'Measurement tree is not exactly the measured TW-13 extractor bytes')
    census_path = STATE / 'full-dev-clusters.private.json'
    census = json.loads(census_path.read_bytes())
    sys.path.insert(0, str(ROOT / 'src'))
    from evallab.production import run_atlas as atlas
    from evallab.production import run_atlas_clusters as clusters
    from evallab.production.corpus_queue import CorpusRegistry

    registry = CorpusRegistry(ROOT)
    require(registry.has_assignment_authority() and registry.split_assignments_digest == ASSIGNMENT,
            'Native assignment authority differs')
    labelled = sorted(census['hits'])
    hits, replay_ms = {}, {}
    for run in atlas.cloud_runs(DATABASE, registry=registry, trace_ids=labelled):
        require(run.run_id in census['hits'] and run.split == 'DEV', 'Unfrozen replay identity')
        first = time.perf_counter_ns()
        hits.update(clusters.rule_hits([run]))
        replay_ms[run.run_id] = (time.perf_counter_ns() - first) / 1_000_000
    require(sorted(hits) == labelled, 'Replay did not retain the exact frozen labelled set')
    before = census['hits']
    labels = {}
    for row in census['clusters']:
        for item in row['positives']:
            labels[item] = True
        for item in row['clean']:
            labels[item] = False
    newly_covered = sorted(x for x in labelled if hits[x] and not before[x])
    newly_uncovered = sorted(x for x in labelled if before[x] and not hits[x])
    rows = []
    for row in census['clusters']:
        positives, clean = row['positives'], row['clean']
        rate = len(positives) / (len(positives) + len(clean)) if positives or clean else None
        recall = sum(hits[x] for x in positives) / len(positives) if positives else None
        strict = row['level'] == 1
        qualifies = bool(row['level'] in (1, 2) and row['size'] >= 5
                         and len(positives) + len(clean) >= 5 and rate is not None
                         and rate >= (0.5 if strict else 0.4) and recall is not None
                         and recall <= (0.2 if strict else 0.4))
        rows.append({'cluster_id': row['cluster_id'], 'level': row['level'], 'size': row['size'],
                     'positive_n': len(positives), 'clean_n': len(clean), 'positive_rate': rate,
                     'stage1_positive_hit_n_before': sum(before[x] for x in positives),
                     'stage1_positive_hit_n_after': sum(hits[x] for x in positives),
                     'stage1_recall_before': row['stage1_recall'], 'stage1_recall_after': recall,
                     'stage1_clean_hit_n_before': sum(before[x] for x in clean),
                     'stage1_clean_hit_n_after': sum(hits[x] for x in clean),
                     'qualifies_before': row['qualifies_predeclared_l1_l2_rule'],
                     'qualifies_after': qualifies})
    private = {'schema': 'pg2-coverage-with-tw13-shapes-private/v1', 'created_at': datetime.now(UTC).isoformat(),
               'census': pin(census_path), 'extractor_pins': {'edit_signals': EDIT_SIGNALS, 'tripwires': TRIPWIRES},
               'hits_after': hits, 'replay_ms_by_run': replay_ms,
               'newly_covered_ids': newly_covered, 'newly_uncovered_ids': newly_uncovered, 'clusters': rows}
    private_pin = write_new(OUT, private)
    positives = [x for x in labelled if labels[x] is True]
    clean = [x for x in labelled if labels[x] is False]
    summary = {'schema': 'pg2-coverage-with-tw13-shapes-summary/v1', 'private': private_pin,
               'labelled_n': len(labelled), 'positive_n': len(positives), 'clean_n': len(clean),
               'positive_hits_before': sum(before[x] for x in positives),
               'positive_hits_after': sum(hits[x] for x in positives),
               'clean_hits_before': sum(before[x] for x in clean),
               'clean_hits_after': sum(hits[x] for x in clean),
               'newly_covered_positive_n': sum(labels[x] is True for x in newly_covered),
               'newly_covered_clean_n': sum(labels[x] is False for x in newly_covered),
               'newly_uncovered_n': len(newly_uncovered),
               'qualifying_before': [x['cluster_id'] for x in rows if x['qualifies_before']],
               'qualifying_after': [x['cluster_id'] for x in rows if x['qualifies_after']],
               'qualifying_after_rows': [x for x in rows if x['qualifies_after']],
               'derivation_group': next(x for x in rows if x['cluster_id'] == 'pg2:L1-11.2'),
               'stage1_replay_latency_ms': {'n': len(replay_ms), 'median': nearest_rank(replay_ms.values(), 0.5),
                                            'p95': nearest_rank(replay_ms.values(), 0.95)}}
    summary_pin = write_new(SUMMARY, summary)
    print(json.dumps({'status': 'extraction_repair_coverage_frozen', 'summary': summary,
                      'summary_sha256': summary_pin['sha256']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
