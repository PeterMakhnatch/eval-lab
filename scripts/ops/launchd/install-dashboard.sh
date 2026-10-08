#!/bin/bash
# install-dashboard.sh — keep the Eval Lab dashboard (Integrity + Operations) always on.
#
# Like the nightly refresh, the runtime is a `git archive` snapshot of this
# commit with its own locked venv, so pruning a feature worktree or a lagging
# primary checkout cannot change or break the running service. Data is read
# live: derived Parquet from the data root and published runs from the
# results home. Re-run to upgrade; --uninstall removes the job.
#
# Usage: install-dashboard.sh [--load] [--uninstall] [--port N] [--data-root DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LABEL=com.petermakhnatch.evallab.dashboard
PORT=8501
STATE="$HOME/.local/state/evallab-dashboard"
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
DATA_ROOT="$(dirname "$COMMON")"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/evallab"
LOAD=0
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ "$UNINSTALL" -eq 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$DEST"
  echo "removed: $DEST"
  exit 0
fi
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "commit changes before installing the dashboard source snapshot" >&2; exit 1
fi
mkdir -p "$STATE" "$LOGS" "$(dirname "$DEST")"
SNAPSHOT="$STATE/runtime/$COMMIT"
if [ ! -e "$SNAPSHOT/READY" ]; then
  rm -rf "$SNAPSHOT"
  mkdir -p "$SNAPSHOT"
  ARCHIVE="$(mktemp -t evallab-dashboard)"
  trap 'rm -f "$ARCHIVE"' EXIT
  git -C "$ROOT" archive --format=tar --output="$ARCHIVE" "$COMMIT"
  tar -xf "$ARCHIVE" -C "$SNAPSHOT"
  uv sync --frozen --no-default-groups --project "$SNAPSHOT"
  STREAMLIT="$("$SNAPSHOT/.venv/bin/python" -c 'from evallab.cli import STREAMLIT_REQUIREMENT; print(STREAMLIT_REQUIREMENT)')"
  uv pip install --python "$SNAPSHOT/.venv/bin/python" "$STREAMLIT"
  printf '%s\n' "$COMMIT" > "$SNAPSHOT/READY"
fi
python3 - "$ROOT/scripts/ops/launchd/$LABEL.plist" "$DEST" "$SNAPSHOT" "$DATA_ROOT" "$PORT" "$LOGS" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

template, destination, source, data, port, logs = sys.argv[1:]
source = str(Path(source).resolve())
config = plistlib.loads(Path(template).read_bytes())
config["ProgramArguments"] = [
    source + "/.venv/bin/python", "-m", "streamlit", "run", source + "/dashboard/app.py",
    "--server.address=127.0.0.1", f"--server.port={port}", "--server.headless=true",
    "--browser.gatherUsageStats=false", "--client.toolbarMode=viewer",
]
config["WorkingDirectory"] = source
config["EnvironmentVariables"]["HOME"] = str(Path.home())
config["EnvironmentVariables"]["PYTHONPATH"] = source
config["EnvironmentVariables"]["EVALLAB_DERIVED_ROOT"] = str(Path(data).resolve() / "derived/parquet")
config["StandardOutPath"] = str(Path(logs) / "dashboard.log")
config["StandardErrorPath"] = str(Path(logs) / "dashboard.error.log")
path = Path(destination)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}")
print(f"runtime: {source}")
print(f"url: http://127.0.0.1:{port}")
PY
if [ "$LOAD" -eq 1 ] || launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$DEST"
  echo "loaded: gui/$(id -u)/$LABEL"
fi
