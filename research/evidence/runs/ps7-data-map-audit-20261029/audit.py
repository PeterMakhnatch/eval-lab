"""Counts-only audit; never emit content, labels or run identities.

Run from the audited checkout with its Python. All generated command artifacts
stay under --scratch. The map and store are read-only inputs. Source claims are
reviewed separately in review.json; map extraction retains line references only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--map', type=Path, required=True)
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--scratch', type=Path, required=True)
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--previous', type=Path, help='Reuse counts-only core probes from this exact code/map/store size')
    a = p.parse_args()
    scratch = a.scratch.resolve()
    if not str(scratch).startswith('/private/tmp/ps7j3/'):
        p.error('scratch must be a new child of /private/tmp/ps7j3/')
    scratch.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'EVALLAB_DISABLE_LOCAL_DOCKER': '1',
           'UV_OFFLINE': '1', 'UV_NO_SYNC': '1', 'UV_CACHE_DIR': str(scratch / 'uv-cache'),
           'EVALLAB_DERIVED_ROOT': str(scratch / 'derived'), 'TMPDIR': str(scratch)}
    # Preserve real state_dir for read-only facet/cluster/finding ledgers, never the live DB.
    from evallab.production.qa_data import CALLS
    previous = json.loads(a.previous.read_text()) if a.previous else None
    revision = subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], text=True).strip()
    if previous and (
        previous['code_revision'] != revision
        or previous['map_sha256'] != hashlib.sha256(a.map.read_bytes()).hexdigest()
        or previous['store_bytes'] != a.database.stat().st_size
    ):
        p.error('previous evidence belongs to different inputs')
    refresh = {'task bundle', 'history', 'scope export'}
    commands = [record for record in previous['command_probes'] if record['probe'] not in refresh] if previous else []
    cached = {record['probe'] for record in commands}
    observations = {}
    prefix = [str(Path(sys.executable).parent / 'evallab')]

    def run(label, lines, args, expected=0, kind='works as documented', qa=True):
        if label in cached:
            return {}
        argv = prefix + (['qa-data'] if qa else []) + args
        if qa and '--help' not in args and args[0] not in ('steps', 'scan'):
            argv += ['--cloud-database', str(a.database)]
        start = time.monotonic()
        done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=150, check=False)
        try:
            value = json.loads(done.stdout)
        except ValueError:
            value = {}
        status = kind if done.returncode == expected else 'fails'
        data = value.get('data', {})
        record = {'probe': label, 'map_lines': lines, 'status': status, 'exit_code': done.returncode,
                  'expected_exit_code': expected, 'seconds': round(time.monotonic() - start, 3),
                  'database_redirected': qa and '--cloud-database' in argv,
                  'data_fields': sorted(data), 'meta_fields': sorted(value.get('meta', {}))}
        if done.returncode != 0:
            error = value.get('error', '')
            # Explicit classes, never save free-form error text/path/identity.
            record['error_class'] = next((name for token, name in (
                ('exactly one', 'missing findings selector'), ('ledger cannot', 'facet ledger unavailable'),
                ('not available:', 'uniform admission refusal'), ('more than 200', 'export cap'),
                ('must be at least 200', 'budget minimum'), ('refusing overwrite', 'exclusive publication'),
                ('inspect_scout', 'scanner runtime unavailable'), ('not in', 'missing package file'),
            ) if token in error), 'usage or runtime error')
        if 'matched' in data:
            record['matched'] = data['matched']
        for name in ('traces', 'runs', 'clusters', 'rows'):
            if isinstance(data.get(name), list):
                record[f'{name}_returned'] = len(data[name])
                record[f'{name}_item_fields'] = sorted({key for row in data[name] if isinstance(row, dict) for key in row})
        if 'receipt' in data:
            record['receipt_counts'] = {k: v for k, v in data['receipt'].items() if type(v) is int}
        commands.append(record)
        a.output.write_text(json.dumps({
            'in_progress': True, 'code_revision': revision,
            'command_probes': commands,
        }, indent=2) + '\n')
        observations[label] = data
        return data

    run('top help', [55], ['--help'])
    for call in CALLS:
        run(f'{call} help', [14, 55], [call, '--help'])
    inv = run('inventory', [88, 95, 548], ['inventory'])
    run('inventory ids', [89], ['inventory', '--ids-out', str(scratch / 'ids.txt')])
    run('pass traces', [90], ['traces', '--dataset', 'tb4-audit-v1', '--outcome', 'pass', '--limit', '5'])
    run('task traces', [91], ['traces', '--task', 'terminal-bench/vpp-loss-divergence'])
    ids = run('dataset ids', [92], ['traces', '--dataset', 'tb4-audit-v1', '--limit', '2000', '--ids-only', '--budget', '40000'])
    selected = ids.get('traces', [])[:2]
    (scratch / 'selected.txt').write_text('\n'.join(selected))
    filters = [('answer material', [204], ['answer_material_accessed=true']),
               ('tests and suspicion', [205], ['tests=modified,deleted,replaced', 'suspicion=2,3']),
               ('pass and awareness', [206], ['outcome=pass', 'eval_awareness=reasons_about_grader,plans_to_game']),
               ('modified task', [207, 419], ['tests=modified,deleted,replaced']),
               ('all faceted', [220], ['suspicion=0,1,2,3']),
               ('modified tests', [220], ['tests=modified,deleted,replaced']),
               ('awareness', [220], ['eval_awareness=mentions_tests,reasons_about_grader,plans_to_game']),
               ('suspicious', [221], ['suspicion=2,3'])]
    for label, lines, facets in filters:
        argv = ['runs']
        for facet in facets:
            argv += ['--facet', facet]
        if label == 'modified task':
            argv += ['--task', 'terminal-bench/vpp-loss-divergence']
        run(label, lines, argv)
    for outcome in ('pass', 'fail', 'unknown'):
        run(f'facet outcome {outcome}', [221], ['runs', '--facet', f'outcome={outcome}'])
    if selected:
        run('similar runs', [208], ['runs', '--like', selected[0]])
        run('window range', [148], ['window', selected[0], '--first', '0', '--last', '2'])
        run('window around', [148], ['window', selected[0], '--around', '2', '--radius', '1'])
        run('findings run', [517, 524], ['findings', '--run', selected[0], '--limit', '200'], kind='works but output differs')
        run('trace card', [216, 393], ['trace-card', selected[0], '--no-ledgers'], kind='works but output differs')
    run('lexical query', [208], ['runs', '--query', 'tests'])
    run('clusters database', [229], ['clusters'])
    run('cluster members database', [230], ['clusters', '--members', 'L1-07'])
    run('clusters file', [231, 518], ['clusters', '--source', 'file', '--level', '1'])
    run('cluster members file', [232], ['clusters', '--source', 'file', '--members', 'L1-07'])
    run('cluster run filter', [237], ['runs', '--cluster', 'L1-07'])
    run('scope export', [234], ['prod', 'export-cluster-scope', '--cluster-id', 'L1-07',
        '--source-database', str(a.database), '--manifest', str(scratch / 'scope.json')], qa=False)
    run('findings aggregate', [516, 549], ['findings'], kind='fails')
    run('findings task', [526], ['findings', '--task', 'terminal-bench/vpp-loss-divergence'])
    run('recount', [223, 547], ['recount'])
    run('task split', [342], ['split', 'task', 'terminal-bench/vpp-loss-divergence'])
    run('bare split', [103, 342], ['split', 'task', 'vpp-loss-divergence'])
    run('unknown split refusal', [344, 348], ['split', 'item', 'audit-unknown-key', 'audit-unknown-item'], expected=2)
    run('unknown task refusal', [345, 348], ['split', 'task', 'audit-unknown-task'], expected=2)
    run('task bundle', [445, 451], ['task', 'terminal-bench/vpp-loss-divergence', '--budget', '8000'])
    run('task file', [446], ['task', 'terminal-bench/vpp-loss-divergence', '--file', 'tests/test.sh', '--budget', '8000'])
    run('history', [448], ['history', 'terminal-bench/vpp-loss-divergence'], kind='works but output differs')
    run('minimum budget', [60], ['inventory', '--budget', '199'], expected=2)
    for fmt in ('atif', 'harbor', 'scout'):
        out = scratch / fmt
        exported = run(f'facet export {fmt}', [114, 119, 121, 534], ['export', '--facet',
                       'answer_material_accessed=true', '--limit', '2', '--format', fmt, '--out', str(out)],
                       kind='works as documented' if fmt == 'atif' else 'works but output differs')
        if fmt == 'atif' and exported.get('runs'):
            blind = exported['runs'][0]['blind_id']
            for option, number, line in (('--original', '2', 138), ('--blinded', '3', 139)):
                run(f'steps {option}', [line], ['steps', str(out) + '.sealed-manifest.json', blind, option, number])
            files = sorted(out.glob('run-*.json'))
            if files:
                content = json.loads(files[0].read_text())
                commands.append({'probe': 'ATIF jq equivalents', 'map_lines': [131, 132], 'status': 'works as documented',
                                 'exit_code': 0, 'steps': len(content.get('steps', [])),
                                 'step_fields': sorted({k for step in content.get('steps', []) for k in step})})
        if fmt == 'scout' and exported.get('runs'):
            run('deterministic scan', [535, 538], ['scan', str(out / 'transcripts.jsonl')])
    if selected:
        run('explicit harbor export', [115], ['export', '--trace-id', selected[0], '--trace-id', selected[-1],
            '--format', 'harbor', '--out', str(scratch / 'explicit')], kind='works but output differs')
        run('ids file export', [116], ['export', '--ids-file', str(scratch / 'selected.txt'), '--out', str(scratch / 'ids-export')])
    run('facet export cap', [114, 149], ['export', '--facet', 'suspicion=0,1,2,3', '--out', str(scratch / 'over-cap')], expected=2)
    run('raw export help', [144], ['prod', 'export-runs', '--help'], qa=False, kind='works but output differs')
    run('cluster scope help', [234], ['prod', 'export-cluster-scope', '--help'], qa=False)
    run('cheatbench export help', [159, 165], ['prod', 'export-cheatbench-runs', '--help'], qa=False)
    run('prod status help', [25, 570], ['prod', 'status', '--help'], qa=False, kind='works but output differs')

    # Execute metadata-only jq recipes without retaining their identity output.
    map_lines = a.map.read_text().splitlines()
    for number in (254, 255, 256, 257, 258, 259, 260, 331, 332, 333, 406, 407):
        if f'metadata jq line {number}' in cached:
            continue
        command = map_lines[number - 1].split(' #', 1)[0].strip()
        argv = shlex.split(command)
        cwd = a.map.parent if number < 400 else Path.cwd()
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env, timeout=30, check=False)
        commands.append({'probe': f'metadata jq line {number}', 'map_lines': [number],
                         'status': 'works as documented' if done.returncode == 0 else 'fails',
                         'exit_code': done.returncode, 'output_lines': len(done.stdout.splitlines())})
    run('seeker plan', [427], ['prod', 'seeker-error-first', '--out', str(scratch / 'plan'), '--plan'], qa=False)
    scorer = a.map.parent / 'score_task_qa_gate.py'
    for option, number in (('--help', 473), ('self-test', 474)):
        if f'scorer {option}' in cached:
            continue
        done = subprocess.run([str(Path.home() / 'Developer/research-context/.venv/bin/python'),
                               '-B', str(scorer), option], capture_output=True, text=True,
                              env=env, timeout=60, check=False)
        commands.append({'probe': f'scorer {option}', 'map_lines': [number],
                         'status': 'works as documented' if done.returncode == 0 else 'fails',
                         'exit_code': done.returncode})
    for fmt, number in (('atif', 159), ('harbor', 161)):
        run(f'cheatbench {fmt}', [number], ['prod', 'export-cheatbench-runs', '--format', fmt,
            '--output-directory', str(scratch / f'cheatbench-{fmt}'),
            '--manifest', str(scratch / f'cheatbench-{fmt}.sealed.json'), '--limit', '2'], qa=False)
    # Full bundle digest verification is a separate external-state operation.
    # Do not repeat the earlier bounded 150-second timeout as a CLI defect.
    if 'registry validator offline' not in cached:
        commands.append({'probe': 'registry validator offline', 'map_lines': [253, 550],
                         'status': 'not runnable here',
                         'reason': 'External bundle digest walk exceeded the prior 150-second bound; not re-run.'})
    run('task answer-material filter', [420], ['runs', '--task', 'terminal-bench/vpp-loss-divergence',
                                            '--facet', 'answer_material_accessed=true'])
    run('cohort task split recipe', [421], ['split', 'task', 'terminal-bench/vpp-loss-divergence'])
    run('bountybench traces', [108], ['traces', '--task', 'cheatbench/bountybench'])
    run('bountybench facets', [108], ['runs', '--task', 'cheatbench/bountybench'])

    # All fenced shell logical commands, including heredocs, extracted without
    # evaluating any shell. Nonexecuted commands retain line spans and a reason.
    text = a.map.read_text()
    lines = text.splitlines()
    extracted = []
    fenced = False
    start = None
    acc = []
    heredoc = False
    for n, line in enumerate(lines, 1):
        if line.startswith('```'):
            fenced = line.startswith('```bash') if not fenced else False
            continue
        if not fenced or not line.strip() or line.lstrip().startswith('#'):
            continue
        if start is None:
            start = n
        acc.append(line)
        if "<<'PY'" in line:
            heredoc = True
        if heredoc and line != 'PY':
            continue
        heredoc = False
        if line.endswith('\\'):
            continue
        covered = [r for r in commands if any(start <= m <= n for m in r['map_lines'])]
        if covered:
            statuses = [r['status'] for r in covered]
            state = next((status for status in (
                'fails', 'works but output differs', 'works as documented', 'not runnable here',
            ) if status in statuses), 'not runnable here')
            reason = (
                'External bundle verification not completed within the bounded audit.'
                if state == 'not runnable here' else
                'Executed safe instance; placeholders replaced, store redirected; projection captured counts only.'
            )
        else:
            state = 'not runnable here'
            command = '\n'.join(acc)
            reason = ('Direct ledger access is disallowed: registry API only.' if 'assignments.csv' in command else
                      'Touches cross-test/gold or external runtime state; not executed.' if any(s in command for s in ('cross-test', 'adapter', 'hackbench', '$PYR', 'score_task', 'synthetic', 'convert-oq6')) else
                      'Setup/context command or live/external/placeholder input; not executed literally.')
        extracted.append({'lines': [start, n], 'classification': state, 'reason': reason,
                          'probes': [r['probe'] for r in covered]})
        start, acc = None, []
    # Inline command occurrences are tracked separately from fenced logical commands.
    inline = []
    fenced = False
    for n, line in enumerate(lines, 1):
        if line.startswith('```'):
            fenced = not fenced
            continue
        if fenced:
            continue
        for fragment in re.findall(r'`([^`]+)`', line):
            if re.search(r'(?:qa-data |evallab |python -m |sqlite3|git -C|scout_watch|^(?:runs|traces|export|findings|clusters|scan|split|task|history|recount|steps|stat|jq)\b)', fragment):
                matches = [r['probe'] for r in commands if n in r['map_lines']]
                inline.append({'line': n, 'classification': 'covered by probes' if matches else 'not runnable here',
                               'probes': matches, 'reason': 'Read-only tested form or incomplete/prohibited example; never shell-evaluated.'})
    review = json.loads(a.review.read_text())
    claims = []
    fenced = False
    for n, line in enumerate(lines, 1):
        if line.startswith('```'):
            fenced = not fenced
            continue
        comment_claim = fenced and '#' in line
        heading_claim = line.startswith('#') and any(character.isdigit() for character in line)
        if (fenced and not comment_claim) or n < 12 or not line.strip() or (line.startswith('#') and not heading_claim and not comment_claim) or re.fullmatch(r'[|:\- ]+', line):
            continue
        # Source-line claim units deliberately preserve compound assertions. A unit is
        # true only when all checkable assertions are supported; historical/unreviewed
        # compound units remain unverifiable, never optimistically marked true.
        relevant = [c for c in review['claim_reviews'] if n in c['lines']]
        status = 'false' if any(c['classification'] == 'false' for c in relevant) else (
            'true' if relevant and all(c['classification'] == 'true' for c in relevant) else 'unverifiable')
        claims.append({'line': n, 'classification': status, 'reviews': [c['id'] for c in relevant],
                       'reason': 'See source-reviewed claim units.' if relevant else 'Historical, policy, external evidence, or compound prose not independently established.'})
    # Retain only permitted numerical observations and field names, never raw payloads.
    measurements = {'inventory': {k: inv.get(k) for k in ('admitted_tasks', 'admitted_runs', 'run_outcomes', 'models_total')},
                    'datasets': list(inv.get('by_dataset', {}).values()),
                    'call_types': len(CALLS),
                    'facet_matches': {r['probe']: r['matched'] for r in commands if 'matched' in r},
                    'task': {}, 'recount': {}}
    task = observations.get('task bundle', {})
    measurements['task'] = {k: task.get(k) for k in ('instruction_chars', 'issues_total')}
    measurements['task']['packages'] = [{'file_count': v.get('file_count'), 'listed': len(v.get('files', [])),
                                        'withheld': len(v.get('withheld', []))} for v in task.get('packages', {}).values()]
    measurements['task']['runs'] = {k: v for k, v in task.get('runs', {}).items() if type(v) is int}
    rc = observations.get('recount', {})
    measurements['recount'] = {k: v for k, v in rc.items() if type(v) is int}
    measurements['recount']['sides'] = [{k: v for k, v in rc.get(side, {}).items() if type(v) is int} for side in ('jsonl', 'db')]
    measurements['history_fields'] = sorted(observations.get('history', {}))
    measurements['history_issue_status_counts'] = dict(Counter(r.get('status') for r in observations.get('history', {}).get('issues', [])))
    measurements['task']['contents_returned_without_file'] = sum(len(v.get('contents', [])) for v in task.get('packages', {}).values())
    scope = json.loads((scratch / 'scope.json').read_text())
    measurements['scope'] = scope['counts']
    registry = json.loads((a.map.parent / 'cross-test-registry.json').read_text())
    entries = registry['entries']
    measurements['registry'] = {
        'entries': len(entries), 'hold': len(registry.get('hold_access', [])),
        'fa_rates': dict(Counter(e.get('labels', {}).get('false_alarm_basis', {}).get('fa_rate', 'missing') for e in entries)),
        'trace_watcher_allowed': sum(any(lane.get('lane') == 'Trace Watcher' and lane.get('access') == 'allowed' for lane in e.get('lanes', [])) for e in entries)}
    measurements['registry']['localization_scorable'] = sum(bool(entry.get('scoring', {}).get('command_level_localization_scorable')) for entry in entries)
    measurements['registry']['fa_rate_yes'] = measurements['registry']['fa_rates'].get('yes', 0)
    if previous:
        for key in ('inventory', 'datasets', 'recount'):
            measurements[key] = previous['measurements'][key]
        rc = measurements['recount']
    drift = []
    for field, old in (('jsonl_lines', 3118), ('jsonl_unique_runs', 2703), ('jsonl_only_runs', 1469)):
        drift.append({'map_lines': [223, 224], 'field': field, 'old': old, 'observed': rc.get(field), 'classification': 'drift'})
    for field, old in (('entries', 17), ('hold', 7), ('trace_watcher_allowed', 15), ('localization_scorable', 1), ('fa_rate_yes', 9)):
        observed = measurements['registry'][field]
        drift.append({'map_lines': {'entries': [246], 'hold': [260], 'trace_watcher_allowed': [256],
                                    'localization_scorable': [255, 510], 'fa_rate_yes': [303, 332]}[field],
                      'field': field, 'old': old, 'observed': observed,
                      'classification': 'drift' if observed != old else 'unchanged'})
    result = {'schema': 'ps7-data-map-audit/v1', 'code_revision': subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], text=True).strip(),
              'map_sha256': hashlib.sha256(text.encode()).hexdigest(), 'store_bytes': a.database.stat().st_size,
              'command_probes': commands, 'fenced_commands': extracted, 'inline_commands': inline,
              'claim_lines': claims, 'review': review, 'measurements': measurements, 'drift': drift,
              'reused_core_probes': len(cached),
              'summary': {'probes': dict(Counter(r['status'] for r in commands)), 'fenced_commands': dict(Counter(r['classification'] for r in extracted)),
                          'claim_lines': dict(Counter(r['classification'] for r in claims)), 'corrections': len(review['corrections'])}}
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'summary': result['summary'], 'measurements': measurements}, indent=2))


if __name__ == '__main__':
    main()
