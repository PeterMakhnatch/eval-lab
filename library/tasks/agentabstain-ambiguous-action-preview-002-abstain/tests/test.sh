#!/bin/sh
set -eu
mkdir -p /logs/verifier
if python3 /tests/verify.py; then
  printf '1\n' > /logs/verifier/reward.txt
else
  printf '0\n' > /logs/verifier/reward.txt
fi
