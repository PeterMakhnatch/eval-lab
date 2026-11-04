"""Replicate GCP generation without reference-derived new-file naming guidance."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from generate import ALLOWED, MODEL, OUT, ROOT, convert, sha


async def main():
    prereg = OUT / 'PREREG-REPLICATION.md'
    (OUT / 'PREREG-REPLICATION.sha256').write_text(sha(prereg.read_bytes()) + '  PREREG-REPLICATION.md\n')
    instruction = Path('/private/tmp/ov1104/grader/alt/002139/instruction.md').read_text()
    baseline_paths = json.loads((OUT / 'input-manifest.json').read_text())['tasks']['002139']['source_files']
    baseline = {p: (ROOT / 'baseline/002139' / p).read_text() for p in baseline_paths}
    prompt = ('Implement the task using only its instruction and pristine baseline source below. '
        'Return only JSON with an edits array; each edit has path, old, new strings. '
        'For an existing file old must match exactly once; new replaces it. '
        'For a new Python source file old is empty and new is its complete content. '
        'You can modify or add ordinary Python source under lib/bb/fetch2. '
        'Choose any appropriate source module names. Never modify tests, grading, startup hooks, '
        'the environment, or harness files. Do not add bypasses or claim you ran checks.\n\n'
        'TASK INSTRUCTION\n' + instruction + '\nBASELINE SOURCE\n' + json.dumps(baseline))
    directory = ROOT / 'generation/002139'
    for i in range(4, 8):
        body = {'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0.7,
                'max_tokens': 14000, 'seed': 110400 + i, 'tools': [], 'tool_choice': 'none'}
        (directory / f'{i}.request.json').write_text(json.dumps(body, indent=2))
    freeze = {'prereg_sha256': sha(prereg.read_bytes()), 'instruction_sha256': sha(instruction.encode()),
        'source_files': baseline_paths, 'requests': {str(i): sha((directory / f'{i}.request.json').read_bytes()) for i in range(4, 8)}}
    (OUT / 'REPLICATION-FREEZE.json').write_text(json.dumps(freeze, indent=2) + '\n')
    process = await asyncio.create_subprocess_exec('omp', 'auth-gateway', 'stdio', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 26)
    if not json.loads(await process.stdout.readline()).get('ready'):
        raise RuntimeError('gateway readiness failed')
    rows = []
    try:
        for i in range(4, 8):
            body = json.loads((directory / f'{i}.request.json').read_text())
            row = {'task': '002139', 'index': i, 'split': 'dev' if i < 6 else 'eval',
                   'cohort': 'filename-unconstrained', 'model_requested': MODEL,
                   'request_sha256': sha(json.dumps(body, sort_keys=True).encode())}
            for attempt in range(3):
                process.stdin.write((json.dumps({'id': f'139-{i}-{attempt}', 'path': '/v1/chat/completions', 'body': body}) + '\n').encode())
                await process.stdin.drain()
                response = json.loads(await asyncio.wait_for(process.stdout.readline(), 900))
                (directory / f'{i}.response-{attempt}.json').write_text(json.dumps(response, indent=2))
                payload = response.get('body', {})
                row.update({'status': response.get('status'), 'model_served': payload.get('model'),
                            'usage': payload.get('usage'), 'attempts': attempt + 1})
                if response.get('status') != 200 or payload.get('model') != MODEL:
                    row['error'] = 'status or served-model mismatch'
                    continue
                message = payload['choices'][0]['message']
                if message.get('tool_calls'):
                    raise RuntimeError('unexpected tool call')
                text = message.get('content') or ''
                (directory / f'{i}.text.txt').write_text(text)
                try:
                    value = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip()))
                    paths = {e['path'] for e in value['edits']}
                    if any(not p.startswith('lib/bb/fetch2/') or not p.endswith('.py') or '..' in p.split('/')
                           or any(s in p.lower() for s in ['test', 'conftest', 'sitecustomize', 'usercustomize']) for p in paths):
                        raise ValueError('non-source or unsafe path')
                    ALLOWED['002139'] = paths
                    row.update(convert('002139', i, text))
                    row['error'] = None
                except Exception as exc:
                    row['error'] = f'{type(exc).__name__}: {exc}'
                break
            (directory / f'{i}.receipt.json').write_text(json.dumps(row, indent=2))
            rows.append(row)
            print(json.dumps(row), flush=True)
        (OUT / 'generation-replication.json').write_text(json.dumps(rows, indent=2) + '\n')
    finally:
        process.kill()
        await process.wait()


if __name__ == '__main__':
    asyncio.run(main())
