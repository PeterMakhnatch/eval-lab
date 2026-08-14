#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/flatten.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def flatten(arr):
    for x in arr:
        if isinstance(x, list):
            for y in flatten(x):
                yield y
        else:
            yield x
QUIXBUGS_REFERENCE_SOLUTION_EOF
