#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/subsequences.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def subsequences(a, b, k):
    if k == 0:
        return [[]]

    ret = []
    for i in range(a, b + 1 - k):
        ret.extend(
            [i] + rest for rest in subsequences(i + 1, b, k - 1)
        )

    return ret
QUIXBUGS_REFERENCE_SOLUTION_EOF
