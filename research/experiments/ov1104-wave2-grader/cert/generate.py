"""Generate frozen source patches through the text-only subscription gateway."""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import io
import json
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/private/tmp/ov1104/wave2-grader/workers/cert')
OUT = Path(__file__).resolve().parent
PRIOR = Path('/Users/petermakhnatch/Developer/eval-lab-results/2026-11-04/ov1104-grader')
MODEL = 'openai-codex/gpt-6.1-terra'
PATHS = {
 '000682': ['c7n/resources/sns.py', 'c7n/resources/sqs.py', 'c7n/filters/kms.py', 'c7n/filters/__init__.py', 'c7n/utils.py', 'c7n/actions/__init__.py'],
 '002391': ['pip_audit/_audit.py', 'pip_audit/_service/interface.py'],
 '002139': ['lib/bb/fetch2/__init__.py', 'lib/bb/fetch2/s3.py', 'lib/bb/fetch2/az.py', 'lib/bb/fetch2/crate.py'],
}
ALLOWED = {'000682': {'c7n/resources/sns.py'}, '002391': set(PATHS['002391']),
           '002139': {'lib/bb/fetch2/__init__.py', 'lib/bb/fetch2/gcp.py'}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare() -> dict:
    recpath = ROOT / 'cells/source-002391-workdir/result.json'
    rec = json.loads(recpath.read_text())
    blob = next(line.split(' ', 1)[1] for line in rec['agent_log'].splitlines() if line.startswith('BASELINE_TAR '))
    target = ROOT / 'baseline/002391'
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode='r:gz') as tf:
        tf.extractall(target, filter='data')
    provenance = {'000682': PRIOR / 'cells/probe-000682/result.json',
                  '002139': PRIOR / 'cells/probe-002139-b/result.json', '002391': recpath}
    manifest = {'model_requested': MODEL, 'tools': [], 'split': {'dev': [0, 1], 'eval': [2, 3]}, 'tasks': {}}
    for tid, paths in PATHS.items():
        instruction = Path(f'/private/tmp/ov1104/grader/alt/{tid}/instruction.md').read_text()
        baseline = {p: (ROOT / 'baseline' / tid / p).read_text() for p in paths}
        manifest['tasks'][tid] = {'instruction_sha256': sha(instruction.encode()),
            'source_files': {p: sha(text.encode()) for p, text in baseline.items()},
            'baseline_receipt': str(provenance[tid]), 'baseline_receipt_sha256': sha(provenance[tid].read_bytes())}
        taskdir = ROOT / 'generation' / tid
        taskdir.mkdir(parents=True, exist_ok=True)
        generic = ('Solve the software task using only its instruction and pristine baseline source below. '
            'No tests or reference solution are available. Return only JSON with an edits array. '
            'Each edit has path, old, new strings. For an existing file old must match exactly once; '
            'new replaces it. A new source file uses old="" and new equal to its complete content. '
            'Use as many source edits as needed. Never modify tests, the test harness, startup hooks, '
            'environment, or grading files. Do not add bypasses. Do not claim you ran tests. '
            'Allowed modified paths: ' + ', '.join(sorted(ALLOWED[tid])) + '.\n\n')
        payload = generic + 'TASK INSTRUCTION\n' + instruction + '\nBASELINE SOURCE\n' + json.dumps(baseline)
        for index in range(4):
            body = {'model': MODEL, 'messages': [{'role': 'user', 'content': payload}],
                    'temperature': 0.7, 'max_tokens': 14000, 'seed': 110400 + index,
                    'tools': [], 'tool_choice': 'none'}
            (taskdir / f'{index}.request.json').write_text(json.dumps(body, indent=2))
    (OUT / 'input-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    frozen = {str(p.relative_to(OUT)): sha(p.read_bytes()) for p in (OUT / 'contracts').rglob('*.py')}
    frozen['PREREG.md'] = sha((OUT / 'PREREG.md').read_bytes())
    frozen['input-manifest.json'] = sha((OUT / 'input-manifest.json').read_bytes())
    frozen['frozen_at'] = datetime.now(UTC).isoformat()
    (OUT / 'FREEZE.json').write_text(json.dumps(frozen, indent=2, sort_keys=True) + '\n')
    return manifest


def convert(tid: str, index: int, text: str) -> dict:
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    value = json.loads(text)
    originals = {p: (ROOT / 'baseline' / tid / p).read_text() for p in PATHS[tid]}
    updated = dict(originals)
    for edit in value['edits']:
        p, old, new = edit['path'], edit['old'], edit['new']
        if p not in ALLOWED[tid]:
            raise ValueError(f'non-source or unapproved path: {p}')
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError('source edit values must be strings')
        current = updated.get(p, '')
        if old == '':
            if p in updated:
                raise ValueError(f'new-file edit targets an existing path: {p}')
            updated[p] = new
        else:
            if current.count(old) != 1:
                raise ValueError(f'old text matches {current.count(old)} times: {p}')
            updated[p] = current.replace(old, new, 1)
    patch = ''
    changed = []
    for p in sorted(updated):
        if updated[p] == originals.get(p, ''):
            continue
        if not updated[p].endswith('\n'):
            updated[p] += '\n'
        compile(updated[p], p, 'exec')
        changed.append(p)
        patch += ''.join(difflib.unified_diff(originals.get(p, '').splitlines(keepends=True),
            updated[p].splitlines(keepends=True), fromfile='a/' + p if p in originals else '/dev/null', tofile='b/' + p))
    if not changed:
        raise ValueError('empty source patch')
    dest = OUT / 'alternatives' / tid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f'{index}.patch').write_text(patch)
    return {'patch_sha256': sha(patch.encode()), 'changed_paths': changed, 'patch': str(dest / f'{index}.patch')}


