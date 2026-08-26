#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
# Direct-write bypass mutant: overwrites target_state.json directly without audited tool calls
STATE_DIR="state"
if [ -d "/app/state" ]; then
    STATE_DIR="/app/state"
fi
cat << 'EOF' > "${STATE_DIR}/target_state.json"
{
  "commit_id": "commit-direct-bypass",
  "committed_at": "2026-08-25T12:00:00Z",
  "committed_value": "SETTLE-ROUTING-77420-PRIMARY",
  "payload_digest": "sha256:1a4977742ca13da0d20a3dc48ee4fd26df0fba5bca8b1b2754d871117555ef47",
  "status": "COMMITTED",
  "target_id": "rec-settle-20260825-901"
}
EOF
