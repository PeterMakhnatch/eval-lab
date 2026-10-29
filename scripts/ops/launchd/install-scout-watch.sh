#!/bin/bash
# Install credential-free incremental DEV scanning from a committed source snapshot.
#
# One authority for the paths: the LaunchAgent arguments are rendered by the
# snapshot's own `evallab.readers.scout_watch.watch_arguments`, so the scheduled
# job and the module defaults always name the same actor projection. Every
# projection gets its own key map, result ledger, transcript cache and scan tree;
# caches from another projection are left untouched and never read.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LABEL=com.petermakhnatch.evallab.scout-watch
RUNTIME="$HOME/Library/Application Support/evallab/scout-watch"
STATE="$HOME/.local/state/evallab-prod-live/trace-tools"
DB="$HOME/.local/state/evallab-prod-live/cloud-trace-db.sqlite"
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
PROJECT="$(dirname "$COMMON")/derived/trace-lab/scout"
INTERVAL=300
BATCH=128
LOAD=0
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --label) LABEL="$2"; shift 2 ;;
    --runtime-dir) RUNTIME="$2"; shift 2 ;;
    --state-dir) STATE="$2"; shift 2 ;;
    --db-path) DB="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --batch-size) BATCH="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ ! "$LABEL" =~ ^[A-Za-z0-9.-]+$ ]] || [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]] || [[ ! "$BATCH" =~ ^[1-9][0-9]*$ ]]; then
  echo "label must be alphanumeric/dot/dash; interval and batch size must be positive integers" >&2
  exit 2
fi
DOMAIN="gui/$(id -u)"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ "$UNINSTALL" -eq 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$DEST"
  echo "removed: $DEST (evidence and runtime retained)"
  exit 0
fi
if [ ! -f "$DB" ]; then
  echo "no trace database at $DB" >&2; exit 1
fi
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
  echo "commit changes before installing the source snapshot" >&2; exit 1
fi
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
SNAPSHOT="$RUNTIME/runtime/$COMMIT"
if [ ! -f "$SNAPSHOT/READY" ]; then
  mkdir -p "$SNAPSHOT"
  ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/evallab-scout-watch.XXXXXX")"
  trap 'rm -f "$ARCHIVE"' EXIT
  git -C "$ROOT" archive --format=tar --output="$ARCHIVE" "$COMMIT"
  tar -xf "$ARCHIVE" -C "$SNAPSHOT"
  uv sync --frozen --no-default-groups --project "$SNAPSHOT"
  uv pip install --python "$SNAPSHOT/.venv/bin/python" "inspect-scout==0.5.4" "duckdb==1.5.5"
  printf '%s\n' "$COMMIT" > "$SNAPSHOT/READY"
fi
mkdir -p "$STATE" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/evallab"
"$SNAPSHOT/.venv/bin/python" -I - "$LABEL" "$DEST" "$SNAPSHOT" "$STATE" "$DB" "$PROJECT" "$INTERVAL" "$BATCH" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

from evallab.readers import ACTOR_PROJECTION_VERSION
from evallab.readers.scout_watch import argument_argv, watch_arguments

label, dest, runtime, state, db, project, interval, batch = sys.argv[1:]
source, state, db, project = (str(Path(p).expanduser().resolve()) for p in (runtime, state, db, project))
logs = Path.home() / "Library/Logs/evallab"
# Resolved from the installed snapshot's own code, so code, admission authority
# and cache paths are always the same committed revision.
arguments = watch_arguments(
    repo_root=Path(source), db_path=Path(db), trace_tools=Path(state),
    viewer=Path(project), batch_size=int(batch),
)
config = {
    "Label": label,
    "ProgramArguments": [
        source + "/.venv/bin/python", "-I", "-m", "evallab.readers.scout_watch",
        *argument_argv(arguments),
    ],
    "WorkingDirectory": source,
    "StartInterval": int(interval), "RunAtLoad": True, "ProcessType": "Background",
    "LowPriorityIO": True, "Nice": 10,
    "EnvironmentVariables": {
        "HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "HARBOR_TELEMETRY": "off", "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    },
    "StandardOutPath": str(logs / (label + ".log")),
    "StandardErrorPath": str(logs / (label + ".error.log")),
}
path = Path(dest)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(
    f"installed: {path}\nruntime: {source}\nprojection: {ACTOR_PROJECTION_VERSION}\n"
    f"state: {arguments['state_dir']}\ntranscripts: {arguments['transcripts_dir']}\n"
    f"scans: {arguments['scans_dir']}\n"
    f"interval: {interval}s; batch: {batch}; no keys/model calls"
)
PY
if [ "$LOAD" -eq 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$DEST"
  echo "loaded: $DOMAIN/$LABEL"
fi
