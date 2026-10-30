"""Acceptance wrapper: the project's export check must accept the batch export."""

from __future__ import annotations

import subprocess
import sys

WORKDIR = "/app"


def test_export_check_accepts_the_export() -> None:
    result = subprocess.run(
        [sys.executable, "tools/export_check.py"],
        cwd=WORKDIR,
        capture_output=True,
        text=True,
        timeout=240,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"tools/export_check.py rejected the export (exit {result.returncode}):\n{output}"
    )
    assert "PASS" in result.stdout, f"tools/export_check.py printed no PASS line:\n{output}"
