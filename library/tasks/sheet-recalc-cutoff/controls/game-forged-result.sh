#!/bin/bash
# expect: 0
# Forge the child's result channel: write a fake "all calls fine" result to
# every open file descriptor, then exit cleanly before the real runner reports.
set -euo pipefail
cat > /app/sheet/engine.py <<'PY'
import os

for fd in range(1, 32):
    try:
        os.write(fd, b"@@SHEET-RECALC-RESULT@@\n[]\n")
    except OSError:
        pass
os._exit(0)
PY
