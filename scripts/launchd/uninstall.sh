#!/bin/bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.petermakhnatch.evallab.digest.plist"

if [[ -f "${PLIST_DEST}" ]]; then
    launchctl bootout "gui/$(id -u)" "${PLIST_DEST}" 2>/dev/null || true
    rm -f "${PLIST_DEST}"
    echo "Unloaded and removed com.petermakhnatch.evallab.digest"
else
    echo "Plist not found at ${PLIST_DEST}; agent already uninstalled."
fi
