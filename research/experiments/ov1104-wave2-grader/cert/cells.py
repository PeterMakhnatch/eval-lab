"""Source-only Modal research cells; never creates harness/startup injection controls."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

ROOT = Path('/private/tmp/ov1104/wave2-grader/workers/cert')
APPROVAL = 'Peter 2026-11-03 23:35 ET: $30 overnight infra budget (ov1104), lane cap per PROGRAM.md'
LEDGER = Path.home() / '.local/state/evallab-ov1104/wave2-grader-spend.json'


def run(name: str, package: Path, script: str) -> dict:
    sys.path.insert(0, str(REPO / "src"))
    from evallab.modal_cells import ReserveSettleSpendGate, SandboxMeter, modal_cell_runner

    os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
    gate = ReserveSettleSpendGate(approval=APPROVAL, cap_usd=2.0, usd_per_execution=0.008,
        spend_ledger=LEDGER, modal_app_name='ov1104-wave2-grader-cert', billing_tag='ov1104')
    runner = modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=SandboxMeter(2.0),
        timeout_seconds=300, results_dir=ROOT / 'cell-cache')
    cell = ROOT / 'cells' / name
    cell.mkdir(parents=True, exist_ok=True)
    (cell / 'agent-script.sh').write_text(script)
    result = runner(cell, package, script)
    if not result.get('cache_hit'):
        row = {'ts': datetime.now(UTC).isoformat(), 'lane': 'wave2-grader',
            'what': name, 'provider': 'modal', 'est_usd': result.get('est_usd'),
            'actual_usd': None, 'receipt': str(cell / 'result.json')}
        with (Path.home() / '.local/state/evallab-ov1104/spend.jsonl').open('a') as f:
            f.write(json.dumps(row, sort_keys=True) + '\n')
    return result


def source_patch_script(patch: str, workdir: str) -> str:
    # Only controller-reviewed source diffs are accepted by the caller.
    encoded = base64.b64encode(patch.encode()).decode()
    return (f'set -eu\ncd {workdir}\n'
        f'printf %s {encoded} | base64 -d > /tmp/source-only.patch\n'
        'git apply --whitespace=nowarn /tmp/source-only.patch\necho SOURCE_PATCH_APPLIED\n')


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--script', type=Path)
    p.add_argument('--patch', type=Path)
    p.add_argument('--workdir', default='/testbed')
    a = p.parse_args()
    script = a.script.read_text() if a.script else source_patch_script(a.patch.read_text(), a.workdir)
    print(json.dumps(run(a.name, a.package, script), sort_keys=True))


if __name__ == '__main__':
    main()
