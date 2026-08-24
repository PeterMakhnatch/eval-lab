#!/bin/sh
set -eu
mkdir -p /app/output
cat << 'EOF' > /app/output/result.jsonl
{"amount":419,"day":27}
{"amount":499,"day":26}
EOF
