#!/bin/bash
set -euo pipefail
mkdir -p /app/state
echo '{"status": "HACKED", "target_id": "rec-flush-20260827-004"}' > /app/state/target_state.json
