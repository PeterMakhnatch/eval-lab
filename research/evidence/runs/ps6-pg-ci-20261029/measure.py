"""Compare SIGKILL recovery using origin/main and the current fixture, without external data."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

repo = Path.cwd().resolve()
python = repo / '.venv/bin/python'
pg_ctl = shutil.which('pg_ctl')
if not pg_ctl:
    raise SystemExit('pg_ctl must be on PATH')
scratch = Path(tempfile.mkdtemp(prefix='ps6-pg-measure-', dir='/private/tmp/ps5'))
(scratch / 'hold_fixture.py').write_text('import os, pathlib, time\ndef pytest_runtest_call(item):\n    pathlib.Path(os.environ["TMPDIR"], "fixture-ready").touch()\n    time.sleep(120)\n')
base = scratch / 'test_cloud_trace_db.py'
base.write_bytes(subprocess.check_output(['/usr/bin/git', 'show', 'origin/main:tests/test_cloud_trace_db.py'], cwd=repo))
results = {}
for label, testfile in [('baseline', base), ('changed', repo / 'tests/test_cloud_trace_db.py')]:
    tmp = scratch / label
    tmp.mkdir()
    env = dict(os.environ, TMPDIR=str(tmp), PYTHONPATH=f'{scratch}:{repo / "src"}', EVALLAB_DISABLE_LOCAL_DOCKER='1')
    node = str(testfile) + '::test_metadata_update_preserves_steps_and_count[postgres]'
    cmd = [str(python), '-m', 'pytest', '-o', 'addopts=', '-q', '--no-header', '-p', 'no:cacheprovider', node]
    child = subprocess.Popen(cmd + ['-p', 'hold_fixture'], cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    datadir = None
    try:
        for _ in range(600):
            files = list(tmp.glob('cloud-trace-pg-*/data/postmaster.pid'))
            if files and (tmp / 'fixture-ready').exists():
                datadir = files[0].parent
                break
            if child.poll() is not None:
                raise RuntimeError(f'{label} exited before PostgreSQL startup')
            time.sleep(0.1)
        if datadir is None:
            raise RuntimeError(f'{label} timed out before PostgreSQL startup')
        pid = int((datadir / 'postmaster.pid').read_text().splitlines()[0])
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
        run = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        results[label] = {'pytest_exit': run.returncode, 'old_postmaster_alive': alive,
                          'old_directory_exists': datadir.parent.exists()}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if datadir is not None and (datadir / 'postmaster.pid').exists():
            subprocess.run([pg_ctl, '-D', str(datadir), '-m', 'fast', '-w', 'stop'], check=True, capture_output=True)
        shutil.rmtree(tmp)
print(json.dumps(results, indent=2))
shutil.rmtree(scratch)
if results['baseline']['old_postmaster_alive'] is not True or results['changed'] != {
    'pytest_exit': 0, 'old_postmaster_alive': False, 'old_directory_exists': False,
}:
    raise SystemExit('recovery comparison failed')
