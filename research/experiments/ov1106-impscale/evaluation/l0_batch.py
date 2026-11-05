"""Run pinned native L0 and derive complete rule rows from retained raw reasons."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evaluate import LANE, ROOT, digest, gates, keyed, load_contract, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--raw', type=Path, required=True)
    args = parser.parse_args()
    gate = gates(LANE / 'ADMITTED.json', LANE / 'FROZEN.json')
    for path in [args.runs, *(Path(row['view']) for row in rows(args.runs))]:
        if gate['freeze']['inputs'].get(str(path.resolve())) != digest(path):
            raise ValueError(f'unfrozen L0 input: {path}')
    subprocess.run([sys.executable, str(ROOT / 'research/experiments/ov1104-detbench/l0_rules.py'),
                    '--runs', str(args.runs), '--view', 'full', '--workers', '1',
                    '--signals-dir', str(args.out), '--raw-dir', str(args.raw)], check=True, cwd=ROOT)
    native = keyed(rows(args.raw / 'l0v_full_raw.jsonl'))
    if set(native) != set(keyed(rows(args.runs))):
        raise ValueError('native L0 raw results do not cover the frozen sample')
    contract = load_contract()
    for name in ('l0v_full_l0_hard_gate_rule_hidden_info_read', 'l0v_full_any_hard'):
        output = []
        for key, row in native.items():
            error = row.get('error')
            reason = 'L0_HARD_GATE_RULE:hidden_info_read'
            fired = (reason in row['reasons'] if name.endswith('hidden_info_read')
                     else any(r.startswith('L0_HARD_') for r in row['reasons']))
            output.append({'run_key': key, 'signal': name, 'version': contract['signals'][name]['version'],
                           'score': None if error else int(fired), 'abstain': error,
                           'model': None, 'evidence_steps': row['evidence_steps'],
                           'raw_sha256': digest(args.raw / 'l0v_full_raw.jsonl')})
        (args.out / (name + '.jsonl')).write_text(''.join(json.dumps(row) + '\n' for row in output))


if __name__ == '__main__':
    main()
