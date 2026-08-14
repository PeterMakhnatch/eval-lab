#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/get_factors.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def get_factors(n):
    if n == 1:
        return []

    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return [i] + get_factors(n // i)

    return [n]

"""
def get_factors(n):
    if n == 1:
        return []

    for i in range(2, n + 1):
        if n % i == 0:
            return [i] + get_factors(n // i)

    return []
"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
