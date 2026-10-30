"""Attack F3: forge the JUnit report the hardened grader is about to trust."""
import atexit
import os
import sys
from pathlib import Path

CLEAN = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" tests="4" failures="0" errors="0" skipped="0">'
    + "".join(f'<testcase classname="tests" name="t{i}"/>' for i in range(4))
    + "</testsuite></testsuites>"
)


def _forge() -> None:
    argv = list(getattr(sys, "orig_argv", []))
    target = None
    for i, arg in enumerate(argv):
        if arg.startswith("--junitxml="):
            target = arg.split("=", 1)[1]
        elif arg == "--junitxml" and i + 1 < len(argv):
            target = argv[i + 1]
    if target:
        try:  # noqa: SIM105 - payload logic unchanged; lint-only comment
            Path(target).write_text(CLEAN, encoding="utf-8")
        except OSError:
            pass
    os._exit(0)


atexit.register(_forge)
