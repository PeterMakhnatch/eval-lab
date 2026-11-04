"""Reuse frozen ov1104 prompts through a lane-local, no-fallback gateway."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

from evaluate import LANE, ROOT, digest, gates, rows

sys.path.insert(0, str(ROOT / 'research/experiments/ov1104-judges'))
from judge import judge_run, slug  # noqa: E402

CONFIG = Path('/private/tmp/ov1106/shared/nofallback.yml')


class ModelMismatch(BaseException):
    """Stop the entire batch, including subsequent windows, on route mismatch."""


class LaneCaller:
    def __init__(self, out: Path):
        self.out = out
        self.process = None
        self.stderr = None
        self.lock = asyncio.Lock()
        self.next_id = 0

        self.halted = False
    async def start(self):
        if not CONFIG.is_file():
            raise RuntimeError('required nofallback configuration is missing')
        self.stderr = (self.out / 'gateway-stderr.log').open('ab')
        self.process = await asyncio.create_subprocess_exec(
            'omp', 'auth-gateway', 'stdio', '--config', str(CONFIG),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr, limit=1 << 26)
        ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 120))
        if ready.get('ready') is not True:
            raise RuntimeError('gateway did not report ready')

    async def close(self):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.stderr is not None:
            self.stderr.close()

    async def call(self, model, prompt, *, tag, seed=None):
        async with self.lock:
            if self.halted:
                raise ModelMismatch('batch already stopped on model attestation failure')
            record = {**tag, 'model_requested': model, 'model_served': None,
                      'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                      'prompt_chars': len(prompt), 'usage': None, 'latency_s': None,
                      'error': None, 'text': '', 'status': None}
            body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.0, 'max_tokens': 1500}
            if seed is not None:
                body['seed'] = seed
            for attempt in range(1, 4):
                self.next_id += 1
                request_id = str(self.next_id)
                started = time.monotonic()
                self.process.stdin.write((json.dumps({'id': request_id,
                    'path': '/v1/chat/completions', 'body': body}) + '\n').encode())
                await self.process.stdin.drain()
                response = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 600))
                if str(response.get('id')) != request_id:
                    raise RuntimeError('gateway response identity mismatch')
                payload = response.get('body')
                status = int(response.get('status', 0))
                served = payload.get('model') if isinstance(payload, dict) else None
                record.update(status=status, model_served=served, attempts=attempt,
                              latency_s=round(time.monotonic() - started, 2))
                # A different attested route always stops, even on an HTTP error.
                mismatch = served is not None and served != model
                missing = 200 <= status < 300 and served is None
                if mismatch or missing:
                    self.halted = True
                    record['error'] = f'model attestation failed: requested={model}, served={served}'
                    self.log(record)
                    raise ModelMismatch(record['error'])
                if not 200 <= status < 300:
                    record['error'] = f'HTTP {status}: {str(payload)[:400]}'
                    self.log(record)
                    if status in (429, 500, 502, 503, 504) and attempt < 3:
                        await asyncio.sleep(3 * attempt)
                        continue
                    return record
                choices = payload.get('choices') or []
                content = (choices[0].get('message') or {}).get('content') if choices else ''
                if isinstance(content, list):
                    content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
                record.update(text=content or '', usage=payload.get('usage'), error=None)
                self.log(record)
                return record

    def log(self, record):
        with (self.out / 'calls.jsonl').open('a') as handle:
            handle.write(json.dumps({k: v for k, v in record.items() if k != 'text'}
                                    | {'text_chars': len(record['text'])}) + '\n')


async def run(args):
    destination = args.out / ('j2b__' + slug(args.model))
    results = destination / 'results'
    results.mkdir(parents=True, exist_ok=True)
    caller = LaneCaller(destination)
    try:
        await caller.start()
        for line in args.runs.read_text().splitlines():
            row = json.loads(line)
            path = results / (slug(row['run_key']) + '.json')
            # No implicit reuse: every result belongs to this frozen launch.
            if path.exists():
                raise RuntimeError(f'refuse overwriting existing result: {path}')
            await judge_run(caller, judge='j2b', model=args.model, row=row,
                            window_chars=60000, seed=None, out_path=path)
    finally:
        await caller.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=('google-antigravity/gemini-3.8-flash',
                                          'openai-codex/gpt-6.1-terra'), required=True)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    frozen = gates(LANE / 'ADMITTED.json', LANE / 'FROZEN.json')['freeze']
    inputs = frozen.get('inputs', {})
    for path in [args.runs, *(Path(row['view']) for row in rows(args.runs))]:
        if inputs.get(str(path.resolve())) != digest(path):
            raise ValueError(f'judge input is not frozen: {path}')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
