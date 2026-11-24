from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

API_KEY = "OPENROUTER_API_KEY"
MANAGEMENT_KEY = "OPENROUTER_MANAGEMENT_KEY"
LANE_PREFIX = "OPENROUTER_KEY_"
KEY_ENV = re.compile(r"OPENROUTER_KEY_[A-Z][A-Z0-9_]{0,63}")


def valid_name(value: object) -> bool:
    return isinstance(value, str) and KEY_ENV.fullmatch(value) is not None


def protected(name: str) -> bool:
    return name in (API_KEY, MANAGEMENT_KEY) or name.startswith(LANE_PREFIX)


def scrub_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if not protected(name)}


def arm_environment(source: str, environ: Mapping[str, str]) -> dict[str, str] | None:
    key = environ.get(source)
    if not key:
        return None
    env = scrub_environment(environ)
    env[API_KEY] = key
    return env


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3 or not valid_name(args[0]) or args[1] != "--" or not args[2]:
        print("campaign: keyenv: expected OPENROUTER_KEY_<LANE> -- COMMAND [ARGS]", file=sys.stderr)
        return 2
    source = args[0]
    env = arm_environment(source, os.environ)
    if env is None:
        print(f"campaign: keyenv: {source} is missing or empty; arm command refused", file=sys.stderr)
        return 1
    command = args[2:]
    try:
        os.execvpe(command[0], command, env)
    except (OSError, ValueError):
        print("campaign: keyenv: cannot execute the arm command", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
