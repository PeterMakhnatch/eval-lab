#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 exec python3 -B -u - <<'PY'
def run_policy(send, token):
    return None


if __name__ == "__main__":
    run_policy(None, "")
PY
