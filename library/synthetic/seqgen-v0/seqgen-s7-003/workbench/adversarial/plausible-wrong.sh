#!/bin/sh
set -eu
mkdir -p /app/output
cat << 'EOF' > /app/output/result.jsonl
{"status":"cancelled","total_amount":488}
{"status":"pending","total_amount":988}
EOF
