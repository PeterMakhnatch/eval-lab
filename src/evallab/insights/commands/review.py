"""Record a named human's verdict on an insight (the only acceptance path)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.models import HumanReview, short_digest
from evallab.insights.store import DB_NAME, InsightStore
from evallab.verdicts import ALLOWED_STATUSES, validate_human_actor, validate_status


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "review", help="Accept, reject or hold an insight (human reviewers only)"
    )
    parser.add_argument("insight_id")
    parser.add_argument("status", choices=list(ALLOWED_STATUSES))
    parser.add_argument("--by", required=True, help="Human reviewer name, e.g. 'Peter Makhnatch'")
    parser.add_argument("--note", default=None)
    add_state_dir(parser)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    by = validate_human_actor(args.by)
    status = validate_status(args.status)
    at = datetime.now(UTC)
    with InsightStore(state_dir(args) / DB_NAME) as store:
        if store.get_insight(args.insight_id) is None:
            print(f"error: unknown insight id {args.insight_id}", file=sys.stderr)
            return 2
        review = HumanReview(
            review_id="R-" + short_digest(args.insight_id, by, at.isoformat()),
            insight_id=args.insight_id,
            status=status,
            by=by,
            note=args.note,
            at=at,
        )
        store.add_review(review)
    emit(review.model_dump(mode="json"))
    return 0
