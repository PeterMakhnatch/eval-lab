#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/sqrt.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def sqrt(x, epsilon):
    approx = x / 2
    while abs(x - approx ** 2) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx

"""
def sqrt(x, epsilon):
    approx = x / 2
    while abs(x - approx * approx) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx
"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
