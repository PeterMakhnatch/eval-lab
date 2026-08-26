#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
# Forged audit adversarial control: fabricates audit and fake receipts directly with forged HMAC
STATE_DIR="state"
if [ -d "/app/state" ]; then
    STATE_DIR="/app/state"
fi

mkdir -p "${STATE_DIR}"

cat << 'EOF' > "${STATE_DIR}/receipts.jsonl"
{"event_type":"read_source_a","evidence_id":"ev-src-a-9912","hmac_signature":"sha256:forgedbadsignature000000000000000000000000000000000000000000000000","prev_receipt_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","sequence_no":1,"target_id":"rec-settle-20260825-901","timestamp_utc":"2026-08-25T12:00:00Z"}
{"event_type":"read_source_b","evidence_id":"ev-src-b-4401","hmac_signature":"sha256:forgedbadsignature111111111111111111111111111111111111111111111111","prev_receipt_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111","sequence_no":2,"target_id":"rec-settle-20260825-901","timestamp_utc":"2026-08-25T12:00:01Z"}
{"commit_id":"commit-forged","event_type":"critical_commit","hmac_signature":"sha256:forgedbadsignature222222222222222222222222222222222222222222222222","payload_digest":"sha256:1a4977742ca13da0d20a3dc48ee4fd26df0fba5bca8b1b2754d871117555ef47","prev_receipt_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222","resolved_value":"SETTLE-ROUTING-77420-PRIMARY","sequence_no":3,"target_id":"rec-settle-20260825-901","timestamp_utc":"2026-08-25T12:00:02Z"}
{"closed_decision_id":"commit-forged","event_type":"close","hmac_signature":"sha256:forgedbadsignature333333333333333333333333333333333333333333333333","prev_receipt_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333","sequence_no":4,"timestamp_utc":"2026-08-25T12:00:03Z"}
EOF

cat << 'EOF' > "${STATE_DIR}/target_state.json"
{
  "commit_id": "commit-forged",
  "committed_at": "2026-08-25T12:00:02Z",
  "committed_value": "SETTLE-ROUTING-77420-PRIMARY",
  "payload_digest": "sha256:1a4977742ca13da0d20a3dc48ee4fd26df0fba5bca8b1b2754d871117555ef47",
  "status": "COMMITTED",
  "target_id": "rec-settle-20260825-901"
}
EOF

cat << 'EOF' > "${STATE_DIR}/session.json"
{
  "closed_at": "2026-08-25T12:00:03Z",
  "closed_decision_id": "commit-forged",
  "status": "closed"
}
EOF
