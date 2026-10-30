"""Counts-only qa-data seal sweep; run from the repository root with a read-only snapshot.

Each invocation resumes a checkpoint and runs for at most --seconds. Outputs and
exceptions are never retained; only aggregate counts and field names are recorded.
"""
import argparse
import collections
import json
import os
import shutil
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
SCRATCH = Path('/private/tmp/ps7/full-sweep')
RESULT = Path(__file__).resolve().parents[1] / 'results.json'
STEP_SEED = Path('/private/tmp/ps7/step-manifest.json')


def main():
    global RESULT, SCRATCH
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=int, default=200)
    ap.add_argument('--command', default=None)
    ap.add_argument('--parity', action='store_true')
    ap.add_argument('--checkpoint', type=Path, default=RESULT)
    ap.add_argument('--scratch', type=Path, default=SCRATCH)
    opts = ap.parse_args()
    RESULT, SCRATCH = opts.checkpoint, opts.scratch
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest='command', required=True))
    ctx = open_context(DB, budget=100000)
    # Connection-local read tuning only: never changes snapshot bytes or admission.
    ctx.database._connection.execute('PRAGMA mmap_size=8589934592')
    ctx.database._connection.execute('PRAGMA cache_size=-65536')
    with ctx.database._transaction():
        admitted, seals = ctx.retriever._admitted_ids()
        rows = ctx.database._rows('SELECT trace_id, task_id FROM agent_traces ORDER BY trace_id')
        clusters = ctx.database._rows('SELECT cluster_id, level FROM run_clusters ORDER BY level, cluster_id')
    admitted = set(admitted)
    pat = _sealed_pattern(frozenset(seals))
    collisions = sorted(t for t in admitted if pat.search(t.casefold()))
    tasks = sorted(admitted)
    runs = [r['trace_id'] for r in rows if r['task_id'] in admitted]
    cases = collections.defaultdict(list)
    for cmd in ('inventory', 'traces', 'runs', 'recount', 'findings'):
        cases[cmd].append([])
    for t in tasks:
        for cmd, args in [('task', [t]), ('traces', ['--task', t]),
                          ('runs', ['--task', t]), ('findings', ['--task', t]), ('split', ['task', t])]:
            cases[cmd].append(args)
    for t in tasks:
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
    with ctx.database._transaction():
        served, _ = ctx.database._admitted_facets(ctx.retriever)
    for key in ('source', 'split', 'facet_model'):
        values[key] = sorted({row[key] for row in served})
    for key, vals in values.items():
        for value in vals:
            cases['runs'].append(['--facet', f'{key}={value}'])
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for fmt in ('atif', 'harbor', 'scout'):
        for offset in range(0, len(runs), 200):
            cases['export'].append([
                *[arg for run_id in runs[offset:offset + 200] for arg in ('--trace-id', run_id)],
                '--format', fmt, '--out', str(SCRATCH / 'export'),
            ])
    for key, vals in values.items():
        for value in vals:
            for fmt in ('atif', 'harbor', 'scout'):
                cases['export'].append([
                    '--facet', f'{key}={value}', '--limit', '1', '--format', fmt,
                    '--out', str(SCRATCH / 'export'),
                ])
    # Reuse the reward-free joins from ATIF batches instead of re-exporting every run.
    for run_id in runs:
        cases['steps'].append([str(STEP_SEED), run_id])
    cases['scan'].append([str(SCRATCH / 'export' / 'transcripts.jsonl')])
    for index, row in enumerate(clusters):
        cases['export-cluster-scope'].append([row['cluster_id'], str(SCRATCH / f'scope-{index:03d}.json')])
    if RESULT.exists():
        result = json.loads(RESULT.read_text())
    else:
        result = {'seals': len(seals), 'admitted_tasks': len(admitted),
                  'collision_tasks': len(collisions), 'admitted_runs': len(runs),
                  'cluster_records': len(clusters), 'commands': {}, 'parity': {}}
    result['planned'] = {cmd: len(items) for cmd, items in cases.items()}
    result['facet_values'] = sum(map(len, values.values()))
    encoding = {
        'escaped_seals': sum(json.dumps(s, ensure_ascii=False)[1:-1] != s for s in seals),
        'delimiter_seals': sum(any(ch in s for ch in '"{}[],\\') for s in seals),
        'numeric_fragment_seals': sum(bool(s) and all(ch in '-+.0123456789e' for ch in s) for s in seals),
        'literal_fragment_seals': sum(s in lit for s in seals for lit in ('true', 'false', 'null')),
    }
    if any(encoding.values()):
        raise RuntimeError('seal set requires canonical JSON matching rather than string-leaf matching')
    result['encoding'] = encoding
    # Collision ordinal sets are private checkpoint state; published JSON contains counts only.
    private = SCRATCH / 'collision-sets.json'
    distinct = json.loads(private.read_text()) if private.exists() else {}

    def classify(value, hits, field='root'):
        if isinstance(value, dict):
            for key, item in value.items():
                classify(key, hits, '<key>')
                classify(item, hits, key)
        elif isinstance(value, list):
            for item in value:
                classify(item, hits, field)
        elif isinstance(value, str):
            text = value.casefold()
            if not pat.search(text):
                return
            spans = []
            for index, task in enumerate(collisions):
                for identity in (task.casefold(), task.rsplit('/', 1)[-1].split('@', 1)[0].casefold()):
                    offset = 0
                    while (pos := text.find(identity, offset)) >= 0:
                        spans.append((pos, pos + len(identity), index))
                        offset = pos + 1
            for seal in seals:
                if not seal:
                    continue
                offset = 0
                while (pos := text.find(seal, offset)) >= 0:
                    owners = {i for a, b, i in spans if a <= pos and pos + len(seal) <= b}
                    cls = 'b' if owners else 'c'
                    safe_field = '<sealed-key>' if pat.search(field.casefold()) else field
                    hit = hits.setdefault(f'{cls}:{safe_field}', {'occurrences': 0, 'tasks': set()})
                    hit['occurrences'] += 1
                    hit['tasks'].update(owners)
                    offset = pos + 1

    def invoke(cmd, argv):
        ctx.budget = Budget(99746)
        if cmd == 'scan':
            export_args = parser.parse_args([
                'qa-data', 'export', '--trace-id', runs[0],
                '--format', 'scout', '--out', str(SCRATCH / 'export'),
            ])
            _dispatch('export', ctx, export_args)
            ctx.budget = Budget(99746)
        if cmd == 'export-cluster-scope':
            from evallab.production.cli import _export_cluster_scope

            manifest = Path(argv[1])
            args = argparse.Namespace(source_database=Path(DB), cluster_id=argv[0], manifest=manifest)
            receipt = _export_cluster_scope(args, Path.cwd())
            return {'receipt': receipt, 'manifest': json.loads(manifest.read_text())}
        args = parser.parse_args(['qa-data', cmd, *argv])
        data = (_dispatch_without_store(cmd, args, ctx.budget)
                if cmd in ('steps', 'scan') else _dispatch(cmd, ctx, args))
        if cmd == 'export' and args.format == 'atif':
            from evallab.production.qa_data_export import _manifest

            joins = json.loads(STEP_SEED.read_text()) if STEP_SEED.exists() else {}
            joins.update(_manifest(Path(data['sealed_manifest'])))
            STEP_SEED.write_text(json.dumps(joins))
        return data

    def cleanup():
        for path in SCRATCH.iterdir():
            if path.name == private.name:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def checkpoint():
        private.write_text(json.dumps(distinct, sort_keys=True))
        RESULT.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    start = time.monotonic()
    for cmd, invocations in cases.items():
        if opts.command and opts.command != cmd:
            continue
        if opts.parity:
            if cmd in ('export', 'export-cluster-scope', 'steps', 'scan'):
                continue
            # Six evenly spaced cases of each read-only command, 66 total.
            invocations = [invocations[i * (len(invocations) - 1) // 5] for i in range(6)]
            counts = result['parity'].setdefault(cmd, {'invocations': 0, 'matches': 0})
        else:
            counts = result['commands'].setdefault(cmd, {
                'invocations': 0, 'replies': 0, 'refusals': 0, 'b': 0, 'c': 0,
                'fields': {}, 'cpu_seconds': 0, 'wall_seconds': 0,
            })
        while counts['invocations'] < len(invocations):
            argv = invocations[counts['invocations']]
            cleanup()
            cpu, wall = time.process_time(), time.monotonic()
            hits = {}
            try:
                data, error = invoke(cmd, argv), None
            except QaDataError as exc:
                data, error = None, str(exc)
            if opts.parity:
                flags = [cmd, *argv, '--budget', '105157', '--cloud-database', DB]
                proc = subprocess.run(
                    ['.venv/bin/evallab', 'qa-data', *flags], capture_output=True, text=True,
                    check=False, timeout=150,
                    env={**os.environ, 'EVALLAB_DISABLE_LOCAL_DOCKER': '1'},
                )
                try:
                    actual = json.loads(proc.stdout)
                except ValueError:
                    actual = {}
                match = ((error is None and proc.returncode == 0 and actual.get('ok')
                          and actual.get('data') == data)
                         or (error is not None and proc.returncode == 2 and not actual.get('ok')
                             and actual.get('error') == error))
                counts['matches'] += int(bool(match))
            else:
                counts['cpu_seconds'] += time.process_time() - cpu
                counts['wall_seconds'] += time.monotonic() - wall
                counts['replies' if error is None else 'refusals'] += 1
                classify(data if error is None else error, hits, 'root' if error is None else 'error')
                reply_tasks = set()
                for label, hit in hits.items():
                    cls = label[0]
                    counts[cls] += hit['occurrences']
                    stats = counts['fields'].setdefault(label, {'occurrences': 0, 'replies': 0, 'distinct_tasks': 0})
                    stats['occurrences'] += hit['occurrences']
                    stats['replies'] += 1
                    key = f'{cmd}:{label}'
                    distinct[key] = sorted(set(distinct.get(key, [])) | hit['tasks'])
                    stats['distinct_tasks'] = len(distinct[key])
                    reply_tasks.update(hit['tasks'])
                if reply_tasks:
                    counts['b_replies'] = counts.get('b_replies', 0) + 1
                    distinct[cmd] = sorted(set(distinct.get(cmd, [])) | reply_tasks)
                counts['b_distinct_tasks'] = len(distinct.get(cmd, []))
            counts['invocations'] += 1
            if not opts.parity:
                counts['cpu_seconds_per_1000'] = 1000 * counts['cpu_seconds'] / counts['invocations']
            cleanup()
            checkpoint()
            if time.monotonic() - start >= opts.seconds:
                ctx.database.close()
                print(json.dumps({'last_command': cmd, 'completed': counts['invocations'], 'total': len(invocations)}))
                return
    ctx.database.close()
    print(json.dumps({'complete': True, 'parity': opts.parity, 'command': opts.command}))

if __name__ == '__main__':
    main()
