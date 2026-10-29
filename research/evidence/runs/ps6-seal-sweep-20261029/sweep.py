"""Counts-only qa-data seal sweep; run from the repository root with a read-only snapshot.

Each invocation resumes a checkpoint and runs for at most --seconds. Outputs and
exceptions are never retained; only aggregate counts and field names are recorded.
"""
import argparse
import collections
import json
import os
import subprocess
import time
from pathlib import Path

from evallab.production.cloud_trace_db import _sealed_pattern
from evallab.production.qa_data import (
    Budget,
    QaDataError,
    _dispatch,
    _dispatch_without_store,
    open_context,
    register_parser,
)

DB = '/private/tmp/ps2/store.sqlite'
SCRATCH = Path('/private/tmp/ps5/i3-sweep')
RESULT = Path(__file__).with_name('results.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=int, default=200)
    ap.add_argument('--parity', action='store_true')
    opts = ap.parse_args()
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest='command', required=True))
    ctx = open_context(DB, budget=100000)
    with ctx.database._transaction():
        admitted, seals = ctx.retriever._admitted_ids()
        rows = ctx.database._rows('SELECT trace_id, task_id FROM agent_traces ORDER BY trace_id')
        task_rows = ctx.database._rows('SELECT task_id, dataset_id FROM corpus_tasks ORDER BY task_id')
        clusters = ctx.database._rows('SELECT cluster_id, level FROM run_clusters ORDER BY level, cluster_id')
    admitted = set(admitted)
    pat = _sealed_pattern(frozenset(seals))
    collisions = sorted(t for t in admitted if pat.search(t.casefold()))
    identities = set()
    for task in collisions:
        identities.add(task.casefold())
        identities.add(task.rsplit('/', 1)[-1].split('@', 1)[0].casefold())
    groups = collections.defaultdict(list)
    for row in task_rows:
        if row['task_id'] in admitted:
            groups[row['dataset_id']].append(row['task_id'])
    tasks = []
    for i in range(30):
        for dataset in sorted(groups):
            if i < len(groups[dataset]) and len(tasks) < 30:
                tasks.append(groups[dataset][i])
    tasks.extend(task for task in collisions if task not in tasks)
    tasks.extend(task for task in sorted(admitted)[:3] if task not in tasks)
    runs = [r['trace_id'] for r in rows if r['task_id'] in admitted][:30]
    special = 'retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4'
    if any(r['trace_id'] == special and r['task_id'] in admitted for r in rows) and special not in runs:
        runs[-1] = special
    if opts.parity:
        outcomes = collections.defaultdict(lambda: {'cases': 0, 'matches': 0})
        for task, run_id in zip(sorted(admitted)[:3], runs[:3], strict=True):
            checks = [('task', [task]), ('history', [task]), ('traces', ['--task', task]),
                      ('runs', ['--task', task]), ('split', ['task', task]), ('findings', ['--task', task]),
                      ('window', [run_id]), ('trace-card', [run_id, '--no-ledgers']),
                      ('clusters', ['--level', '1']), ('inventory', [])]
            for cmd, arguments in checks:
                flags = [cmd, *arguments, '--budget', '105157', '--cloud-database', DB]
                args = parser.parse_args(['qa-data', *flags])
                ctx.budget = Budget(99746)
                try:
                    expected, error = _dispatch(cmd, ctx, args), None
                except QaDataError as exc:
                    expected, error = None, str(exc)
                proc = subprocess.run(
                    ['.venv/bin/evallab', 'qa-data', *flags], capture_output=True, text=True, check=False,
                    env={**os.environ, 'EVALLAB_DISABLE_LOCAL_DOCKER': '1'},
                )
                try:
                    actual = json.loads(proc.stdout)
                except ValueError:
                    actual = {}
                match = (error is None and actual.get('ok') and actual.get('data') == expected) or (
                    error is not None and not actual.get('ok') and actual.get('error') == error
                )
                outcomes[cmd]['cases'] += 1
                outcomes[cmd]['matches'] += int(bool(match))
        ctx.database.close()
        print(json.dumps(outcomes, sort_keys=True))
        return
    cases = collections.defaultdict(list)
    for cmd in ('inventory', 'traces', 'runs', 'recount', 'findings'):
        cases[cmd].append([])
    for t in tasks:
        for cmd, args in [('task', [t]), ('traces', ['--task', t]),
                          ('runs', ['--task', t]), ('findings', ['--task', t]), ('split', ['task', t])]:
            cases[cmd].append(args)
    for t in sorted(set(tasks[:30]) | set(collisions)):
        cases['history'].append([t])
    for t in sorted(admitted)[:3]:
        if [t] not in cases['history']:
            cases['history'].append([t])
    for r in runs:
        for cmd, args in [('window', [r]), ('trace-card', [r, '--no-ledgers']), ('findings', ['--run', r]), ('runs', ['--like', r])]:
            cases[cmd].append(args)
    for level in (1, 2, 3):
        cases['clusters'].append(['--level', str(level)])
    for row in clusters:
        cases['clusters'].append(['--members', row['cluster_id']])
        cases['runs'].append(['--cluster', row['cluster_id']])
    values = {'outcome': ['pass', 'fail', 'unknown'], 'tests': ['untouched', 'read', 'modified', 'deleted', 'replaced'],
              'eval_awareness': ['none', 'mentions_tests', 'reasons_about_grader', 'plans_to_game'],
              'grader_or_harness_touched': ['true', 'false'], 'answer_material_accessed': ['true', 'false'],
              'success_claim_without_evidence': ['true', 'false'], 'suspicion': ['0', '1', '2', '3']}
    for key, vals in values.items():
        for value in vals:
            cases['runs'].append(['--facet', f'{key}={value}'])
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for fmt in ('atif', 'harbor', 'scout'):
        cases['export'].append(['--trace-id', runs[0], '--format', fmt, '--out', str(SCRATCH / fmt)])
    cases['steps'].append([str(SCRATCH / 'atif.sealed-manifest.json'), runs[0]])
    cases['scan'].append([str(SCRATCH / 'scout' / 'transcripts.jsonl')])
    for index, row in enumerate(clusters):
        cases['export-cluster-scope'].append([row['cluster_id'], str(SCRATCH / f'scope-{index:03d}.json')])
    if RESULT.exists():
        result = json.loads(RESULT.read_text())
    else:
        result = {'seals': len(seals), 'admitted_tasks': len(admitted), 'sampled_tasks': len(tasks),
                  'collision_tasks': len(collisions), 'sampled_runs': len(runs), 'cluster_records': len(clusters),
                  'commands': {}}
    result.update(sampled_tasks=len(tasks), sampled_runs=len(runs))
    encoding = {
        'escaped_seals': sum(json.dumps(s, ensure_ascii=False)[1:-1] != s for s in seals),
        'delimiter_seals': sum(any(ch in s for ch in '"{}[],\\') for s in seals),
        'numeric_fragment_seals': sum(bool(s) and all(ch in '-+.0123456789e' for ch in s) for s in seals),
        'literal_fragment_seals': sum(s in lit for s in seals for lit in ('true', 'false', 'null')),
    }
    if any(encoding.values()):
        raise RuntimeError('seal set requires canonical JSON matching rather than string-leaf matching')
    result['encoding'] = encoding
    def classify(value, counts, field='root'):
        if isinstance(value, dict):
            for key, item in value.items():
                classify(key, counts, '<key>')
                classify(item, counts, key)
        elif isinstance(value, list):
            for item in value:
                classify(item, counts, field)
        elif isinstance(value, str):
            text = value.casefold()
            if not pat.search(text):
                return
            spans = []
            for identity in identities:
                offset = 0
                while (pos := text.find(identity, offset)) >= 0:
                    spans.append((pos, pos + len(identity)))
                    offset = pos + 1
            for seal in seals:
                if not seal:
                    continue
                offset = 0
                while (pos := text.find(seal, offset)) >= 0:
                    inside = any(a <= pos and pos + len(seal) <= b for a, b in spans)
                    cls = 'b' if inside else 'unclassified'
                    counts[cls] = counts.get(cls, 0) + 1
                    fields = counts.setdefault('fields', {})
                    label = f'{cls}:{"<sealed-key>" if pat.search(field.casefold()) else field}'
                    fields[label] = fields.get(label, 0) + 1
                    offset = pos + 1
    start = time.monotonic()
    for cmd, invocations in cases.items():
        counts = result['commands'].setdefault(cmd, {'invocations': 0, 'replies': 0, 'refusals': 0, 'b': 0, 'unclassified': 0})
        while counts['invocations'] < len(invocations):
            argv = invocations[counts['invocations']]
            ctx.budget = Budget(99746)
            try:
                if cmd == 'export-cluster-scope':
                    from evallab.production.cli import _export_cluster_scope

                    manifest = Path(argv[1])
                    args = argparse.Namespace(source_database=Path(DB), cluster_id=argv[0], manifest=manifest)
                    receipt = _export_cluster_scope(args, Path.cwd())
                    data = {'receipt': receipt, 'manifest': json.loads(manifest.read_text())}
                else:
                    args = parser.parse_args(['qa-data', cmd, *argv])
                    data = (_dispatch_without_store(cmd, args, ctx.budget) if cmd in ('steps', 'scan') else _dispatch(cmd, ctx, args))
                counts['replies'] += 1
                classify(data, counts)
            except QaDataError as exc:
                counts['refusals'] += 1
                classify(str(exc), counts, 'error')
            counts['invocations'] += 1
            RESULT.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
            if time.monotonic() - start >= opts.seconds:
                ctx.database.close()
                print(json.dumps({'last_command': cmd, 'completed': counts['invocations'], 'total': len(invocations)}))
                return
    ctx.database.close()
    print(json.dumps({'complete': True}))

if __name__ == '__main__':
    main()
