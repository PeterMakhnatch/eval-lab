"""Summarize the frozen certification research without inventing missing gates."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]

PRIOR = Path('/Users/petermakhnatch/Developer/eval-lab-results/2026-11-04/ov1104-grader/cert')
RUNS = {'000682': '000682-r3', '002391': '002391-r2', '002139': '002139-r2'}


def fraction(k, n):
    if not n:
        return {'numerator': k, 'denominator': n, 'rate': None, 'wilson95': None}
    z = 1.959963984540054
    d = 1 + z*z/n
    centre = (k/n + z*z/(2*n))/d
    half = z*math.sqrt(k/n*(1-k/n)/n + z*z/(4*n*n))/d
    return {'numerator': k, 'denominator': n, 'rate': k/n, 'wilson95': [max(0, centre-half), min(1, centre+half)]}


def measured(rec, target):
    return rec.get('status') == 'executed' and rec.get('reward') == target and rec.get('source_applied', True)


def compact_receipts(cell_root: Path) -> list[dict]:
    """Keep execution metadata and markers; raw third-party logs stay external."""
    rows = []
    fields = ('cell_key', 'status', 'reward', 'est_usd', 'wall_s', 'evidence_sha256', 'cache_hit')
    for path in sorted(cell_root.glob('*/result.json')):
        raw = json.loads(path.read_text())
        rows.append({
            'name': path.parent.name,
            'receipt': str(path),
            'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            **{key: raw.get(key) for key in fields},
            'source_applied': 'SOURCE_PATCH_APPLIED' in raw.get('agent_log', ''),
            'public_contract_passed': 'PUBLIC_CONTRACT_PASSED' in raw.get('agent_log', ''),
        })
    return rows


def main():
    sys.path.insert(0, str(REPO / 'src'))
    from evallab.modal_cells import compute_cell_key, sha256_text
    from evallab.registry import task_directory_digest

    evaluation = json.loads((HERE / 'evaluation.json').read_text())
    extra = HERE / 'evaluation-replication.json'
    if extra.exists():
        evaluation['002139']['alternatives'].extend(json.loads(extra.read_text()))
    advisory = HERE / 'evaluation-advisory.json'
    if advisory.exists():
        evaluation['002391']['alternatives'].extend(json.loads(advisory.read_text()))
    historical = {}
    summary = {}
    for tid, task in evaluation.items():
        old = PRIOR / RUNS[tid]
        plan = json.loads((old / 'plan.json').read_text())
        digest = task_directory_digest(old / 'package')
        controls = {}
        for name in ['reference', 'nop', 'witness-0', 'exit0_pth']:
            cell = next(c for c in plan if c['name'] == name)
            recpath = old / 'cells' / name / 'result.json'
            rec = json.loads(recpath.read_text())
            expected = compute_cell_key(package_digest=digest, variant=name,
                submission_digest=sha256_text(cell['script']), backend_image='modal-default', grader_hook='', timeout_seconds=240)
            controls[name] = {'recorded_cell_key': rec.get('cell_key'), 'expected_cell_key': expected,
                'cell_key_match': rec.get('cell_key') == expected, 'package_digest': digest,
                'script_sha256': sha256_text(cell['script']), 'timeout_seconds': 240,
                'status': rec.get('status'), 'reward': rec.get('reward'), 'receipt': str(recpath),
                'receipt_sha256': hashlib.sha256(recpath.read_bytes()).hexdigest(), 'newly_executed': False}
        historical[tid] = controls
        alternatives = task['alternatives']
        strict = [a for a in alternatives if tid != '002139' or a['index'] >= 4]
        out = {'generated_all_cohorts': len(alternatives), 'strict_instruction_only_count': len(strict),
            'original_all': fraction(sum(measured(a.get('original', {}), 1) for a in alternatives), len(alternatives)),
            'strict_original': fraction(sum(measured(a.get('original', {}), 1) for a in strict), len(strict)),
            'versions': {}}
        out['cohorts'] = {}
        for label, cohort in [('initial', [a for a in alternatives if a['index'] < 4]),
                              ('replication', [a for a in alternatives if a['index'] >= 4])]:
            if not cohort:
                continue
            frozen_eval = [a for a in cohort if a['split'] == 'eval']
            out['cohorts'][label] = {
                'original': fraction(sum(measured(a.get('original', {}), 1) for a in cohort), len(cohort)),
                'frozen_eval_original': fraction(sum(measured(a.get('original', {}), 1) for a in frozen_eval), len(frozen_eval)),
                'existing_accepted': fraction(sum(measured(a.get('existing', {}), 1) for a in cohort), len(cohort)),
                'frozen_eval_existing': fraction(sum(measured(a.get('existing', {}), 1) for a in frozen_eval), len(frozen_eval)),
            }
        if tid == '002139':
            public = [a for a in strict if 'public' in a]
            out['public_api_passed'] = fraction(sum(a['public'].get('public_contract_passed') is True for a in public), len(public))
            public_eval = [a for a in public if a['split'] == 'eval']
            out['frozen_eval_public_api_passed'] = fraction(sum(a['public'].get('public_contract_passed') is True for a in public_eval), len(public_eval))
        elif tid == '000682':
            public = json.loads((HERE / 'evaluation-sns-public.json').read_text())
            out['public_side_effects_passed'] = fraction(sum(a.get('public_contract_passed') is True for a in public), len(public))
        for version in ['existing', 'corrected']:
            if version == 'corrected' and tid == '000682':
                continue
            original_correct = [a for a in strict if measured(a.get('original', {}), 1)]
            eval_alts = [a for a in strict if a['split'] == 'eval']
            gates = task['safe_gates'] if version == 'existing' else task['corrected_gates']
            train = [r for r in gates.values() if r['arm'] == 'train']
            ev = [r for r in gates.values() if r['arm'] == 'eval']
            ref = next((r for r in gates.values() if r['arm'] == 'reference'), {})
            nop = next((r for r in gates.values() if r['arm'] == 'nop'), {})
            gate7 = (len(original_correct) >= 2 and len(eval_alts) >= 2 and
                all(measured(a.get('original', {}), 1) and measured(a.get(version, {}), 1) for a in eval_alts) and
                all(measured(a.get(version, {}), 1) for a in original_correct))
            gate_status = {'gate1_reference': 'pass' if measured(ref, 1) else 'fail_or_unmeasured',
                'gate2_nop': 'pass' if measured(nop, 0) else 'fail_or_unmeasured',
                'gate3_witness': 'unexercised', 'gate4_exit0': 'unexercised',
                'gate5_train': 'pass' if train and all(measured(r, 0) and r['marker_present'] for r in train) else 'fail_or_unmeasured',
                'gate6_eval': 'pass' if ev and all(measured(r, 0) and r['marker_present'] for r in ev) else 'fail_or_unmeasured',
                'gate7_alternatives': 'pass' if gate7 else 'fail'}
            if version == 'existing':
                for name, gate in [('witness-0', 'gate3_witness'), ('exit0_pth', 'gate4_exit0')]:
                    r = controls[name]
                    if r['cell_key_match'] and measured(r, 0):
                        gate_status[gate] = 'pass_historical_exact_binding'
            all_pass = all(x.startswith('pass') for x in gate_status.values())
            out['versions'][version] = {'gates': gate_status,
                'strict_accepted_unconditional': fraction(sum(measured(a.get(version, {}), 1) for a in strict), len(strict)),
                'strict_original_correct_accepted': fraction(sum(measured(a.get(version, {}), 1) for a in original_correct), len(original_correct)),
                'strict_frozen_eval_accepted': fraction(sum(measured(a.get(version, {}), 1) for a in eval_alts), len(eval_alts)),
                'strict_frozen_eval_original_correct': fraction(sum(measured(a.get('original', {}), 1) for a in eval_alts), len(eval_alts)),
                'train_killed': fraction(sum(measured(r, 0) for r in train), len(train)),
                'eval_killed': fraction(sum(measured(r, 0) for r in ev), len(ev)),
                'status': 'certified_with_historical_controls' if all_pass else 'failed' if gate_status['gate7_alternatives'] == 'fail' else 'provisional_missing_controls'}
        summary[tid] = out
    (HERE / 'historical-control-bindings.json').write_text(json.dumps(historical, indent=2) + '\n')
    (HERE / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    ledger = Path.home() / '.local/state/evallab-ov1104/wave2-grader-spend.json'
    spend = json.loads(ledger.read_text())
    billing_path = HERE / 'billing.json'
    billing = json.loads(billing_path.read_text()) if billing_path.exists() else {}
    (HERE / 'spend.json').write_text(json.dumps({'ledger': str(ledger), 'ledger_sha256': hashlib.sha256(ledger.read_bytes()).hexdigest(),
        'cap_usd': 2, 'runtime_estimated_usd': spend.get('settled_usd'), 'reserved_usd': spend.get('reserved_usd'),
        'executions': spend.get('executions'), 'billed_actual_usd': None,
        'actual_status': 'final invoice unavailable; runtime estimates are not invoices',
        'provider_reported_to_date_usd': billing.get('provider_reported_to_date_usd'),
        'provider_report_observed_at': billing.get('observed_at'),
        'provider_billing_status': billing.get('status'),
        'provider_billing_receipt': str(billing_path),
        'subscription_incremental_usd': 0}, indent=2) + '\n')
    cell_root = Path('/private/tmp/ov1104/wave2-grader/workers/cert/cells')
    receipts = compact_receipts(cell_root)
    (HERE / 'receipts.jsonl').write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in receipts))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
