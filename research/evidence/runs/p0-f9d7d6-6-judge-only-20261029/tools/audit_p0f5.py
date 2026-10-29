"""Label-free replay of the 555 p0f5 cached v8 judgments and gate cards."""
import json
import multiprocessing as mp
from pathlib import Path

from evallab.production.trace_harness import resolve_runs
from evallab.production.trace_harness_stream import StoredVerdictJudge, VerdictCache, replay

ROOT = Path.home() / '.local/state/evallab-prod-live/trace-harness/p0f5'
READER = 'anthropic/claude-haiku-5-5'
OLD = {r['run_id']: r for r in map(json.loads, (ROOT / 'decomposition-rows.jsonl').read_text().splitlines())}
CACHE = VerdictCache(ROOT / 'judge-cache-anthropic_claude-haiku-5-5.jsonl', model=READER)


def check(run):
    stream = replay(run.run_id, run.steps, StoredVerdictJudge(CACHE))
    cards = stream.cards()
    confirmed = sorted({c['rule'] for c in cards if c['status'] == 'confirmed' and c['rule'] != 'grader_tamper'})
    gate = [c for c in cards if c['rule'] == 'grader_tamper']
    return (run.run_id, confirmed == OLD[run.run_id]['judge_confirmed'], bool(gate),
            sum(c['triage'] == 'gate_pass_through' for c in gate), stream.unavailable)


def main():
    runs = resolve_runs(','.join(sorted(OLD)), repo_root=Path.cwd())
    with mp.get_context('fork').Pool(8) as pool:
        rows = pool.map(check, runs, chunksize=4)
    print(json.dumps({'expected': len(OLD), 'resolved': len(runs), 'matches': sum(r[1] for r in rows),
                      'mismatches': [r[0] for r in rows if not r[1]], 'gate_runs': sum(r[2] for r in rows),
                      'unjudged_gate_cards': sum(r[3] for r in rows), 'unavailable': sum(r[4] for r in rows)}))


if __name__ == '__main__':
    main()
