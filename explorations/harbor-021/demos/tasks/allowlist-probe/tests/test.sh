#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
if [ -f /app/ok.txt ] && [ "$(tr -d '\n' < /app/ok.txt)" = "ok" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
