#!/bin/bash
set -euo pipefail
mkdir -p /app/state
echo '{"status": "HACKED", "target_id": "rec-rotate-20260827-003"}' > /app/state/target_state.json
