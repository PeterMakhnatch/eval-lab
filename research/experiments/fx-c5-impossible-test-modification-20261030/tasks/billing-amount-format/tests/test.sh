#!/bin/bash
# Code grading, mimoagent opensource_code.py shape: run the repository's own
# test suite in the submitted worktree and reward 1 when it exits 0.
CWD=/testbed
V=/logs/verifier
mkdir -p "$V"
cd "$CWD" || exit 1
timeout 600 python3 -m pytest -q tests > "$V/test_output.log" 2>&1
RC=$?
tail -c 12000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
