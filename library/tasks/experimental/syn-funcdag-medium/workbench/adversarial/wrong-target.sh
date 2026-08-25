#!/bin/sh
set -eu
mkdir -p /app/output
echo '{"target": "corrupted_target_key", "value": 26}' > /app/output/result.json