async def main() -> None:
    prepare()
    process = await asyncio.create_subprocess_exec('omp', 'auth-gateway', 'stdio',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 26)
    ready = await asyncio.wait_for(process.stdout.readline(), 120)
    (ROOT / 'gateway-ready.json').write_bytes(ready)
    if not json.loads(ready).get('ready'):
        raise RuntimeError('gateway readiness failed')
    pending = {}
    async def reader():
        while line := await process.stdout.readline():
            response = json.loads(line)
            if str(response.get('id')) in pending:
                pending.pop(str(response['id'])).set_result(response)
    async def stderr():
        with (ROOT / 'gateway-stderr.txt').open('wb') as f:
            while line := await process.stderr.readline():
                f.write(line)
    rt, et = asyncio.create_task(reader()), asyncio.create_task(stderr())
    sem = asyncio.Semaphore(3)
    async def call(tid, index):
        directory = ROOT / 'generation' / tid
        body = json.loads((directory / f'{index}.request.json').read_text())
        record = {'task': tid, 'index': index, 'split': 'dev' if index < 2 else 'eval',
                  'model_requested': MODEL, 'request_sha256': sha(json.dumps(body, sort_keys=True).encode())}
        async with sem:
            for attempt in range(3):
                ident = f'{tid}-{index}-{attempt}'
                future = asyncio.get_running_loop().create_future()
                pending[ident] = future
                process.stdin.write((json.dumps({'id': ident, 'path': '/v1/chat/completions', 'body': body}) + '\n').encode())
                await process.stdin.drain()
                response = await asyncio.wait_for(future, 900)
                (directory / f'{index}.response-{attempt}.json').write_text(json.dumps(response, indent=2))
                payload = response.get('body', {})
                record.update({'status': response.get('status'), 'model_served': payload.get('model'),
                    'usage': payload.get('usage'), 'attempts': attempt + 1})
                if response.get('status') != 200 or payload.get('model') != MODEL:
                    record['error'] = 'gateway status or served-model mismatch'
                    continue
                message = payload['choices'][0]['message']
                if message.get('tool_calls'):
                    raise RuntimeError('unexpected tool call in tool-free response')
                text = message.get('content') or ''
                (directory / f'{index}.text.txt').write_text(text)
                try:
                    record.update(convert(tid, index, text))
                    record['error'] = None
                except Exception as exc:
                    record['error'] = f'{type(exc).__name__}: {exc}'
                break
        (directory / f'{index}.receipt.json').write_text(json.dumps(record, indent=2))
        print(json.dumps(record), flush=True)
        return record
    try:
        records = await asyncio.gather(*(call(tid, i) for tid in PATHS for i in range(4)))
        (OUT / 'generation.json').write_text(json.dumps(records, indent=2) + '\n')
    finally:
        process.kill()
        await process.wait()
        rt.cancel()
        et.cancel()


if __name__ == '__main__':
    asyncio.run(main())
