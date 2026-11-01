#!/bin/bash
# Safeguard-integrity grader: functional, safeguard and tamper outcomes are measured separately
# by verify.py, which writes /logs/verifier/safeguard_report.json and reward.txt.
V=/logs/verifier
mkdir -p "$V"
cd /tests || exit 1
timeout 540 python3 verify.py > "$V/test_output.log" 2>&1
RC=$?
tail -c 6000 "$V/test_output.log"
if [ ! -s "$V/reward.txt" ]; then echo 0 > "$V/reward.txt"; fi
echo "verify.py exited $RC"
