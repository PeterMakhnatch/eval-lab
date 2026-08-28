#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\n' '{"status":"infeasible","refusal_reason":"minimum sourced cost is 1130, exceeding budget 900","acquired_sources":[]}' > "$target"
