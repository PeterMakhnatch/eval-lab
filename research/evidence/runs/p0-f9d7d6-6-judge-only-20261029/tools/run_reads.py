"""Run paired v8 and judge-only Haiku reads with quota, wall and attempt stops."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from evallab.production.trace_harness import (
    DEFAULT_DATABASE,
    FindingStore,
    HarnessOptions,
    resolve_runs,
    run_detectors,
)
from evallab.production.trace_harness_stream import (
    CallGuard,
    CascadeJudgeOnlyStreamDetector,
    CascadeStreamDetector,
    QuotaGate,
    QuotaStop,
    VerdictCache,
    live_call,
    quota_used,
)

ROOT = Path.home() / '.local/state/evallab-prod-live/trace-harness/p0f6'
SPEND = Path.home() / '.local/state/evallab-failure-library/spend.jsonl'
READER = 'anthropic/claude-haiku-5-5'
DEADLINE = datetime.fromisoformat('2026-10-29T21:39:57+00:00').timestamp()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    ledger = ROOT / 'attempts.jsonl'
    ids = [json.loads(line)['run_id'] for line in (ROOT / 'sample.jsonl').read_text().splitlines()]
    resolved = {r.run_id:r for r in resolve_runs(','.join(ids), repo_root=Path.cwd())}
    runs = [resolved[r] for r in ids if r in resolved]
    print('sample', len(ids), 'resolved', len(runs), flush=True)
    gate = QuotaGate(provider='anthropic', stop=.85, refresh_seconds=30)
    guard = CallGuard(max_attempts=1800)
    call = live_call(READER, gate, ledger, guard)
    cache = VerdictCache(ROOT / 'judge-cache.jsonl', model=READER)
    options = HarnessOptions(database=DEFAULT_DATABASE, repo_root=Path.cwd(), allow_model=True, reader=READER)
    detectors = [CascadeStreamDetector(options, cache=cache, call=call, workers=16),
                 CascadeJudgeOnlyStreamDetector(options, cache=cache, call=call, workers=16)]
    stores = [FindingStore(ROOT / 'v8.jsonl'), FindingStore(ROOT / 'judge-only.jsonl')]
    for index in range(0,len(runs),16):
        if time.time() >= DEADLINE:
            print('STOP wall deadline',flush=True)
            break
        try:
            quota=quota_used('anthropic')
        except QuotaStop as exc:
            print('STOP quota unavailable',str(exc),flush=True)
            break
        if quota >= .85:
            print('STOP quota',quota,flush=True)
            break
        chunk=runs[index:index+16]
        offset=ledger.stat().st_size if ledger.exists() else 0
        for detector,store in zip(detectors,stores,strict=True):
            run_detectors(chunk,[detector],store,log=lambda msg: print(msg,flush=True))
            if guard.stop_reason or detector.stats.quota_stopped:
                break
        rows = []
        if ledger.exists():
            with ledger.open('rb') as f:
                f.seek(offset)
                rows = [json.loads(line) for line in f if line.strip()]
        row={'ts':datetime.now(UTC).isoformat(), 'route':READER, 'usd':0, 'purpose':'p0-SELF-f9d7d6-6',
             'batch':index//16, 'runs':len(chunk), 'attempts':len(rows),
             'served':dict(Counter(str(r.get('served')) for r in rows)),
             'rejected':sum(not r.get('ok') for r in rows),
             'input_tokens':sum(int(r.get('input_tokens') or 0) for r in rows),
             'output_tokens':sum(int(r.get('output_tokens') or 0) for r in rows)}
        SPEND.parent.mkdir(parents=True,exist_ok=True)
        with SPEND.open('a') as f:
            f.write(json.dumps(row,sort_keys=True)+'\n')
        print('batch',index//16,'quota',quota,'guard',guard.as_dict(),flush=True)
        if guard.stop_reason or any(d.stats.quota_stopped for d in detectors):
            print('STOP guard',guard.as_dict(),flush=True)
            break
    print(json.dumps({'guard':guard.as_dict(),'stats':[d.stats.as_dict() for d in detectors]}),flush=True)


if __name__ == '__main__':
    main()
