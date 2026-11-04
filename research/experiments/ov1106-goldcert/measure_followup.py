"""Prepare, publish, execute once, and summarize clean native followup cells.

Preparation is provider-free. Execute only after the parent publishes the freeze
and the exact core runner revision. No model calls or integrity payloads occur.
"""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from evallab.modal_cells import SandboxMeter, compute_cell_key
from evallab.regrade import CloudSpendGate, SpendCapReached

import measure_contained as old
from grade_alternatives import require
from repair_sns import binding, inventory

HERE, REPO, PRIVATE = old.HERE, old.REPO, old.PRIVATE
ROOT = PRIVATE / 'helpers/safe/followup'
PREPARED = ROOT / 'prepared.json'
FREEZE = HERE / 'followup-freeze.json'
OUT = ROOT / 'execution.json'
AGGREGATE = HERE / 'followup-aggregate.json'
GENERATION = PRIVATE / 'resumed-terra-writers'
ADMISSION = PRIVATE / 'pregrade-provenance-summary/summary.json'
LEDGER = old.STATE / 'goldcert-followup-spend.json'
APP = 'ov1106-goldcert-followup'
CAP, UNIT = 3.0, 0.05
SELECTED_TASKS = ('000682', '002139')
PLAN = HERE / 'followup-budget-plan.json'
QUALIFICATION = old.ROOT / 'qualification.json'
QUALIFICATION_SHA256 = '7f7507d8b5a5cf48a76892d2268125a009a4d4bd1db40391dbbed87d784c1d05'
CORE = {
    'src/evallab/gold_certifier.py': '2a0523c4bcba81f797cc537841f98071a0bbefa51d40f736f63699c458fd861c',
    'src/evallab/regrade.py': '7659f97d04dc4976f2234dbdfa7a224bb7777ad11eacd8b474627bdb985b624f',
    'src/evallab/modal_cells.py': 'c4f49b3cc6831af3d0f69529c9d005e97644a7c8bcc8d50076f66bb2a6350acf',
    'src/evallab/qa/task_modal.py': '96e0ec0f3bf7a4f29916856fe4cb280b0a76b7bbcf8f2832f2ee996b53233955',
}
CONFIG = '/private/tmp/ov1106/shared/nofallback.yml'
APPROVAL = ('Peter 2026-11-04 15:30 ET: $20 infra budget (ov1106); '
            'goldcert $8 allocation: contained legacy $3 + contracts $2 + native followup $3')
POLICY = dict(cap_usd=CAP, unit_usd=UNIT, ledger=str(LEDGER), app=APP, billing_tag='ov1106',
              max_concurrency=2, timeout_seconds=240, meter_cap_usd=CAP,
              gate='CloudSpendGate', runner='ContainedModalRunner',
              native_calls_planned=10, units_per_hardened_cell=6, fixed_reservations_retained=True,
              estimates_not_invoices=True, approval=APPROVAL)
REUSED = {
    '000081': 'sha256:23ea08b4e6002ec577a56dc0aee25e06ef4912c21b432c67e3122a6221426717',
    '002139': 'sha256:4a8b4f14a5ae4cd8ef1446ca00edc19b3bbe49b45824b65b606b804dac5818cc',
    '003007': 'sha256:5da958aee2dd6953a1da6a2c5ade58bc72146b010664206414983fb6a2bdaad5',
}
checked, load, save, sha = old.checked, old.load, old.save, old.sha


def key(job):
    return compute_cell_key(package_digest=job['package_digest'], variant=job['name'],
                            submission_digest=job['script_sha256'], backend_image='modal-default',
                            grader_hook='', timeout_seconds=240)


def selected(job):
    return (job['task'] == '000682' and job['arm'] in ('reference', 'nop')
            or job['task'] in SELECTED_TASKS and job['arm'] == 'alternative')


def budget_plan():
    plan = load(PLAN)
    require(plan['schema'] == 'ov1106-goldcert-bounded-followup-v1'
            and plan['native_gate'] == 'exact evallab.regrade.CloudSpendGate'
            and plan['native_qualification_sha256'] == QUALIFICATION_SHA256
            and plan['lane_cap_usd'] == 8 and plan['unit_usd'] == UNIT
            and plan['slice_caps_usd'] == dict(initial_contained=3, contracts=2, native_followup=3)
            and plan['selected_tasks'] == list(SELECTED_TASKS)
            and plan['candidate_indices'] == [0, 1, 2, 3]
            and plan['historical_validation_indices'] == [0, 1]
            and plan['historical_held_out_indices'] == [2, 3]
            and plan['planned_denominator_tasks'] == list(old.TASKS)
            and plan['planned_alternatives'] == 20
            and plan['newly_held_out'] is False and plan['sns_alternative_informed'] is True
            and plan['old_reservations_retained'] is True
            and plan['sns_contract'] == 'contracts-v3/000682.py'
            and plan['assertion_changes_other_tasks'] is False
            and plan['new_native_cells'] == {
                '000682': ['reference', 'nop'] + ['alternative-' + str(i) for i in range(4)],
                '002139': ['alternative-' + str(i) for i in range(4)]},
            'parent bounded budget plan differs from the admitted exact 10-cell contract')
    return binding(PLAN)


