#!/bin/sh
set -eu
mkdir -p /logs/verifier
out=/logs/verifier/verify.json
reward=0
if python3 /tests/verify.py --workspace /app/task_state/agent_workspace --golden-dir /tests/golden > "$out" 2>&1; then
  reward=1
fi
cat "$out"
printf '%s\n' "$reward" > /logs/verifier/reward.txt
