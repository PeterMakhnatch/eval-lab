"""Measure the isolated watcher regression in alternating home environments."""
import json
import os
import resource
import statistics
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[4]
command = [str(root / '.venv/bin/python'), '-m', 'pytest', '-o', 'addopts=', '-q',
           '-p', 'no:cacheprovider', 'tests/test_run_atlas_watch.py',
           '-k', 'backfill_usage_cannot_starve']
results = {'real_home': [], 'clean_home': []}
with tempfile.TemporaryDirectory(dir='/private/tmp/ps5b') as home:
    for _ in range(3):
        for label in results:
            env = dict(os.environ) if label == 'real_home' else {
                'HOME': home, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
            env['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            results[label].append(round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 6))
print(json.dumps({'n': 3, 'order': 'real_home, clean_home interleaved',
                  'cpu_seconds': results,
                  'median_cpu_seconds': {k: statistics.median(v) for k, v in results.items()}}, indent=2))
