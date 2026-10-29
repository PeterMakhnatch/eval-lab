#!/usr/bin/env python3
"""OQ-8 read-only QA-data gateway. Operator-supplied target and paths are trusted.

Example: python qa_proxy.py --target-task TASK --target-item ITEM --counter CASE.count
    --log CASE.jsonl -- task TASK --file tests/test.sh
The counter and log must be outside the auditor's writable environment. Token counts
use qa-data's compact-JSON character/4 estimate, not a model tokenizer.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
PYTHON = Path('/Users/petermakhnatch/Developer/eval-lab/.worktrees/oq6-tq-v3-runtime-20261027/.venv/bin/python')
ALLOWED = frozenset({'task', 'runs', 'window', 'history', 'findings', 'trace-card', 'clusters', 'inventory'})
UNAVAILABLE = 'not available: not an admitted DEV task or run'
REFUSAL = {'ok': False, 'error': UNAVAILABLE}
# Never let clients select output files, alternate stores, repaired packages, or labels.
PATH_MARKER = re.compile(r'(?i)(?:answer[-_ /]*keys?|scor(?:er|ing)[-_ /]*keys?|ground[-_ /]*truth|research/answer-keys)')
OPTIONS = {
    'task': {'--file'},
    'runs': {'--facet', '--cluster', '--like', '--query', '--task', '--limit'},
    'window': {'--first', '--last', '--around', '--radius'},
    'history': {'--flag', '--limit'},
    'findings': {'--run', '--task', '--limit'},
    'trace-card': {'--no-ledgers'},
    'clusters': {'--members', '--level'},
    'inventory': set(),
}


def tokens(value):
    return math.ceil(len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))) / 4)


def clean_paths(value):
    if isinstance(value, str):
        return '[withheld]' if PATH_MARKER.search(value) else value
    if isinstance(value, list):
        return [clean_paths(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_paths(v) for k, v in value.items() if not PATH_MARKER.search(k) and k != 'withheld'}
    return value


def same_target(registry, target_task, target_item, task=None, item=None):
    names = {registry.task_name(target_task)}
    ids = {target_item.casefold()}
    for row in registry.items():
        if row.item_id.casefold() == target_item.casefold() or (row.task_id and registry.task_name(row.task_id) in names):
            ids.add(row.item_id.casefold())
            if row.task_id:
                names.add(registry.task_name(row.task_id))
    return bool((task and registry.task_name(task) in names) or (item and item.casefold() in ids))


def mask(result, command, registry, target_task, target_item):
    if not result.get('ok') or not isinstance(result.get('data'), dict):
        return REFUSAL
    data = result['data']
    if command == 'task' and same_target(registry, target_task, target_item, task=data.get('task_id')):
        data = {k: data[k] for k in ('task_id', 'instruction', 'instruction_chars', 'instruction_truncated', 'packages', 'runs', 'truncated') if k in data}
        data['packages'] = {k: v for k, v in data.get('packages', {}).items() if k == '0:original_package'}
        runs = data.get('runs', {})
        data['runs'] = {k: runs[k] for k in ('total', 'list', 'omitted') if k in runs}
        data['runs']['list'] = [{k: row[k] for k in ('trace_id', 'step_count', 'outcome') if k in row} for row in runs.get('list', [])]
    if command == 'history':
        # Similar rows can be another version or dataset alias of the target.
        data['similar_issues'] = [r for r in data.get('similar_issues', []) if not same_target(registry, target_task, target_item, task=r.get('task_id'), item=r.get('item_id'))]
    if command == 'runs':
        safe = []
        for row in data.get('runs', []):
            if same_target(registry, target_task, target_item, task=row.get('task_id'), item=row.get('run_id')):
                row = {k: row[k] for k in ('run_id', 'task_id', 'outcome') if k in row}
            safe.append(row)
        data['runs'] = safe
    return clean_paths({'ok': True, 'data': data, 'meta': result.get('meta', {})})


def invoke(args, argv):
    from evallab.production.qa_data import open_context
    if not argv or argv[0] not in ALLOWED or any(
        a.startswith('-') and a.split('=', 1)[0] not in OPTIONS[argv[0]]
        for a in argv[1:]
    ):
        return REFUSAL
    command = argv[0]
    # Preflight must see every positional identity, even with reordered options.
    if command in ('task', 'history', 'window', 'trace-card') and (
        len(argv) < 2 or argv[1].startswith('-')
    ):
        return REFUSAL
    ctx = open_context(args.database, repo_root=ROOT)
    try:
        registry = ctx.retriever.registry
        if registry.assigned_test_anywhere(task_id=args.target_task, item_id=args.target_item) or registry.sealed_identity(task_id=args.target_task, item_id=args.target_item):
            return REFUSAL
        # Resolve every explicit identity before invoking the CLI; it enforces DEV admission.
        task = None
        run = None
        if command in ('task', 'history') and len(argv) > 1 and not argv[1].startswith('-'):
            task = ctx.require_task(argv[1])['task_id']
        elif command in ('window', 'trace-card') and len(argv) > 1 and not argv[1].startswith('-'):
            run = argv[1]
        for option in ('--task', '--run', '--like'):
            for i, value in enumerate(argv[1:], 1):
                if value == option and i + 1 < len(argv):
                    selected = argv[i + 1]
                elif value.startswith(option + '='):
                    selected = value.split('=', 1)[1]
                else:
                    continue
                if option == '--task':
                    task = ctx.require_task(selected)['task_id']
                else:
                    run = selected
                if option != '--task':
                    resolved = ctx.task_of_run(selected)
                    if registry.assigned_test_anywhere(task_id=resolved, item_id=selected):
                        return REFUSAL
                    if command == 'findings' and same_target(registry, args.target_task, args.target_item, task=resolved, item=selected):
                        return REFUSAL
        if run:
            run_task = ctx.task_of_run(run)
            if registry.assigned_test_anywhere(task_id=run_task, item_id=run):
                return REFUSAL
            if command in ('window', 'trace-card', 'findings') and same_target(registry, args.target_task, args.target_item, task=run_task, item=run):
                return REFUSAL
        if task and (registry.assigned_test_anywhere(task_id=task) or (command in ('history', 'findings') and same_target(registry, args.target_task, args.target_item, task=task))):
            return REFUSAL
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), EVALLAB_DISABLE_LOCAL_DOCKER='1')
        cmd = [str(PYTHON), '-c', 'from evallab.entry import main; main()', 'qa-data', *argv, '--cloud-database', args.database, '--budget', str(args.token_cap)]
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        if proc.returncode:
            return REFUSAL
        return mask(json.loads(proc.stdout), command, registry, args.target_task, args.target_item)
    finally:
        ctx.database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-task', required=True)
    parser.add_argument('--target-item', required=True)
    parser.add_argument('--counter', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--database', default=str(Path.home() / '.local/state/evallab-prod-live/cloud-trace-db.sqlite'))
    parser.add_argument('--token-cap', type=int, default=4000)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    started = time.monotonic()
    result = REFUSAL
    try:
        if args.token_cap < 200:
            raise ValueError('invalid cap')
        args.counter.parent.mkdir(parents=True, exist_ok=True)
        with args.counter.open('a+', encoding='utf-8') as counter:
            fcntl.flock(counter, fcntl.LOCK_EX)
            counter.seek(0)
            text = counter.read().strip()
            count = int(text) if text else 0
            if count < 8:
                counter.seek(0)
                counter.truncate()
                counter.write(str(count + 1))
                counter.flush()
                os.fsync(counter.fileno())
                permitted = True
            else:
                permitted = False
        if permitted:
            result = invoke(args, argv)
        if tokens(result) > args.token_cap:
            result = REFUSAL
    except Exception:
        result = REFUSAL
    entry = {'args': argv, 'return_tokens': tokens(result), 'wall_seconds': round(time.monotonic() - started, 6), 'ok': result.get('ok', False)}
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('a', encoding='utf-8') as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    sys.exit(main())
