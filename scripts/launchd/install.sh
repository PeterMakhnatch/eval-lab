#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    if [[ -x "$HOME/.cargo/bin/uv" ]]; then
        UV_BIN="$HOME/.cargo/bin/uv"
    elif [[ -x "$HOME/.local/bin/uv" ]]; then
        UV_BIN="$HOME/.local/bin/uv"
    elif [[ -x "/opt/homebrew/bin/uv" ]]; then
        UV_BIN="/opt/homebrew/bin/uv"
    elif [[ -x "/usr/local/bin/uv" ]]; then
        UV_BIN="/usr/local/bin/uv"
    else
        echo "Error: uv executable not found in PATH" >&2
        exit 1
    fi
fi

LOGS_DIR="${REPO_ROOT}/runs/logs"
mkdir -p "${LOGS_DIR}"
mkdir -p "$HOME/Library/LaunchAgents"

PLIST_NAME="com.petermakhnatch.evallab.digest.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/${PLIST_NAME}"

cat <<EOF > "${TARGET_PLIST}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.petermakhnatch.evallab.digest</string>
	<key>RunAtLoad</key>
	<true/>
	<key>WorkingDirectory</key>
	<string>${REPO_ROOT}</string>
	<key>ProgramArguments</key>
	<array>
		<string>${UV_BIN}</string>
		<string>run</string>
		<string>python</string>
		<string>${REPO_ROOT}/scripts/daily_digest.py</string>
	</array>
	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>7</integer>
		<key>Minute</key>
		<integer>0</integer>
	</dict>
	<key>StartInterval</key>
	<integer>14400</integer>
	<key>StandardOutPath</key>
	<string>${LOGS_DIR}/digest.out</string>
	<key>StandardErrorPath</key>
	<string>${LOGS_DIR}/digest.err</string>
</dict>
</plist>
EOF

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}" "${TARGET_PLIST}" 2>/dev/null || launchctl bootout "gui/${UID_NUM}/${PLIST_NAME}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "${TARGET_PLIST}"

echo "Installed and bootstrapped com.petermakhnatch.evallab.digest"
echo "Verify status with:"
echo "  launchctl print gui/\$(id -u)/com.petermakhnatch.evallab.digest"
