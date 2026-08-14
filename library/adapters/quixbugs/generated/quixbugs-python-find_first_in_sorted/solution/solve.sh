#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/find_first_in_sorted.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)

    while lo < hi:
        mid = (lo + hi) // 2

        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid

        elif x <= arr[mid]:
            hi = mid

        else:
            lo = mid + 1

    return -1

"""
def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)

    while lo <= hi - 1:
        mid = (lo + hi) // 2

        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid

        elif x <= arr[mid]:
            hi = mid

        else:
            lo = mid + 1

    return -1

def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)

    while lo + 1 <= hi:
        mid = (lo + hi) // 2

        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid

        elif x <= arr[mid]:
            hi = mid

        else:
            lo = mid + 1

    return -1

"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
