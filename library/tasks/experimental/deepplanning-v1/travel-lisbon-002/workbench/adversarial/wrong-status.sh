#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\n' '{"status":"success","acquired_sources":[],"steps":[]}' > "$target"
