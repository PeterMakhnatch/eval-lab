"""Join paired finding identities without labels; incomplete pairs stay unread."""
import json
from pathlib import Path

ROOT = Path.home() / '.local/state/evallab-prod-live/trace-harness/p0f6'


def load(name):
    path = ROOT / name
    if not path.exists():
        return {}
    return {row['run_id']: row for row in map(json.loads, path.read_text().splitlines())}


def main():
    v8, judge = load('v8.jsonl'), load('judge-only.jsonl')
    rows = []
    for run_id in sorted(v8.keys() & judge.keys()):
        a, b = v8[run_id], judge[run_id]
        if a['verdict'] not in ('alarm', 'clear', 'abstain') or b['verdict'] not in ('alarm', 'clear', 'abstain'):
            continue
        rows.append({'run_id': run_id, 'v8': a['verdict'] == 'alarm', 'judge_only': b['verdict'] == 'alarm',
                     'abstain': a['verdict'] == 'abstain' or b['verdict'] == 'abstain'})
    (ROOT / 'scoring-input.jsonl').write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    print(f'paired completed: {len(rows)}, v8: {len(v8)}, judge-only: {len(judge)}')


if __name__ == '__main__':
    main()
