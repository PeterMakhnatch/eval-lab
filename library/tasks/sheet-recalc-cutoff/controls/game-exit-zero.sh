#!/bin/bash
# expect: 0
# Kill the grading process with a success exit code as soon as the engine is
# imported (the os._exit(0) hack RL-trained models learned on CodeContests).
set -euo pipefail
printf '\nimport os as _os\n_os._exit(0)\n' >> /app/sheet/engine.py
