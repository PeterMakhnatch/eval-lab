"""Evaluate every filename-unconstrained replication, including independent public checks."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cells import ROOT, run, source_patch_script

HERE = Path(__file__).resolve().parent


def main():
    freeze = json.loads((HERE / 'PUBLIC-FREEZE.json').read_text())
    for name, expected in freeze.items():
        if name != 'frozen_at' and hashlib.sha256((HERE / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('public contract or source review drift')
    tasks = json.loads((HERE / 'package-bindings.json').read_text())
    task = tasks['002139']
    rows = json.loads((HERE / 'generation-replication.json').read_text())
    reviews = json.loads((HERE / 'source-review-replication.json').read_text())
    output = HERE / 'evaluation-replication.json'
    for row in rows:
        row['source_review'] = reviews[f'002139-{row["index"]}']
    for version in ['public', 'original', 'existing', 'corrected']:
        for row in rows:
            if row.get('error'):
                row[version] = {'status': 'not_generated', 'reward': None}
                output.write_text(json.dumps(rows, indent=2) + '\n')
                continue
            patch = Path(row['patch'])
            if hashlib.sha256(patch.read_bytes()).hexdigest() != row['patch_sha256']:
                raise RuntimeError('source patch drift')
            package = Path(task['original_package'] if version == 'public' else task[version + '_package'])
            script = source_patch_script(patch.read_text(), '/workspace/repo')
            if version == 'public':
                payload = base64.b64encode((HERE / 'public_gcs.py').read_bytes()).decode()
                script += (f'printf %s {payload} | base64 -d > /tmp/public_gcs.py\n'
                    'python3 /tmp/public_gcs.py\necho PUBLIC_CONTRACT_PASSED\n')
            name = f'002139-alt-{row["index"]}-{version}'
            result = run(name, package, script)
            row[version] = {'status': result.get('status'), 'reward': result.get('reward'),
                'source_applied': 'SOURCE_PATCH_APPLIED' in result.get('agent_log', ''),
                'public_contract_passed': 'PUBLIC_CONTRACT_PASSED' in result.get('agent_log', ''),
                'receipt': str(ROOT / 'cells' / name / 'result.json'), 'est_usd': result.get('est_usd')}
            output.write_text(json.dumps(rows, indent=2) + '\n')
            print(json.dumps({'index': row['index'], 'version': version, **row[version]}), flush=True)


if __name__ == '__main__':
    main()
