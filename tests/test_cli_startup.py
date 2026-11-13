"""The console entry point preserves the full CLI's version route."""

from __future__ import annotations

import json
import subprocess
import sys

PROBE = """
import sys
from {module} import main
try:
    main({argv!r})
except SystemExit as stop:
    code = stop.code
else:
    code = 0
sys.stderr.write(json.dumps({{"code": code, "full_cli": "evallab.cli" in sys.modules,
                              "workers": "evallab.production.seeker_worker" in sys.modules}}))
"""


def run(module: str, argv: list[str]) -> tuple[str, dict[str, object]]:
    done = subprocess.run(
        [sys.executable, "-c", "import json\n" + PROBE.format(module=module, argv=argv)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    stderr = done.stderr.strip()
    state = json.loads(stderr[stderr.rindex("{"):])
    return done.stdout, state


def test_other_commands_still_reach_the_full_cli() -> None:
    out, state = run("evallab.entry", ["--version"])
    assert out.strip() and state["full_cli"] is True
