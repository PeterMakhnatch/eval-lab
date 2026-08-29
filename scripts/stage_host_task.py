#!/usr/bin/env python3
"""Stage a canonical task package into a host-adapted execution copy.

Thin CLI over ``evallab.host_task_staging.stage_task_for_host`` following the
``harbor_network`` conventions (pure functions, typed records, fail-closed
validation). Every adaptation is explicit on the command line: platform pins
and agent public egress are never applied unless requested. The staged copy
carries a typed ``run_manifest.json`` recording the adapter digest/version,
requested/effective networks, platform reason, and source/staged digests.

TRUSTED-TASK-ONLY LANE: attaching public egress to ``main`` exists so a
reviewed agent (e.g. the Z.ai/OpenCode credential-mount lane) can reach its
provider. The staged run is NOT network-isolated and is NOT proxy-grade
credential isolation; do not use it for untrusted tasks.

Usage:
    python scripts/stage_host_task.py SOURCE DESTINATION \
        [--pin-platform] [--attach-agent-egress] \
        [--platform linux/amd64] [--platform-reason TEXT]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evallab.host_task_staging import (  # noqa: E402
    TRUSTED_WHEELHOUSE_PLATFORM,
    stage_task_for_host,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="canonical task package directory")
    parser.add_argument("destination", type=Path, help="staging destination (must not exist)")
    parser.add_argument(
        "--pin-platform",
        action="store_true",
        help=(
            "pin compose services and environment/tests Dockerfiles to "
            "--platform for the reviewed trusted wheelhouse target"
        ),
    )
    parser.add_argument(
        "--attach-agent-egress",
        action="store_true",
        help=(
            "attach the public default network to the 'main' service only; "
            "the MCP sidecar stays internal-only"
        ),
    )
    parser.add_argument(
        "--platform",
        default=TRUSTED_WHEELHOUSE_PLATFORM,
        help=f"target platform when pinning (default: {TRUSTED_WHEELHOUSE_PLATFORM})",
    )
    parser.add_argument(
        "--platform-reason",
        default=None,
        help="recorded justification overriding the default platform reason",
    )
    args = parser.parse_args(argv)

    manifest = stage_task_for_host(
        args.source,
        args.destination,
        pin_platform=args.pin_platform,
        attach_agent_egress=args.attach_agent_egress,
        platform=args.platform,
        platform_reason=args.platform_reason,
    )
    print(json.dumps(asdict(manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
