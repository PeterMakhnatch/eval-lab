"""Verify retained native measurements offline, without importing experiment controllers.

python research/experiments/ov1106-goldcert/verify_retained.py [--lane ARCHIVED_LANE]
Only the output file is writable; --out uses exclusive creation.
"""
import argparse
import hashlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

OLD = Path('/private/tmp/ov1106/goldcert')
TASKS = ('000081', '000682', '001267', '002139', '003007')
FILES = ('agent-script.sh', 'result.json', 'trial/qa-execution.json',
         'trial/egress-lock.json', 'trial/agent/oracle.txt', 'trial/verifier/test_output.log')
ARMS = dict(reference=('G1', 1), nop=('G2', 0), train=('G5', 0),
            eval=('G6', 0), alternative=('G7', 1))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def counts(rows, wanted=1):
    values = [r['reward'] for r in rows if r['reward'] is not None]
    n, k = len(values), sum(v == wanted for v in values)
    interval = None
    if n:
        z = 1.959963984540054
        center = (k / n + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(k / n * (1 - k / n) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = [center - half, center + half]
    return dict(planned=len(rows), measured=n, accepted=sum(v == 1 for v in values),
                rejected=sum(v == 0 for v in values), unmeasured=len(rows) - n,
                successes=k, wilson95=interval)


def compare_counts(actual, recorded):
    need({k: v for k, v in actual.items() if k != 'wilson95'} ==
         {k: v for k, v in recorded.items() if k != 'wilson95'}, 'count mismatch')
    if actual['wilson95'] is None:
        need(recorded['wilson95'] is None, 'unexpected interval')
    else:
        need(len(recorded['wilson95']) == 2 and all(abs(x - y) < 1e-6 for x, y in
             zip(actual['wilson95'], recorded['wilson95'], strict=True)), 'interval mismatch')


class Retained:
    def __init__(self, lane):
        self.lane = lane.resolve()
        self.hashes = {}
        self.native = {}

    def mapped(self, recorded):
        path = Path(recorded)
        need(path.is_absolute() and '..' not in path.parts, 'unsafe recorded path: ' + str(path))
        need(path.is_relative_to(OLD), 'input outside retained lane: ' + str(path))
        mapped = self.lane / path.relative_to(OLD)
        need(mapped.resolve().is_relative_to(self.lane), 'path escapes lane: ' + str(path))
        return mapped

    def read(self, recorded, expected=None):
        path = self.mapped(recorded)
        need(path.is_file() and not path.is_symlink(), 'missing/nonregular file: ' + str(path))
        data = path.read_bytes()
        if expected is not None:
            need(digest(data) == expected, 'hash mismatch: ' + str(recorded))
            self.hashes[str(recorded)] = expected
        return data

    def load(self, recorded, expected=None):
        return json.loads(self.read(recorded, expected))

    def bound(self, binding):
        return self.load(binding['path'], binding['sha256'])

    def native_row(self, row, job):
        cell = Path(row['receipt']).parent
        need(cell.name == job['name'] and Path(row['receipt']).name == 'result.json', 'cell name mismatch')
        hashes = row['hashes']
        need(set(FILES) <= set(hashes), 'missing native hash declaration')
        data = {}
        for name, sha in hashes.items():
            part = Path(name)
            need(not part.is_absolute() and '..' not in part.parts, 'unsafe raw hash path')
            data[name] = self.read(cell / part, sha)
        need(digest(data['agent-script.sh']) == job['script_sha256'] == row.get('script_sha256', job['script_sha256']),
             'agent script digest mismatch')
        if 'script' in job:
            need(data['agent-script.sh'] == job['script'].encode(), 'script bytes mismatch')
        raw, native, egress = (json.loads(data[name]) for name in FILES[1:4])
        reward = raw.get('reward')
        need(all(type(x) in (int, float) and x in (0, 1) for x in
                 (reward, row['reward'], native['reward'])) and reward == row['reward'] == native['reward'],
             'raw/native/aggregate reward mismatch: ' + job['name'])
        need(raw.get('status') == row['status'] == 'executed' and raw.get('cache_hit') is False
             and raw['reason'] is None and native['exception'] is None
             and native.get('environment') == 'modal' and native.get('mode') == 'shortcut'
             and native.get('package_digest') == job['package_digest']
             and native.get('job_path') == str(cell / 'trial'), 'native identity/status mismatch')
        if job.get('cloud'):
            cloud = native.get('cloud_spend') or {}
            need(all(cloud.get(k) == val for k, val in job['cloud'].items()), 'native budget identity mismatch')
        if 'cell_key' in row:
            need(raw.get('cell_key') == row['cell_key'], 'cell key mismatch')
        log, output = (data[name].decode() for name in FILES[4:])
        need(raw.get('agent_log') == log and raw.get('test_output_tail') == output[-600:]
             and raw.get('evidence_sha256') == digest((log + output).encode()), 'native log mismatch')
        if job.get('marker'):
            marker = job['marker']
            n = job['script'].count(marker)
            need(n > 0 and log.count(marker) == native.get('agent_stdout', '').count(marker) == n,
                 'partial script application')
        need(egress.get('schema_version') == 2 and egress.get('requested') is True
             and egress['error'] is None and any(
                 r.get('source') == 'creation' and r.get('network_mode') in ('public', 'allowlist', 'no-network')
                 and isinstance(r.get('sandbox_id'), str) and r['sandbox_id'].startswith('sb-')
                 for r in egress.get('applications', [])), 'invalid creation receipt')
        self.native[str(cell)] = reward
        return reward


def ledger_report(ledger):
    rows = ledger['reservations']
    ids = [r['reservation_id'] for r in rows]
    need(len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids), 'duplicate reservation')
    total = sum((Decimal(str(r['reserved_usd'])) for r in rows), Decimal(0))
    need(total == Decimal(str(ledger['reserved_usd'])) <= Decimal(str(ledger['max_usd'])), 'ledger balance mismatch')
    need(all(r['reserved_usd'] == r['usd_per_execution'] and r['primitive'] == 'qa-execution'
             and 'finalized_at' not in r for r in rows), 'not retained fixed reservations')
    return dict(reservations=len(rows), reservation_ids=ids, sum_reserved_usd=float(total),
                reserved_usd=ledger['reserved_usd'], max_usd=ledger['max_usd'])


def verify(args):
    v = Retained(args.lane)
    base = args.repo.resolve() / 'research/experiments/ov1106-goldcert'
    aggregate_path = args.aggregate or base / 'followup-aggregate.json'
    a = json.loads(aggregate_path.read_bytes())
    freeze = json.loads((base / 'followup-freeze.json').read_bytes())
    need(digest((base / 'followup-freeze.json').read_bytes()) == a['freeze_sha256'], 'freeze mismatch')
    need(a['prepared'] == freeze['prepared'], 'prepared binding mismatch')
    p = v.bound(a['prepared'])
    execution = v.bound(a['execution'])
    need(p['policy'] == a['policy'] == execution['policy'] == freeze['policy']
         and execution['publication_commit'] == a['publication_commit']
         and execution['freeze_sha256'] == a['freeze_sha256'], 'execution binding mismatch')
    # Only retained inputs are opened. Original packages and live ledgers are not required.
    bindings = {item['path']: item['sha256'] for item in p['inputs']}
    def retained_input(relative):
        path = str(OLD / relative)
        need(path in bindings, 'missing frozen input binding: ' + relative)
        return v.load(path, bindings[path])
    contracts = retained_input('helpers/contracts/prepared.json')
    admission = retained_input('pregrade-provenance-summary/summary.json')
    def frozen_json(name):
        suffix = '/research/experiments/ov1106-goldcert/' + name
        sha = next(item['sha256'] for item in p['source_files'] if item['path'].endswith(suffix))
        data = (base / name).read_bytes()
        need(digest(data) == sha, 'frozen aggregate/manifest mismatch: ' + name)
        return json.loads(data)
    old_manifest = frozen_json('contained-manifest.json')
    old_aggregate = frozen_json('contained-aggregate.json')
    plan = frozen_json('followup-budget-plan.json')
    need(set(plan['planned_denominator_tasks']) == set(TASKS) and plan['candidate_indices'] == list(range(4)),
         'bounded cohort plan mismatch')
    transport = retained_input('resumed-terra-writers/frozen/binding.json')
    writer_manifest = retained_input('resumed-terra-writers/frozen/manifest.json')
    public_tasks = {t['id']: t for t in writer_manifest['tasks']}
    need(set(public_tasks) == set(TASKS), 'public source cohort mismatch')
    old_cloud = {k: old_aggregate['spend'][k] for k in
                 ('approval', 'billing_tag', 'cap_usd', 'modal_app_name', 'spend_ledger', 'usd_per_execution')}
    policy = a['policy']
    new_cloud = dict(approval=policy['approval'], billing_tag=policy['billing_tag'],
                     cap_usd=policy['cap_usd'], modal_app_name=policy['app'],
                     spend_ledger=policy['ledger'], usd_per_execution=policy['unit_usd'])
    need(a['packages'] == {t: {k: val for k, val in package.items() if k != 'hardening'}
                          for t, package in p['packages'].items()}, 'aggregate package record mismatch')
    need(set(a['tasks']) == set(p['packages']) == set(contracts['tasks']) == set(TASKS), 'task cohort mismatch')
    jobs = {j['name']: j for j in p['jobs']}
    expected = set(jobs) | {t + '-followupv2-' + arm for t in TASKS for arm in ('witness', 'startup')}
    rows = a['rows']
    need(len(jobs) == len(p['jobs']) and len(rows) == len(expected)
         and {r['name'] for r in rows} == expected, 'missing/duplicate cohort row')
    need(len({(r['task'], r['arm'], r.get('control', r['arm'])) for r in rows}) == len(rows), 'duplicate control')
    frozen_mutants = {(t, c['arm'], c['name'], c.get('mutant_sha256', c['name'].split('-', 1)[1]))
                      for t in TASKS for c in contracts['tasks'][t]['controls'] if c['arm'] in ('train', 'eval')}
    actual_mutants = {(r['task'], r['arm'], r['control'], r.get('mutant_sha256'))
                      for r in rows if r['arm'] in ('train', 'eval')}
    need(actual_mutants == frozen_mutants, 'frozen mutant pool mismatch')
    candidates = [r for r in rows if r['arm'] == 'alternative']
    slots = {(s['task'], s['index']): s for s in admission['slots']}
    expected_slots = {(t, i) for t in TASKS for i in range(4)}
    need(len(slots) == len(admission['slots']) == len(candidates) and set(slots) == expected_slots
         and {(r['task'], r['index']) for r in candidates} == expected_slots, 'candidate cohort mismatch')
    for row in candidates:
        tid, index = row['task'], row['index']
        split = 'validation' if index < 2 else 'held_out'
        slot = slots[tid, index]
        need(row['split'] == slot['split'] == split and row['alternative_informed'] == (tid == '000682')
             and row['newly_held_out'] is False, 'candidate split/informed mismatch')
        directory = OLD / 'resumed-terra-writers/writers' / tid / str(index)
        receipt = v.load(directory / 'receipt.json', slot['receipt_sha256'])
        v.read(directory / 'solution.patch', slot['patch_sha256'])
        need(receipt['task'] == tid and receipt['index'] == index and receipt['frozen_split'] == split
             and receipt['patch'] == str(directory / 'solution.patch')
             and receipt['patch_sha256'] == row['patch_sha256'] == slot['patch_sha256']
             and row['receipt_sha256'] == slot['receipt_sha256'] and slot['provenance'] is True
             and receipt['test_aware'] is False and receipt['eligible_for_execution'] is True
             and receipt['cleanup_ok'] is True and receipt['error'] is None
             and receipt['quarantined_patch'] is None and receipt['tool_names'] == ['bash']
             and receipt['models_served'] == [receipt['model_requested']], 'source-only admission mismatch')
        public = public_tasks[tid]
        need(receipt['binding'] == transport and all(receipt.get(k) == val for k, val in transport.items())
             and receipt['instruction_sha256'] == public['instruction_sha256']
             and receipt['source_files'] == public['source_hashes']
             and receipt['modified_paths'] and all(
                 not Path(path).is_absolute() and '..' not in Path(path).parts
                 and any(path.startswith(prefix) for prefix in public['allowed_prefixes'])
                 for path in receipt['modified_paths']), 'source-only identity mismatch')
    execution_rows = {r['name']: r for r in execution['rows']}
    need(len(execution_rows) == len(execution['rows']) and set(execution_rows) <=
         {j['name'] for j in jobs.values() if j['selected_native']}, 'execution cohort mismatch')
    for row in rows:
        need(row['task'] in TASKS, 'unknown task')
        if row['name'] not in jobs:
            need(row['reward'] is None and row['status'] == 'unmeasured', 'integrity controls must remain null')
            continue
        job = jobs[row['name']]
        need(all(row.get(k) == val for k, val in job.items() if k not in ('script', 'marker')), 'job identity mismatch: ' + row['name'])
        package = p['packages'][row['task']]
        need(row['package_digest'] == package.get('package_digest') and row['package'] == package['package'], 'package identity mismatch')
        if row['reward'] is None:
            need(row['status'] == 'unmeasured', 'null reward with executed status')
            need(not row.get('reuse') and row['name'] not in execution_rows, 'missing measured reward')
        else:
            need(job['reuse'] or job['selected_native'], 'unselected native reward')
            actual_job = dict(job, name=job['historical_name']) if job['reuse'] else job
            actual_job = dict(actual_job, cloud=old_cloud if job['reuse'] else new_cloud)
            v.native_row(row, actual_job)
            if not job['reuse']:
                saved = execution_rows[row['name']]
                need(v.load(Path(row['receipt']).with_name('measurement.json')) == saved
                     and all(row.get(k) == val for k, val in saved.items()), 'measurement mismatch')
                fixed = saved['fixed_reservations']
                need(fixed['executions'] == policy['units_per_hardened_cell']
                     and Decimal(str(fixed['this_gate_reserved_usd'])) ==
                     Decimal(str(policy['unit_usd'])) * policy['units_per_hardened_cell'],
                     'per-cell fixed units mismatch')
    tasks = {}
    for tid in TASKS:
        group = [r for r in rows if r['task'] == tid]
        gates, stats = dict(G3=None, G4=None), {}
        for arm, (gate, wanted) in ARMS.items():
            subset = [r for r in group if r['arm'] == arm]
            stats[arm] = counts(subset, wanted)
            compare_counts(stats[arm], a['tasks'][tid]['counts'][arm])
            values = [r['reward'] for r in subset]
            gates[gate] = (False if any(x is not None and x != wanted for x in values)
                           else True if values and all(x == wanted for x in values) else None)
        splits = {s: counts([r for r in group if r['arm'] == 'alternative' and r['split'] == s])
                  for s in ('validation', 'held_out')}
        for s, stat in splits.items():
            compare_counts(stat, a['tasks'][tid]['splits'][s])
        need(gates == a['tasks'][tid]['gates'] and a['tasks'][tid]['certified'] is False, 'gate/certificate mismatch')
        record, package = a['tasks'][tid], p['packages'][tid]
        need(record['package_digest'] == package.get('package_digest')
             and record['contract_sha256'] == package['contract_sha256']
             and record['alternative_informed'] == (tid == '000682'), 'task package/informed mismatch')
        integrity = {arm: counts([r for r in group if r['arm'] == arm], 0)
                     for arm in ('witness', 'startup')}
        tasks[tid] = dict(gates=gates, counts=stats, splits=splits, integrity_counts=integrity,
                          alternative_informed=tid == '000682', certified=False)
    need(a['certified'] is False, 'incomplete certification')
    candidate_counts = counts(candidates)
    compare_counts(candidate_counts, a['candidate_counts'])
    splits = {s: counts([r for r in candidates if r['split'] == s]) for s in ('validation', 'held_out')}
    for s, stat in splits.items():
        compare_counts(stat, a['candidate_splits'][s])
    selected = [r for r in candidates if r['selected_native']]
    need(len(selected) == a['native_alternatives_planned'] and len(candidates) - len(selected) == a['unselected_alternatives'], 'selection denominator mismatch')
    # Verify all historical measured controls, including the superseded SNS controls.
    historical = a['historical_G1_G2']
    need(historical == p['historical_G1_G2'] and len({r['job_name'] for r in historical}) == len(historical), 'historical cohort mismatch')
    need({r['job_name'] for r in historical} == {r['name'] for r in old_aggregate['rows']
         if r['arm'] in ('reference', 'nop') and r['reward'] is not None}, 'missing historical control')
    for row in historical:
        control = next(c for c in contracts['tasks'][row['task']]['controls'] if c['arm'] == row['arm'])
        job = dict(name=row['job_name'], script=control['script'], script_sha256=digest(control['script'].encode()),
                   package_digest=old_manifest['tasks'][row['task']]['package_digest'], cloud=old_cloud)
        v.native_row(row, job)
    q = a['reused_qualification']
    need(q == p['reused_qualification'] == execution['reused_qualification'], 'qualification reuse mismatch')
    qualification = v.bound(q['receipt'])
    need(qualification['qualified'] is True, 'unqualified runner')
    outcome = qualification['outcome']
    qjob = dict(name=outcome['name'], package_digest=outcome['package_digest'],
                script_sha256=q['outcome']['hashes']['agent-script.sh'])
    need(outcome['package_digest'] == contracts['tasks']['000081']['original_digest'], 'qualification package mismatch')
    qjob.update(script='set -eu\nprintf "CONTAINED_INERT_NOP\\n"\n', cloud=old_cloud)
    v.native_row(q['outcome'], qjob)
    # Count every recorded initial native result, including failures and the UI probe.
    # The probe is runtime evidence, never a G1/G2 or alternative measurement.
    initial_results = []
    for row in old_aggregate['rows']:
        if 'result_sha256' not in row:
            continue
        raw = v.load(row['receipt'], row['result_sha256'])
        need(raw['reward'] == row['reward'], 'historical raw reward mismatch')
        cell = Path(row['receipt']).parent
        for name in ('qa-execution.json', 'egress-lock.json'):
            if name + '_sha256' in row:
                v.read(cell / 'trial' / name, row[name + '_sha256'])
        initial_results.append(raw)
    ui = retained_input('helpers/safe/measurement/ui-runtime-probe.json')['row']
    ui_raw = v.load(ui['receipt'], ui['result_sha256'])
    ui_cell = Path(ui['receipt']).parent
    ui_native = v.load(ui_cell / 'trial/qa-execution.json', ui['qa-execution.json_sha256'])
    ui_egress = v.load(ui_cell / 'trial/egress-lock.json', ui['egress-lock.json_sha256'])
    need(ui_raw['reward'] == ui_native['reward'] == ui['reward'] and ui_raw['status'] == 'executed'
         and ui_native['environment'] == 'modal' and ui_native['mode'] == 'shortcut'
         and ui_native['exception'] is None and ui_native['job_path'] == str(ui_cell / 'trial')
         and ui_native['package_digest'] == contracts['tasks']['001267']['original_digest']
         and ui_egress['schema_version'] == 2 and ui_egress['error'] is None
         and any(r.get('source') == 'creation' and r.get('sandbox_id', '').startswith('sb-')
                 for r in ui_egress['applications']), 'UI runtime reward identity mismatch')
    initial_results.extend((ui_raw, dict(reward=q['outcome']['reward'])))
    raw_results = dict(initial=counts(initial_results),
                       followup=counts(list(execution_rows.values())))
    # Retained snapshots replace inaccessible live state paths, with the original frozen hashes.
    old_snapshot = OLD / 'helpers/safe/measurement/spend-snapshot.json'
    old_ledger_key = next(k for k in bindings if k.endswith('/goldcert-contained-spend.json'))
    legacy = v.load(old_snapshot, bindings[old_ledger_key])
    need(execution['ledger_snapshot'] == a['budget'], 'followup ledger mismatch')
    followup = a['budget']
    need(digest((json.dumps(followup, indent=2, sort_keys=True) + '\n').encode()) == execution['ledger']['sha256'], 'followup ledger snapshot hash mismatch')
    ledgers = dict(initial_native=ledger_report(legacy), followup_native=ledger_report(followup))
    need(not (set(ledgers['initial_native']['reservation_ids']) & set(ledgers['followup_native']['reservation_ids'])), 'cross-ledger duplicate reservation')
    need(all(r['usd_per_execution'] == policy['unit_usd'] for ledger in (legacy, followup)
             for r in ledger['reservations']), 'fixed unit price mismatch')
    need(legacy['reserved_usd'] == plan['slice_caps_usd']['initial_contained']
         and followup['max_usd'] == plan['slice_caps_usd']['native_followup'], 'slice cap mismatch')
    native_by_phase = {phase: counts([dict(reward=value) for cell, value in v.native.items()
                       if ('/followup/cells/' in cell) == (phase == 'followup')])
                       for phase in ('initial', 'followup')}
    gate_totals = {g: dict(planned=len(tasks), passed=sum(t['gates'][g] is True for t in tasks.values()),
                          failed=sum(t['gates'][g] is False for t in tasks.values()),
                          unmeasured=sum(t['gates'][g] is None for t in tasks.values()))
                   for g in ('G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7')}
    return dict(verified=True, aggregate=str(aggregate_path), aggregate_sha256=digest(aggregate_path.read_bytes()),
                tasks=tasks, all_rows=counts(rows), candidates=candidate_counts, candidate_splits=splits,
                gate_totals=gate_totals,
                split_semantics='Indices 0-1 validation; 2-3 historical held-out. SNS is alternative-informed, not newly held-out.',
                selected_native_alternatives=counts(selected), mutant_pool_members=len(frozen_mutants),
                historical_controls=counts(historical), certifications=sum(t['certified'] for t in tasks.values()),
                native_ledgers=ledgers, total_native_reserved_usd=sum(x['sum_reserved_usd'] for x in ledgers.values()),
                total_native_reservations=sum(x['reservations'] for x in ledgers.values()),
                verified_native_cells=len(v.native), verified_native_rewards=counts([dict(reward=x) for x in v.native.values()]),
                verified_native_rewards_by_phase=native_by_phase,
                recorded_native_raw_results=raw_results,
                checked_file_hashes=len(v.hashes), raw_native_file_hashes=sum('/cells/' in x for x in v.hashes),
                limits=['Estimates are not invoices.', 'Public egress does not establish network isolation.',
                        'No full correctness or certification claim; G3/G4 remain unmeasured.',
                        'No native G5/G6 inference from standalone results.',
                        'Package identity strings are checked; official package byte digests are excluded.',
                        'No live publication check; frozen publication identity is retained, not re-proved.',
                        'Verified native rewards cover followup, historical G1/G2 and qualification; UI probes and original-grader cells are excluded.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', type=Path, default=OLD)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--aggregate', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        report = verify(args)
        text = json.dumps(report, indent=2, sort_keys=True) + '\n'
        if args.out:
            with args.out.open('x') as handle:
                handle.write(text)
        print(text, end='')
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as exc:
        print(json.dumps(dict(verified=False, error=str(exc))), file=sys.stdout)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
