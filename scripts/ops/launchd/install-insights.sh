#!/bin/bash
# Install a committed runtime snapshot; never depend on a surviving worktree.
# Usage: install-insights.sh [--load] [--uninstall] [--state-dir DIR]
# Uninstall retains the ledger, pass receipts and runtime snapshots.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STATE="$HOME/.local/state/evallab-insights"
LOAD=0
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --state-dir)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "--state-dir requires a directory" >&2
        exit 2
      fi
      STATE="$2"
      shift 2
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
LABEL=com.petermakhnatch.evallab.insights
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/evallab"
# Removal works even when source is dirty or dependencies are unavailable.
if [ "$UNINSTALL" -eq 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$DEST"
  echo "removed: $DEST (runtime snapshots and state retained)"
  exit 0
fi
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
  echo "commit changes before installing the insights runtime snapshot" >&2
  exit 1
fi
# Resolve relative state paths before launchd changes the working directory.
STATE="$(python3 - "$STATE" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
SNAPSHOT="$STATE/runtime/$COMMIT"
mkdir -p "$STATE" "$LOGS" "$(dirname "$DEST")"
ARCHIVE=""
cleanup() {
  if [ -n "$ARCHIVE" ]; then
    rm -f "$ARCHIVE"
  fi
}
trap cleanup EXIT
if [ ! -e "$SNAPSHOT/READY" ]; then
  mkdir -p "$SNAPSHOT"
  ARCHIVE="$(mktemp -t evallab-insights)"
  git -C "$ROOT" archive --format=tar --output="$ARCHIVE" "$COMMIT"
  tar -xf "$ARCHIVE" -C "$SNAPSHOT"
  uv sync --frozen --no-default-groups --project "$SNAPSHOT"
  printf '%s\n' "$COMMIT" > "$SNAPSHOT/READY"
fi
python3 - "$SNAPSHOT/scripts/ops/launchd/$LABEL.plist" "$DEST" "$SNAPSHOT" "$STATE" "$LOGS" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

template, destination, source, state, logs = sys.argv[1:]
source = str(Path(source).resolve())
config = plistlib.loads(Path(template).read_bytes())
config["ProgramArguments"] = [
    source + "/.venv/bin/python", "-m", "evallab.cli", "insights", "watch",
    "--state-dir", state,
]
config["WorkingDirectory"] = source
config["StandardOutPath"] = str(Path(logs) / "insights.out.log")
config["StandardErrorPath"] = str(Path(logs) / "insights.err.log")
config["EnvironmentVariables"]["HOME"] = str(Path.home())
config["EnvironmentVariables"]["PATH"] = (
    str(Path.home() / ".local/bin")
    + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
config["EnvironmentVariables"]["PYTHONUNBUFFERED"] = "1"
config["EnvironmentVariables"]["EVALLAB_INSIGHTS_STATE"] = state
path = Path(destination)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}")
print(f"runtime: {source}")
print(f"state: {state}")
PY
if [ "$LOAD" -eq 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$DEST"
  echo "loaded: gui/$(id -u)/$LABEL (continuous watcher)"
fi