def retained_qualification(prepared):
    """Inspect retained native bytes and cached app-stop receipts without a provider call."""
    for path, digest in CORE.items():
        checked(REPO / path, digest)
    receipt = json.loads(checked(QUALIFICATION, QUALIFICATION_SHA256))
    task = prepared['tasks']['000081']
    script = 'set -eu\nprintf "CONTAINED_INERT_NOP\\n"\n'
    job = dict(name='qualify-inert-000081', task='000081', arm='qualification',
               package=task['original_package'], package_digest=task['original_digest'],
               script=script, script_sha256=sha(script.encode()), marker='CONTAINED_INERT_NOP')
    outcome = receipt['outcome']
    cell = old.ROOT / 'cells' / job['name']
    require(Path(outcome['receipt']).resolve() == (cell / 'result.json').resolve()
            and outcome['package'] == job['package'] and outcome['package_digest'] == job['package_digest']
            and outcome['name'] == job['name'] and outcome['status'] == 'executed',
            'retained qualification identity changed')
    hashes = {'agent-script.sh': job['script_sha256'], 'result.json': outcome['result_sha256'],
              'trial/qa-execution.json': outcome['qa-execution.json_sha256'],
              'trial/egress-lock.json': outcome['egress-lock.json_sha256']}
    hashes.update({name: sha(checked(cell / name)) for name in
                   ('trial/agent/oracle.txt', 'trial/verifier/test_output.log')})
    proof = validate_native(job, cell, hashes)
    require(receipt['qualified'] is True and proof['reward'] == outcome['reward'] == 0
            and receipt['native_execution'] == load(cell / 'trial/qa-execution.json')
            and receipt['native_egress'] == load(cell / 'trial/egress-lock.json'),
            'retained native qualification failed')
    cloud = receipt['native_execution']['cloud_spend']
    stops = receipt['app_stops']
    require(cloud['modal_app_name'] == old.APP and cloud['cap_usd'] == 3
            and cloud['usd_per_execution'] == UNIT and cloud['billing_tag'] == 'ov1106'
            and Path(cloud['spend_ledger']).resolve() == old.LEDGER.resolve()
            and len(stops) == 1 and stops[0]['returncode'] == 0
            and stops[0]['app_id'].startswith('ap-'), 'retained owned-app stop/budget receipt invalid')
    return (dict(receipt=binding(QUALIFICATION), outcome=proof, core_sha256=CORE,
                 app_stops=stops, scope='native environment only; no network-isolation claim'),
            [QUALIFICATION] + [cell / name for name in hashes])


def admit():
    """Revalidate all 20 original source-only identities, not score metadata."""
    budget_plan()
    prepared = old.frozen()
    admission = load(ADMISSION)
    jobs = old.jobs_for(old.TASKS)
    generation = load(GENERATION / 'generation.json')
    generated = {(r['task'], r['index']): r for r in generation}
    slots = {(r['task'], r['index']): r for r in admission['slots']}
    expected = {(tid, i) for tid in old.TASKS for i in range(4)}
    require(len(generation) == len(admission['slots']) == 20
            and set(generated) == set(slots) == expected, 'nominal 20 slots changed')
    transport = load(GENERATION / 'frozen/binding.json')
    writer_manifest = json.loads(checked(GENERATION / 'frozen/manifest.json', transport['manifest_sha256']))
    tasks = {r['id']: r for r in writer_manifest['tasks']}
    require(set(tasks) == set(old.TASKS), 'writer cohort changed')
    canary_path = GENERATION / 'accepted-canary.json'
    canary = load(canary_path)
    canary_receipt = json.loads(checked(canary['receipt_path'], canary['receipt_sha256']))
    require(canary_receipt == canary['receipt'] and canary_receipt['passed'] is True
            and canary_receipt['cleanup_ok'] is True and canary_receipt['error'] is None
            and canary_receipt['binding'] == transport, 'canary identity failed')
    inputs = [ADMISSION, GENERATION / 'generation.json', canary_path, Path(canary['receipt_path']),
              GENERATION / 'frozen/binding.json', GENERATION / 'frozen/manifest.json',
              old.CONTRACTS / 'prepared.json', old.CONTRACTS / 'contract-freeze.json']
    for task in tasks.values():
        checked(task['instruction'], task['instruction_sha256'])
        inputs.append(Path(task['instruction']))
        require(set(task['sources']) == set(task['source_hashes']), 'source inventory changed')
        for name, path in task['sources'].items():
            checked(path, task['source_hashes'][name])
            inputs.append(Path(path))
    for tid, index in sorted(expected):
        directory = GENERATION / 'writers' / tid / str(index)
        slot, task = slots[tid, index], tasks[tid]
        receipt_path, patch_path = directory / 'receipt.json', directory / 'solution.patch'
        receipt = json.loads(checked(receipt_path, slot['receipt_sha256']))
        require(receipt == generated[tid, index] and receipt['binding'] == transport
                and all(receipt.get(k) == v for k, v in transport.items())
                and receipt['source_files'] == task['source_hashes']
                and receipt['instruction_sha256'] == task['instruction_sha256']
                and receipt['test_aware'] is False and receipt['quarantined_patch'] is None
                and receipt['eligible_for_execution'] is True and receipt['cleanup_ok'] is True
                and receipt['error'] is None and receipt['tool_names'] == ['bash']
                and receipt['models_served'] == [transport['model']]
                and receipt['model_requested'] == transport['model']
                and receipt['canary_sha256'] == sha(json.dumps(
                    canary_receipt, sort_keys=True, separators=(',', ':')).encode()), 'source-only receipt mismatch')
        require(Path(receipt['patch']).resolve() == patch_path.resolve()
                and receipt['patch_sha256'] == slot['patch_sha256'], 'patch path/hash binding changed')
        checked(patch_path, slot['patch_sha256'])
        require(receipt['modified_paths'] and all(
            any(p.startswith(prefix) for prefix in task['allowed_prefixes'])
            and '..' not in Path(p).parts and not Path(p).is_absolute()
            for p in receipt['modified_paths']), 'non-source modification admitted')
        inputs.extend((receipt_path, patch_path))
    require(len(jobs) == 48 and sum(j['arm'] == 'train' for j in jobs) == 8
            and sum(j['arm'] == 'eval' for j in jobs) == 10, 'frozen 18 mutant pool changed')
    return prepared, jobs, inputs


