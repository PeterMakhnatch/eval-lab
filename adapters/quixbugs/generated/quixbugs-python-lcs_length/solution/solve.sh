#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/lcs_length.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def lcs_length(s, t):
    from collections import Counter

    dp = Counter()

    for i in range(len(s)):
        for j in range(len(t)):
            if s[i] == t[j]:
                dp[i, j] = dp[i - 1, j - 1] + 1

    return max(dp.values()) if dp else 0
QUIXBUGS_REFERENCE_SOLUTION_EOF
