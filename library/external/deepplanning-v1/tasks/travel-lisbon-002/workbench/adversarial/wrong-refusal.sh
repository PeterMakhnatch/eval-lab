#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\n' '{"status":"infeasible","refusal_reason":"budget exceeded","acquired_sources":["flight-lis","hotel-baixa","museum-pass"]}' > "$target"
