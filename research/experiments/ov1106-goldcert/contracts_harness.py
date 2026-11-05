"""Safe, source-only public-contract measurements. No model or injection controls."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOT = Path('/private/tmp/ov1106/goldcert/helpers/contracts')
STATE = Path.home() / '.local/state/evallab-ov1106'
APP = 'ov1106-goldcert-contracts'


def packages():
    manifest = json.loads((REPO / 'research/experiments/ov1104-wave2-grader/smoke/manifest.json').read_text())
    return {r['task_id'][-6:]: Path(r['package']) for r in manifest['tasks']
            if r['task_id'][-6:] in {'000081', '000682', '001267', '002139', '003007'}}


def log(name, phase, estimate, receipt):
    STATE.mkdir(parents=True, exist_ok=True)
    record = dict(ts=datetime.now(UTC).isoformat(), lane='goldcert', what=f'{phase}:{name}',
                  provider='modal', est_usd=estimate, actual_usd=None, receipt=str(receipt))
    with (STATE / 'spend.jsonl').open('a') as handle:
        handle.write(json.dumps(record, sort_keys=True) + '\n')


def run(name, package, script):
    sys.path.insert(0, str(REPO / 'src'))
    from evallab.modal_cells import ReserveSettleSpendGate, SandboxMeter, modal_cell_runner
    os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
    gate = ReserveSettleSpendGate(
        approval='Peter 2026-11-04 15:30 ET: $20 infra budget (ov1106); goldcert contracts initial $2 slice',
        cap_usd=2.0, usd_per_execution=0.05,
        spend_ledger=STATE / 'goldcert-contracts-spend.json',
        modal_app_name=APP, billing_tag='ov1106')
    runner = modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=SandboxMeter(2.0),
                              timeout_seconds=300, results_dir=ROOT / 'cell-cache')
    cell = ROOT / 'cells' / name
    cell.mkdir(parents=True, exist_ok=True)
    (cell / 'agent-script.sh').write_text(script)
    # Log before admission, including cache attempts. A cache hit spends nothing.
    log(name, 'before', 0.05, cell / 'result.json')
    try:
        result = runner(cell, Path(package), script)
    except BaseException:
        log(name, 'after-error-reservation-retained', 0.05, cell)
        raise
    log(name, 'after-cache' if result.get('cache_hit') else 'after',
        0 if result.get('cache_hit') else result.get('est_usd'), cell / 'result.json')
    return result


def patch_script(patch, workdir):
    encoded = base64.b64encode(patch.encode()).decode()
    return (f'set -eu\ncd {workdir}\nprintf %s {encoded} | base64 -d > /tmp/public-source.patch\n'
            'git apply --whitespace=nowarn /tmp/public-source.patch\necho SOURCE_PATCH_APPLIED\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['probe', 'cell'])
    parser.add_argument('--task', required=True)
    parser.add_argument('--name')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--script', type=Path)
    args = parser.parse_args()
    package = args.package or packages()[args.task]
    workdir = tomllib.loads((package / 'task.toml').read_text())['environment']['workdir']
    if args.mode == 'probe':
        # Pristine public source only; exclude all test, solution, and VCS material.
        script = f'''set -eu
cd {workdir}
python - <<'PUBLIC_EXPORT'
import base64, io, os, tarfile
buf=io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as archive:
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ('.git', 'tests', 'test', 'testing', 'node_modules', '__pycache__', '.venv', 'venv', 'build_env')]
        for name in files:
            if name.endswith(('.py', '.js', '.html', '.json')) and not name.startswith(('test', 'mimo', 'conftest')):
                path=os.path.join(root,name)
                if os.path.isfile(path) and not os.path.islink(path):
                    archive.add(path, recursive=False)
print('PUBLIC_SOURCE_TAR ' + base64.b64encode(buf.getvalue()).decode())
PUBLIC_EXPORT
'''
    else:
        script = args.script.read_text()
    result = run(args.name or f'{args.task}-{args.mode}', package, script)
    print(json.dumps({k:v for k,v in result.items() if k not in {'agent_log'}}, sort_keys=True))

if __name__ == '__main__':
    main()
