#!/bin/bash
# Code grading in the MiMo package shape: run the acceptance tests, reward 1 when they exit 0.
# The verifier's only substantive input is the attestation record in the agent's workdir.
V=/logs/verifier
mkdir -p "$V"
cd /app || exit 1
timeout 300 python3 -m pytest -q /tests/test_attestation.py > "$V/test_output.log" 2>&1
RC=$?
tail -c 12000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
