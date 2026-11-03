"""Print one ledger record by id: claim (C-), insight (I-), signal (S-), pass (P-) or experiment."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.recompute import describe_recompute
from evallab.insights.store import DB_NAME, InsightStore


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("show", help="Show a claim, insight, signal, pass or experiment")
    parser.add_argument("record_id")
    add_state_dir(parser)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    record_id: str = args.record_id
    with InsightStore.open_readonly(state_dir(args) / DB_NAME) as store:
        found: object | None
        if record_id.startswith("C-"):
            claim = store.get_claim(record_id)
            found = None if claim is None else {
                "claim": claim.model_dump(mode="json"),
                "recompute_summary": describe_recompute(claim.recompute),
                "rederivations": [r.model_dump(mode="json") for r in store.list_rederivations(claim_id=record_id)],
            }
        elif record_id.startswith("I-"):
            insight = store.get_insight(record_id)
            review = store.latest_review(record_id)
            found = None if insight is None else {
                "insight": insight.model_dump(mode="json"),
                "human_review": None if review is None else review.model_dump(mode="json"),
                "predictions": [
                    p.model_dump(mode="json")
                    for p in store.list_predictions()
                    if p.insight_id == record_id
                ],
            }
        elif record_id.startswith("S-"):
            matches = [s for s in store.list_signals(status=None, limit=100_000) if s.signal_id == record_id]
            found = matches[0].model_dump(mode="json") if matches else None
        elif record_id.startswith("P-"):
            record = store.get_pass(record_id)
            found = None if record is None else record.model_dump(mode="json")
        else:
            experiment = store.get_experiment(record_id)
            found = None if experiment is None else {
                "experiment": experiment.model_dump(mode="json"),
                "claims": [
                    {"claim_id": c.claim_id, "statement": c.claim.statement, "check": c.check.status,
                     "recompute": describe_recompute(c.recompute)}
                    for c in store.list_claims(experiment=experiment.exp_id, include_failed=True)
                ],
            }
    if found is None:
        print(f"error: no record {record_id}", file=sys.stderr)
        return 2
    emit(found)
    return 0
