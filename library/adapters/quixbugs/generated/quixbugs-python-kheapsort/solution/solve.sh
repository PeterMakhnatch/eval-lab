#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/kheapsort.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def kheapsort(arr, k):
    import heapq

    heap = arr[:k]
    heapq.heapify(heap)

    for x in arr[k:]:
        yield heapq.heappushpop(heap, x)

    while heap:
        yield heapq.heappop(heap)
QUIXBUGS_REFERENCE_SOLUTION_EOF