def validate_native(job, cell, hashes=None):
    """Never replace a failed native result with a different scorer's reward."""
    names = ('agent-script.sh', 'result.json', 'trial/qa-execution.json', 'trial/egress-lock.json',
             'trial/agent/oracle.txt', 'trial/verifier/test_output.log')
    bindings = {name: sha(checked(cell / name, hashes[name] if hashes else None)) for name in names}
    checked(cell / 'agent-script.sh', job['script_sha256'])
    raw, native, egress = (load(cell / name) for name in names[1:4])
    agent_log = checked(cell / names[4]).decode()
    test_output = checked(cell / names[5]).decode()
    require(raw.get('agent_log') == agent_log and raw.get('test_output_tail') == test_output[-600:]
            and raw.get('evidence_sha256') == sha((agent_log + test_output).encode()),
            'native output/evidence digest mismatch')
    if '-followupv2-' in job['name']:
        cloud = native.get('cloud_spend') or {}
        require(cloud.get('cap_usd') == CAP and cloud.get('usd_per_execution') == UNIT
                and cloud.get('modal_app_name') == APP and cloud.get('billing_tag') == 'ov1106'
                and cloud.get('approval') == APPROVAL
                and Path(cloud.get('spend_ledger', '')).resolve() == LEDGER.resolve(),
                'native cumulative budget identity mismatch')
    require(task_digest(job) == job['package_digest'], 'native package changed')
    require(raw.get('cache_hit') is False and raw.get('cell_key') == key(job)
            and raw.get('status') == 'executed' and type(raw.get('reward')) in (int, float)
            and raw['reward'] in (0, 1) and native.get('exception') is None
            and native.get('environment') == 'modal' and native.get('mode') == 'shortcut'
            and native.get('package_digest') == job['package_digest']
            and Path(native['job_path']).resolve() == (cell / 'trial').resolve()
            and native.get('reward') == raw['reward'], 'invalid native outcome/identity')
    marker = job['marker']
    count = job['script'].count(marker)
    require(count > 0 and raw.get('agent_log', '').count(marker) == count
            and native.get('agent_stdout', '').count(marker) == count, 'partial application or marker mismatch')
    require(egress.get('schema_version') == 2 and egress.get('requested') is True
            and egress.get('error') is None and any(
                r.get('source') == 'creation' and r.get('network_mode') in ('public', 'allowlist', 'no-network')
                and isinstance(r.get('sandbox_id'), str) and r['sandbox_id'].startswith('sb-')
                for r in egress.get('applications', [])), 'missing actual native creation receipt')
    return dict(status='executed', reward=raw['reward'], hashes=bindings,
                receipt=str(cell / 'result.json'), native_exception=None,
                est_usd=raw.get('est_usd'), cell_key=raw['cell_key'])


def task_digest(job):
    return old.task_directory_digest(Path(job['package']))


