"""``evallab failures loop canary``: manual DEV integration heartbeat."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.failure_library.loop.canary import canary


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    result = canary(
        root,
        dry_run=args.dry_run,
        state_root=args.state_root,
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        max_items=args.max_items,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if args.dry_run or result["status"] == "ok" else 1


def _run_readonly(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.failure_library.loop.readonly import diagnostic_pilot

    result = diagnostic_pilot(
        args.manifest,
        args.pack_dir,
        args.output_dir,
        lane=args.lane,
        dry_run=args.dry_run,
        allow_paid=args.allow_paid,
        allow_curated=args.allow_curated_fallback,
        max_usd=args.max_usd,
        per_request_usd=args.per_request_usd,
        model=args.model,
        endpoint=args.endpoint,
        api_key_env=args.api_key_env,
        input_price=args.input_usd_per_million,
        output_price=args.output_usd_per_million,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] in {"planned", "completed"} else 1


def register(subparsers: Any) -> None:
    loop = subparsers.add_parser(
        "loop", help="Manual Failure Library consumer heartbeat (no scheduler)"
    )
    commands = loop.add_subparsers(dest="failure_loop_command", required=True)
    command = commands.add_parser(
        "canary", help="Freeze DEV targets, compare knowledge OFF/ON and retain honest failures"
    )
    command.add_argument(
        "--dry-run",
        action="store_true",
        help="Offline deterministic plan; no writes or child commands",
    )
    command.add_argument(
        "--state-root",
        type=Path,
        help="Library state root (default: EVALLAB_FAILURE_LIBRARY_STATE)",
    )
    command.add_argument(
        "--manifest",
        type=Path,
        help="Frozen slice JSON (default: output-dir/slice.json; never rerolled)",
    )
    command.add_argument(
        "--output-dir",
        type=Path,
        help="Small public receipts (default: research/failure-library/loop)",
    )
    command.add_argument(
        "--max-items",
        type=int,
        default=2,
        help="Balanced execution subset per agent/arm, 1..40 (default: 2)",
    )
    command.set_defaults(func=_run)
    pilot = commands.add_parser(
        "readonly-pilot",
        help="Bounded public-source Task/Trace diagnostic; no tools, replay or native Harbor claim",
    )
    pilot.add_argument(
        "--manifest", type=Path, required=True, help="Digest-frozen private source-view manifest"
    )
    pilot.add_argument(
        "--pack-dir", type=Path, required=True, help="Actual sanitized compiled defensive packet"
    )
    pilot.add_argument(
        "--output-dir", type=Path, required=True, help="New private immutable receipt directory"
    )
    pilot.add_argument(
        "--dry-run", action="store_true", help="Validate inputs and caps offline; write nothing"
    )
    pilot.add_argument(
        "--allow-paid", action="store_true", help="Explicitly authorize the bounded model requests"
    )
    pilot.add_argument(
        "--allow-curated-fallback",
        action="store_true",
        help="Label source-independent taxonomy separately from empirical retrieval",
    )
    pilot.add_argument("--lane", choices=("omp", "openai"), default="omp")
    pilot.add_argument("--max-usd", type=float, default=0)
    pilot.add_argument("--per-request-usd", type=float, default=0)
    pilot.add_argument("--model", required=True)
    pilot.add_argument(
        "--endpoint", choices=("https://api.deepseek.com", "https://api.z.ai/api/paas/v4")
    )
    pilot.add_argument("--api-key-env", help="Environment variable NAME, never a key value")
    pilot.add_argument("--input-usd-per-million", type=float)
    pilot.add_argument("--output-usd-per-million", type=float)
    pilot.set_defaults(func=_run_readonly)
