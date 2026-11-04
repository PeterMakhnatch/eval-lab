"""Launch one frozen subscription-judge batch after gates and a fresh quota check."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from evaluate import LANE, ROOT, digest, gates, keyed, rows

MODELS = {'j2b': 'google-antigravity/gemini-3.8-flash', 'j3': 'openai-codex/gpt-6.1-terra'}


def quota_value(text: str, section: str, label: str) -> float:
    parts = text.split(section, 1)
    if len(parts) != 2:
        raise ValueError(f'missing quota section: {section}')
    body = parts[1].split('\n\n', 1)[0]
    values = re.findall(label + r'[^\n]*?([0-9]+(?:\.[0-9]+)?)% used', body)
    if not values:
        raise ValueError(f'missing quota value: {label}')
    return max(float(value) for value in values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge', choices=tuple(MODELS), required=True)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--admitted', type=Path, default=LANE / 'ADMITTED.json')
    parser.add_argument('--frozen', type=Path, default=LANE / 'FROZEN.json')
    args = parser.parse_args()
    gate = gates(args.admitted, args.frozen)
    frozen_inputs = gate['freeze'].get('inputs', {})
    if frozen_inputs.get(str(args.runs.resolve())) != digest(args.runs):
        raise ValueError('judge runs table must be hash-bound in FROZEN.json inputs')
    manifest = keyed(rows(Path(gate['admission']['cohort_manifest'])))
    runs = keyed(rows(args.runs))
    if set(runs) - set(manifest):
        raise ValueError('judge input contains nonadmitted rows')
    for key, row in runs.items():
        if set(row) != {'run_key', 'set', 'view'}:
            raise ValueError('judge rows must contain only run_key, set, view')
        path = Path(row['view']).resolve()
        if frozen_inputs.get(str(path)) != digest(path):
            raise ValueError(f'view lacks frozen digest: {key}')
    usage = subprocess.check_output(['omp', 'usage'], cwd=ROOT, text=True)
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    receipt = args.out / f'quota-{args.judge}-{stamp}.txt'
    receipt.write_text(usage)
    codex = quota_value(usage, 'Openai Codex —', r'7 days')
    gemini = quota_value(usage, 'Google Antigravity —', r'Gemini \(Weekly\)')
    short = quota_value(usage, 'Google Antigravity —', r'Gemini \(5 Hour\)')
    if codex >= 82 or gemini >= 78 or (args.judge == 'j2b' and short >= 85):
        raise ValueError(f'quota gate refuses batch; receipt: {receipt}')
    command = [sys.executable, str(Path(__file__).with_name('judge_runner.py')),
               '--model', MODELS[args.judge], '--runs', str(args.runs),
               '--out', str(args.out)]
    (args.out / f'launch-{args.judge}-{stamp}.json').write_text(json.dumps({
        'command': command, 'freeze_commit': gate['freeze']['commit'],
        'quota_receipt': str(receipt), 'runs_sha256': digest(args.runs),
        'ts': datetime.now(UTC).isoformat()}, indent=2) + '\n')
    return subprocess.call(command, cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
