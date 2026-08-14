#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/hanoi.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def hanoi(height, start=1, end=3):
    steps = []
    if height > 0:
        helper = ({1, 2, 3} - {start} - {end}).pop()
        steps.extend(hanoi(height - 1, start, helper))
        steps.append((start, end))
        steps.extend(hanoi(height - 1, helper, end))

    return steps
QUIXBUGS_REFERENCE_SOLUTION_EOF
