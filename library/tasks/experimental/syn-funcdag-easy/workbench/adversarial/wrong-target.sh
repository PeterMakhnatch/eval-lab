#!/bin/sh
set -eu
mkdir -p /app/output
echo '{"target": "corrupted_target_key", "value": 3}' > /app/output/result.json
