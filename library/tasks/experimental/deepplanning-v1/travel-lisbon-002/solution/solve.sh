#!/bin/bash
set -euo pipefail
python3 /solution/solve.py 2>/dev/null || python3 "$(dirname "$0")/solve.py"
