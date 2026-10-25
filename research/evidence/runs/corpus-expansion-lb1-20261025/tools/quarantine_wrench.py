#!/usr/bin/env python3
"""Quarantine the two Terminal Wrench tasks admitted on an invalid DEV-clearance basis.

Cause: the allowlist (sha256 c82b251f...) was derived from stored DEV rows, and the verifier
(pW-SELF-3) reports that those two names are `test` in other ledgers. The protected registry
is NOT opened here; the verifier's finding is acted on because quarantine is cheap and reversible.

Dry run by default. --execute: export rows verbatim, delete them in ONE transaction with exact
count assertions, then move (not delete) CAS objects referenced only by those rows.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, shutil, sqlite3, sys
from datetime import UTC, datetime
from pathlib import Path

os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
ROOT = Path('/private/tmp/lb1-20261025')
WORKER = ROOT / 'goodfire-wrench'
PROD = Path('/Users/petermakhnatch/.local/state/evallab-prod-live')
RECEIPTS = PROD / 'corpus-expansion-lb1'
QUAR = RECEIPTS / 'quarantine-terminal-wrench'
DB = PROD / 'cloud-trace-db.sqlite'
SNAP = ROOT / 'cloud_trace_db-822d9c49e.py'
SNAP_SHA = '8233cdad2d080059e4d1bfc41d74cb9f89032c0fefa0217847c87d8230e86a59'
URI = re.compile(r'cas://sha256/([0-9a-f]{64})')
REMOVE = {'corpus_tasks': 2, 'agent_traces': 126, 'trace_steps': 1079}
BEFORE = {'corpus_tasks': 3375, 'task_issues': 252, 'agent_traces': 3563, 'trace_steps': 223589}
AFTER = {'corpus_tasks': 3373, 'task_issues': 252, 'agent_traces': 3437, 'trace_steps': 222510}


def sha_bytes(b): return hashlib.sha256(b).hexdigest()
def sha_file(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as s:
        for blk in iter(lambda: s.read(1 << 20), b''): h.update(blk)
    return h.hexdigest()


assert sha_file(SNAP) == SNAP_SHA
spec = importlib.util.spec_from_file_location('lb1_quarantine_frozen_db', SNAP)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

TASK_IDS = [json.loads(l)['task_id'] for l in (WORKER / 'wrench/handoff-v3/tasks.jsonl').open()]
assert len(TASK_IDS) == 2 and all(t.startswith('terminal-wrench/') for t in TASK_IDS)
MARKS = ','.join('?' for _ in TASK_IDS)


def scan_remaining(conn):
    """URIs referenced by every row that is NOT one of the target rows."""
    found = set()
    plans = [
        ('corpus_tasks', f'SELECT * FROM corpus_tasks WHERE task_id NOT IN ({MARKS})', TASK_IDS),
        ('task_issues', f'SELECT * FROM task_issues WHERE task_id NOT IN ({MARKS})', TASK_IDS),
        ('agent_traces', f'SELECT * FROM agent_traces WHERE task_id NOT IN ({MARKS})', TASK_IDS),
        ('trace_steps', f'SELECT * FROM trace_steps WHERE trace_id NOT IN (SELECT trace_id FROM agent_traces WHERE task_id IN ({MARKS}))', TASK_IDS),
    ]
    for _, sql, params in plans:
        for row in conn.execute(sql, params):
            text = '\x00'.join(str(v) for v in row if isinstance(v, str))
            found.update(URI.findall(text))
    return found


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--execute', action='store_true'); args = ap.parse_args()
    ro = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)
    counts = {t: ro.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in BEFORE}
    assert counts == BEFORE, counts
    trace_ids = [r[0] for r in ro.execute(f'SELECT trace_id FROM agent_traces WHERE task_id IN ({MARKS}) ORDER BY trace_id', TASK_IDS)]
    steps = ro.execute(f'SELECT count(*) FROM trace_steps WHERE trace_id IN (SELECT trace_id FROM agent_traces WHERE task_id IN ({MARKS}))', TASK_IDS).fetchone()[0]
    assert (len(trace_ids), steps) == (REMOVE['agent_traces'], REMOVE['trace_steps']), (len(trace_ids), steps)
    assert ro.execute(f'SELECT count(*) FROM task_issues WHERE task_id IN ({MARKS})', TASK_IDS).fetchone()[0] == 0
    assert ro.execute(f"SELECT count(*) FROM corpus_tasks WHERE dataset_id='terminal-wrench'").fetchone()[0] == 2
    fetch = json.loads((WORKER / 'wrench-fetch-dev-subset-v3.json').read_text())
    target_uris = {f['cas_uri'][13:] for f in fetch['files']}
    with module.CloudTraceDatabase(DB) as db:
        exported = [db.get_trace(t) for t in trace_ids]
        tasks = [db.get_task(t) for t in TASK_IDS]
    blob = json.dumps({'tasks': tasks, 'traces': exported}, ensure_ascii=False, sort_keys=True)
    target_uris |= set(URI.findall(blob))
    shared = scan_remaining(ro)
    movable = sorted(target_uris - shared)
    kept_shared = sorted(target_uris & shared)
    cas = PROD / 'cas' / 'sha256'
    present = [d for d in movable if (cas / d).is_file()]
    plan = {'task_ids': TASK_IDS, 'traces': len(trace_ids), 'steps': steps, 'referenced_cas_objects': len(target_uris),
            'shared_with_remaining_rows_kept_in_place': len(kept_shared), 'to_move': len(movable), 'present_to_move': len(present),
            'bytes_to_move': sum((cas / d).stat().st_size for d in present)}
    print(json.dumps({'dry_run': not args.execute, 'plan': plan}, indent=2))
    if not args.execute:
        return
    QUAR.mkdir(mode=0o700, exist_ok=False)
    rows_path = QUAR / 'removed-rows.jsonl'
    with rows_path.open('x', encoding='utf-8') as out:
        for t in tasks: out.write(json.dumps({'kind': 'task', 'row': t}, ensure_ascii=False, sort_keys=True) + '\n')
        for t in exported: out.write(json.dumps({'kind': 'trace_hydrated', 'row': t}, ensure_ascii=False, sort_keys=True) + '\n')
    rows_sha = sha_file(rows_path)
    with module.CloudTraceDatabase(DB) as db:
        with db._transaction(write=True):
            n_steps = db._execute(f'DELETE FROM trace_steps WHERE trace_id IN (SELECT trace_id FROM agent_traces WHERE task_id IN ({MARKS}))', TASK_IDS).rowcount
            n_traces = db._execute(f'DELETE FROM agent_traces WHERE task_id IN ({MARKS})', TASK_IDS).rowcount
            n_tasks = db._execute(f'DELETE FROM corpus_tasks WHERE task_id IN ({MARKS}) AND dataset_id=?', [*TASK_IDS, 'terminal-wrench']).rowcount
            assert (n_tasks, n_traces, n_steps) == (2, 126, 1079), (n_tasks, n_traces, n_steps)  # else ROLLBACK
        deleted = {'corpus_tasks': n_tasks, 'agent_traces': n_traces, 'trace_steps': n_steps}
        fts = {}
        for name in ('corpus_tasks_fts', 'task_issues_fts', 'agent_traces_fts', 'trace_steps_fts'):
            db._execute(f"INSERT INTO {name}({name}) VALUES('integrity-check')")  # raises on any mismatch
            fts[name] = 'ok'
    moved = []
    for d in present:
        src = cas / d
        assert sha_file(src) == d
        dst = QUAR / 'cas' / 'sha256' / d
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        assert sha_file(dst) == d and not src.exists()
        moved.append({'sha256': d, 'bytes': dst.stat().st_size})
    # independent post-state verification
    post = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)
    post_counts = {t: post.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in AFTER}
    assert post_counts == AFTER, post_counts
    assert post.execute("SELECT count(*) FROM corpus_tasks WHERE dataset_id='terminal-wrench'").fetchone()[0] == 0
    assert post.execute("SELECT count(*) FROM agent_traces WHERE run_id LIKE 'terminal-wrench%'").fetchone()[0] == 0
    integrity = post.execute('PRAGMA integrity_check').fetchone()[0]
    fk = post.execute('PRAGMA foreign_key_check').fetchall()
    assert integrity == 'ok' and fk == []
    baseline = json.loads((ROOT / 'preexisting-dev-row-fingerprints.private.json').read_text())
    pks = {'corpus_tasks': ('task_id',), 'agent_traces': ('trace_id',), 'task_issues': ('issue_id',), 'trace_steps': ('trace_id', 'step_index')}
    originals = {}
    for table, info in baseline['tables'].items():
        ok = 0
        for key, expected in info['row_hashes'].items():
            row = post.execute('SELECT * FROM ' + table + ' WHERE ' + ' AND '.join(c + '=?' for c in pks[table]), json.loads(key)).fetchone()
            cols = [c[1] for c in post.execute(f'PRAGMA table_info({table})')]
            canon = json.dumps(dict(zip(cols, row)), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            assert sha_bytes(canon.encode()) == expected
            ok += 1
        originals[table] = ok
    remaining = set(scan_remaining(post))  # target rows are gone, so this is now every row
    missing_remaining = sorted(d for d in remaining if not (cas / d).is_file())
    assert not (set(movable) & remaining)
    gf = json.loads((RECEIPTS / 'safe-traces-ingestion.json').read_text())['goodfire']['decoded_runs']
    gf_check = []
    for r in gf:
        rid = 'goodfire-reward-hack-data@bd8fb473568b8ef1d12fc2325a5c8af939c15ad9/' + r['run']
        t, s = post.execute('SELECT count(*), sum(step_count) FROM agent_traces WHERE run_id=?', (rid,)).fetchone()
        assert (t, s) == (r['native_rows'], r['steps']); gf_check.append({'run': r['run'], 'traces': t, 'steps': s})
    receipt = {'schema': 'lb1.terminal_wrench_quarantine/v1', 'created_at': datetime.now(UTC).isoformat(),
               'reason': 'The 2 Terminal Wrench tasks were admitted on an allowlist derived from stored DB rows that inherited a tb2-to-tb21-v1 DEV mislabel; verifier pW-SELF-3 reports both names are `test` in other ledgers. Not a tb4-audit-v1 TEST (41) or MiMo heldout (33) breach per the verifier. The protected registry was NOT opened to re-derive this; the finding was acted on because quarantine is cheap and reversible.',
               'script_sha256': sha_file(__file__), 'database_module_sha256': SNAP_SHA,
               'removed_rows': deleted, 'removed_rows_export': {'path': str(rows_path), 'sha256': rows_sha, 'bytes': rows_path.stat().st_size},
               'cas_objects_referenced': len(target_uris), 'cas_objects_moved_not_deleted': len(moved),
               'cas_bytes_moved': sum(m['bytes'] for m in moved), 'cas_objects_shared_kept': len(kept_shared),
               'cas_objects_listed_but_absent_from_store': len(movable) - len(present),
               'before': BEFORE, 'after': post_counts, 'fts_integrity': fts,
               'integrity_check': integrity, 'foreign_key_check': 'ok', 'original_rows_unchanged': originals,
               'goodfire_runs_unchanged': gf_check, 'remaining_referenced_cas_objects': len(remaining),
               'remaining_referenced_cas_objects_missing_from_store': len(missing_remaining),
               'scratch_copies_remaining': 'The worker scratch tree under /private/tmp/lb1-20261025/goodfire-wrench still holds unserved copies (raw payloads, mixed task Parquet, handoffs); they are not part of any served store.',
               'registry_reads': 0, 'docker_runs': 0, 'cloud_connections': 0, 'model_calls': 0}
    with (RECEIPTS / 'quarantine-terminal-wrench.json').open('x') as f:
        json.dump(receipt, f, indent=2, sort_keys=True); f.write('\n')
    print(json.dumps({k: receipt[k] for k in ('removed_rows', 'after', 'cas_objects_moved_not_deleted', 'cas_bytes_moved', 'cas_objects_shared_kept', 'integrity_check', 'remaining_referenced_cas_objects_missing_from_store')}, indent=2))


if __name__ == '__main__':
    main()
