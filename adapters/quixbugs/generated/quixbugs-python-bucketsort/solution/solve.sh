#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/bucketsort.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def bucketsort(arr, k):
    counts = [0] * k
    for x in arr:
        counts[x] += 1

    sorted_arr = []
    for i, count in enumerate(counts):
        sorted_arr.extend([i] * count)

    return sorted_arr

"""
def bucketsort(arr, k):
    counts = [0] * k
    for x in arr:
        counts[x] += 1

    sorted_arr = []
    for i, count in enumerate(arr):
        sorted_arr.extend([i] * counts[i])

    return sorted_arr
"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
