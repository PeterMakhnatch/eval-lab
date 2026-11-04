"""Prepare immutable source-only controls and execute a bounded safe matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from contracts_harness import REPO, ROOT, packages, patch_script, run

HERE = Path(__file__).resolve().parent
PRIOR = Path('/Users/petermakhnatch/Developer/eval-lab-results/2026-11-04/ov1104-grader/cert')
A2 = Path('/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks')
PYTHON = {'000081': '/root/.venv_xdoctest/bin/python', '000682': 'python3',
          '002139': 'python3', '003007': '/root/.pyenv/versions/3.9.21/bin/python'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(revision):
    sys.path.insert(0, str(REPO / 'src'))
    from evallab.registry import task_directory_digest
    manifest = {'created_at': datetime.now(UTC).isoformat(), 'revision': revision, 'tasks': {},
                'G3': 'unexercised', 'G4': 'unexercised', 'G7': 'pending_parent_blind_alternatives'}
    for tid, original in packages().items():
        workdir = tomllib.loads((original / 'task.toml').read_text())['environment']['workdir']
        reference = REPO / f'research/experiments/mimo-v6-full-fleet/references/format-code-task-{tid}.patch'
        controls = [{'name': 'reference', 'arm': 'reference', 'script': patch_script(reference.read_text(), workdir)},
                    {'name': 'nop', 'arm': 'nop', 'script': 'set -eu\nprintf "SAFE_NOP\\n"\n'}]
        if tid in {'000682', '002139'}:
            old = PRIOR / {'000682': '000682-r3', '002139': '002139-r2'}[tid]
            # Only the source-mutant arms are admitted. Never copy injection controls.
            for cell in json.loads((old / 'plan.json').read_text()):
                if cell['arm'] in {'train', 'eval'}:
                    controls.append({k: cell[k] for k in ('name', 'arm', 'script')})
        else:
            audit = (A2 / f'format-code-task-{tid}' / 'audit.json') if tid != '003007' else REPO / 'research/experiments/ov1104-grader/audit/003007/audit.json'
            mutants = json.loads(audit.read_text())['mutants']
            for index, mutant in enumerate(mutants):
                controls.append({'name': f'mutant-{index}', 'arm': 'train' if index == 0 else 'eval',
                    'mutant_sha256': mutant['sha256'],
                    'script': patch_script(reference.read_text(), workdir) + patch_script(mutant['diff'], workdir)})
        row = {'original_package': str(original), 'original_digest': task_directory_digest(original),
               'reference': str(reference), 'reference_sha256': sha(reference), 'workdir': workdir,
               'controls': controls}
        if tid == '001267':
            row.update(gold_package=None, gold_blocker='public browser harness missing; model-method-only checks would pin an unspecified internal interface',
                       contract_sha256=sha(HERE / 'contracts/001267.json'))
        else:
            target = ROOT / ('packages-' + revision) / tid
            if target.exists():
                raise RuntimeError(f'prepared package already exists: {target}')
            shutil.copytree(original, target)
            target.chmod(0o755)
            for copied in target.rglob('*'):
                copied.chmod(0o755 if copied.is_dir() else copied.stat().st_mode | 0o200)
            shutil.copyfile(HERE / f'contracts/{tid}.py', target / 'tests/public_contract.py')
            # A new standalone public grader. No original hidden assertion is treated as public truth.
            script = f'''#!/bin/bash
set -u
mkdir -p /logs/verifier
cd {workdir} || exit 2
{PYTHON[tid]} /tests/public_contract.py > /logs/verifier/test_output.log 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then printf '1\\n' > /logs/verifier/reward.txt; else printf '0\\n' > /logs/verifier/reward.txt; fi
exit "$RC"
'''
            (target / 'tests/test.sh').write_text(script)
            row.update(gold_package=str(target), gold_digest=task_directory_digest(target),
                       contract_sha256=sha(HERE / f'contracts/{tid}.py'))
        manifest['tasks'][tid] = row
    (ROOT / 'prepared.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (ROOT / f'prepared-{revision}.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Prepared source-only controls; not frozen until explicit freeze.')


def execute(tid, version, names):
    sys.path.insert(0, str(REPO / 'src'))
    from evallab.registry import task_directory_digest
    manifest = json.loads((ROOT / 'prepared.json').read_text())
    task = manifest['tasks'][tid]
    package = task[version + '_package']
    if package is None:
        print(json.dumps({'task': tid, 'version': version, 'status': 'unmeasured', 'reason': task['gold_blocker']}))
        return
    if task_directory_digest(Path(package)) != task[version + '_digest']:
        raise RuntimeError('Prepared package drift')
    for control in task['controls']:
        if names and control['name'] not in names:
            continue
        name = f'{tid}-{version}-{manifest.get("revision", "v1")}-{control["name"]}'
        result = run(name, Path(package), control['script'])
        print(json.dumps({'task': tid, 'version': version, 'control': control['name'],
            'arm': control['arm'], 'status': result.get('status'), 'reward': result.get('reward'),
            'receipt': str(ROOT / 'cells' / name / 'result.json')}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'execute', 'freeze'])
    parser.add_argument('--task', action='append')
    parser.add_argument('--version', default='gold', choices=['original', 'gold'])
    parser.add_argument('--control', action='append')
    parser.add_argument('--revision', default='v2')
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args.revision)
    elif args.mode == 'freeze':
        frozen = {str(p.relative_to(HERE)): sha(p) for p in sorted((HERE / 'contracts').iterdir()) if p.is_file()}
        for name in ('contract-grounding.json', 'contracts_harness.py', 'measure_contracts.py'):
            frozen[name] = sha(HERE / name)
        frozen['prepared_manifest_sha256'] = sha(ROOT / 'prepared.json')
        frozen['frozen_at'] = datetime.now(UTC).isoformat()
        (ROOT / 'contract-freeze.json').write_text(json.dumps(frozen, indent=2) + '\n')
        lines = ['# Frozen public contracts', '', 'No blind alternative output has been accessed.',
                 'G3/G4 remain unexercised. Task 001267 has no executable Gold scorer.',
                 'The SNS adapter scope remains the scope in contract-grounding.json.', '',
                 'Frozen at: ' + frozen['frozen_at'], '', '| File | SHA-256 |', '|---|---|']
        lines.extend('| ' + name + ' | `' + digest + '` |' for name, digest in frozen.items()
                     if name != 'frozen_at')
        (ROOT / 'FREEZE.md').write_text('\n'.join(lines) + '\n')
        print(json.dumps(frozen, indent=2))
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, tid, args.version, args.control) for tid in args.task]
            for future in futures:
                future.result()


if __name__ == '__main__':
    main()
