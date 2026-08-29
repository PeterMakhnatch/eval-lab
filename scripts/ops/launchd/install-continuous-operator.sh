#!/bin/bash
# Render the disabled launchd unit with absolute macOS state/log paths.
# Precreates 0700 directories and 0600 log files. Does not launchctl load.
# No services or runs authorized.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOME_DIR="${EVAL_LAB_OPERATOR_HOME:-$HOME}"
DEST="${1:-}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
python3 - "$ROOT" "$HOME_DIR" "$DEST" <<'PY'
import sys
from pathlib import Path

from evallab.ops_continuous import render_launchd_plist

root = Path(sys.argv[1])
home = Path(sys.argv[2])
dest_arg = sys.argv[3]
template = root / "scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist"
if dest_arg:
    dest = Path(dest_arg)
else:
    dest = home / "Library/Application Support/EvalLab/com.petermakhnatch.evallab.continuous-operator.plist"
render_launchd_plist(template, dest, home=home)
print(f"rendered={dest}")
print("loaded=no")
print("launchctl=skipped")
PY
