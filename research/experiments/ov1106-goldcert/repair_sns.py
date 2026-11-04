"""Prepare an alternative-informed SNS fixture repair; execute only after publication.

Preparation is local and provider-free. Execution reuses the unchanged contracts
harness, shared $2 infrastructure ledger, and the exact existing source scripts.
No fresh generation, candidate changes, integrity controls, or certification.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from contracts_harness import APP, REPO, ROOT, patch_script, run
from grade_alternatives import (
    LEDGER, TASKS, checked, digest, ledger, load, require, save,
    stop_owned_apps, task_directory_digest,
)

HERE = Path(__file__).resolve().parent
FREEZE = HERE / 'sns-v3-freeze.json'
PREPARED = ROOT / 'sns-v3-prepared.json'
PACKAGE = ROOT / 'packages-v3/000682'
OUT = ROOT / 'sns-v3-grades.json'
GENERATION = Path('/private/tmp/ov1106/goldcert/resumed-terra-writers')
ADMISSION = Path('/private/tmp/ov1106/goldcert/pregrade-provenance-summary/summary.json')
LABEL = 'ALTERNATIVE-INFORMED; reused candidates; historical splits only, not newly held-out'


def binding(path):
    return {'path': str(Path(path).resolve()), 'sha256': digest(checked(path))}


def inventory(package):
    result = {}
    for path in sorted(package.rglob('*')):
        require(not path.is_symlink(), 'symlink in package: ' + str(path))
        if path.is_file():
            result[str(path.relative_to(package))] = digest(checked(path))
        else:
            require(path.is_dir(), 'special file in package: ' + str(path))
    return result


def validate_v2():
    admitted = load(ADMISSION)
    require(admitted.get('generation_error') is None and admitted.get('alternative_informed') is False
            and admitted.get('contract_drift') == [] and admitted.get('planned_slots') == 20,
            'original provenance admission failed')
    prepared_path = ROOT / 'prepared.json'
    prepared = json.loads(checked(prepared_path, admitted['input_hashes']['prepared']))
    freeze_path = ROOT / 'contract-freeze.json'
    freeze = json.loads(checked(freeze_path, admitted['input_hashes']['freeze']))
    require(prepared['revision'] == admitted['cell_revision'] == 'v2'
            and set(prepared['tasks']) == set(TASKS), 'v2 cohort/revision changed')
    for name, expected in freeze.items():
        if name != 'frozen_at':
            checked(prepared_path if name == 'prepared_manifest_sha256' else HERE / name, expected)
    generation = load(GENERATION / 'generation.json')
    slots = {(s['task'], s['index']): s for s in admitted['slots']}
    generated = {(s['task'], s['index']): s for s in generation}
    expected = {(tid, index) for tid in TASKS for index in range(4)}
    require(len(admitted['slots']) == len(generation) == 20 and set(slots) == set(generated) == expected,
            'missing/duplicate/unexpected candidate')
    task = prepared['tasks']['000682']
    for version in ('original', 'gold'):
        require(task_directory_digest(Path(task[version + '_package'])) == task[version + '_digest'],
                'v2 SNS package drift')
    checked(task['reference'], task['reference_sha256'])
    require([c['arm'] for c in task['controls']] == ['reference', 'nop', 'train', 'eval'],
            'unexpected source control matrix')
    candidates = []
    for index in range(4):
        split = 'validation' if index < 2 else 'held_out'
        slot = slots['000682', index]
        require(slot.get('provenance') is True and slot['split'] == split, 'slot admission failed')
        directory = GENERATION / 'writers/000682' / str(index)
        receipt_path, patch_path = directory / 'receipt.json', directory / 'solution.patch'
        receipt = json.loads(checked(receipt_path, slot['receipt_sha256']))
        require(receipt == generated['000682', index] and receipt['task'] == '000682'
                and receipt['index'] == index and receipt['frozen_split'] == split
                and receipt.get('eligible_for_execution') is True and receipt.get('error') is None
                and receipt.get('cleanup_ok') is True, 'candidate receipt admission mismatch')
        require(Path(receipt['patch']).resolve() == patch_path.resolve()
                and receipt['patch_sha256'] == slot['patch_sha256'], 'candidate patch binding mismatch')
        patch = checked(patch_path, slot['patch_sha256']).decode()
        require(bool(patch), 'empty patch')
        candidates.append(dict(index=index, split=split, candidate_status='reused/informed',
                               patch=binding(patch_path), candidate_receipt=binding(receipt_path),
                               script=patch_script(patch, task['workdir'])))
    return task, candidates


def jobs_for(task, candidates):
    jobs = []
    for control in task['controls']:
        jobs.append(dict(name='000682-gold-v3-' + control['name'], control=control['name'],
                         arm=control['arm'], script=control['script'],
                         marker={'reference': 'SOURCE_PATCH_APPLIED', 'nop': 'SAFE_NOP',
                                 'train': 'MUTANT_APPLIED', 'eval': 'MUTANT_APPLIED'}[control['arm']]))
    for candidate in candidates:
        jobs.append(dict(name='000682-gold-v3-alternative-' + str(candidate['index']),
                         control='alternative-' + str(candidate['index']), arm='alternative',
                         marker='SOURCE_PATCH_APPLIED', **candidate))
    for job in jobs:
        job['script_sha256'] = digest(job['script'].encode())
    return jobs


def prepare():
    require(not any(p.exists() for p in (FREEZE, PREPARED, PACKAGE, OUT)),
            'v3 preparation/output already exists; no overwrites')
    task, candidates = validate_v2()
    old = checked(HERE / 'contracts/000682.py').decode()
    new = checked(HERE / 'contracts-v3/000682.py').decode()
    # Enforce a fixture-only repair. Discovery, service behavior and assertions stay byte-identical.
    expected = old.replace(
        "            manager = types.SimpleNamespace(session_factory=SessionFactory(service),\n"
        "                config=Config.empty(region='us-east-1', account_id='123456789012'))\n",
        "            factory = SessionFactory(service)\n")
    expected = expected.replace(
        "                    cls({'type': name, boolean: enabled, string: selected}, manager).process([dict(r) for r in TOPICS])\n",
        "                    policy = Policy({'name': 'public-encryption', 'resource': 'sns',\n"
        "                        'actions': [{'type': name, boolean: enabled, string: selected}]},\n"
        "                        Config.empty(region='us-east-1', account_id='123456789012'), session_factory=factory)\n"
        "                    policy.resource_manager.actions[0].process([dict(r) for r in TOPICS])\n")
    require(new == expected and new != old, 'repair changes more than the manager/action fixture')
    source = Path(task['gold_package'])
    before = inventory(source)
    shutil.copytree(source, PACKAGE)
    target = PACKAGE / 'tests/public_contract.py'
    target.chmod(target.stat().st_mode | 0o200)
    target.write_bytes(new.encode())
    after = inventory(PACKAGE)
    require(set(before) == set(after)
            and [name for name in before if before[name] != after[name]] == ['tests/public_contract.py'],
            'v3 package must replace only public_contract.py')
    require(task_directory_digest(source) == task['gold_digest'], 'v2 package changed')
    jobs = jobs_for(task, candidates)
    for job in jobs:
        require(not (ROOT / 'cells' / job['name']).exists(), 'v3 cell already exists')
    inputs = [ROOT / 'prepared.json', ROOT / 'contract-freeze.json', ADMISSION,
              GENERATION / 'generation.json', ROOT / 'alternative-grades.json',
              HERE / 'control-outcomes.json', Path(task['reference'])]
    comparisons = []
    prior = load(ROOT / 'alternative-grades.json')
    for row in prior['rows']:
        if row['task'] == '000682':
            checked(row['receipt'], row['result_sha256'])
            inputs.append(Path(row['receipt']))
            comparisons.append({k: row[k] for k in ('index', 'split', 'version', 'reward', 'status', 'receipt')})
    control_comparisons = [r for r in load(HERE / 'control-outcomes.json')['rows']
                           if r['task'] == '000682']
    inputs.extend(Path(r['receipt']) for r in control_comparisons)
    for candidate in candidates:
        inputs.extend((Path(candidate['patch']['path']), Path(candidate['candidate_receipt']['path'])))
    api = ROOT / 'sns-v3-pristine-api'
    inputs.extend(api / name for name in ('policy.py', 'manager.py', 'query.py'))
    inputs.append(Path('/private/tmp/ov1106/goldcert/source-exports/000682/c7n/resources/sns.py'))
    sources = [HERE / name for name in ('repair_sns.py', 'contracts-v3/000682.py', 'grade_alternatives.py',
                                       'contracts_harness.py', 'measure_contracts.py', 'contract-grounding.json')]
    sources.extend(sorted((HERE / 'contracts').iterdir()))
    sources.extend(REPO / name for name in ('src/evallab/modal_cells.py', 'src/evallab/registry.py',
                                           'src/evallab/regrade.py', 'src/evallab/qa/task_modal.py'))
    manifest = dict(schema='ov1106-sns-v3-prepared-v1', created_at=datetime.now(UTC).isoformat(),
                    task='000682', revision='v3', alternative_informed=True, label=LABEL,
                    informed_by='000682 validation index 0 manager.retry failure; all v2 scores already observed',
                    candidate_status='reused/informed', newly_held_out=False,
                    certification_possible=False, G3='unexercised', G4='unexercised',
                    package=str(PACKAGE), package_digest=task_directory_digest(PACKAGE),
                    package_files=after, v2_package=str(source), v2_package_digest=task['gold_digest'],
                    package_change=['tests/public_contract.py'], jobs=jobs,
                    v2_alternatives=comparisons, v2_controls=control_comparisons,
                    input_files=[binding(p) for p in dict.fromkeys(inputs)],
                    source_files=[binding(p) for p in sources],
                    ledger_path=str(LEDGER), ledger_at_prepare={k: ledger()[k] for k in
                        ('effective_spend_usd', 'cumulative_spend_usd', 'executions', 'max_usd')},
                    max_remote_cells=2, output=str(OUT))
    save(PREPARED, manifest)
    freeze = dict(schema='ov1106-sns-v3-freeze-v1', frozen_at=datetime.now(UTC).isoformat(),
                  label=LABEL, alternative_informed=True, newly_held_out=False,
                  certification_possible=False, prepared=binding(PREPARED),
                  source_files=manifest['source_files'], package_digest=manifest['package_digest'],
                  candidates=[{k: c[k] for k in ('index', 'split', 'candidate_status', 'patch', 'candidate_receipt')}
                              for c in candidates])
    save(FREEZE, freeze)
    # Read-only packages and manifests are private immutable evidence, not Git permissions.
    for path in sorted(PACKAGE.rglob('*'), reverse=True):
        path.chmod(path.stat().st_mode & ~0o222)
    PACKAGE.chmod(PACKAGE.stat().st_mode & ~0o222)
    PREPARED.chmod(0o444)
    print(json.dumps(dict(freeze=binding(FREEZE), prepared=binding(PREPARED),
                         package=str(PACKAGE), package_digest=manifest['package_digest'],
                         cells=len(jobs), label=LABEL), sort_keys=True))


def verify_frozen(expected_hash):
    freeze = json.loads(checked(FREEZE, expected_hash))
    require(freeze['alternative_informed'] is True and freeze['newly_held_out'] is False,
            'v3 informed label missing')
    manifest = json.loads(checked(freeze['prepared']['path'], freeze['prepared']['sha256']))
    require(manifest['source_files'] == freeze['source_files'] and manifest['max_remote_cells'] == 2
            and manifest['output'] == str(OUT) and manifest['ledger_path'] == str(LEDGER),
            'frozen execution contract mismatch')
    for item in manifest['source_files'] + manifest['input_files']:
        checked(item['path'], item['sha256'])
    require(manifest['package'] == str(PACKAGE) and inventory(PACKAGE) == manifest['package_files']
            and task_directory_digest(PACKAGE) == manifest['package_digest'] == freeze['package_digest'],
            'v3 package drift')
    task, candidates = validate_v2()
    require(jobs_for(task, candidates) == manifest['jobs'], 'frozen script/patch/admission matrix drift')
    return manifest


def require_publication(commit, manifest):
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'full published commit SHA required')
    files = [FREEZE] + [Path(item['path']) for item in manifest['source_files']]
    for path in files:
        relative = path.relative_to(REPO).as_posix()
        published = subprocess.run(['git', 'show', commit + ':' + relative], cwd=REPO,
                                   check=True, capture_output=True).stdout
        require(published == checked(path), 'published source/freeze differs: ' + relative)
    remote = subprocess.run(['git', 'ls-remote', '--heads', 'origin'], cwd=REPO,
                            check=True, capture_output=True, text=True)
    tips = [line.split()[0] for line in remote.stdout.splitlines()]
    require(any(subprocess.run(['git', 'merge-base', '--is-ancestor', commit, tip], cwd=REPO,
                               capture_output=True).returncode == 0 for tip in tips),
            'freeze commit is not on a published origin branch')


def cell_key(job, package_digest):
    return digest('\0'.join((package_digest, job['name'], job['script_sha256'],
                             'modal-default', '', '300')).encode())


def run_job(job, manifest):
    directory = ROOT / 'cells' / job['name']
    row = {k: v for k, v in job.items() if k not in ('script', 'marker')}
    row.update(task='000682', version='gold', revision='v3', alternative_informed=True,
               candidate_status='reused/informed', newly_held_out=False,
               status='unmeasured', reward=None, category='infrastructure_or_binding_error',
               receipt=str(directory / 'result.json'), script=str(directory / 'agent-script.sh'))
    try:
        for key in ('patch', 'candidate_receipt'):
            if key in job:
                checked(job[key]['path'], job[key]['sha256'])
        require(digest(job['script'].encode()) == job['script_sha256'], 'script drift')
        require(task_directory_digest(PACKAGE) == manifest['package_digest'], 'package drift before allocation')
        ledger()
        result = run(job['name'], PACKAGE, job['script'])
        require(checked(row['script']) == job['script'].encode(), 'executed script differs')
        require(result.get('cell_key') == cell_key(job, manifest['package_digest']), 'cell/package/script binding differs')
        row['result_sha256'] = digest(checked(row['receipt']))
        require(not result.get('cache_hit'), 'existing cached cell refused')
        require(job['marker'] in result.get('agent_log', ''), 'existing applied marker missing')
        require(result.get('status') == 'executed' and type(result.get('reward')) in (int, float)
                and result['reward'] in (0, 1), result.get('reason') or 'unexecuted/missing reward')
        row.update(status='executed', reward=result['reward'], category='measured')
    except Exception as error:
        row['reason'] = type(error).__name__ + ': ' + str(error)
        save(directory / 'grading-error.json', row)
    return row


def execute(args):
    require(args.freeze_sha256 and args.published_commit, 'execute requires exact freeze hash and published commit')
    manifest = verify_frozen(args.freeze_sha256)
    require_publication(args.published_commit, manifest)
    with (ROOT / 'sns-v3-execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not OUT.exists(), 'v3 output exists; no retry')
        for job in manifest['jobs']:
            require(not (ROOT / 'cells' / job['name']).exists(), 'existing v3 cell refused')
            require(not (ROOT / 'cell-cache' / cell_key(job, manifest['package_digest'])).exists(),
                    'existing keyed v3 cache refused')
        active = json.loads(subprocess.run(['modal', 'app', 'list', '--json'], check=True,
                                           capture_output=True, text=True).stdout)
        require(not any(a['description'] == APP and a['state'] != 'stopped' for a in active),
                'shared harness app already active; cannot establish ownership')
        report = dict(schema='ov1106-sns-v3-grades-v1', label=LABEL, alternative_informed=True,
                      candidate_status='reused/informed', newly_held_out=False, certification_possible=False,
                      published_commit=args.published_commit, freeze_sha256=args.freeze_sha256,
                      package_digest=manifest['package_digest'], ledger_path=str(LEDGER),
                      ledger_before=ledger(), started_at=datetime.now(UTC).isoformat(),
                      rows=[dict(name=j['name'], control=j['control'], arm=j['arm'],
                                 status='unmeasured', reward=None, reason='not completed') for j in manifest['jobs']],
                      v2_alternatives=manifest['v2_alternatives'], v2_controls=manifest['v2_controls'])
        save(OUT, report)
        try:
            # Reserve every name locally before any allocation; interrupted matrices cannot silently retry.
            for job in manifest['jobs']:
                (ROOT / 'cells' / job['name']).mkdir()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(run_job, job, manifest): index
                           for index, job in enumerate(manifest['jobs'])}
                for future in as_completed(futures):
                    report['rows'][futures[future]] = future.result()
                    save(OUT, report)
        finally:
            try:
                report['app_stops'] = stop_owned_apps()
            except Exception as error:
                report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            report['ledger_after'] = ledger()
            report['finished_at'] = datetime.now(UTC).isoformat()
            report['aggregate'] = [dict(arm=arm, planned=len(group),
                accepted=sum(r['reward'] == 1 for r in group), rejected=sum(r['reward'] == 0 for r in group),
                unmeasured=sum(r['reward'] is None for r in group))
                for arm in ('reference', 'nop', 'train', 'eval', 'alternative')
                if (group := [r for r in report['rows'] if r['arm'] == arm])]
            report['v2_comparison'] = [dict(index=row['index'], historical_split=row['split'],
                candidate_status='reused/informed', v3_reward=row['reward'],
                v2_gold_reward=next(r['reward'] for r in manifest['v2_alternatives']
                                    if r['version'] == 'gold' and r['index'] == row['index']),
                v2_original_reward=next(r['reward'] for r in manifest['v2_alternatives']
                                        if r['version'] == 'original' and r['index'] == row['index']))
                for row in report['rows'] if row['arm'] == 'alternative' and 'index' in row]
            save(OUT, report)
        print(json.dumps({k: report[k] for k in ('label', 'aggregate', 'v2_comparison')}, sort_keys=True))
        require(not report.get('cleanup_error') and all(r['returncode'] == 0 for r in report['app_stops']),
                'owned app cleanup failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'execute'))
    parser.add_argument('--freeze-sha256')
    parser.add_argument('--published-commit')
    args = parser.parse_args()
    prepare() if args.mode == 'prepare' else execute(args)


if __name__ == '__main__':
    main()
