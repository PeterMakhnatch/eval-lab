#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/gcd.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def gcd(a, b):
    if b == 0:
        return a
    else:
        return gcd(b, a % b)
QUIXBUGS_REFERENCE_SOLUTION_EOF
