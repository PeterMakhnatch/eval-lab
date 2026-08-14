#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/wrap.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def wrap(text, cols):
    lines = []
    while len(text) > cols:
        end = text.rfind(' ', 0, cols + 1)
        if end == -1:
            end = cols
        line, text = text[:end], text[end:]
        lines.append(line)

    lines.append(text)
    return lines
QUIXBUGS_REFERENCE_SOLUTION_EOF
