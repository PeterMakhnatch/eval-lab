"""Freeze and execute minimal SNS semantic checks on every original candidate."""
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from cells import ROOT, run, source_patch_script

HERE = Path(__file__).resolve().parent


def main():
    freeze = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
              for name in ['PREREG-SNS-PUBLIC.md', 'public_sns.py']}
    freeze['frozen_at'] = datetime.now(UTC).isoformat()
    (HERE / 'SNS-PUBLIC-FREEZE.json').write_text(json.dumps(freeze, indent=2) + '\n')
    task = json.loads((HERE / 'package-bindings.json').read_text())['000682']
    rows = [r for r in json.loads((HERE / 'generation.json').read_text()) if r['task'] == '000682']
    output = []
    for row in rows:
        script = source_patch_script(Path(row['patch']).read_text(), '/testbed')
        payload = base64.b64encode((HERE / 'public_sns.py').read_bytes()).decode()
        script += (f'printf %s {payload} | base64 -d > /tmp/public_sns.py\n'
                   'python3 /tmp/public_sns.py\necho PUBLIC_CONTRACT_PASSED\n')
        name = f'000682-alt-{row["index"]}-public'
        r = run(name, Path(task['original_package']), script)
        out = {'index': row['index'], 'split': row['split'], 'status': r.get('status'), 'reward': r.get('reward'),
               'public_contract_passed': 'PUBLIC_CONTRACT_PASSED' in r.get('agent_log', ''),
               'receipt': str(ROOT / 'cells' / name / 'result.json'), 'est_usd': r.get('est_usd')}
        output.append(out)
        (HERE / 'evaluation-sns-public.json').write_text(json.dumps(output, indent=2) + '\n')
        print(json.dumps(out), flush=True)


if __name__ == '__main__':
    main()
