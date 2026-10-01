#!/usr/bin/env python3
"""Standalone live telemetry sampler for overnight model eval rounds (HAR-126).

Polls every 10-15s into JSONL until stopped:
- SGLang Prometheus /metrics (num_running_reqs, num_queue_reqs, gen_throughput, token_usage)
- Modal active container count and GPU utilization via nvidia-smi
- Lab trial status (queue / running / finished)

Usage:
    python3 scripts/telemetry_sampler.py --out ~/Developer/eval-lab-results/$(date +%F)/g2-telemetry.jsonl
    python3 scripts/telemetry_sampler.py --out runs/g2-telemetry.jsonl --runs-dir runs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo's src/ is on sys.path if invoked directly
_repo_src = Path(__file__).resolve().parent.parent / "src"
if _repo_src.is_dir() and str(_repo_src) not in sys.path:
    sys.path.insert(0, str(_repo_src))

from evallab.run_telemetry import (  # noqa: E402
    DEFAULT_METRICS_URL,
    DEFAULT_MODAL_APP,
    run_sampler,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll live SGLang metrics, Modal container count, and lab status into JSONL."
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination JSONL path (e.g. ~/Developer/eval-lab-results/<date>/<round>-telemetry.jsonl)",
    )
    parser.add_argument(
        "--metrics-url",
        default=DEFAULT_METRICS_URL,
        help=f"SGLang /metrics Prometheus endpoint (default: {DEFAULT_METRICS_URL})",
    )
    parser.add_argument(
        "--modal-app",
        default=DEFAULT_MODAL_APP,
        help=f"Modal app ID/name filter (default: {DEFAULT_MODAL_APP})",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Lab runs/ directory to monitor running/finished trials",
    )
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=None,
        help="Lab queue/ directory to monitor approved/running/waiting tasks",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="Polling interval in seconds (default: 15.0)",
    )
    args = parser.parse_args()

    print(
        f"Starting telemetry sampler -> {args.out} (interval: {args.interval}s, Ctrl-C to stop)",
        file=sys.stderr,
    )
    try:
        run_sampler(
            args.out,
            metrics_url=args.metrics_url,
            runs_dir=args.runs_dir,
            modal_app=args.modal_app,
            queue_dir=args.queue_dir,
            interval_s=args.interval,
        )
    except KeyboardInterrupt:
        print("\nSampler stopped by operator.", file=sys.stderr)


if __name__ == "__main__":
    main()
