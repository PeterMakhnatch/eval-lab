"""Execute only source-only and NOP certification cells, preserving every candidate."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tomllib
from pathlib import Path

from cells import REPO, ROOT, run, source_patch_script

HERE = Path(__file__).resolve().parent

PRIOR = Path('/Users/petermakhnatch/Developer/eval-lab-results/2026-11-04/ov1104-grader/cert')
RUNS = {'000682': '000682-r3', '002391': '002391-r2', '002139': '002139-r2'}
SAFE = {'reference', 'nop', 'train', 'eval', 'evalbase'}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    sys.path.insert(0, str(REPO / 'src'))
    from evallab.registry import task_directory_digest

    generation = json.loads((HERE / 'generation.json').read_text())
    review_path = HERE / 'source-review.json'
    if not review_path.is_file():
        raise RuntimeError('Independent source review must precede Gold grading')
    review = json.loads(review_path.read_text())
    if any(f"{r['task']}-{r['index']}" not in review for r in generation):
        raise RuntimeError('Missing source review for one or more candidates')
    frozen = json.loads((HERE / 'FREEZE.json').read_text())
    for path, digest in frozen.items():
        if path != 'frozen_at' and sha(HERE / path) != digest:
            raise RuntimeError(f'frozen input drift: {path}')
    tasks = {}
    for tid, oldrun in RUNS.items():
        old = PRIOR / oldrun
        plan = json.loads((old / 'plan.json').read_text())
        existing = old / 'package'
        original = Path(next(c['package'] for c in plan if c['arm'] == 'evalbase'))
        prior_cert = json.loads((old / 'certification.json').read_text())
        digest = task_directory_digest(existing)
        if digest != prior_cert['package_digest']:
            raise RuntimeError(f'previous package digest drift: {tid}')
        if sha(existing / 'tests/test_gold_contract.py') != sha(HERE / 'contracts' / tid / 'existing.py'):
            raise RuntimeError(f'previous contract drift: {tid}')
        inputs = json.loads((old / 'inputs.json').read_text())
        if task_directory_digest(original) != inputs['base_package_digest']:
            raise RuntimeError(f'original package digest drift: {tid}')
        task = {'task': tid, 'original_package': str(original), 'original_digest': inputs['base_package_digest'],
                'existing_package': str(existing), 'existing_digest': digest,
                'prior_certification': str(old / 'certification.json'), 'prior_certification_sha256': sha(old / 'certification.json'),
                'alternatives': [], 'safe_gates': {}, 'historical_controls': {}}
        # Do not read or reproduce injection scripts. Reuse result metadata only, exact package-bound.
        for name in ['reference', 'nop', 'witness-0', 'exit0_pth']:
            receipt = old / 'cells' / name / 'result.json'
            r = json.loads(receipt.read_text())
            task['historical_controls'][name] = {k: r.get(k) for k in ['cell_key', 'status', 'reward', 'evidence_sha256']}
            task['historical_controls'][name].update({'receipt': str(receipt), 'receipt_sha256': sha(receipt),
                'valid_for_package_digest': digest, 'newly_executed': False})
        for c in plan:
            if c['arm'] in SAFE:
                receipt = old / 'cells' / c['name'] / 'result.json'
                r = json.loads(receipt.read_text())
                task['safe_gates'][c['name']] = {'arm': c['arm'], 'status': r.get('status'),
                    'reward': r.get('reward'), 'marker_present': c['marker'] in r.get('agent_log', ''),
                    'receipt': str(receipt), 'receipt_sha256': sha(receipt), 'reused': True}
        if tid != '000682':
            corrected = ROOT / 'packages' / (tid + '-corrected')
            if not corrected.exists():
                shutil.copytree(existing, corrected)
                shutil.copyfile(HERE / 'contracts' / tid / 'corrected.py', corrected / 'tests/test_gold_contract.py')
            task.update({'corrected_package': str(corrected), 'corrected_digest': task_directory_digest(corrected),
                'corrected_contract_sha256': sha(corrected / 'tests/test_gold_contract.py'),
                'corrected_gates': {}, 'corrected_injection_gates': 'unexercised; changed package cannot inherit prior controls'})
        tasks[tid] = task
    (HERE / 'package-bindings.json').write_text(json.dumps(tasks, indent=2) + '\n')
    results_path = HERE / 'evaluation.json'
    def checkpoint():
        results_path.write_text(json.dumps(tasks, indent=2) + '\n')
    # Original correctness first, then frozen existing contract, then preregistered correction.
    for version in ['original', 'existing', 'corrected']:
        for row in generation:
            tid, index = row['task'], row['index']
            task = tasks[tid]
            if version == 'corrected' and tid == '000682':
                continue
            if row.get('error'):
                if version == 'original':
                    task['alternatives'].append({**row, 'original': {'status': 'not_generated', 'reward': None}})
                continue
            patch = Path(row['patch'])
            if sha(patch) != row['patch_sha256']:
                raise RuntimeError('alternative patch drift')
            package = Path(task[version + '_package'])
            wd = tomllib.loads((package / 'task.toml').read_text())['environment']['workdir']
            script = source_patch_script(patch.read_text(), wd)
            result = run(f'{tid}-alt-{index}-{version}', package, script)
            rec = {'status': result.get('status'), 'reward': result.get('reward'),
                'source_applied': 'SOURCE_PATCH_APPLIED' in result.get('agent_log', ''),
                'receipt': str(ROOT / 'cells' / f'{tid}-alt-{index}-{version}' / 'result.json'),
                'est_usd': result.get('est_usd'), 'cache_hit': result.get('cache_hit')}
            if version == 'original':
                task['alternatives'].append({**row, 'source_review': review[f'{tid}-{index}'], version: rec})
            else:
                next(a for a in task['alternatives'] if a['index'] == index)[version] = rec
            checkpoint()
            print(json.dumps({'task': tid, 'index': index, 'version': version, **rec}), flush=True)
    for tid, task in tasks.items():
        if tid == '000682':
            continue
        plan = json.loads((PRIOR / RUNS[tid] / 'plan.json').read_text())
        for cell in plan:
            if cell['arm'] not in {'reference', 'nop', 'train', 'eval'}:
                continue
            # Existing source-only scripts, never witness/exit0 scripts.
            result = run(f'{tid}-corrected-{cell["name"]}', Path(task['corrected_package']), cell['script'])
            task['corrected_gates'][cell['name']] = {'arm': cell['arm'], 'status': result.get('status'),
                'reward': result.get('reward'), 'marker_present': cell['marker'] in result.get('agent_log', ''),
                'receipt': str(ROOT / 'cells' / f'{tid}-corrected-{cell["name"]}' / 'result.json'),
                'est_usd': result.get('est_usd')}
            checkpoint()
            print(json.dumps({'task': tid, 'gate': cell['name'], 'status': result.get('status'), 'reward': result.get('reward')}), flush=True)
    checkpoint()


if __name__ == '__main__':
    main()
