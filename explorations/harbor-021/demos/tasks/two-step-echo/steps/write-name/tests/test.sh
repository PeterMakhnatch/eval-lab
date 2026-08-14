#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
if [ -f /app/name.txt ] && [ "$(tr -d '\n' < /app/name.txt)" = "Harbor" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
  echo "expected /app/name.txt to contain Harbor" >&2
fi
