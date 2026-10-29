#!/usr/bin/env python3
"""Build the OQ-7 DEV label population from local, read-only sources."""

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

DB = Path.home() / '.local/state/evallab-prod-live/cloud-trace-db.sqlite'
PLAN = Path.home() / 'Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun-v4/plan.json'
OUT = Path.home() / 'Developer/eval-lab-results/2026-10-29/oq7-atlas-guided-audit/A'


def main():
    registry = CorpusRegistry(ROOT)
    if not registry.has_assignment_authority():
        raise RuntimeError('Missing authoritative split assignments')
    conn = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    # Never fetch instruction or other body columns.
    tasks = {r[0]: r[1] for r in conn.execute("SELECT task_id,dataset_id FROM corpus_tasks WHERE lower(split)='dev'")}
    traced = {r[0] for r in conn.execute('SELECT DISTINCT task_id FROM agent_traces') if r[0] in tasks}
    faceted = {r[0] for r in conn.execute('SELECT DISTINCT task_id FROM run_facets') if r[0] in tasks}
    names = defaultdict(list)
    for task, dataset in tasks.items():
        names[(dataset, registry.task_name(task))].append(task)

    def resolve(dataset, task):
        matches = names.get((dataset, registry.task_name(task)), [])
        return matches[0] if len(matches) == 1 else None

    def excluded(dataset, task, item=None):
        return (registry.assigned_test_anywhere(item_id=item, task_id=task)
                or registry.assigned_split(dataset, item_id=item, task_id=task) == 'test')

    cases = [x for x in json.loads(PLAN.read_text())['cases'] if x.get('stage') == 'dev']
    mapped = defaultdict(list)
    case_counts = Counter()
    case_blocked = set()
    for case in cases:
        task = resolve(case['dataset_id'], case.get('task_id') or case['item_id'])
        if task is None:
            case_counts['unmapped_or_ambiguous'] += 1
        elif excluded(case['dataset_id'], task, case['item_id']):
            case_counts['mapped_excluded_test'] += 1
            case_blocked.add(task)
        else:
            mapped[task].append(case)
            case_counts['mapped_eligible'] += 1
    candidates = traced | set(mapped) | case_blocked
    blocked = {task for task in candidates if task in case_blocked or excluded(tasks[task], task)}
    exclusion_reasons = Counter()
    for task in blocked:
        if task in case_blocked:
            exclusion_reasons['cohort_item_test'] += 1
        elif registry.assigned_test_anywhere(task_id=task):
            exclusion_reasons['cross_key_test'] += 1
        else:
            exclusion_reasons['dataset_assignment_test'] += 1
    population = candidates - blocked
    # For candidate identities, check cohort item IDs too before opening any keys.
    labels = {task: {} for task in population}
    for task in sorted(population):
        for case in mapped.get(task, []):
            path = Path(case['keys_path'])
            for key in json.loads(path.read_text()):
                key_id = key['key_id']
                labels[task].setdefault(key_id, set()).add(str(path))

    # Assignment metadata is checked before parsing a DEV row's contents; TEST rows
    # are never parsed. Only task-level defect (fail) keys are positive labels.
    index_counts = Counter()
    for path in sorted((ROOT / 'research/answer-keys').glob('*/items.jsonl')):
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if not re.search(r'"split"\s*:\s*"dev"', line, re.I):
                    continue
                # Inspect identity metadata before parsing a potentially sealed row.
                key_match = re.search(r'"key_id"\s*:\s*("(?:\\.|[^"\\])*")', line)
                item_match = re.search(r'"item_id"\s*:\s*("(?:\\.|[^"\\])*")', line)
                subject_match = re.search(r'"subject"\s*:\s*(\{[^{}]*\})', line)
                if not (key_match and item_match and subject_match):
                    raise ValueError(f'Missing identity metadata in {path}')
                key_id = json.loads(key_match.group(1))
                item_id = json.loads(item_match.group(1))
                subject = json.loads(subject_match.group(1))
                source_task = subject.get('task_id')
                if not source_task:
                    continue
                if excluded(key_id, source_task, item_id):
                    index_counts['excluded_test'] += 1
                    continue
                row = json.loads(line)
                if str(row.get('split', '')).lower() != 'dev':
                    continue
                if subject.get('kind') != 'task' or row.get('expected_verdict') != 'fail':
                    continue
                assignment = registry.assigned_split(key_id, item_id=item_id, task_id=source_task)
                if registry.has_split_assignments(key_id) and assignment != 'dev':
                    index_counts['missing_dev_assignment'] += 1
                    continue
                # Exact identity wins; otherwise an unambiguous normalized name
                # across the population is allowed, never an ambiguous join.
                if source_task in population:
                    target = source_task
                else:
                    matches = [t for t in population if registry.task_name(t) == registry.task_name(source_task)]
                    target = matches[0] if len(matches) == 1 else None
                if target is None:
                    index_counts['unmapped_or_ambiguous'] += 1
                    continue
                labels[target].setdefault(item_id, set()).add(str(path.relative_to(ROOT)))
                index_counts['mapped'] += 1

    rows = []
    for task in sorted(population):
        keys = labels[task]
        rows.append({'task_id': task, 'dataset_id': tasks[task], 'split': 'dev',
                     'keyed': bool(keys), 'key_count': len(keys), 'key_ids': sorted(keys),
                     'key_sources': sorted({source for sources in keys.values() for source in sources})})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'labels.jsonl').write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
    by_dataset = defaultdict(lambda: Counter())
    for row in rows:
        counts = by_dataset[row['dataset_id']]
        counts['tasks'] += 1
        counts['keyed'] += row['keyed']
        counts['faceted'] += row['task_id'] in faceted
        counts['keyed_faceted'] += row['keyed'] and row['task_id'] in faceted
    dist = Counter(row['key_count'] for row in rows)
    report = ['# OQ-7 DEV labels', '',
        'Yield is the fraction of audited tasks carrying at least one DEV defect key: a lower bound on true bug yield. Unkeyed tasks are not certified negatives.', '',
        f"Run: `{sys.executable} {Path(__file__)}` at {datetime.now(UTC).isoformat()}. Cloud SQLite opened `mode=ro`, `query_only=ON`; no network or model calls.",
        f'Database: `{DB}`. Split ledger: `{registry.split_assignments_digest}`. Cohort: `{PLAN}`.', '',
        f'Traced DEV DB tasks before strict exclusion: {len(traced)}. Mapped eligible OQ-6 cohort tasks: {len(mapped)}. Union before final strict exclusion: {len(candidates)}.',
        f'Strict exclusions from union: {len(blocked)}; mutually exclusive reasons in priority order: {dict(sorted(exclusion_reasons.items()))}. Cohort case exclusions are counted separately below.',
        f'Final population: {len(rows)}; keyed: {sum(r["keyed"] for r in rows)}; at least one faceted run: {sum(r["task_id"] in faceted for r in rows)}; keyed and faceted: {sum(r["keyed"] and r["task_id"] in faceted for r in rows)}.',
        f'Key-count distribution (number of tasks): {dict(sorted(dist.items()))}.', '',
        '## OQ-6 DEV cohort (separate case counts)', '',
        f'Total cases: {len(cases)}; mapping outcomes: {dict(sorted(case_counts.items()))}; distinct mapped eligible tasks: {len(mapped)}; of these traced: {len(set(mapped) & traced)}, faceted: {len(set(mapped) & faceted)}.', '',
        '## Dataset breakdown', '', '| Dataset | Tasks | Keyed | Faceted | Keyed and faceted |', '|---|---:|---:|---:|---:|']
    for dataset, counts in sorted(by_dataset.items()):
        report.append(f'| {dataset} | {counts["tasks"]} | {counts["keyed"]} | {counts["faceted"]} | {counts["keyed_faceted"]} |')
    report += ['', '## Method and limitations', '',
        'Population is the union of DB DEV tasks with a trace and uniquely mapped, eligible OQ-6 DEV cases. Mapping uses dataset plus normalized task name; ambiguous and absent DB identities are not guessed. Registry `assigned_test_anywhere` and `assigned_split` reject TEST identities, including case item IDs; no TEST key bodies are parsed.',
        'Primary keys are admitted OQ-6 key IDs plus DEV task-level answer-index items with expected_verdict=fail. Pass, unknown and trace-level rows are not task defect labels. Index items require an explicit DEV assignment when their key has assignments, and the native cross-key guard. Exact task IDs are preferred; a unique normalized name across the population is used otherwise. Index item IDs and OQ-6 key IDs are deduplicated only when identical; distinct keys may describe the same underlying defect, so key counts are not independent defect counts. Package versions are not reconciled by name-only mapping.',
        f'Eligible defect index item mapping outcomes: {dict(sorted(index_counts.items()))}. No task_issues were used. Facet coverage means a task ID appears in run_facets, not necessarily that every trace is faceted.', '']
    (OUT / 'report.md').write_text('\n'.join(report))
    print('\n'.join(report))


if __name__ == '__main__':
    main()
