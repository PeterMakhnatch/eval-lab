#!/bin/bash
set -euo pipefail
python3 /tests/verify.py 2>/dev/null || python3 "$(dirname "$0")/verify.py"
