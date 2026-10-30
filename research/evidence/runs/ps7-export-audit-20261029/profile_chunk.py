"""Run bounded incremental profiling without concurrent measurements."""

import subprocess
import sys
import time

started = time.monotonic()
while time.monotonic() - started < 110:
    import json
    from pathlib import Path

    progress = json.loads(Path("/private/tmp/ps7j2/results.json").read_text()).get("profile", {})
    if progress.get("runs", 0) >= 2000:
        break
    subprocess.run([sys.executable, "/private/tmp/ps7j2/profile_exports.py"], check=True)
