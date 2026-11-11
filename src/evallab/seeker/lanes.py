"""Read subscription quota for retained historical study readers."""

from __future__ import annotations

import json
import subprocess


def codex_weekly_used() -> float | None:
    """Account-wide 7-day Codex used fraction from `omp usage` (None if unreadable)."""
    try:
        out = subprocess.run(
            ["omp", "usage", "--json", "--redact", "-p", "openai-codex"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        ).stdout
        doc = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    used = [
        float(limit["amount"]["usedFraction"])
        for report in doc.get("reports", [])
        if report.get("provider") == "openai-codex"
        for limit in report.get("limits", [])
        if (limit.get("window") or {}).get("id") == "7d"
    ]
    return max(used) if used else None
