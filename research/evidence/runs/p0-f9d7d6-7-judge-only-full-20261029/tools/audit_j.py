"""Compare cache-only v8 non-gate confirmed rules to historical decomposition."""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import run_reads as rr

from evallab.production.trace_harness_stream import StoredVerdictJudge, VerdictCache, replay

RUNS = []
CACHE = None


def audit_one(index):
    run = RUNS[index]
    stream = replay(run.run_id, run.steps, StoredVerdictJudge(CACHE))
    return {'run_id': run.run_id, 'judge_confirmed': sorted({c['rule'] for c in stream.cards()
            if c['status'] == 'confirmed' and c['rule'] != 'grader_tamper'}),
            'unavailable': stream.unavailable}


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
    historical = {}
    for name in ('p0f5', 'p0-f9d7d6-3'):
        path = rr.STATE / 'evallab-prod-live/trace-harness' / name / 'decomposition-rows.jsonl'
        for row in rr.jsonl(path):
            historical.setdefault(row['run_id'], []).append(row)
    with mp.get_context('fork').Pool(8) as pool:
        rows = pool.map(audit_one, range(len(RUNS)), chunksize=4)
    missing, mismatch = [], []
    for row in rows:
        previous = historical.get(row['run_id'], [])
        if not previous:
            missing.append(row['run_id'])
        elif any(sorted(r['judge_confirmed']) != row['judge_confirmed'] for r in previous):
            mismatch.append(row['run_id'])
    result = {'runs': len(rows), 'missing_historical': missing, 'mismatches': mismatch,
              'unavailable': sum(row['unavailable'] for row in rows)}
    out = rr.ROOT / 'historical-j-audit.json'
    with out.open('x') as handle:
        json.dump(result, handle, sort_keys=True, indent=2)
        handle.write('\n')
    print(json.dumps({'runs': len(rows), 'missing_historical': len(missing),
                      'mismatches': len(mismatch), 'unavailable': result['unavailable']}), flush=True)


if __name__ == '__main__':
    main()
