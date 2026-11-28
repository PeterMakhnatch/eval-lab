from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from .harbor_seats import SeatEvidenceError, read_seat_evidence

_IDENTITY_FIELDS = ("seat", "under_test", "agent_name", "model_name", "model_provider")
_USAGE_FIELDS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
type _SeatKey = tuple[str, bool, str | None, str | None, str | None]


def _summarize(seats: list[dict[str, Any]]) -> dict[str, object]:
    summary: dict[str, object] = {"seat_results": len(seats)}
    for field in _USAGE_FIELDS:
        known = [value for seat in seats if (value := seat.get(field)) is not None]
        try:
            known_sum = (math.fsum(known) if field == "cost_usd" else sum(known)) if known else None
        except OverflowError:
            raise SeatEvidenceError("seat cost total is not finite") from None
        if isinstance(known_sum, float) and not math.isfinite(known_sum):
            raise SeatEvidenceError("seat cost total is not finite")
        summary[field] = {
            "known_sum": known_sum,
            "reported_seats": len(known),
            "complete": len(known) == len(seats),
        }
    return summary


def summarize_seat_usage(path: Path) -> dict[str, object]:
    receipt = read_seat_evidence(path)
    if receipt["issues"]:
        raise SeatEvidenceError("seat evidence has issues")

    trials = cast(list[dict[str, Any]], receipt["trials"])
    seats = [seat for trial in trials for seat in trial["seats"]]
    groups: dict[_SeatKey, list[dict[str, Any]]] = defaultdict(list)
    for seat in seats:
        key = (
            seat["seat"],
            seat["under_test"],
            seat["agent_name"],
            seat["model_name"],
            seat["model_provider"],
        )
        groups[key].append(seat)

    ordered = sorted(groups, key=lambda key: (key[0], key[1], *(part or "" for part in key[2:])))
    return {
        "schema": "harbor-seat-usage/v1",
        "job_id": receipt["job_id"],
        "scope": "retained_trial_results",
        "n_trials": len(trials),
        "total": _summarize(seats),
        "by_seat": [
            {**dict(zip(_IDENTITY_FIELDS, key, strict=True)), **_summarize(groups[key])}
            for key in ordered
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -S -m evallab.evidence.harbor_seat_usage",
        description="Summarize recorded seat usage from a completed Olam Harbor job or trial.",
    )
    parser.add_argument("path", type=Path, metavar="PATH", help="raw Harbor job or trial directory")
    args = parser.parse_args(argv)
    try:
        report = summarize_seat_usage(args.path)
        encoded = json.dumps(report, allow_nan=False, sort_keys=True, indent=2)
    except SeatEvidenceError as exc:
        print(f"harbor seat usage: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("harbor seat usage: usage total cannot be represented as JSON", file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