def historical(jobs):
    aggregate = load(HERE / 'contained-aggregate.json')
    rows, inputs = [], [HERE / 'contained-aggregate.json', HERE / 'contained-manifest.json']
    for job in jobs:
        if job['package'] and job['arm'] in ('train', 'eval', 'alternative'):
            previous = old.ROOT / 'cells' / job['name'] / 'result.json'
            require(not previous.is_file() or load(previous).get('reward') is None,
                    'clean cell already has a native reward; no behavioral repeat')
        if job['package'] and job['arm'] in ('reference', 'nop'):
            matches = [r for r in aggregate['rows'] if r['name'] == job['name']]
            require(len(matches) == 1, 'missing historical reference/nop')
            row = matches[0]
            require(all(row.get(k) == job.get(k) for k in ('task', 'arm', 'control', 'package_digest', 'script_sha256')),
                    'historical control binding changed')
            cell = old.ROOT / 'cells' / job['name']
            require(Path(row['receipt']) == cell / 'result.json', 'historical receipt path changed')
            hashes = {'agent-script.sh': job['script_sha256'], 'result.json': row['result_sha256'],
                      'trial/qa-execution.json': row['qa-execution.json_sha256'],
                      'trial/egress-lock.json': row['egress-lock.json_sha256'],
                      'trial/agent/oracle.txt': sha(checked(cell / 'trial/agent/oracle.txt')),
                      'trial/verifier/test_output.log': sha(checked(cell / 'trial/verifier/test_output.log'))}
            proof = validate_native(job, cell, hashes)
            require(proof['reward'] == (1 if job['arm'] == 'reference' else 0), 'historical G1/G2 failed')
            rows.append(dict(job_name=job['name'], task=job['task'], arm=job['arm'], **proof))
            inputs.extend(cell / name for name in hashes)
    require(len(rows) == 8, 'expected 8 historical native G1/G2 receipts')
    # Retain observed UI runtime evidence, without inventing browser grading.
    ui = load(old.ROOT / 'ui-runtime-probe.json')
    inputs.append(old.ROOT / 'ui-runtime-probe.json')
    probe_cell = Path(ui['row']['receipt']).parent
    inputs.extend(p for p in (probe_cell / 'agent/oracle.txt', probe_cell / 'trial/agent/oracle.txt',
                             probe_cell / 'result.json', probe_cell / 'trial/qa-execution.json') if p.is_file())
    return rows, inputs


def stage_sns(task):
    source, target = ROOT / 'public-parent/000682', ROOT / 'hardened/000682'
    require(not source.exists() and not target.exists(), 'SNS staging paths exist')
    shutil.copytree(task['original_package'], source)
    for path in (source, *source.rglob('*')):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
    code = checked(HERE / 'contracts-v3/000682.py',
                   '483774c5a299c827f079cc0bf3a80bee2579899ca6ff1ef8bee813382fd5289a')
    module = 'test_ov1106_public_contract.py'
    command = (f'#!/bin/bash\nset -eu\ncd {task["workdir"]}\n'
               f'{old.PYTHON["000682"]} -m unittest -v test_ov1106_public_contract\n').encode()
    (source / 'tests/test.patch').write_text(old.added_patch(module, code) + old.added_patch('mimo_test_command.sh', command))
    (source / 'tests/test_command.sh').write_text(f'bash {task["workdir"]}/mimo_test_command.sh\n')
    envelope, count = re.subn(r"(done <<'EOF_RESET_TEST_FILES'\n).*?(\nEOF_RESET_TEST_FILES)",
                             lambda m: m[1] + module + '\nmimo_test_command.sh' + m[2],
                             checked(source / 'tests/test.sh').decode(), count=1, flags=re.S)
    require(count == 1, 'unknown standard MiMo reset envelope')
    (source / 'tests/test.sh').write_text(envelope)
    result = old.harden(source, target, single_container=False, allow_degraded=False)
    report = result.to_json()
    require(result.separate_verifier and result.shape == 'mimo' and result.runner == 'unittest'
            and report['transform'] == 'full-hardening@1'
            and any(r['layer'] == 'L4' and r['status'] == 'applied'
                    and 'separate-verifier@10' in r['mechanism'] for r in report['layers']),
            'canonical separate-verifier@10 unittest hardening required')
    require(old.task_directory_digest(Path(task['original_package'])) == task['original_digest'], 'original changed')
    return dict(package=str(target), package_digest=old.task_directory_digest(target),
                contract_sha256=sha(code), public_parent_digest=old.task_directory_digest(source),
                test_patch_sha256=sha(checked(target / 'tests/test.patch')), hardening=report)


