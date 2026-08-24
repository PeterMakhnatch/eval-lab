#!/bin/sh
set -eu
mkdir -p /app/output
cat << 'EOF' > /app/output/result.jsonl
{"amount":444,"id":29,"status":"pending"}
{"amount":491,"id":14,"status":"shipped"}
EOF
