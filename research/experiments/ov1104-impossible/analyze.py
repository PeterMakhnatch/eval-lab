#!/usr/bin/env python3
"""Report admitted DEV observations; grades alone never certify a label."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from admission import CONTRACT_VERSION, admitted_runs, object_digest

HERE = Path(__file__).resolve().parent
OUT = HERE / 'results'


def metric(k: int, n: int) -> dict[str, Any]:
    if not n:
        return {'k': k, 'n': n, 'rate': None, 'wilson_ci_95': None}
    p, z = k / n, 1.959964
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return {'k': k, 'n': n, 'rate': p, 'wilson_ci_95': [max(0, centre - half), min(1, centre + half)]}


def summarize(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [d for d in decisions if d['analysis_eligible']]
    labelled = [d for d in eligible if d['label'] is not None]
    groups = {}
    for model in sorted({d['run']['model'] for d in eligible}):
        subset = [d for d in eligible if d['run']['model'] == model]
        groups[model] = {
            'analysis_eligible': len(subset),
            'G1_pass_observation': metric(sum(d['run']['reward_G1'] == 1 for d in subset), len(subset)),
            'G2_pass_observation': metric(sum(d['run']['reward_G2'] == 1 for d in subset), len(subset)),
            'certified_positive_count': sum(d['label'] == 1 for d in subset),
            'operational_negative_count': sum(d['label'] == 0 for d in subset),
        }
    return {
        'generated_at': datetime.now(UTC).isoformat(),
        'contract_version': CONTRACT_VERSION,
        'available': bool(labelled),
        'notice': 'Grades are observations. Operational negatives do not establish universal honesty. No certified labels means unavailable metrics.',
        'corpus_sha256': object_digest([{k: v for k, v in d.items() if k != 'run'} for d in decisions]),
        'cohort': {'directories': len(decisions), 'analysis_eligible': len(eligible), 'labelled': len(labelled), 'excluded': len(decisions) - len(eligible)},
        'certified_positive_fraction_of_labelled': metric(sum(d['label'] == 1 for d in labelled), len(labelled)),
        'by_model': groups,
        'admissions': [{k: v for k, v in d.items() if k != 'run'} for d in decisions],
    }


def main() -> None:
    summary = summarize(admitted_runs())
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep historical derived bytes. No raw run artifact is changed.
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    previous = OUT / 'quarantined-generations' / stamp
    existing = [OUT / name for name in ('summary.json', 'summary.md') if (OUT / name).is_file()]
    if existing:
        previous.mkdir(parents=True, exist_ok=False)
        for path in existing:
            (previous / path.name).write_bytes(path.read_bytes())
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    text = ('# impossible-v1: admitted evidence\n\n'
            f"Available: {summary['available']}. Analysis-eligible runs: {summary['cohort']['analysis_eligible']}. "
            f"Certified labels: {summary['cohort']['labelled']}.\n\n"
            + summary['notice'] + '\n')
    (OUT / 'summary.md').write_text(text)
    print(text)


if __name__ == '__main__':
    main()
