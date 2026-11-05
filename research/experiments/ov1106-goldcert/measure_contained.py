"""Measure frozen public contracts through the concrete native Modal boundary.

No model calls or host-side task execution. Integrity exploit arms are not run.
Run each mode with uv run --no-sync python; every cell uses fresh evidence.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PRIVATE = Path('/private/tmp/ov1106/goldcert')
ROOT = PRIVATE / 'helpers/safe/measurement'
CONTRACTS = PRIVATE / 'helpers/contracts'
STATE = Path.home() / '.local/state/evallab-ov1106'
LEDGER = STATE / 'goldcert-contained-spend.json'
APP = 'ov1106-goldcert-contained'
CAP = 3.0
UNIT = 0.05
TASKS = ('000081', '000682', '001267', '002139', '003007')
PYTHON = {'000081': '/root/.venv_xdoctest/bin/python', '000682': 'python3',
          '002139': 'python3', '003007': '/root/.pyenv/versions/3.9.21/bin/python'}
sys.path.insert(0, str(REPO / 'src'))
from evallab.full_hardening import harden
from evallab.gold_certifier import ContainedModalRunner, wilson_interval
from evallab.modal_cells import SandboxMeter
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate
from contracts_harness import patch_script


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked(path, expected=None):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('nonregular input: ' + str(path))
    data = path.read_bytes()
    if expected is not None and sha(data) != expected:
        raise ValueError('input drift: ' + str(path))
    return data


def load(path):
    return json.loads(checked(path))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def frozen():
    freeze = load(CONTRACTS / 'contract-freeze.json')
    for name, expected in freeze.items():
        if name != 'frozen_at':
            checked(CONTRACTS / 'prepared.json' if name == 'prepared_manifest_sha256' else HERE / name, expected)
    prepared = load(CONTRACTS / 'prepared.json')
    for task in prepared['tasks'].values():
        if task_directory_digest(Path(task['original_package'])) != task['original_digest']:
            raise ValueError('original package drift')
    return prepared


def runner():
    os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
    gate = CloudSpendGate(
        approval='Peter 2026-11-04 15:30 ET: $20 infra budget (ov1106); goldcert contained initial $3 slice',
        cap_usd=CAP, usd_per_execution=UNIT, spend_ledger=LEDGER,
        modal_app_name=APP, billing_tag='ov1106')
    return ContainedModalRunner(repo_root=REPO, spend_gate=gate, meter=SandboxMeter(CAP)), gate


def allocation(name, phase, receipt, estimate):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'spend.jsonl').open('a') as handle:
        handle.write(json.dumps(dict(ts=datetime.now(UTC).isoformat(), lane='goldcert',
            what=phase + ':contained:' + name, provider='modal', est_usd=estimate,
            actual_usd=None, receipt=str(receipt))) + '\n')


def execute(run_cell, job):
    cell = ROOT / 'cells' / job['name']
    row = {k: v for k, v in job.items() if k != 'script'}
    row.update(status='unmeasured', reward=None, receipt=str(cell / 'result.json'))
    if not job.get('package'):
        return dict(row, reason='frozen public UI scorer unavailable')
    if cell.exists():
        raise ValueError('fresh cell required: ' + str(cell))
    if task_directory_digest(Path(job['package'])) != job['package_digest']:
        raise ValueError('package drift')
    cell.mkdir(parents=True)
    (cell / 'agent-script.sh').write_text(job['script'])
    allocation(job['name'], 'before', cell / 'result.json', UNIT)
    try:
        result = run_cell(cell, Path(job['package']), job['script'])
        row['raw_status'] = result.get('status')
        row['raw_reason'] = result.get('reason')
        row['result_sha256'] = sha(checked(cell / 'result.json'))
        for filename in ('qa-execution.json', 'egress-lock.json'):
            row[filename + '_sha256'] = sha(checked(cell / 'trial' / filename))
        if result.get('status') != 'executed' or type(result.get('reward')) not in (int, float) or result['reward'] not in (0, 1):
            raise ValueError(result.get('reason') or 'invalid native outcome')
        if result.get('agent_log', '').count(job['marker']) != job['script'].count(job['marker']):
            raise ValueError('source application/probe marker missing')
        row.update(status='executed', reward=result['reward'], est_usd=result.get('est_usd'))
    except Exception as exc:
        row['reason'] = type(exc).__name__ + ': ' + str(exc)
        (cell / 'exception.txt').write_text(traceback.format_exc())
    finally:
        allocation(job['name'], 'after', cell / 'result.json', row.get('est_usd', UNIT))
    save(cell / 'measurement.json', row)
    print(json.dumps(row), flush=True)
    return row


def stop_apps():
    listing = subprocess.run(['modal', 'app', 'list', '--json'], capture_output=True, text=True, check=True)
    rows = []
    for app in json.loads(listing.stdout):
        if app['description'] == APP and app['state'] != 'stopped':
            done = subprocess.run(['modal', 'app', 'stop', '-y', app['app_id']], capture_output=True, text=True)
            rows.append(dict(app_id=app['app_id'], returncode=done.returncode, stdout=done.stdout, stderr=done.stderr))
    return rows


def qualification():
    prepared = frozen()
    task = prepared['tasks']['000081']
    run_cell, gate = runner()
    report = {}
    try:
        report['outcome'] = execute(run_cell, dict(name='qualify-inert-000081', task='000081', arm='qualification',
            package=task['original_package'], package_digest=task['original_digest'],
            script='set -eu\nprintf "CONTAINED_INERT_NOP\\n"\n', marker='CONTAINED_INERT_NOP'))
        report['qualified'] = report['outcome']['status'] == 'executed' and report['outcome']['reward'] == 0
        cell = ROOT / 'cells/qualify-inert-000081'
        report['native_execution'] = load(cell / 'trial/qa-execution.json')
        report['native_egress'] = load(cell / 'trial/egress-lock.json')
    finally:
        report['spend'] = gate.to_json()
        report['app_stops'] = stop_apps()
        save(ROOT / 'qualification.json', report)
    if not report.get('qualified'):
        raise ValueError('inert native qualification failed closed')


def added_patch(name, data):
    text = data.decode()
    return (f'diff --git a/{name} b/{name}\nnew file mode 100644\n' + ''.join(difflib.unified_diff(
        [], text.splitlines(keepends=True), fromfile='/dev/null', tofile='b/' + name)))


def stage():
    if not load(ROOT / 'qualification.json').get('qualified'):
        raise ValueError('native qualification required first')
    prepared = frozen()
    manifest = dict(schema='ov1106-contained-packages-v1', created_at=datetime.now(UTC).isoformat(),
        prepared_sha256=sha(checked(CONTRACTS / 'prepared.json')), tasks={}, integrity_controls='not executed')
    try:
        for tid in TASKS:
            task = prepared['tasks'][tid]
            row = dict(original_digest=task['original_digest'], contract_sha256=task['contract_sha256'])
            manifest['tasks'][tid] = row
            if tid == '001267':
                row.update(status='unavailable', package=None, reason=task['gold_blocker'])
                continue
            source = ROOT / 'public-parent' / tid
            target = ROOT / 'hardened' / tid
            try:
                if source.exists() or target.exists():
                    raise ValueError('staging paths already exist')
                shutil.copytree(task['original_package'], source)
                for path in (source, *source.rglob('*')):
                    if not path.is_symlink():
                        path.chmod(path.stat().st_mode | 0o200)
                code = checked(HERE / 'contracts' / (tid + '.py'), task['contract_sha256'])
                module = 'test_ov1106_public_contract.py'
                command = (f'#!/bin/bash\nset -eu\ncd {task["workdir"]}\n'
                           f'{PYTHON[tid]} -m unittest -v test_ov1106_public_contract\n').encode()
                patch = added_patch(module, code) + added_patch('mimo_test_command.sh', command)
                (source / 'tests/test.patch').write_text(patch)
                (source / 'tests/test_command.sh').write_text(f'bash {task["workdir"]}/mimo_test_command.sh\n')
                envelope = checked(source / 'tests/test.sh').decode()
                envelope, count = re.subn(r"(done <<'EOF_RESET_TEST_FILES'\n).*?(\nEOF_RESET_TEST_FILES)",
                    lambda m: m[1] + module + '\nmimo_test_command.sh' + m[2], envelope, count=1, flags=re.S)
                if count != 1:
                    raise ValueError('unknown MiMo reset envelope')
                (source / 'tests/test.sh').write_text(envelope)
                hardened = harden(source, target, single_container=False, allow_degraded=False)
                if not hardened.separate_verifier or hardened.shape != 'mimo' or hardened.runner != 'unittest':
                    raise ValueError('unexpected hardening shape/runner')
                row.update(status='staged', package=str(target), package_digest=task_directory_digest(target),
                    public_parent_digest=task_directory_digest(source), test_patch_sha256=sha(checked(target / 'tests/test.patch')),
                    hardening=hardened.to_json())
            except Exception as exc:
                row.update(status='failed', package=None, reason=type(exc).__name__ + ': ' + str(exc))
                (ROOT / ('staging-' + tid + '-error.txt')).write_text(traceback.format_exc())
    finally:
        save(ROOT / 'contained-manifest.json', manifest)
        save(HERE / 'contained-manifest.json', manifest)


def jobs_for(tasks):
    prepared = frozen()
    manifest = load(ROOT / 'contained-manifest.json')
    admission_path = PRIVATE / 'pregrade-provenance-summary/summary.json'
    admission = load(admission_path)
    if (admission['generation_error'] is not None or admission['alternative_informed'] is not False
            or admission['contract_drift'] or admission['planned_slots'] != 20):
        raise ValueError('failed pregrade admission')
    checked(CONTRACTS / 'prepared.json', admission['input_hashes']['prepared'])
    checked(CONTRACTS / 'contract-freeze.json', admission['input_hashes']['freeze'])
    slots = {(r['task'], r['index']): r for r in admission['slots']}
    if len(slots) != 20 or set(slots) != {(tid, i) for tid in TASKS for i in range(4)}:
        raise ValueError('missing candidate slot')
    candidates = {}
    for (tid, index), slot in slots.items():
        directory = PRIVATE / 'resumed-terra-writers/writers' / tid / str(index)
        receipt = json.loads(checked(directory / 'receipt.json', slot['receipt_sha256']))
        split = 'validation' if index < 2 else 'held_out'
        if (slot['provenance'] is not True or slot['split'] != split or receipt['patch_sha256'] != slot['patch_sha256']
                or receipt['task'] != tid or receipt['index'] != index or receipt['frozen_split'] != split
                or receipt.get('eligible_for_execution') is not True or receipt.get('error') is not None
                or receipt.get('cleanup_ok') is not True):
            raise ValueError('candidate provenance/identity failed')
        candidates[tid, index] = checked(directory / 'solution.patch', slot['patch_sha256']).decode()
    jobs = []
    for tid in tasks:
        task = prepared['tasks'][tid]
        hard = manifest['tasks'][tid]
        mutant_hash = lambda c: c.get('mutant_sha256') or c['name'].rsplit('-', 1)[-1]
        pools = {arm: {mutant_hash(c) for c in task['controls'] if c['arm'] == arm} for arm in ('train', 'eval')}
        if pools['train'] & pools['eval']:
            raise ValueError('frozen mutant pools overlap')
        for control in task['controls']:
            script = control['script']
            jobs.append(dict(task=tid, arm=control['arm'], control=control['name'], name=tid + '-hardened-' + control['name'],
                package=hard['package'], package_digest=hard.get('package_digest'), script=script,
                mutant_sha256=mutant_hash(control) if control['arm'] in ('train', 'eval') else None,
                script_sha256=sha(script.encode()), marker='SAFE_NOP' if control['arm'] == 'nop' else
                ('SOURCE_PATCH_APPLIED' if 'SOURCE_PATCH_APPLIED' in script else 'MUTANT_APPLIED')))
        for index in range(4):
            slot = slots[tid, index]
            script = patch_script(candidates[tid, index], task['workdir'])
            jobs.append(dict(task=tid, arm='alternative', control='alternative-' + str(index),
                name=tid + '-hardened-alternative-' + str(index), package=hard['package'], package_digest=hard.get('package_digest'),
                patch_sha256=slot['patch_sha256'], receipt_sha256=slot['receipt_sha256'], split=slot['split'],
                script=script, script_sha256=sha(script.encode()), marker='SOURCE_PATCH_APPLIED'))
    return jobs


def matrix(tasks, controls):
    if not load(ROOT / 'qualification.json').get('qualified'):
        raise ValueError('qualification required')
    jobs = [j for j in jobs_for(tasks) if not controls or j['control'] in controls]
    # Complete reference/nop qualification across the cohort before candidate scoring.
    priority = {'reference': 0, 'nop': 1, 'alternative': 2, 'train': 3, 'eval': 4}
    jobs.sort(key=lambda job: (priority[job['arm']], job['task'], job['control']))
    run_cell, gate = runner()
    report = dict(started_at=datetime.now(UTC).isoformat(), jobs=[{k: v for k, v in j.items() if k != 'script'} for j in jobs], rows=[])
    output = ROOT / ('matrix-' + '-'.join(tasks) + ('-smoke' if controls else '') + '.json')
    if output.exists():
        raise ValueError('matrix output already exists')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = []
            for job in jobs:
                previous = ROOT / 'cells' / job['name'] / 'measurement.json'
                if previous.exists():
                    row = load(previous)
                    for key in ('package_digest', 'script_sha256', 'task', 'control'):
                        if row.get(key) != job.get(key):
                            raise ValueError('prior measurement binding mismatch: ' + key)
                    checked(row['receipt'], row['result_sha256'])
                    for filename in ('qa-execution.json', 'egress-lock.json'):
                        checked(previous.parent / 'trial' / filename, row[filename + '_sha256'])
                    report['rows'].append(row)
                else:
                    pending.append(job)
            for row in pool.map(lambda job: execute(run_cell, job), pending):
                report['rows'].append(row)
                save(output, report)
    finally:
        report['spend'] = gate.to_json()
        report['app_stops'] = stop_apps()
        save(output, report)


def probe():
    task = frozen()['tasks']['001267']
    run_cell, gate = runner()
    report = {}
    try:
        report['row'] = execute(run_cell, dict(task='001267', arm='runtime-probe', name='001267-ui-runtime-probe',
            package=task['original_package'], package_digest=task['original_digest'],
            script=checked(PRIVATE / 'ui-runtime-probe.sh').decode(), marker='UI_RUNTIME_PROBE_DONE'))
    finally:
        report['spend'] = gate.to_json()
        report['app_stops'] = stop_apps()
        save(ROOT / 'ui-runtime-probe.json', report)


def summarize():
    prepared = frozen()
    manifest = load(ROOT / 'contained-manifest.json')
    rows = []
    for path in sorted((ROOT / 'cells').glob('*/measurement.json')):
        row = load(path)
        if row.get('arm') in ('qualification', 'runtime-probe'):
            continue
        if row.get('result_sha256'):
            checked(row['receipt'], row['result_sha256'])
        rows.append(row)
    planned = jobs_for(TASKS)
    actual = {row['name']: row for row in rows}
    rows = [actual.get(job['name'], dict(
        **{k: v for k, v in job.items() if k != 'script'},
        status='unmeasured', reward=None,
        reason='unavailable public UI scorer' if not job['package'] else 'not executed')) for job in planned]
    for job, row in zip(planned, rows, strict=True):
        row['contract_sha256'] = manifest['tasks'][job['task']]['contract_sha256']
        if row.get('receipt'):
            native_path = Path(row['receipt']).parent / 'trial/qa-execution.json'
            if native_path.is_file():
                native = load(native_path)
                row['native_exception'] = native.get('exception')
                row['native_receipt_sha256'] = sha(checked(native_path))
                if native.get('exception'):
                    row.update(status='unmeasured', reward=None, category='native_execution_error')
            if not job['package']:
                row['category'] = 'unavailable_public_scorer'
        if row['status'] != 'executed':
            continue
        cell = Path(row['receipt']).parent
        checked(cell / 'agent-script.sh', job['script_sha256'])
        raw = load(cell / 'result.json')
        if raw.get('agent_log', '').count(job['marker']) != job['script'].count(job['marker']):
            row.update(status='unmeasured', reward=None, reason='not all source patches apply')
        for filename in ('qa-execution.json', 'egress-lock.json'):
            checked(cell / 'trial' / filename, row[filename + '_sha256'])
    for tid in TASKS:
        for arm in ('witness', 'exit0_pth'):
            rows.append(dict(task=tid, arm=arm, name=tid + '-hardened-' + arm,
                status='unmeasured', reward=None,
                package_digest=manifest['tasks'][tid].get('package_digest'),
                contract_sha256=manifest['tasks'][tid]['contract_sha256'],
                reason='Integrity bypass execution is outside this measurement safety boundary.'))
    originals = load(CONTRACTS / 'safe-matrix.json')
    original_rows = []
    for tid, task in prepared['tasks'].items():
        for control in task['controls']:
            matches = [row for row in originals if row['task'] == tid
                       and row['version'] == 'original' and row['control'] == control['name']]
            if len(matches) != 1:
                continue
            item = matches[0]
            cell = Path(item['receipt']).parent
            if checked(cell / 'agent-script.sh') != control['script'].encode():
                raise ValueError('original control script/hash mismatch')
            native = load(cell / 'trial/qa-execution.json')
            if native['package_digest'] != task['original_digest']:
                raise ValueError('original package binding mismatch')
            raw = load(cell / 'result.json')
            reward = raw.get('reward') if raw.get('status') == 'executed' else None
            if reward != native['reward']:
                raise ValueError('original native reward mismatch')
            original_rows.append(dict(task=tid, control=control['name'], arm=control['arm'],
                mutant_sha256=control.get('mutant_sha256') or
                    (control['name'].rsplit('-', 1)[-1] if control['arm'] in ('train', 'eval') else None),
                script_sha256=sha(control['script'].encode()), reward=reward,
                result_sha256=sha(checked(cell / 'result.json')), receipt=str(cell / 'result.json')))
    def gate(values, expected):
        if any(value is not None and value != expected for value in values):
            return 'FAIL'
        return 'PASS' if values and all(value == expected for value in values) else 'U'
    tasks = {}
    for tid in TASKS:
        group = [row for row in rows if row['task'] == tid]
        arm = lambda name: [r['reward'] for r in group if r['arm'] == name]
        gates = dict(G1=gate(arm('reference'), 1), G2=gate(arm('nop'), 0),
                     G3='U', G4='U', G5=gate(arm('train'), 0), G6=gate(arm('eval'), 0),
                     G7=gate(arm('alternative'), 1))
        base = [r for r in original_rows if r['task'] == tid and r['arm'] == 'eval']
        if gates['G6'] == 'PASS' and any(r['reward'] is None for r in base):
            gates['G6'] = 'U'
        counts = {}
        for name, desired in (('train', 0), ('eval', 0), ('alternative', 1)):
            values = arm(name)
            measured = sum(v is not None for v in values)
            successes = sum(v == desired for v in values)
            counts[name] = dict(planned=len(values), measured=measured, successes=successes,
                wilson95=list(wilson_interval(successes, measured)) if measured else None)
        tasks[tid] = dict(gates=gates, certified=False, counts=counts,
            package_digest=manifest['tasks'][tid].get('package_digest'),
            contract_sha256=manifest['tasks'][tid]['contract_sha256'],
            alternative_splits={split: [r['reward'] for r in group
                if r['arm'] == 'alternative' and r['split'] == split] for split in ('validation', 'held_out')})
    qualification = load(ROOT / 'qualification.json')
    qualification['receipt'] = str(ROOT / 'qualification.json')
    qualification['receipt_sha256'] = sha(checked(ROOT / 'qualification.json'))
    qualification['native_execution'] = {k: v for k, v in qualification['native_execution'].items()
                                         if k not in ('agent_stdout', 'verifier_stdout')}
    ledger = load(LEDGER)
    save(ROOT / 'spend-snapshot.json', ledger)
    measured_estimate = sum(load(path).get('est_usd') or 0
                           for path in (ROOT / 'cells').glob('*/result.json'))
    matrix_path = ROOT / ('matrix-' + '-'.join(TASKS) + '.json')
    matrix = load(matrix_path)
    cleanup_receipts = [load(ROOT / name).get('app_stops', [])
                        for name in ('qualification.json', 'matrix-000081-smoke.json',
                                     'ui-runtime-probe.json', matrix_path.name)]
    report = dict(schema='ov1106-contained-aggregate-v1', tasks=tasks, rows=rows, original_rows=original_rows,
        driver_sha256=sha(checked(Path(__file__))), manifest_sha256=sha(checked(ROOT / 'contained-manifest.json')),
        qualification=qualification, spend=runner()[1].to_json(),
        ledger_snapshot_sha256=sha(checked(ROOT / 'spend-snapshot.json')),
        measured_cell_estimate_usd=measured_estimate,
        estimate_semantics='Fixed reservations and measured cell estimates are not provider invoices.',
        matrix_receipt=str(matrix_path), matrix_receipt_sha256=sha(checked(matrix_path)),
        matrix_app_stops=matrix['app_stops'], all_app_stops=cleanup_receipts,
        integrity_blocker='Integrity bypass scripts are outside this measurement safety boundary; neither arm executes.',
        ui_probe=load(ROOT / 'ui-runtime-probe.json') if (ROOT / 'ui-runtime-probe.json').exists() else None)
    save(ROOT / 'contained-aggregate.json', report)
    save(HERE / 'contained-aggregate.json', report)
    print(json.dumps(dict(tasks=tasks, spend=report['spend']), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('qualify', 'stage', 'matrix', 'probe', 'summarize'))
    parser.add_argument('--task', action='append', choices=TASKS)
    parser.add_argument('--control', action='append')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.mode == 'qualify':
        qualification()
    elif args.mode == 'stage':
        stage()
    elif args.mode == 'probe':
        probe()
    elif args.mode == 'summarize':
        summarize()
    else:
        matrix(args.task or list(TASKS), args.control)


if __name__ == '__main__':
    main()
