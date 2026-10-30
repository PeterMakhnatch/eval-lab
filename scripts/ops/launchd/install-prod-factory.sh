#!/bin/bash
# Install (or upgrade, or remove) the local DEV production factory and its watchdog.
#
# Builds an immutable release from a committed revision (git archive + uv sync
# --locked --no-dev, never an editing checkout), renders the two LaunchAgent
# templates with that release's paths, and (re)loads them. Re-running with a newer
# revision is the upgrade; the journal and every export under
# ~/.local/state/evallab-prod-live/factory are never touched.
#
#   install-prod-factory.sh [--rev <git rev>] [--uninstall]
#
# After a reboot launchd starts the service again (RunAtLoad + KeepAlive); the watchdog
# job writes factory/factory-watchdog.json every minute and exits 2 when the heartbeat
# is stalled, which the live supervisor's launchd source turns into an alert.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
REV=HEAD
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --rev) REV="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "usage: $0 [--rev <git rev>] [--uninstall]" >&2; exit 2 ;;
  esac
done
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.petermakhnatch.evallab.prod-factory com.petermakhnatch.evallab.prod-factory-watchdog)
if [ "$UNINSTALL" -eq 1 ]; then
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
  done
  echo "removed the factory LaunchAgents (journal, exports and releases retained)"
  exit 0
fi
SHA="$(git -C "$ROOT" rev-parse "$REV^{commit}")"
RELEASES="$HOME/.local/state/evallab-prod-live/releases"
RELEASE="$RELEASES/$SHA"
LOGS="$HOME/.local/state/evallab-prod-live/factory/logs"
mkdir -p "$LOGS" "$AGENTS"
if [ ! -x "$RELEASE/.venv/bin/python" ]; then
  mkdir -p "$RELEASE"
  git -C "$ROOT" archive --format=tar "$SHA" | tar -x -C "$RELEASE"
  (cd "$RELEASE" && unset VIRTUAL_ENV && uv sync --locked --no-dev --quiet)
fi
EVALLAB_DISABLE_LOCAL_DOCKER=1 "$RELEASE/.venv/bin/python" -m evallab.production.factory --help >/dev/null
for label in "${LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
    sleep 1
  done
  sed -e "s#__WORKDIR__#$RELEASE#g" -e "s#__HOME__#$HOME#g" -e "s#__LOG_DIR__#$LOGS#g" \
    "$RELEASE/scripts/ops/launchd/$label.plist" > "$AGENTS/$label.plist"
  plutil -lint "$AGENTS/$label.plist" >/dev/null
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
done
echo "factory release $SHA loaded:"
launchctl list | awk '/evallab.prod-factory/'
