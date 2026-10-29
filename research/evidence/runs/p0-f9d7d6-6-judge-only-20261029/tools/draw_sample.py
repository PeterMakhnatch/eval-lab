"""Draw the preregistered fresh, label-free reader sample; run from the repo root.

Usage: uv run python research/evidence/runs/p0-f9d7d6-6-judge-only-20261029/tools/draw_sample.py PRIVATE/sample.jsonl > PRIVATE/sample-report.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from evallab.production.trace_harness import DEFAULT_DATABASE
from evallab.production.trace_harness_corpora import load_corpus_labels
from evallab.production.trace_harness_matched import goodfire_dev_items

SEED = 20261029
QUOTA = 100
PRIVATE = Path.home() / '.local/state/evallab-prod-live/trace-harness'
PAIRS = PRIVATE / 'np31-length-matched/goodfire-dev-pairs.jsonl'
P0F5 = PRIVATE / 'p0f5/sample.jsonl'


def digest(run_id: str) -> str:
    return hashlib.sha256(f'{SEED}:{run_id}'.encode()).hexdigest()


def stratum(length: int) -> int:
    return 1 + sum(cut < length for cut in (40, 89))


def main(out: Path) -> None:
    repo = Path.cwd()
    items = goodfire_dev_items(database=DEFAULT_DATABASE, repo_root=repo)
    pairs = [json.loads(line) for line in PAIRS.read_text().splitlines() if line.strip()]
    pair_ids = {r for pair in pairs for r in (pair['enacted'], pair['clean'])}
    labels = load_corpus_labels('goodfire', database=DEFAULT_DATABASE, repo_root=repo)
    old_ids = {r for r in labels.run_ids if labels.positive.get(r) is not None}
    p0f5_ids = {json.loads(line)['run_id'] for line in P0F5.read_text().splitlines() if line.strip()}
    if len(pair_ids) != 246 or len(old_ids) != 150 or len(p0f5_ids) != 555:
        raise ValueError(f'Unexpected exclusion sizes: pairs={len(pair_ids)}, 150={len(old_ids)}, p0f5={len(p0f5_ids)}')
    excluded = pair_ids | old_ids | p0f5_ids
    eligible = [i for i in items if i.run_id not in excluded]
    gate = {i.run_id for i in items}
    report = {'gate_items': len(items), 'exclusions': {name: {'ids': len(ids), 'in_gate': len(ids & gate)}
        for name, ids in (('pairs', pair_ids), ('goodfire_150', old_ids), ('p0f5', p0f5_ids))},
        'eligible': len(eligible), 'cells': {}}
    tasks: Counter[str] = Counter()
    chosen = []
    for s in (1, 2, 3):
        for label in (True, False):
            pool = sorted((i for i in eligible if stratum(i.length) == s and i.label == label), key=lambda i: digest(i.run_id))
            taken = []
            leftover = pool
            cap = 2
            while leftover and len(taken) < QUOTA:
                skipped = []
                for item in leftover:
                    if len(taken) < QUOTA and tasks[item.cluster] < cap:
                        taken.append(item)
                        tasks[item.cluster] += 1
                    else:
                        skipped.append(item)
                leftover = skipped
                if leftover and len(taken) < QUOTA:
                    cap += 1
            report['cells'][f"t{s}-{'enacted' if label else 'clean'}"] = {
                'eligible': len(pool), 'drawn': len(taken), 'task_cap_used': cap}
            chosen.extend((s, i) for i in taken)
    rows = sorted(({'run_id': i.run_id, 'stratum': s, 'steps': i.length} for s, i in chosen), key=lambda r: digest(r['run_id']))
    report.update(drawn=len(rows), max_runs_per_task=max(tasks.values(), default=0), tasks=len(tasks),
                  tasks_over_two=sum(v > 2 for v in tasks.values()),
                  ids_sha256=hashlib.sha256('\n'.join(sorted(r['run_id'] for r in rows)).encode()).hexdigest())
    out.write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main(Path(sys.argv[1]))
