"""Score frozen CM-1 outputs without sending answer-key material to models."""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KEY = REPO / 'research/answer-keys/tb4-audit-v1'
DEFAULT_OUT = Path.home() / 'Developer/eval-lab-results/2026-10-21/cheap-model-frontier'
MODES = ('category_1to1', 'locus_and_class', 'task_any_1to1')


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain_rows(root: Path, category: dict[str, str]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    counts: Counter = Counter()
    failures = []
    statuses = sorted((root / 'test').glob('*/status.json'))
    for path in statuses:
        status = json.loads(path.read_text())
        counts[status['status']] += 1
        if status['error']:
            failures.append({'task': status['task'], 'error': status['error']})
        final_path = path.with_name('final.json')
        if not final_path.exists():
            continue
        final = json.loads(final_path.read_text())
        for rank, finding in enumerate(final['findings']):
            cls = finding['class']
            if cls not in category:
                raise ValueError(f'unknown reader class: {cls}')
            summary = '\n'.join(str(s) for s in (finding.get('title'), finding['summary'],
                                finding.get('instruction_quote'), finding.get('grader_quote')) if s)
            rows.append({'schema': 'qa.finding/v1', 'id': f"cm1:{root.name}:{status['aid']}:{rank}",
                         'verdict': 'fail', 'subject': {'kind': 'task',
                         'task_id': f"terminal-bench/{status['task']}"},
                         'category': category[cls], 'taxonomy_class': cls,
                         'summary': summary, 'evidence_refs': finding.get('evidence_refs') or [],
                         'confidence': finding.get('confidence'), 'rank': rank, 'aid': status['aid']})
    return rows, {'status_counts': dict(counts), 'tasks_with_status': len(statuses),
                  'complete_tasks': counts['complete'], 'abstained_tasks': counts['abstained'],
                  'failures': failures}


def v1_rows(root: Path) -> tuple[list[dict], dict]:
    rows = []
    counts: Counter = Counter()
    failures = []
    statuses = sorted((root / 'test/runs').glob('*/status.json'))
    for path in statuses:
        status = json.loads(path.read_text())
        counts[status['status']] += 1
        if status['error']:
            failures.append({'task': status['task'], 'error': status['error']})
        for finding_path in sorted((path.parent / 'findings').glob('*.json')):
            row = json.loads(finding_path.read_text())
            row['id'] = f"cm1:v1:{root.name}:{status['sid']}:{finding_path.stem}"
            rows.append(row)
    return rows, {'status_counts': dict(counts), 'tasks_with_status': len(statuses),
                  'complete_tasks': counts['complete'], 'abstained_tasks': 0,
                  'failures': failures}


def cost_for(events: list[dict], prefix: str) -> dict:
    started = [e for e in events if e['event'] == 'started' and e['identity'].startswith(prefix)]
    settled = {e['call_id']: e for e in events if e['event'] == 'settled'}
    known = sum(settled[e['call_id']]['cost_nano'] for e in started if e['call_id'] in settled)
    unknown = [e for e in started if e['call_id'] not in settled]
    return {'model_calls': len(started), 'settled_calls': len(started) - len(unknown),
            'billed_usd': known / 1e9 if not unknown else None,
            'known_billed_usd': known / 1e9,
            'unknown_cost_calls': len(unknown),
            'retained_unknown_reservation_usd': sum(e['reserved_nano'] for e in unknown) / 1e9,
            'enforced_charge_usd': (known + sum(e['reserved_nano'] for e in unknown)) / 1e9,
            'cost_basis': 'matching OpenRouter generation.total_cost; missing stays unknown'}


def score(rows: list[dict], output: Path) -> dict:
    predictions = output.with_suffix('.predictions.jsonl')
    predictions.write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
    subprocess.run([sys.executable, str(KEY / 'score_audit.py'), '--predictions', str(predictions),
                    '--out', str(output)], check=True, capture_output=True, cwd=REPO)
    raw = json.loads(output.read_text())
    return {mode: raw['modes'][mode]['headline']['test'] for mode in MODES}


def make_plot(result: dict) -> str:
    """Opus is a horizontal reference, not a fabricated dollar-coordinate."""
    width, height, left, right, top, bottom = 1000, 610, 90, 710, 60, 480
    points = [r for r in result['rows'] if r['measurement_complete'] and r['test_cost']['billed_usd'] is not None]
    xmax = max([r['test_cost']['billed_usd'] for r in points] + [0.05]) * 1.2
    ymax = 152
    def x(cost):
        return left + (right - left) * cost / xmax

    def y(hits):
        return bottom - (bottom - top) * hits / ymax

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<g font-family="sans-serif" fill="#17212b">',
           '<text x="90" y="27" font-size="20">TB4 QA: observed finding matches versus billed model cost</text>']
    for tick in (0, 38, 76, 114, 152):
        yy = y(tick)
        svg += [f'<path d="M{left},{yy} H{right}" stroke="#e4e9ed"/>',
                f'<text x="78" y="{yy + 5}" text-anchor="end" font-size="13">{tick}</text>']
    for step in range(5):
        value = xmax * step / 4
        xx = x(value)
        svg += [f'<path d="M{xx},{top} V{bottom}" stroke="#eef0f2"/>',
                f'<text x="{xx}" y="503" text-anchor="middle" font-size="12">${value:.3f}</text>']
    for label, hits, color in [('Opus plain (historical)', 78, '#777777'), ('Opus v1 (historical)', 80, '#222222')]:
        yy = y(hits)
        svg += [f'<path d="M{left},{yy} H{right}" stroke="{color}" stroke-dasharray="7,5"/>']
        label_y = yy + (20 if hits == 78 else -9)
        svg += [f'<text x="725" y="{label_y}" font-size="12">{label}: {hits}/152</text>']
    colors = ['#1565c0', '#9c27b0', '#008577', '#b45309']
    models = list(dict.fromkeys(r['model'] for r in result['rows']))
    for index, row in enumerate(points):
        xx = x(row['test_cost']['billed_usd'])
        yy = y(row['score']['category_1to1']['recall']['k'])
        color = colors[models.index(row['model']) % len(colors)]
        if row['protocol'] == 'plain':
            svg += [f'<circle cx="{xx}" cy="{yy}" r="6" fill="{color}"/>']
        else:
            svg += [f'<rect x="{xx - 6}" y="{yy - 6}" width="12" height="12" fill="{color}"/>']
        label = html.escape(f"{row['slug']} / {row['protocol']}")
        svg += [f'<text x="{xx + 9}" y="{yy + (-10 if index % 2 else 16)}" font-size="12">{label}</text>']
    svg += [f'<path d="M{left},{top} V{bottom} H{right}" stroke="#17212b" fill="none"/>',
            '<text x="400" y="537" text-anchor="middle" font-size="14">TEST billed OpenRouter model cost (USD; DEV cost separate)</text>',
            '<text x="22" y="270" transform="rotate(-90 22,270)" text-anchor="middle" font-size="14">Category one-to-one matches / 152 gold findings</text>',
            '<text x="90" y="566" font-size="12">Only complete, fully billed TEST runs are plotted. Circles: plain; squares: v1.</text>',
            '<text x="90" y="587" font-size="12">Opus API-equivalent cost is unmeasured; subscription marginal $0 is not a fair API-price coordinate.</text>',
            '</g></svg>']
    return '\n'.join(svg) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--publish', type=Path)
    args = parser.parse_args()
    harness = load_module('cm1_original_v1', HERE.parent / 'tb4-qa-agent-v1-20261020/harness.py')
    models = json.loads((HERE / 'models.json').read_text())['models']
    journal = args.out / 'cost.jsonl'
    events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    work = args.out / 'scoring'
    work.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {'schema': 'cm1.cost-quality-frontier/v1', 'key': 'tb4-audit-v1',
        'metric': 'category_1to1 TEST finding matches; source-only claims, not confirmed exploits',
        'budget_cap_usd': 4, 'rows': [], 'historical_reference': {
            'source': 'research/experiments/tb4-qa-agent-v1-20261020/results-test.json',
            'source_sha256': hashlib.sha256((HERE.parent / 'tb4-qa-agent-v1-20261020/results-test.json').read_bytes()).hexdigest(),
            'plain': {'hits': 78, 'findings': 152, 'api_equivalent_usd': None},
            'v1': {'hits': 80, 'findings': 152, 'api_equivalent_usd': None}},
        'external_context': {'url': 'https://usetokenless.com/eval3/',
            'local_copy': 'research-context/sources/links/usetokenless.com/2026-10-20-eval-leaderboard-tokenless.md',
            'glm_5_3_flash': {'elo': 826, 'total_cost_usd': 4.75},
            'opus': {'elo': 1120, 'total_cost_usd': 335},
            'limitation': '65-environment Elo/total-spend comparison, not TB4 finding recall or a transferable dollar price'}}
    for model in models:
        for protocol in ('plain', 'v1'):
            root = args.out / protocol / model['slug']
            rows, coverage = plain_rows(root, harness.CATEGORY) if protocol == 'plain' else v1_rows(root)
            attempted = (root / 'test-exposure-claim.json').exists()
            cells = score(rows, work / f"{protocol}-{model['slug']}.score.json") if attempted else None
            freeze = root / 'freeze.json'
            result['rows'].append({'model': model['id'], 'slug': model['slug'], 'protocol': protocol,
                'provider': model['provider'], 'test_started': attempted, 'coverage': coverage,
                'measurement_complete': attempted and coverage['tasks_with_status'] == 41
                    and not coverage['failures']
                    and coverage['complete_tasks'] + coverage['abstained_tasks'] == 41,
                'score': cells, 'test_cost': cost_for(events, f"{protocol}/{model['slug']}/test/"),
                'dev_cost': cost_for(events, f"{protocol}/{model['slug']}/dev/"),
                'freeze_sha256': hashlib.sha256(freeze.read_bytes()).hexdigest() if freeze.exists() else None})
    result['campaign_cost'] = cost_for(events, '')
    target = args.publish or work
    target.mkdir(parents=True, exist_ok=True)
    (target / 'frontier.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    (target / 'frontier.svg').write_text(make_plot(result))
    print(json.dumps({'artifact': str(target / 'frontier.json'), 'complete_configurations':
                      sum(r['measurement_complete'] for r in result['rows']),
                      'campaign_cost': result['campaign_cost']}))


if __name__ == '__main__':
    main()
