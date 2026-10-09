#!/bin/bash
# install-scout-view.sh — keep the Inspect Scout viewer always on at http://127.0.0.1:7576.
#
# Serves the Trace Lab Scout project (<data root>/derived/trace-lab/scout:
# scout.yaml, transcripts under data/, scan results anywhere under scans/).
# Like the results viewer, the runtime is its own venv outside every worktree,
# so pruning a worktree cannot break it. inspect-scout is not a repository
# dependency, so the pin lives here. Re-run to upgrade; --uninstall removes the job.
#
# Usage: install-scout-view.sh [--load] [--uninstall] [--port N] [--project DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LABEL=com.petermakhnatch.evallab.scout-view
PORT=7576
SCOUT_VERSION=0.5.4
STATE="$HOME/Library/Application Support/evallab/scout-view"
VENV="$STATE/venv"
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
PROJECT="$(dirname "$COMMON")/derived/trace-lab/scout"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/evallab"
DOMAIN="gui/$(id -u)"
LOAD=0
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

stop_job() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  # bootout returns before launchd drops the job; bootstrapping early fails with EIO (5).
  for _ in $(seq 1 30); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
    sleep 1
  done
}

if [ "$UNINSTALL" -eq 1 ]; then
  stop_job
  rm -f "$DEST"
  echo "removed: $DEST (venv left at $VENV)"
  exit 0
fi
if [ ! -f "$PROJECT/scout.yaml" ]; then
  echo "no Scout project at $PROJECT (expected scout.yaml)" >&2; exit 1
fi

WAS_LOADED=0
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  WAS_LOADED=1
  stop_job
fi
# The URL is the contract: refuse to install behind an ad hoc server on the port.
SQUATTERS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
if [ -n "$SQUATTERS" ]; then
  echo "port $PORT is held by another process; stop it first:" >&2
  ps -o pid=,command= -p "$(echo "$SQUATTERS" | paste -sd, -)" >&2
  exit 1
fi

mkdir -p "$STATE" "$LOGS" "$(dirname "$DEST")"
uv venv --clear --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" "inspect-scout==$SCOUT_VERSION"
INSTALLED_VERSION="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("inspect-scout"))')"
if [ "$INSTALLED_VERSION" != "$SCOUT_VERSION" ]; then
  echo "expected inspect-scout $SCOUT_VERSION, got $INSTALLED_VERSION" >&2; exit 1
fi
{
  echo "commit=$(git -C "$ROOT" rev-parse HEAD)"
  echo "installed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "inspect_scout=$INSTALLED_VERSION"
  echo "project=$PROJECT"
} > "$STATE/INSTALLED"
cat "$STATE/INSTALLED"

python3 - "$ROOT/scripts/ops/launchd/$LABEL.plist" "$DEST" "$VENV" "$PROJECT" "$PORT" "$LOGS" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

template, destination, venv, project, port, logs = sys.argv[1:]
project = str(Path(project).resolve())
config = plistlib.loads(Path(template).read_bytes())
# Scans are listed recursively, so per-card folders under scans/ show up too.
config["ProgramArguments"] = [
    venv + "/bin/scout", "view", project,
    "--host", "127.0.0.1", "--port", port, "--no-browser", "--display", "plain",
]
config["WorkingDirectory"] = project
config["EnvironmentVariables"]["HOME"] = str(Path.home())
config["StandardOutPath"] = str(Path(logs) / "scout-view.log")
config["StandardErrorPath"] = str(Path(logs) / "scout-view.error.log")
path = Path(destination)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}")
print(f"project: {project}")
print(f"url: http://127.0.0.1:{port}")
PY
if [ "$LOAD" -eq 1 ] || [ "$WAS_LOADED" -eq 1 ]; then
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$DEST"
  echo "loaded: $DOMAIN/$LABEL"
fi
