"""Convert original cached J3 DEV windows into input-bound, label-free line readings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line import j3

CORPORA = ('goodfire', 'cheatbench', 'terminal-wrench', 'c1c5', 'sol-astra')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def convert(inputs: Path, source: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read no labels; require native prompts to match the original cached windows."""
    cache = {r['run_key']: (p, r) for p in sorted((source / 'results').glob('*.json')) for r in [json.loads(p.read_text())]}
    output, mismatches, sources = [], [], []
    complete, unavailable, windows = 0, 0, 0
    for corpus in CORPORA:
        manifest = inputs / 'corpora' / f'{corpus}-manifest.jsonl'
        native = {str(Path(r['path']).resolve()): r['id'] for r in map(json.loads, manifest.read_text().splitlines())}
        sources.append({'path': str(manifest), 'sha256': digest(manifest)})
        for item in hd.load_rollouts([manifest]):
            native_id = native[str(Path(item.source_path).resolve())]
            key = f'{corpus}:{native_id}'
            path, old = cache[key]
            sources.append({'path': str(path), 'sha256': digest(path)})
            if old.get('model') != j3.ROUTE or old.get('rubric_sha256') != j3.RUBRIC_SHA:
                raise ValueError(f'cached J3 identity differs: {key}')
            _, prompts = j3.prepare(item)
            expected = [w['prompt_sha256'] for w in old.get('windows', [])]
            actual = [j3.sha(p) for p in prompts]
            if actual != expected:
                mismatches.append({'run_key': key, 'expected': expected, 'actual': actual})
                continue
            converted = []
            for index, window in enumerate(old.get('windows', [])):
                parsed, usage = window.get('parsed') or {}, window.get('usage') or {}
                attested = window.get('model_served') == j3.ROUTE
                error = 'unattested' if not attested else ('failed' if window.get('error') else ('unparsed' if parsed.get('suspicion') is None else None))
                converted.append({'index': index, 'prompt_sha256': window['prompt_sha256'], 'served_route': j3.ROUTE if attested else None, 'suspicion': parsed.get('suspicion') if error is None else None, 'evidence_steps': (parsed.get('cited_steps') or [])[:5], 'error': error, 'tokens_in': int(usage.get('prompt_tokens') or 0), 'tokens_out': int(usage.get('completion_tokens') or 0), 'latency_s': float(window.get('latency_s') or 0), 'cost_usd': 0.0})
            error = 'no_trajectory' if not prompts else ('incomplete' if not converted or any(w['error'] for w in converted) else None)
            value = j3.validate(j3.reading(item, converted, error=error))
            output.append({'schema': 'evallab.j3_cache/v1', 'run_id': item.item_id, 'reading': value})
            complete += error is None
            unavailable += error is not None
            windows += len(converted)
    return output, {'freeze_commit': '5d5cd6ba4a78af1894c910388dc82fc3d92c873e', 'runs': len(output), 'complete': complete, 'unavailable': unavailable, 'windows': windows, 'mismatch_count': len(mismatches), 'mismatches': mismatches, 'sources': sources}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path.home() / 'Developer/eval-lab-results/line/replay-v1/inputs')
    parser.add_argument('--source', type=Path, default=Path('/private/tmp/ov1104/judges/out/j2b__openai-codex__gpt-6.1-terra'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    rows, report = convert(args.inputs, args.source)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    if report['mismatches']:
        raise SystemExit(f"refused cache conversion: {report['mismatch_count']} input mismatches; see {args.report}")
    if len({r['run_id'] for r in rows}) != len(rows):
        raise SystemExit('duplicate native run IDs')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    print(json.dumps({k: v for k, v in report.items() if k not in ('sources', 'mismatches')} | {'cache_sha256': digest(args.out)}, indent=2))


if __name__ == '__main__':
    main()
