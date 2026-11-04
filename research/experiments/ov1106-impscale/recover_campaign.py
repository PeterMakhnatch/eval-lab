"""Reconstruct an interrupted campaign summary without changing original evidence.

This command starts no agent, quota probe, grader, or container. The parent must
supply a preserved interruption receipt. It never writes summary.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def need(condition, message):
    if not condition:
        raise ValueError(message)


def reconstruct(root, plan_path, interruption_path):
    root = Path(root).resolve()
    inputs = {}

    def load(path):
        path = Path(path).resolve()
        body = path.read_bytes()
        inputs[str(path)] = hashlib.sha256(body).hexdigest()
        return json.loads(body)

    campaign = load(root / 'campaign.json')
    plan = load(plan_path)
    need(inputs[str(Path(plan_path).resolve())] == campaign['plan_sha256'], 'plan digest differs')
    plan = plan[:campaign['max_runs']]
    planned_ids = [row['id'] for row in plan]
    need(len(plan) == campaign['planned'] and len(set(planned_ids)) == len(plan), 'invalid campaign plan')
    need(not (root / 'summary.json').exists(), 'original terminal summary exists; recovery forbidden')
    interruption = load(interruption_path)
    need(interruption['schema'] == 'ov1106.scale-interruption/v1', 'unknown interruption schema')
    need(Path(interruption['campaign']).resolve() == root, 'interruption campaign differs')
    need(interruption['campaign_sha256'] == inputs[str(root / 'campaign.json')], 'interruption campaign digest differs')
    need(interruption['exception_type'] == 'subprocess.CalledProcessError'
         and type(interruption['returncode']) is int and interruption['returncode'] != 0
         and interruption['command'][1:] == ['usage', '--json', '--redact'], 'unsupported observed interruption')
    outcomes_path = root / 'outcomes.jsonl'
    body = outcomes_path.read_bytes()
    inputs[str(outcomes_path)] = hashlib.sha256(body).hexdigest()
    need(interruption['outcomes_sha256'] == inputs[str(outcomes_path)], 'interruption outcomes digest differs')
    outcomes = [json.loads(line) for line in body.splitlines() if line.strip()]
    ids = [row['id'] for row in outcomes]
    need(len(ids) == len(set(ids)) and set(ids) <= set(planned_ids), 'duplicate or unplanned outcome')
    need(all(row['status'] in ('complete', 'error') for row in outcomes), 'unknown saved outcome')
    next_index = interruption['next_plan_index']
    need(type(next_index) is int and 0 <= next_index < len(plan)
         and len(ids) == next_index and set(ids) == set(planned_ids[:next_index]), 'interruption does not follow completed batch prefix')
    complete = sum(row['status'] == 'complete' for row in outcomes)
    need(complete == interruption['observed_completed_outcomes'], 'observed completed count differs')
    run_root = root / 'runs'
    need({path.name for path in run_root.iterdir() if path.is_dir()} == set(ids), 'run directories differ from outcomes; unknown attempts exist')
    for row in outcomes:
        if row['status'] == 'complete':
            path = run_root / row['id'] / 'receipt.json'
            load(path)
            need(inputs[str(path)] == row['receipt_sha256'], 'completed receipt digest differs')
    source = Path(__file__).resolve()
    inputs[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    result = {'schema': 'ov1106.recovery-summary/v1', 'reconstructed': True,
              'original_terminal_summary_present': False, 'campaign': str(root),
              'plan_path': str(Path(plan_path).resolve()),
              'interruption_path': str(Path(interruption_path).resolve()),
              'observed_exception': interruption, 'outcomes': outcomes,
              'attempted': len(outcomes), 'complete': complete,
              'errors': len(outcomes) - complete, 'quota_stopped': False,
              'quota_probe_failed': True, 'stop_reason': 'quota_probe_failed',
              'next_plan_index': next_index, 'not_attempted_ids': sorted(set(planned_ids) - set(ids)),
              'paid_infra_usd': campaign['paid_infra_usd'],
              'inputs': [{'path': path, 'sha256': value} for path, value in sorted(inputs.items())]}
    for item in result['inputs']:
        need(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256'], 'input changes during reconstruction')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--interruption', type=Path, required=True)
    args = parser.parse_args()
    result = reconstruct(args.campaign, args.plan, args.interruption)
    destination = args.campaign / 'recovery-summary.json'
    with destination.open('x') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps({'path': str(destination), 'reconstructed': True,
                      'attempted': result['attempted'], 'not_attempted': len(result['not_attempted_ids']),
                      'stop_reason': result['stop_reason']}, sort_keys=True))


if __name__ == '__main__':
    main()
