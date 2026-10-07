#!/bin/bash
# Install the existing nightly label as a $0 refresh, without changing tick.
# Source/interpreter snapshots survive feature-worktree retirement.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STATE="$HOME/.local/state/evallab-nightly"
FACTS="$HOME/.local/state/daily-report"
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
DATA_ROOT="$(dirname "$COMMON")"
VERDICT_ROOT=""
LOAD=0
QUEUE_ROOTS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --state-dir) STATE="$2"; shift 2 ;;
    --facts-root) FACTS="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --verdict-root) VERDICT_ROOT="$2"; shift 2 ;;
    --queue-root) QUEUE_ROOTS+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
LABEL=com.petermakhnatch.evallab.nightly
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/evallab"
mkdir -p "$STATE" "$LOGS" "$(dirname "$DEST")"
if [ -e "$DEST" ] && [ ! -e "$DEST.bak-har199" ]; then
  cp -p "$DEST" "$DEST.bak-har199"
fi
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
# Refuse to publish a runtime whose code differs from the receipt's commit.
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]; then
  echo "commit changes before installing the nightly source snapshot" >&2; exit 1
fi
SNAPSHOT="$STATE/runtime/$COMMIT"
if [ ! -e "$SNAPSHOT/READY" ]; then
  mkdir -p "$SNAPSHOT"
  ARCHIVE="$(mktemp -t evallab-nightly)"
  trap 'rm -f "$ARCHIVE"' EXIT
  git -C "$ROOT" archive --format=tar --output="$ARCHIVE" "$COMMIT"
  tar -xf "$ARCHIVE" -C "$SNAPSHOT"
  uv sync --frozen --no-default-groups --project "$SNAPSHOT"
  printf '%s\n' "$COMMIT" > "$SNAPSHOT/READY"
fi
python3 - "$ROOT/scripts/ops/launchd/$LABEL.plist" "$DEST" "$SNAPSHOT" "$DATA_ROOT" "$STATE" "$FACTS" "$VERDICT_ROOT" "$LOGS" ${QUEUE_ROOTS[@]+"${QUEUE_ROOTS[@]}"} <<'PY'
import os
from pathlib import Path
import plistlib
import sys

template, destination, source, data, state, facts, verdict, logs, *roots = sys.argv[1:]
source = str(Path(source).resolve())
config = plistlib.loads(Path(template).read_bytes())
program = (
    "import sys; from pathlib import Path; from evallab.cli import run_cli; "
    f"sys.exit(run_cli(workspace=Path({source!r})))"
)
config["ProgramArguments"] = [
    source + "/.venv/bin/python", "-I", "-c", program,
    "nightly", "--refresh", "--data-root", str(Path(data).resolve()),
    "--state-dir", str(Path(state).resolve()), "--facts-root", str(Path(facts).resolve()),
]
if verdict:
    config["ProgramArguments"] += ["--verdict-root", str(Path(verdict).resolve())]
for root in roots:
    config["ProgramArguments"] += ["--queue-root", str(Path(root).resolve())]
config["WorkingDirectory"] = source
config["StandardOutPath"] = str(Path(logs) / "nightly-refresh.log")
config["StandardErrorPath"] = str(Path(logs) / "nightly-refresh.error.log")
config["EnvironmentVariables"]["HOME"] = str(Path.home())
config["EnvironmentVariables"]["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
path = Path(destination)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}")
print(f"runtime: {source}")
print(f"facts input: {facts}/inputs/evallab-nightly.json")
PY
if [ "$LOAD" -eq 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$DEST"
  echo "loaded: gui/$(id -u)/$LABEL; nightly 02:30 ET; no RunAtLoad"
fi
