#!/bin/bash
# install-sync-primary.sh — install the primary-checkout sync timer.
#
# Copies scripts/sync-primary-checkout.sh to a stable home outside the repo
# (so the timer never depends on the checkout it syncs), renders the launchd
# plist with absolute paths, and optionally loads it.
#
# The installed copy is deliberate: after this branch merges, ~/Developer/eval-lab
# carries the script too, but the timer must keep working even while the primary
# checkout is 30+ commits behind. Re-run this installer after changing the script;
# it prints the installed sha256 so drift is visible.
#
# Usage:
#   install-sync-primary.sh [--load] [--label L] [--repo PATH]
#                           [--interval SECONDS] [--state-dir DIR] [--dest PATH]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

LABEL="com.petermakhnatch.evallab.sync-primary"
REPO="$HOME/Developer/eval-lab"
INTERVAL=300
STATE_DIR="$HOME/Library/Application Support/evallab"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOAD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift;;
    --label) LABEL="$2"; DEST="$HOME/Library/LaunchAgents/$LABEL.plist"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --state-dir) STATE_DIR="$2"; shift 2;;
    --dest) DEST="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

BIN_DIR="$STATE_DIR/bin"
LOG_DIR="$HOME/Library/Logs/evallab"
mkdir -p "$BIN_DIR" "$LOG_DIR" "$STATE_DIR"
cp "$ROOT/scripts/sync-primary-checkout.sh" "$BIN_DIR/sync-primary-checkout.sh"
chmod +x "$BIN_DIR/sync-primary-checkout.sh"

python3 - "$ROOT/scripts/ops/launchd/com.petermakhnatch.evallab.sync-primary.plist" "$DEST" <<PY
import sys
template, dest = sys.argv[1], sys.argv[2]
text = open(template).read()
subs = {
    "__LABEL__": """$LABEL""",
    "__SCRIPT__": """$BIN_DIR/sync-primary-checkout.sh""",
    "__REPO__": """$REPO""",
    "__STATE_DIR__": """$STATE_DIR""",
    "__LOG_DIR__": """$LOG_DIR""",
    "__INTERVAL__": """$INTERVAL""",
    "__NOTIFY_EVERY__": "21600",
}
for old, new in subs.items():
    assert old in text, old
    text = text.replace(old, new)
leftover = [line for line in text.splitlines() if "__" in line and "plist" not in line.lower()]
assert not leftover, leftover
open(dest, "w").write(text)
print(f"rendered={dest}")
PY

echo "script=$BIN_DIR/sync-primary-checkout.sh"
echo -n "sha256="
shasum -a 256 "$BIN_DIR/sync-primary-checkout.sh" | cut -d' ' -f1

if [ "$LOAD" -eq 1 ]; then
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl unload "$DEST"
  fi
  launchctl load "$DEST"
  echo "loaded=yes"
else
  echo "loaded=no (re-run with --load)"
fi
