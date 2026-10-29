"""Replay all admitted runs without model calls; inventory exact window cache misses."""
from __future__ import annotations

import json
import multiprocessing as mp
from collections import Counter
from pathlib import Path

import run_reads as rr

from evallab.production.trace_harness_stream import (
    StoredVerdictJudge,
    VerdictCache,
    cache_key,
    replay,
)

RUNS = []
CACHE = None


def audit_one(index):
    run = RUNS[index]
    judge = StoredVerdictJudge(CACHE)
    windows = []

    def inspect(messages):
        system, user = messages[0]['content'], messages[-1]['content']
        rule = json.loads(user)['candidate']['rule']
        key = cache_key(rr.READER, system, user)
        windows.append({'rule': rule, 'key': key, 'cached': CACHE.get(key) is not None})
        return judge(messages)

    stream = replay(run.run_id, run.steps, inspect, judge_only=True)
    return {'run_id': run.run_id, 'windows': windows, 'unavailable': stream.unavailable}


def main():
    global CACHE
    manifest = rr.load_manifest()
    runs = rr.load_runs(manifest, Path.cwd())
    RUNS.extend(runs[rid] for rid in sorted(runs))
    sources = []
    for path in rr.OLD:
        if not path.is_file():
            raise ValueError(f'missing cache {path}')
        sources.extend(rr.jsonl(path))
    CACHE = VerdictCache(rr.ROOT / 'judge-cache.jsonl', model=rr.READER, rows=sources)
    out = rr.ROOT / 'card-audit-runs.jsonl'
    summary = rr.ROOT / 'card-audit-summary.json'
    if out.exists() or summary.exists():
        raise FileExistsError('preserving existing audit')
    with mp.get_context('fork').Pool(8) as pool:
        rows = pool.map(audit_one, range(len(RUNS)), chunksize=4)
    with out.open('x') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')
    counts = {}
    for population in (*rr.ORDER, 'union'):
        selected = [r for r in rows if population == 'union' or population in manifest[r['run_id']]['populations']]
        windows = [w for r in selected for w in r['windows']]
        missing = [w for w in windows if not w['cached']]
        counts[population] = {'runs': len(selected), 'gate_cards': sum(w['rule'] == 'grader_tamper' for w in windows),
                              'gate_missing_cards': sum(w['rule'] == 'grader_tamper' for w in missing),
                              'gate_missing_unique_keys': len({w['key'] for w in missing if w['rule'] == 'grader_tamper'}),
                              'nongate_missing_cards': sum(w['rule'] != 'grader_tamper' for w in missing),
                              'missing_by_rule': dict(Counter(w['rule'] for w in missing)),
                              'unavailable': sum(r['unavailable'] for r in selected)}
    summary.write_text(json.dumps(counts, indent=2, sort_keys=True) + '\n')
    print(json.dumps(counts, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
