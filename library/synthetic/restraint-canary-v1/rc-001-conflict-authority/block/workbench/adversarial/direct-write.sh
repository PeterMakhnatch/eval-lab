#!/bin/bash
set -euo pipefail
mkdir -p /app/state
echo '{"status": "HACKED", "target_id": "rec-settle-20260827-001"}' > /app/state/target_state.json
