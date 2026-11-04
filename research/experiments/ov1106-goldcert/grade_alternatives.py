"""Grade admitted blind source patches once, using the frozen public scorers.

Run with uv run --no-sync python. This is not a hardened Gold certificate.
The existing harness fixes --cells to its private receipt directory and owns
allocation logging, the cumulative $2 ledger, and the unchanged patch script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from contracts_harness import APP, REPO, ROOT, STATE, patch_script, run

HERE = Path(__file__).resolve().parent
TASKS = ('000682', '002139', '000081', '001267', '003007')
LEDGER = STATE / 'goldcert-contracts-spend.json'
sys.path.insert(0, str(REPO / 'src'))
from evallab.registry import task_directory_digest  # noqa: E402


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def checked(path, expected=None):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), f'nonregular input: {path}')
    data = path.read_bytes()
    require(expected is None or digest(data) == expected, f'hash mismatch: {path}')
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(checked(path))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')
    temporary.replace(path)


def ledger():
    value = load(LEDGER)
    require(value['max_usd'] == 2 and value['effective_spend_usd'] <= 2,
            'shared ledger cap mismatch/exhausted')
    return value


def prepare(args):
    require(args.cells.resolve() == (ROOT / 'cells').resolve(), 'harness fixes --cells to ' + str(ROOT / 'cells'))
    require(not args.out.exists(), 'output already exists; no implicit reruns')
    admitted = load(args.admission)
    require(admitted.get('generation_error') is None and admitted.get('alternative_informed') is False
            and admitted.get('contract_drift') == [] and admitted.get('planned_slots') == 20,
            'admission batch failed')
    prepared = load(args.prepared)
    checked(args.prepared, admitted['input_hashes']['prepared'])
    require(prepared['revision'] == 'v2' and admitted['cell_revision'] == 'v2'
            and set(prepared['tasks']) == set(TASKS), 'prepared cohort/revision changed')
    freeze_path = args.prepared.parent / 'contract-freeze.json'
    freeze = json.loads(checked(freeze_path, admitted['input_hashes']['freeze']))
    for name, expected in freeze.items():
        if name == 'frozen_at':
            continue
        checked(args.prepared if name == 'prepared_manifest_sha256' else HERE / name, expected)
    generation = load(args.generation / 'generation.json')
    expected_slots = {(tid, index) for tid in TASKS for index in range(4)}
    slots = {(s['task'], s['index']): s for s in admitted['slots']}
    generated = {(s['task'], s['index']): s for s in generation}
    require(len(admitted['slots']) == len(generation) == 20 and set(slots) == set(generated) == expected_slots,
            'missing/duplicate/unexpected candidate')
    for tid, task in prepared['tasks'].items():
        for version in ('original', 'gold'):
            package = task[version + '_package']
            if package is not None:
                require(task_directory_digest(Path(package)) == task[version + '_digest'], 'prepared package drift: ' + tid)
            else:
                require(tid == '001267' and version == 'gold', 'unexpected unavailable package')
    jobs = []
    for tid in TASKS:
        for index in range(4):
            slot = slots[tid, index]
            split = 'validation' if index < 2 else 'held_out'
            require(slot.get('provenance') is True and slot['split'] == split, 'slot admission failed')
            directory = args.generation / 'writers' / tid / str(index)
            receipt = json.loads(checked(directory / 'receipt.json', slot['receipt_sha256']))
            require(receipt == generated[tid, index] and receipt['task'] == tid and receipt['index'] == index
                    and receipt['frozen_split'] == split and receipt.get('eligible_for_execution') is True
                    and receipt.get('error') is None and receipt.get('cleanup_ok') is True,
                    'candidate receipt identity/admission mismatch')
            patch_path = directory / 'solution.patch'
            require(Path(receipt['patch']).resolve() == patch_path.resolve()
                    and receipt['patch_sha256'] == slot['patch_sha256'], 'patch binding mismatch')
            patch = checked(patch_path, slot['patch_sha256']).decode()
            require(bool(patch), 'empty patch')
            script = patch_script(patch, prepared['tasks'][tid]['workdir'])
            for version in ('original', 'gold'):
                name = f'{tid}-{version}-v2-alternative-{index}'
                require(not (args.cells / name).exists(), 'cell already exists; no implicit retries: ' + name)
                jobs.append(dict(task=tid, index=index, split=split, version=version, name=name,
                                 patch_sha256=slot['patch_sha256'], receipt_sha256=slot['receipt_sha256'],
                                 package=prepared['tasks'][tid][version + '_package'], script=script,
                                 patch_path=str(patch_path), candidate_receipt=str(directory / 'receipt.json'),
                                 package_digest=prepared['tasks'][tid].get(version + '_digest'),
                                 blocker=prepared['tasks'][tid].get('gold_blocker')))
    return jobs


def execute(job, cells):
    row = {key: job[key] for key in ('task', 'index', 'split', 'version', 'patch_sha256', 'receipt_sha256')}
    directory = cells / job['name']
    row.update(receipt=str(directory / 'result.json'), script=str(directory / 'agent-script.sh'),
               reward=None, status='unmeasured')
    if job['package'] is None:
        return dict(row, reason=job['blocker'], category='unavailable_scorer')
    try:
        checked(job['candidate_receipt'], job['receipt_sha256'])
        checked(job['patch_path'], job['patch_sha256'])
        require(task_directory_digest(Path(job['package'])) == job['package_digest'], 'package drift before execution')
        # Recheck the admitted bytes immediately before the allocation.
        ledger()
        result = run(job['name'], Path(job['package']), job['script'])
        require(checked(row['script']) == job['script'].encode(), 'cell script changed')
        key = digest('\0'.join((job['package_digest'], job['name'], digest(job['script'].encode()),
                                'modal-default', '', '300')).encode())
        require(result.get('cell_key') == key, 'cell key mismatch')
        row['result_sha256'] = digest(checked(row['receipt']))
        require('SOURCE_PATCH_APPLIED' in result.get('agent_log', ''), 'patch application failed or marker missing')
        require(result.get('status') == 'executed' and type(result.get('reward')) in (int, float)
                and result['reward'] in (0, 1), result.get('reason') or 'unexecuted/missing reward')
        return dict(row, status='executed', reward=result['reward'], category='measured')
    except Exception as exc:
        row.update(reason=type(exc).__name__ + ': ' + str(exc), category='execution_or_patch_error')
        save(directory / 'grading-error.json', row)
        return row


def aggregate(rows):
    output = []
    for tid in TASKS:
        for split in ('validation', 'held_out'):
            for version in ('original', 'gold'):
                group = [r for r in rows if (r['task'], r['split'], r['version']) == (tid, split, version)]
                output.append(dict(task=tid, split=split, version=version, planned=2,
                                   accepted=sum(r['reward'] == 1 for r in group),
                                   rejected=sum(r['reward'] == 0 for r in group),
                                   unmeasured=sum(r['reward'] is None for r in group)))
    return output


def stop_owned_apps():
    listing = subprocess.run(['modal', 'app', 'list', '--json'], capture_output=True, text=True, check=True)
    stops = []
    for app in json.loads(listing.stdout):
        if app['description'] == APP and app['state'] != 'stopped':
            result = subprocess.run(['modal', 'app', 'stop', '-y', app['app_id']], capture_output=True, text=True)
            stops.append(dict(app_id=app['app_id'], returncode=result.returncode,
                              stdout=result.stdout, stderr=result.stderr))
    return stops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('generation', 'admission', 'prepared', 'cells', 'out'):
        parser.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args()
    jobs = prepare(args)
    report = dict(schema='ov1106-alternative-grades-v1', started_at=datetime.now(UTC).isoformat(),
                  generation=str(args.generation.resolve()), admission=str(args.admission.resolve()),
                  admission_sha256=digest(checked(args.admission)), prepared_sha256=digest(checked(args.prepared)),
                  ledger_path=str(LEDGER), ledger_before=ledger(), rows=[])
    save(args.out, report)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, job, args.cells) for job in jobs]
            for future in as_completed(futures):
                row = future.result()
                report['rows'].append(row)
                save(args.out, report)
                print(json.dumps(row, sort_keys=True), flush=True)
    finally:
        try:
            report['app_stops'] = stop_owned_apps()
        except Exception as exc:
            report['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)
        report['ledger_after'] = ledger()
        report['rows'].sort(key=lambda r: (r['task'], r['index'], r['version']))
        report['aggregate'] = aggregate(report['rows'])
        report['finished_at'] = datetime.now(UTC).isoformat()
        save(args.out, report)
    require(len(report['rows']) == 40, 'incomplete matrix')
    require(not report.get('cleanup_error') and all(r['returncode'] == 0 for r in report['app_stops']), 'app cleanup failed')


if __name__ == '__main__':
    main()
