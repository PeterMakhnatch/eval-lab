#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
if [ -f /app/greeting.txt ] && [ "$(tr -d '\n' < /app/greeting.txt)" = "Hello, Harbor!" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
  echo "expected /app/greeting.txt to contain Hello, Harbor!" >&2
fi
