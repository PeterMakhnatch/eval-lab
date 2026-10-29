"""Compare cluster facet fields and cold screen CPU on a read-only store copy.

Run with the new worktree's interpreter: measure.py OLD_WORKTREE NEW_WORKTREE.
Only aggregate counts and timings are emitted; cluster rows remain in memory.
"""
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

STORE = Path('/private/tmp/ps2/store.sqlite')


def worker(version, mode):
    from evallab.production import cloud_trace_export as export
    from evallab.production.corpus_queue import CorpusRegistry

    db = sqlite3.connect(STORE.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    registry = CorpusRegistry()
    if mode == 'identity':
        clusters = [r[0] for r in db.execute('select cluster_id from run_clusters order by cluster_id')]
        result = {}
        with tempfile.TemporaryDirectory(dir='/private/tmp/ps5') as directory:
            for index, cluster in enumerate(clusters):
                output = Path(directory) / f'{index}.json'
                export.export_cluster_scope(STORE, cluster, output, registry=registry)
                rows = json.loads(output.read_text())['rows']
                result[cluster] = {row['trace_id']: [row['facet_producer_revision'], row['facet_served_model']]
                                   for row in rows}
        print(json.dumps(result, sort_keys=True))
        return
    identities = [dict(row) for row in db.execute(export._IDENTITY_SQL)]
    admitted = export.admitted_cloud_task_ids(identities, registry=registry)
    members = export._cluster_members(db.execute(
        'select run_ids from run_clusters where cluster_id=?', ('L1-07',)).fetchone()[0])
    runs = [row['trace_id'] for row in db.execute(export._CLUSTER_SCOPE_SQL, (json.dumps(members),))
            if row['trace_id'] is not None and row['task_id'] in admitted]
    if version == 'new':
        base = export.base_seals()
        admitted = export.admitted_cloud_task_ids(identities, registry=registry, sealed_task_ids=base)
    started = time.process_time()
    if version == 'new':
        export._served_facets(db, identities, admitted, registry, runs, base)
    else:
        export._served_facets(db, identities, admitted, registry, runs)
    print(time.process_time() - started)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
        return
    roots = dict(zip(('old', 'new'), sys.argv[1:], strict=True))

    def run(version, mode):
        env = dict(os.environ, PYTHONPATH=str(Path(roots[version]) / 'src'), EVALLAB_DISABLE_LOCAL_DOCKER='1')
        return json.loads(subprocess.check_output(
            [sys.executable, __file__, '--worker', version, mode], cwd=roots[version], env=env, text=True))

    old, new = run('old', 'identity'), run('new', 'identity')
    samples = {'old': [], 'new': []}
    for i in range(7):
        for version in (('old', 'new') if i % 2 == 0 else ('new', 'old')):
            samples[version].append(run(version, 'cold'))
    print(json.dumps({
        'clusters': len(old), 'rows': sum(map(len, old.values())), 'identical': old == new,
        'n': 7, 'cold_cpu_seconds': samples,
        'median_cpu_seconds': {key: statistics.median(value) for key, value in samples.items()},
    }, indent=2))


if __name__ == '__main__':
    main()
