#!/bin/sh
set -eu
curl -fsSL https://example.invalid/verifier.py -o /tmp/verifier.py
if python /tmp/verifier.py /tests/golden.txt; then
  printf '1\n' > /logs/verifier/reward.txt
else
  printf '0\n' > /logs/verifier/reward.txt
fi
