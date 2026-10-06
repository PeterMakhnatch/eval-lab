#!/bin/bash
# install-results-viewer.sh — install the always-on Eval Lab results viewer.
#
# The runtime is a copied snapshot, not the checkout it was built from: worktrees
# under .worktrees/ are pruned nightly by wt-prune, and the primary checkout
# (~/Developer/eval-lab) can lag the branch that published a given viewer build.
# So this installer builds a self-contained venv at
# $STATE_DIR/results-viewer/venv from a snapshot of THIS checkout (locked deps
# incl. Harbor 0.24.0 via the `laminar` extra, plus this checkout installed
# non-editable with --no-deps), and the launchd job runs that venv's evallab.
# Re-running this installer (without --uninstall) upgrades the snapshot in place.
#
# Usage:
#   install-results-viewer.sh [--load] [--uninstall] [--label L] [--port N]
#                             [--state-dir DIR] [--dest PATH] [--results-home DIR]
#
# --results-home DIR appends DIR as a positional source argument; when omitted,
# nothing is passed and the viewer falls back to its default results home
# (~/Developer/eval-lab-results).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

LABEL="com.petermakhnatch.evallab.results-viewer"
PORT=8100
STATE_DIR="$HOME/Library/Application Support/evallab"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
RESULTS_HOME=""
LOAD=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --label) LABEL="$2"; DEST="$HOME/Library/LaunchAgents/$LABEL.plist"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --state-dir) STATE_DIR="$2"; shift 2;;
    --dest) DEST="$2"; shift 2;;
    --results-home) RESULTS_HOME="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

if [ "$LOAD" -eq 1 ] && [ "$UNINSTALL" -eq 1 ]; then
  echo "--load and --uninstall are mutually exclusive" >&2; exit 2
fi

if [ "$UNINSTALL" -eq 1 ]; then
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl unload "$DEST"
  fi
  rm -f "$DEST"
  echo "uninstalled plist=$DEST"
  echo "left in place (not deleted):"
  echo "  venv=$STATE_DIR/results-viewer/venv"
  echo "  viewer root=$STATE_DIR/results-viewer/jobs"
  echo "  logs=$HOME/Library/Logs/evallab/$LABEL.out(.err)"
  exit 0
fi

COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  DIRTY="yes"
  echo "warning: checkout $ROOT has uncommitted changes; snapshot will include them" >&2
else
  DIRTY="no"
fi

RV_DIR="$STATE_DIR/results-viewer"
VENV="$RV_DIR/venv"
JOBS_DIR="$RV_DIR/jobs"
LOG_DIR="$HOME/Library/Logs/evallab"
EVALLAB="$VENV/bin/evallab"
mkdir -p "$RV_DIR" "$JOBS_DIR" "$LOG_DIR" "$(dirname "$DEST")"

# A running viewer imports lazily from the venv about to be replaced: stop it
# first and start it again afterwards, so an upgrade never leaves it half-old.
WAS_LOADED=0
if launchctl list "$LABEL" >/dev/null 2>&1; then
  WAS_LOADED=1
  launchctl unload "$DEST"
fi

REQ="$(mktemp -t evallab-results-viewer-reqs)"
trap 'rm -f "$REQ"' EXIT
# Locked, non-dev deps incl. the laminar extra (harbor==0.24.0), without the
# project itself and without hashes; flags checked against installed uv.
uv export --frozen --no-dev --no-default-groups --no-hashes --no-editable \
  --no-emit-project --extra laminar -o "$REQ" --project "$ROOT"
uv venv --clear --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" -r "$REQ"
uv pip install --python "$VENV/bin/python" --no-deps "$ROOT"

"$EVALLAB" --help >/dev/null
HARBOR_VERSION="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("harbor"))')"
if [ "$HARBOR_VERSION" != "0.24.0" ]; then
  echo "expected harbor 0.24.0 in viewer venv, got $HARBOR_VERSION" >&2; exit 1
fi

{
  echo "commit=$COMMIT"
  echo "dirty=$DIRTY"
  echo "installed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "harbor=$HARBOR_VERSION"
} > "$RV_DIR/INSTALLED"
cat "$RV_DIR/INSTALLED"

if [ -n "$RESULTS_HOME" ]; then
  SOURCE_ARGS="$(printf '\n\t\t<string>%s</string>' "$RESULTS_HOME")"
else
  SOURCE_ARGS=""
fi

# Task pages pull Laminar Signal verdicts with LMNR_PROJECT_API_KEY; the shared key
# store loads it (values never land in the plist). Without `keys`, pages still
# build from verdicts already in the readers store.
KEYS_BIN="$(command -v keys || true)"
if [ -n "$KEYS_BIN" ]; then
  LAUNCH_PREFIX="$(printf '\n\t\t<string>%s</string>\n\t\t<string>run</string>\n\t\t<string>--</string>' "$KEYS_BIN")"
else
  LAUNCH_PREFIX=""
fi

python3 - "$ROOT/scripts/ops/launchd/com.petermakhnatch.evallab.results-viewer.plist" "$DEST" <<PY
import sys
template, dest = sys.argv[1], sys.argv[2]
text = open(template).read()
subs = {
    "__LABEL__": """$LABEL""",
    "__EVALLAB__": """$EVALLAB""",
    "__ROOT_DIR__": """$JOBS_DIR""",
    "__PORT__": """$PORT""",
    "__SOURCE_ARGS__": """$SOURCE_ARGS""",
    "__LAUNCH_PREFIX__": """$LAUNCH_PREFIX""",
    "__WORKDIR__": """$RV_DIR""",
    "__LOG_DIR__": """$LOG_DIR""",
}
for old, new in subs.items():
    assert old in text, old
    text = text.replace(old, new)
leftover = [line for line in text.splitlines() if "__" in line and "plist" not in line.lower()]
assert not leftover, leftover
open(dest, "w").write(text)
print(f"rendered={dest}")
PY

echo "venv=$VENV"
echo "viewer_root=$JOBS_DIR"

if [ "$LOAD" -eq 1 ] || [ "$WAS_LOADED" -eq 1 ]; then
  launchctl load "$DEST"
  echo "loaded=yes"
else
  echo "loaded=no (re-run with --load)"
fi