def prepare():
    require(not any(p.exists() for p in (ROOT, FREEZE, AGGREGATE, LEDGER)), 'fresh followup preparation required')
    prepared, prior_jobs, inputs = admit()
    history, history_inputs = historical(prior_jobs)
    qualification, qualification_inputs = retained_qualification(prepared)
    inputs.extend(qualification_inputs)
    inputs.extend(history_inputs)
    old_manifest = load(HERE / 'contained-manifest.json')
    require(load(old.ROOT / 'contained-manifest.json') == old_manifest, 'old manifest copies differ')
    packages = {}
    for tid in REUSED:
        item = old_manifest['tasks'][tid]
        require(item['package_digest'] == REUSED[tid] and old.task_directory_digest(Path(item['package'])) == REUSED[tid],
                'exact old hardened digest required')
        checked(HERE / 'contracts' / (tid + '.py'), item['contract_sha256'])
        checked(Path(item['package']) / 'tests/test.patch', item['test_patch_sha256'])
        packages[tid] = item
    ROOT.mkdir(parents=True)
    packages['000682'] = stage_sns(prepared['tasks']['000682'])
    packages['001267'] = old_manifest['tasks']['001267']
    jobs = []
    for job in prior_jobs:
        new = dict(job)
        new['historical_name'] = job['name']
        new['name'] = job['task'] + '-followupv2-' + job['control']
        package = packages[job['task']]
        new.update(package=package['package'], package_digest=package.get('package_digest'),
                   contract_sha256=package['contract_sha256'])
        new['reuse'] = job['task'] in REUSED and job['arm'] in ('reference', 'nop')
        if job['arm'] == 'alternative':
            new.update(index=int(job['control'].rsplit('-', 1)[-1]),
                       alternative_informed=job['task'] == '000682', newly_held_out=False)
        jobs.append(new)
        new['selected_native'] = selected(new)
    require(sum(j['selected_native'] for j in jobs) == 10
            and sum(j['selected_native'] and j['arm'] == 'alternative' for j in jobs) == 8
            and sum(j['arm'] in ('train', 'eval') for j in jobs) == 18,
            'bounded matrix must retain 18 mutants and select only 10 native cells')
    # The old ledger remains fully committed. Never instantiate or settle its gate.
    old_ledger = load(old.LEDGER)
    require(old_ledger['reserved_usd'] == 3.0, 'legacy reservations must remain $3')
    inputs.append(old.LEDGER)
    inputs.extend((old.ROOT / 'contained-manifest.json', old.ROOT / 'ui-runtime-probe.json'))
    sources = [HERE / n for n in ('measure_followup.py', 'measure_contained.py', 'repair_sns.py',
                                  'grade_alternatives.py', 'contracts_harness.py', 'measure_contracts.py',
                                  'contained-manifest.json', 'contained-aggregate.json', 'contracts-v3/000682.py',
                                  'followup-budget-plan.json')]
    sources.extend(sorted((HERE / 'contracts').glob('*.py')))
    # Freeze the complete first-party native implementation, including transform dependencies.
    sources.extend(sorted((REPO / 'src/evallab').rglob('*.py')))
    sources.extend((REPO / 'pyproject.toml', REPO / 'uv.lock'))
    manifest = dict(schema='ov1106-followup-prepared-v3', prepared_at=datetime.now(UTC).isoformat(),
                    policy=POLICY, jobs=jobs, reused_qualification=qualification, historical_G1_G2=history,
                    packages=packages, inputs=[binding(p) for p in dict.fromkeys(inputs)],
                    source_files=[binding(p) for p in dict.fromkeys(sources)],
                    package_files={tid: inventory(Path(p['package'])) for tid, p in packages.items() if p['package']},
                    ui_reason=('Observed original runtime: Python-2 print syntax fails on Python 3.11; '
                               'Python 2, browser executables and UI dependency/build directories are absent.'),
                    safety_boundary='G3/G4 grader-bypass payloads are not executed.',
                    candidate_planned=20, certification_possible=False)
    save(PREPARED, manifest)
    save(FREEZE, dict(schema='ov1106-followup-freeze-v3', prepared=binding(PREPARED),
                      policy=POLICY, source_files=manifest['source_files'], certified=False,
                      alternative_informed_sns=True, newly_held_out_sns=False))
    for tree in (ROOT / 'public-parent', ROOT / 'hardened'):
        for p in sorted(tree.rglob('*'), reverse=True):
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & ~0o222)
        tree.chmod(tree.stat().st_mode & ~0o222)
    PREPARED.chmod(0o444)
    print(json.dumps(dict(freeze=binding(FREEZE), prepared=binding(PREPARED),
                         native_calls_planned=10, selected_native_alternatives=8, policy=POLICY), sort_keys=True))


def verify_frozen(expected):
    require(re.fullmatch('[0-9a-f]{64}', expected or '') is not None, 'exact freeze SHA256 required')
    freeze = json.loads(checked(FREEZE, expected))
    require(freeze['prepared']['path'] == str(PREPARED.resolve()) and freeze['policy'] == POLICY,
            'freeze identity changed')
    manifest = json.loads(checked(PREPARED, freeze['prepared']['sha256']))
    require(manifest['policy'] == POLICY and manifest['source_files'] == freeze['source_files'], 'policy/source drift')
    for item in manifest['inputs'] + manifest['source_files']:
        checked(item['path'], item['sha256'])
    for tid, package in manifest['packages'].items():
        if package['package']:
            path = Path(package['package'])
            require(old.task_directory_digest(path) == package['package_digest']
                    and inventory(path) == manifest['package_files'][tid], 'hardened package changed')
    _, prior, _ = admit()
    history, _ = historical(prior)
    require(history == manifest['historical_G1_G2'], 'historical native bindings changed')
    prior_by_name = {j['name']: j for j in prior}
    require(len(manifest['jobs']) == 48, 'fixed job count changed')
    for job in manifest['jobs']:
        before = prior_by_name[job['historical_name']]
        require(all(job.get(k) == before.get(k) for k in ('script', 'script_sha256', 'marker', 'task', 'arm',
                                                         'control', 'patch_sha256', 'receipt_sha256', 'mutant_sha256')),
                'frozen script/patch/member changed')
        require(job['selected_native'] == selected(job), 'native selection changed')
    qualification, _ = retained_qualification(old.frozen())
    require(qualification == manifest['reused_qualification'], 'retained qualification drift')
    return manifest


