#!/bin/bash
# Install one guarded hosted-free reading per tick from a committed snapshot.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LABEL=com.petermakhnatch.evallab.docent-watch
RUNTIME="$HOME/Library/Application Support/evallab/docent-watch"
STATE="$HOME/.local/state/evallab-prod-live/trace-tools"
DB="$HOME/.local/state/evallab-prod-live/cloud-trace-db.sqlite"
INTERVAL=600
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
    --interval) INTERVAL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ ! "$LABEL" =~ ^[A-Za-z0-9.-]+$ ]] || [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "label must be alphanumeric/dot/dash; interval must be a positive integer" >&2
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
KEYS="$(command -v keys)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
  echo "commit changes before installing the source snapshot" >&2; exit 1
fi
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
SNAPSHOT="$RUNTIME/runtime/$COMMIT"
if [ ! -f "$SNAPSHOT/READY" ]; then
  mkdir -p "$SNAPSHOT"
  ARCHIVE="$(mktemp -t evallab-docent-watch)"
  trap 'rm -f "$ARCHIVE"' EXIT
  git -C "$ROOT" archive --format=tar --output="$ARCHIVE" "$COMMIT"
  tar -xf "$ARCHIVE" -C "$SNAPSHOT"
  uv sync --frozen --no-default-groups --project "$SNAPSHOT"
  uv pip install --python "$SNAPSHOT/.venv/bin/python" "docent==0.1.88"
  printf '%s\n' "$COMMIT" > "$SNAPSHOT/READY"
fi
mkdir -p "$STATE" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/evallab"
python3 - "$LABEL" "$DEST" "$SNAPSHOT" "$STATE" "$DB" "$INTERVAL" "$KEYS" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

label, dest, runtime, state, db, interval, keys = sys.argv[1:]
source, state, db = (str(Path(p).expanduser().resolve()) for p in (runtime, state, db))
logs = Path.home() / "Library/Logs/evallab"
config = {
    "Label": label,
    "ProgramArguments": [
        keys, "run", "--", source + "/.venv/bin/python", "-I", "-m", "evallab.readers.docent_watch",
        "--repo-root", source, "--db-path", db, "--state-dir", state,
        "--results-path", state + "/results-v1.jsonl",
    ],
    "WorkingDirectory": source,
    "StartInterval": int(interval), "RunAtLoad": True, "ProcessType": "Background",
    "LowPriorityIO": True, "Nice": 10,
    "EnvironmentVariables": {
        "HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "HARBOR_TELEMETRY": "off",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    },
    "StandardOutPath": str(logs / (label + ".log")),
    "StandardErrorPath": str(logs / (label + ".error.log")),
}
path = Path(dest)
if path.exists():
    previous = plistlib.loads(path.read_bytes())
    if previous.get("Label") != label or "evallab.readers.docent_watch" not in previous.get("ProgramArguments", []):
        raise SystemExit("refusing to overwrite a different service")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}\nruntime: {source}\ninterval: {interval}s; hosted-free only; one serial reading/tick")
PY
if [ "$LOAD" -eq 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$DEST"
  echo "loaded: $DOMAIN/$LABEL"
fi
