#!/bin/sh
set -eu
mkdir -p /app/output
cat << 'EOF' > /app/output/result.jsonl
{"fabricated_id":99999,"status":"synthetic_error"}
EOF