def publication(commit, manifest):
    require(re.fullmatch('[0-9a-f]{40}', commit or '') is not None, 'full publication commit required')
    files = [FREEZE] + [Path(item['path']) for item in manifest['source_files']]
    relative = list(dict.fromkeys(p.relative_to(REPO).as_posix() for p in files))
    archive = subprocess.run(['git', 'archive', commit, '--', *relative], cwd=REPO,
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for path in relative:
            member = tree.getmember(path)
            require(member.isfile(), 'published file is not regular: ' + path)
            stream = tree.extractfile(member)
            require(stream is not None and stream.read() == checked(REPO / path), 'unpublished frozen bytes: ' + path)
    remote = subprocess.run(['git', 'ls-remote', '--heads', 'origin'], cwd=REPO,
                            check=True, capture_output=True, text=True)
    require(any(subprocess.run(['git', 'merge-base', '--is-ancestor', commit, line.split()[0]],
                               cwd=REPO, capture_output=True).returncode == 0
                for line in remote.stdout.splitlines()), 'commit absent from published origin branches')


def runner():
    os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
    gate = CloudSpendGate(
        approval=APPROVAL, cap_usd=CAP, spend_ledger=LEDGER, usd_per_execution=UNIT,
        modal_app_name=APP, billing_tag='ov1106')
    return old.ContainedModalRunner(repo_root=REPO, spend_gate=gate, meter=SandboxMeter(CAP)), gate


def allocation(job, phase, estimate):
    old.STATE.mkdir(parents=True, exist_ok=True)
    with (old.STATE / 'spend.jsonl').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(dict(ts=datetime.now(UTC).isoformat(), lane='goldcert',
            what=phase + ':followup:' + job['name'], provider='modal', est_usd=estimate,
            actual_usd=None, receipt=str(ROOT / 'cells' / job['name'] / 'result.json'))) + '\n')


def fresh(job):
    cell = ROOT / 'cells' / job['name']
    require(not cell.exists() and not (cell.parent / key(job)).exists(), 'fresh cell/cache path required: ' + job['name'])


def run_job(job):
    fresh(job)
    require(task_digest(job) == job['package_digest'], 'package drift before allocation')
    cell = ROOT / 'cells' / job['name']
    cell.mkdir(parents=True, exist_ok=False)
    (cell / 'agent-script.sh').write_bytes(job['script'].encode())
    row = dict(name=job['name'], status='unmeasured', reward=None, reason=None,
               script_sha256=job['script_sha256'], package_digest=job['package_digest'])
    allocation(job, 'before', 6 * UNIT)
    try:
        native_runner, gate = runner()  # A distinct exact native gate and meter per cell.
        native_runner(cell, Path(job['package']), job['script'])
        row.update(validate_native(job, cell))
        row['fixed_reservations'] = gate.to_json()
    except Exception as exc:
        row.update(status='unmeasured', reward=None, reason=type(exc).__name__ + ': ' + str(exc))
        (cell / 'exception.txt').write_text(traceback.format_exc())
        row['hashes'] = {name: sha(checked(cell / name)) for name in
                         ('agent-script.sh', 'result.json', 'trial/qa-execution.json', 'trial/egress-lock.json',
                          'trial/agent/oracle.txt', 'trial/verifier/test_output.log')
                         if (cell / name).is_file()}
        row['receipt'] = str(cell / 'result.json')
        native_path = cell / 'trial/qa-execution.json'
        if native_path.is_file():
            row['native_exception'] = load(native_path).get('exception')
        raw_path = cell / 'result.json'
        detail = (str(exc) + ' ' + str(row.get('native_exception')) + ' '
                  + str(load(raw_path).get('reason') if raw_path.is_file() else ''))
        row['cap_reached'] = (isinstance(exc, SpendCapReached) or 'SpendCapReached' in detail
                              or 'remaining persisted campaign cap' in detail
                              or 'Modal cap $' in detail)
    finally:
        allocation(job, 'after', row.get('est_usd'))
        save(cell / 'measurement.json', row)
    return row


def apps():
    result = subprocess.run(['modal', 'app', 'list', '--json'], check=True, capture_output=True, text=True)
    return [r for r in json.loads(result.stdout) if r['description'] == APP and r['state'] != 'stopped']


