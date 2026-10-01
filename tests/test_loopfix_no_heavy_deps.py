"""The live loop-fix path must not need Harbor-missing packages (HAR-116).

Wave A loopfix trials died with ``ModuleNotFoundError: No module named
'duckdb'`` because the detector reached ``evallab.traj`` through
``token_flow._is_edit``. The edit patterns now live in the stdlib-only
``evallab.edit_signals``. This runs the live path in a subprocess with the
heavy packages blocked, so a future import of one fails here, not live.
"""

from __future__ import annotations

import subprocess
import sys

BLOCKED = ["duckdb", "polars", "pandas", "pyarrow", "numpy"]

PROBE_TEMPLATE = """
import sys
for name in __BLOCKED__:
    sys.modules[name] = None
    sys.modules[name + ".parquet"] = None

from evallab.loopfix import cap_output, live_loop_action, loop_decision
from evallab.mimo_tool_calls import has_native_completion
from evallab.token_flow import _is_edit

step = {
    "source": "agent",
    "message": "working",
    "tool_calls": [
        {"function_name": "bash_command", "arguments": {"keystrokes": "pytest -q\\n"}}
    ],
}
steps = [dict(step) for _ in range(9)]
assert live_loop_action(steps[:4]) == "nudge", live_loop_action(steps[:4])
assert live_loop_action(steps) == "stop", live_loop_action(steps)
decision = loop_decision(steps)
assert (decision["nudge_call"], decision["stop_call"]) == (4, 9), decision
assert _is_edit(step) == (False, "")
assert callable(has_native_completion)
capped = cap_output("H" * 1500 + "M" * 9000 + "T" * 1500, "/logs/agent/evallab-output/step-0001.txt")
assert "step-0001.txt" in capped and capped.startswith("H" * 1000)
print("live loop-fix path works without heavy deps")
"""

PROBE = PROBE_TEMPLATE.replace("__BLOCKED__", repr(BLOCKED))


def test_live_loop_fix_without_heavy_deps() -> None:
    """The detector, cap, and completion check run with heavy deps absent."""
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "live loop-fix path works without heavy deps" in completed.stdout
