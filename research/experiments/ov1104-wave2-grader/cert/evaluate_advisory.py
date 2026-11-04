"""Evaluate every prospective advisory candidate; preserve first-cohort failures separately."""
import hashlib
import json
from pathlib import Path

from cells import ROOT, run, source_patch_script

HERE = Path(__file__).resolve().parent


def main():
    freeze = json.loads((HERE / 'ADVISORY-FREEZE.json').read_text())
    if hashlib.sha256((HERE / 'PREREG-ADVISORY.md').read_bytes()).hexdigest() != freeze['prereg_sha256']:
        raise RuntimeError('advisory preregistration drift')
    task = json.loads((HERE / 'package-bindings.json').read_text())['002391']
    rows = json.loads((HERE / 'generation-advisory.json').read_text())
    reviews = json.loads((HERE / 'source-review-advisory.json').read_text())
    output = HERE / 'evaluation-advisory.json'
    for row in rows:
        row['source_review'] = reviews[f'002391-{row["index"]}']
    for version in ['original', 'existing', 'corrected']:
        for row in rows:
            if row.get('error'):
                row[version] = {'status': 'not_generated', 'reward': None}
                output.write_text(json.dumps(rows, indent=2) + '\n')
                continue
            patch = Path(row['patch'])
            if hashlib.sha256(patch.read_bytes()).hexdigest() != row['patch_sha256']:
                raise RuntimeError('source patch drift')
            script = source_patch_script(patch.read_text(), '/workspace/repo')
            name = f'002391-alt-{row["index"]}-{version}'
            result = run(name, Path(task[version + '_package']), script)
            row[version] = {'status': result.get('status'), 'reward': result.get('reward'),
                'source_applied': 'SOURCE_PATCH_APPLIED' in result.get('agent_log', ''),
                'receipt': str(ROOT / 'cells' / name / 'result.json'), 'est_usd': result.get('est_usd')}
            output.write_text(json.dumps(rows, indent=2) + '\n')
            print(json.dumps({'index': row['index'], 'version': version, **row[version]}), flush=True)


if __name__ == '__main__':
    main()
