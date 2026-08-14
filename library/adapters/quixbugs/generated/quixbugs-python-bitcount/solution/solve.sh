#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/bitcount.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def bitcount(n):
    count = 0
    while n:
        n &= n - 1
        count += 1
    return count
QUIXBUGS_REFERENCE_SOLUTION_EOF
