"""Compare real CLI replies with meta removed; retain only aggregate results.

Usage: python cli_identity.py NEW_ROOT OLD_ROOT RESULT_JSON
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

new, old, result = map(Path, sys.argv[1:])
python = new / '.venv/bin/python'
store = '/private/tmp/ps2/store.sqlite'
code = 'import sys; from evallab.entry import main; sys.exit(main(sys.argv[1:]))'
out = Path('/private/tmp/ps5/h-export')
manifest = out.with_name(out.name + '.sealed-manifest.json')

def run(root, args):
    env = {**os.environ, 'PYTHONPATH': str(root / 'src'), 'EVALLAB_DISABLE_LOCAL_DOCKER': '1', 'PYTHONWARNINGS': 'ignore'}
    p = subprocess.run([str(python), '-c', code, 'qa-data', *args, '--cloud-database', store], cwd=root, env=env, capture_output=True)
    try:
        doc = json.loads(p.stdout)
        doc.pop('meta', None)
        reply = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()
    except (ValueError, AttributeError):
        reply = p.stdout
    return p.returncode, reply

status, raw = run(new, ['runs', '--facet', 'suspicion=3', '--limit', '6'])
assert status == 0
items = json.loads(raw)['data']['runs']
run_id = items[0]['run_id']
task_id = items[0]['task_id']
calls = [
    ['traces', '--sort', 'alarms', '--limit', '100', '--ids-only'],
    ['traces', '--outcome', 'fail', '--limit', '100'],
    ['traces', '--limit', '200'], ['traces', '--limit', '50', '--budget', '8000'],
    ['traces', '--task', 'cad-model'],
    ['task', 'terminal-bench/cad-model', '--budget', '8000'],
    ['task', 'terminal-bench/layout-config-recreation2', '--budget', '8000'],
    ['clusters'], ['runs', '--like', run_id, '--limit', '10'],
    ['runs', '--facet', 'answer_material_accessed=true'], ['runs', '--limit', '20'],
    ['runs', '--facet', 'suspicion=3'], ['recount'], ['inventory'], ['history', task_id],
    ['trace-card', run_id], ['trace-card', 'retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4'],
    ['traces', '--limit', '5', '--dataset', 'terminal-bench'],
    ['export', '--trace-id', run_id, '--out', str(out)],
]
assert not out.exists() and not manifest.exists()
results = []
for call in calls:
    a = run(old, call)
    if call[0] == 'export':
        if out.exists():
            shutil.rmtree(out)
        manifest.unlink(missing_ok=True)
    b = run(new, call)
    if call[0] == 'export':
        if out.exists():
            shutil.rmtree(out)
        manifest.unlink(missing_ok=True)
    results.append({'command': call[0], 'identical': a == b, 'old_exit': a[0], 'new_exit': b[0]})
result.write_text(json.dumps({'cases': results, 'identical': sum(x['identical'] for x in results), 'total': len(results)}, indent=2) + '\n')
assert all(x['identical'] for x in results), results
assert results[-1]['old_exit'] == 0, results[-1]
