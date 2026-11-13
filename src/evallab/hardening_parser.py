"""Package hardening command registration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _harden_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.full_hardening import HardeningRefused, harden

    try:
        manifest = harden(
            args.package,
            args.out,
            audit_only=args.audit_only,
            expected_tests=args.expected_tests,
            expected_skipped=args.expected_skipped,
            calibrate=args.calibrate,
            helpers=tuple(args.helper),
            verifier_dockerfile=args.verifier_dockerfile,
        )
    except HardeningRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(manifest.to_json(), indent=2, sort_keys=True))
        return 0
    print(
        f"{manifest.transform} {manifest.shape} package, workdir {manifest.workdir}, runner {manifest.runner}"
    )
    for layer in manifest.layers:
        print(f"  {layer.layer} {layer.status}: {layer.mechanism}")
    verifier = manifest.verifier
    print(
        f"  verifier gate_runtime {verifier.gate_runtime}"
        + (f" ({verifier.dockerfile} {verifier.origin}, sha256 {verifier.dockerfile_sha256})" if verifier.dockerfile else "")
    )
    for finding in manifest.findings:
        print(f"  finding {finding.kind} {finding.path}: {finding.detail} [{finding.action}]")
    print(f"  output: {manifest.output or '(audit only)'}")
    return 0


def register_parser(commands: argparse._SubParsersAction) -> None:
    harden_parser = commands.add_parser(
        "harden",
        help="full-hardening@1: L1 auto-load purge, L2 pristine tests/helpers, L3 report gate, L4 separation",
    )
    harden_parser.add_argument(
        "package", type=Path, help="MiMo test.sh or general Harbor task package"
    )
    harden_parser.add_argument("--out", type=Path, help="New directory for the hardened package")
    harden_parser.add_argument(
        "--audit-only", action="store_true", help="Report the plan; write nothing"
    )
    harden_parser.add_argument("--expected-tests", type=int, help="Reference test count (L3 gate)")
    harden_parser.add_argument("--expected-skipped", type=int, default=0)
    harden_parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Record report counts without gating (reference runs only)",
    )
    harden_parser.add_argument(
        "--helper",
        action="append",
        default=[],
        help="Workspace path the grader trusts (L2); repeatable",
    )
    harden_parser.add_argument(
        "--verifier-dockerfile",
        type=Path,
        help=(
            "Verifier image for the separate verifier (harbor packages): copied byte-identical to "
            "tests/Dockerfile; without it the gate runtime is recorded as unverified"
        ),
    )
    harden_parser.add_argument("--json", action="store_true")
    harden_parser.set_defaults(func=_harden_command)
