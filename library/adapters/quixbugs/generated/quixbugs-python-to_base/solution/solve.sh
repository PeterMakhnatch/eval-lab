#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/to_base.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

import string
def to_base(num, b):
    result = ''
    alphabet = string.digits + string.ascii_uppercase
    while num > 0:
        i = num % b
        num = num // b
        result = alphabet[i] + result
    return result

"""
import string
def to_base(num, b):
    result = ''
    alphabet = string.digits + string.ascii_uppercase
    while num > 0:
        i = num % b
        num = num // b
        result = result + alphabet[i]
    return result[::-1]
"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
