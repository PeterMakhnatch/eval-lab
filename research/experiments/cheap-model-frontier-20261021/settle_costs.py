"""Reconcile saved generation IDs once; this command never invokes inference."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import run_plain as cm

from evallab.interpretation.monitor_agent import OpenAIInvestigator


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=cm.DEFAULT_OUT)
    args = parser.parse_args()
    models = json.loads((cm.HERE / 'models.json').read_text())['models']
    reconciled = 0
    for model in models:
        transport = OpenAIInvestigator(endpoint='https://openrouter.ai/api/v1', model=model['id'],
                                      api_key=os.environ['OPENROUTER_API_KEY'])
        reconciled += cm.reconcile_model(args.out, model, transport)
    journal = args.out / 'cost.jsonl'
    final = [json.loads(line) for line in journal.read_text().splitlines()]
    print(json.dumps({'settled_now': reconciled, 'enforced_charge_usd': cm.charged(final) / 1e9,
                      'model_calls': sum(e['event'] == 'started' for e in final),
                      'settled_calls': sum(e['event'] == 'settled' for e in final)}))


if __name__ == '__main__':
    main()