def execute(expected, commit):
    manifest = verify_frozen(expected)
    publication(commit, manifest)
    require(not OUT.exists() and not (ROOT / 'cells').exists() and not LEDGER.exists(), 'execution namespace/ledger exists')
    require(not apps(), 'owned app name already active; refuse rather than stop another run')
    jobs = [j for j in manifest['jobs'] if j['selected_native']]
    require(len(jobs) == 10 and all(j['package'] and not j['reuse'] for j in jobs), 'invalid native selection')
    for job in jobs:
        fresh(job)
    report = dict(schema='ov1106-followup-execution-v3', publication_commit=commit, freeze_sha256=expected,
                  policy=POLICY, reused_qualification=manifest['reused_qualification'],
                  rows=[], started_at=datetime.now(UTC).isoformat())
    save(OUT, report)
    try:
        sns_controls = [j for j in jobs if j['task'] == '000682' and j['arm'] in ('reference', 'nop')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            for row in pool.map(run_job, sns_controls):
                report['rows'].append(row)
                save(OUT, report)
        require(len(sns_controls) == 2 and all(
            next(r for r in report['rows'] if r['name'] == j['name'])['reward'] ==
            (1 if j['arm'] == 'reference' else 0) for j in sns_controls), 'SNS v3 G1/G2 qualification failed')
        pending = [j for j in jobs if j not in sns_controls]
        # Submit only one pair at a time. A cap failure prevents every later pair.
        with ThreadPoolExecutor(max_workers=2) as pool:
            for offset in range(0, len(pending), 2):
                batch = list(pool.map(run_job, pending[offset:offset + 2]))
                report['rows'].extend(batch)
                save(OUT, report)
                require(not any(r.get('cap_reached') for r in batch),
                        'native spend cap reached; stop further allocations')
        if all(r['status'] == 'executed' for r in report['rows']):
            require(load(LEDGER)['reserved_usd'] == CAP,
                    '10 hardened cells must retain the complete $3 fixed commitment')
    except Exception as exc:
        report['stop_reason'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        stops = []
        try:
            for app in apps():
                stopped = subprocess.run(['modal', 'app', 'stop', '-y', app['app_id']], capture_output=True, text=True)
                stops.append(dict(app_id=app['app_id'], returncode=stopped.returncode,
                                  stdout=stopped.stdout, stderr=stopped.stderr))
            report['remaining_owned_apps'] = apps()
        except Exception as exc:
            report['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)
        report['app_stops'] = stops
        report['ledger'] = binding(LEDGER) if LEDGER.is_file() else None
        report['ledger_snapshot'] = load(LEDGER) if LEDGER.is_file() else None
        report['finished_at'] = datetime.now(UTC).isoformat()
        save(OUT, report)


def counts(rows, expected):
    measured = [r for r in rows if r['reward'] is not None]
    success = sum(r['reward'] == expected for r in measured)
    return dict(planned=len(rows), measured=len(measured), accepted=sum(r['reward'] == 1 for r in measured),
                rejected=sum(r['reward'] == 0 for r in measured), unmeasured=len(rows) - len(measured),
                successes=success, wilson95=list(old.wilson_interval(success, len(measured))) if measured else None)


def summarize(expected, commit):
    manifest = verify_frozen(expected)
    publication(commit, manifest)
    require(not AGGREGATE.exists() and not (ROOT / 'aggregate.json').exists(), 'aggregate already exists')
    execution = load(OUT)
    require(execution['publication_commit'] == commit and execution['freeze_sha256'] == expected
            and execution['policy'] == POLICY, 'execution publication mismatch')
    actual = {r['name']: r for r in execution['rows']}
    require(len(actual) == len(execution['rows']), 'duplicate execution rows')
    allowed = {j['name'] for j in manifest['jobs'] if j['selected_native']}
    require(set(actual) <= allowed, 'unexpected execution cell')
    require(execution['reused_qualification'] == manifest['reused_qualification'], 'qualification reuse changed')
    rows = []
    for job in manifest['jobs']:
        row = {k: v for k, v in job.items() if k not in ('script', 'marker')}
        row.update(status='unmeasured', reward=None, reason='not executed before experiment stop')
        if not job['package']:
            row['reason'] = manifest['ui_reason']
        elif not job['reuse'] and not job['selected_native']:
            row['reason'] = 'unselected under the $3 native followup budget; no standalone score substitution'
        elif job['reuse']:
            proof = next(r for r in manifest['historical_G1_G2'] if r['job_name'] == job['historical_name'])
            row.update({k: v for k, v in proof.items() if k not in ('job_name', 'task', 'arm')})
            row['reason'] = None
            row['historical_native_reuse'] = True
        elif job['name'] in actual:
            saved = actual[job['name']]
            cell = ROOT / 'cells' / job['name']
            require(load(cell / 'measurement.json') == saved, 'measurement row drift')
            require(saved['script_sha256'] == job['script_sha256']
                    and saved['package_digest'] == job['package_digest'], 'measurement identity drift')
            for name, digest in saved.get('hashes', {}).items():
                checked(cell / name, digest)
            row.update(saved)
            if saved['status'] == 'executed':
                proof = validate_native(job, cell, saved['hashes'])
                require(proof['reward'] == saved['reward'], 'native reward changed; refuse score substitution')
                row.update(proof)
            else:
                require(saved['reward'] is None, 'infrastructure/partial application cannot score')
        rows.append(row)
    for tid in old.TASKS:
        for arm in ('witness', 'startup'):
            rows.append(dict(task=tid, arm=arm, name=tid + '-followupv2-' + arm,
                             reward=None, status='unmeasured', reason=manifest['safety_boundary']))
    require(len(rows) == 58, 'fixed 58-row denominator changed')
    tasks = {}
    arm_gates = dict(reference=('G1', 1), nop=('G2', 0), train=('G5', 0), eval=('G6', 0), alternative=('G7', 1))
    for tid in old.TASKS:
        group = [r for r in rows if r['task'] == tid]
        gates = dict(G3=None, G4=None)
        stats = {}
        for arm, (gate, wanted) in arm_gates.items():
            selected = [r for r in group if r['arm'] == arm]
            stats[arm] = counts(selected, wanted)
            values = [r['reward'] for r in selected]
            gates[gate] = (False if any(v is not None and v != wanted for v in values)
                           else True if values and all(v == wanted for v in values) else None)
        tasks[tid] = dict(gates=gates, counts=stats, certified=False,
                          alternative_informed=tid == '000682', newly_held_out=False if tid == '000682' else None,
                          package_digest=manifest['packages'][tid].get('package_digest'),
                          contract_sha256=manifest['packages'][tid]['contract_sha256'],
                          splits={split: counts([r for r in group if r['arm'] == 'alternative' and r['split'] == split], 1)
                                  for split in ('validation', 'held_out')},
                          split_semantics='indices 0-1 validation; 2-3 historical held-out; SNS reused/informed')
    if execution.get('ledger'):
        checked(execution['ledger']['path'], execution['ledger']['sha256'])
        ledger = load(LEDGER)
        require(ledger == execution['ledger_snapshot'] and ledger['reserved_usd'] <= CAP
                and all(r['reserved_usd'] == UNIT and r['usd_per_execution'] == UNIT
                        and r['primitive'] == 'qa-execution' and 'finalized_at' not in r
                        for r in ledger['reservations']), 'fixed reservations changed or were finalized')
        if len(actual) == 10 and all(r['status'] == 'executed' for r in actual.values()):
            require(ledger['reserved_usd'] == CAP and len(ledger['reservations']) == 60,
                    'successful 10-cell run must retain all 60 units without refunds')
    report = dict(schema='ov1106-followup-aggregate-v3', tasks=tasks, rows=rows, certified=False,
                  publication_commit=commit, freeze_sha256=expected, policy=POLICY,
                  execution=binding(OUT), prepared=binding(PREPARED),
                  reused_qualification=manifest['reused_qualification'],
                  historical_G1_G2=manifest['historical_G1_G2'], current_G1_G2_sources='6 unchanged historical + 2 new SNS',
                  historical_failed_cells_preserved=str(old.ROOT),
                  candidate_counts=counts([r for r in rows if r['arm'] == 'alternative'], 1),
                  candidate_splits={s: counts([r for r in rows if r['arm'] == 'alternative' and r['split'] == s], 1)
                                    for s in ('validation', 'held_out')},
                  classification_metadata_used_for_gate_arithmetic=False,
                  budget=execution.get('ledger_snapshot'), app_stops=execution.get('app_stops'),
                  cleanup_error=execution.get('cleanup_error'), remaining_owned_apps=execution.get('remaining_owned_apps'),
                  stop_reason=execution.get('stop_reason'), safety_boundary=manifest['safety_boundary'],
                  ui_reason=manifest['ui_reason'], estimates_not_provider_invoices=True,
                  resource_caps='Exact legacy native fixed reservations, $0.05/unit, $3 cap, 240-second runner watchdog; no settlement or invoice claim.',
                  packages={tid: {k: v for k, v in p.items() if k != 'hardening'} for tid, p in manifest['packages'].items()})
    require(report['candidate_counts']['planned'] == 20, 'candidate denominator changed')
    require(sum(j['selected_native'] and j['arm'] == 'alternative' for j in manifest['jobs']) == 8,
            'selected native alternative denominator changed')
    report['native_alternatives_planned'] = 8
    report['unselected_alternatives'] = 12
    save(ROOT / 'aggregate.json', report)
    save(AGGREGATE, report)
    print(json.dumps(dict(tasks=tasks, candidates=report['candidate_counts']), sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'execute', 'summarize'))
    parser.add_argument('--freeze-sha256')
    parser.add_argument('--published-commit')
    args = parser.parse_args()
    # No gateway is launched. Keep the mandatory overlay if child CLIs inherit it.
    os.environ['PI_CONFIG_FILES'] = CONFIG
    if args.stage == 'prepare':
        require(args.freeze_sha256 is None and args.published_commit is None, 'publication flags belong to later stages')
        prepare()
    else:
        require(ROOT.is_dir(), 'parent must prepare and publish first')
        with (ROOT / 'controller.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.stage == 'execute':
                execute(args.freeze_sha256, args.published_commit)
            else:
                summarize(args.freeze_sha256, args.published_commit)


if __name__ == '__main__':
    main()
