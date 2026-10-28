"""BLOCKED forward entrypoint; not a tool-safe transport implementation.

The frozen CLI has no established empty dispatch registry and zero-offered-tools
proof. All future TT-7 native generation is therefore denied BEFORE launching the
archived runner, reading a prompt/entitlement, refreshing quota or invoking any
model/tool handler. No flag, fresh quota, status JSON or entitlement can enable it.
The archived continue_codex.py remains exact as-executed historical evidence.

The offline synthetic test proves this admission STOP prevents transport launch;
it does NOT prove that OMP itself would deny an unsolicited tool call. Resumption
requires a separately reviewed implementation and authorization, not this flag set.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

REASON = (
    "TT-7 native generation BLOCKED: the frozen CLI has no verified empty dispatch "
    "registry plus zero offered tools. Flags and post-return parsing are not a "
    "pre-execution capability boundary. No model or quota call is authorized."
)


class NativeInferenceBlocked(RuntimeError):
    """Terminal pre-transport denial; never a model-negative reading."""


def require_native_boundary(_launch: Callable[[], object]) -> NoReturn:
    """Deny all launches, including a transport with a queued synthetic tool call."""
    raise NativeInferenceBlocked(REASON)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--entitlement", type=Path)
    args = parser.parse_args(argv)
    archived = Path(__file__).with_name("continue_codex.py")
    command = [sys.executable, str(archived), "run"]
    if args.entitlement is not None:
        command.extend(["--entitlement", str(args.entitlement)])
    try:
        require_native_boundary(lambda: subprocess.run(command, check=True))
    except NativeInferenceBlocked:
        print(json.dumps({
            "schema": "tt7.native_generation_gate/v1", "status": "BLOCKED",
            "native_generation_allowed": False, "reason": REASON,
            "model_calls": 0, "quota_queries": 0, "tool_handlers_invoked": 0,
            "cli_dispatch_denial_proven": False,
        }, sort_keys=True))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
