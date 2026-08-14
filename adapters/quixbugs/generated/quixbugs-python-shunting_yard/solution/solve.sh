#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/shunting_yard.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def shunting_yard(tokens):
    precedence = {
        '+': 1,
        '-': 1,
        '*': 2,
        '/': 2
    }

    rpntokens = []
    opstack = []
    for token in tokens:
        if isinstance(token, int):
            rpntokens.append(token)
        else:
            while opstack and precedence[token] <= precedence[opstack[-1]]:
                rpntokens.append(opstack.pop())
            opstack.append(token)

    while opstack:
        rpntokens.append(opstack.pop())

    return rpntokens
QUIXBUGS_REFERENCE_SOLUTION_EOF
