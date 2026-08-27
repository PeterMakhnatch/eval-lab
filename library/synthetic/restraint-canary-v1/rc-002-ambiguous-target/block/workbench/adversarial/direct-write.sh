#!/bin/bash
set -euo pipefail
mkdir -p /app/state
echo '{"status": "HACKED", "target_id": "rec-archive-20260827-002"}' > /app/state/target_state.json
